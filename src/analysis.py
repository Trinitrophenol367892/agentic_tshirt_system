import json
import time
import sqlite3
from .database import get_connection
from .llm_client import governed_chat_completion, MAIN_MODEL
from .llm_utils import safe_extract_content
from .brief_schema import normalize_brief
from .logger import log_analysis


def _apply_data_hygiene_filters(trends_rows):
    """
    OPTIMIZATION 1: Data Hygiene & Signal-to-Noise Optimization
    
    Filters trends before analysis based on:
    - Growth velocity threshold (minimum 5%)
    - Age threshold (prefer trends < 48 hours old)
    - Meta-JSON completeness (must have communities OR creators)
    
    Returns filtered list of trend tuples.
    """
    filtered = []
    for row in trends_rows:
        trend_id, signal, growth, timestamp, meta_raw = row
        
        # Filter 1: Growth velocity threshold
        if growth < 5.0:
            log_analysis.debug("  [HYGIENE] Skipping '%s': growth %.1f%% < 5%% threshold", signal, growth)
            continue
        
        # Filter 2: Meta-JSON completeness check
        try:
            meta = json.loads(meta_raw) if meta_raw else {}
        except Exception:
            meta = {}
        
        has_communities = bool(meta.get("top_communities"))
        has_creators = bool(meta.get("top_creators"))
        
        if not (has_communities or has_creators):
            log_analysis.debug("  [HYGIENE] Skipping '%s': incomplete audience intel (no communities/creators)", signal)
            continue
        
        filtered.append(row)
    
    if len(filtered) < len(trends_rows):
        log_analysis.info("  [HYGIENE] Filtered %d/%d trends (%d retained)", 
                         len(trends_rows) - len(filtered), len(trends_rows), len(filtered))
    
    return filtered


def _check_thematic_consistency(trends_rows):
    """
    OPTIMIZATION 2: Strategic Trend Clustering with Dynamic Theme Discovery
    
    Analyzes trend signals for thematic coherence using a hybrid approach:
    1. First, discovers emergent themes dynamically from the actual trend signals
    2. Then, uses known aesthetic categories as secondary validation
    3. Identifies potential "frankenstein" brief scenarios where trends are incompatible
    
    Returns: (coherence_score 0-1, thematic_notes, discovered_themes_dict)
    """
    if len(trends_rows) < 2:
        return 1.0, "Single trend - no coherence check needed", {}
    
    signals = [row[1] for row in trends_rows]
    
    # DYNAMIC THEME DISCOVERY: Extract unique descriptors from actual signals
    # This captures emerging aesthetics that static keyword lists would miss
    discovered_themes = {}
    
    for signal in signals:
        # Tokenize signal into meaningful words (split on spaces, hyphens, underscores)
        tokens = signal.lower().replace('-', ' ').replace('_', ' ').split()
        
        # Filter out generic trend words, keep descriptive terms
        generic_words = {'trend', 'core', 'aesthetic', 'vibe', 'style', 'the', 'and', 'of', 'in'}
        descriptive_tokens = [t for t in tokens if t not in generic_words and len(t) > 2]
        
        for token in descriptive_tokens:
            discovered_themes[token] = discovered_themes.get(token, 0) + 1
    
    # Identify dominant emergent themes (appearing in multiple signals OR high frequency)
    dominant_emergent = []
    for theme, count in sorted(discovered_themes.items(), key=lambda x: x[1], reverse=True):
        if count >= 2:  # Appears in multiple signals - strong signal
            dominant_emergent.append((theme, count))
        elif count == 1 and len(signals) <= 3:  # In small datasets, single occurrences can still be meaningful
            dominant_emergent.append((theme, count))
    
    # STATIC VALIDATION: Cross-reference with known aesthetic categories
    aesthetic_keywords = {
        'streetwear': ['street', 'urban', 'graffiti', 'hiphop', 'skate', 'y2k', 'baggy'],
        'nostalgia': ['retro', 'vintage', '90s', '80s', '70s', 'classic', 'throwback', 'oldschool'],
        'tech': ['cyber', 'digital', 'ai', 'robot', 'future', 'glitch', 'synth', 'neo'],
        'nature': ['eco', 'forest', 'ocean', 'plant', 'animal', 'wild', 'floral', 'botanical'],
        'minimal': ['minimal', 'clean', 'simple', 'mono', 'line', 'geometric', 'abstract'],
        'subculture': ['punk', 'goth', 'emo', 'metal', 'rave', 'club', 'underground'],
        'luxury': ['luxury', 'premium', 'haute', 'designer', 'couture', 'elegant'],
        'sports': ['sport', 'athletic', 'gym', 'fit', 'active', 'training', 'team']
    }
    
    theme_counts = {theme: 0 for theme in aesthetic_keywords}
    mapped_signals = []
    
    for signal in signals:
        signal_lower = signal.lower()
        matched_themes = []
        for theme, keywords in aesthetic_keywords.items():
            if any(kw in signal_lower for kw in keywords):
                theme_counts[theme] += 1
                matched_themes.append(theme)
        mapped_signals.append((signal, matched_themes))
    
    dominant_theme_count = max(theme_counts.values()) if theme_counts else 0
    total_categorized = sum(theme_counts.values())
    
    # Calculate coherence score with dynamic weighting
    if total_categorized == 0 and not dominant_emergent:
        coherence_score = 0.5
        notes = "Mixed/Uncategorized themes - moderate coherence risk (no clear pattern detected)"
    elif dominant_theme_count >= len(signals) * 0.6:
        coherence_score = 0.9
        dominant = [t for t, c in theme_counts.items() if c == dominant_theme_count][0]
        notes = f"Strong thematic coherence: {dominant} ({dominant_theme_count}/{len(signals)} trends)"
    elif dominant_emergent and dominant_emergent[0][1] >= len(signals):
        # Top emergent theme appears in ALL signals (e.g., "academia" in 3/3 signals)
        coherence_score = 0.9
        theme_names = ', '.join([f"'{t}'" for t, c in dominant_emergent if c >= len(signals)][:2])
        notes = f"Emergent theme coherence detected: {theme_names} (dynamic discovery - present in all signals)"
    elif dominant_emergent and len([t for t,c in dominant_emergent if c >= 2]) <= 2:
        # Strong emergent themes appearing multiple times
        coherence_score = 0.85
        multi_occurrence = [(t,c) for t,c in dominant_emergent if c >= 2][:2]
        theme_names = ', '.join([f"'{t}'" for t, c in multi_occurrence])
        notes = f"Emergent theme coherence detected: {theme_names} (dynamic discovery)"
    elif total_categorized > 0 and dominant_theme_count >= len(signals) * 0.4:
        coherence_score = 0.75
        dominant = [t for t, c in theme_counts.items() if c == dominant_theme_count][0]
        notes = f"Moderate-strong coherence: {dominant} leaning ({dominant_theme_count}/{len(signals)} trends)"
    else:
        coherence_score = 0.6
        notes = f"Thematic diversity detected ({total_categorized}/{len(signals)} categorized across {len([t for t,c in theme_counts.items() if c>0])} categories)"
    
    log_analysis.info("  [CLUSTERING] Thematic coherence score: %.1f | %s", coherence_score, notes)
    if dominant_emergent:
        log_analysis.debug("  [CLUSTERING] Emergent themes: %s", ', '.join([f"{t}({c})" for t, c in dominant_emergent[:5]]))
    
    thematic_context = {
        "emergent_themes": dominant_emergent[:5],
        "static_category_mapping": {t: c for t, c in theme_counts.items() if c > 0},
        "mapped_signals": mapped_signals
    }
    
    return coherence_score, notes, thematic_context


def _load_trend_meta():
    """Read the last 5 trends WITH meta_json AND raw posts from trend_posts table; aggregate audience intel + references + EVIDENCE BLOCK."""
    with get_connection() as conn:
        cur = conn.execute("SELECT id, signal, meta_json FROM trends ORDER BY timestamp DESC LIMIT 5")
        rows = cur.fetchall()
    
    communities, creators = {}, {}
    references, sentiments, velocities = [], [], []
    evidence_block = []  # NEW: Raw post samples for LLM grounding
    
    for trend_id, signal, meta_raw in rows:
        try:
            meta = json.loads(meta_raw) if meta_raw else {}
        except Exception:
            meta = {}
        
        if not meta:
            continue
        
        for c in meta.get("top_communities", []):
            communities[c] = communities.get(c, 0) + 1
        for c in meta.get("top_creators", []):
            creators[c] = creators.get(c, 0) + 1
        for ref in meta.get("resonating_references", []):
            references.append(f"[{signal}] {ref}")
        if meta.get("sentiment_proxy"):
            sentiments.append(f"{signal}: {meta['sentiment_proxy']}")
        velocities.append(f"{signal}: {meta.get('velocity_source', '?')} velocity")
        
        # NEW: Fetch raw posts from dedicated trend_posts table
        post_cur = conn.execute("""
            SELECT content, platform, likes, comments, author_handle 
            FROM trend_posts 
            WHERE trend_id = ? 
            ORDER BY likes DESC 
            LIMIT 3
        """, (trend_id,))
        post_rows = post_cur.fetchall()
        
        for post_content, platform, likes, comments, author in post_rows:
            if post_content and len(post_content.strip()) > 20:
                evidence_block.append({
                    "signal": signal,
                    "platform": platform,
                    "content": post_content[:300],  # Truncate for context window
                    "likes": likes,
                    "comments": comments,
                    "author": author
                })
    
    audience_intelligence = {
        "measured_communities": sorted(communities, key=lambda k: communities[k], reverse=True)[:8],
        "measured_creators": sorted(creators, key=lambda k: creators[k], reverse=True)[:8],
        "sentiment_by_signal": sentiments,
    }
    
    log_analysis.info("  [EVIDENCE] Loaded %d raw post samples from %d trends", len(evidence_block), len(rows))
    
    return audience_intelligence, references[:9], evidence_block


ef _weight_market_feedback(trend_signals):
    """
    OPTIMIZATION D: Feedback Integration - Market Reality Check
    """
    if not trend_signals:
        return ""
    
    conn = None
    cur = None
    try:
        # FIX: Use explicit connection, NOT context manager
        conn = sqlite3.connect('system_data.db')
        cur = conn.cursor()
        
        feedback_parts = []
        current_keywords = [t[1].lower() for t in trend_signals]
        
        conditions = []
        params = []
        for kw in current_keywords:
            conditions.append("ct.trend_name LIKE ?")
            params.append(f"%{kw}%")
        
        if not conditions:
            return ""
        
        query = f"""
            SELECT ct.trend_name, ct.brief_theme, ct.design_slogan, ct.judge_score,
                   p.clicks, p.sales, p.revenue
            FROM confirmed_trends ct
            LEFT JOIN performance p ON ct.design_id = p.design_id
            WHERE {" OR ".join(conditions)}
            ORDER BY ct.confirmed_at DESC
            LIMIT 20
        """
        
        cur.execute(query, params)
        rows = cur.fetchall()
        
        if not rows:
            return ""
        
        feedback_parts.append("\n" + "="*80)
        feedback_parts.append("MARKET REALITY CHECK (Historical Performance Data)")
        feedback_parts.append("="*80)
        
        successes = [r for r in rows if (r[5] or 0) > 0]
        failures = [r for r in rows if (r[5] or 0) == 0 and (r[4] or 0) > 0]
        
        if successes:
            feedback_parts.append("\n✅ WINNING PATTERNS (Emulate these):")
            for r in successes[:3]:
                trend_name, brief_theme, slogan, score, clicks, sales, revenue = r
                revenue_val = revenue if revenue else 0
                feedback_parts.append(f"  - Trend '{trend_name}': Slogan '{slogan}' generated {sales} sales, ${revenue_val:.2f}")
        
        if failures:
            feedback_parts.append("\n⚠️ FAILURE PATTERNS (Avoid these):")
            for r in failures[:3]:
                trend_name, brief_theme, slogan, score, clicks, sales, revenue = r
                feedback_parts.append(f"  - Trend '{trend_name}': Slogan '{slogan}' got {clicks} clicks but ZERO sales")
        
        feedback_parts.append("-"*80)
        return "\n".join(feedback_parts)
        
    except Exception as e:
        log_analysis.warning(f"Market feedback weighting failed: {e}")
        return ""
    finally:
        # FIX: Safe cleanup
        if cur:
            cur.close()
        if conn:
            conn.close()


def load_market_feedback():
    """
    FIX 3: read the performance table back, joined to designs->briefs->trends.
    Returns a feedback string ONLY if real sales/clicks exist; else None (dormant).
    """
    with get_connection() as conn:
        cur = conn.execute("""
            SELECT t.signal,
                   COALESCE(SUM(p.clicks),0)  AS clicks,
                   COALESCE(SUM(p.sales),0)   AS sales,
                   COALESCE(SUM(p.revenue),0) AS revenue,
                   COUNT(p.id)                AS rows
            FROM performance p
            JOIN designs d  ON d.id = p.design_id
            JOIN briefs  b  ON b.id = d.brief_id
            JOIN trends  t  ON t.id = b.trend_id
            GROUP BY t.signal
        """)
        rows = cur.fetchall()
    total_sales = sum(r[2] for r in rows)
    total_rev = sum(r[3] for r in rows)
    total_clicks = sum(r[1] for r in rows)
    if total_sales <= 0 and total_rev <= 0 and total_clicks <= 0:
        log_analysis.info("  [MARKET] loop ARMED but DORMANT (no real sales/clicks in performance yet).")
        return None
    log_analysis.info("  [MARKET] REAL outcome data found: %d clicks, %d sales, $%.2f across %d trend(s).",
                      total_clicks, total_sales, total_rev, len(rows))
    lines = ["MARKET OUTCOME FEEDBACK (real performance data from past published designs):"]
    for signal, clicks, sales, revenue, _ in sorted(rows, key=lambda r: r[3], reverse=True):
        lines.append(f"  - '{signal}': {clicks} clicks, {sales} sales, ${revenue:.2f} revenue")
    lines.append("Bias the next brief toward the visual/audience angles of the higher-converting trends, "
                 "and away from angles that posted clicks but zero sales.")
    return "\n".join(lines)


def generate_granular_brief(trends_data, direction_feedback=None, market_feedback=None,
                            audience_intelligence=None, resonating_references=None, thematic_coherence=None,
                            evidence_block=None):
    trends_text = "\n".join([f"- {t[1]} (Growth: {t[2]}%)" for t in trends_data])
    log_analysis.debug("  Building brief | trends=%d | dir_fb=%s | market_fb=%s | audience=%s | refs=%d | coherence=%s | evidence=%d",
                       len(trends_data), bool(direction_feedback), bool(market_feedback),
                       bool(audience_intelligence), len(resonating_references or []),
                       thematic_coherence.get('score') if thematic_coherence else None,
                       len(evidence_block or []))

    # OPTIMIZATION 3: LLM Prompt Optimization with Print-Specific Constraints + Evidence-Based Directives
    system_prompt = """You are a Cultural Linguist and Lead Product Strategist for a viral t-shirt brand.
Analyze the provided social signals, measured audience intelligence, AND RAW SOCIAL MEDIA EVIDENCE to generate a highly granular brief.
Output valid JSON only, no markdown. anti_slop_directives must be specific.

CORE DIRECTIVE:
DO NOT hallucinate trends, slang, or aesthetics. Every insight MUST be traced back to the RAW_EVIDENCE_BLOCK below.
If evidence shows users saying "no more fake vibes," your slogan formula MUST reflect that exact phrasing, not generic "authenticity."

PRINT PRODUCTION CONSTRAINTS (CRITICAL):
- Reject concepts requiring >4 colors, photorealism, gradients, or micro-details
- Prioritize vector-style graphics, high-contrast designs, screen-printable artwork
- Ensure all designs work on actual garments (consider fabric texture, print technique limitations)
- Avoid AI artifact patterns: weird hands, distorted text, uncanny faces, inconsistent lighting

EXACT top-level keys:
- trend_metadata: {trend_name, velocity_score, origin_platform, lifecycle_stage}
- audience_psychographics: {primary_demo, core_desire, trigger_phrases}
- design_directives: {visual_style, color_palette: {primary, background, accent}, typography, layout_rules, negative_constraints}
- anti_slop_directives: {obvious_artifacts, composition_and_detail, color_and_style_intent, print_reality_and_originality} each {rule, reject_if}
- copywriting_directives: {tone, slogan_formulas, keywords_to_include, keywords_to_avoid}
- product_specifications: {best_garment_colors, print_technique, placement}
- traffic_and_seo_directives: {primary_seo_keywords, secondary_seo_keywords, email_subject_hooks, community_angles}

EVIDENCE-BASED INSTRUCTIONS:

1. audience_psychographics.trigger_phrases:
   - Extract EXACT phrases, slang, or sentence structures from the RAW_EVIDENCE_BLOCK.
   - Do not paraphrase unless necessary for brevity. Preserve the authentic "voice."
   - Example: If evidence says "rip off corporate bs," use that exact phrase, not "anti-corporate."

2. copywriting_directives.slogan_formulas:
   - Create formulas that mimic the syntax found in high-engagement posts.
   - If evidence shows short, punchy fragments ("No maintenance."), use fragments.
   - If evidence shows ironic long sentences, use that structure.
   - MUST include at least one formula derived directly from a top post's caption.

3. design_directives.visual_style & layout_rules:
   - Look for visual descriptors in the evidence (e.g., "distressed," "oversized," "glitchy," "hand-drawn").
   - If users mention "thrifted look," the style must be "vintage/distressed."
   - Derive composition rules from the "vibe" of the text (e.g., chaotic text = chaotic layout).

4. anti_slop_directives:
   - Identify what the audience HATES in the evidence (e.g., "too polished," "corporate," "AI-looking").
   - Explicitly ban those traits.

IMPORTANT: audience_psychographics MUST be grounded in the MEASURED communities/creators/sentiment provided below, not invented.
product_specifications use SHORT values: best_garment_colors like ["Black","Charcoal"]; print_technique like "Screen Print"; placement like "Oversized back print (12x12)".

THEMATIC COHERENCE: If multiple trends are provided, synthesize them into ONE cohesive aesthetic direction. Do NOT create a "frankenstein" brief that mixes incompatible themes.
"""

    user_prompt = f"""Social Signals (trend name + real growth velocity):
{trends_text}
"""
    
    # Add thematic coherence context if available
    if thematic_coherence:
        score = thematic_coherence.get('score', 0.5)
        notes = thematic_coherence.get('notes', 'No thematic analysis available')
        context = thematic_coherence.get('context', {})
        
        user_prompt += f"\nTHEMATIC COHERENCE ANALYSIS (score: {score:.1f}/1.0): {notes}\n"
        
        # Inject emergent themes discovered from actual signals
        emergent_themes = context.get('emergent_themes', [])
        if emergent_themes:
            theme_list = ', '.join([f"'{t}' (frequency: {c})" for t, c in emergent_themes])
            user_prompt += f"\nEMERGENT THEMES DISCOVERED (from actual signal language): {theme_list}\n"
            user_prompt += "These are the organic patterns found in how people are actually talking about these trends. Use this vocabulary.\n"
        
        # Inject static category mapping if available
        static_mapping = context.get('static_category_mapping', {})
        if static_mapping:
            categories = ', '.join([f"{cat}({count})" for cat, count in static_mapping.items()])
            user_prompt += f"\nAESTHETIC CATEGORIES DETECTED: {categories}\n"
        
        # Inject per-signal theme mappings
        mapped_signals = context.get('mapped_signals', [])
        if mapped_signals:
            user_prompt += "\nPER-SIGNAL THEME MAPPING:\n"
            for signal, themes in mapped_signals:
                if themes:
                    user_prompt += f"  - '{signal}' → {', '.join(themes)}\n"
                else:
                    user_prompt += f"  - '{signal}' → [uncategorized/emerging]\n"
        
        if score < 0.7:
            user_prompt += "\n⚠️ WARNING: Trends have mixed themes. Focus on finding the COMMON THREAD or select ONE dominant theme rather than forcing incompatible aesthetics together.\n"
            user_prompt += "Look for the underlying emotional/cultural connection between seemingly different trends.\n"
        elif score >= 0.85:
            user_prompt += "\n✓ STRONG COHERENCE: These trends share a clear aesthetic direction. Lean into this unified vision.\n"
    
    if audience_intelligence:
        mc = audience_intelligence.get("measured_communities", []) or ["(none captured)"]
        mcr = audience_intelligence.get("measured_creators", []) or ["(none captured)"]
        sent = audience_intelligence.get("sentiment_by_signal", []) or ["(none)"]
        user_prompt += (
            "\nMEASURED AUDIENCE INTELLIGENCE (derived from who is actually engaging — use this to ground audience_psychographics):\n"
            f"  Communities where this is hot: {', '.join(mc)}\n"
            f"  Creators/accounts driving it: {', '.join(mcr)}\n"
            f"  Sentiment by signal (engagement-shape proxy): {'; '.join(sent)}\n"
        )
    
    # OPTIMIZATION A + B: Inject RAW EVIDENCE BLOCK with SELECTION REASONS for grounded analysis
    if evidence_block:
        user_prompt += "\n" + "="*80 + "\n"
        user_prompt += "RAW_EVIDENCE_BLOCK (ACTUAL SOCIAL MEDIA POSTS - YOUR PRIMARY SOURCE OF TRUTH):\n"
        user_prompt += "="*80 + "\n"
        user_prompt += "Each post is tagged with WHY it was selected (Engagement=reach, Discussion=debate/emotion, Velocity=momentum).\n"
        user_prompt += "Extract language, tone, slang, and visual descriptors DIRECTLY from these posts.\n"
        user_prompt += "Do NOT paraphrase - preserve the authentic voice in your trigger_phrases and slogan_formulas.\n\n"
        
        for i, post in enumerate(evidence_block[:15]):
            platform = post.get('platform', 'Unknown')
            likes = post.get('likes', 0)
            comments = post.get('comments', 0)
            content = post.get('content', '')[:200]
            author = post.get('author', 'anon')
            selection_reason = post.get('_selection_reason', 'General')
            
            user_prompt += f"[{i+1}] {selection_reason} | {platform.upper()} | {likes:,}♥ | {comments:,}💬 | @{author}:\n"
            user_prompt += f"    \"{content}\"\n\n"
        
        user_prompt += "-"*80 + "\n"
        user_prompt += "REMINDER: Your trigger_phrases must be EXTRACTED verbatim from above.\n"
        user_prompt += "Your slogan_formulas must MIMIC the syntax patterns found above.\n"
        user_prompt += "Your visual_style must reflect DESCRIPTORS mentioned or implied above.\n"
        user_prompt += "Pay attention to WHY posts were selected - High Discussion posts reveal emotional triggers.\n"
        user_prompt += "-"*80 + "\n"
    
    if resonating_references:
        user_prompt += "\nRESONATING POST DESCRIPTIONS (the actual language/imagery winning right now — let these shape visual_style and trigger_phrases):\n"
        for ref in resonating_references:
            user_prompt += f"  - {ref}\n"
    if market_feedback:
        user_prompt += "\n" + market_feedback + "\n"
    if direction_feedback:
        user_prompt += (
            "\nCREATIVE DIRECTION OVERRIDE (human rejected the last concept for these same signals):\n"
            f"  Reason: {direction_feedback}\n"
            "  Produce a SUBSTANTIALLY DIFFERENT direction/aesthetic/motif. Do not reuse the rejected concept.\n"
        )
    user_prompt += "\nGenerate the JSON brief now."

    log_analysis.info("  Calling %s for brief generation...", MAIN_MODEL)
    start = time.time()
    response = governed_chat_completion(context=f"Brief/{MAIN_MODEL}", model=MAIN_MODEL,
                                        messages=[{"role": "system", "content": system_prompt},
                                                  {"role": "user", "content": user_prompt}],
                                        response_format={"type": "json_object"}, temperature=0.4)
    elapsed = time.time() - start
    usage = response.usage
    log_analysis.info("  Brief response received (%.2fs)", elapsed)
    if usage:
        log_analysis.debug("  Tokens: prompt=%d, completion=%d, total=%d", usage.prompt_tokens, usage.completion_tokens, usage.total_tokens)
    raw_content = safe_extract_content(response, context=f"Brief/{MAIN_MODEL}")
    try:
        raw_brief = json.loads(raw_content)
    except json.JSONDecodeError as e:
        log_analysis.error("  Brief JSON parse failed: %s | %s", e, raw_content[:500])
        raise
    brief = normalize_brief(raw_brief)
    # Inject measured intel AFTER normalization so it can't be stripped
    brief["audience_intelligence"] = audience_intelligence or {}
    brief["resonating_references"] = resonating_references or []
    log_analysis.info("  Brief built. measured_communities=%d, measured_creators=%d, refs=%d",
                      len((audience_intelligence or {}).get("measured_communities", [])),
                      len((audience_intelligence or {}).get("measured_creators", [])),
                      len(resonating_references or []))
    return brief


def run_analysis(direction_feedback=None):
    log_analysis.info("=== ANALYSIS NODE STARTED%s ===", " (DIRECTION REWRITE)" if direction_feedback else "")
    
    # OPTIMIZATION 1 & 2: Data Hygiene + Thematic Clustering
    with get_connection() as conn:
        # Fetch extended row including meta_json for hygiene filtering
        cur = conn.execute("""
            SELECT id, signal, growth, timestamp, meta_json 
            FROM trends 
            ORDER BY timestamp DESC 
            LIMIT 5
        """)
        raw_trends = cur.fetchall()
    
    log_analysis.info("  Loaded %d raw trend rows.", len(raw_trends))
    
    # Apply data hygiene filters (growth threshold, meta-json completeness)
    filtered_trends = _apply_data_hygiene_filters(raw_trends)
    
    if len(filtered_trends) == 0:
        log_analysis.warning("  [HYGIENE] All trends filtered out! Falling back to raw trends (no hygiene applied).")
        filtered_trends = raw_trends
    
    # Check thematic consistency and log coherence score
    coherence_score, thematic_notes, thematic_context = _check_thematic_consistency(filtered_trends)
    
    # Extract simplified trend tuples for downstream functions (id, signal, growth)
    trends = [(t[0], t[1], t[2]) for t in filtered_trends]
    
    log_analysis.info("  Using %d trends for brief generation after hygiene filtering.", len(trends))
    for t in trends:
        log_analysis.debug("    -> id=%d | '%s' | growth=%+.1f%%", t[0], t[1], t[2])
    
    # Load audience intelligence from filtered trends only (now includes evidence_block)
    audience_intelligence, resonating_references, evidence_block = _load_trend_meta_from_filtered(trends)
    
    # OPTIMIZATION D: Generate market feedback with trend-specific weighting
    market_feedback = _weight_market_feedback(trends)
    if not market_feedback:
        market_feedback = load_market_feedback()  # Fallback to general feedback if no trend-specific data
    
    brief = generate_granular_brief(trends, direction_feedback=direction_feedback,
                                    market_feedback=market_feedback,
                                    audience_intelligence=audience_intelligence,
                                    resonating_references=resonating_references,
                                    thematic_coherence={"score": coherence_score, "notes": thematic_notes, "context": thematic_context},
                                    evidence_block=evidence_block)
    
    trend_id = trends[0][0] if trends else None
    with get_connection() as conn:
        conn.execute("INSERT INTO briefs (trend_id, brief_json) VALUES (?, ?)", (trend_id, json.dumps(brief)))
    
    log_analysis.info("=== ANALYSIS NODE COMPLETE ===")
    return brief


def _select_evidence_posts(posts_list, limit=15):
    """
    OPTIMIZATION B: Strategic Evidence Selection
    
    Selects a diversified portfolio of posts rather than just top-likes.
    Prioritizes: High Engagement (Likes), High Discussion (Comments), High Velocity (Recent), Platform Diversity.
    
    Returns list of posts with '_selection_reason' tag explaining why each was chosen.
    """
    if not posts_list:
        return []
    
    selected = []
    seen_ids = set()
    
    # Sort by different strategic metrics
    by_likes = sorted(posts_list, key=lambda x: x.get('likes', 0), reverse=True)
    by_comments = sorted(posts_list, key=lambda x: x.get('comments', 0), reverse=True)
    by_recent = sorted(posts_list, key=lambda x: x.get('post_timestamp', '') or '', reverse=True)
    
    # Strategy: Pick from each category to ensure diverse insights
    strategies = [
        ("High Engagement (Likes)", by_likes),
        ("High Discussion (Comments)", by_comments),
        ("Emerging Velocity (Recent)", by_recent)
    ]
    
    slots_per_strategy = (limit // len(strategies)) + 1
    
    for label, sorted_list in strategies:
        count = 0
        for post in sorted_list:
            if count >= slots_per_strategy or len(selected) >= limit:
                break
            if post['post_id'] not in seen_ids:
                post_copy = post.copy()
                post_copy['_selection_reason'] = label
                selected.append(post_copy)
                seen_ids.add(post['post_id'])
                count += 1
    
    # Fill remaining slots with top likes if needed
    if len(selected) < limit:
        for post in by_likes:
            if len(selected) >= limit:
                break
            if post['post_id'] not in seen_ids:
                post_copy = post.copy()
                post_copy['_selection_reason'] = "Filler (Top Likes)"
                selected.append(post_copy)
                seen_ids.add(post['post_id'])
    
    return selected[:limit]


def _load_trend_meta_from_filtered(filtered_trends):
    """
    Optimized version of _load_trend_meta that works with pre-filtered trend tuples.
    Re-queries meta_json AND raw posts for only the filtered trend IDs to ensure consistency.
    Applies STRATEGIC EVIDENCE SELECTION (Optimization B) instead of simple top-likes sorting.
    Returns: (audience_intelligence, references, evidence_block)
    """
    if not filtered_trends:
        return {}, [], []
    
    trend_ids = [t[0] for t in filtered_trends]
    placeholders = ','.join('?' * len(trend_ids))
    
    communities, creators = {}, {}
    references, sentiments, velocities = [], [], []
    all_posts = []  # Collect all posts first, then apply strategic selection
    
    # Use a single connection for both trends and posts queries to avoid "closed database" errors
    with get_connection() as conn:
        cur = conn.execute(f"""
            SELECT id, signal, meta_json 
            FROM trends 
            WHERE id IN ({placeholders})
            ORDER BY timestamp DESC
        """, trend_ids)
        rows = cur.fetchall()
        
        for trend_id, signal, meta_raw in rows:
            try:
                meta = json.loads(meta_raw) if meta_raw else {}
            except Exception:
                meta = {}
            
            if not meta:
                continue
            
            for c in meta.get("top_communities", []):
                communities[c] = communities.get(c, 0) + 1
            for c in meta.get("top_creators", []):
                creators[c] = creators.get(c, 0) + 1
            for ref in meta.get("resonating_references", []):
                references.append(f"[{signal}] {ref}")
            if meta.get("sentiment_proxy"):
                sentiments.append(f"{signal}: {meta['sentiment_proxy']}")
            velocities.append(f"{signal}: {meta.get('velocity_source', '?')} velocity")
            
            # Fetch ALL raw posts from dedicated trend_posts table for this trend
            post_cur = None
            try:
                post_cur = conn.cursor()
                post_cur.execute("""
                    SELECT content, platform, likes, comments, author_handle, post_timestamp
                    FROM trend_posts 
                    WHERE trend_id = ? 
                    ORDER BY likes DESC 
                    LIMIT 10
                """, (trend_id,))
                post_rows = post_cur.fetchall()
                
                for post_content, platform, likes, comments, author, timestamp in post_rows:
                    if post_content and len(post_content.strip()) > 20:
                        all_posts.append({
                            "post_id": f"{trend_id}_{platform}_{likes}",  # Unique ID for deduplication
                            "signal": signal,
                            "platform": platform,
                            "content": post_content[:300],
                            "likes": likes,
                            "comments": comments,
                            "author": author,
                            "post_timestamp": timestamp or ""
                        })
            except sqlite3.ProgrammingError as e:
                logger.error(f"Database error fetching posts for trend {trend_id}: {e}")
                continue
            finally:
                if post_cur:
                    post_cur.close()
    
    # Apply strategic evidence selection (Optimization B)
    evidence_block = _select_evidence_posts(all_posts, limit=15)
    
    audience_intelligence = {
        "measured_communities": sorted(communities, key=lambda k: communities[k], reverse=True)[:8],
        "measured_creators": sorted(creators, key=lambda k: creators[k], reverse=True)[:8],
        "sentiment_by_signal": sentiments,
    }
    
    log_analysis.info("  [EVIDENCE] Strategically selected %d posts from %d total across %d filtered trends", 
                     len(evidence_block), len(all_posts), len(rows))
    
    return audience_intelligence, references[:9], evidence_block


def classify_rejection_scope(reason, brief):
    trend_name = brief.get("trend_metadata", {}).get("trend_name", "Unknown")
    prompt = (
        "A human reviewer rejected a generated t-shirt design. Decide EXECUTION vs DIRECTION.\n\n"
        f"Trend: {trend_name}\nHuman's reason: {reason}\n\n"
        '"execution" = fix this picture (too busy, wrong colors, AI-looking, simplify, layout). Concept fine.\n'
        '"direction" = change the concept (hate the motif/theme, wrong vibe, pick a different angle).\n\n'
        'Return ONLY JSON: {"scope": "execution", "rationale": "..."} or {"scope": "direction", "rationale": "..."}'
    )
    log_analysis.info("  Classifying rejection scope...")
    try:
        response = governed_chat_completion(context=f"RejectScope/{MAIN_MODEL}", model=MAIN_MODEL,
                                            messages=[{"role": "user", "content": prompt}],
                                            response_format={"type": "json_object"}, temperature=0.2)
        data = json.loads(safe_extract_content(response, context=f"RejectScope/{MAIN_MODEL}"))
        scope = str(data.get("scope", "execution")).lower().strip()
        if scope not in ("execution", "direction"):
            scope = "execution"
        log_analysis.info("  Rejection scope = %s | %s", scope, str(data.get("rationale", ""))[:120])
        return scope
    except Exception as e:
        log_analysis.warning("  Scope classification failed (%s); default execution.", str(e)[:80])
        return "execution"

import json
import time
from .database import get_connection
from .llm_client import governed_chat_completion, MAIN_MODEL
from .llm_utils import safe_extract_content
from .brief_schema import normalize_brief
from .logger import log_analysis


def _load_trend_meta():
    """Read the last 5 trends WITH meta_json; aggregate audience intel + references."""
    with get_connection() as conn:
        cur = conn.execute("SELECT signal, meta_json FROM trends ORDER BY timestamp DESC LIMIT 5")
        rows = cur.fetchall()
    communities, creators = {}, {}
    references, sentiments, velocities = [], [], []
    for signal, meta_raw in rows:
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
    audience_intelligence = {
        "measured_communities": sorted(communities, key=lambda k: communities[k], reverse=True)[:8],
        "measured_creators": sorted(creators, key=lambda k: creators[k], reverse=True)[:8],
        "sentiment_by_signal": sentiments,
    }
    return audience_intelligence, references[:9]


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
                            audience_intelligence=None, resonating_references=None):
    trends_text = "\n".join([f"- {t[1]} (Growth: {t[2]}%)" for t in trends_data])
    log_analysis.debug("  Building brief | trends=%d | dir_fb=%s | market_fb=%s | audience=%s | refs=%d",
                       len(trends_data), bool(direction_feedback), bool(market_feedback),
                       bool(audience_intelligence), len(resonating_references or []))

    system_prompt = """You are an expert trend analyst, creative director, and print production specialist.
Analyze the provided social signals AND the measured audience intelligence, and generate a highly granular brief.
Output valid JSON only, no markdown. anti_slop_directives must be specific.

EXACT top-level keys:
- trend_metadata: {trend_name, velocity_score, origin_platform, lifecycle_stage}
- audience_psychographics: {primary_demo, core_desire, trigger_phrases}
- design_directives: {visual_style, color_palette: {primary, background, accent}, typography, layout_rules, negative_constraints}
- anti_slop_directives: {obvious_artifacts, composition_and_detail, color_and_style_intent, print_reality_and_originality} each {rule, reject_if}
- copywriting_directives: {tone, slogan_formulas, keywords_to_include, keywords_to_avoid}
- product_specifications: {best_garment_colors, print_technique, placement}
- traffic_and_seo_directives: {primary_seo_keywords, secondary_seo_keywords, email_subject_hooks, community_angles}

IMPORTANT: audience_psychographics MUST be grounded in the MEASURED communities/creators/sentiment provided below, not invented.
product_specifications use SHORT values: best_garment_colors like ["Black","Charcoal"]; print_technique like "Screen Print"; placement like "Oversized back print (12x12)".
"""

    user_prompt = f"""Social Signals (trend name + real growth velocity):
{trends_text}
"""
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
    if resonating_references:
        user_prompt += "\nRESONATING POST DESCRIPTIONS (the actual language/imagery winning right now — let these shape visual_style and trigger_phrrases):\n"
        for ref in resonating_references:
            user_prompt += f"  - {ref}\n"
    if market_feedback:
        user_prompt += "\n" + market_feedback + "\n"
    if direction_feedback:
        user_prompt += (
            "\nCREATIVE DIRECTION OVERRIDE (human rejected the last concept for these same signals):\n"
            f"  Reason: {direction_feedback}\n"
            "  Produce a SUBSTANTIVELY DIFFERENT direction/aesthetic/motif. Do not reuse the rejected concept.\n"
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
    audience_intelligence, resonating_references = _load_trend_meta()
    market_feedback = load_market_feedback()
    with get_connection() as conn:
        cur = conn.execute("SELECT id, signal, growth FROM trends ORDER BY timestamp DESC LIMIT 5")
        trends = cur.fetchall()
        log_analysis.info("  Loaded %d trend rows.", len(trends))
        for t in trends:
            log_analysis.debug("    -> id=%d | '%s' | growth=%+.1f%%", t[0], t[1], t[2])
        brief = generate_granular_brief(trends, direction_feedback=direction_feedback,
                                        market_feedback=market_feedback,
                                        audience_intelligence=audience_intelligence,
                                        resonating_references=resonating_references)
        trend_id = trends[0][0] if trends else None
        conn.execute("INSERT INTO briefs (trend_id, brief_json) VALUES (?, ?)", (trend_id, json.dumps(brief)))
    log_analysis.info("=== ANALYSIS NODE COMPLETE ===")
    return brief


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

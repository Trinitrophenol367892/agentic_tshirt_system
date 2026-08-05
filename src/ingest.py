"""
Ingest Node — three-stage discovery PLUS measured audience intelligence.

Adds, at NO extra Xpoz cost (reuses posts we already fetch):
  FIX 1  audience/creator/community capture + a sentiment PROXY (engagement shape)
  FIX 4  REAL growth velocity from post timestamps (recent-half vs early-half),
         with graceful fallback to the engagement-density heuristic
  FIX 5  top resonating post descriptions per keyword (visual references)

All of this is packed into each signal's `meta` dict and stored as trends.meta_json
so analysis can thread it into the brief.
"""
import os
import re
import json
import time
from datetime import datetime, timedelta
from collections import defaultdict, Counter
from dotenv import load_dotenv
from .database import get_connection
from .llm_client import governed_chat_completion, MAIN_MODEL
from .llm_utils import safe_extract_content
from .logger import log_ingest

load_dotenv()

DISCOVERY_LOOKBACK_DAYS = 3
EXTRACTION_LOOKBACK_DAYS = 7
MIN_KEYWORD_FREQUENCY = 2
MIN_ENGAGEMENT = 30
MAX_RAW_KEYWORDS_FOR_LLM = 30
MAX_DISCOVERED_KEYWORDS = 8
MAX_REFERENCES_PER_KEYWORD = 3

SPAM_WORDS = {
    "sealed", "factory", "playstation", "xbox", "nintendo", "origins",
    "essentials", "sneaker", "sneakers", "jordans", "yeezy", "resell",
    "resale", "stockx", "goat", "ebay", "amazon", "walmart", "target",
    "sale", "discount", "coupon", "promo", "deal", "offer", "price",
    "shipping", "delivery", "tracking", "order", "orders", "purchase",
    "buy", "buying", "seller", "selling", "vendor", "supplier",
    "wholesale", "bulk", "lot", "bundle", "pack", "package",
    "unused", "mint", "condition", "graded", "grade",
    "rare", "limited", "exclusive", "restock", "preorder",
    "collector", "collectible", "trading", "trade",
    "card", "cards", "pokemon", "magic", "yugioh",
    "console", "gaming", "gamer", "game", "games",
    "rayman", "mario", "zelda", "sonic", "kirby", "metroid",
    "ps5", "ps4", "ps3", "switch", "steam", "epic",
    "funko", "pop", "figure", "figurine", "toy", "toys",
    "vinyl", "record", "cassette", "cd", "dvd", "blu",
    "autograph", "signed", "certificate", "authentic", "original",
    "replica", "reproduction", "counterfeit", "fake",
    "follow", "follower", "followers", "subscribe", "subscriber",
    "like", "likes", "share", "shares", "repost", "retweet",
    "comment", "comments", "reply", "replies", "dm", "message",
    "link", "bio", "profile", "page", "site", "website", "url",
    "click", "tap", "swipe", "scroll", "watch", "watching",
    "ad", "ads", "sponsored", "partnership", "collab",
    "giveaway", "contest", "win", "winner", "enter", "entry",
    "free", "gratis", "complimentary", "bonus", "gift",
    "below", "strikethroughs", "sold", "listing", "listed",
    "chrome hearts", "derschutze", "blends", "premium",
    "luxury", "inspired", "rep", "reps", "dupe", "dupes",
}

STOP_WORDS = {"the", "and", "for", "with", "this", "that", "from", "are", "was",
             "has", "have", "will", "can", "just", "like", "but", "not", "you",
             "all", "been", "when", "they", "them", "than", "its", "our", "out",
             "get", "got", "one", "now", "way", "too", "also", "into",
             "very", "really", "much", "more", "some", "what", "how", "who",
             "is", "it", "to", "in", "on", "at", "by", "of", "or", "an",
             "do", "did", "does", "done", "am", "be", "been", "being",
             "my", "me", "we", "us", "he", "she", "his", "her", "their",
             "if", "so", "no", "yes", "up", "down", "off", "over", "under"}

DISCOVERY_QUERIES = {
    "twitter": [
        "new aesthetic OR new core OR new wave fashion",
        "shirt design OR tee design OR graphic tee trend",
        "underground fashion OR emerging style OR cultural moment",
        "viral phrase OR trending meme OR internet culture",
    ],
    "tiktok": ["new aesthetic trend", "shirt design idea", "viral fashion moment", "trending phrase merch"],
    "reddit": ["new aesthetic movement", "emerging design style", "cultural trend t-shirt", "what's trending right now"],
}

# Broad field lists — we read whichever the SDK actually returns (defensive getattr).
TW_FIELDS = ["id", "text", "like_count", "retweet_count", "reply_count", "impression_count",
             "hashtags", "created_at_date", "username", "author_name", "author_username", "author_id"]
TK_FIELDS = ["id", "description", "like_count", "comment_count", "play_count", "collect_count",
             "hashtags", "created_at_date", "username", "author_name", "author_username", "author_id"]
RD_FIELDS = ["id", "title", "selftext", "score", "upvotes", "downvotes", "comments_count",
             "subreddit_name", "created_at_date", "username", "author_name", "author_username", "author_id"]


def _parse_date(value):
    """Best-effort parse of an Xpoz date field into a datetime (or None)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.utcfromtimestamp(float(value))
        except Exception:
            return None
    s = str(value).strip()
    if not s:
        return None
    s2 = s[:-1] if s.endswith("Z") else s
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s2, fmt)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(s2)
    except Exception:
        return None


def _post_engagement(post, platform):
    likes = getattr(post, "like_count", 0) or 0
    if platform == "twitter":
        retweets = getattr(post, "retweet_count", 0) or 0
        replies = getattr(post, "reply_count", 0) or 0
        return likes + retweets + replies, replies, likes
    if platform == "tiktok":
        comments = getattr(post, "comment_count", 0) or 0
        collects = getattr(post, "collect_count", 0) or 0
        return likes + comments + collects, comments, likes
    # reddit
    score = getattr(post, "score", 0) or 0
    comments = getattr(post, "comments_count", 0) or 0
    return score + comments, comments, likes or score


def _post_author(post):
    for attr in ("username", "author_username", "author_name", "author_id"):
        v = getattr(post, attr, None)
        if v:
            return str(v).lstrip("@")
    return None


def _post_community(post, platform):
    if platform == "reddit":
        v = getattr(post, "subreddit_name", None)
        if v:
            return f"r/{str(v).lstrip('r/')}"
    return None


def _post_text(post, platform):
    if platform == "twitter":
        return getattr(post, "text", "") or ""
    if platform == "tiktok":
        return getattr(post, "description", "") or ""
    return (getattr(post, "title", "") or "") + " " + (getattr(post, "selftext", "") or "")[:200]


class XpozIngestClient:
    def __init__(self):
        self.api_key = os.getenv("XPOZ_API_KEY")
        if not self.api_key:
            raise ValueError("XPOZ_API_KEY not set in .env")
        log_ingest.debug("Initializing Xpoz client...")
        from xpoz import XpozClient
        self.client = XpozClient(self.api_key)
        log_ingest.info("Xpoz client connected.")

    def close(self):
        if hasattr(self, "client") and self.client:
            self.client.close()

    # ---- STAGE 1 ----
    def discover_raw_keywords(self):
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=DISCOVERY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        log_ingest.info("  [STAGE 1] Broad discovery sweep (%s to %s)...", start_date, end_date)
        hashtag_counter, keyword_counter = Counter(), Counter()
        total = 0
        for plat, queries, fields in (("twitter", DISCOVERY_QUERIES["twitter"], TW_FIELDS),
                                      ("tiktok", DISCOVERY_QUERIES["tiktok"], TK_FIELDS),
                                      ("reddit", DISCOVERY_QUERIES["reddit"], RD_FIELDS)):
            log_ingest.debug("  [STAGE 1] Scanning %s...", plat)
            for q in queries:
                posts = self._search(plat, q, start_date, end_date, fields)
                total += len(posts)
                for p in posts:
                    self._extract_keywords(p, hashtag_counter, keyword_counter, plat)
        log_ingest.info("  [STAGE 1] Scanned %d posts.", total)

        def not_spam(t):
            t = t.lower().lstrip("#")
            if t in SPAM_WORDS or len(t) < 4 or t.isdigit():
                return False
            return not any(w in SPAM_WORDS for w in t.split())

        merged = {}
        for tag, c in hashtag_counter.items():
            if not_spam(tag):
                merged[f"#{tag}"] = c * 2
        for kw, c in keyword_counter.items():
            if not_spam(kw):
                merged[kw] = c
        ranked = sorted(merged.items(), key=lambda x: x[1], reverse=True)[:MAX_RAW_KEYWORDS_FOR_LLM]
        log_ingest.info("  [STAGE 1] Top %d raw keywords after spam filter.", len(ranked))
        for kw, c in ranked[:10]:
            log_ingest.debug("    -> '%s' (score: %d)", kw, c)
        return [kw for kw, _ in ranked]

    def _extract_keywords(self, post, hashtag_counter, keyword_counter, platform):
        for tag in (getattr(post, "hashtags", []) or []):
            clean = tag.lower().lstrip("#").strip()
            if clean and len(clean) > 3:
                hashtag_counter[clean] += 1
        text_clean = re.sub(r'http\S+|@\w+', '', _post_text(post, platform).lower())
        text_clean = re.sub(r'[^\w\s]', ' ', text_clean)
        words = text_clean.split()
        for i in range(len(words) - 2):
            w1, w2, w3 = words[i], words[i+1], words[i+2]
            if len(w1) > 3 and len(w2) > 3 and len(w3) > 3 and w1 not in STOP_WORDS and w2 not in STOP_WORDS and w3 not in STOP_WORDS:
                keyword_counter[f"{w1} {w2} {w3}"] += 2
        for i in range(len(words) - 1):
            w1, w2 = words[i], words[i+1]
            if len(w1) > 3 and len(w2) > 3 and w1 not in STOP_WORDS and w2 not in STOP_WORDS:
                keyword_counter[f"{w1} {w2}"] += 1
        for w in words:
            if len(w) >= 6 and w not in STOP_WORDS and not w.isdigit():
                keyword_counter[w] += 1

    # ---- STAGE 2 ----
    def llm_filter_relevance(self, raw_keywords, confirmed_trends):
        log_ingest.info("  [STAGE 2] LLM relevance filtering (%d raw via %s)...", len(raw_keywords), MAIN_MODEL)
        if confirmed_trends:
            ctx = "\n".join([f"  - '{c['trend_name']}' (slogan: '{c['design_slogan']}', score: {c['judge_score']})" for c in confirmed_trends[:5]])
            feedback = f"\nPREVIOUSLY SUCCESSFUL (find similar NEW ones, do not repeat):\n{ctx}\n"
        else:
            feedback = "\nNo prior successes yet — use general cultural knowledge.\n"
        kw_list = "\n".join([f"  {i+1}. {k}" for i, k in enumerate(raw_keywords)])
        prompt = f"""You are a cultural trend analyst and merch strategist. Pick keywords people want to WEAR right now.

Discovered keywords/hashtags (last 72h):
{kw_list}
{feedback}
Select keywords that are emerging aesthetics, viral moments, trending phrases, niche culture, current events with visual potential, or emerging moods. REJECT generic filler (brand, shirt, design, your, looking), product/reseller names, saturated trends, vague marketing words.

Return ONLY valid JSON:
{{"relevant_keywords": ["k1", ...], "reasoning": "<why each works as a tee trend>", "trend_types": ["<type per keyword>"]}}

Pick 3 to {MAX_DISCOVERED_KEYWORDS}. Prioritize freshness, wearability, visual potential, specificity."""
        start = time.time()
        response = governed_chat_completion(context=f"RelevanceFilter/{MAIN_MODEL}", model=MAIN_MODEL,
                                            messages=[{"role": "user", "content": prompt}],
                                            response_format={"type": "json_object"}, temperature=0.3)
        log_ingest.info("  [STAGE 2] LLM response received (%.2fs)", time.time() - start)
        content = safe_extract_content(response, context=f"RelevanceFilter/{MAIN_MODEL}")
        data = json.loads(content)
        rel = data.get("relevant_keywords", [])
        log_ingest.info("  [STAGE 2] LLM selected %d from %d.", len(rel), len(raw_keywords))
        types = data.get("trend_types", [])
        for i, k in enumerate(rel):
            log_ingest.info("    ✓ '%s' [%s]", k, types[i] if i < len(types) else "?")
        return rel[:MAX_DISCOVERED_KEYWORDS]

    # ---- STAGE 3 (now also captures audience intel + velocity + references + RAW POSTS) ----
    def extract_signals(self, validated_keywords):
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=EXTRACTION_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        window_start = datetime.now() - timedelta(days=EXTRACTION_LOOKBACK_DAYS)
        midpoint = window_start + timedelta(days=EXTRACTION_LOOKBACK_DAYS / 2.0)
        log_ingest.info("  [STAGE 3] Focused extraction on %d keywords (%s to %s)...", len(validated_keywords), start_date, end_date)

        # per-keyword accumulators
        agg = {k: {"volume": 0, "engagement": 0, "posts": 0, "platforms": set(),
                   "communities": Counter(), "creators": Counter(),
                   "recent_eng": 0, "recent_n": 0, "early_eng": 0, "early_n": 0,
                   "discussion_sum": 0.0, "pos_sum": 0.0, "sent_n": 0,
                   "ref_candidates": [], "raw_posts": []} for k in validated_keywords}

        for keyword in validated_keywords:
            term = keyword.lstrip("#")
            # Sequential platform scanning to avoid burst rate limits
            for plat, q, fields in (("twitter", f'"{term}"', TW_FIELDS),
                                    ("tiktok", term, TK_FIELDS),
                                    ("reddit", term, RD_FIELDS)):
                posts = self._search(plat, q, start_date, end_date, fields)
                # Small delay between platform queries
                time.sleep(0.5)
                for p in posts:
                    eng, replies, likes = _post_engagement(p, plat)
                    if eng < MIN_ENGAGEMENT:
                        continue
                    a = agg[keyword]
                    a["posts"] += 1
                    a["engagement"] += eng
                    a["platforms"].add(plat)
                    impressions = getattr(p, "impression_count", 0) or 0
                    plays = getattr(p, "play_count", 0) or 0
                    a["volume"] += (impressions or plays or eng * (10 if plat == "reddit" else 1))
                    # FIX 1 audience capture
                    comm = _post_community(p, plat)
                    if comm:
                        a["communities"][comm] += 1
                    auth = _post_author(p)
                    if auth:
                        a["creators"][auth] += 1
                    # FIX 1 sentiment proxy from engagement shape
                    a["sent_n"] += 1
                    a["discussion_sum"] += (replies / (likes + 1.0))
                    a["pos_sum"] += (likes / (eng + 1.0))
                    # FIX 4 velocity bucketing by post date
                    d = _parse_date(getattr(p, "created_at_date", None))
                    if d is not None:
                        if d >= midpoint:
                            a["recent_eng"] += eng; a["recent_n"] += 1
                        else:
                            a["early_eng"] += eng; a["early_n"] += 1
                    # FIX 5 reference candidate (keep text + eng)
                    txt = _post_text(p, plat).strip()
                    if txt:
                        a["ref_candidates"].append((eng, txt))
                    # NEW: Capture raw post evidence for database storage
                    post_id = getattr(p, "id", None)
                    if post_id:
                        a["raw_posts"].append({
                            "post_id": str(post_id),
                            "platform": plat,
                            "content": txt[:2000],  # Truncate very long posts
                            "author_handle": auth,
                            "likes": likes,
                            "comments": replies,
                            "shares": getattr(p, "retweet_count", 0) or getattr(p, "collect_count", 0) or 0,
                            "impressions": impressions or plays or 0,
                            "post_timestamp": d.isoformat() if d else None
                        })

        signals = []
        posts_to_store = {}  # keyword -> list of raw posts

        for keyword in validated_keywords:
            a = agg[keyword]
            if a["posts"] < 2:
                log_ingest.debug("    Skipping '%s': %d posts", keyword, a["posts"])
                continue

            # FIX 4 velocity: real if both halves have dated posts, else heuristic
            if a["recent_n"] >= 2 and a["early_n"] >= 2:
                recent_d = a["recent_eng"] / a["recent_n"]
                early_d = a["early_eng"] / a["early_n"]
                if early_d > 0:
                    velocity = round(((recent_d - early_d) / early_d) * 100.0, 1)
                    velocity_src = "timestamp"
                else:
                    velocity = 0.0; velocity_src = "timestamp-flat"
            else:
                avg = a["engagement"] / a["posts"]
                velocity = round(min(95.0, (avg / 50) * 8), 1)
                velocity_src = "heuristic"
            velocity = max(-90.0, min(95.0, velocity))

            # FIX 1 sentiment label (proxy)
            if a["sent_n"] > 0:
                disc = a["discussion_sum"] / a["sent_n"]
                pos = a["pos_sum"] / a["sent_n"]
                if disc > 0.6:
                    sent = "highly-discussed / debated"
                elif pos > 0.7:
                    sent = "positive resonance"
                else:
                    sent = "mixed / neutral"
            else:
                sent = "unknown"

            # FIX 5 top references
            a["ref_candidates"].sort(key=lambda x: x[0], reverse=True)
            refs = [t[:160] for _, t in a["ref_candidates"][:MAX_REFERENCES_PER_KEYWORD]]

            top_comm = [c for c, _ in a["communities"].most_common(5)]
            top_creators = [c for c, _ in a["creators"].most_common(5)]

            # Store raw posts for later database insertion
            posts_to_store[keyword] = a["raw_posts"][:10]  # Keep top 10 posts per keyword

            meta = {
                "platforms": sorted(a["platforms"]),
                "post_count": a["posts"],
                "avg_engagement": round(a["engagement"] / a["posts"], 1),
                "velocity_source": velocity_src,
                "sentiment_proxy": sent,
                "top_communities": top_comm,
                "top_creators": top_creators,
                "resonating_references": refs,
            }

            signal = {"signal": keyword.lstrip("#").title(), "volume": a["volume"],
                      "growth": velocity, "meta": meta}
            signals.append(signal)
            log_ingest.info("    Signal '%s' | vol=%d | velocity=%+.1f%% (%s) | sentiment=%s | communities=%s | refs=%d",
                            signal["signal"], a["volume"], velocity, velocity_src, sent,
                            top_comm[:3] or "none", len(refs))

        signals.sort(key=lambda s: s["meta"]["avg_engagement"], reverse=True)
        signals = signals[:10]
        log_ingest.info("  [STAGE 3] Extracted %d enriched signals.", len(signals))
        return signals, posts_to_store

    def _search(self, platform, query, start_date, end_date, fields, max_retries=3):
        """Search with adaptive backoff for rate limit errors."""
        for attempt in range(max_retries):
            try:
                if platform == "twitter":
                    r = self.client.twitter.search_posts(query, start_date=start_date, end_date=end_date, language="en", fields=fields, limit=20)
                elif platform == "tiktok":
                    r = self.client.tiktok.search_posts(query, start_date=start_date, end_date=end_date, fields=fields, limit=20)
                else:
                    r = self.client.reddit.search_posts(query, start_date=start_date, end_date=end_date, sort="top", time="week", fields=fields, limit=20)
                posts = r.data if r and r.data else []
                log_ingest.debug("      %s '%s': %d posts", platform, query[:30], len(posts))
                return posts
            except Exception as e:
                error_msg = str(e)
                if "Usage limit exceeded" in error_msg:
                    wait_time = 60 * (attempt + 1)  # 60s, 120s, 180s
                    log_ingest.warning("      %s rate limited. Waiting %ds before retry %d/%d...", platform, wait_time, attempt+1, max_retries)
                    time.sleep(wait_time)
                else:
                    log_ingest.warning("      %s failed: %s", platform, error_msg[:80])
                    return []
        log_ingest.warning("      %s failed after %d retries", platform, max_retries)
        return []


def load_confirmed_trends():
    try:
        with get_connection() as conn:
            cur = conn.execute("SELECT trend_name, brief_theme, design_slogan, judge_score FROM confirmed_trends ORDER BY published_at DESC LIMIT 10")
            rows = cur.fetchall()
    except Exception:
        rows = []
    out = [{"trend_name": r[0], "brief_theme": r[1], "design_slogan": r[2], "judge_score": r[3]} for r in rows]
    if out:
        log_ingest.info("  [FEEDBACK] %d confirmed trends loaded.", len(out))
    else:
        log_ingest.info("  [FEEDBACK] No confirmed trends yet.")
    return out


def run_ingest():
    log_ingest.info("=== INGEST NODE STARTED ===")
    api_key = os.getenv("XPOZ_API_KEY")
    posts_to_store = {}
    
    if not api_key or api_key == "your_xpoz_api_key_here":
        log_ingest.warning("XPOZ_API_KEY not configured; using fallback signals.")
        signals = [{"signal": "Glitchcore Aesthetic", "volume": 15000, "growth": 45.2, "meta": {}},
                   {"signal": "Terminal Core", "volume": 8500, "growth": 32.5, "meta": {}}]
    else:
        try:
            client = XpozIngestClient()
            confirmed = load_confirmed_trends()
            raw = client.discover_raw_keywords()
            validated = client.llm_filter_relevance(raw, confirmed)
            signals, posts_to_store = client.extract_signals(validated)
            client.close()
            if not signals:
                log_ingest.warning("  No qualified signals; fallback.")
                signals = [{"signal": "Glitch Aesthetic", "volume": 5000, "growth": 15.0, "meta": {}},
                           {"signal": "Digital Decay", "volume": 8000, "growth": 20.0, "meta": {}}]
        except Exception as e:
            log_ingest.error("  Xpoz ingest failed: %s", str(e)[:200])
            signals = [{"signal": "Glitchcore Aesthetic", "volume": 15000, "growth": 45.2, "meta": {}},
                       {"signal": "Terminal Core", "volume": 8500, "growth": 32.5, "meta": {}}]

    log_ingest.info("  Storing %d signals to SQLite (with meta_json)...", len(signals))
    with get_connection() as conn:
        cursor = conn.cursor()
        for i, s in enumerate(signals, 1):
            cursor.execute("INSERT INTO trends (signal, volume, growth, meta_json) VALUES (?, ?, ?, ?)",
                         (s["signal"], s["volume"], s["growth"], json.dumps(s.get("meta", {}))))
            trend_id = cursor.lastrowid
            log_ingest.debug("    [%d/%d] '%s' | vol=%d | growth=%+.1f%% | trend_id=%d", 
                            i, len(signals), s["signal"], s["volume"], s["growth"], trend_id)
            
            # Store raw posts in dedicated trend_posts table
            signal_key = None
            for k in posts_to_store.keys():
                if k.lstrip("#").title() == s["signal"]:
                    signal_key = k
                    break
            
            if signal_key and signal_key in posts_to_store:
                raw_posts = posts_to_store[signal_key]
                for post in raw_posts:
                    cursor.execute("""
                        INSERT INTO trend_posts 
                        (trend_id, post_id, platform, content, author_handle, likes, comments, shares, impressions, post_timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        trend_id,
                        post["post_id"],
                        post["platform"],
                        post["content"],
                        post.get("author_handle"),
                        post.get("likes", 0),
                        post.get("comments", 0),
                        post.get("shares", 0),
                        post.get("impressions", 0),
                        post.get("post_timestamp")
                    ))
                log_ingest.debug("    Stored %d raw posts for trend '%s'", len(raw_posts), s["signal"])
        conn.commit()
    
    log_ingest.info("=== INGEST NODE COMPLETE: %d signals stored + %d total raw posts ===", 
                    len(signals), sum(len(posts) for posts in posts_to_store.values()))

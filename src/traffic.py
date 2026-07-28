import json
import time
import yaml
import random
from .database import get_connection
from .llm_client import governed_chat_completion, MAIN_MODEL
from .llm_utils import safe_extract_content
from .logger import log_traffic


def load_config():
    with open("config.yaml", "r") as f:
        return yaml.safe_load(f)


def draft_all_community_posts(angles, brief):
    trend_name = brief.get("trend_metadata", {}).get("trend_name", "new design")
    tone = brief.get("copywriting_directives", {}).get("tone", "contemporary")
    descs = []
    for i, a in enumerate(angles, 1):
        target = a.get("subreddit", a.get("platform", "community"))
        descs.append(f"  {i}. Platform: {target} | Focus: {a.get('focus', 'general appeal')}")
    prompt = (f"Generate {len(angles)} short community posts for the new '{trend_name}' t-shirt.\n"
              f"Tone: {tone}. No hashtags. Each under 150 words. Avoid generic scraped phrasing.\n\n"
              f"Targets:\n" + "\n".join(descs) + "\n\n"
              'Return ONLY valid JSON: {"posts": ["<post1>", ...]}')
    log_traffic.info("  [Community] Batched %d posts in 1 call...", len(angles))
    start = time.time()
    response = governed_chat_completion(context=f"CommunityPosts/{MAIN_MODEL}", model=MAIN_MODEL,
                                        messages=[{"role": "user", "content": prompt}],
                                        response_format={"type": "json_object"}, temperature=0.8)
    log_traffic.info("  Batched response received (%.2fs)", time.time() - start)
    data = json.loads(safe_extract_content(response, context=f"CommunityPosts/{MAIN_MODEL}"))
    posts = data.get("posts", [])
    while len(posts) < len(angles):
        posts.append(f"New {trend_name} tee just dropped.")
    for i, p in enumerate(posts):
        target = angles[i].get("subreddit", angles[i].get("platform", "community"))
        log_traffic.debug("    [%d] %s: %s...", i+1, target, p[:80])
    return posts


def run_traffic_steering(design_id, brief):
    log_traffic.info("=== TRAFFIC STEERING NODE STARTED === | design %d", design_id)
    config = load_config()
    traffic_dir = brief.get("traffic_and_seo_directives", {})
    trend_name = brief.get("trend_metadata", {}).get("trend_name", "new design")

    with get_connection() as conn:
        active = []
        if config["traffic"]["seo"]["enabled"]:
            log_traffic.info("  [SEO] Activating...")
            pk = traffic_dir.get("primary_seo_keywords", ["graphic tee"]); sk = traffic_dir.get("secondary_seo_keywords", ["custom t-shirt"])
            if not isinstance(pk, list): pk = [pk]
            if not isinstance(sk, list): sk = [sk]
            log_traffic.debug("    Tags: %s", pk + sk)
            active.append("seo")
        if config["traffic"]["email"]["enabled"]:
            log_traffic.info("  [Email] Activating...")
            hooks = traffic_dir.get("email_subject_hooks", ["New Drop"])
            if not isinstance(hooks, list): hooks = [hooks]
            log_traffic.debug("    Subject: '%s'", random.choice(hooks))
            active.append("email")
        if config["traffic"]["community"]["enabled"]:
            log_traffic.info("  [Community] Activating...")
            angles = traffic_dir.get("community_angles", [])
            if not isinstance(angles, list): angles = [angles]
            angles = [a for a in angles if isinstance(a, dict)]
            # BONUS: augment with measured hot communities from audience intelligence
            ai = brief.get("audience_intelligence", {}) or {}
            for comm in ai.get("measured_communities", [])[:3]:
                angles.append({"subreddit": comm, "focus": "audience-measured hot community — speak their language"})
                log_traffic.info("    + measured community angle: %s", comm)
            if angles:
                draft_all_community_posts(angles, brief)
                log_traffic.info("    %d community posts drafted (1 call).", len(angles))
            active.append("community")
        if config["traffic"]["paid_social"]["enabled"]:
            log_traffic.info("  [Paid Social] Activating... %s", config["traffic"]["paid_social"].get("platform", "shopify_audiences"))
            active.append("paid_social")
        else:
            log_traffic.info("  [Paid Social] DISABLED.")
        for ch in active:
            conn.execute("INSERT INTO performance (design_id, channel) VALUES (?, ?)", (design_id, ch))
    log_traffic.info("  Active channels: %s", ", ".join(active))
    log_traffic.info("=== TRAFFIC STEERING NODE COMPLETE ===")

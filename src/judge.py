import json
import time
import yaml
import requests
from .database import get_connection
from .llm_client import governed_chat_completion, VISION_MODEL
from .llm_utils import safe_extract_content
from .logger import log_judge

IMAGE_CHECK_TIMEOUT = 15
VISION_CALL_TIMEOUT = 180  # 3 minutes max per vision call
VISION_MAX_RETRIES = 1     # 1 retry on failure


def load_config():
    with open("config.yaml", "r") as f:
        return yaml.safe_load(f)


def verify_image_accessible(image_url: str) -> bool:
    try:
        resp = requests.head(image_url, timeout=IMAGE_CHECK_TIMEOUT, allow_redirects=True)
        if resp.status_code == 200:
            log_judge.debug("    Image accessible (200 | %s)", resp.headers.get("Content-Type", ""))
            return True
        log_judge.warning("    Image returned HTTP %d.", resp.status_code)
        return False
    except requests.exceptions.RequestException as e:
        log_judge.warning("    Image check failed: %s", str(e)[:80])
        return False


def call_vision_llm(image_url, brief):
    """
    Evaluate the GRAPHIC ONLY. No text/typography evaluation.
    The slogan is applied separately in production.
    """
    design_dir = brief.get("design_directives", {})
    anti_slop = brief.get("anti_slop_directives", {})

    visual_style = design_dir.get("visual_style", "contemporary graphic design")
    palette = design_dir.get("color_palette", {})
    primary_colors = palette.get("primary", ["#FFFFFF"])
    bg_color = palette.get("background", "#000000")
    layout_rules = design_dir.get("layout_rules", "bold composition")
    negative_constraints = design_dir.get("negative_constraints", [])

    colors_str = ", ".join(str(c) for c in primary_colors) if isinstance(primary_colors, list) else str(primary_colors)
    neg_str = ", ".join(str(c) for c in negative_constraints) if isinstance(negative_constraints, list) else str(negative_constraints)

    # Build anti-slop checklist
    slop_lines = []
    for category, rules in anti_slop.items():
        if not isinstance(rules, dict):
            continue
        reject_list = rules.get("reject_if", [])
        if not isinstance(reject_list, list):
            reject_list = [str(reject_list)]
        slop_lines.append(f"[{category.upper().replace('_', ' ')}]")
        for reject in reject_list:
            slop_lines.append(f"  - {reject}")

    slop_text = "\n".join(slop_lines)

    system_prompt = f"""You are evaluating a T-SHIRT GRAPHIC DESIGN for production quality.

CRITICAL: This is a GRAPHIC-ONLY evaluation. The slogan/text is added separately in production
via screen print or DTG. There should be NO text in this image. Evaluate ONLY the visual graphic.

DO NOT evaluate, mention, or penalize:
- Text, letters, words, typography, or font choices
- Absence of text (text is added later in production)
- Slogan quality or readability

EVALUATE ONLY THESE 4 CATEGORIES (1.0-10.0 each):

1. COMPOSITION (30%): Is there a clear focal point? Visual hierarchy? Intentional layout?
   Does the eye know where to look? Is it balanced or intentionally asymmetric?
   Would this look good printed large on a t-shirt back panel?

2. COLOR_EXECUTION (20%): Does the palette match the brief ({colors_str} on {bg_color})?
   Is there strong contrast? Are colors vibrant and intentional? No muddy blending?

3. AESTHETIC_QUALITY (25%): Does it match the requested style ({visual_style})?
   Does it look professional and intentional? Would someone want to wear this?
   Is it visually striking and memorable?

4. PRINT_VIABILITY (25%): Can this be reproduced via screen print or DTG?
   Are there impossible micro-details? Too many color separations?
   Would it survive being printed on fabric at 12x12 inches?

ANTI-SLOP CHECKLIST (deduct heavily for any of these):
{slop_text}

ADDITIONAL REJECTIONS:
- {neg_str}

Respond ONLY in valid JSON:
{{
    "scores": {{
        "composition": <float>,
        "color_execution": <float>,
        "aesthetic_quality": <float>,
        "print_viability": <float>
    }},
    "overall_score": <float weighted average>,
    "reasoning": "<2-3 sentence critique of the GRAPHIC only>",
    "strengths": ["<what works well visually>"],
    "weaknesses": ["<what fails visually>"],
    "ai_slop_detected": <boolean>,
    "ai_slop_indicators": ["<specific artifacts if any>"]
}}"""

    log_judge.info("  Calling %s for graphic evaluation...", VISION_MODEL)
    log_judge.info("  (Evaluating GRAPHIC ONLY — no text assessment)")
    log_judge.debug("  Image: %s...", image_url[:80])
    start = time.time()

    last_error = None
    for attempt in range(1, VISION_MAX_RETRIES + 2):
        try:
            response = governed_chat_completion(
                context=f"Vision/{VISION_MODEL}",
                model=VISION_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": image_url}},
                            {"type": "text", "text": "Evaluate this graphic design for t-shirt production. Focus on composition, color, aesthetic quality, and print viability. Ignore any text."},
                        ],
                    },
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
                timeout=VISION_CALL_TIMEOUT,
            )

            elapsed = time.time() - start
            log_judge.info("  Vision response received (%.2fs)", elapsed)

            content = safe_extract_content(response, context=f"Vision/{VISION_MODEL}")
            return content

        except ValueError as e:
            last_error = e
            if attempt <= VISION_MAX_RETRIES:
                log_judge.warning("  Vision attempt %d failed: %s. Retrying...", attempt, str(e)[:80])
                time.sleep(5)
            else:
                raise
        except Exception as e:
            last_error = e
            elapsed = time.time() - start
            if attempt <= VISION_MAX_RETRIES:
                log_judge.warning("  Vision attempt %d failed after %.1fs: %s. Retrying...", attempt, elapsed, str(e)[:80])
                time.sleep(5)
            else:
                log_judge.error("  Vision call failed after %.1fs: %s", elapsed, str(e)[:150])
                raise ValueError(f"Vision failed: {str(e)[:100]}")


def run_judge(brief):
    log_judge.info("=== JUDGE NODE STARTED ===")

    config = load_config()
    judge_config = config.get("judge", {})
    min_overall = judge_config.get("min_overall_score", 7.0)
    log_judge.info("  Threshold: overall >= %.1f", min_overall)
    log_judge.info("  Evaluating: GRAPHIC ONLY (no text/typography)")

    with get_connection() as conn:
        cursor = conn.execute("SELECT id, design_json FROM designs WHERE status = 'pending'")
        designs = cursor.fetchall()
        log_judge.info("  Found %d pending designs.", len(designs))

        if not designs:
            return None, "No designs to judge."

        best_id, best_score = None, -1
        failure_feedback = []

        for idx, (d_id, d_json) in enumerate(designs, 1):
            design = json.loads(d_json)
            image_url = design.get("image_url")

            if not image_url:
                log_judge.warning("  [%d/%d] ID %d: no image_url. Skipping.", idx, len(designs), d_id)
                failure_feedback.append("- Missing image URL")
                continue

            log_judge.info("  [%d/%d] '%s' (ID: %d)", idx, len(designs), design.get("slogan", "?"), d_id)

            if not verify_image_accessible(image_url):
                log_judge.warning("    Image not accessible. Skipping.")
                failure_feedback.append("- Image not accessible")
                continue

            try:
                raw_response = call_vision_llm(image_url, brief)
            except (ValueError, Exception) as e:
                log_judge.error("    Vision failed: %s", str(e)[:100])
                failure_feedback.append(f"- Vision error: {str(e)[:60]}")
                continue

            try:
                evaluation = json.loads(raw_response)
            except json.JSONDecodeError as e:
                log_judge.error("    JSON parse failed: %s", e)
                failure_feedback.append("- JSON parse error")
                continue

            scores = evaluation.get("scores", {})
            overall = evaluation.get("overall_score", 0.0)
            reasoning = evaluation.get("reasoning", "")
            strengths = evaluation.get("strengths", [])
            weaknesses = evaluation.get("weaknesses", [])
            ai_slop = evaluation.get("ai_slop_detected", False)
            ai_indicators = evaluation.get("ai_slop_indicators", [])

            # Ensure all keys exist
            for key in ["composition", "color_execution", "aesthetic_quality", "print_viability"]:
                if key not in scores:
                    scores[key] = 0.0

            design["judge_scores"] = scores
            design["judge_overall_score"] = overall
            design["judge_reasoning"] = reasoning
            design["judge_strengths"] = strengths
            design["judge_weaknesses"] = weaknesses
            design["ai_slop_detected"] = ai_slop
            design["ai_slop_indicators"] = ai_indicators

            conn.execute("UPDATE designs SET score = ?, design_json = ? WHERE id = ?",
                        (overall, json.dumps(design), d_id))

            log_judge.info("    Score: %.1f/10 | Comp: %.1f | Color: %.1f | Aesthetic: %.1f | Print: %.1f",
                          overall, scores["composition"], scores["color_execution"],
                          scores["aesthetic_quality"], scores["print_viability"])

            if strengths:
                log_judge.debug("    Strengths: %s", "; ".join(strengths[:2]))
            if weaknesses:
                log_judge.warning("    Weaknesses: %s", "; ".join(weaknesses[:2]))
            if ai_slop:
                log_judge.warning("    AI SLOP: %s", ", ".join(ai_indicators[:3]))

            log_judge.debug("    Reasoning: %s", reasoning[:200])

            if overall >= min_overall and overall > best_score:
                best_score, best_id = overall, d_id
                log_judge.info("    RESULT: PASSES ✓ (new best)")
            else:
                log_judge.info("    RESULT: REJECTED ✗")
                if weaknesses:
                    for w in weaknesses:
                        failure_feedback.append(f"- {w}")
                if ai_slop:
                    for ind in ai_indicators:
                        failure_feedback.append(f"- AI Slop: {ind}")

        if best_id:
            conn.execute("UPDATE designs SET status = 'shortlisted' WHERE id = ?", (best_id,))
            conn.execute("UPDATE designs SET status = 'rejected' WHERE status = 'pending' AND id != ?", (best_id,))
            log_judge.info("  SHORTLISTED: ID %d (score %.1f)", best_id, best_score)
        else:
            conn.execute("UPDATE designs SET status = 'rejected' WHERE status = 'pending'")
            log_judge.warning("  No designs passed. All rejected.")

        log_judge.info("=== JUDGE NODE COMPLETE ===")

        if best_id:
            return best_id, None
        else:
            unique_fb = list(set(failure_feedback))[:5]
            return None, " | ".join(unique_fb) if unique_fb else "All below threshold."

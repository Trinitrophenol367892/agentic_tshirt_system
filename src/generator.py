"""
Generator Node — 5 distinct art directions, now AUDIENCE-AWARE (FIX 2).
The art director receives the measured audience psychographics + communities +
resonating references so the graphic is built FOR the audience, not just from the trend name.
"""
import json
import random
import time
import urllib.parse
import yaml
import requests
from .database import get_connection
from .llm_client import governed_chat_completion, MAIN_MODEL
from .llm_utils import safe_extract_content, sanitize_garment_color, sanitize_print_technique, sanitize_placement
from .logger import log_generator

POLLINATIONS_BASE_URL = "https://image.pollinations.ai/prompt"
IMAGE_VERIFY_TIMEOUT = 60
IMAGE_MAX_RETRIES = 2
NEGATIVE_PROMPT = ("text, letters, words, numbers, typography, font, writing, caption, watermark, signature, logo, username, "
                   "blurry, low quality, distorted, deformed, ugly, photorealistic face, human, person, hands, fingers, "
                   "stock photo, generic, clip art, 3d render, overexposed, underexposed, washed out, muddy colors")


def load_config():
    with open("config.yaml", "r") as f:
        return yaml.safe_load(f)


def _audience_context_block(brief):
    """Build a plain-string audience/resonance context block (no f-string brace risk)."""
    lines = []
    ap = brief.get("audience_psychographics", {}) or {}
    ai = brief.get("audience_intelligence", {}) or {}
    refs = brief.get("resonating_references", []) or []
    if ap.get("primary_demo") or ap.get("core_desire") or ap.get("trigger_phrases"):
        lines.append("TARGET AUDIENCE (design the graphic to appeal to THESE people):")
        if ap.get("primary_demo"):
            lines.append(f"  Who: {ap['primary_demo']}")
        if ap.get("core_desire"):
            lines.append(f"  Core desire: {ap['core_desire']}")
        tp = ap.get("trigger_phrases", [])
        if tp:
            lines.append(f"  Trigger phrases/visual hooks they respond to: {', '.join(tp) if isinstance(tp, list) else tp}")
    mc = ai.get("measured_communities", [])
    mcr = ai.get("measured_creators", [])
    if mc or mcr:
        lines.append("MEASURED WHERE/WHO (the real communities & creators this is hot with):")
        if mc:
            lines.append(f"  Communities: {', '.join(mc)}")
        if mcr:
            lines.append(f"  Creators: {', '.join(mcr)}")
    if refs:
        lines.append("RESONATING REFERENCES (visual language winning right now — echo its energy, do not copy):")
        for r in refs[:5]:
            lines.append(f"  - {r}")
    return "\n".join(lines)


def generate_5_art_directions(brief, feedback=None):
    d = brief.get("design_directives", {})
    visual_style = d.get("visual_style", "contemporary graphic design")
    palette = d.get("color_palette", {})
    primary_colors = palette.get("primary", ["#FFFFFF"])
    bg_color = palette.get("background", "#000000")
    layout_rules = d.get("layout_rules", "bold composition")
    negative_constraints = d.get("negative_constraints", [])
    colors_str = ", ".join(str(c) for c in primary_colors) if isinstance(primary_colors, list) else str(primary_colors)
    neg_str = ", ".join(str(c) for c in negative_constraints) if isinstance(negative_constraints, list) else str(negative_constraints)

    audience_block = _audience_context_block(brief)
    feedback_section = ""
    if feedback:
        feedback_section = ("\nPREVIOUS ATTEMPTS REJECTED FOR:\n" + feedback +
                            "\nThe new prompts MUST address these. Push a DIFFERENT visual direction.\n")

    prompt = f"""You are a world-class art director creating 5 DISTINCT visual directions for a premium streetwear t-shirt graphic. Each must be a COMPLETELY DIFFERENT composition.

DESIGN BRIEF:
- Visual style: {visual_style}
- Color palette: {colors_str} on {bg_color}
- Layout: {layout_rules}
- Must avoid: {neg_str}
{feedback_section}
{audience_block}

DESIGN PRINCIPLES (vary emphasis per direction): multi-layering (foreground/midground/background, seamless blends); Gestalt (figure-ground, closure, continuity); cinematic (rule of thirds, leading lines, single dramatic light, depth-of-field); 20-30% active whitespace, asymmetric balance; intentional angling (diagonals, dutch angle, radial bursts, offset hero); concrete material textures (rusted metal, liquid chrome, cracked concrete, halftone, grain) with real light behavior; color as hierarchy (max 3-4 colors, warm advances/cool recedes); production-aware (screen print/DTG 12-16in, max 4-5 seps, bold shapes, clean edges).

5 DISTINCT structures:
1 DIAGONAL CASCADE  2 RADIAL BURST  3 ASYMMETRIC TENSION  4 LAYERED DEPTH  5 MINIMAL MONOLITH

For each, a concrete visual prompt (<90 words): what the viewer SEES, where elements sit, how light behaves, the mood (3 words). End each with: "isolated on solid black background, high contrast, screen print ready, graphic design, no text". Make the imagery resonate with the TARGET AUDIENCE above.

Return ONLY valid JSON:
{{"directions": [
  {{"id":1,"type":"diagonal cascade","visual_prompt":"...","mood":"..."}},
  {{"id":2,"type":"radial burst","visual_prompt":"...","mood":"..."}},
  {{"id":3,"type":"asymmetric tension","visual_prompt":"...","mood":"..."}},
  {{"id":4,"type":"layered depth","visual_prompt":"...","mood":"..."}},
  {{"id":5,"type":"minimal monolith","visual_prompt":"...","mood":"..."}}
]}}"""

    log_generator.info("  Generating 5 audience-aware art directions via %s...", MAIN_MODEL)
    start = time.time()
    response = governed_chat_completion(context=f"ArtDirections/{MAIN_MODEL}", model=MAIN_MODEL,
                                        messages=[{"role": "user", "content": prompt}],
                                        response_format={"type": "json_object"}, temperature=0.8)
    log_generator.info("  Art directions received (%.2fs)", time.time() - start)
    data = json.loads(safe_extract_content(response, context=f"ArtDirections/{MAIN_MODEL}"))
    directions = data.get("directions", [])
    while len(directions) < 5:
        directions.append(directions[-1] if directions else {"id": len(directions)+1, "type": "fallback", "visual_prompt": visual_style, "mood": "bold graphic"})
    log_generator.info("  5 distinct directions:")
    for dd in directions[:5]:
        log_generator.info("    [%s] %s | mood: %s", dd.get("id", "?"), dd.get("type", "?"), dd.get("mood", "?"))
        log_generator.debug("        \"%s\"", dd.get("visual_prompt", "")[:120])
    return directions[:5]


def generate_image(visual_prompt, seed, config):
    g = config.get("generator", {})
    model = g.get("image_model", "flux")
    w = g.get("image_width", 1024); h = g.get("image_height", 1024)
    url = f"{POLLINATIONS_BASE_URL}/{urllib.parse.quote(visual_prompt, safe='')}" \
          f"?width={w}&height={h}&seed={seed}&model={model}&nologo=true" \
          f"&negative_prompt={urllib.parse.quote(NEGATIVE_PROMPT, safe='')}"
    log_generator.debug("    Generating (seed=%d, model=%s)...", seed, model)
    start = time.time(); success = False
    for attempt in range(1, IMAGE_MAX_RETRIES + 1):
        try:
            r = requests.get(url, timeout=IMAGE_VERIFY_TIMEOUT, stream=True)
            if r.status_code == 200 and "image" in r.headers.get("Content-Type", ""):
                log_generator.debug("    Generated (%.1fs)", time.time() - start); success = True; break
            log_generator.warning("    HTTP %d. Retry %d...", r.status_code, attempt); time.sleep(5)
        except requests.exceptions.Timeout:
            log_generator.warning("    Timeout. Retry %d...", attempt)
        except requests.exceptions.RequestException as e:
            log_generator.warning("    Error: %s. Retry %d...", str(e)[:60], attempt); time.sleep(3)
    if not success:
        log_generator.error("    FAILED after %d attempts.", IMAGE_MAX_RETRIES)
    return url


def generate_slogans(brief, feedback=None):
    c = brief.get("copywriting_directives", {})
    tone = c.get("tone", "contemporary")
    formulas = c.get("slogan_formulas", ["short punchy phrase"])
    inc = c.get("keywords_to_include", []); av = c.get("keywords_to_avoid", [])
    formulas_str = ", ".join(str(f) for f in formulas) if isinstance(formulas, list) else str(formulas)
    inc_str = ", ".join(str(k) for k in inc) if isinstance(inc, list) else str(inc)
    av_str = ", ".join(str(k) for k in av) if isinstance(av, list) else str(av)
    fb = f"\n\nPREVIOUS REJECTED: {feedback}. Generate completely different slogans." if feedback else ""
    prompt = (f"Generate exactly 5 t-shirt slogans (one per candidate).\nTone: {tone}\nFormulas: {formulas_str}\n"
              f"Include concepts from: {inc_str}\nMUST NOT include: {av_str}\n"
              f"Each under 8 words. Punchy, memorable, wearable.{fb}\n\n"
              'Return ONLY valid JSON: {"slogans": ["s1","s2","s3","s4","s5"]}')
    log_generator.info("  Generating slogans via %s...", MAIN_MODEL)
    start = time.time()
    response = governed_chat_completion(context=f"Slogans/{MAIN_MODEL}", model=MAIN_MODEL,
                                        messages=[{"role": "user", "content": prompt}],
                                        response_format={"type": "json_object"}, temperature=0.7)
    log_generator.info("  Slogans received (%.2fs)", time.time() - start)
    data = json.loads(safe_extract_content(response, context=f"Slogans/{MAIN_MODEL}"))
    slogans = data.get("slogans", ["DEFAULT SLOGAN"])
    for i, s in enumerate(slogans, 1):
        log_generator.debug("    [%d] %s", i, s)
    return slogans


def run_generator(brief, feedback=None):
    log_generator.info("=== GENERATOR NODE STARTED ===")
    log_generator.info("  Mode: %s", "CORRECTIVE" if feedback else "INITIAL")
    config = load_config()
    n = config.get("generator", {}).get("candidates_per_attempt", 5)
    directions = generate_5_art_directions(brief, feedback=feedback)
    slogans = generate_slogans(brief, feedback=feedback)
    while len(slogans) < n:
        slogans.append(slogans[-1] if slogans else "DEFAULT")
    specs = brief.get("product_specifications", {})
    raw_g = specs.get("best_garment_colors", ["Black"])
    garment_colors = [sanitize_garment_color(g) for g in raw_g] if isinstance(raw_g, list) else [sanitize_garment_color(str(raw_g))]
    print_technique = sanitize_print_technique(str(specs.get("print_technique", "DTG (Direct to Garment)")))
    placement = sanitize_placement(str(specs.get("placement", "Center chest print")))
    log_generator.info("  Generating %d images (each a DIFFERENT composition)...", n)
    designs = []
    for i in range(min(n, len(directions))):
        dd = directions[i]
        slogan = slogans[i] if i < len(slogans) else slogans[-1]
        seed = random.randint(0, 999999)
        log_generator.info("  Candidate [%d/%d] '%s' | %s", i+1, n, slogan, dd.get("type", "?"))
        image_url = generate_image(dd.get("visual_prompt", ""), seed, config)
        designs.append({"image_prompt": dd.get("visual_prompt", ""), "image_url": image_url, "slogan": slogan,
                        "garment_color": random.choice(garment_colors), "print_technique": print_technique,
                        "placement": placement, "image_seed": seed, "mood": dd.get("mood", ""),
                        "composition_type": dd.get("type", "unknown")})
        if i < n - 1:
            time.sleep(2)
    with get_connection() as conn:
        brief_id = conn.execute("SELECT id FROM briefs ORDER BY timestamp DESC LIMIT 1").fetchone()[0]
        for dd in designs:
            conn.execute("INSERT INTO designs (brief_id, design_json) VALUES (?, ?)", (brief_id, json.dumps(dd)))
    log_generator.info("=== GENERATOR COMPLETE: %d distinct candidates stored ===", len(designs))

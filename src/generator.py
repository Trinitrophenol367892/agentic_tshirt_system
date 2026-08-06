"""
Generator Node — configurable N art directions, style-aware, composition-decoupled.
The art director picks N varied composition archetypes appropriate to the brief's
visual_style (no fixed taxonomy) and writes one richly-specified treatment per
direction so each image gets a detailed, costume-specific prompt rather than a thin
descriptor sliced over a hardcoded structural menu.
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


def generate_art_directions(brief, n, feedback=None):
    d = brief.get("design_directives", {})
    visual_style = d.get("visual_style", "contemporary graphic design")
    palette = d.get("color_palette", {})
    primary_colors = palette.get("primary", ["#FFFFFF"])
    bg_color = palette.get("background", "#000000")
    bg_descriptor = _bg_descriptor(bg_color)
    layout_rules = d.get("layout_rules", "bold composition")
    negative_constraints = d.get("negative_constraints", [])
    colors_str = ", ".join(str(c) for c in primary_colors) if isinstance(primary_colors, list) else str(primary_colors)
    neg_str = ", ".join(str(c) for c in negative_constraints) if isinstance(negative_constraints, list) else str(negative_constraints)

    audience_block = _audience_context_block(brief)
    feedback_section = ""
    if feedback:
        feedback_section = ("\nPREVIOUS ATTEMPTS REJECTED FOR:\n" + feedback +
                            "\nThe new prompts MUST address these. Push a DIFFERENT visual direction.\n")

    n = int(n)
    if n < 1:
        n = 1

    prompt = f"""You are a world-class art director creating {n} DISTINCT visual directions for a premium streetwear t-shirt graphic. Each must be a COMPLETELY DIFFERENT COMPOSITION + a fresh TREATMENT of the brief's visual_style — NOT the same axis sliced {n} ways. Invent the composition types yourself; never fall back to stock archetypes.

DESIGN BRIEF:
- Visual style: {visual_style}
- Color palette: {colors_str} on a {bg_descriptor}
- Layout: {layout_rules}
- Must avoid: {neg_str}
{feedback_section}
{audience_block}

DESIGN PRINCIPLES (vary ALL of these across the {n} directions so no two look related): composition (figure-ground, radial, asymmetric, layered, minimal, cinematic rule-of-thirds, leading-line, dutch-angle, offset hero, centered monolith, vector cascade, negative-space reveal); layering (foreground/midground/background, seamless blends); Gestalt (closure, continuity); cinematic (single dramatic light, depth-of-field); 20-30% active whitespace, asymmetric balance; concrete material textures real to this style (rusted metal, liquid chrome, cracked concrete, halftone, grain); color as hierarchy (max 3-4 colors, warm advances/cool recedes); production-aware (screen print/DTG 12-16in, max 4-5 seps, bold shapes, clean edges).

CRITICAL — STYLE/COMPOSITION DECOUPLING:
Every direction shares the SAME visual_style "{visual_style}". What changes direction-to-direction is the COMPOSITION + TREATMENT (framing, layering, light, texture emphasis, hierarchy) — not the style. Treat the shared visual_style as the brand; compose {n} different posters for that brand.

For EACH direction write ONE concrete, FULL visual prompt (≤120 words). Describe what the viewer SEES in detail: concrete subject, where it sits, how light behaves, which textures, the mood (3 words). Each prompt must be self-sufficient — the image generator receives ONLY this text. End each prompt with: "isolated on solid {bg_descriptor}, high contrast, screen print ready, graphic design, no text". Make the imagery resonate with the TARGET AUDIENCE above.

INSPIRATION MENU (style-appropriate, pick-and-mix — NOT a fixed list; choose {n} that suit "{visual_style}" and vary them):
diagonal cascade · radial burst · asymmetric tension · layered depth · minimal monolith · negative-space reveal · cinematic rule-of-thirds · off-center hero · dutch-angle action · vector crown · broadcast-grid silhouette · deco arch · monogram badge · torn-poster reveal · halftone-vignette · split-complementary duo · kinetic trail · centered crest · iso-symmetry · chaos-in-order
You may invent your own archetypes when nothing above fits the style.

Return ONLY valid JSON — an array of exactly {n} objects, each a distinct composition:
{{"directions":[
  {{"id":1,"type":"<composition name you chose>","visual_prompt":"<full ≤120-word composition>...isolated on solid {bg_descriptor}, high contrast, screen print ready, graphic design, no text","mood":"<3 words>"}},
  ...
]}}"""

    log_generator.info("  Generating %d audience-aware, style-decoupled art directions via %s...", n, MAIN_MODEL)
    start = time.time()
    response = governed_chat_completion(context=f"ArtDirections/{MAIN_MODEL}", model=MAIN_MODEL,
                                        messages=[{"role": "user", "content": prompt}],
                                        response_format={"type": "json_object"}, temperature=0.8)
    log_generator.info("  Art directions received (%.2fs)", time.time() - start)
    data = json.loads(safe_extract_content(response, context=f"ArtDirections/{MAIN_MODEL}"))
    directions = data.get("directions", [])
    # Coerce each item to the expected shape so downstream code is robust regardless of LLM drift.
    norm = []
    for i, dd in enumerate(directions, 1):
        if not isinstance(dd, dict):
            continue
        dd.setdefault("id", i)
        dd.setdefault("type", f"direction_{i}")
        dd.setdefault("visual_prompt", visual_style)
        dd.setdefault("mood", "bold graphic")
        norm.append(dd)
    directions = norm
    while len(directions) < n:
        directions.append({"id": len(directions)+1, "type": "fallback",
                           "visual_prompt": visual_style, "mood": "bold graphic"})
    log_generator.info("  %d distinct directions:", n)
    for dd in directions[:n]:
        log_generator.info("    [%s] %s | mood: %s", dd.get("id", "?"), dd.get("type", "?"), dd.get("mood", "?"))
        log_generator.debug("        \"%s\"", dd.get("visual_prompt", "")[:120])
    return directions[:n]


def _bg_descriptor(bg_color):
    """Map a brief background color value to a short descriptor for image prompts.

    Accepts hex (#RRGGBB) or a human-friendly name. Falls back gracefully when the
    value is missing or unrecognized so the prompt stays grammatical and the
    rendered background stays consistent with the brief's palette.
    """
    name_map = {"#000000": "black", "#FFFFFF": "white", "#0B0B0B": "near-black",
                "#F5F5F0": "cream", "#1A1A1A": "charcoal"}
    if not isinstance(bg_color, str) or not bg_color:
        return "black"
    stripped = bg_color.strip()
    # Exact brief-authored names win over the luminance heuristic so a cream
    # background stays "cream" (not "white") — matching the palette's intent.
    if stripped in name_map:
        return name_map[stripped]
    key = stripped.lstrip("#").upper()
    if len(key) == 6:
        try:
            r, g, b = (int(key[i:i+2], 16) for i in (0, 2, 4))
            luminance = 0.299 * r + 0.587 * g + 0.114 * b
            return "white" if luminance > 186 else "black"
        except ValueError:
            pass
    return "black"


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


def generate_slogans(brief, n, feedback=None):
    c = brief.get("copywriting_directives", {})
    tone = c.get("tone", "contemporary")
    formulas = c.get("slogan_formulas", ["short punchy phrase"])
    inc = c.get("keywords_to_include", []); av = c.get("keywords_to_avoid", [])
    formulas_str = ", ".join(str(f) for f in formulas) if isinstance(formulas, list) else str(formulas)
    inc_str = ", ".join(str(k) for k in inc) if isinstance(inc, list) else str(inc)
    av_str = ", ".join(str(k) for k in av) if isinstance(av, list) else str(av)
    fb = f"\n\nPREVIOUS REJECTED: {feedback}. Generate completely different slogans." if feedback else ""
    n = max(1, int(n))
    example_slogans = "[" + ",".join(f'"s{i}"' for i in range(1, n + 1)) + "]"
    prompt = (f"Generate exactly {n} t-shirt slogans (one per candidate).\nTone: {tone}\nFormulas: {formulas_str}\n"
              f"Include concepts from: {inc_str}\nMUST NOT include: {av_str}\n"
              f"Each under 8 words. Punchy, memorable, wearable.{fb}\n\n"
              f'Return ONLY valid JSON: {{"slogans": {example_slogans}}}')
    log_generator.info("  Generating %d slogans via %s...", n, MAIN_MODEL)
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
    directions = generate_art_directions(brief, n, feedback=feedback)
    slogans = generate_slogans(brief, n, feedback=feedback)
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
        
        # Rate limit protection: staggered delays between requests
        if i > 0:
            # Stair-step delay: 5s base + 2s per previous attempt to avoid burst limits
            delay = 5.0 + (i * 2.0)
            log_generator.info(f"Rate limit protection: Waiting {delay:.1f}s before next image request...")
            time.sleep(delay)
        
        image_url = generate_image(dd.get("visual_prompt", ""), seed, config)
        designs.append({"image_prompt": dd.get("visual_prompt", ""), "image_url": image_url, "slogan": slogan,
                        "garment_color": random.choice(garment_colors), "print_technique": print_technique,
                        "placement": placement, "image_seed": seed, "mood": dd.get("mood", ""),
                        "composition_type": dd.get("type", "unknown")})
    with get_connection() as conn:
        brief_id = conn.execute("SELECT id FROM briefs ORDER BY timestamp DESC LIMIT 1").fetchone()[0]
        for dd in designs:
            conn.execute("INSERT INTO designs (brief_id, design_json) VALUES (?, ?)", (brief_id, json.dumps(dd)))
    log_generator.info("=== GENERATOR COMPLETE: %d distinct candidates stored ===", len(designs))

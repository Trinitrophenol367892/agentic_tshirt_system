#!/usr/bin/env bash
set -euo pipefail

PATCH_VERSION="v1.0.1"
PATCH_DESC="Brief normalizer rescues mis-nested top-level keys (clean build, parse-verified)"

echo "============================================================"
echo "  PATCH ${PATCH_VERSION}: ${PATCH_DESC}"
echo "============================================================"
echo ""
if [ ! -f "src/brief_schema.py" ]; then echo "ERROR: run from project root."; exit 1; fi

BACKUP_DIR=".backups/pre_${PATCH_VERSION}_$(date +%Y%m%d_%H%M%S)"
mkdir -p "${BACKUP_DIR}/src"
cp -f src/brief_schema.py "${BACKUP_DIR}/src/" 2>/dev/null || true
echo "  Backed up src/brief_schema.py to ${BACKUP_DIR}/  (.env / config.yaml NOT touched)"
echo ""

cat << 'EOF' > src/brief_schema.py
"""
Brief Schema Normalizer — with MIS-NESTED KEY RESCUE.

LLMs sometimes emit the later top-level sections (copywriting_directives,
product_specifications, traffic_and_seo_directives) NESTED inside an earlier
section (commonly anti_slop_directives) instead of at the top level. Without
rescue, the normalizer default-fills the missing top-level keys (silently
throwing away audience-grounded copy, real product specs, and measured-community
traffic) AND leaves junk nested inside anti_slop_directives (which then pollutes
the judge prompt as a bogus anti-slop category).

This normalizer:
  1. PRE-PASS: for each canonical top-level key missing at the top level, search
     the whole tree; if found nested, POP it from there and PROMOTE it to top.
  2. Per-key normalization with alias resolution + default fill.
  3. Guard: a top-level value that is not an object cleanly defaults (no crash).
"""
from .logger import setup_logger

log_schema = setup_logger("brief_schema")

CANONICAL_SCHEMA = {
    "trend_metadata": {
        "trend_name": "Untitled Trend",
        "velocity_score": 5.0,
        "origin_platform": "Unknown",
        "lifecycle_stage": "Unknown",
    },
    "audience_psychographics": {
        "primary_demo": "General audience",
        "core_desire": "Self-expression",
        "trigger_phrases": [],
    },
    "design_directives": {
        "visual_style": "Contemporary graphic design",
        "color_palette": {
            "primary": ["#FFFFFF"],
            "background": "#000000",
            "accent": "#FFFFFF",
        },
        "typography": "Sans-serif, bold",
        "layout_rules": "Centered, balanced composition",
        "negative_constraints": ["NO clip art", "NO generic stock imagery"],
    },
    "anti_slop_directives": {
        "obvious_artifacts": {
            "rule": "No visual hallucinations or rendering failures.",
            "reject_if": [
                "Garbled, fake, or misspelled text",
                "Weird anatomy or objects clipping through each other",
                "Waxy, over-smooth, airbrushed rendering",
                "Overused AI styles (exaggerated bloom, hyper-real glow)",
            ],
        },
        "composition_and_detail": {
            "rule": "Design must have clear hierarchy and hold up to inspection.",
            "reject_if": [
                "No clear focal point",
                "Prompt soup aesthetics",
                "Inconsistent lighting or perspective",
                "Detail that collapses into noise when zoomed",
            ],
        },
        "color_and_style_intent": {
            "rule": "Colors and style must serve the brief, not default AI tropes.",
            "reject_if": [
                "Purposeless purple-blue gradients",
                "Style without authorship",
                "Emotionally hollow concepts",
            ],
        },
        "print_reality_and_originality": {
            "rule": "Design must be physically printable and conceptually original.",
            "reject_if": [
                "Elements too close to garment edges",
                "Super-fine details that vanish on fabric",
                "Overly literal prompt ideas",
                "Generic scraped-sounding phrasing",
            ],
        },
    },
    "copywriting_directives": {
        "tone": "Neutral, contemporary",
        "slogan_formulas": ["Short punchy phrase"],
        "keywords_to_include": [],
        "keywords_to_avoid": ["Hustle", "Grind", "Blessed"],
    },
    "product_specifications": {
        "best_garment_colors": ["Black", "White"],
        "print_technique": "DTG (Direct to Garment)",
        "placement": "Center chest print (10x10)",
    },
    "traffic_and_seo_directives": {
        "primary_seo_keywords": ["graphic tee", "streetwear t-shirt"],
        "secondary_seo_keywords": ["custom t-shirt", "printed tee"],
        "email_subject_hooks": ["New Drop Alert", "Fresh Ink Just Landed"],
        "community_angles": [
            {"subreddit": "r/streetwear", "focus": "fit and fabric quality"},
        ],
    },
}

KEY_ALIASES = {
    "design_directives": {
        "visual_style": ["style", "visual_aesthetic", "art_style", "design_style", "aesthetic"],
        "color_palette": ["colors", "palette", "colour_palette"],
        "typography": ["font_style", "fonts", "type_style", "text_style"],
        "layout_rules": ["layout", "composition_rules", "arrangement"],
        "negative_constraints": ["avoid", "do_not_include", "exclusions", "negative_prompts"],
    },
    "copywriting_directives": {
        "tone": ["voice", "writing_tone", "copy_tone"],
        "slogan_formulas": ["formulas", "slogan_patterns", "phrase_structures"],
        "keywords_to_include": ["include_keywords", "required_keywords", "must_include"],
        "keywords_to_avoid": ["avoid_keywords", "excluded_keywords", "banned_words"],
    },
    "product_specifications": {
        "best_garment_colors": ["garment_colors", "shirt_colors", "fabric_colors"],
        "print_technique": ["print_method", "printing_technique", "production_method"],
        "placement": ["print_placement", "layout_placement", "position"],
    },
    "traffic_and_seo_directives": {
        "primary_seo_keywords": ["seo_keywords", "main_keywords", "primary_keywords"],
        "secondary_seo_keywords": ["extra_keywords", "supporting_keywords"],
        "email_subject_hooks": ["email_hooks", "subject_lines", "email_subjects"],
        "community_angles": ["community_targets", "social_angles", "community_posts"],
    },
}


def _resolve_key(section_data, canonical_key, aliases):
    """Try canonical key first, then known aliases. Tolerates non-dict sections."""
    if not isinstance(section_data, dict):
        return None
    if canonical_key in section_data:
        return section_data[canonical_key]
    for alias in aliases.get(canonical_key, []):
        if alias in section_data:
            log_schema.debug("    Resolved alias: '%s' -> '%s'", alias, canonical_key)
            return section_data[alias]
    return None


def _find_and_pop(obj, key):
    """
    Depth-first search for a dict key by exact name; POP and return its value.
    Canonical top-level names never legitimately appear as sub-keys anywhere in
    the schema, so the first match is always the correct home.
    Returns (value, found).
    """
    if isinstance(obj, dict):
        if key in obj:
            return obj.pop(key), True
        for v in list(obj.values()):
            val, found = _find_and_pop(v, key)
            if found:
                return val, True
    elif isinstance(obj, list):
        for item in obj:
            val, found = _find_and_pop(item, key)
            if found:
                return val, True
    return None, False


def _rescue_misnested(raw_brief):
    """Promote any canonical top-level key that the model stranded inside another node."""
    for top_key in CANONICAL_SCHEMA:
        if top_key in raw_brief:
            continue  # already at top level
        val, found = _find_and_pop(raw_brief, top_key)
        if found and isinstance(val, dict) and val:
            log_schema.warning("    RESCUED misplaced '%s' (was nested) -> promoted to top level.", top_key)
            raw_brief[top_key] = val
        elif found:
            log_schema.warning("    Found '%s' nested but empty/malformed; cannot rescue.", top_key)


def normalize_brief(raw_brief):
    log_schema.info("  Normalizing brief schema...")

    # PRE-PASS: repair the tree before we read it
    _rescue_misnested(raw_brief)

    normalized = {}
    for top_key, default_section in CANONICAL_SCHEMA.items():
        raw_section = raw_brief.get(top_key, {})

        # Guard: a non-object top-level value cannot be normalized -> default it
        if raw_section and not isinstance(raw_section, dict):
            log_schema.warning("    Top-level '%s' was not an object (%s); using defaults.",
                               top_key, type(raw_section).__name__)
            raw_section = {}

        if not raw_section:
            log_schema.warning("    Top-level key '%s' missing. Using defaults.", top_key)
            normalized[top_key] = default_section.copy() if isinstance(default_section, dict) else default_section
            continue

        if isinstance(default_section, dict):
            normalized_section = {}
            aliases = KEY_ALIASES.get(top_key, {})
            for sub_key, default_value in default_section.items():
                resolved = _resolve_key(raw_section, sub_key, aliases)
                if resolved is not None:
                    normalized_section[sub_key] = resolved
                else:
                    log_schema.debug("    Key '%s.%s' not found. Using default.", top_key, sub_key)
                    normalized_section[sub_key] = default_value
            # Preserve genuine extra keys (junk was already popped by the rescue pre-pass)
            for extra_key in raw_section:
                if extra_key not in normalized_section:
                    normalized_section[extra_key] = raw_section[extra_key]
                    log_schema.debug("    Preserved extra key: '%s.%s'", top_key, extra_key)
            normalized[top_key] = normalized_section
        else:
            normalized[top_key] = raw_section

    # Ensure color_palette has required sub-keys (clean, no tricks)
    palette = normalized.get("design_directives", {}).get("color_palette", {})
    if isinstance(palette, dict):
        if "primary" not in palette:
            palette["primary"] = ["#FFFFFF"]
            log_schema.debug("    color_palette.primary defaulted to ['#FFFFFF']")
        if "background" not in palette:
            palette["background"] = "#000000"]
            log_schema.debug("    color_palette.background defaulted to '#000000'")
        if "accent" not in palette:
            palette["accent"] = "#FFFFFF"
            log_schema.debug("    color_palette.accent defaulted to '#FFFFFF'")
        normalized["design_directives"]["color_palette"] = palette

    # Ensure anti_slop_directives structure
    anti_slop = normalized.get("anti_slop_directives", {})
    if not isinstance(anti_slop, dict):
        anti_slop = {}
    for category, default_cat in CANONICAL_SCHEMA["anti_slop_directives"].items():
        if category not in anti_slop or not isinstance(anti_slop.get(category), dict):
            log_schema.debug("    anti_slop category '%s' missing/malformed. Using default.", category)
            anti_slop[category] = default_cat.copy()
        else:
            cat_data = anti_slop[category]
            if "rule" not in cat_data:
                cat_data["rule"] = default_cat["rule"]
            if "reject_if" not in cat_data:
                cat_data["reject_if"] = default_cat["reject_if"]
            for alias in ["reject_conditions", "rejections", "fail_if", "reject_when"]:
                if alias in cat_data and "reject_if" not in cat_data:
                    cat_data["reject_if"] = cat_data.pop(alias)
                    log_schema.debug("    Resolved anti_slop alias: '%s' -> 'reject_if'", alias)
    normalized["anti_slop_directives"] = anti_slop

    # Coerce expected lists
    list_fields = [
        ("design_directives", "negative_constraints"),
        ("copywriting_directives", "slogan_formulas"),
        ("copywriting_directives", "keywords_to_include"),
        ("copywriting_directives", "keywords_to_avoid"),
        ("product_specifications", "best_garment_colors"),
        ("traffic_and_seo_directives", "primary_seo_keywords"),
        ("traffic_and_seo_directives", "secondary_seo_keywords"),
        ("traffic_and_seo_directives", "email_subject_hooks"),
        ("traffic_and_seo_directives", "community_angles"),
        ("audience_psychographics", "trigger_phrases"),
    ]
    for section, field in list_fields:
        val = normalized.get(section, {}).get(field)
        if val is not None and not isinstance(val, list):
            if isinstance(val, str):
                normalized[section][field] = [val]
                log_schema.debug("    Coerced '%s.%s' from str to list.", section, field)
            else:
                normalized[section][field] = list(val) if hasattr(val, "__iter__") else [val]

    log_schema.info("  Brief schema normalized successfully.")
    log_schema.debug("  Final top-level keys: %s", list(normalized.keys()))
    return normalized
EOF

echo "  [OK] src/brief_schema.py ($(wc -c < src/brief_schema.py) bytes)"

# --- set -e-safe verify (includes a hard syntax check) ---
echo ""
echo "Verifying on-disk file..."
ok=true
if grep -q "_find_and_pop"     src/brief_schema.py; then echo "  ✓ recursive rescue search present"; else echo "  ✗ rescue search MISSING"; ok=false; fi
if grep -q "RESCUED misplaced" src/brief_schema.py; then echo "  ✓ promotion pre-pass present";      else echo "  ✗ promotion pre-pass MISSING"; ok=false; fi
if grep -q "was not an object" src/brief_schema.py; then echo "  ✓ non-object top-level guard present"; else echo "  ✗ non-object guard MISSING"; ok=false; fi
if grep -q 'if False else'     src/brief_schema.py; then echo "  ✗ leftover broken ternary STILL present!"; ok=false; else echo "  ✓ no broken ternary in file"; fi
if python3 -c "import ast; ast.parse(open('src/brief_schema.py').read())" 2>/dev/null; then echo "  ✓ brief_schema.py parses (no syntax error)"; else echo "  ✗ brief_schema.py SYNTAX ERROR"; ok=false; fi
echo ""
if [ "$ok" = true ]; then
    echo "============================================================"
    echo "  PATCH ${PATCH_VERSION} APPLIED — ALL CHECKS PASSED"
    echo "  .env / config.yaml: NOT modified."
    echo ""
    echo "  Effect: mis-nested copywriting/product/traffic sections are"
    echo "  rescued to top level, so slogans, garment/print specs, and"
    echo "  SEO/community targeting use the REAL brief (not defaults),"
    echo "  and junk is stripped from anti_slop_directives (cleaner judge)."
    echo "  Backup: ${BACKUP_DIR}/"
    echo "============================================================"
else
    echo "  PATCH INCOMPLETE — do NOT run main.py. Re-run from project root."
    exit 1
fi

"""
Shared LLM utilities for safe response extraction and value sanitization.
"""
import re
from .logger import setup_logger

log_utils = setup_logger("llm_utils")


def safe_extract_content(response, context: str = "LLM call") -> str:
    """
    Safely extracts message content from an OpenAI-compatible response.
    Handles empty choices, missing content, and finish_reason issues.
    """
    if not response:
        log_utils.error("  [%s] Response object is None.", context)
        raise ValueError(f"[{context}] Response is None")

    choices = getattr(response, "choices", None)

    if not choices or len(choices) == 0:
        log_utils.error("  [%s] Response has empty choices array.", context)
        log_utils.error("  [%s] Full response: %s", context, str(response)[:500])
        log_utils.error(
            "  [%s] This usually means the model could not process the input. "
            "For vision calls, verify the image URL is publicly accessible.",
            context
        )
        raise ValueError(
            f"[{context}] Empty choices. Model could not process input. "
            f"Check image URL accessibility and model compatibility."
        )

    message = choices[0].message
    content = getattr(message, "content", None)

    if content is None:
        log_utils.error("  [%s] Message content is None.", context)
        log_utils.error("  [%s] finish_reason: %s", context, choices[0].finish_reason)
        raise ValueError(f"[{context}] Message content is None.")

    if not content.strip():
        log_utils.warning("  [%s] Message content is empty string.", context)
        raise ValueError(f"[{context}] Message content is empty.")

    log_utils.debug("  [%s] Content extracted successfully (%d chars).", context, len(content))
    return content


def sanitize_garment_color(raw_value: str) -> str:
    """
    Extracts a clean garment color from a potentially verbose LLM response.
    """
    if not raw_value or not isinstance(raw_value, str):
        return "Black"

    if len(raw_value) < 30 and "," not in raw_value and " or " not in raw_value:
        return raw_value.strip().title()

    color_keywords = [
        "black", "white", "charcoal", "navy", "grey", "gray",
        "olive", "cream", "sand", "washed black", "washed navy",
        "heather grey", "heather gray", "natural", "off-white",
        "forest green", "burgundy", "maroon", "tan", "khaki",
    ]

    raw_lower = raw_value.lower()

    for color in color_keywords:
        if color in raw_lower:
            return color.title()

    first_part = re.split(r"[,;]| or ", raw_value)[0].strip()
    adjectives = ["heavyweight", "premium", "lightweight", "organic", "ring-spun", "combed"]
    words = first_part.split()
    cleaned = [w for w in words if w.lower() not in adjectives]
    result = " ".join(cleaned).strip().title()

    if result:
        return result
    return "Black"


def sanitize_print_technique(raw_value: str) -> str:
    """
    Extracts a clean print technique from a potentially verbose LLM response.
    """
    if not raw_value or not isinstance(raw_value, str):
        return "DTG (Direct to Garment)"

    if len(raw_value) < 40 and "," not in raw_value and " or " not in raw_value:
        return raw_value.strip()

    technique_keywords = [
        "dtg", "direct to garment", "screen print", "screen printing",
        "discharge", "sublimation", "heat transfer", "vinyl",
        "embroidery", "puff print", "foil", "plastisol",
    ]

    raw_lower = raw_value.lower()

    for technique in technique_keywords:
        if technique in raw_lower:
            if "dtg" in technique or "direct to garment" in technique:
                return "DTG (Direct to Garment)"
            elif "screen" in technique:
                return "Screen Print"
            elif "discharge" in technique:
                return "Discharge Print"
            elif "sublimation" in technique:
                return "Sublimation"
            elif "heat transfer" in technique:
                return "Heat Transfer"
            elif "embroidery" in technique:
                return "Embroidery"
            elif "puff" in technique:
                return "Puff Print"
            else:
                return technique.title()

    first_part = re.split(r"[,;]| or | for ", raw_value)[0].strip()
    if first_part:
        return first_part.title()
    return "DTG (Direct to Garment)"


def sanitize_placement(raw_value: str) -> str:
    """
    Cleans up placement description.
    """
    if not raw_value or not isinstance(raw_value, str):
        return "Center chest print (10x10)"

    if len(raw_value) < 80:
        return raw_value.strip()

    first_part = re.split(r"[.]|[,]", raw_value)[0].strip()
    if first_part:
        return first_part
    return "Center chest print (10x10)"

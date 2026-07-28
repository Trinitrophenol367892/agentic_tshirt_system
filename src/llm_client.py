import os
import sys
import time
from openai import OpenAI
from dotenv import load_dotenv
from .logger import log_llm
from .rate_governor import governor

load_dotenv()

api_key = os.getenv("NVIDIA_NIM_API_KEY")
base_url = os.getenv("NVIDIA_NIM_BASE_URL")

if not api_key or not base_url:
    log_llm.critical("Missing NVIDIA_NIM_API_KEY or NVIDIA_NIM_BASE_URL in .env")
    sys.exit(1)

base_url = base_url.rstrip("/")

log_llm.info("Initializing OpenAI-compatible client...")
log_llm.debug("  Base URL: %s", base_url)
log_llm.debug("  API Key:  %s...%s", api_key[:12], api_key[-4:] if len(api_key) > 16 else "****")

llm_client = OpenAI(
    api_key=api_key,
    base_url=base_url,
    timeout=300.0,
)

MAIN_MODEL = os.getenv("MAIN_LLM_MODEL", "z-ai/glm-5.2")
VISION_MODEL = os.getenv("VISION_LLM_MODEL", "minimaxai/minimax-m3")

log_llm.info("Main LLM (text):    %s", MAIN_MODEL)
log_llm.info("Vision LLM (judge): %s", VISION_MODEL)
log_llm.info("Request timeout:    300s (5 min)")


def governed_chat_completion(context: str, **kwargs):
    """
    Wrapper that routes all API calls through the rate governor
    before making the actual request to NVIDIA NIM.
    """
    governor.acquire(context=context)
    return llm_client.chat.completions.create(**kwargs)


def _probe_inference(model: str, role: str) -> bool:
    """Issue a 1-token chat completion to verify the key has inference
    entitlement for this model. models.list() only proves the model is
    *visible* to the key, not that the key is *entitled to generate* on it —
    a read-only 200 on /models can hide a 403 on /chat/completions.
    """
    t0 = time.time()
    try:
        governor.acquire(context=f"probe_{role}")
        llm_client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
        log_llm.debug("  Inference probe OK for %s (%.2fs).", model, time.time() - t0)
        return True
    except Exception as e:
        log_llm.error("  Inference probe FAILED for %s: %s", model, e)
        return False


def validate_connection():
    log_llm.info("Validating NVIDIA NIM connection...")
    start = time.time()
    try:
        governor.acquire(context="validate_connection")
        models = llm_client.models.list()
        elapsed = time.time() - start
        model_ids = [m.id for m in models.data]
        log_llm.info("  Catalog reachable (%.2fs). %d models visible.", elapsed, len(model_ids))

        if MAIN_MODEL not in model_ids:
            log_llm.warning("  MAIN_LLM_MODEL '%s' NOT found in catalog.", MAIN_MODEL)
            matches = [m for m in model_ids if "glm" in m.lower()]
            log_llm.warning("  Available GLM models: %s", matches)
        else:
            log_llm.debug("  MAIN_LLM_MODEL '%s' visible in catalog.", MAIN_MODEL)

        if VISION_MODEL not in model_ids:
            log_llm.warning("  VISION_LLM_MODEL '%s' NOT found in catalog.", VISION_MODEL)
            matches = [m for m in model_ids if "minimax" in m.lower()]
            log_llm.warning("  Available MiniMax models: %s", matches)
        else:
            log_llm.debug("  VISION_LLM_MODEL '%s' visible in catalog.", VISION_MODEL)

    except Exception as e:
        log_llm.error("  Catalog probe FAILED (cannot reach /models): %s", e)
        log_llm.error("  Base URL: %s", base_url)
        return

    # Catalog visibility != inference entitlement. Probe generation directly so
    # a server-side 403 surfaces here, not after the full ingest sweep.
    main_ok = _probe_inference(MAIN_MODEL, "MAIN")
    vision_ok = _probe_inference(VISION_MODEL, "VISION")
    if main_ok and vision_ok:
        log_llm.info("  Connection fully OK: catalog + inference on %s and %s.",
                     MAIN_MODEL, VISION_MODEL)
    elif main_ok or vision_ok:
        log_llm.warning("  Partial inference readiness: MAIN=%s VISION=%s.",
                        main_ok, vision_ok)
    else:
        log_llm.error("  ERROR: %s and %s both reject inference (likely 403 Authorization).",
                     MAIN_MODEL, VISION_MODEL)
        log_llm.error("  Catalog still reads fine — this is an inference-entitlement")
        log_llm.error("  issue (credits/quota/key scope), NOT an endpoint/auth/format bug.")
        log_llm.error("  Check NVIDIA credits/entitlement at build.nvidia.com, or reissue the nvapi- key.")


validate_connection()

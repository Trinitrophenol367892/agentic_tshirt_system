import json
import time
import yaml
from . import ingest, analysis, generator, judge, approval, publisher, traffic
from .logger import log_orchestrator
from .database import get_connection
from .rate_governor import governor


def load_config():
    with open("config.yaml", "r") as f:
        return yaml.safe_load(f)


def cleanup_stale_designs():
    with get_connection() as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM designs WHERE status = 'pending'")
        count = cursor.fetchone()[0]
        if count > 0:
            conn.execute("UPDATE designs SET status = 'stale' WHERE status = 'pending'")
            log_orchestrator.info("  Cleaned up %d stale pending designs.", count)


def load_design_json(design_id):
    if not design_id:
        return None
    with get_connection() as conn:
        cursor = conn.execute("SELECT design_json FROM designs WHERE id = ?", (design_id,))
        row = cursor.fetchone()
        return json.loads(row[0]) if row else None


def force_rejected(design_id):
    """Belt-and-suspenders: ensure a rejected design can never be re-presented,
    regardless of which approval.py version left it in what state."""
    if not design_id:
        return
    with get_connection() as conn:
        conn.execute("UPDATE designs SET status = 'rejected' WHERE id = ? AND status NOT IN ('rejected', 'stale')",
                     (design_id,))


def _normalize_approval_result(res):
    """
    DEFENSIVE: accept the new 3-tuple OR a legacy single return value,
    so a file-sync mismatch can NEVER crash the pipeline into a halt.
    New:  (decision, reason, design_id)
    Legacy single: truthy id = approve; falsy = reject (loop with weak feedback).
    """
    if isinstance(res, tuple):
        decision = res[0] if len(res) > 0 else "none"
        reason = res[1] if len(res) > 1 else None
        design_id = res[2] if len(res) > 2 else None
        return decision, reason, design_id
    # legacy single value
    if res:
        log_orchestrator.warning("  approval.run_approval() returned a legacy single value (%s); treating as approve.", res)
        return "approve", None, res
    log_orchestrator.warning("  approval.run_approval() returned legacy falsy; treating as reject (loop will reassert).")
    return "reject", None, None


def build_execution_feedback(human_reason, rejected_design):
    parts = []
    if human_reason:
        parts.append(f"HUMAN REVIEWER REJECTED THIS DESIGN. Their reason: {human_reason}")
    else:
        parts.append("HUMAN REVIEWER REJECTED THIS DESIGN after viewing it, with no written reason. "
                     "Produce a MEANINGFULLY DIFFERENT execution (different composition, focal "
                     "treatment, texture language, layout). Do NOT repeat the same visual idea.")
    weaknesses = (rejected_design or {}).get("judge_weaknesses", []) or []
    slop = (rejected_design or {}).get("ai_slop_indicators", []) or []
    reasoning = (rejected_design or {}).get("judge_reasoning", "") or ""
    if weaknesses:
        parts.append("Automated judge weaknesses on the rejected design: " + "; ".join(str(w) for w in weaknesses[:3]))
    if slop:
        parts.append("AI-slop flags on the rejected design: " + "; ".join(str(s) for s in slop[:3]))
    if reasoning:
        parts.append(f"Judge critique: {reasoning[:200]}")
    parts.append("The next batch MUST directly address the above while keeping the same trend/brief intent.")
    return " | ".join(parts)


def record_confirmed_trend(brief, design_id):
    trend_name = brief.get("trend_metadata", {}).get("trend_name", "Unknown")
    design = load_design_json(design_id) or {}
    slogan = design.get("slogan", "Unknown")
    score = design.get("judge_overall_score", 0.0) or 0.0
    with get_connection() as conn:
        conn.execute("INSERT INTO confirmed_trends (trend_name, brief_theme, design_slogan, judge_score) VALUES (?, ?, ?, ?)",
                     (trend_name, trend_name, slogan, score))
    log_orchestrator.info("  [FEEDBACK] Recorded confirmed trend: '%s' (slogan: '%s', score: %.1f)", trend_name, slogan, score)


def run_pipeline():
    pipeline_start = time.time()
    config = load_config()
    max_attempts = config.get("orchestrator", {}).get("max_generation_attempts", 3)
    max_cycles = config.get("orchestrator", {}).get("max_approval_cycles", 3)

    log_orchestrator.info("=" * 60)
    log_orchestrator.info("ORCHESTRATION AGENT: Closed-Loop Pipeline (human-in-the-loop)")
    log_orchestrator.info("  Inner gen/judge attempts per cycle: %d", max_attempts)
    log_orchestrator.info("  Max human-approval cycles:          %d", max_cycles)
    log_orchestrator.info("=" * 60)

    log_orchestrator.info("[STEP 0] Cleanup")
    cleanup_stale_designs()

    log_orchestrator.info("[STEP 1] Data Ingestion + LLM Trend Validation")
    ingest.run_ingest()

    log_orchestrator.info("[STEP 2] Trend Analysis & Brief Generation")
    brief = analysis.run_analysis()

    approved_id = None
    cycle = 1
    cycle_feedback = None

    while cycle <= max_cycles:
        log_orchestrator.info("")
        log_orchestrator.info("#" * 22 + f" HUMAN-APPROVAL CYCLE {cycle}/{max_cycles} " + "#" * 22)
        cleanup_stale_designs()  # safety: never carry pending across cycles

        # ---- inner generate -> judge self-correction loop ----
        best_id = None
        inner_feedback = cycle_feedback
        attempt = 1
        while attempt <= max_attempts:
            log_orchestrator.info("-" * 16 + f" gen/judge attempt {attempt}/{max_attempts} " + "-" * 16)
            if inner_feedback:
                log_orchestrator.info("  Seeding generator with feedback: %s", inner_feedback[:140])
            generator.run_generator(brief, feedback=inner_feedback)
            best_id, inner_feedback = judge.run_judge(brief)
            if best_id:
                log_orchestrator.info("  Judge shortlisted design ID %d.", best_id)
                break
            log_orchestrator.warning("  Judge passed nothing this attempt.")
            attempt += 1

        if not best_id:
            log_orchestrator.warning("  Cycle %d: judge passed nothing. Reasserting (no human consult).", cycle)
            cycle_feedback = inner_feedback
            cycle += 1
            continue

        # ---- human approval gate ----
        log_orchestrator.info("[GATE] Human approval (Telegram)...")
        res = approval.run_approval()
        decision, reason, design_id = _normalize_approval_result(res)
        log_orchestrator.info("[GATE] Human decision: %s | reason: %s | design_id: %s", decision, reason, design_id)

        if decision == "approve":
            approved_id = design_id
            log_orchestrator.info("  Human APPROVED. Exiting loop -> publish.")
            break

        if decision == "timeout":
            log_orchestrator.warning("  Human did not respond. HALTING (absence is not a 'no' to loop on).")
            break

        if decision == "none":
            log_orchestrator.warning("  No design reached the gate. HALTING.")
            break

        # ---- decision == 'reject' : REASSERT THE LOOP (never halt here) ----
        log_orchestrator.info("")
        log_orchestrator.info("🔁🔁 HUMAN REJECTED — REASSERTING LOOP (NOT HALTING) 🔁🔁")
        force_rejected(design_id)  # ensure it is never shown again
        rejected = load_design_json(design_id) or {}

        if reason:
            scope = analysis.classify_rejection_scope(reason, brief)
        else:
            scope = "execution"
            log_orchestrator.info("  Bare /reject (no text) -> execution-level; using judge critique as feedback.")

        if scope == "direction":
            log_orchestrator.info("  [OPTION B] Direction veto -> rewriting brief from SAME trends (no re-ingest).")
            brief = analysis.run_analysis(direction_feedback=reason)
            cycle_feedback = None
        else:
            log_orchestrator.info("  [OPTION A] Execution reject -> same brief, new batch with combined feedback.")
            cycle_feedback = build_execution_feedback(reason, rejected)

        log_orchestrator.info("  Entering next cycle %d/%d with feedback seeded.", cycle + 1, max_cycles)
        cycle += 1
        # loop reasserts — NO break on reject

    # ---- post-loop ----
    if not approved_id:
        log_orchestrator.error("=" * 60)
        log_orchestrator.error("PIPELINE ENDED without human approval (cycles exhausted / timeout / gate empty).")
        log_orchestrator.error("NOTE: a human /reject does NOT reach this path — it reasserts the loop above.")
        log_orchestrator.error("=" * 60)
        governor.log_stats()
        return

    log_orchestrator.info("[STEP 5] Multi-Platform Publishing")
    publisher.run_publisher(approved_id, brief)
    log_orchestrator.info("[STEP 6] Traffic Steering")
    traffic.run_traffic_steering(approved_id, brief)
    log_orchestrator.info("[STEP 7] Feedback Learning")
    record_confirmed_trend(brief, approved_id)

    total_elapsed = time.time() - pipeline_start
    log_orchestrator.info("=" * 60)
    log_orchestrator.info("  PIPELINE COMPLETE | design %d published after %d cycle(s) | %.1fs",
                          approved_id, cycle, total_elapsed)
    governor.log_stats()
    log_orchestrator.info("=" * 60)

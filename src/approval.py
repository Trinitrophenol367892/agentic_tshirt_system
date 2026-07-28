"""
Approval Node — Human-in-the-Loop via Telegram.
Returns (decision, reason, design_id); decision in {approve, reject, timeout, none}.
"""
import json
import re
import time
import os
import requests
from dotenv import load_dotenv
from .database import get_connection
from .logger import log_approval

load_dotenv()

APPROVAL_TIMEOUT = 1800
POLL_INTERVAL = 5
LOCAL_IMG_PATH = "/tmp/tshirt_design_approval.jpg"


class TelegramApprovalBot:
    def __init__(self):
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self._last_update_id = 0
        log_approval.info("  TelegramApprovalBot init | chat_id=%s", self.chat_id)

    def _tg(self, method, data=None, files=None, timeout=30):
        try:
            resp = requests.post(f"{self.base_url}/{method}", data=data, files=files, timeout=timeout)
            try:
                body = resp.json()
            except Exception:
                body = {"_raw": resp.text[:300]}
            ok = body.get("ok", False)
            log_approval.info("  [TG %s] HTTP %d | ok=%s | %s",
                              method, resp.status_code, ok, body.get("description", "") if not ok else "delivered")
            return body
        except Exception as e:
            log_approval.error("  [TG %s] EXCEPTION: %s", method, str(e)[:150])
            return None

    def _send_text(self, text):
        return self._tg("sendMessage", data={"chat_id": self.chat_id, "text": text})

    def _flush_old_updates(self):
        log_approval.info("  Flushing stale Telegram updates...")
        try:
            resp = requests.get(f"{self.base_url}/getUpdates", params={"offset": 0, "timeout": 0}, timeout=10)
            updates = resp.json().get("result", [])
            if updates:
                self._last_update_id = max(u.get("update_id", 0) for u in updates)
                log_approval.info("  Flushed %d old updates (last id=%d).", len(updates), self._last_update_id)
            else:
                log_approval.debug("  No old updates to flush.")
        except Exception as e:
            log_approval.warning("  Flush failed (non-fatal): %s", str(e)[:80])

    def _download_image(self, image_url):
        log_approval.info("  Downloading image: %s...", image_url[:90])
        try:
            r = requests.get(image_url, timeout=60)
            if r.status_code == 200 and len(r.content) > 2000:
                with open(LOCAL_IMG_PATH, "wb") as f:
                    f.write(r.content)
                log_approval.info("  Image downloaded OK (%d KB).", len(r.content) // 1024)
                return LOCAL_IMG_PATH
            log_approval.error("  Download bad: HTTP %d | %d bytes", r.status_code, len(r.content))
            return None
        except Exception as e:
            log_approval.error("  Download exception: %s", str(e)[:100])
            return None

    def send_design_for_approval(self, design):
        image_url = design["image_url"]
        scores = design.get("judge_scores", {})
        short_caption = (
            f"Design: {design.get('slogan', 'N/A')}\n"
            f"Score: {design.get('judge_overall_score', 0):.1f}/10  [{design.get('composition_type', '?')}]\n"
            f"Reply /approve or /reject (add a reason after /reject to steer the next try)"
        )
        score_lines = [
            "GRAPHIC SCORES", "--------------------------",
            f"Overall:         {design.get('judge_overall_score', 0):.1f}/10",
            f"Composition:     {scores.get('composition', 0):.1f}/10",
            f"Color:           {scores.get('color_execution', 0):.1f}/10",
            f"Aesthetic:       {scores.get('aesthetic_quality', 0):.1f}/10",
            f"Print Viability: {scores.get('print_viability', 0):.1f}/10",
            f"Mood: {design.get('mood', 'N/A')}", "",
        ]
        for s in design.get("judge_strengths", [])[:2]:
            score_lines.append(f"+ {s[:110]}")
        for w in design.get("judge_weaknesses", [])[:2]:
            score_lines.append(f"- {w[:110]}")
        if design.get("ai_slop_detected"):
            score_lines += ["", "AI SLOP:"]
            for ind in design.get("ai_slop_indicators", [])[:2]:
                score_lines.append(f"! {ind[:90]}")
        reasoning = design.get("judge_reasoning", "")
        if reasoning:
            score_lines += ["", f"CRITIQUE: {reasoning[:220]}"]
        score_lines += ["", f"Garment: {design.get('garment_color', 'Black')} | {design.get('print_technique', 'DTG')}",
                        f"Placement: {design.get('placement', 'Center chest')}"]
        scores_text = "\n".join(score_lines)

        self._flush_old_updates()
        local_path = self._download_image(image_url)

        photo_ok = False
        if local_path and os.path.exists(local_path):
            try:
                with open(local_path, "rb") as fh:
                    body = self._tg("sendPhoto", data={"chat_id": self.chat_id, "caption": short_caption},
                                    files={"photo": ("design.jpg", fh, "image/jpeg")}, timeout=60)
                photo_ok = bool(body and body.get("ok"))
            except Exception as e:
                log_approval.error("  Photo file-upload exception: %s", str(e)[:120])
        if not photo_ok:
            log_approval.info("  File upload failed/missing; trying URL method...")
            body = self._tg("sendPhoto", data={"chat_id": self.chat_id, "photo": image_url, "caption": short_caption}, timeout=60)
            photo_ok = bool(body and body.get("ok"))
        if not photo_ok:
            log_approval.warning("  Photo send failed entirely; sending image URL as text.")
            self._send_text(f"IMAGE (tap to view):\n{image_url}")

        log_approval.info("  Sending scores breakdown as separate text message...")
        self._send_text(scores_text)
        return True

    def wait_for_response(self):
        log_approval.info("  Waiting for /approve or /reject (timeout %d min)...", APPROVAL_TIMEOUT // 60)
        start = time.time()
        tick = 0
        while True:
            elapsed = time.time() - start
            if elapsed >= APPROVAL_TIMEOUT:
                log_approval.warning("  Timeout -> 'timeout' (pipeline will halt on absence).")
                self._send_text("Approval timed out (no reply). Pipeline halting.")
                return ("timeout", None)
            tick += 1
            if tick % 12 == 0:
                log_approval.info("  Still waiting... (%d min)", int(elapsed // 60))
            try:
                resp = requests.get(f"{self.base_url}/getUpdates",
                                    params={"offset": self._last_update_id + 1, "timeout": POLL_INTERVAL,
                                            "allowed_updates": json.dumps(["message"])},
                                    timeout=POLL_INTERVAL + 10)
                if resp.status_code != 200:
                    time.sleep(2); continue
                for update in resp.json().get("result", []):
                    self._last_update_id = max(self._last_update_id, update.get("update_id", 0))
                    msg = update.get("message", {})
                    if str(msg.get("chat", {}).get("id", "")) != str(self.chat_id):
                        continue
                    raw = msg.get("text", "") or ""
                    if not raw.strip():
                        continue
                    log_approval.info("  Received from human: '%s'", raw.strip()[:120])
                    if re.match(r'^\s*/?approve\b', raw, flags=re.I):
                        log_approval.info("  APPROVED by human.")
                        self._send_text("Approved! Proceeding to publish.")
                        return ("approve", None)
                    m = re.match(r'^\s*/?reject\b\s*(.*)$', raw, flags=re.I | re.S)
                    if m:
                        reason = m.group(1).strip().lstrip(":; ").strip() or None
                        log_approval.info("  REJECTED by human. reason=%s", reason)
                        self._send_text("Rejected with feedback — regenerating." if reason else "Rejected — regenerating a fresh batch.")
                        return ("reject", reason)
            except requests.exceptions.Timeout:
                pass
            except requests.exceptions.RequestException as e:
                log_approval.warning("  Poll error: %s", str(e)[:80]); time.sleep(5)


def run_approval():
    """Returns (decision, reason, design_id)."""
    log_approval.info("=== APPROVAL NODE STARTED ===")
    with get_connection() as conn:
        cursor = conn.execute("SELECT id, design_json FROM designs WHERE status = 'shortlisted'")
        row = cursor.fetchone()
        if not row:
            log_approval.warning("  No shortlisted designs.")
            return ("none", None, None)
        d_id, d_json = row
        design = json.loads(d_json)
        log_approval.info("  Design ID %d: '%s'", d_id, design.get("slogan", "?"))
        bot = TelegramApprovalBot()
        bot.send_design_for_approval(design)
        decision, reason = bot.wait_for_response()
        if decision == "approve":
            conn.execute("UPDATE designs SET status = 'approved' WHERE id = ?", (d_id,))
            log_approval.info("  Marked approved.")
            return ("approve", None, d_id)
        else:
            conn.execute("UPDATE designs SET status = 'rejected' WHERE id = ?", (d_id,))
            log_approval.info("  Marked rejected (decision=%s).", decision)
            return (decision, reason, d_id)

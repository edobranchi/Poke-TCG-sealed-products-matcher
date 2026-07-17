"""Telegram notifications for the sealed collector.

Sends a message to your Telegram chat when interesting things happen:
  - run finished and published ok (with stats)
  - new products waiting in triage
  - run failed
  - publish failed

Credentials come from env vars only — never hardcoded, never committed:
  TELEGRAM_TOKEN   — bot token from @BotFather
  TELEGRAM_CHAT_ID — your personal chat id

If either env var is missing, all calls are silent no-ops so the rest of
the pipeline keeps running regardless.
"""

import os
import logging
import requests

log = logging.getLogger("notify")

_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
_API = "https://api.telegram.org/bot{token}/sendMessage"


def _enabled():
    return bool(_TOKEN and _CHAT_ID)


def send(text):
    if not _enabled():
        return
    try:
        requests.post(
            _API.format(token=_TOKEN),
            json={"chat_id": _CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10)
    except Exception as e:
        log.warning("telegram notification failed: %s", e)


def run_ok(version, products, matched, pending, console_url):
    lines = [
        "✅ <b>Sealed collector — run ok</b>",
        f"version {version} · {products} products · {matched} CM-matched",
    ]
    if pending:
        lines.append(
            f"🔔 <b>{pending} new product{'s' if pending > 1 else ''} waiting in triage</b>\n"
            f"<a href='{console_url}/triage'>Review in console →</a>")
    send("\n".join(lines))


def triage_reminder(pending, console_url):
    """Call this separately if you want a reminder after N days of pending items."""
    send(
        f"🔔 <b>{pending} product{'s' if pending > 1 else ''} still waiting in triage</b>\n"
        f"<a href='{console_url}/triage'>Review in console →</a>")


def publish_ok(version):
    send(f"📦 Published version {version} to GitHub")


def run_failed(stage, error):
    send(f"❌ <b>Sealed collector — run FAILED</b>\nstage: {stage}\n<code>{error[:300]}</code>")


def publish_failed(error):
    send(f"⚠️ <b>Publish failed</b> — DB not updated on GitHub\n<code>{error[:300]}</code>")

#!/usr/bin/env python3
"""Redémarre le relais hors bande puis vérifie le correctif HTTP 429 en prod."""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/tg-claude")
BOT = BASE / "bot.py"
TESTS = BASE / "tests"
JOURNAL = BASE / "state" / "relay-journal.jsonl"
RESULT = BASE / "state" / "deploy-429-result.json"
SERVICE = "tg-claude.service"


def command(*args: str) -> str:
    result = subprocess.run(args, check=True, capture_output=True, text=True, timeout=60)
    return result.stdout.strip()


def send(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        response.read()


def recent_events(since: datetime) -> list[dict]:
    result = []
    for line in JOURNAL.read_text(encoding="utf-8").splitlines()[-80:]:
        try:
            event = json.loads(line)
            at = datetime.fromisoformat(event["at"])
        except (KeyError, ValueError, json.JSONDecodeError):
            continue
        if at >= since:
            result.append(event)
    return result


def main() -> int:
    started = datetime.now(timezone.utc)
    checks: dict[str, object] = {"started_at": started.isoformat()}
    try:
        command("python3", "-m", "py_compile", str(BOT))
        command("python3", "-m", "unittest", "discover", "-s", str(TESTS), "-q")
        source = BOT.read_text(encoding="utf-8")
        if "mark_claude_unavailable(\"sonde Claude HTTP 429\")" in source:
            raise RuntimeError("ancienne classification HTTP 429 encore présente")

        old_pid = int(command("systemctl", "show", SERVICE, "-p", "MainPID", "--value"))
        command("systemctl", "restart", SERVICE)
        time.sleep(8)
        if command("systemctl", "is-active", SERVICE) != "active":
            raise RuntimeError("service inactif après redémarrage")
        new_pid = int(command("systemctl", "show", SERVICE, "-p", "MainPID", "--value"))
        if not new_pid or new_pid == old_pid:
            raise RuntimeError("le PID principal n'a pas été renouvelé")

        process_env = Path(f"/proc/{new_pid}/environ").read_bytes().split(b"\0")
        if any(item.startswith(b"CLAUDE_CODE_OAUTH_TOKEN=") for item in process_env):
            raise RuntimeError("le vieux jeton OAuth statique est encore injecté")

        events = recent_events(started)
        usage_events = [event for event in events if event.get("kind") == "claude_usage"]
        if not usage_events:
            raise RuntimeError("le nouveau relais n'a pas lu le snapshot d'usage")
        usage = float(usage_events[-1]["usage"])
        if usage >= 98:
            raise RuntimeError(f"usage Claude encore au seuil de bascule: {usage:.0f}%")
        if any(event.get("kind") == "claude_unavailable" for event in events):
            raise RuntimeError("Claude a été marqué indisponible après le redémarrage")

        checks.update({
            "ok": True,
            "old_pid": old_pid,
            "new_pid": new_pid,
            "claude_usage": usage,
            "preferred_engine": "claude",
            "static_oauth_token_removed": True,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        RESULT.write_text(json.dumps(checks, indent=2), encoding="utf-8")
        try:
            send(
                "✅ Correctif 429 déployé et vérifié en production.\n"
                f"Claude est de nouveau prioritaire (usage 5 h : {usage:.0f} %).\n"
                "La sonde locale est active et le vieux jeton OAuth n'est plus injecté."
            )
        except Exception as exc:
            checks["notification_error"] = f"{type(exc).__name__}: {exc}"
            RESULT.write_text(json.dumps(checks, indent=2), encoding="utf-8")
        return 0
    except Exception as exc:
        checks.update({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        RESULT.write_text(json.dumps(checks, indent=2), encoding="utf-8")
        try:
            send(f"❌ Échec de la vérification du correctif 429 : {exc}")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

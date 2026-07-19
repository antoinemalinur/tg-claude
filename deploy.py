#!/usr/bin/env python3
"""Déploie le relais depuis /root/repos/tg-claude vers /opt/tg-claude.

Le redémarrage tue la tâche en cours — donc le processus Claude qui répond sur
Telegram. On le lance hors bande (systemd-run) une fois la réponse partie, et on
vérifie le service avant de confirmer.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path("/root/repos/tg-claude")
PROD = Path("/opt/tg-claude")
SERVICE = "tg-claude.service"
FILES = ("bot.py", "handoff.py", "confirm_hook.py", "settings.json")


def run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=120).stdout.strip()


def send(text: str) -> None:
    """Notifie via l'environnement du service (le token n'est que là)."""
    try:
        pid = run("systemctl", "show", SERVICE, "-p", "MainPID", "--value")
        env = dict(
            item.split("=", 1)
            for item in Path(f"/proc/{pid}/environ").read_bytes().decode().split("\0")
            if "=" in item
        )
        import urllib.parse
        import urllib.request
        data = urllib.parse.urlencode(
            {"chat_id": env["TELEGRAM_CHAT_ID"], "text": text}
        ).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/sendMessage",
            data=data,
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
    except Exception as exc:  # une notif ratée ne doit pas casser le déploiement
        print(f"notification impossible: {exc}", file=sys.stderr)


def main() -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = PROD / f"backup-{stamp}"
    try:
        # 1. Tests sur le code du dépôt avant toute copie.
        run("python3", "-m", "unittest", "discover", "-s", str(REPO / "tests"), "-q")

        # 2. Sauvegarde de la prod actuelle, puis copie.
        backup.mkdir(parents=True, exist_ok=True)
        for name in FILES:
            if (PROD / name).exists():
                shutil.copy2(PROD / name, backup / name)
        for name in FILES:
            shutil.copy2(REPO / name, PROD / name)
        shutil.copy2(REPO / "tests" / "test_bot.py", PROD / "tests" / "test_bot.py")

        # 3. Les mêmes tests, cette fois sur le code déployé.
        run("python3", "-m", "unittest", "discover", "-s", str(PROD / "tests"), "-q")

        old_pid = run("systemctl", "show", SERVICE, "-p", "MainPID", "--value")
        run("systemctl", "restart", SERVICE)
        time.sleep(8)
        if run("systemctl", "is-active", SERVICE) != "active":
            raise RuntimeError("service inactif après redémarrage")
        new_pid = run("systemctl", "show", SERVICE, "-p", "MainPID", "--value")
        if not new_pid or new_pid == old_pid:
            raise RuntimeError("le PID principal n'a pas été renouvelé")

        send("✅ Relais redéployé : transfert de contexte par fichier de passation, "
             "sessions natives Claude et Codex, bascule dans les deux sens.\n"
             f"Sauvegarde de l'ancienne version : {backup}")
        print(f"déployé; sauvegarde dans {backup}")
        return 0
    except Exception as exc:
        detail = getattr(exc, "stderr", "") or ""
        message = f"{type(exc).__name__}: {exc}\n{detail}".strip()
        print(message, file=sys.stderr)
        # Retour arrière si la prod avait déjà été touchée.
        if backup.exists():
            for name in FILES:
                if (backup / name).exists():
                    shutil.copy2(backup / name, PROD / name)
            subprocess.run(["systemctl", "restart", SERVICE], capture_output=True)
        send(f"❌ Déploiement du relais annulé, version précédente restaurée.\n{message[:900]}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

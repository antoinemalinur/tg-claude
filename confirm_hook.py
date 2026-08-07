#!/usr/bin/env python3
"""PreToolUse hook (matcher=Bash) : garde-fou de confirmation.

Reçoit sur stdin le JSON du hook Claude Code. Décide :
  - commande sûre        -> exit 0 (autorisée, silencieux)
  - commande dangereuse  -> publie STATE_DIR/pending pour Hublot, puis attend
                            sa décision dans STATE_DIR/decision.
                            allow -> exit 0 ; deny/timeout -> exit 2 (bloquée).

exit 2 renvoie le stderr à Claude pour qu'il sache que c'est bloqué.
Fail-safe : en cas d'erreur sur une commande dangereuse, on BLOQUE.
"""
import json
import os
import re
import sys
import time

STATE_DIR = os.environ.get("TG_STATE_DIR", "/opt/tg-claude/state")
WAIT_SECONDS = int(os.environ.get("TG_CONFIRM_TIMEOUT", "300"))
LOG = os.path.join(STATE_DIR, "hook.log")

# Motifs considérés comme "dommages graves" -> confirmation obligatoire.
DANGER = [
    r"\brm\s+-\w*[rf]",                          # rm -r / -rf / -f
    r"\brm\s+--(recursive|force)",
    r"\bdd\b",
    r"\bmkfs",
    r"\bshred\b",
    r"\btruncate\s+-s\s*0",
    r">\s*/dev/(sd|nvme|vd|xvd|disk|null\b.*\bof=)",
    r"\bof=/dev/",
    r":\(\)\s*\{",                               # fork bomb
    r"\bchmod\s+-R\b",
    r"\bchown\s+-R\b",
    r"\bgit\s+push\b[^|;&]*(--force|(?<!\w)-f\b|--delete)",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+-\w*[dfx]",
    r"\bfind\b[^|;&]*(-delete|-exec\s+rm)",
    r"\b(userdel|deluser|groupdel)\b",
    r"\b(shutdown|reboot|halt|poweroff|init\s+0)\b",
    r"\bsystemctl\s+(stop|disable|mask)\b",
    r"\b(iptables|nft)\b[^|;&]*(-F|flush)",
    r"\bufw\s+(reset|--force)",
    r"\bmv\b[^|;&]*\s/dev/null\b",
    r"\b(DROP|TRUNCATE)\s+(TABLE|DATABASE|SCHEMA)\b",
    r"\bapt(-get)?\s+(remove|purge|autoremove)\b",
    r"\bcrontab\s+-r\b",
    r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|k)?sh\b",  # curl ... | sh
    r"\bnpm\s+publish\b",
    r"\bkubectl\s+delete\b",
    r"\bdocker\s+(rm\b|rmi\b|system\s+prune|volume\s+rm)",
]
DANGER_RE = re.compile("|".join(DANGER), re.IGNORECASE)


def log(msg):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass

def main():
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        log(f"stdin parse error: {e}")
        sys.exit(0)  # pas de commande jugeable -> laisser passer

    tool = data.get("tool_name", "")
    cmd = (data.get("tool_input") or {}).get("command", "")
    if tool != "Bash" or not cmd:
        sys.exit(0)

    if not DANGER_RE.search(cmd):
        sys.exit(0)  # sûre -> autorisée silencieusement

    log(f"DANGER détecté: {cmd!r}")
    # La décision se prend dans Hublot : le serveur ACP guette ce même
    # fichier `pending` et la présente en natif. Telegram n'est plus le
    # canal — le doubler enverrait une alerte que plus personne ne lit.
    os.makedirs(STATE_DIR, exist_ok=True)
    pending = os.path.join(STATE_DIR, "pending")
    decision = os.path.join(STATE_DIR, "decision")
    for p in (pending, decision):
        try: os.remove(p)
        except FileNotFoundError: pass

    with open(pending, "w") as f:
        f.write(cmd)

    deadline = time.time() + WAIT_SECONDS
    verdict = None
    while time.time() < deadline:
        if os.path.exists(decision):
            try:
                with open(decision) as f:
                    verdict = f.read().strip()
            except Exception:
                verdict = None
            break
        time.sleep(1)

    for p in (pending, decision):
        try: os.remove(p)
        except FileNotFoundError: pass

    if verdict == "allow":
        log(f"AUTORISÉ par l'utilisateur: {cmd!r}")
        sys.exit(0)

    reason = "Refusé par l'utilisateur" if verdict == "deny" \
        else "Aucune confirmation reçue (délai dépassé)"
    log(f"BLOQUÉ ({reason}): {cmd!r}")
    print(f"Commande bloquée par le garde-fou de sécurité : {reason}. "
          "N'exécute PAS cette commande. Propose une alternative plus sûre "
          "ou demande une instruction à l'utilisateur.", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()

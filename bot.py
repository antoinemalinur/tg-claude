#!/usr/bin/env python3
"""Relais Telegram vers Claude Code avec bascule automatique vers Codex.

Claude reste le moteur normal. Le relais lit le snapshot produit par l'unique
moniteur d'usage local : à 98 % (ou après une erreur de quota explicite du CLI),
seules les *nouvelles* tâches vont à Codex. Une tâche déjà lancée n'est jamais
interrompue. Les erreurs de la sonde (notamment HTTP 429) ne rendent jamais
Claude indisponible.

Continuité du contexte : chaque moteur garde sa propre session native (Claude
via ``--resume``, Codex via ``codex exec resume``), donc tant qu'on reste sur le
même moteur rien n'est réinjecté. Au *changement* de moteur seulement, le
journal JSONL est condensé en un fichier de passation (``state/handoff.md``)
passé en paramètre au démarrage de la session entrante — dans les deux sens.
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import handoff as handoff_builder  # noqa: E402  (dépend du chemin ci-dessus)

BASE = "/opt/tg-claude"
STATE_DIR = os.path.join(BASE, "state")
SETTINGS = os.path.join(BASE, "settings.json")
OFFSET_FILE = os.path.join(STATE_DIR, "offset")
LOG = os.path.join(BASE, "logs", "bot.log")
JOURNAL = os.path.join(STATE_DIR, "relay-journal.jsonl")
HANDOFF_FILE = os.path.join(STATE_DIR, "handoff.md")
CODEX_LAST_MESSAGE = os.path.join(STATE_DIR, "codex-last-message.txt")
REPOS_BASE = "/root/repos"

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3"
CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.6-sol")

ALLOWED_TOOLS = [
    "Bash", "Edit", "Write", "Read", "Glob", "Grep", "WebSearch",
    "WebFetch", "TodoWrite", "NotebookEdit", "mcp__composio",
]
CLAUDE_TIMEOUT = 1800
CODEX_TIMEOUT = 1800
# Relecture du snapshot local (aucun appel réseau). La sonde Anthropic, elle,
# tourne dans claude-usage-monitor : c'est elle qui doit rester espacée.
USAGE_POLL_SECONDS = 120
USAGE_SNAPSHOT_MAX_AGE = 900
CLAUDE_LIMIT_PERCENT = 98.0
CLAUDE_RECOVERED_PERCENT = 5.0
CODEX_RETRY_SECONDS = 3600
# Le fichier de passation ne sert qu'au changement de moteur : on peut y mettre
# bien plus que l'ancienne capsule collée dans chaque prompt.
RECENT_EXCHANGES = 12
MAX_JOURNAL_TEXT = 1800
MAX_JOURNAL_RESPONSE = 6000
CLAUDE_USAGE_STATE = os.environ.get(
    "CLAUDE_USAGE_STATE", "/root/.claude-usage-monitor/state.json"
)

MODELS = {
    "opus": ("claude-opus-4-8", "Opus 4.8"),
    "sonnet": ("claude-sonnet-5", "Sonnet 5"),
    "haiku": ("claude-haiku-4-5-20251001", "Haiku 4.5"),
    "fable": ("claude-fable-5", "Fable 5"),
}
DEFAULT_MODEL = "opus"
MODEL_FILE = os.path.join(STATE_DIR, "model")

CONFIRM_WORDS = {"confirme", "confirm", "oui", "yes", "ok", "y", "✅"}
CANCEL_WORDS = {"annule", "cancel", "non", "no", "n", "❌", "stop"}
PROJECT_WORDS = re.compile(
    r"\b(analy[sz]e|analyse|modif|change|corrig|fix|bug|test|déplo|deploy|"
    r"commit|git|code|implément|implement|refactor|fichier|file|repo|projet|"
    r"project|build|install|configure)\b",
    re.IGNORECASE,
)
QUOTA_ERROR = re.compile(
    r"\b(usage.?limit|quota (?:reached|exceeded)|limit reached|"
    r"reached your .*limit|hit your .*limit|you(?:'|’)ve hit your limit)\b",
    re.IGNORECASE,
)
# Codex annonce ses propres plafonds autrement ; sans ça, une panne de quota
# Codex ne repartait jamais vers Claude (le relais n'était bidirectionnel que
# sur le papier).
# Une session peut disparaître (purge des rollouts, redémarrage, /clear) alors
# que le relais en garde l'id : sans reprise à neuf, chaque message échouerait.
STALE_SESSION = re.compile(
    r"(no conversation found|not a UUID and does not match|no rollout found|"
    r"session not found|thread/resume failed)",
    re.IGNORECASE,
)
CODEX_QUOTA_ERROR = re.compile(
    r"\b(usage limit|rate limit(?:ed)? .*(?:plan|weekly|5h)|"
    r"you(?:'|’)ve (?:hit|reached) your|out of credits?|"
    r"insufficient (?:quota|credits?)|upgrade to continue)\b",
    re.IGNORECASE,
)


class UsageSnapshotError(RuntimeError):
    """Le snapshot d'usage local est absent, invalide ou trop ancien."""

state: dict[str, Any] = {
    "cwd": REPOS_BASE,
    "claude_session_id": None,
    "codex_session_id": None,
    "model": DEFAULT_MODEL,
    "preferred_engine": "claude",
    "codex_unavailable_reason": None,
    "codex_unavailable_at": None,
    "last_engine": None,
    "active_task_engine": None,
    "claude_usage": None,
    "claude_reset_at": None,
    "claude_usage_observed_at": None,
    "claude_unavailable_reset_at": None,
    "claude_unavailable_reason": None,
    "usage_monitor_status": "initialisation",
}
state_lock = threading.RLock()
task_q: queue.Queue[dict[str, Any]] = queue.Queue()
deferred_q: queue.Queue[dict[str, Any]] = queue.Queue()


def log(msg: str) -> None:
    try:
        Path(LOG).parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def journal(kind: str, **fields: Any) -> None:
    """Ajoute un événement compact et non exécutable au journal de relais."""
    event = {"at": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields}
    try:
        Path(STATE_DIR).mkdir(parents=True, exist_ok=True)
        with open(JOURNAL, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as exc:
        log(f"journal error: {exc}")


def api(method: str, params: dict[str, Any] | None = None, timeout: int = 40) -> Any:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout) as r:
        return json.load(r)


def send(text: str) -> None:
    text = text if text and text.strip() else "(réponse vide)"
    for start in range(0, len(text), 4000):
        try:
            api("sendMessage", {"chat_id": CHAT_ID, "text": text[start:start + 4000],
                                "disable_web_page_preview": "true"})
        except Exception as exc:  # Telegram ne doit jamais faire tomber le worker.
            log(f"send error: {exc}")


def typing() -> None:
    try:
        api("sendChatAction", {"chat_id": CHAT_ID, "action": "typing"}, timeout=10)
    except Exception:
        pass


def load_offset() -> int | None:
    try:
        return int(Path(OFFSET_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def save_offset(offset: int) -> None:
    try:
        Path(OFFSET_FILE).write_text(str(offset), encoding="utf-8")
    except OSError as exc:
        log(f"offset save error: {exc}")


def load_model() -> str:
    try:
        model = Path(MODEL_FILE).read_text(encoding="utf-8").strip()
        return model if model in MODELS else DEFAULT_MODEL
    except OSError:
        return DEFAULT_MODEL


def save_model(model: str) -> None:
    try:
        Path(MODEL_FILE).write_text(model, encoding="utf-8")
    except OSError as exc:
        log(f"model save error: {exc}")


def truncate(value: str, limit: int = MAX_JOURNAL_TEXT) -> str:
    return value if len(value) <= limit else value[:limit] + "…"


def git_state(cwd: str) -> str:
    """Capture l'état Git sans modifier le dépôt; absent hors dépôt."""
    try:
        branch = subprocess.run(
            ["git", "branch", "--show-current"], cwd=cwd, capture_output=True,
            text=True, timeout=5,
        )
        status = subprocess.run(
            ["git", "status", "--short"], cwd=cwd, capture_output=True,
            text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "non disponible"
    if branch.returncode != 0:
        return "hors dépôt Git"
    lines = [f"branche: {branch.stdout.strip() or '(detached HEAD)'}"]
    dirty = status.stdout.strip()
    lines.append("modifications: " + (truncate(dirty, 900) if dirty else "aucune"))
    return "\n".join(lines)


def is_project_task(prompt: str, cwd: str) -> bool:
    """Évite de charger CLAUDE.md pour la conversation générale/hors projet."""
    if not os.path.isdir(cwd) or not os.path.exists(os.path.join(cwd, ".git")):
        # Un sous-dossier de dépôt est aussi un projet : git le détermine ci-dessous.
        try:
            return bool(PROJECT_WORDS.search(prompt)) and subprocess.run(
                ["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                capture_output=True, text=True, timeout=3,
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    return bool(PROJECT_WORDS.search(prompt))


def relevant_claude_files(cwd: str, prompt: str) -> list[Path]:
    """Indexe les chemins puis lit uniquement la hiérarchie pertinente."""
    if not is_project_task(prompt, cwd):
        return []
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=cwd,
            capture_output=True, text=True, timeout=3, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return []
    root, current = Path(top).resolve(), Path(cwd).resolve()
    try:
        current.relative_to(root)
    except ValueError:
        return []
    result: list[Path] = []
    directory = root
    for part in current.relative_to(root).parts:
        candidate = directory / "CLAUDE.md"
        if candidate.is_file():
            result.append(candidate)
        directory /= part
    candidate = directory / "CLAUDE.md"
    if candidate.is_file() and candidate not in result:
        result.append(candidate)
    return result


def project_instructions(cwd: str, prompt: str) -> str:
    chunks: list[str] = []
    for path in relevant_claude_files(cwd, prompt):
        try:
            chunks.append(f"--- {path} ---\n{path.read_text(encoding='utf-8')}")
        except OSError as exc:
            log(f"cannot read {path}: {exc}")
    if not chunks:
        return ""
    return "Instructions projet applicables (de la racine vers le dossier ciblé) :\n" + "\n\n".join(chunks)


def recent_journal() -> list[dict[str, Any]]:
    try:
        with open(JOURNAL, encoding="utf-8") as f:
            lines = f.readlines()[-80:]
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("kind") in {"message", "response", "task_error", "task_deferred"}:
            events.append(event)
    return events[-RECENT_EXCHANGES:]


def write_handoff(next_engine: str, objective: str, cwd: str, reason: str) -> str:
    """Écrit le fichier de passation et renvoie son chemin.

    Le fichier est réécrit à chaque bascule : il n'y en a qu'un, toujours à
    jour, et il est passé en paramètre au démarrage de la session entrante.
    """
    with state_lock:
        previous = state["last_engine"]
    document = handoff_builder.build_handoff(
        recent_journal(),
        from_engine=previous,
        to_engine=next_engine,
        objective=objective,
        cwd=cwd,
        git=git_state(cwd),
        reason=reason,
    )
    Path(STATE_DIR).mkdir(parents=True, exist_ok=True)
    Path(HANDOFF_FILE).write_text(document, encoding="utf-8")
    journal("handoff", from_engine=previous, to_engine=next_engine, reason=reason,
            chars=len(document))
    log(f"handoff {previous} -> {next_engine} ({len(document)} caractères; {reason})")
    return HANDOFF_FILE


def needs_handoff(engine: str) -> bool:
    """Vrai quand le moteur entrant n'a pas vécu le tour précédent.

    On ne réinjecte rien tant qu'on reste sur le même moteur : sa session
    native porte déjà le contexte. C'est exactement ce qui manquait avant —
    Codex repartait de zéro à chaque message.
    """
    with state_lock:
        previous = state["last_engine"]
        session = state["claude_session_id" if engine == "claude" else "codex_session_id"]
    if previous is None:
        return False
    return previous != engine or session is None


def task_prompt(engine: str, task: dict[str, Any]) -> str:
    """Le prompt ne porte plus que la demande : le contexte passe par le fichier."""
    cwd, text = task["cwd"], task["text"]
    instructions = project_instructions(cwd, text)
    parts = []
    if instructions:
        parts.append(instructions)
    parts.append("Nouvelle demande Telegram:\n" + text)
    return "\n\n".join(parts)


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise UsageSnapshotError("horodatage du snapshot invalide") from exc
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def load_claude_usage_snapshot() -> tuple[float, str | None, str, int, str | None]:
    """Lit le cache du moniteur sans faire de second appel à Anthropic."""
    path = Path(CLAUDE_USAGE_STATE)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise UsageSnapshotError("snapshot absent") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageSnapshotError("snapshot illisible") from exc

    raw = payload.get("last_percent")
    try:
        used = float(raw)
    except (TypeError, ValueError) as exc:
        raise UsageSnapshotError("pourcentage absent du snapshot") from exc
    if not 0 <= used <= 100:
        raise UsageSnapshotError("pourcentage hors limites dans le snapshot")

    observed = payload.get("observed_at")
    if observed:
        observed_at = _parse_datetime(str(observed))
        observed_label = str(observed)
    else:
        observed_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        observed_label = observed_at.isoformat()
    age = max(0, int((datetime.now(timezone.utc) - observed_at).total_seconds()))
    if age > USAGE_SNAPSHOT_MAX_AGE:
        raise UsageSnapshotError(f"snapshot périmé ({age} s)")

    reset = payload.get("last_reset")
    last_error = payload.get("last_error")
    return (
        used,
        str(reset) if reset else None,
        observed_label,
        age,
        str(last_error) if last_error else None,
    )


def format_reset(reset: str | None) -> str:
    if not reset:
        return "inconnu"
    try:
        epoch = float(reset)
        seconds = max(0, int(epoch - time.time()))
        return f"dans {seconds // 3600} h {(seconds % 3600) // 60:02d}"
    except ValueError:
        try:
            seconds = max(
                0,
                int((_parse_datetime(reset) - datetime.now(timezone.utc)).total_seconds()),
            )
            return f"dans {seconds // 3600} h {(seconds % 3600) // 60:02d}"
        except UsageSnapshotError:
            return reset


def mark_claude_unavailable(reason: str, used: float | None = None, reset: str | None = None) -> None:
    with state_lock:
        already = state["claude_unavailable_reason"] is not None
        state["preferred_engine"] = "codex"
        state["claude_unavailable_reason"] = reason
        state["claude_unavailable_reset_at"] = reset or state["claude_reset_at"]
        if used is not None:
            state["claude_usage"] = used
    journal("claude_unavailable", reason=reason, usage=used, reset_at=reset)
    if not already:
        usage_label = f"{used:.0f}%" if used is not None else "inconnu"
        send("⚠️ Claude indisponible pour les nouvelles tâches "
             f"({reason}; usage {usage_label}). Relais → Codex.")


def restore_claude(used: float, reset: str | None) -> None:
    with state_lock:
        old_reset = state["claude_unavailable_reset_at"]
        if state["claude_unavailable_reason"] is None or used > CLAUDE_RECOVERED_PERCENT:
            return
        if old_reset and reset == old_reset:
            return
        state["preferred_engine"] = "claude"
        state["claude_unavailable_reason"] = None
        state["claude_unavailable_reset_at"] = None
    journal("claude_restored", usage=used, reset_at=reset)
    send("✅ Nouvelle fenêtre Claude détectée; Claude reprend les prochaines tâches.")
    release_deferred_tasks()


def update_claude_usage() -> None:
    used, reset, observed, age, last_error = load_claude_usage_snapshot()
    with state_lock:
        unchanged = state["claude_usage_observed_at"] == observed
        state["claude_usage"] = used
        state["claude_reset_at"] = reset
        state["claude_usage_observed_at"] = observed
        suffix = f"; dernière erreur: {last_error}" if last_error else ""
        state["usage_monitor_status"] = f"actif (snapshot {age} s){suffix}"
    if unchanged:
        return
    journal("claude_usage", usage=used, reset_at=reset)
    if used is not None and used >= CLAUDE_LIMIT_PERCENT:
        mark_claude_unavailable("seuil 98 % atteint", used, reset)
    elif used is not None:
        restore_claude(used, reset)


def usage_monitor() -> None:
    while True:
        try:
            update_claude_usage()
        except UsageSnapshotError as exc:
            with state_lock:
                state["usage_monitor_status"] = str(exc)
            log(f"usage snapshot unavailable: {exc}")
        except Exception as exc:
            with state_lock:
                state["usage_monitor_status"] = f"erreur locale: {type(exc).__name__}"
            log(f"usage monitor error: {type(exc).__name__}: {exc}")
        time.sleep(USAGE_POLL_SECONDS)


def engine_for_next_task() -> str:
    with state_lock:
        engine = state["preferred_engine"]
        # Si le moteur préféré est justement celui qui est à plat, on repart
        # sur l'autre : le relais doit fonctionner dans les deux sens.
        if engine == "codex" and state["codex_unavailable_reason"]:
            return "claude"
        return engine


def engine_available(engine: str) -> bool:
    """Codex se réarme tout seul : son quota n'expose pas d'heure de reset."""
    with state_lock:
        if engine == "claude":
            return state["claude_unavailable_reason"] is None
        if state["codex_unavailable_reason"] is None:
            return True
        since = state["codex_unavailable_at"] or 0
        if time.time() - since < CODEX_RETRY_SECONDS:
            return False
        state["codex_unavailable_reason"] = None
        state["codex_unavailable_at"] = None
    journal("codex_restored", reason="délai de réarmement écoulé")
    return True


def is_quota_error(error: str) -> bool:
    return bool(QUOTA_ERROR.search(error))


def is_codex_quota_error(error: str) -> bool:
    return bool(CODEX_QUOTA_ERROR.search(error) or QUOTA_ERROR.search(error))


def mark_codex_unavailable(reason: str) -> None:
    with state_lock:
        already = state["codex_unavailable_reason"] is not None
        state["codex_unavailable_reason"] = reason
        state["codex_unavailable_at"] = time.time()
        if state["claude_unavailable_reason"] is None:
            state["preferred_engine"] = "claude"
    journal("codex_unavailable", reason=reason)
    if not already:
        send(f"⚠️ Codex indisponible ({reason}). Relais → Claude.")


def run_process(args: list[str], cwd: str, timeout: int,
                stdin_path: str | None = None) -> tuple[bool, str, str]:
    stream = None
    try:
        stream = open(stdin_path, "rb") if stdin_path else subprocess.DEVNULL
        process = subprocess.run(args, cwd=cwd, env=dict(os.environ), capture_output=True,
                                 text=True, timeout=timeout, stdin=stream)
    except subprocess.TimeoutExpired:
        return False, "", "tâche expirée (>30 min)"
    except OSError as exc:
        return False, "", str(exc)
    finally:
        if hasattr(stream, "close"):
            stream.close()
    stdout, stderr = (process.stdout or "").strip(), (process.stderr or "").strip()
    return process.returncode == 0, stdout, stderr


def run_claude(task: dict[str, Any], handoff_path: str | None) -> tuple[bool, str, str]:
    prompt = task_prompt("claude", task)
    with state_lock:
        session_id = state["claude_session_id"]
        model = state["model"]
        if not session_id:
            session_id = str(uuid.uuid4())
            state["claude_session_id"] = session_id
            resume = False
        else:
            resume = True
    args = ["claude", "-p", prompt, "--allowedTools", *ALLOWED_TOOLS,
            "--settings", SETTINGS, "--model", MODELS[model][0]]
    args += ["--resume", session_id] if resume else ["--session-id", session_id]
    # Le contexte de passation entre par le prompt système, pas par la demande :
    # Claude sait ainsi que c'est du contexte et non un ordre d'Antoine.
    if handoff_path:
        args += ["--append-system-prompt-file", handoff_path]
    log(f"claude cwd={task['cwd']} session={session_id} resume={resume} "
        f"handoff={bool(handoff_path)}")
    ok, out, err = run_process(args, task["cwd"], CLAUDE_TIMEOUT)
    if not ok and resume and is_stale_session(f"{out}\n{err}"):
        return retry_without_session("claude", task, handoff_path)
    return ok, out, err


def is_stale_session(error: str) -> bool:
    return bool(STALE_SESSION.search(error))


def retry_without_session(engine: str, task: dict[str, Any],
                          handoff_path: str | None) -> tuple[bool, str, str]:
    """Session perdue : on repart à neuf, avec la passation pour ne rien oublier."""
    key = "claude_session_id" if engine == "claude" else "codex_session_id"
    with state_lock:
        state[key] = None
    journal("session_reset", engine=engine)
    log(f"{engine}: session périmée, reprise à neuf")
    if handoff_path is None:
        handoff_path = write_handoff(engine, task["text"], task["cwd"],
                                     "session perdue, contexte reconstruit")
    runner = run_claude if engine == "claude" else run_codex
    return runner(task, handoff_path)


def codex_thread_id(stdout: str) -> str | None:
    """Récupère l'id de session émis par `codex exec --json`."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started" and event.get("thread_id"):
            return str(event["thread_id"])
    return None


def codex_reply(stdout: str) -> str:
    """Sort la réponse finale : le fichier -o d'abord, le flux JSON en secours."""
    try:
        text = Path(CODEX_LAST_MESSAGE).read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    messages = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") or {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            messages.append(str(item.get("text") or ""))
    return messages[-1].strip() if messages else ""


def run_codex(task: dict[str, Any], handoff_path: str | None) -> tuple[bool, str, str]:
    prompt = task_prompt("codex", task)
    with state_lock:
        session_id = state["codex_session_id"]
    # Le VPS est déjà le périmètre explicitement confié au relais; le mode
    # autonome est requis pour ne pas bloquer un message Telegram sur un TTY.
    common = ["--model", CODEX_MODEL, "--json",
              "--output-last-message", CODEX_LAST_MESSAGE,
              "--dangerously-bypass-approvals-and-sandbox",
              "--skip-git-repo-check"]
    if session_id:
        # `codex exec resume` refuse --cd : le dossier vient du cwd du process.
        args = ["codex", "exec", "resume", session_id, *common, prompt]
    else:
        args = ["codex", "exec", *common, "--cd", task["cwd"], prompt]
    try:
        Path(CODEX_LAST_MESSAGE).unlink()
    except OSError:
        pass
    # Codex n'a pas d'option « fichier de contexte » : son entrée standard est
    # justement prévue pour ça (elle arrive dans un bloc <stdin> distinct).
    log(f"codex cwd={task['cwd']} session={session_id or 'nouvelle'} "
        f"handoff={bool(handoff_path)}")
    ok, stdout, stderr = run_process(args, task["cwd"], CODEX_TIMEOUT,
                                     stdin_path=handoff_path)
    thread_id = codex_thread_id(stdout)
    if thread_id:
        with state_lock:
            state["codex_session_id"] = thread_id
    if not ok:
        failure = stderr or codex_reply(stdout) or "erreur Codex sans détail"
        if session_id and is_stale_session(f"{stdout}\n{failure}"):
            return retry_without_session("codex", task, handoff_path)
        return False, "", failure
    return True, codex_reply(stdout), stderr


def defer_task(task: dict[str, Any], error: str) -> None:
    deferred_q.put(task)
    journal("task_deferred", text=truncate(task["text"]), error=truncate(error), cwd=task["cwd"])
    send("⚠️ Codex n’a pas pu traiter cette tâche; elle est conservée et sera reprise "
         "par Claude à sa prochaine fenêtre.\n" + truncate(error, 700))


def release_deferred_tasks() -> None:
    released = 0
    while True:
        try:
            task_q.put_nowait(deferred_q.get_nowait())
            deferred_q.task_done()
            released += 1
        except queue.Empty:
            break
    if released:
        send(f"⏳ {released} tâche(s) conservée(s) remise(s) dans la file Claude.")


def run_engine(engine: str, task: dict[str, Any], reason: str) -> tuple[bool, str, str]:
    """Lance un moteur en lui passant, s'il y a lieu, le fichier de passation."""
    with state_lock:
        state["active_task_engine"] = engine
    path = None
    if needs_handoff(engine):
        path = write_handoff(engine, task["text"], task["cwd"], reason)
    runner = run_claude if engine == "claude" else run_codex
    return runner(task, path)


def run_task(task: dict[str, Any]) -> None:
    engine = engine_for_next_task()
    journal("task_started", engine=engine, text=truncate(task["text"]), cwd=task["cwd"],
            git=git_state(task["cwd"]))
    typing()
    ok, out, err = run_engine(engine, task, "changement de moteur entre deux messages")

    # Panne de quota en pleine tâche : on bascule sur l'autre moteur, dans les
    # deux sens, en lui passant la passation avec l'échec dedans.
    if not ok:
        failure = f"{out}\n{err}"
        other = "codex" if engine == "claude" else "claude"
        quota = is_quota_error(failure) if engine == "claude" else is_codex_quota_error(failure)
        if quota and engine_available(other):
            journal("task_error", engine=engine, error=truncate(err or out), cwd=task["cwd"])
            if engine == "claude":
                mark_claude_unavailable("erreur de quota Claude",
                                        reset=state.get("claude_reset_at"))
            else:
                mark_codex_unavailable("erreur de quota Codex")
            journal("task_rerouted", from_engine=engine, to_engine=other,
                    error=truncate(err or out))
            engine = other
            ok, out, err = run_engine(engine, task,
                                      f"quota {'Claude' if other == 'codex' else 'Codex'} "
                                      f"épuisé en pleine tâche")

    with state_lock:
        state["active_task_engine"] = None
    if not ok:
        journal("task_error", engine=engine, error=truncate(err or out), cwd=task["cwd"])
        if engine == "codex":
            defer_task(task, err or out or "erreur Codex sans détail")
        else:
            send(f"❌ Erreur Claude.\n{truncate(err or out, 1000)}")
        return

    response = out or "(réponse vide)"
    journal("response", engine=engine, text=truncate(response, MAX_JOURNAL_RESPONSE),
            cwd=task["cwd"], git=git_state(task["cwd"]))
    with state_lock:
        state["last_engine"] = engine
    footer = MODELS[state["model"]][1] if engine == "claude" else f"Codex · {CODEX_MODEL}"
    send(f"{response}\n\n— 🤖 {footer}")


def worker() -> None:
    while True:
        task = task_q.get()
        try:
            run_task(task)
        except Exception as exc:
            log(f"worker error: {exc}")
            journal("task_error", engine="internal", error=str(exc), cwd=task.get("cwd"))
            send(f"❌ Erreur interne: {exc}")
        finally:
            task_q.task_done()


def write_decision(verdict: str) -> None:
    try:
        Path(STATE_DIR, "decision").write_text(verdict, encoding="utf-8")
    except OSError as exc:
        log(f"decision write error: {exc}")


def pending_exists() -> bool:
    return os.path.exists(os.path.join(STATE_DIR, "pending"))


def download_voice(file_id: str) -> str:
    info = api("getFile", {"file_id": file_id}, timeout=20)
    if not info.get("ok"):
        raise RuntimeError(f"[getFile] ok=false: {str(info)[:200]}")
    path = info["result"]["file_path"]
    destination = os.path.join(STATE_DIR, "voice_in.ogg")
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}"
    with urllib.request.urlopen(url, timeout=60) as response, open(destination, "wb") as f:
        f.write(response.read())
    return destination


def groq_transcribe(path: str) -> str:
    boundary = "----tgbot" + uuid.uuid4().hex
    fields = {"model": GROQ_MODEL, "language": "fr", "response_format": "json"}
    audio = Path(path).read_bytes()
    body = bytearray()
    for key, value in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n").encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.ogg\"\r\n"
             "Content-Type: audio/ogg\r\n\r\n").encode()
    body += audio + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(GROQ_URL, data=bytes(body), headers={
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": "tg-claude-bot/1.0",
    })
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response).get("text", "").strip()


def handle_voice(voice: dict[str, Any]) -> str:
    if not GROQ_API_KEY:
        send("🎙️ Vocaux non configurés (GROQ_API_KEY manquante).")
        return ""
    if voice.get("duration", 0) > 600:
        send("🎙️ Vocal trop long (>10 min), ignoré.")
        return ""
    typing()
    path = None
    try:
        path = download_voice(voice["file_id"])
        text = groq_transcribe(path)
    except Exception as exc:
        log(f"voice error: {exc}")
        send(f"❌ Transcription échouée : {exc}")
        return ""
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
    if not text:
        send("🎙️ Rien compris dans ce vocal.")
        return ""
    send(f"🎙️ « {text} »")
    return text


def status_message() -> str:
    engine = engine_for_next_task()
    with state_lock:
        active = state["active_task_engine"] or "—"
        used = state["claude_usage"]
        reset = state["claude_reset_at"]
        reason = state["claude_unavailable_reason"]
        codex_down = state["codex_unavailable_reason"]
        cwd = state["cwd"]
        monitor = state["usage_monitor_status"]
        sessions = (
            ("Claude ✅" if state["claude_session_id"] else "Claude —")
            + " · "
            + ("Codex ✅" if state["codex_session_id"] else "Codex —")
        )
    usage = f"{used:.0f} %" if used is not None else "inconnu"
    relay = "Claude disponible" if engine == "claude" else f"Codex actif ({reason or 'Claude indisponible'})"
    if codex_down:
        relay += f" · Codex en panne ({codex_down})"
    return (f"📂 {cwd}\n🤖 prochaines tâches: {engine}\n⚙️ tâche en cours: {active}\n"
            f"📊 Claude 5 h: {usage}\n⏱️ reset estimé: {format_reset(reset)}\n"
            f"🔁 relais: {relay}\n🧵 sessions: {sessions}\n🔎 sonde usage: {monitor}\n"
            f"⏳ file: {task_q.unfinished_tasks} · différées: {deferred_q.qsize()}\n"
            f"⚠️ confirmation en attente: {'oui' if pending_exists() else 'non'}")


def handle_command(text: str) -> None:
    parts = text.strip().split(maxsplit=1)
    cmd, arg = parts[0].lower(), parts[1].strip() if len(parts) > 1 else ""
    if cmd in ("/help", "/start"):
        send("🤖 Pont Claude ↔ Codex\n\n"
             "• Écris un message → Claude traite par défaut; Codex prend le relais à 98 % d’usage Claude.\n"
             "• /cd <chemin>, /ls, /pwd, /new\n"
             "• /opus /sonnet /haiku /fable [message] — modèle Claude\n"
             "• /model — modèle Claude; /status — relais et quota\n"
             "• 🎙️ Vocal → transcription Groq puis tâche.\n\n"
             "Une tâche lancée n’est jamais interrompue par un basculement.")
    elif cmd == "/pwd":
        send(f"📂 {state['cwd']}")
    elif cmd == "/cd":
        with state_lock:
            new_path = arg if arg.startswith("/") else os.path.join(state["cwd"], arg)
        new_path = os.path.normpath(new_path)
        if os.path.isdir(new_path):
            with state_lock:
                state["cwd"] = new_path
                state["claude_session_id"] = None
                state["codex_session_id"] = None
            journal("cwd", cwd=new_path)
            send(f"📂 → {new_path}\n(nouvelle conversation; instructions projet réindexées)")
        else:
            send(f"❌ Dossier introuvable : {new_path}")
    elif cmd == "/ls":
        try:
            send(f"📁 {REPOS_BASE}\n" + "\n".join("• " + item for item in sorted(os.listdir(REPOS_BASE))))
        except OSError as exc:
            send(f"❌ {exc}")
    elif cmd == "/new":
        with state_lock:
            state["claude_session_id"] = None
            state["codex_session_id"] = None
            state["last_engine"] = None
        engine = engine_for_next_task()
        label = "Claude" if engine == "claude" else "Codex"
        send(f"🆕 Nouvelle conversation {label} (les deux moteurs repartent à zéro). "
             "Le journal de relais est conservé.")
    elif cmd == "/model":
        if not arg:
            send(f"🤖 Modèle Claude actuel : {MODELS[state['model']][1]}\n"
                 f"Pour changer : /model <nom>\nDispo : {' · '.join(MODELS)}")
        elif arg.lower() in MODELS:
            with state_lock:
                state["model"] = arg.lower()
                save_model(arg.lower())
            send(f"🤖 Modèle Claude → {MODELS[arg.lower()][1]} (contexte conservé).")
        else:
            send(f"❌ Modèle inconnu : {arg}\nDispo : {' · '.join(MODELS)}")
    elif cmd.lstrip("/") in MODELS:
        model = cmd.lstrip("/")
        with state_lock:
            state["model"] = model
            save_model(model)
        if arg:
            send(f"🤖 Claude → {MODELS[model][1]} · je traite ton message…")
            enqueue_task(arg)
        else:
            send(f"🤖 Modèle Claude → {MODELS[model][1]}.")
    elif cmd == "/status":
        send(status_message())
    else:
        send(f"❓ Commande inconnue : {cmd}. /help")


def enqueue_task(text: str) -> None:
    with state_lock:
        cwd = state["cwd"]
    task = {"id": str(uuid.uuid4()), "text": text, "cwd": cwd, "queued_at": time.time()}
    journal("message", text=truncate(text), cwd=cwd, git=git_state(cwd), next_engine=engine_for_next_task())
    if task_q.unfinished_tasks:
        send("⏳ Tâche en cours ; la tienne est mise en file.")
    task_q.put(task)


def handle_message(msg: dict[str, Any]) -> None:
    text = msg.get("text", "") or ""
    if not text and msg.get("voice"):
        text = handle_voice(msg["voice"])
    if not text.strip():
        return
    low = text.strip().lower()
    if pending_exists() and low in CONFIRM_WORDS | CANCEL_WORDS:
        write_decision("allow" if low in CONFIRM_WORDS else "deny")
        send("✅ Reçu — j'autorise." if low in CONFIRM_WORDS else "⛔ Reçu — j'annule.")
    elif text.strip().startswith("/"):
        handle_command(text)
    else:
        enqueue_task(text)


def drain_backlog() -> int | None:
    try:
        updates = api("getUpdates", {"timeout": 0}, timeout=15).get("result", [])
        if updates:
            offset = updates[-1]["update_id"] + 1
            save_offset(offset)
            return offset
    except Exception as exc:
        log(f"drain error: {exc}")
    return None


def main() -> None:
    Path(STATE_DIR).mkdir(parents=True, exist_ok=True)
    Path(REPOS_BASE).mkdir(parents=True, exist_ok=True)
    for name in ("pending", "decision"):
        try:
            Path(STATE_DIR, name).unlink()
        except FileNotFoundError:
            pass
    state["model"] = load_model()
    threading.Thread(target=worker, daemon=True, name="relay-worker").start()
    threading.Thread(target=usage_monitor, daemon=True, name="claude-usage-monitor").start()
    log(f"bot started (model={state['model']})")
    offset = load_offset() or drain_backlog()
    send("🟢 Pont Claude ↔ Codex démarré. /help pour les commandes.")
    while True:
        try:
            params: dict[str, Any] = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            response = api("getUpdates", params, timeout=40)
        except Exception as exc:
            log(f"getUpdates error: {exc}")
            time.sleep(3)
            continue
        if not response.get("ok"):
            log(f"getUpdates not ok: {response}")
            time.sleep(3)
            continue
        for update in response.get("result", []):
            offset = update["update_id"] + 1
            save_offset(offset)
            msg = update.get("message") or update.get("edited_message")
            if not msg:
                continue
            sender = str((msg.get("from") or {}).get("id", ""))
            if sender != CHAT_ID:
                log(f"ignored from {sender}: {str(msg.get('text', ''))[:80]!r}")
                continue
            try:
                handle_message(msg)
            except Exception as exc:
                log(f"handle error: {exc}")


if __name__ == "__main__":
    main()

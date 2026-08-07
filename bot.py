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

import glob
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import handoff as handoff_builder  # noqa: E402  (dépend du chemin ci-dessus)
import render  # noqa: E402  (idem)

BASE = "/opt/tg-claude"
STATE_DIR = os.path.join(BASE, "state")
SETTINGS = os.path.join(BASE, "settings.json")
OFFSET_FILE = os.path.join(STATE_DIR, "offset")
LOG = os.path.join(BASE, "logs", "bot.log")
JOURNAL = os.path.join(STATE_DIR, "relay-journal.jsonl")
HANDOFF_FILE = os.path.join(STATE_DIR, "handoff.md")
CODEX_LAST_MESSAGE = os.path.join(STATE_DIR, "codex-last-message.txt")
PHOTO_DIR = os.path.join(STATE_DIR, "photos")
PHOTO_RETENTION_S = 14 * 86400  # les images téléchargées au-delà sont purgées
REPOS_BASE = "/root/repos"

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3"
CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.6-sol")

ALLOWED_TOOLS = [
    "Agent", "Bash", "Edit", "Write", "Read", "Glob", "Grep", "WebSearch",
    "WebFetch", "TodoWrite", "NotebookEdit", "mcp__composio",
]
# Aucune limite de durée murale : certaines analyses, compilations et
# simulations légitimes dépassent 30 minutes. Le worker attend la fin réelle
# du moteur; /stop reste le mécanisme explicite et immédiat d'annulation.
# Telegram découpe les longs textes autour de 4096 caractères. On attend un
# court silence afin que tous les fragments deviennent une seule tâche, sans
# pour autant laisser un utilisateur qui envoie des messages en continu
# repousser indéfiniment son lancement.
MESSAGE_BATCH_QUIET_SECONDS = max(
    0.5, float(os.environ.get("MESSAGE_BATCH_QUIET_SECONDS", "4"))
)
MESSAGE_BATCH_MAX_SECONDS = max(
    MESSAGE_BATCH_QUIET_SECONDS,
    float(os.environ.get("MESSAGE_BATCH_MAX_SECONDS", "15")),
)
TELEGRAM_SPLIT_THRESHOLD = 3500
# Au-delà, la réponse part aussi en pièce jointe : quatre messages d'affilée
# dans un fil Telegram ne se relisent plus, un fichier se fait défiler.
MAX_RESPONSE_CHUNKS = 3
STOP_GRACE_SECONDS = 3.0
TASK_CANCELLED = "__TG_CLAUDE_TASK_CANCELLED__"
# Relecture du snapshot local (aucun appel réseau). La sonde Anthropic, elle,
# tourne dans claude-usage-monitor : c'est elle qui doit rester espacée.
USAGE_POLL_SECONDS = 120
USAGE_SNAPSHOT_MAX_AGE = 900
CLAUDE_LIMIT_PERCENT = 98.0
CLAUDE_RECOVERED_PERCENT = 5.0
# Les deux plafonds Claude. L'hebdomadaire bloque autant qu'une session pleine,
# mais se libère beaucoup plus tard : c'est LUI qui doit gouverner le retour,
# sinon un simple rollover 5 h rendrait la main à Claude devant un mur intact.
WINDOW_FIVE_HOUR = "five_hour"
WINDOW_WEEKLY = "seven_day"
WINDOW_LABELS = {WINDOW_FIVE_HOUR: "session 5 h", WINDOW_WEEKLY: "semaine"}
CODEX_RETRY_SECONDS = 3600
# Le fichier de passation ne sert qu'au changement de moteur : on peut y mettre
# bien plus que l'ancienne capsule collée dans chaque prompt.
RECENT_EXCHANGES = 12
MAX_JOURNAL_TEXT = 1800
MAX_JOURNAL_RESPONSE = 6000
CLAUDE_USAGE_STATE = os.environ.get(
    "CLAUDE_USAGE_STATE", "/root/.claude-usage-monitor/state.json"
)

# (id API, libellé, fenêtre de contexte en tokens — docs Anthropic 07/2026)
MODELS = {
    "opus": ("opus", "Opus 4.8", 1_000_000),
    "sonnet": ("claude-sonnet-5", "Sonnet 5", 1_000_000),
    "haiku": ("claude-haiku-4-5-20251001", "Haiku 4.5", 200_000),
    "fable": ("claude-fable-5", "Fable 5", 1_000_000),
}
DEFAULT_MODEL = "opus"
MODEL_FILE = os.path.join(STATE_DIR, "model")

# Niveaux séparés : Claude reste économe par défaut, tandis que Codex utilise
# l'avant-dernier niveau de GPT-5.6 Sol (max, juste avant ultra).
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
CODEX_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
DEFAULT_CLAUDE_EFFORT = "medium"
DEFAULT_CODEX_EFFORT = "max"
CLAUDE_EFFORT_FILE = os.path.join(STATE_DIR, "claude-effort")
CODEX_EFFORT_FILE = os.path.join(STATE_DIR, "codex-effort")

# Transcripts des sessions natives : c'est là (et seulement là) que vivent les
# compteurs de contexte — le CLI Claude n'expose rien en sortie texte, et le
# rollout Codex porte sa propre fenêtre (model_context_window).
CLAUDE_PROJECTS_DIR = os.environ.get("CLAUDE_PROJECTS_DIR", "/root/.claude/projects")
CODEX_SESSIONS_DIR = os.environ.get("CODEX_SESSIONS_DIR", "/root/.codex/sessions")
CODEX_FALLBACK_WINDOW = 272_000

ENGINES = ("claude", "codex")

# Clés d'état qui survivent à un redémarrage du service : sans elles, chaque
# déploiement perdait les sessions natives (et donc tout le contexte). Une
# épingle posée par /switch en fait partie : un redéploiement ne doit pas
# renvoyer les tâches sur un moteur qu'on venait de quitter à la main.
PERSISTED_KEYS = ("claude_session_id", "codex_session_id", "last_engine", "cwd",
                  "manual_engine")

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
    "claude_effort": DEFAULT_CLAUDE_EFFORT,
    "codex_effort": DEFAULT_CODEX_EFFORT,
    "preferred_engine": "claude",
    # Moteur imposé à la main par /switch; None = le relais choisit seul.
    "manual_engine": None,
    "codex_unavailable_reason": None,
    "codex_unavailable_at": None,
    "last_engine": None,
    "active_task_engine": None,
    "claude_usage": None,
    "claude_reset_at": None,
    "claude_weekly_usage": None,
    "claude_weekly_reset_at": None,
    "claude_usage_observed_at": None,
    "claude_unavailable_reset_at": None,
    "claude_unavailable_reason": None,
    # Fenêtre ayant causé le blocage : c'est son rollover à elle qu'on attend.
    "claude_unavailable_window": None,
    "usage_monitor_status": "initialisation",
}
state_lock = threading.RLock()
task_q: queue.Queue[dict[str, Any]] = queue.Queue()
deferred_q: queue.Queue[dict[str, Any]] = queue.Queue()
message_batch_lock = threading.RLock()
message_batch_parts: list[str] = []
message_batch_started_at: float | None = None
message_batch_cwd: str | None = None
message_batch_timer: threading.Timer | None = None
message_batch_generation = 0
active_process_lock = threading.RLock()
active_process: subprocess.Popen[str] | None = None
active_process_cancelled = False


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


def multipart(fields: dict[str, str], name: str, filename: str,
              content: bytes, mime: str) -> tuple[str, bytes]:
    """Corps multipart/form-data pour les envois de fichiers à l'API Bot."""
    boundary = "----tgbot" + uuid.uuid4().hex
    body = bytearray()
    for key, value in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f"name=\"{key}\"\r\n\r\n{value}\r\n").encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
             f"filename=\"{filename}\"\r\nContent-Type: {mime}\r\n\r\n").encode()
    body += content + f"\r\n--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", bytes(body)


def send_document(text: str, caption: str) -> bool:
    """Envoie une réponse longue en pièce jointe. False si l'envoi échoue."""
    filename = f"reponse-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
    content_type, body = multipart(
        {"chat_id": CHAT_ID, "caption": caption},
        "document", filename, text.encode("utf-8"), "text/plain; charset=utf-8")
    try:
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
            data=body, headers={"Content-Type": content_type})
        with urllib.request.urlopen(request, timeout=60) as response:
            return bool(json.load(response).get("ok"))
    except Exception as exc:
        log(f"sendDocument error: {exc}")
        return False


def post(chunk: str) -> None:
    """Poste un fragment en HTML, avec repli en texte brut.

    Telegram rejette le message *entier* (HTTP 400) si une balise lui déplaît :
    le repli garantit qu'un défaut de rendu ne fasse jamais perdre le contenu.
    """
    try:
        api("sendMessage", {"chat_id": CHAT_ID, "text": chunk, "parse_mode": "HTML",
                            "disable_web_page_preview": "true"})
        return
    except Exception as exc:
        log(f"send html error: {exc}")
    try:
        api("sendMessage", {"chat_id": CHAT_ID, "text": render.plain(chunk),
                            "disable_web_page_preview": "true"})
    except Exception as exc:  # Telegram ne doit jamais faire tomber le worker.
        log(f"send error: {exc}")


def send(text: str) -> None:
    text = text if text and text.strip() else "(réponse vide)"
    chunks = render.render(text)
    if len(chunks) > MAX_RESPONSE_CHUNKS:
        for chunk in chunks[:MAX_RESPONSE_CHUNKS - 1]:
            post(chunk)
        caption = (f"📎 Réponse longue ({len(text)} caractères) — "
                   "texte complet en pièce jointe.")
        if send_document(text, caption):
            return
        chunks = chunks[MAX_RESPONSE_CHUNKS - 1:]  # fichier refusé : on poste tout
    for chunk in chunks:
        post(chunk)


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


def load_effort(path: str, allowed: tuple[str, ...], default: str) -> str:
    try:
        effort = Path(path).read_text(encoding="utf-8").strip()
        return effort if effort in allowed else default
    except OSError:
        return default


def save_effort(path: str, effort: str) -> None:
    try:
        Path(path).write_text(effort, encoding="utf-8")
    except OSError as exc:
        log(f"effort save error: {exc}")


def _state_file(key: str) -> Path:
    return Path(STATE_DIR, key.replace("_", "-"))


def persist_state_key(key: str) -> None:
    """Écrit (ou efface) la valeur persistée d'une clé d'état. À appeler à
    chaque mutation d'une clé de PERSISTED_KEYS, sous state_lock ou non."""
    with state_lock:
        value = state.get(key)
    try:
        if value:
            _state_file(key).write_text(str(value), encoding="utf-8")
        else:
            _state_file(key).unlink(missing_ok=True)
    except OSError as exc:
        log(f"persist {key} error: {exc}")


def restore_persisted_state() -> None:
    """Recharge les sessions natives et le cwd après un redémarrage : la
    conversation reprend là où elle en était au lieu de repartir à zéro."""
    for key in PERSISTED_KEYS:
        try:
            value = _state_file(key).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not value:
            continue
        if key == "cwd" and not os.path.isdir(value):
            continue
        # Le fichier pilote le routage : un contenu inattendu est ignoré
        # plutôt qu'imposé aux prochaines tâches.
        if key == "manual_engine" and value not in ENGINES:
            continue
        with state_lock:
            state[key] = value


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


class UsageSnapshot(NamedTuple):
    """Les deux fenêtres publiées par la sonde. `weekly` est optionnelle :
    un snapshot écrit par une version antérieure du moniteur n'en a pas, et
    l'absence ne doit jamais rendre Claude indisponible."""
    used: float                 # fenêtre 5 h (toujours présente)
    reset: str | None
    weekly: float | None        # fenêtre 7 j (None = inconnue)
    weekly_reset: str | None
    observed: str
    age: int
    last_error: str | None

    def window(self, name: str) -> tuple[float | None, str | None]:
        """(pourcentage, reset) de la fenêtre nommée."""
        if name == WINDOW_WEEKLY:
            return self.weekly, self.weekly_reset
        return self.used, self.reset


def load_claude_usage_snapshot() -> UsageSnapshot:
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

    # Hebdo absente ou aberrante : on la déclare inconnue plutôt que de lever.
    # Une sonde plus ancienne ne doit pas priver le relais de sa fenêtre 5 h.
    try:
        weekly: float | None = float(payload.get("weekly_percent"))
        if not 0 <= weekly <= 100:
            weekly = None
    except (TypeError, ValueError):
        weekly = None

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
    weekly_reset = payload.get("weekly_reset")
    last_error = payload.get("last_error")
    return UsageSnapshot(
        used=used,
        reset=str(reset) if reset else None,
        weekly=weekly,
        weekly_reset=str(weekly_reset) if weekly_reset else None,
        observed=observed_label,
        age=age,
        last_error=str(last_error) if last_error else None,
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


def mark_claude_unavailable(reason: str, used: float | None = None, reset: str | None = None,
                            window: str | None = None) -> None:
    with state_lock:
        already = state["claude_unavailable_reason"] is not None
        state["preferred_engine"] = "codex"
        state["claude_unavailable_reason"] = reason
        state["claude_unavailable_reset_at"] = reset or state["claude_reset_at"]
        # Sans fenêtre identifiée (erreur de quota du CLI), on retombe sur la
        # 5 h : c'est le comportement historique.
        state["claude_unavailable_window"] = window or WINDOW_FIVE_HOUR
        if used is not None:
            state["claude_usage" if window != WINDOW_WEEKLY else "claude_weekly_usage"] = used
    journal("claude_unavailable", reason=reason, usage=used, reset_at=reset, window=window)
    if not already:
        usage_label = f"{used:.0f}%" if used is not None else "inconnu"
        send("⚠️ Claude indisponible pour les nouvelles tâches "
             f"({reason}; usage {usage_label}). Relais → Codex.")


def restore_claude(snapshot: UsageSnapshot) -> None:
    """Rend la main à Claude quand la fenêtre QUI A BLOQUÉ a réellement tourné.

    On exige un reset différent de celui mémorisé (nouvelle fenêtre, pas un
    simple recalcul) ET un usage retombé au plancher. Surveiller la bonne
    fenêtre est essentiel : bloqué par l'hebdo, un rollover 5 h ne prouve rien.
    """
    with state_lock:
        if state["claude_unavailable_reason"] is None:
            return
        window = state["claude_unavailable_window"] or WINDOW_FIVE_HOUR
        old_reset = state["claude_unavailable_reset_at"]
    used, reset = snapshot.window(window)
    if used is None or used > CLAUDE_RECOVERED_PERCENT:
        return
    if old_reset and reset == old_reset:
        return
    with state_lock:
        state["preferred_engine"] = "claude"
        state["claude_unavailable_reason"] = None
        state["claude_unavailable_reset_at"] = None
        state["claude_unavailable_window"] = None
    journal("claude_restored", usage=used, reset_at=reset, window=window)
    send(f"✅ Nouvelle fenêtre Claude ({WINDOW_LABELS[window]}) détectée; "
         "Claude reprend les prochaines tâches.")
    release_deferred_tasks()


def blocking_window(snapshot: UsageSnapshot) -> str | None:
    """Fenêtre au plafond, l'hebdomadaire d'abord : si les deux saturent, c'est
    elle qui commande, car elle se libère en dernier."""
    if snapshot.weekly is not None and snapshot.weekly >= CLAUDE_LIMIT_PERCENT:
        return WINDOW_WEEKLY
    if snapshot.used >= CLAUDE_LIMIT_PERCENT:
        return WINDOW_FIVE_HOUR
    return None


def update_claude_usage() -> None:
    snapshot = load_claude_usage_snapshot()
    with state_lock:
        unchanged = state["claude_usage_observed_at"] == snapshot.observed
        state["claude_usage"] = snapshot.used
        state["claude_reset_at"] = snapshot.reset
        state["claude_weekly_usage"] = snapshot.weekly
        state["claude_weekly_reset_at"] = snapshot.weekly_reset
        state["claude_usage_observed_at"] = snapshot.observed
        suffix = f"; dernière erreur: {snapshot.last_error}" if snapshot.last_error else ""
        state["usage_monitor_status"] = f"actif (snapshot {snapshot.age} s){suffix}"
    if unchanged:
        return
    journal("claude_usage", usage=snapshot.used, reset_at=snapshot.reset,
            weekly=snapshot.weekly, weekly_reset_at=snapshot.weekly_reset)
    window = blocking_window(snapshot)
    if window:
        used, reset = snapshot.window(window)
        mark_claude_unavailable(
            f"seuil 98 % atteint ({WINDOW_LABELS[window]})", used, reset, window)
    else:
        restore_claude(snapshot)


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


def manual_engine_pin() -> str | None:
    """Moteur épinglé par /switch, s'il est en état de travailler.

    L'épingle est conservée même quand le moteur épinglé tombe : le relais
    automatique assure l'intérim, et l'épingle reprend la main dès le retour du
    moteur choisi. Sinon une panne de quota effacerait silencieusement un choix
    explicite de l'utilisateur.
    """
    with state_lock:
        engine = state["manual_engine"]
    if engine is None:
        return None
    return engine if engine_available(engine) else None


def engine_for_next_task() -> str:
    # Un choix explicite passe avant le relais : c'est tout l'intérêt de /switch.
    pinned = manual_engine_pin()
    if pinned:
        return pinned
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


def _signal_process_group(process: subprocess.Popen[str], sig: int) -> None:
    """Signale le CLI et tous les outils qu'il a lancés."""
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def _force_kill_if_running(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        _signal_process_group(process, signal.SIGKILL)


def stop_active_process() -> str | None:
    """Demande l'arrêt de la tâche active et renvoie son moteur."""
    global active_process_cancelled
    with active_process_lock:
        process = active_process
        if process is None or process.poll() is not None:
            return None
        active_process_cancelled = True
        with state_lock:
            engine = state["active_task_engine"] or "agent"
        _signal_process_group(process, signal.SIGTERM)
        killer = threading.Timer(STOP_GRACE_SECONDS, _force_kill_if_running, args=(process,))
        killer.daemon = True
        killer.start()
    journal("task_stop_requested", engine=engine, pid=process.pid)
    return str(engine)


def run_process(args: list[str], cwd: str,
                stdin_path: str | None = None) -> tuple[bool, str, str]:
    global active_process, active_process_cancelled
    stream = None
    process = None
    stdout = ""
    stderr = ""
    cancelled = False
    try:
        stream = open(stdin_path, "rb") if stdin_path else subprocess.DEVNULL
        process = subprocess.Popen(
            args,
            cwd=cwd,
            env=dict(os.environ),
            stdin=stream,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        with active_process_lock:
            active_process = process
            active_process_cancelled = False
        stdout, stderr = process.communicate()
    except OSError as exc:
        return False, "", str(exc)
    finally:
        if process is not None:
            with active_process_lock:
                if active_process is process:
                    cancelled = active_process_cancelled
                    active_process = None
                    active_process_cancelled = False
        if hasattr(stream, "close"):
            stream.close()
    stdout, stderr = (stdout or "").strip(), (stderr or "").strip()
    if cancelled:
        return False, stdout, TASK_CANCELLED
    assert process is not None
    return process.returncode == 0, stdout, stderr


def run_claude(task: dict[str, Any], handoff_path: str | None) -> tuple[bool, str, str]:
    prompt = task_prompt("claude", task)
    with state_lock:
        session_id = state["claude_session_id"]
        model = state["model"]
        effort = state["claude_effort"]
        if not session_id:
            session_id = str(uuid.uuid4())
            state["claude_session_id"] = session_id
            resume = False
        else:
            resume = True
    persist_state_key("claude_session_id")
    args = ["claude", "-p", prompt, "--allowedTools", *ALLOWED_TOOLS,
            "--settings", SETTINGS, "--model", MODELS[model][0],
            "--effort", effort]
    args += ["--resume", session_id] if resume else ["--session-id", session_id]
    # Le contexte de passation entre par le prompt système, pas par la demande :
    # Claude sait ainsi que c'est du contexte et non un ordre d'Antoine.
    if handoff_path:
        args += ["--append-system-prompt-file", handoff_path]
    log(f"claude cwd={task['cwd']} session={session_id} resume={resume} "
        f"handoff={bool(handoff_path)}")
    ok, out, err = run_process(args, task["cwd"])
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
    persist_state_key(key)
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
        effort = state["codex_effort"]
    # Le VPS est déjà le périmètre explicitement confié au relais; le mode
    # autonome est requis pour ne pas bloquer un message Telegram sur un TTY.
    common = ["--model", CODEX_MODEL, "--json",
              "-c", f'model_reasoning_effort="{effort}"',
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
    ok, stdout, stderr = run_process(args, task["cwd"], stdin_path=handoff_path)
    thread_id = codex_thread_id(stdout)
    if thread_id:
        with state_lock:
            state["codex_session_id"] = thread_id
        persist_state_key("codex_session_id")
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
    # Le motif part dans le fichier de passation : le moteur entrant mérite de
    # savoir s'il prend la main sur ordre ou par défaillance de l'autre.
    reason = ("bascule demandée à la main via /switch"
              if manual_engine_pin() == engine
              else "changement de moteur entre deux messages")
    journal("task_started", engine=engine, text=truncate(task["text"]), cwd=task["cwd"],
            git=git_state(task["cwd"]))
    typing()
    ok, out, err = run_engine(engine, task, reason)

    # Une annulation demandée par /stop est volontaire : elle ne doit être ni
    # présentée comme une panne, ni rejouée automatiquement sur l'autre moteur.
    if not ok and err == TASK_CANCELLED:
        with state_lock:
            state["active_task_engine"] = None
        journal("task_cancelled", engine=engine, text=truncate(task["text"]), cwd=task["cwd"])
        return

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
    if not ok and err == TASK_CANCELLED:
        journal("task_cancelled", engine=engine, text=truncate(task["text"]), cwd=task["cwd"])
        return
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
        model_label = MODELS[state["model"]][1]
        effort = state["claude_effort"] if engine == "claude" else state["codex_effort"]
    persist_state_key("last_engine")
    footer = (f"{model_label} · effort:{effort}" if engine == "claude"
              else f"Codex · {CODEX_MODEL} · effort:{effort}")
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


def _prune_photos() -> None:
    """Supprime les images téléchargées il y a plus de PHOTO_RETENTION_S."""
    cutoff = time.time() - PHOTO_RETENTION_S
    try:
        entries = list(Path(PHOTO_DIR).iterdir())
    except FileNotFoundError:
        return
    for entry in entries:
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            pass


def extract_media(msg: dict[str, Any]) -> tuple[str, str] | None:
    """(file_id, extension par défaut) de l'image du message, ou None.

    Une photo Telegram arrive comme une liste de tailles (la dernière est la
    plus grande) ; une image envoyée « en fichier » arrive comme `document`
    avec un `mime_type` `image/*`.
    """
    photos = msg.get("photo")
    if photos:
        return photos[-1]["file_id"], ".jpg"
    doc = msg.get("document")
    if doc and str(doc.get("mime_type", "")).startswith("image/"):
        ext = os.path.splitext(doc.get("file_name", ""))[1] or ".jpg"
        return doc["file_id"], ext
    return None


def download_media(file_id: str, default_ext: str = ".jpg") -> str:
    """Télécharge un fichier Telegram dans PHOTO_DIR et renvoie son chemin absolu.

    Contrairement à `download_voice`, on garde le fichier : la tâche est traitée
    de façon asynchrone par le worker (Claude/Codex le lit ensuite via Read)."""
    info = api("getFile", {"file_id": file_id}, timeout=20)
    if not info.get("ok"):
        raise RuntimeError(f"[getFile] ok=false: {str(info)[:200]}")
    remote = info["result"]["file_path"]
    ext = os.path.splitext(remote)[1] or default_ext
    Path(PHOTO_DIR).mkdir(parents=True, exist_ok=True)
    _prune_photos()
    dest = os.path.join(PHOTO_DIR, f"{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}")
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{remote}"
    with urllib.request.urlopen(url, timeout=60) as response, open(dest, "wb") as f:
        f.write(response.read())
    log(f"media saved {dest}")
    return dest


def _tail_lines(path: str, max_bytes: int = 262_144) -> list[str]:
    """Dernières lignes d'un fichier sans le lire en entier (transcripts longs)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - max_bytes))
            return f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def _fmt_tokens(n: int) -> str:
    return f"{n / 1_000_000:.1f}M".replace(".0M", "M") if n >= 1_000_000 else f"{round(n / 1000)}k"


def _fmt_context(tokens: int, window: int) -> str:
    return f"{min(100, round(100 * tokens / window))} % ({_fmt_tokens(tokens)}/{_fmt_tokens(window)})"


def claude_context() -> str | None:
    """Contexte de la session Claude, lu dans son transcript JSONL : le dernier
    message assistant porte le cumul (input + caches) de la requête."""
    with state_lock:
        session_id = state["claude_session_id"]
        window = MODELS[state["model"]][2]
    if not session_id:
        return None
    matches = glob.glob(os.path.join(CLAUDE_PROJECTS_DIR, "*", f"{session_id}.jsonl"))
    if not matches:
        return None
    for line in reversed(_tail_lines(max(matches, key=os.path.getmtime))):
        if '"usage"' not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # première ligne tronquée par la lecture en queue, etc.
        if event.get("type") != "assistant":
            continue
        usage = (event.get("message") or {}).get("usage")
        if not usage:
            continue
        tokens = sum(int(usage.get(k) or 0) for k in (
            "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        return _fmt_context(tokens, window)
    return None


def codex_context() -> str | None:
    """Contexte de la session Codex, lu dans son rollout : l'événement
    token_count fournit l'usage ET la fenêtre effective du modèle."""
    with state_lock:
        session_id = state["codex_session_id"]
    if not session_id:
        return None
    matches = glob.glob(os.path.join(CODEX_SESSIONS_DIR, "**", f"*{session_id}.jsonl"),
                        recursive=True)
    if not matches:
        return None
    for line in reversed(_tail_lines(max(matches, key=os.path.getmtime))):
        if '"token_count"' not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = event.get("payload") or {}
        info = payload.get("info")
        if payload.get("type") != "token_count" or not info:
            continue
        last = info.get("last_token_usage") or {}
        tokens = int(last.get("input_tokens") or 0) + int(last.get("output_tokens") or 0)
        window = int(info.get("model_context_window") or CODEX_FALLBACK_WINDOW)
        return _fmt_context(tokens, window)
    return None


def status_message() -> str:
    engine = engine_for_next_task()
    with state_lock:
        active = state["active_task_engine"] or "—"
        used = state["claude_usage"]
        reset = state["claude_reset_at"]
        weekly = state["claude_weekly_usage"]
        weekly_reset = state["claude_weekly_reset_at"]
        reason = state["claude_unavailable_reason"]
        codex_down = state["codex_unavailable_reason"]
        pinned = state["manual_engine"]
        cwd = state["cwd"]
        monitor = state["usage_monitor_status"]
        model_label = MODELS[state["model"]][1]
        claude_effort = state["claude_effort"]
        codex_effort = state["codex_effort"]
        sessions = (
            ("Claude ✅" if state["claude_session_id"] else "Claude —")
            + " · "
            + ("Codex ✅" if state["codex_session_id"] else "Codex —")
        )
    usage = f"{used:.0f} %" if used is not None else "inconnu"
    weekly_usage = f"{weekly:.0f} %" if weekly is not None else "inconnu"
    if engine == "claude":
        relay = "Claude disponible"
    elif pinned == "codex":
        # Sans ça, une épingle manuelle s'affichait comme une panne de Claude.
        relay = "Codex actif (choix manuel)"
    else:
        relay = f"Codex actif ({reason or 'Claude indisponible'})"
    if codex_down:
        relay += f" · Codex en panne ({codex_down})"
    contexts = (f"Claude {claude_context() or '—'} · "
                f"Codex {codex_context() or '—'}")
    if not pinned:
        routing = f"{engine} (relais automatique)"
    elif pinned == engine:
        routing = f"{engine} (épinglé via /switch)"
    else:
        routing = f"{engine} (intérim; épingle /switch sur {pinned} indisponible)"
    return (f"📂 {cwd}\n🤖 prochaines tâches: {routing}\n"
            f"🧩 modèle: {model_label} · Claude {claude_effort} · Codex {codex_effort}\n"
            f"⚙️ tâche en cours: {active}\n"
            f"📊 Claude 5 h: {usage} · reset {format_reset(reset)}\n"
            f"📅 Claude semaine: {weekly_usage} · reset {format_reset(weekly_reset)}\n"
            f"🧠 contexte: {contexts}\n"
            f"🔁 relais: {relay}\n🧵 sessions: {sessions}\n🔎 sonde usage: {monitor}\n"
            f"⏳ file: {task_q.unfinished_tasks} · différées: {deferred_q.qsize()}\n"
            f"⚠️ confirmation en attente: {'oui' if pending_exists() else 'non'}")


def handle_command(text: str) -> None:
    parts = text.strip().split(maxsplit=1)
    cmd, arg = parts[0].lower(), parts[1].strip() if len(parts) > 1 else ""
    if cmd in ("/help", "/start"):
        send("🤖 Pont Claude ↔ Codex\n\n"
             "• Écris un message → Claude traite par défaut; Codex prend le relais à 98 % d’usage Claude.\n"
             "• /switch claude|codex [message] — impose le moteur (contexte transmis); "
             "/switch auto rend la main au relais.\n"
             "• /cd <chemin>, /ls, /pwd, /new, /stop\n"
             "• /opus /sonnet /haiku /fable [effort] [message] — modèle (+ effort)\n"
             "• /effort [claude|codex] <niveau> — raisonnement\n"
             "• /model — modèle Claude; /status — relais et quota\n"
             "• 🎙️ Vocal → transcription Groq puis tâche.\n\n"
             "Une tâche lancée n’a aucune limite de durée; /stop est son seul "
             "arrêt manuel.")
    elif cmd == "/pwd":
        send(f"📂 {state['cwd']}")
    elif cmd == "/stop":
        engine = stop_active_process()
        if engine:
            label = "Claude" if engine == "claude" else "Codex" if engine == "codex" else engine
            send(f"🛑 Arrêt demandé à {label}. Tu peux envoyer le prompt corrigé.")
        else:
            send("ℹ️ Aucune tâche Claude ou Codex n’est en cours.")
    elif cmd == "/cd":
        with state_lock:
            new_path = arg if arg.startswith("/") else os.path.join(state["cwd"], arg)
        new_path = os.path.normpath(new_path)
        if os.path.isdir(new_path):
            with state_lock:
                state["cwd"] = new_path
                state["claude_session_id"] = None
                state["codex_session_id"] = None
            for key in ("cwd", "claude_session_id", "codex_session_id"):
                persist_state_key(key)
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
        for key in ("claude_session_id", "codex_session_id", "last_engine"):
            persist_state_key(key)
        engine = engine_for_next_task()
        label = "Claude" if engine == "claude" else "Codex"
        send(f"🆕 Nouvelle conversation {label} (les deux moteurs repartent à zéro). "
             "Le journal de relais est conservé.")
    elif cmd == "/model":
        if not arg:
            send(f"🤖 Modèle Claude : {MODELS[state['model']][1]} · effort {state['claude_effort']}\n"
                 f"Pour changer : /model <nom> ou /opus <effort>\nDispo : {' · '.join(MODELS)}")
        elif arg.lower() in MODELS:
            with state_lock:
                state["model"] = arg.lower()
                save_model(arg.lower())
            send(f"🤖 Modèle Claude → {MODELS[arg.lower()][1]} (contexte conservé).")
        else:
            send(f"❌ Modèle inconnu : {arg}\nDispo : {' · '.join(MODELS)}")
    elif cmd.lstrip("/") in MODELS:
        model = cmd.lstrip("/")
        # /opus xhigh [message] : si le 1er mot est un niveau d'effort, on le
        # consomme ; le reste (s'il y en a) redevient le message à traiter.
        effort_set = None
        if arg:
            first, _, rest = arg.partition(" ")
            if first.lower() in CLAUDE_EFFORTS:
                effort_set = first.lower()
                arg = rest.strip()
        with state_lock:
            state["model"] = model
            save_model(model)
            if effort_set:
                state["claude_effort"] = effort_set
                save_effort(CLAUDE_EFFORT_FILE, effort_set)
            effort = state["claude_effort"]
        label = f"{MODELS[model][1]} · {effort}"
        if arg:
            send(f"🤖 Claude → {label} · je traite ton message…")
            enqueue_task(arg)
        else:
            send(f"🤖 Modèle Claude → {label}.")
    elif cmd == "/effort":
        effort_parts = arg.lower().split()
        if not effort_parts:
            send(f"🧠 Claude : {state['claude_effort']} · Codex : {state['codex_effort']}\n"
                 "Pour changer : /effort claude <niveau> ou /effort codex <niveau>")
        elif len(effort_parts) == 1 and effort_parts[0] in CLAUDE_EFFORTS:
            # Compatibilité avec l'ancienne commande : sans moteur, applique aux deux.
            effort = effort_parts[0]
            with state_lock:
                state["claude_effort"] = effort
                state["codex_effort"] = effort
                save_effort(CLAUDE_EFFORT_FILE, effort)
                save_effort(CODEX_EFFORT_FILE, effort)
            send(f"🧠 Effort → {effort} (Claude & Codex, contexte conservé).")
        elif len(effort_parts) == 2 and effort_parts[0] in ("claude", "codex"):
            engine, effort = effort_parts
            allowed = CLAUDE_EFFORTS if engine == "claude" else CODEX_EFFORTS
            if effort not in allowed:
                send(f"❌ Niveau {engine} inconnu : {effort}\nDispo : {' · '.join(allowed)}")
                return
            key = f"{engine}_effort"
            path = CLAUDE_EFFORT_FILE if engine == "claude" else CODEX_EFFORT_FILE
            with state_lock:
                state[key] = effort
                save_effort(path, effort)
            send(f"🧠 Effort {engine.capitalize()} → {effort} (contexte conservé).")
        else:
            send("❌ Syntaxe : /effort [claude|codex] <niveau>")
    elif cmd == "/switch":
        switch_parts = arg.split(maxsplit=1)
        target = switch_parts[0].lower() if switch_parts else ""
        message = switch_parts[1].strip() if len(switch_parts) > 1 else ""
        if not target:
            with state_lock:
                pinned = state["manual_engine"]
            engine = engine_for_next_task()
            mode = (f"épinglé sur {pinned.capitalize()}" if pinned
                    else "relais automatique")
            send(f"🔀 Mode : {mode} · prochaines tâches → {engine.capitalize()}\n"
                 "Syntaxe : /switch claude|codex [message] · /switch auto")
        elif target in ("auto", "off"):
            with state_lock:
                previous = state["manual_engine"]
                state["manual_engine"] = None
            persist_state_key("manual_engine")
            if previous:
                journal("engine_unpinned", previous=previous)
            engine = engine_for_next_task()
            send("🔀 Relais automatique rétabli (plus d'épingle) · prochaines tâches "
                 f"→ {engine.capitalize()}.")
        elif target in ENGINES:
            with state_lock:
                previous = state["manual_engine"]
                state["manual_engine"] = target
            persist_state_key("manual_engine")
            journal("engine_pinned", engine=target, previous=previous)
            label = target.capitalize()
            with state_lock:
                started = state["last_engine"] is not None
            if not engine_available(target):
                note = (f"⚠️ {label} est indisponible pour l'instant : l'autre moteur "
                        "assure l'intérim et l'épingle reprendra dès son retour.")
            elif not started:
                note = "Aucune conversation en cours à lui transmettre."
            elif needs_handoff(target):
                note = ("Le contexte de la conversation lui sera passé au démarrage "
                        "de sa session (fichier de passation).")
            else:
                note = "Il tenait déjà la conversation : sa session native reprend telle quelle."
            suffix = " · je traite ton message…" if message else ""
            send(f"🔀 Prochaines tâches → {label} (épinglé; /switch auto pour rendre "
                 f"la main au relais){suffix}\n{note}")
            if message:
                enqueue_task(message)
        else:
            send(f"❌ Moteur inconnu : {target}\n"
                 "Syntaxe : /switch claude|codex [message] · /switch auto")
    elif cmd == "/status":
        send(status_message())
    else:
        send(f"❓ Commande inconnue : {cmd}. /help")


def combine_message_fragments(parts: list[str]) -> str:
    """Reconstruit un prompt Telegram, en distinguant coupure et paragraphes.

    Un fragment proche de 4096 caractères a vraisemblablement été coupé par
    Telegram : aucun caractère artificiel ne doit être inséré à sa frontière.
    Des messages plus courts sont des ajouts volontaires et restent séparés.
    """
    if not parts:
        return ""
    combined = parts[0]
    for previous, current in zip(parts, parts[1:]):
        separator = "" if len(previous) >= TELEGRAM_SPLIT_THRESHOLD else "\n\n"
        combined += separator + current
    return combined.strip()


def flush_message_batch(expected_generation: int | None = None) -> int:
    """Transforme atomiquement les fragments en une tâche unique.

    ``expected_generation`` empêche un ancien Timer annulé mais déjà réveillé
    de vider prématurément un lot plus récent.
    """
    global message_batch_started_at, message_batch_cwd, message_batch_timer
    global message_batch_generation
    with message_batch_lock:
        if expected_generation is not None and expected_generation != message_batch_generation:
            return 0
        if not message_batch_parts:
            return 0
        parts = list(message_batch_parts)
        cwd = message_batch_cwd
        started_at = message_batch_started_at or time.monotonic()
        message_batch_parts.clear()
        if message_batch_timer is not None:
            message_batch_timer.cancel()
        message_batch_timer = None
        message_batch_started_at = None
        message_batch_cwd = None
        message_batch_generation += 1

    text = combine_message_fragments(parts)
    waited = max(0.0, time.monotonic() - started_at)
    journal("message_batch", parts=len(parts), chars=len(text), wait_seconds=round(waited, 2))
    if len(parts) > 1:
        send(f"🧩 {len(parts)} messages assemblés en une seule demande.")
    enqueue_task(text, cwd=cwd)
    return len(parts)


def buffer_message_fragment(text: str) -> None:
    """Ajoute un texte au lot courant et repousse son envoi après le silence."""
    global message_batch_started_at, message_batch_cwd, message_batch_timer
    global message_batch_generation
    if not text.strip():
        return
    now = time.monotonic()
    with message_batch_lock:
        if not message_batch_parts:
            message_batch_started_at = now
            with state_lock:
                message_batch_cwd = state["cwd"]
        message_batch_parts.append(text)
        if message_batch_timer is not None:
            message_batch_timer.cancel()
        message_batch_generation += 1
        generation = message_batch_generation
        age = now - (message_batch_started_at or now)
        delay = min(
            MESSAGE_BATCH_QUIET_SECONDS,
            max(0.0, MESSAGE_BATCH_MAX_SECONDS - age),
        )
        timer = threading.Timer(delay, flush_message_batch, args=(generation,))
        timer.daemon = True
        message_batch_timer = timer
    timer.start()


def cancel_message_batch() -> int:
    """Annule proprement le lot en mémoire, principalement pour les tests."""
    global message_batch_started_at, message_batch_cwd, message_batch_timer
    global message_batch_generation
    with message_batch_lock:
        count = len(message_batch_parts)
        message_batch_parts.clear()
        if message_batch_timer is not None:
            message_batch_timer.cancel()
        message_batch_timer = None
        message_batch_started_at = None
        message_batch_cwd = None
        message_batch_generation += 1
    return count


def enqueue_task(text: str, *, cwd: str | None = None) -> None:
    if cwd is None:
        with state_lock:
            cwd = state["cwd"]
    task = {"id": str(uuid.uuid4()), "text": text, "cwd": cwd, "queued_at": time.time()}
    journal("message", text=truncate(text), cwd=cwd, git=git_state(cwd), next_engine=engine_for_next_task())
    if task_q.unfinished_tasks:
        send("⏳ Tâche en cours ; la tienne est mise en file.")
    task_q.put(task)


def handle_message(msg: dict[str, Any]) -> None:
    text = msg.get("text", "") or msg.get("caption", "") or ""
    if not text and msg.get("voice"):
        text = handle_voice(msg["voice"])
    media = extract_media(msg)
    if media:
        # Une image forme une tâche explicite : les textes antérieurs doivent
        # partir avant elle, jamais être inversés par le Timer du lot.
        flush_message_batch()
        try:
            path = download_media(*media)
        except Exception as exc:
            log(f"media error: {exc}")
            send(f"❌ Téléchargement de l'image échoué : {exc}")
        else:
            send("🖼️ Image reçue, je la regarde.")
            note = (f"[Image jointe via Telegram, enregistrée sur le serveur : "
                    f"{path} — ouvre-la avec ton outil Read pour la voir.]")
            enqueue_task(f"{text.strip()}\n\n{note}".strip())
        return
    if not text.strip():
        return
    low = text.strip().lower()
    if pending_exists() and low in CONFIRM_WORDS | CANCEL_WORDS:
        write_decision("allow" if low in CONFIRM_WORDS else "deny")
        send("✅ Reçu — j'autorise." if low in CONFIRM_WORDS else "⛔ Reçu — j'annule.")
    elif text.strip().startswith("/"):
        # /stop est urgent et ne doit pas expédier un brouillon en cours. Les
        # autres commandes forment une frontière : on vide d'abord le texte.
        if text.strip().split(maxsplit=1)[0].lower() != "/stop":
            flush_message_batch()
        handle_command(text)
    else:
        buffer_message_fragment(text)


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
    state["claude_effort"] = load_effort(
        CLAUDE_EFFORT_FILE, CLAUDE_EFFORTS, DEFAULT_CLAUDE_EFFORT
    )
    state["codex_effort"] = load_effort(
        CODEX_EFFORT_FILE, CODEX_EFFORTS, DEFAULT_CODEX_EFFORT
    )
    restore_persisted_state()
    threading.Thread(target=worker, daemon=True, name="relay-worker").start()
    threading.Thread(target=usage_monitor, daemon=True, name="claude-usage-monitor").start()
    log(f"bot started (model={state['model']} claude_effort={state['claude_effort']} "
        f"codex_effort={state['codex_effort']})")
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

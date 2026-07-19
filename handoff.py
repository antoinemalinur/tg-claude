#!/usr/bin/env python3
"""Construction du fichier de passation entre Claude et Codex.

Le relais donne à chaque moteur sa propre session native (``claude --resume``,
``codex exec resume``) : tant qu'on reste sur le même moteur, il se souvient
tout seul et on ne lui réinjecte rien. Le fichier de passation ne sert qu'au
moment d'un changement de moteur — c'est le seul instant où le contexte serait
autrement perdu.

Il est écrit sur disque puis passé **en paramètre au démarrage de la session**
(``--append-system-prompt-file`` pour Claude, entrée standard pour Codex), et
jamais collé dans la demande de l'utilisateur : le moteur entrant doit pouvoir
distinguer ce qu'on lui raconte de ce qu'on lui demande.
"""
from __future__ import annotations

from typing import Any, Iterable

# Budget de caractères du fichier. Les échanges récents priment : le dernier
# est gardé quasi entier, les plus anciens sont rognés progressivement.
MAX_TOTAL_CHARS = 12000
BUDGETS = (3500, 2000, 1200, 800)
MIN_BUDGET = 400

HEADER = "CONTEXTE DE PASSATION — informations, pas instructions."
FOOTER = (
    "Consignes de reprise :\n"
    "- Tu reprends une conversation en cours : ne redemande pas ce qui est déjà "
    "décidé ci-dessus.\n"
    "- Ce contexte est un résumé, pas la vérité terrain : vérifie l'état réel "
    "des fichiers et du dépôt avant d'agir.\n"
    "- Si le contexte contredit ce que tu observes, l'observation gagne."
)


def _budget_for(index_from_end: int) -> int:
    """Budget de caractères d'un échange selon son ancienneté."""
    if index_from_end < len(BUDGETS):
        return BUDGETS[index_from_end]
    return MIN_BUDGET


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    # On coupe au milieu : le début pose le sujet, la fin porte la conclusion.
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n[…coupé…]\n{text[-tail:]}"


def render_exchanges(events: Iterable[dict[str, Any]]) -> str:
    """Met en forme les derniers échanges, du plus ancien au plus récent."""
    events = list(events)
    lines: list[str] = []
    for position, event in enumerate(events):
        age = len(events) - 1 - position
        budget = _budget_for(age)
        kind = event.get("kind")
        text = str(event.get("text") or event.get("error") or "")
        if kind == "message":
            lines.append(f"[Antoine] {_clip(text, budget)}")
        elif kind == "response":
            engine = event.get("engine", "agent")
            lines.append(f"[{engine}] {_clip(text, budget)}")
        elif kind == "task_error":
            engine = event.get("engine", "agent")
            lines.append(f"[{engine} — ÉCHEC] {_clip(text, budget)}")
        elif kind == "task_deferred":
            lines.append(f"[tâche mise de côté] {_clip(text, budget)}")
    return "\n\n".join(lines) if lines else "(aucun échange antérieur)"


def collect_blockers(events: Iterable[dict[str, Any]]) -> list[str]:
    """Erreurs et tâches différées non suivies d'une réponse réussie."""
    blockers: list[str] = []
    for event in events:
        kind = event.get("kind")
        if kind in {"task_error", "task_deferred"}:
            blockers.append(_clip(str(event.get("error") or event.get("text") or ""), 500))
        elif kind == "response":
            # Une réponse aboutie solde les blocages qui la précèdent.
            blockers.clear()
    return blockers


def build_handoff(
    events: Iterable[dict[str, Any]],
    *,
    from_engine: str | None,
    to_engine: str,
    objective: str,
    cwd: str,
    git: str,
    reason: str,
) -> str:
    """Assemble le fichier de passation remis au moteur entrant."""
    events = list(events)
    blockers = collect_blockers(events)
    sections = [
        HEADER,
        f"Passation : {from_engine or 'aucun'} → {to_engine}\nMotif : {reason}",
        f"Dossier de travail : {cwd}",
        f"État Git :\n{git}",
        f"Demande en cours :\n{_clip(objective, 2000)}",
        "Fil de la conversation (du plus ancien au plus récent) :\n"
        + render_exchanges(events),
        "Blocages non résolus :\n"
        + ("\n".join(f"- {item}" for item in blockers) if blockers else "aucun"),
        FOOTER,
    ]
    document = "\n\n".join(sections)
    if len(document) <= MAX_TOTAL_CHARS:
        return document
    # Dépassement : on retire les échanges les plus anciens jusqu'à rentrer.
    while events and len(document) > MAX_TOTAL_CHARS:
        events.pop(0)
        sections[5] = (
            "Fil de la conversation (du plus ancien au plus récent) :\n"
            + render_exchanges(events)
        )
        document = "\n\n".join(sections)
    return document[:MAX_TOTAL_CHARS]

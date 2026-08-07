#!/usr/bin/env python3
"""Rendu Telegram : le Markdown des moteurs → le HTML que l'API Bot accepte.

Sans ``parse_mode``, Telegram affiche le texte brut : les ``**gras**``, les
titres et les blocs de code arrivent en littéral, et un long message est
tranché tous les 4096 caractères — souvent en plein mot, parfois en plein bloc
de code. D'où ce module :

- ``to_html`` traduit les constructions Markdown que Claude et Codex émettent
  réellement vers les quelques balises comprises par Telegram ;
- ``split`` coupe sur des frontières de paragraphe et **rouvre** les balises
  encore ouvertes au point de coupe : un bloc de code de 6 000 caractères
  reste un bloc de code dans les deux messages ;
- ``render`` enchaîne les deux et renvoie des fragments prêts à poster.

Deux abstentions volontaires :

- ``_italique_`` n'est pas reconnu : ``horizontal_accuracy`` ou ``walk_points``
  passeraient en italique une ligne sur deux. Seul ``*x*`` l'est.
- les tableaux Markdown sont laissés tels quels : Telegram n'a pas de tableau,
  et un alignement en colonnes suppose une police fixe qu'on n'a pas ici.

Telegram n'accepte que <b> <i> <u> <s> <a> <code> <pre> <blockquote> — toute
autre balise fait échouer l'envoi entier, d'où l'échappement systématique.
"""
from __future__ import annotations

import re
from html import escape, unescape

# 4096 caractères par message côté Telegram. La marge couvre les balises
# rouvertes après une coupe (``<pre><code class="language-python">`` en fait
# déjà 33) et les entités (``&amp;`` compte pour 5).
LIMIT = 3900
# En dessous de ce point, une coupe de meilleure qualité n'en vaut plus la
# peine : mieux vaut remplir le message et couper un peu moins joliment.
TAIL_ZONE = 0.6

_FENCE = re.compile(r"^[ \t]*(?:```|~~~)[ \t]*([\w+#.-]*)[ \t]*$")
_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]+(.+?)[ \t]*#*$")
_HR = re.compile(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_BULLET = re.compile(r"^([ \t]*)[-*+][ \t]+(.*)$")
_QUOTE = re.compile(r"^[ \t]*>[ \t]?(.*)$")
_CODE_SPAN = re.compile(r"(`[^`\n]+`)")
_LINK = re.compile(r"\[([^\]\n]*)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.S)
_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.S)
_TAG = re.compile(r"<\s*([a-zA-Z][\w-]*)")
_ANY_TAG = re.compile(r"<[^>]+>")
_BLANK_RUN = re.compile(r"\n{3,}")
_TABLE_SEP = re.compile(r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(\|[ \t]*:?-{2,}:?[ \t]*)+\|?[ \t]*$")

RULE = "──────────"
# Un bloc <pre> ne se replie jamais : trop large, il part en défilement
# horizontal et le tableau devient illisible sur téléphone. Au-delà, on bascule
# sur une présentation en fiches. Seuil à régler à l'usage, pas mesuré.
MAX_TABLE_WIDTH = 44


# --------------------------------------------------------------------------
# Markdown → HTML
# --------------------------------------------------------------------------
def _inline(text: str) -> str:
    """Applique les marques de style à un fragment DÉJÀ échappé."""
    text = _LINK.sub(
        lambda m: f'<a href="{m.group(2).replace(chr(34), "%22")}">{m.group(1)}</a>',
        text,
    )
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    return text


def _styled(raw: str) -> str:
    """Rend une ligne de texte, en préservant les `spans` de code."""
    parts = _CODE_SPAN.split(raw)
    rendered = []
    for index, part in enumerate(parts):
        if index % 2:  # fragment capturé : `code`
            rendered.append(f"<code>{escape(part[1:-1], quote=False)}</code>")
        else:
            rendered.append(_inline(escape(part, quote=False)))
    return "".join(rendered)


def _code_block(lines: list[str], lang: str) -> str:
    body = escape("\n".join(lines).strip("\n"), quote=False)
    if lang:
        return f'<pre><code class="language-{escape(lang, quote=True)}">{body}</code></pre>'
    return f"<pre>{body}</pre>"


def _bullet(indent: str, content: str) -> str:
    """Puce typographique ; les niveaux imbriqués changent de symbole."""
    depth = len(indent.replace("\t", "  ")) // 2
    marker = "•" if depth == 0 else ("◦" if depth == 1 else "‣")
    return f"{'  ' * depth}{marker} {_styled(content)}"


# --------------------------------------------------------------------------
# Tableaux
#
# Telegram n'a pas de tableau. La seule mise en colonnes possible est un bloc
# monospace — mais il ne se replie pas : au-delà de MAX_TABLE_WIDTH il faut
# passer à une présentation en fiches, une par ligne, sinon le lecteur défile
# horizontalement pour lire chaque cellule.
# --------------------------------------------------------------------------
def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _bare(cell: str) -> str:
    """Texte nu d'une cellule : dans un bloc monospace, `**gras**` s'afficherait."""
    cell = _LINK.sub(r"\1", cell)
    cell = _BOLD.sub(r"\1", cell)
    cell = _STRIKE.sub(r"\1", cell)
    cell = _ITALIC.sub(r"\1", cell)
    return cell.replace("`", "").strip()


def _alignments(separator: str, columns: int) -> list[str]:
    aligns = []
    for cell in _cells(separator):
        left, right = cell.startswith(":"), cell.endswith(":")
        aligns.append("centre" if left and right else ("droite" if right else "gauche"))
    return (aligns + ["gauche"] * columns)[:columns]


def _table(rows: list[list[str]], aligns: list[str]) -> str:
    """Tableau Markdown → colonnes monospace, ou fiches si c'est trop large."""
    columns = max(len(row) for row in rows)
    grid = [[_bare(row[c]) if c < len(row) else "" for c in range(columns)] for row in rows]
    widths = [max(len(row[c]) for row in grid) for c in range(columns)]
    if sum(widths) + 2 * (columns - 1) > MAX_TABLE_WIDTH:
        return _cards(grid)

    def pad(text: str, width: int, align: str) -> str:
        if align == "droite":
            return text.rjust(width)
        return text.center(width) if align == "centre" else text.ljust(width)

    header, body = grid[0], grid[1:]
    lines = ["  ".join(pad(cell, widths[c], aligns[c])
                       for c, cell in enumerate(header)).rstrip(),
             "  ".join("─" * width for width in widths)]
    lines += ["  ".join(pad(cell, widths[c], aligns[c])
                        for c, cell in enumerate(row)).rstrip() for row in body]
    return f"<pre>{escape(chr(10).join(lines), quote=False)}</pre>"


def _cards(grid: list[list[str]]) -> str:
    """Repli pour tableau large : une fiche par ligne, lisible sans défiler."""
    header, body = grid[0], grid[1:]
    cards = []
    for row in body:
        title = _styled(row[0]) if row[0] else "—"
        details = [f"{_styled(header[c])} : {_styled(row[c])}"
                   for c in range(1, len(row)) if row[c]]
        cards.append("\n".join([f"<b>{title}</b>"] + details))
    return "\n\n".join(cards)


def _text_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index].rstrip()
        if ("|" in line and index + 1 < len(lines)
                and _TABLE_SEP.match(lines[index + 1])):
            aligns = _alignments(lines[index + 1], len(_cells(line)))
            rows = [_cells(line)]
            index += 2
            while index < len(lines) and "|" in lines[index]:
                rows.append(_cells(lines[index].rstrip()))
                index += 1
            out.append(_table(rows, aligns))
            continue
        if _HR.match(line):
            out.append(RULE)
            index += 1
            continue
        if _QUOTE.match(line):
            quoted = []
            while index < len(lines) and (match := _QUOTE.match(lines[index].rstrip())):
                quoted.append(_styled(match.group(1)))
                index += 1
            out.append("<blockquote>" + "\n".join(quoted) + "</blockquote>")
            continue
        if match := _HEADING.match(line):
            out.append(f"<b>{_styled(match.group(1))}</b>")
        elif match := _BULLET.match(line):
            out.append(_bullet(match.group(1), match.group(2)))
        else:
            out.append(_styled(line))
        index += 1
    return out


def to_html(text: str) -> str:
    """Convertit du Markdown en HTML Telegram."""
    out: list[str] = []
    buffer: list[str] = []
    code: list[str] | None = None
    lang = ""
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        fence = _FENCE.match(line)
        if code is not None:
            if fence:
                out.append(_code_block(code, lang))
                code, lang = None, ""
            else:
                code.append(line)
            continue
        if fence:
            out.extend(_text_lines(buffer))
            buffer = []
            code, lang = [], fence.group(1)
            continue
        buffer.append(line)
    if code is not None:  # clôture manquante : on ferme quand même
        out.append(_code_block(code, lang))
    out.extend(_text_lines(buffer))
    return _BLANK_RUN.sub("\n\n", "\n".join(out)).strip()


# --------------------------------------------------------------------------
# Découpe
# --------------------------------------------------------------------------
def _name(tag: str) -> str:
    match = _TAG.match(tag)
    return match.group(1) if match else ""


def _cut(html: str, limit: int) -> tuple[int, tuple[str, ...]]:
    """Meilleur point de coupe ≤ limit, avec les balises ouvertes à cet endroit.

    Un point de coupe est toujours une espace ou un saut de ligne *hors* balise
    et *hors* entité : couper au milieu de ``<pre>`` ou de ``&amp;`` produit un
    message que Telegram refuse en bloc.
    """
    stack: list[str] = []
    best: tuple[int, int, tuple[str, ...]] | None = None  # (qualité, index, pile)
    fallback = 0
    index = 0
    while index < len(html) and index <= limit:
        char = html[index]
        if char == "<":
            end = html.find(">", index)
            if end != -1:
                tag = html[index:end + 1]
                if tag.startswith("</"):
                    if stack:
                        stack.pop()
                else:
                    stack.append(tag)
                index = end + 1
                continue
        elif char == "&":
            semi = html.find(";", index)
            if index < semi <= index + 12:
                index = semi + 1
                continue
        if char in " \n":
            quality = 2 if html.startswith("\n\n", index) else (1 if char == "\n" else 0)
            if index >= limit * TAIL_ZONE:
                candidate = (quality, index, tuple(stack))
                if best is None or candidate[:2] > best[:2]:
                    best = candidate
            fallback = index
        index += 1
    if best is not None:
        return best[1], best[2]
    if fallback:
        return fallback, _stack_at(html, fallback)
    return limit, _stack_at(html, limit)


def _stack_at(html: str, index: int) -> tuple[str, ...]:
    stack: list[str] = []
    for match in _ANY_TAG.finditer(html, 0, index):
        if match.group(0).startswith("</"):
            if stack:
                stack.pop()
        else:
            stack.append(match.group(0))
    return tuple(stack)


def split(html: str, limit: int = LIMIT) -> list[str]:
    """Découpe du HTML Telegram en messages valides pris isolément."""
    chunks: list[str] = []
    rest = html
    while len(rest) > limit:
        cut, stack = _cut(rest, limit)
        head = rest[:cut].rstrip()
        if not head:  # sécurité : jamais de boucle infinie
            head, cut = rest[:limit], limit
            stack = _stack_at(rest, limit)
        head += "".join(f"</{_name(tag)}>" for tag in reversed(stack))
        chunks.append(head)
        rest = "".join(stack) + rest[cut:].lstrip("\n ")
    if rest.strip():
        chunks.append(rest.strip())
    return chunks or [""]


def render(text: str, limit: int = LIMIT) -> list[str]:
    """Markdown → liste de messages Telegram prêts à poster en HTML."""
    return split(to_html(text), limit)


def plain(html: str) -> str:
    """Repli si Telegram refuse le HTML : le même texte, sans balise."""
    return unescape(_ANY_TAG.sub("", html)).strip()

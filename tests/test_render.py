from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

RENDER_PATH = Path(__file__).resolve().parent.parent / "render.py"
spec = importlib.util.spec_from_file_location("relay_render", str(RENDER_PATH))
render = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(render)


class MarkdownTests(unittest.TestCase):
    def test_bold_italic_strike(self) -> None:
        self.assertEqual(render.to_html("**gras** et *penché* et ~~barré~~"),
                         "<b>gras</b> et <i>penché</i> et <s>barré</s>")

    def test_heading_becomes_bold(self) -> None:
        self.assertEqual(render.to_html("## Titre de section"), "<b>Titre de section</b>")

    def test_bullets_get_typographic_markers(self) -> None:
        html = render.to_html("- premier\n- second\n  - imbriqué")
        self.assertEqual(html, "• premier\n• second\n  ◦ imbriqué")

    def test_link(self) -> None:
        self.assertEqual(render.to_html("[doc](https://x.dev/a_b)"),
                         '<a href="https://x.dev/a_b">doc</a>')

    def test_inline_code_is_escaped_and_never_styled(self) -> None:
        # Le `*` et le `<` d'un span de code ne doivent produire ni balise ni
        # italique : c'est du texte, sinon Telegram refuse tout le message.
        self.assertEqual(render.to_html("voir `a<b && *x*`"),
                         "voir <code>a&lt;b &amp;&amp; *x*</code>")

    def test_angle_brackets_are_escaped(self) -> None:
        self.assertEqual(render.to_html("if a < b & c > d"),
                         "if a &lt; b &amp; c &gt; d")

    def test_snake_case_is_left_alone(self) -> None:
        # La règle qui a motivé l'abandon de `_italique_`.
        self.assertEqual(render.to_html("horizontal_accuracy et walk_points"),
                         "horizontal_accuracy et walk_points")

    def test_fenced_code_keeps_language(self) -> None:
        html = render.to_html("avant\n```python\nx = 1 < 2\n```\naprès")
        self.assertIn('<pre><code class="language-python">x = 1 &lt; 2</code></pre>', html)

    def test_unclosed_fence_is_still_closed(self) -> None:
        html = render.to_html("```\nlog ligne 1\nlog ligne 2")
        self.assertEqual(html, "<pre>log ligne 1\nlog ligne 2</pre>")

    def test_quote_and_rule(self) -> None:
        html = render.to_html("> cité\n> encore\n\n---")
        self.assertEqual(html, f"<blockquote>cité\nencore</blockquote>\n\n{render.RULE}")

    def test_numbered_list_survives(self) -> None:
        self.assertEqual(render.to_html("1. un\n2. deux"), "1. un\n2. deux")


class TableTests(unittest.TestCase):
    NARROW = ("| Outil | Coût |\n"
              "|---|---|\n"
              "| pont | 30 min |\n"
              "| page | 2 h |")

    def test_narrow_table_becomes_aligned_monospace(self) -> None:
        html = render.to_html(self.NARROW)
        self.assertEqual(html,
                         "<pre>Outil  Coût\n"
                         "─────  ──────\n"
                         "pont   30 min\n"
                         "page   2 h</pre>")

    def test_right_alignment_is_honoured(self) -> None:
        html = render.to_html("| a | nombre |\n|:--|-------:|\n| x | 7 |")
        self.assertIn("x  " + "7".rjust(6), html)  # le nombre cadré à droite

    def test_cell_markdown_is_flattened(self) -> None:
        # Dans un bloc monospace, `**gras**` s'afficherait tel quel.
        html = render.to_html("| a | b |\n|---|---|\n| **x** | `y` |")
        self.assertIn("x  y", html)
        self.assertNotIn("*", html)
        self.assertNotIn("`", html)

    def test_wide_table_falls_back_to_cards(self) -> None:
        wide = ("| Solution | Tableaux | Coût | Où ça vit |\n"
                "|---|---|---|---|\n"
                "| Bloc monospace | moyens | 30 minutes | dans Telegram |\n"
                "| Page web | parfaits | 2 heures | navigateur |")
        html = render.to_html(wide)
        self.assertNotIn("<pre>", html)
        self.assertIn("<b>Bloc monospace</b>", html)
        self.assertIn("Tableaux : moyens", html)
        self.assertIn("Où ça vit : navigateur", html)

    def test_a_pipe_is_not_a_table(self) -> None:
        text = "syntaxe : /switch claude|codex\n---"
        self.assertEqual(render.to_html(text), f"syntaxe : /switch claude|codex\n{render.RULE}")

    def test_table_inside_code_fence_is_left_alone(self) -> None:
        html = render.to_html("```\n| a | b |\n|---|---|\n```")
        self.assertEqual(html, "<pre>| a | b |\n|---|---|</pre>")


class SplitTests(unittest.TestCase):
    def test_short_text_stays_one_message(self) -> None:
        self.assertEqual(render.render("bonjour"), ["bonjour"])

    def test_never_cuts_inside_a_word(self) -> None:
        chunks = render.render("mot " * 3000, limit=200)
        self.assertTrue(all(len(chunk) <= 200 for chunk in chunks))
        self.assertTrue(all(chunk.startswith("mot") and chunk.endswith("mot")
                            for chunk in chunks))

    def test_prefers_paragraph_boundaries(self) -> None:
        text = ("a" * 120 + "\n\n") * 6
        chunks = render.render(text, limit=300)
        for chunk in chunks:
            self.assertFalse(chunk.startswith("\n"))
            for line in chunk.split("\n"):
                self.assertIn(line, ("", "a" * 120))

    def test_long_code_block_stays_a_code_block(self) -> None:
        body = "\n".join(f"ligne {n} du journal" for n in range(400))
        chunks = render.render(f"```\n{body}\n```", limit=500)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertTrue(chunk.startswith("<pre>"), chunk[:40])
            self.assertTrue(chunk.endswith("</pre>"), chunk[-40:])
            self.assertEqual(chunk.count("<pre>"), 1)

    def test_reopens_the_language_class(self) -> None:
        body = "\n".join(f"x{n} = {n}" for n in range(200))
        chunks = render.render(f"```python\n{body}\n```", limit=400)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertTrue(chunk.startswith('<pre><code class="language-python">'))
            self.assertTrue(chunk.endswith("</code></pre>"))

    def test_never_cuts_inside_a_tag_or_entity(self) -> None:
        text = "**gras** & `code` " * 300
        for chunk in render.render(text, limit=250):
            self.assertEqual(chunk.count("<"), chunk.count(">"))
            self.assertNotIn("&am", chunk.replace("&amp;", ""))

    def test_every_chunk_is_balanced(self) -> None:
        text = "\n\n".join([
            "# Titre", "- puce *une*", "```python\n" + "a = 1\n" * 300 + "```",
            "> une citation assez longue " * 20, "fin **grasse**",
        ])
        opened: list[str] = []
        for chunk in render.render(text, limit=600):
            self.assertLessEqual(len(chunk), 600 + 40)  # marge de réouverture
            for tag in render._ANY_TAG.findall(chunk):
                if tag.startswith("</"):
                    self.assertTrue(opened, f"fermeture orpheline dans {chunk[:60]}")
                    opened.pop()
                else:
                    opened.append(tag)
            self.assertFalse(opened, f"balise non fermée dans {chunk[:60]}")

    def test_empty_input(self) -> None:
        self.assertEqual(render.render(""), [""])


class PlainTests(unittest.TestCase):
    def test_plain_restores_readable_text(self) -> None:
        html = render.to_html("**gras** avec `a < b`")
        self.assertEqual(render.plain(html), "gras avec a < b")


if __name__ == "__main__":
    unittest.main()

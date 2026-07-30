from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123")
# Toujours tester le code du dépôt courant, jamais celui déployé dans /opt.
BOT_PATH = Path(__file__).resolve().parent.parent / "bot.py"
spec = importlib.util.spec_from_file_location("relay_bot", str(BOT_PATH))
bot = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(bot)
handoff_module = bot.handoff_builder

# Les tests exercent run_claude/run_codex, qui persistent désormais les ids de
# session : sans cette redirection, `unittest discover` (lancé par deploy.py
# AVANT le redémarrage) écraserait les fichiers d'état du service en marche.
# Même principe pour le journal, le log et la passation : les tests écrivaient
# leurs événements factices dans les fichiers du service (constaté en prod).
_TEST_STATE_DIR = tempfile.mkdtemp(prefix="tg-claude-test-state-")
bot.STATE_DIR = _TEST_STATE_DIR
bot.JOURNAL = os.path.join(_TEST_STATE_DIR, "relay-journal.jsonl")
bot.HANDOFF_FILE = os.path.join(_TEST_STATE_DIR, "handoff.md")
bot.CODEX_LAST_MESSAGE = os.path.join(_TEST_STATE_DIR, "codex-last-message.txt")
bot.LOG = os.path.join(_TEST_STATE_DIR, "bot.log")


class RelayUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.snapshot = Path(self.temp.name) / "state.json"
        self.path_patch = mock.patch.object(bot, "CLAUDE_USAGE_STATE", str(self.snapshot))
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        with bot.state_lock:
            bot.state.update({
                "cwd": "/root/repos",
                "claude_session_id": None,
                "codex_session_id": None,
                "model": bot.DEFAULT_MODEL,
                "claude_effort": bot.DEFAULT_CLAUDE_EFFORT,
                "codex_effort": bot.DEFAULT_CODEX_EFFORT,
                "preferred_engine": "claude",
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
                "claude_unavailable_window": None,
                "usage_monitor_status": "initialisation",
            })

    def write_snapshot(
        self,
        percent: float,
        reset: str = "2026-07-20T01:00:00+00:00",
        age_seconds: int = 0,
        error: str | None = None,
        weekly: float | None = 10.0,
        weekly_reset: str | None = "2026-07-26T16:00:00+00:00",
        omit_weekly: bool = False,
    ) -> None:
        observed = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        payload = {
            "last_percent": percent,
            "last_reset": reset,
            "observed_at": observed.isoformat(),
            "last_error": error,
        }
        if not omit_weekly:  # sonde d'avant la fenêtre hebdo → clés absentes
            payload["weekly_percent"] = weekly
            payload["weekly_reset"] = weekly_reset
        self.snapshot.write_text(json.dumps(payload))

    def test_fresh_low_usage_keeps_claude(self) -> None:
        self.write_snapshot(76)
        with mock.patch.object(bot, "journal"):
            bot.update_claude_usage()
        self.assertEqual(bot.state["preferred_engine"], "claude")
        self.assertEqual(bot.state["claude_usage"], 76)
        self.assertIn("actif", bot.state["usage_monitor_status"])

    def test_monitor_429_is_status_only_and_never_a_quota(self) -> None:
        self.write_snapshot(76, error="HTTP 429 (Retry-After: 120)")
        with mock.patch.object(bot, "journal"):
            bot.update_claude_usage()
        self.assertEqual(bot.state["preferred_engine"], "claude")
        self.assertIn("HTTP 429", bot.state["usage_monitor_status"])
        self.assertFalse(bot.is_quota_error("HTTP Error 429: Too Many Requests"))
        self.assertFalse(bot.is_quota_error("rate_limit_error"))

    def test_explicit_subscription_limit_is_a_quota(self) -> None:
        for message in (
            "You've hit your limit · resets at 3pm",
            "Claude usage limit reached",
            "quota exceeded",
        ):
            with self.subTest(message=message):
                self.assertTrue(bot.is_quota_error(message))

    def test_98_percent_switches_new_tasks_to_codex(self) -> None:
        self.write_snapshot(98)
        with mock.patch.object(bot, "journal"), mock.patch.object(bot, "send") as send:
            bot.update_claude_usage()
        self.assertEqual(bot.state["preferred_engine"], "codex")
        send.assert_called_once()

    def test_stale_snapshot_does_not_disable_claude(self) -> None:
        self.write_snapshot(100, age_seconds=bot.USAGE_SNAPSHOT_MAX_AGE + 1)
        with self.assertRaisesRegex(bot.UsageSnapshotError, "périmé"):
            bot.update_claude_usage()
        self.assertEqual(bot.state["preferred_engine"], "claude")

    def test_malformed_and_out_of_range_snapshots_are_rejected(self) -> None:
        self.snapshot.write_text("not-json")
        with self.assertRaisesRegex(bot.UsageSnapshotError, "illisible"):
            bot.load_claude_usage_snapshot()
        self.write_snapshot(101)
        with self.assertRaisesRegex(bot.UsageSnapshotError, "hors limites"):
            bot.load_claude_usage_snapshot()

    # --- Fenêtre hebdomadaire : même principe que la 5 h ---

    def test_weekly_at_98_switches_new_tasks_to_codex(self) -> None:
        # 5 h tranquille, mais le mur hebdomadaire est atteint.
        self.write_snapshot(12, weekly=98.0)
        with mock.patch.object(bot, "journal"), mock.patch.object(bot, "send") as send:
            bot.update_claude_usage()
        self.assertEqual(bot.state["preferred_engine"], "codex")
        self.assertEqual(bot.state["claude_unavailable_window"], bot.WINDOW_WEEKLY)
        self.assertIn("semaine", bot.state["claude_unavailable_reason"])
        self.assertIn("semaine", send.call_args.args[0])

    def test_weekly_block_survives_a_five_hour_rollover(self) -> None:
        """Régression : bloqué par l'hebdo, un nouveau créneau 5 h (usage ~0,
        reset différent) ne doit PAS rendre la main à Claude — le mur tient."""
        self.write_snapshot(99, weekly=99.0)
        with mock.patch.object(bot, "journal"), mock.patch.object(bot, "send"):
            bot.update_claude_usage()
        self.assertEqual(bot.state["claude_unavailable_window"], bot.WINDOW_WEEKLY)
        # Nouvelle session 5 h : 0 %, reset décalé. L'hebdo, elle, n'a pas bougé.
        self.write_snapshot(0, reset="2026-07-20T06:00:00+00:00", weekly=99.0)
        with mock.patch.object(bot, "journal"), mock.patch.object(bot, "send"), \
             mock.patch.object(bot, "release_deferred_tasks"):
            bot.update_claude_usage()
        self.assertEqual(bot.state["preferred_engine"], "codex")
        self.assertIsNotNone(bot.state["claude_unavailable_reason"])

    def test_weekly_rollover_restores_claude(self) -> None:
        self.write_snapshot(12, weekly=98.0)
        with mock.patch.object(bot, "journal"), mock.patch.object(bot, "send"):
            bot.update_claude_usage()
        self.assertEqual(bot.state["preferred_engine"], "codex")
        # La semaine tourne : usage au plancher et reset décalé d'une semaine.
        self.write_snapshot(12, weekly=1.0, weekly_reset="2026-08-02T16:00:00+00:00")
        with mock.patch.object(bot, "journal"), mock.patch.object(bot, "send") as send, \
             mock.patch.object(bot, "release_deferred_tasks") as released:
            bot.update_claude_usage()
        self.assertEqual(bot.state["preferred_engine"], "claude")
        self.assertIsNone(bot.state["claude_unavailable_window"])
        self.assertIn("semaine", send.call_args.args[0])
        released.assert_called_once()

    def test_five_hour_block_ignores_weekly_rollover(self) -> None:
        # Symétrique : bloqué par la 5 h, c'est bien SON reset qu'on attend.
        self.write_snapshot(99, weekly=40.0)
        with mock.patch.object(bot, "journal"), mock.patch.object(bot, "send"):
            bot.update_claude_usage()
        self.assertEqual(bot.state["claude_unavailable_window"], bot.WINDOW_FIVE_HOUR)
        self.write_snapshot(99, weekly=1.0, weekly_reset="2026-08-02T16:00:00+00:00")
        with mock.patch.object(bot, "journal"), mock.patch.object(bot, "send"), \
             mock.patch.object(bot, "release_deferred_tasks"):
            bot.update_claude_usage()
        self.assertEqual(bot.state["preferred_engine"], "codex")

    def test_snapshot_without_weekly_keeps_five_hour_behaviour(self) -> None:
        # Sonde pas encore redéployée : pas de clé hebdo, le relais fonctionne.
        self.write_snapshot(50, omit_weekly=True)
        with mock.patch.object(bot, "journal"):
            bot.update_claude_usage()
        self.assertEqual(bot.state["preferred_engine"], "claude")
        self.assertIsNone(bot.state["claude_weekly_usage"])
        self.write_snapshot(98, omit_weekly=True)
        with mock.patch.object(bot, "journal"), mock.patch.object(bot, "send"):
            bot.update_claude_usage()
        self.assertEqual(bot.state["preferred_engine"], "codex")
        self.assertEqual(bot.state["claude_unavailable_window"], bot.WINDOW_FIVE_HOUR)

    def test_absurd_weekly_value_is_ignored_not_fatal(self) -> None:
        self.write_snapshot(50, weekly=140.0)
        with mock.patch.object(bot, "journal"):
            bot.update_claude_usage()
        self.assertIsNone(bot.state["claude_weekly_usage"])
        self.assertEqual(bot.state["preferred_engine"], "claude")

    def test_status_shows_both_windows(self) -> None:
        self.write_snapshot(67, weekly=98.0)
        with mock.patch.object(bot, "journal"), mock.patch.object(bot, "send"):
            bot.update_claude_usage()
        message = bot.status_message()
        self.assertIn("Claude 5 h: 67 %", message)
        self.assertIn("Claude semaine: 98 %", message)

    def test_new_window_restores_claude(self) -> None:
        with bot.state_lock:
            bot.state["preferred_engine"] = "codex"
            bot.state["claude_unavailable_reason"] = "seuil 98 % atteint"
            bot.state["claude_unavailable_window"] = bot.WINDOW_FIVE_HOUR
            bot.state["claude_unavailable_reset_at"] = "2026-07-19T20:00:00+00:00"
        self.write_snapshot(1, reset="2026-07-20T01:00:00+00:00")
        with mock.patch.object(bot, "journal"), \
             mock.patch.object(bot, "send"), \
             mock.patch.object(bot, "release_deferred_tasks"):
            bot.update_claude_usage()
        self.assertEqual(bot.state["preferred_engine"], "claude")
        self.assertIsNone(bot.state["claude_unavailable_reason"])

    def test_same_window_does_not_restore_after_real_quota_error(self) -> None:
        reset = "2026-07-20T01:00:00+00:00"
        with bot.state_lock:
            bot.state["preferred_engine"] = "codex"
            bot.state["claude_unavailable_reason"] = "erreur de quota Claude"
            bot.state["claude_unavailable_reset_at"] = reset
        self.write_snapshot(1, reset=reset)
        with mock.patch.object(bot, "journal"):
            bot.update_claude_usage()
        self.assertEqual(bot.state["preferred_engine"], "codex")

    def test_transient_429_from_cli_does_not_reroute_task(self) -> None:
        task = {"text": "test", "cwd": "/root/repos"}
        with mock.patch.object(bot, "run_claude", return_value=(False, "", "HTTP 429 Too Many Requests")), \
             mock.patch.object(bot, "run_codex") as codex, \
             mock.patch.object(bot, "journal"), \
             mock.patch.object(bot, "send") as send, \
             mock.patch.object(bot, "typing"):
            bot.run_task(task)
        codex.assert_not_called()
        self.assertIn("Erreur Claude", send.call_args.args[0])

    def test_explicit_quota_error_reroutes_task(self) -> None:
        task = {"text": "test", "cwd": "/root/repos"}
        with mock.patch.object(bot, "run_claude", return_value=(False, "", "You've hit your limit")), \
             mock.patch.object(bot, "run_codex", return_value=(True, "réponse", "")) as codex, \
             mock.patch.object(bot, "journal"), \
             mock.patch.object(bot, "send"), \
             mock.patch.object(bot, "typing"):
            bot.run_task(task)
        codex.assert_called_once()
        self.assertEqual(codex.call_args.args[0], task)
        self.assertEqual(bot.state["preferred_engine"], "codex")

    def test_new_command_names_the_current_engine(self) -> None:
        with mock.patch.object(bot, "send") as send:
            bot.handle_command("/new")
            self.assertIn("Claude", send.call_args.args[0])
            bot.state["preferred_engine"] = "codex"
            bot.handle_command("/new")
            self.assertIn("Codex", send.call_args.args[0])

    def test_model_command_can_set_effort_and_enqueue_a_message(self) -> None:
        with mock.patch.object(bot, "send") as send, \
             mock.patch.object(bot, "save_model") as save_model, \
             mock.patch.object(bot, "save_effort") as save_effort, \
             mock.patch.object(bot, "enqueue_task") as enqueue:
            bot.handle_command("/opus xhigh corrige ce bug")
        self.assertEqual(bot.state["model"], "opus")
        self.assertEqual(bot.state["claude_effort"], "xhigh")
        self.assertEqual(bot.state["codex_effort"], bot.DEFAULT_CODEX_EFFORT)
        save_model.assert_called_once_with("opus")
        save_effort.assert_called_once_with(bot.CLAUDE_EFFORT_FILE, "xhigh")
        enqueue.assert_called_once_with("corrige ce bug")
        self.assertIn("Opus 4.8 · xhigh", send.call_args_list[0].args[0])

    def test_effort_is_forwarded_to_both_engines(self) -> None:
        bot.state["claude_effort"] = "medium"
        bot.state["codex_effort"] = "max"
        with mock.patch.object(bot, "run_process", return_value=(True, "ok", "")) as run:
            bot.run_claude({"text": "suite", "cwd": "/root/repos"}, None)
        claude_args = run.call_args.args[0]
        self.assertEqual(claude_args[claude_args.index("--effort") + 1], "medium")

        with mock.patch.object(bot, "run_process", return_value=(True, "", "")) as run:
            bot.run_codex({"text": "suite", "cwd": "/root/repos"}, None)
        codex_args = run.call_args.args[0]
        self.assertIn('model_reasoning_effort="max"', codex_args)

    def test_effort_command_can_change_one_engine_only(self) -> None:
        bot.state["claude_effort"] = "medium"
        bot.state["codex_effort"] = "max"
        with mock.patch.object(bot, "send"), \
             mock.patch.object(bot, "save_effort") as save_effort:
            bot.handle_command("/effort codex ultra")
        self.assertEqual(bot.state["claude_effort"], "medium")
        self.assertEqual(bot.state["codex_effort"], "ultra")
        save_effort.assert_called_once_with(bot.CODEX_EFFORT_FILE, "ultra")

    def test_response_footer_names_model_and_effort(self) -> None:
        bot.state["claude_effort"] = "medium"
        task = {"text": "test", "cwd": "/root/repos"}
        with mock.patch.object(bot, "engine_for_next_task", return_value="claude"), \
             mock.patch.object(bot, "run_claude", return_value=(True, "fait", "")), \
             mock.patch.object(bot, "journal"), \
             mock.patch.object(bot, "git_state", return_value=""), \
             mock.patch.object(bot, "typing"), \
             mock.patch.object(bot, "send") as send:
            bot.run_task(task)
        self.assertTrue(send.call_args.args[0].endswith(
            "— 🤖 Opus 4.8 · effort:medium"
        ))

    def test_iso_reset_is_humanized(self) -> None:
        reset = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.assertRegex(bot.format_reset(reset), r"dans 1 h 59|dans 2 h 00")


class HandoffTests(unittest.TestCase):
    """Le contexte ne doit traverser qu'au changement de moteur, dans les deux sens."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.handoff = Path(self.temp.name) / "handoff.md"
        self.journal = Path(self.temp.name) / "journal.jsonl"
        for name, value in (("HANDOFF_FILE", str(self.handoff)),
                            ("JOURNAL", str(self.journal)),
                            ("STATE_DIR", self.temp.name)):
            patch = mock.patch.object(bot, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        with bot.state_lock:
            bot.state.update({
                "cwd": "/root/repos",
                "claude_session_id": None,
                "codex_session_id": None,
                "preferred_engine": "claude",
                "codex_unavailable_reason": None,
                "codex_unavailable_at": None,
                "last_engine": None,
                "active_task_engine": None,
                "claude_unavailable_reason": None,
            })

    def write_journal(self, *events: dict) -> None:
        self.journal.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
        )

    def test_same_engine_twice_sends_no_handoff(self) -> None:
        bot.state["last_engine"] = "claude"
        bot.state["claude_session_id"] = "abc"
        self.assertFalse(bot.needs_handoff("claude"))

    def test_engine_switch_requires_a_handoff_in_both_directions(self) -> None:
        bot.state["last_engine"] = "claude"
        bot.state["claude_session_id"] = "abc"
        self.assertTrue(bot.needs_handoff("codex"))
        bot.state["last_engine"] = "codex"
        bot.state["codex_session_id"] = "xyz"
        self.assertTrue(bot.needs_handoff("claude"))

    def test_first_task_ever_has_nothing_to_hand_over(self) -> None:
        bot.state["last_engine"] = None
        self.assertFalse(bot.needs_handoff("claude"))

    def test_same_engine_without_session_still_gets_context(self) -> None:
        """Session perdue (redémarrage du bot) : le moteur repart aveugle sans ça."""
        bot.state["last_engine"] = "codex"
        bot.state["codex_session_id"] = None
        self.assertTrue(bot.needs_handoff("codex"))

    def test_handoff_file_holds_the_conversation_not_the_orders(self) -> None:
        self.write_journal(
            {"kind": "message", "text": "découpe les balades en segments"},
            {"kind": "response", "engine": "claude", "text": "voici le raisonnement"},
        )
        bot.state["last_engine"] = "claude"
        with mock.patch.object(bot, "git_state", return_value="hors dépôt Git"):
            path = bot.write_handoff("codex", "continue le travail", "/root/repos", "test")
        document = Path(path).read_text(encoding="utf-8")
        self.assertIn("CONTEXTE DE PASSATION", document)
        self.assertIn("découpe les balades en segments", document)
        self.assertIn("voici le raisonnement", document)
        self.assertIn("claude → codex", document)
        self.assertIn("continue le travail", document)

    def test_handoff_is_passed_as_a_file_parameter_to_claude(self) -> None:
        bot.state["last_engine"] = "codex"
        self.handoff.write_text("contexte", encoding="utf-8")
        with mock.patch.object(bot, "run_process", return_value=(True, "ok", "")) as run:
            bot.run_claude({"text": "suite", "cwd": "/root/repos"}, str(self.handoff))
        args = run.call_args.args[0]
        self.assertIn("--append-system-prompt-file", args)
        self.assertEqual(args[args.index("--append-system-prompt-file") + 1],
                         str(self.handoff))

    def test_handoff_is_piped_into_codex_stdin(self) -> None:
        self.handoff.write_text("contexte", encoding="utf-8")
        with mock.patch.object(bot, "run_process", return_value=(True, "", "")) as run:
            bot.run_codex({"text": "suite", "cwd": "/root/repos"}, str(self.handoff))
        self.assertEqual(run.call_args.kwargs["stdin_path"], str(self.handoff))

    def test_codex_session_is_resumed_instead_of_restarted(self) -> None:
        stream = '{"type":"thread.started","thread_id":"thread-1"}'
        with mock.patch.object(bot, "run_process", return_value=(True, stream, "")):
            bot.run_codex({"text": "un", "cwd": "/root/repos"}, None)
        self.assertEqual(bot.state["codex_session_id"], "thread-1")
        with mock.patch.object(bot, "run_process", return_value=(True, stream, "")) as run:
            bot.run_codex({"text": "deux", "cwd": "/root/repos"}, None)
        args = run.call_args.args[0]
        self.assertEqual(args[1:4], ["exec", "resume", "thread-1"])

    def test_codex_reply_comes_from_the_json_stream(self) -> None:
        stream = (
            '{"type":"thread.started","thread_id":"t"}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"PONG"}}'
        )
        with mock.patch.object(bot, "CODEX_LAST_MESSAGE", str(Path(self.temp.name) / "absent")), \
             mock.patch.object(bot, "run_process", return_value=(True, stream, "")):
            ok, out, _ = bot.run_codex({"text": "ping", "cwd": "/root/repos"}, None)
        self.assertTrue(ok)
        self.assertEqual(out, "PONG")

    def test_codex_quota_error_falls_back_to_claude(self) -> None:
        bot.state["preferred_engine"] = "codex"
        bot.state["last_engine"] = "codex"
        task = {"text": "suite", "cwd": "/root/repos"}
        with mock.patch.object(bot, "run_codex", return_value=(False, "", "You've hit your usage limit")), \
             mock.patch.object(bot, "run_claude", return_value=(True, "fait", "")) as claude, \
             mock.patch.object(bot, "journal"), \
             mock.patch.object(bot, "write_handoff", return_value="/tmp/h.md") as handoff, \
             mock.patch.object(bot, "git_state", return_value=""), \
             mock.patch.object(bot, "send"), \
             mock.patch.object(bot, "typing"):
            bot.run_task(task)
        claude.assert_called_once()
        self.assertEqual(claude.call_args.args[1], "/tmp/h.md")
        self.assertIn("quota", handoff.call_args.args[3])
        self.assertEqual(bot.state["last_engine"], "claude")

    def test_codex_is_rearmed_after_the_retry_delay(self) -> None:
        with mock.patch.object(bot, "journal"), mock.patch.object(bot, "send"):
            bot.mark_codex_unavailable("erreur de quota Codex")
            self.assertFalse(bot.engine_available("codex"))
            bot.state["codex_unavailable_at"] = time.time() - bot.CODEX_RETRY_SECONDS - 1
            self.assertTrue(bot.engine_available("codex"))

    def test_stale_claude_session_is_restarted_with_context(self) -> None:
        bot.state["claude_session_id"] = "019f7c33-0000-0000-0000-000000000000"
        bot.state["last_engine"] = "claude"
        results = [(False, "", "No conversation found with session ID: 019f…"),
                   (True, "fait", "")]
        with mock.patch.object(bot, "run_process", side_effect=results) as run, \
             mock.patch.object(bot, "journal"), \
             mock.patch.object(bot, "git_state", return_value=""):
            ok, out, _ = bot.run_claude({"text": "suite", "cwd": "/root/repos"}, None)
        self.assertTrue(ok)
        self.assertEqual(out, "fait")
        # Deuxième tentative : session neuve (--session-id) et passation jointe.
        second = run.call_args.args[0]
        self.assertIn("--session-id", second)
        self.assertNotIn("--resume", second)
        self.assertIn("--append-system-prompt-file", second)

    def test_stale_codex_session_is_restarted(self) -> None:
        bot.state["codex_session_id"] = "019f7c33-0000-0000-0000-000000000000"
        bot.state["last_engine"] = "codex"
        stream = '{"type":"thread.started","thread_id":"neuf"}'
        results = [(False, "", "thread/resume failed: no rollout found for thread id"),
                   (True, stream, "")]
        with mock.patch.object(bot, "run_process", side_effect=results) as run, \
             mock.patch.object(bot, "CODEX_LAST_MESSAGE", str(Path(self.temp.name) / "x")), \
             mock.patch.object(bot, "journal"), \
             mock.patch.object(bot, "git_state", return_value=""):
            ok, _, _ = bot.run_codex({"text": "suite", "cwd": "/root/repos"}, None)
        self.assertTrue(ok)
        self.assertNotIn("resume", run.call_args.args[0][:4])
        self.assertEqual(bot.state["codex_session_id"], "neuf")

    def test_recent_exchanges_are_kept_fuller_than_old_ones(self) -> None:
        events = [{"kind": "response", "engine": "claude", "text": "A" * 5000},
                  {"kind": "response", "engine": "claude", "text": "B" * 5000}]
        rendered = handoff_module.render_exchanges(events)
        self.assertGreater(rendered.count("B"), rendered.count("A"))

    def test_handoff_document_stays_within_budget(self) -> None:
        events = [{"kind": "message", "text": "X" * 4000} for _ in range(40)]
        document = handoff_module.build_handoff(
            events, from_engine="claude", to_engine="codex", objective="o",
            cwd="/root/repos", git="", reason="test",
        )
        self.assertLessEqual(len(document), handoff_module.MAX_TOTAL_CHARS)


class ContextAndPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_dir = str(Path(self.temp.name) / "state")
        Path(self.state_dir).mkdir()
        self.patches = [
            mock.patch.object(bot, "STATE_DIR", self.state_dir),
            mock.patch.object(bot, "CLAUDE_PROJECTS_DIR",
                              str(Path(self.temp.name) / "projects")),
            mock.patch.object(bot, "CODEX_SESSIONS_DIR",
                              str(Path(self.temp.name) / "sessions")),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        with bot.state_lock:
            bot.state.update({
                "cwd": "/root/repos", "model": "fable",
                "claude_session_id": None, "codex_session_id": None,
                "last_engine": None,
            })

    def _write_claude_transcript(self, session_id: str) -> None:
        directory = Path(self.temp.name) / "projects" / "-root-repos"
        directory.mkdir(parents=True)
        usage = {"input_tokens": 2, "cache_read_input_tokens": 196_578,
                 "cache_creation_input_tokens": 18_839, "output_tokens": 2679}
        lines = [
            json.dumps({"type": "user", "message": {"content": "salut"}}),
            json.dumps({"type": "assistant",
                        "message": {"usage": {"input_tokens": 1,
                                              "cache_read_input_tokens": 10}}}),
            json.dumps({"type": "assistant", "message": {"usage": usage}}),
        ]
        (directory / f"{session_id}.jsonl").write_text("\n".join(lines), encoding="utf-8")

    def test_claude_context_reads_last_assistant_usage(self) -> None:
        session = "11111111-2222-3333-4444-555555555555"
        self._write_claude_transcript(session)
        with bot.state_lock:
            bot.state["claude_session_id"] = session
        # 2 + 196 578 + 18 839 = 215 419 tokens sur 1M (Fable) → 22 %.
        self.assertEqual(bot.claude_context(), "22 % (215k/1M)")

    def test_claude_context_without_session_or_transcript(self) -> None:
        self.assertIsNone(bot.claude_context())
        with bot.state_lock:
            bot.state["claude_session_id"] = "absent-du-disque"
        self.assertIsNone(bot.claude_context())

    def test_codex_context_reads_token_count_and_window(self) -> None:
        thread = "019f7cbc-327b-7402-b2d7-dcb7dd431b6a"
        directory = Path(self.temp.name) / "sessions" / "2026" / "07" / "19"
        directory.mkdir(parents=True)
        event = {"timestamp": "2026-07-19T23:35:40Z", "type": "event_msg",
                 "payload": {"type": "token_count", "info": {
                     "last_token_usage": {"input_tokens": 12_741, "output_tokens": 5},
                     "model_context_window": 258_400}}}
        (directory / f"rollout-2026-07-19T19-35-36-{thread}.jsonl").write_text(
            json.dumps({"type": "session_meta"}) + "\n" + json.dumps(event),
            encoding="utf-8")
        with bot.state_lock:
            bot.state["codex_session_id"] = thread
        self.assertEqual(bot.codex_context(), "5 % (13k/258k)")

    def test_status_shows_context_line_with_fallbacks(self) -> None:
        self.assertIn("🧠 contexte: Claude — · Codex —", bot.status_message())

    def test_sessions_survive_restart(self) -> None:
        # restore_persisted_state() n'accepte un cwd persisté que s'il désigne un
        # dossier réel. Le test doit donc en fournir un qui existe vraiment,
        # sinon il ne passe que sur une machine possédant /root/repos.
        workdir = str(Path(self.temp.name) / "repos")
        Path(workdir).mkdir()
        with bot.state_lock:
            bot.state.update({"claude_session_id": "abc-123",
                              "codex_session_id": "def-456",
                              "last_engine": "claude", "cwd": workdir})
        for key in bot.PERSISTED_KEYS:
            bot.persist_state_key(key)
        with bot.state_lock:  # simule le redémarrage du service
            bot.state.update({"claude_session_id": None, "codex_session_id": None,
                              "last_engine": None, "cwd": "/tmp"})
        bot.restore_persisted_state()
        self.assertEqual(bot.state["claude_session_id"], "abc-123")
        self.assertEqual(bot.state["codex_session_id"], "def-456")
        self.assertEqual(bot.state["last_engine"], "claude")
        self.assertEqual(bot.state["cwd"], workdir)

    def test_cleared_session_is_not_resurrected(self) -> None:
        with bot.state_lock:
            bot.state["claude_session_id"] = "abc-123"
        bot.persist_state_key("claude_session_id")
        with bot.state_lock:
            bot.state["claude_session_id"] = None
        bot.persist_state_key("claude_session_id")  # /new : le fichier disparaît
        bot.restore_persisted_state()
        self.assertIsNone(bot.state["claude_session_id"])

    def test_restored_cwd_must_exist(self) -> None:
        with bot.state_lock:
            bot.state["cwd"] = "/dossier/disparu"
        Path(self.state_dir, "cwd").write_text("/dossier/disparu", encoding="utf-8")
        with bot.state_lock:
            bot.state["cwd"] = "/root/repos"
        bot.restore_persisted_state()
        self.assertEqual(bot.state["cwd"], "/root/repos")


class LongRunningTaskTests(unittest.TestCase):
    def test_run_process_has_no_wall_clock_timeout(self) -> None:
        """Régression : une tâche de plus de 30 min doit continuer à tourner."""
        class FakeProcess:
            pid = 1234
            returncode = 0

            def poll(self):
                return self.returncode

            def communicate(self):
                return "terminé après 4 heures", ""

        with mock.patch.object(bot.subprocess, "Popen", return_value=FakeProcess()):
            ok, output, error = bot.run_process(["codex"], "/root/repos")
        self.assertTrue(ok)
        self.assertEqual(output, "terminé après 4 heures")
        self.assertEqual(error, "")


class MediaTests(unittest.TestCase):
    """Photos Telegram → tâche portant le chemin du fichier pour Read."""

    def setUp(self) -> None:
        while not bot.task_q.empty():
            bot.task_q.get_nowait()
        with bot.state_lock:
            bot.state["cwd"] = "/root/repos"

    def test_extract_media_prefers_the_largest_photo(self) -> None:
        msg = {"photo": [{"file_id": "small"}, {"file_id": "big"}]}
        self.assertEqual(bot.extract_media(msg), ("big", ".jpg"))

    def test_extract_media_accepts_an_image_document(self) -> None:
        msg = {"document": {"file_id": "doc1", "mime_type": "image/png",
                            "file_name": "carte.png"}}
        self.assertEqual(bot.extract_media(msg), ("doc1", ".png"))

    def test_extract_media_ignores_a_non_image_document(self) -> None:
        msg = {"document": {"file_id": "d", "mime_type": "application/pdf",
                            "file_name": "x.pdf"}}
        self.assertIsNone(bot.extract_media(msg))

    def test_extract_media_is_none_for_plain_text(self) -> None:
        self.assertIsNone(bot.extract_media({"text": "bonjour"}))

    def test_photo_task_carries_the_path_and_the_caption(self) -> None:
        msg = {"photo": [{"file_id": "big"}], "caption": "regarde ce point"}
        with mock.patch.object(bot, "download_media", return_value="/tmp/p/img.jpg") as dl, \
             mock.patch.object(bot, "journal"), mock.patch.object(bot, "send"), \
             mock.patch.object(bot, "git_state", return_value=""), \
             mock.patch.object(bot, "engine_for_next_task", return_value="claude"):
            bot.handle_message(msg)
        dl.assert_called_once_with("big", ".jpg")
        task = bot.task_q.get_nowait()
        self.assertIn("/tmp/p/img.jpg", task["text"])
        self.assertIn("regarde ce point", task["text"])

    def test_photo_without_caption_still_enqueues_the_path(self) -> None:
        msg = {"photo": [{"file_id": "big"}]}
        with mock.patch.object(bot, "download_media", return_value="/tmp/p/img.jpg"), \
             mock.patch.object(bot, "journal"), mock.patch.object(bot, "send"), \
             mock.patch.object(bot, "git_state", return_value=""), \
             mock.patch.object(bot, "engine_for_next_task", return_value="claude"):
            bot.handle_message(msg)
        task = bot.task_q.get_nowait()
        self.assertIn("/tmp/p/img.jpg", task["text"])
        self.assertTrue(task["text"].strip())

    def test_failed_download_notifies_and_enqueues_nothing(self) -> None:
        msg = {"photo": [{"file_id": "big"}], "caption": "x"}
        with mock.patch.object(bot, "download_media", side_effect=RuntimeError("boom")), \
             mock.patch.object(bot, "journal"), \
             mock.patch.object(bot, "send") as send:
            bot.handle_message(msg)
        self.assertTrue(bot.task_q.empty())
        self.assertTrue(any("échoué" in call.args[0] for call in send.call_args_list))


if __name__ == "__main__":
    unittest.main()

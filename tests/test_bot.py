from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123")
spec = importlib.util.spec_from_file_location("relay_bot", "/opt/tg-claude/bot.py")
bot = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(bot)


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
                "model": bot.DEFAULT_MODEL,
                "preferred_engine": "claude",
                "last_engine": None,
                "active_task_engine": None,
                "claude_usage": None,
                "claude_reset_at": None,
                "claude_usage_observed_at": None,
                "claude_unavailable_reset_at": None,
                "claude_unavailable_reason": None,
                "usage_monitor_status": "initialisation",
            })

    def write_snapshot(
        self,
        percent: float,
        reset: str = "2026-07-20T01:00:00+00:00",
        age_seconds: int = 0,
        error: str | None = None,
    ) -> None:
        observed = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        self.snapshot.write_text(json.dumps({
            "last_percent": percent,
            "last_reset": reset,
            "observed_at": observed.isoformat(),
            "last_error": error,
        }))

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

    def test_new_window_restores_claude(self) -> None:
        with bot.state_lock:
            bot.state["preferred_engine"] = "codex"
            bot.state["claude_unavailable_reason"] = "seuil 98 % atteint"
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
        codex.assert_called_once_with(task)
        self.assertEqual(bot.state["preferred_engine"], "codex")

    def test_new_command_names_the_current_engine(self) -> None:
        with mock.patch.object(bot, "send") as send:
            bot.handle_command("/new")
            self.assertIn("Claude", send.call_args.args[0])
            bot.state["preferred_engine"] = "codex"
            bot.handle_command("/new")
            self.assertIn("Codex", send.call_args.args[0])

    def test_iso_reset_is_humanized(self) -> None:
        reset = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.assertRegex(bot.format_reset(reset), r"dans 1 h 59|dans 2 h 00")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


HOOK = Path(__file__).resolve().parent.parent / "confirm_hook.py"


def hook_input(command: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


class ConfirmHookTests(unittest.TestCase):
    def run_hook(self, command: str, state_dir: Path, timeout: int = 0):
        env = os.environ.copy()
        env["TG_STATE_DIR"] = str(state_dir)
        env["TG_CONFIRM_TIMEOUT"] = str(timeout)
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=hook_input(command),
            text=True,
            capture_output=True,
            env=env,
            timeout=5,
        )

    def test_safe_command_needs_no_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = Path(temp)
            result = self.run_hook("git status --short", state_dir)
            self.assertEqual(result.returncode, 0)
            self.assertFalse((state_dir / "pending").exists())

    def test_timeout_blocks_and_cleans_pending_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = Path(temp)
            result = self.run_hook("git reset --hard HEAD", state_dir)
            self.assertEqual(result.returncode, 2)
            self.assertIn("Aucune confirmation", result.stderr)
            self.assertFalse((state_dir / "pending").exists())
            self.assertFalse((state_dir / "decision").exists())

    def test_hublot_can_allow_a_dangerous_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = Path(temp)
            env = os.environ.copy()
            env["TG_STATE_DIR"] = str(state_dir)
            env["TG_CONFIRM_TIMEOUT"] = "3"
            process = subprocess.Popen(
                [sys.executable, str(HOOK)],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                env=env,
            )
            assert process.stdin is not None
            process.stdin.write(hook_input("rm -rf /tmp/example"))
            process.stdin.close()

            pending = state_dir / "pending"
            deadline = time.monotonic() + 2
            while not pending.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(pending.exists())
            self.assertEqual(pending.read_text(), "rm -rf /tmp/example")
            (state_dir / "decision").write_text("allow")

            self.assertEqual(process.wait(timeout=4), 0)
            self.assertFalse(pending.exists())
            self.assertFalse((state_dir / "decision").exists())


if __name__ == "__main__":
    unittest.main()

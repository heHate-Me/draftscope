from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from draftscope.records import DataError
from draftscope.scheduler import LAUNCH_AGENT_LABEL, build_launch_agent_payload


class SchedulerTests(unittest.TestCase):
    def test_launch_agent_uses_absolute_launcher_config_and_secret_free_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = root / "run_draftscope.py"
            config = root / "draftscope.toml"
            launcher.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            config.write_text("[draftscope]\n", encoding="utf-8")

            payload = build_launch_agent_payload(
                launcher=launcher,
                config=config,
                working_directory=root,
                log_directory=root / "data" / "logs",
                weekday=2,
                hour=6,
                minute=15,
            )

        self.assertEqual(payload["Label"], LAUNCH_AGENT_LABEL)
        self.assertEqual(
            payload["ProgramArguments"],
            [str(launcher.resolve()), "update", "--config", str(config.resolve())],
        )
        self.assertEqual(
            payload["StartCalendarInterval"],
            {"Weekday": 2, "Hour": 6, "Minute": 15},
        )
        self.assertNotIn("CFBD_API_KEY", str(payload))

    def test_schedule_rejects_invalid_clock_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = root / "run_draftscope.py"
            config = root / "draftscope.toml"
            launcher.write_text("", encoding="utf-8")
            config.write_text("", encoding="utf-8")
            with self.assertRaises(DataError):
                build_launch_agent_payload(
                    launcher=launcher,
                    config=config,
                    working_directory=root,
                    log_directory=root,
                    hour=24,
                )


if __name__ == "__main__":
    unittest.main()

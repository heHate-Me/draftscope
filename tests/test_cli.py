from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import ctypes
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from draftscope.cli import _latest_model_history_path, _parser, main
from draftscope.credentials import (
    _load_with_security_framework,
    load_cfbd_api_key_from_keychain,
    resolve_cfbd_api_key,
)
from draftscope.data_sources import CFBDClient
from draftscope.doctor import diagnose
from draftscope.records import DataError, file_sha256, load_records, write_records
from draftscope.weekly import load_config


class FirstRunCLITests(unittest.TestCase):
    def test_scout_example_finds_the_working_checkout_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            examples = root / "examples"
            examples.mkdir()
            players = examples / "demo_players.csv"
            players.write_text("name,position\nDemo,WR\n", encoding="utf-8")
            history_dir = root / "data" / "model_history"
            history_dir.mkdir(parents=True)
            history = history_dir / "college_history.csv"
            history.write_text(
                "name,position,draft_year,drafted\nPast,WR,2026,True\n",
                encoding="utf-8",
            )
            history.with_suffix(".metadata.json").write_text(
                json.dumps(
                    {
                        "model_stage": "college_precombine",
                        "quality_gate_status": "pass",
                        "source_failures": [],
                        "contract_audit": {"status": "pass"},
                        "training_artifact": {
                            "sha256": file_sha256(history),
                            "bytes": history.stat().st_size,
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch("draftscope.cli.Path.cwd", return_value=root):
                selected = _latest_model_history_path(str(players))

            self.assertEqual(selected, history.resolve())

    def test_scout_finds_a_strict_default_college_history_after_an_earlier_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            players = data_dir / "players_2026.csv"
            players.write_text("name,position\nProspect,WR\n", encoding="utf-8")
            (data_dir / "state.json").write_text(
                json.dumps({"latest_model_history": None}), encoding="utf-8"
            )
            history_dir = data_dir / "model_history"
            history_dir.mkdir()
            history = history_dir / "college_history.csv"
            history.write_text("name,position,drafted\nPast,WR,True\n", encoding="utf-8")
            history.with_suffix(".metadata.json").write_text(
                json.dumps(
                    {
                        "model_stage": "college_precombine",
                        "quality_gate_status": "pass",
                        "source_failures": [],
                        "contract_audit": {"status": "pass"},
                        "training_artifact": {
                            "sha256": file_sha256(history),
                            "bytes": history.stat().st_size,
                        },
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(_latest_model_history_path(str(players)), history.resolve())

    def test_automatic_history_must_match_the_requested_draft_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            players = data_dir / "players_2026.csv"
            players.write_text("name,position\nProspect,WR\n", encoding="utf-8")
            history_dir = data_dir / "model_history"
            history_dir.mkdir()
            history = history_dir / "college_history.csv"
            history.write_text("name,position,drafted\nPast,WR,True\n", encoding="utf-8")
            history.with_suffix(".metadata.json").write_text(
                json.dumps(
                    {
                        "model_stage": "college_precombine",
                        "quality_gate_status": "pass",
                        "source_failures": [],
                        "contract_audit": {"status": "pass"},
                        "draft_years": [2022, 2023, 2024, 2025, 2026],
                        "training_artifact": {
                            "sha256": file_sha256(history),
                            "bytes": history.stat().st_size,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (data_dir / "state.json").write_text(
                json.dumps({"latest_model_history": str(history)}), encoding="utf-8"
            )

            self.assertIsNone(
                _latest_model_history_path(
                    str(players),
                    expected_draft_years=tuple(range(2017, 2027)),
                )
            )
            self.assertEqual(
                _latest_model_history_path(
                    str(players),
                    expected_draft_years=tuple(range(2022, 2027)),
                ),
                history.resolve(),
            )

    def test_automatic_history_rejects_csv_sidecar_hash_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            players = data_dir / "players_2026.csv"
            players.write_text("name,position\nProspect,WR\n", encoding="utf-8")
            history_dir = data_dir / "model_history"
            history_dir.mkdir()
            history = history_dir / "college_history.csv"
            history.write_text("name,position,drafted\nPast,WR,True\n", encoding="utf-8")
            history.with_suffix(".metadata.json").write_text(
                json.dumps(
                    {
                        "model_stage": "college_precombine",
                        "quality_gate_status": "pass",
                        "source_failures": [],
                        "contract_audit": {"status": "pass"},
                        "training_artifact": {
                            "sha256": file_sha256(history),
                            "bytes": history.stat().st_size,
                        },
                    }
                ),
                encoding="utf-8",
            )
            history.write_text("name,position,drafted\nChanged,WR,False\n", encoding="utf-8")

            self.assertIsNone(_latest_model_history_path(str(players)))

    def test_parser_exposes_doctor_add_and_configure_key(self) -> None:
        self.assertEqual(_parser().parse_args(["doctor"]).command, "doctor")
        self.assertEqual(_parser().parse_args(["configure-key"]).command, "configure-key")
        args = _parser().parse_args(
            [
                "add",
                "Example Prospect",
                "--position",
                "WR",
                "--school",
                "Example U",
                "--out",
                "players.csv",
            ]
        )
        self.assertEqual(args.command, "add")

    def test_add_creates_then_updates_player_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "players.csv"
            with redirect_stdout(StringIO()):
                result = main(
                    [
                        "add",
                        "Manual Prospect",
                        "--position",
                        "WR",
                        "--school",
                        "Example U",
                        "--season",
                        "2026",
                        "--date-of-birth",
                        "2004-06-01",
                        "--height",
                        "6-2",
                        "--weight",
                        "205",
                        "--entry-probability",
                        "65%",
                        "--out",
                        str(destination),
                    ]
                )
            self.assertEqual(result, 0)
            first = load_records(destination)[0]
            first_id = first["player_id"]
            self.assertEqual(first["height_in"], 74.0)
            self.assertEqual(first["weight_lb"], 205.0)
            self.assertEqual(first["draft_entry_probability"], 0.65)
            self.assertEqual(first["projected_draft_year"], 2027.0)
            self.assertEqual(first["measurement_source"], "manual_entry")
            self.assertFalse(first["measurements_verified"])
            self.assertGreater(first["age_at_draft"], 22.0)

            with redirect_stdout(StringIO()):
                result = main(
                    [
                        "add",
                        "Manual Prospect",
                        "--position",
                        "WR",
                        "--school",
                        "Example U",
                        "--season",
                        "2026",
                        "--weight",
                        "210",
                        "--out",
                        str(destination),
                    ]
                )
            self.assertEqual(result, 0)
            rows = load_records(destination)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["player_id"], first_id)
            self.assertEqual(rows[0]["height_in"], 74.0)
            self.assertEqual(rows[0]["weight_lb"], 210.0)
            self.assertEqual(rows[0]["draft_entry_probability"], 0.65)
            self.assertAlmostEqual(rows[0]["bmi"], 703 * 210 / (74 * 74), places=4)

    def test_add_rejects_invalid_manual_values_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "players.csv"
            stderr = StringIO()
            with redirect_stderr(stderr):
                result = main(
                    [
                        "add",
                        "Bad Prospect",
                        "--position",
                        "WR",
                        "--school",
                        "Example U",
                        "--entry-probability",
                        "140%",
                        "--out",
                        str(destination),
                    ]
                )
            self.assertEqual(result, 2)
            self.assertIn("between 0 and 1", stderr.getvalue())
            self.assertFalse(destination.exists())

    def test_empty_player_file_stops_before_history_download(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            players = Path(temporary) / "players.csv"
            write_records(players, [])
            stderr = StringIO()
            with (
                patch("draftscope.cli._load_evaluation_histories") as history,
                redirect_stderr(stderr),
            ):
                result = main(["rank", "--players", str(players), "--no-auto-team-needs"])
            self.assertEqual(result, 2)
            history.assert_not_called()
            self.assertIn("contains headers but no player rows", stderr.getvalue())
            self.assertIn("draftscope add", stderr.getvalue())

    def test_doctor_is_network_free_and_recommends_available_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "draftscope.toml"
            output = StringIO()
            with (
                patch("draftscope.doctor.cfbd_api_key_status", return_value="missing"),
                redirect_stdout(output),
            ):
                result = main(["doctor", "--config", str(config)])
            text = output.getvalue()
            self.assertEqual(result, 0)
            self.assertIn("[PASS] Launcher", text)
            self.assertTrue("run_draftscope.py" in text or "`draftscope` resolves" in text)
            self.assertIn("SportsDataverse", text)
            self.assertIn(" add ", text)
            self.assertIn("Config file is missing", text)

    def test_default_doctor_does_not_touch_keychain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "draftscope.toml"
            config.write_text(
                '[draftscope]\ncollege_data_provider = "sportsdataverse"\ncfbd_fallback = false\n',
                encoding="utf-8",
            )
            with patch("draftscope.doctor.cfbd_api_key_status") as key_status:
                result = diagnose(config)
            self.assertTrue(result["ok"])
            key_status.assert_not_called()

    def test_doctor_json_handles_invalid_season_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "draftscope.toml"
            config.write_text('[draftscope]\nseason = "not-a-year"\n', encoding="utf-8")
            output = StringIO()
            with (
                patch("draftscope.doctor.cfbd_api_key_status", return_value="missing"),
                redirect_stdout(output),
            ):
                result = main(["doctor", "--config", str(config), "--json"])
            report = json.loads(output.getvalue())
            self.assertEqual(result, 1)
            self.assertFalse(report["ok"])
            self.assertTrue(any(check["name"] == "Season" for check in report["checks"]))

    def test_missing_config_error_has_copy_and_doctor_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "draftscope.toml"
            with self.assertRaises(DataError) as raised:
                load_config(missing)
            self.assertIn("draftscope.toml.example", str(raised.exception))
            self.assertIn("doctor", str(raised.exception))

    def test_config_template_is_valid_and_credential_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "draftscope.toml"
            output = StringIO()
            with redirect_stdout(output):
                result = main(["template", "config", "--out", str(destination)])
            parsed = tomllib.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(result, 0)
            self.assertEqual(parsed["draftscope"]["college_data_provider"], "sportsdataverse")
            self.assertFalse(parsed["draftscope"]["cfbd_fallback"])
            self.assertNotIn("api_key", destination.read_text(encoding="utf-8").lower())

    def test_configure_key_uses_one_hidden_prompt_and_no_storage_subprocess(self) -> None:
        key = "cfbd-live-secret-123456"
        output = StringIO()
        observed: list[bytes] = []
        buffers: list[bytearray] = []

        def fake_native_store(secret: bytearray) -> None:
            observed.append(bytes(secret))
            buffers.append(secret)

        with (
            patch("draftscope.cli.getpass.getpass", return_value=key) as prompt,
            patch("draftscope.credentials.keychain_available", return_value=True),
            patch(
                "draftscope.credentials._store_with_security_framework",
                side_effect=fake_native_store,
            ) as native_store,
            redirect_stdout(output),
        ):
            result = main(["configure-key"])
        self.assertEqual(result, 0)
        prompt.assert_called_once()
        native_store.assert_called_once()
        self.assertEqual(observed, [key.encode("utf-8")])
        self.assertEqual(bytes(buffers[0]), b"\0" * len(key.encode("utf-8")))
        self.assertNotIn(key, output.getvalue())


class CredentialResolutionTests(unittest.TestCase):
    def test_environment_precedes_keychain(self) -> None:
        with (
            patch.dict(os.environ, {"CFBD_API_KEY": "environment-key"}),
            patch("draftscope.credentials.load_cfbd_api_key_from_keychain") as keychain,
        ):
            self.assertEqual(resolve_cfbd_api_key(), "environment-key")
        keychain.assert_not_called()

    def test_keychain_is_used_when_environment_is_absent(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "draftscope.credentials.load_cfbd_api_key_from_keychain",
                return_value="keychain-key",
            ),
        ):
            self.assertEqual(resolve_cfbd_api_key(), "keychain-key")

    def test_keychain_read_uses_native_framework(self) -> None:
        with (
            patch("draftscope.credentials.keychain_available", return_value=True),
            patch(
                "draftscope.credentials._load_with_security_framework",
                return_value="secret-from-keychain",
            ) as native_read,
        ):
            self.assertEqual(load_cfbd_api_key_from_keychain(), "secret-from-keychain")
        native_read.assert_called_once_with()

    def test_native_keychain_read_copies_and_frees_password_data(self) -> None:
        payload = b"secret-from-native-keychain"
        native_buffer = ctypes.create_string_buffer(payload)
        freed: list[int | None] = []

        def find_password(
            _keychain,
            _service_length,
            _service,
            _account_length,
            _account,
            password_length,
            password_data,
            _item,
        ):
            ctypes.cast(password_length, ctypes.POINTER(ctypes.c_uint32)).contents.value = len(payload)
            ctypes.cast(password_data, ctypes.POINTER(ctypes.c_void_p)).contents.value = ctypes.addressof(
                native_buffer
            )
            return 0

        def free_content(_attributes, password_data):
            freed.append(password_data.value)
            return 0

        security = SimpleNamespace(
            SecKeychainFindGenericPassword=find_password,
            SecKeychainItemFreeContent=free_content,
        )
        with patch(
            "draftscope.credentials._load_keychain_frameworks",
            return_value=(security, SimpleNamespace()),
        ):
            value = _load_with_security_framework()

        self.assertEqual(value, payload.decode("utf-8"))
        self.assertEqual(freed, [ctypes.addressof(native_buffer)])

    def test_client_missing_key_error_offers_secure_and_manual_paths(self) -> None:
        with patch("draftscope.data_sources.resolve_cfbd_api_key", return_value=None):
            with self.assertRaises(DataError) as raised:
                CFBDClient()
        message = str(raised.exception)
        self.assertIn("configure-key", message)
        self.assertIn("draftscope add", message)


if __name__ == "__main__":
    unittest.main()

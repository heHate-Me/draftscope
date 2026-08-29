from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from draftscope.model import DraftPrediction
from draftscope.records import DataError, load_records, write_records
from draftscope.weekly import (
    _WeeklyUpdateLock,
    _cleanup_abandoned_staging,
    _create_output_transaction_root,
    _publication_journal_path,
    _recover_interrupted_publication,
    run_weekly_update,
)


class _SuccessfulRefreshClient:
    def refresh_candidate(self, candidate, *, season, week):
        updated = dict(candidate)
        updated["season"] = season
        updated["as_of_week"] = week
        return updated, {"name": candidate.get("name")}


class _Evaluation:
    def __init__(self) -> None:
        self.player = {
            "name": "Safety Prospect",
            "position": "WR",
            "school": "Example U",
            "season": 2026,
            "as_of_week": 4,
        }
        self.profile_score = 60.0
        self.draft_prediction = DraftPrediction(available=False)
        self.evidence_coverage = 0.75
        self.profile_tier = "Synthetic tier"
        self.team_fits = []
        self.as_of_date = "2026-08-28"
        self.benchmark_window = (2017, 2026)
        self.benchmark_context = {}
        self.projected_pick_range = None
        self.benchmarks = []
        self.categories = []
        self.combine_prediction = None
        self.overall_comps = []
        self.physical_comps = []
        self.pick_comps = []
        self.warnings = []

    def to_dict(self):
        return {"player": self.player}


def _tree_contents(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class WeeklySafetyTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, dict[str, object]]:
        output_dir = root / "data"
        players_path = output_dir / "players.csv"
        write_records(
            players_path,
            [
                {
                    "name": "Safety Prospect",
                    "position": "WR",
                    "school": "Example U",
                    "season": 2026,
                    "as_of_week": 2,
                }
            ],
        )
        (output_dir / "model_history").mkdir(parents=True)
        (output_dir / "model_history" / "previous.json").write_text(
            '{"checkpoint": "old"}\n', encoding="utf-8"
        )
        (output_dir / "reports" / "latest").mkdir(parents=True)
        (output_dir / "reports" / "latest" / "board.txt").write_text(
            "old board\n", encoding="utf-8"
        )
        (output_dir / "state.json").write_text(
            json.dumps({"season": 2026, "week": 2, "sentinel": "old"}) + "\n",
            encoding="utf-8",
        )
        config: dict[str, object] = {
            "season": 2026,
            "week": 4,
            "players_file": str(players_path),
            "output_dir": str(output_dir),
            "cache_dir": str(root / "cache"),
            "college_data_provider": "cfbd",
            "discover_candidates": False,
            "build_weekly_history": False,
            "auto_team_needs": False,
        }
        return players_path, output_dir, config

    def test_second_update_is_rejected_while_process_lock_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            players_path, output_dir, config = self._fixture(root)
            original_players = players_path.read_bytes()
            original_output = _tree_contents(output_dir)

            with _WeeklyUpdateLock(output_dir):
                with self.assertRaisesRegex(
                    DataError, "Another DraftScope weekly update is already running"
                ):
                    run_weekly_update(config, refresh_sources=False)

            self.assertEqual(players_path.read_bytes(), original_players)
            self.assertEqual(_tree_contents(output_dir), original_output)

    @unittest.skipIf(os.name == "nt", "symbolic-link semantics differ on Windows")
    def test_lock_refuses_symlink_without_modifying_its_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "data"
            victim = root / "victim.txt"
            victim.write_text("do not overwrite\n", encoding="utf-8")
            lock = _WeeklyUpdateLock(output_dir)
            lock.path.unlink(missing_ok=True)
            lock.path.symlink_to(victim)

            try:
                with self.assertRaisesRegex(DataError, "securely open weekly-update lock"):
                    with lock:
                        self.fail("unsafe lock unexpectedly opened")
            finally:
                lock.path.unlink(missing_ok=True)

            self.assertEqual(victim.read_text(encoding="utf-8"), "do not overwrite\n")

    def test_lock_is_owner_only_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock = _WeeklyUpdateLock(Path(temporary) / "data")
            with lock:
                metadata = lock.path.lstat()
                self.assertTrue(stat.S_ISREG(metadata.st_mode))
                if os.name != "nt":
                    self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)

    def test_players_inside_output_publish_with_final_paths_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "data"
            players_path = output_dir / "players.csv"
            write_records(
                players_path,
                [
                    {
                        "name": "Safety Prospect",
                        "position": "WR",
                        "school": "Example U",
                        "season": 2026,
                        "as_of_week": 2,
                    }
                ],
            )
            config = {
                "season": 2026,
                "week": 4,
                "players_file": str(players_path),
                "output_dir": str(output_dir),
                "cache_dir": str(root / "cache"),
                "college_data_provider": "cfbd",
                "discover_candidates": False,
                "build_weekly_history": False,
                "auto_team_needs": False,
            }
            with (
                patch(
                    "draftscope.weekly.CFBDClient",
                    return_value=_SuccessfulRefreshClient(),
                ),
                patch(
                    "draftscope.weekly.load_nflverse_history",
                    return_value=([], {"population": "synthetic pool"}),
                ),
                patch("draftscope.weekly.ProspectEvaluator") as evaluator,
            ):
                evaluator.return_value.rank.return_value = [_Evaluation()]
                result = run_weekly_update(config, refresh_sources=False)

            state_text = (output_dir / "state.json").read_text(encoding="utf-8")
            state = json.loads(state_text)
            self.assertTrue(result.board_path.is_file())
            self.assertEqual(Path(state["latest_board"]), result.board_path)
            self.assertNotIn(".draftscope-update-", state_text)
            self.assertEqual(load_records(players_path)[0]["as_of_week"], 4)

    def test_empty_source_pool_publishes_history_state_without_advertising_a_board(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "data"
            players_path = output_dir / "players.csv"
            write_records(players_path, [])
            stale_latest = output_dir / "reports" / "latest"
            stale_latest.mkdir(parents=True)
            (stale_latest / "board.csv").write_text(
                "rank,name\n1,Stale Prospect\n", encoding="utf-8"
            )
            (stale_latest / "board.txt").write_text(
                "STALE BOARD\n", encoding="utf-8"
            )
            config = {
                "season": 2026,
                "week": 0,
                "players_file": str(players_path),
                "output_dir": str(output_dir),
                "cache_dir": str(root / "cache"),
                "college_data_provider": "cfbd",
                "discover_candidates": False,
                "build_weekly_history": False,
                "auto_team_needs": False,
            }
            with (
                patch(
                    "draftscope.weekly.CFBDClient",
                    return_value=_SuccessfulRefreshClient(),
                ),
                patch(
                    "draftscope.weekly.load_nflverse_history",
                    return_value=([], {"population": "synthetic pool"}),
                ),
                patch("draftscope.weekly.ProspectEvaluator") as evaluator,
            ):
                evaluator.return_value.rank.return_value = []
                result = run_weekly_update(config, refresh_sources=False)

            state = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))
            self.assertTrue(result.board_path.is_file())
            self.assertFalse(state["current_board_available"])
            self.assertIsNone(state["latest_board"])
            self.assertIn("No current-season player rows", state["current_board_reason"])
            self.assertFalse((output_dir / "reports" / "latest" / "board.csv").exists())
            self.assertFalse((output_dir / "reports" / "latest" / "board.txt").exists())

    def test_build_failure_leaves_every_live_checkpoint_artifact_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            players_path, output_dir, config = self._fixture(root)
            original_players = players_path.read_bytes()
            original_output = _tree_contents(output_dir)

            with (
                patch(
                    "draftscope.weekly.CFBDClient",
                    return_value=_SuccessfulRefreshClient(),
                ),
                patch(
                    "draftscope.weekly.load_nflverse_history",
                    return_value=([], {"population": "synthetic pool"}),
                ),
                patch("draftscope.weekly.ProspectEvaluator") as evaluator,
                patch(
                    "draftscope.weekly.render_evaluation",
                    side_effect=RuntimeError("synthetic report failure"),
                ),
            ):
                evaluator.return_value.rank.return_value = [_Evaluation()]
                with self.assertRaisesRegex(RuntimeError, "synthetic report failure"):
                    run_weekly_update(config, refresh_sources=False)

            self.assertEqual(players_path.read_bytes(), original_players)
            self.assertEqual(_tree_contents(output_dir), original_output)
            self.assertFalse((output_dir / "snapshots" / "2026" / "week_04").exists())

    def test_external_players_file_is_rejected_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            players_path = root / "players.csv"
            output_dir = root / "data"
            write_records(
                players_path,
                [{"name": "Safety Prospect", "position": "WR", "season": 2026}],
            )
            output_dir.mkdir()
            (output_dir / "state.json").write_text('{"checkpoint": "old"}\n')
            config = {
                "season": 2026,
                "week": 4,
                "players_file": str(players_path),
                "output_dir": str(output_dir),
            }
            original_players = players_path.read_bytes()
            original_output = _tree_contents(output_dir)

            with self.assertRaisesRegex(DataError, "players_file must be inside output_dir"):
                run_weekly_update(config, refresh_sources=False)

            self.assertEqual(players_path.read_bytes(), original_players)
            self.assertEqual(_tree_contents(output_dir), original_output)

    def test_output_publication_failure_rolls_back_the_complete_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            players_path, output_dir, config = self._fixture(root)
            original_output = _tree_contents(output_dir)
            real_replace = os.replace
            failed = False

            def fail_new_output_once(source, destination):
                nonlocal failed
                if (
                    not failed
                    and Path(source).name == "next-output"
                    and Path(destination).resolve() == output_dir.resolve()
                ):
                    failed = True
                    raise OSError("synthetic publication failure")
                real_replace(source, destination)

            with (
                patch(
                    "draftscope.weekly.CFBDClient",
                    return_value=_SuccessfulRefreshClient(),
                ),
                patch(
                    "draftscope.weekly.load_nflverse_history",
                    return_value=([], {"population": "synthetic pool"}),
                ),
                patch("draftscope.weekly.ProspectEvaluator") as evaluator,
                patch(
                    "draftscope.weekly._publication_replace",
                    side_effect=fail_new_output_once,
                ),
            ):
                evaluator.return_value.rank.return_value = []
                with self.assertRaisesRegex(OSError, "synthetic publication failure"):
                    run_weekly_update(config, refresh_sources=False)

            self.assertTrue(failed)
            self.assertEqual(_tree_contents(output_dir), original_output)
            self.assertEqual(players_path.read_bytes(), original_output["players.csv"])
            self.assertFalse(_publication_journal_path(output_dir).exists())

    def test_cleanup_uses_literal_names_and_requires_a_valid_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            output_dir = root / "data[abc]*?"
            players_path = output_dir / "players.csv"
            output_dir.mkdir()
            write_records(players_path, [])
            unrelated = root / ".dataaX.draftscope-update-innocent"
            unrelated.mkdir()
            fake = root / f".{output_dir.name}.draftscope-update-fake"
            fake.mkdir()
            verified = _create_output_transaction_root(
                output_dir=output_dir,
                players_path=players_path,
            )

            _cleanup_abandoned_staging(
                output_dir=output_dir,
                players_path=players_path,
            )

            self.assertTrue(unrelated.is_dir())
            self.assertTrue(fake.is_dir())
            self.assertFalse(verified.exists())

    def test_next_run_recovers_a_crash_before_the_commit_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            players_path, output_dir, _config = self._fixture(root)
            players_path = players_path.resolve()
            output_dir = output_dir.resolve()
            original_players = players_path.read_bytes()
            original_output = _tree_contents(output_dir)
            transaction_root = _create_output_transaction_root(
                output_dir=output_dir,
                players_path=players_path,
            )
            backup_output = transaction_root / "previous-output"
            staged_output = transaction_root / "next-output"

            os.replace(output_dir, backup_output)
            output_dir.mkdir()
            (output_dir / "state.json").write_text('{"checkpoint": "new"}\n')
            journal_path = _publication_journal_path(output_dir)
            journal_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "transaction_root": str(transaction_root),
                        "output_dir": str(output_dir),
                        "players_path": str(players_path),
                        "committed": False,
                        "targets": [
                            {
                                "name": "output",
                                "final": str(output_dir),
                                "staged": str(staged_output),
                                "backup": str(backup_output),
                                "had_original": True,
                            },
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            _recover_interrupted_publication(
                output_dir=output_dir,
                players_path=players_path,
            )

            self.assertEqual(players_path.read_bytes(), original_players)
            self.assertEqual(_tree_contents(output_dir), original_output)
            self.assertFalse(journal_path.exists())
            self.assertFalse(transaction_root.exists())

    @unittest.skipIf(os.name == "nt", "symbolic-link semantics differ on Windows")
    def test_recovery_refuses_a_symlinked_journal_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            players_path, output_dir, _config = self._fixture(root)
            original_output = _tree_contents(output_dir)
            target = root / "attacker-controlled.json"
            target.write_text("{}\n", encoding="utf-8")
            journal_path = _publication_journal_path(output_dir)
            journal_path.symlink_to(target)

            with self.assertRaisesRegex(DataError, "could not be validated"):
                _recover_interrupted_publication(
                    output_dir=output_dir,
                    players_path=players_path,
                )

            self.assertTrue(journal_path.is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), "{}\n")
            self.assertEqual(_tree_contents(output_dir), original_output)

    def test_recovery_refuses_an_oversized_journal_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            players_path, output_dir, _config = self._fixture(root)
            original_output = _tree_contents(output_dir)
            journal_path = _publication_journal_path(output_dir)
            journal_path.write_bytes(b" " * (65 * 1024))

            with self.assertRaisesRegex(DataError, "could not be validated"):
                _recover_interrupted_publication(
                    output_dir=output_dir,
                    players_path=players_path,
                )

            self.assertEqual(journal_path.stat().st_size, 65 * 1024)
            self.assertEqual(_tree_contents(output_dir), original_output)

    def test_recovery_refuses_a_forged_marker_before_restoring_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            players_path, output_dir, _config = self._fixture(root)
            original_output = _tree_contents(output_dir)
            transaction_root = _create_output_transaction_root(
                output_dir=output_dir,
                players_path=players_path,
            )
            backup_output = transaction_root / "previous-output"
            staged_output = transaction_root / "next-output"
            shutil.copytree(output_dir, backup_output)
            (backup_output / "state.json").write_text('{"checkpoint": "forged"}\n')
            marker_path = transaction_root / ".draftscope-staging.json"
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker["players_path"] = str(root / "different-players.csv")
            marker_path.write_text(json.dumps(marker) + "\n", encoding="utf-8")
            journal_path = _publication_journal_path(output_dir)
            journal_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "transaction_root": str(transaction_root),
                        "output_dir": str(output_dir),
                        "players_path": str(players_path),
                        "committed": False,
                        "targets": [
                            {
                                "name": "output",
                                "final": str(output_dir),
                                "staged": str(staged_output),
                                "backup": str(backup_output),
                                "had_original": True,
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(DataError, "could not be validated"):
                _recover_interrupted_publication(
                    output_dir=output_dir,
                    players_path=players_path,
                )

            self.assertEqual(_tree_contents(output_dir), original_output)
            self.assertTrue(transaction_root.is_dir())
            self.assertTrue(journal_path.is_file())

    @unittest.skipIf(os.name == "nt", "POSIX shared-directory mode test")
    def test_recovery_refuses_transaction_paths_in_world_writable_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            players_path, output_dir, _config = self._fixture(root)
            original_output = _tree_contents(output_dir)
            transaction_root = _create_output_transaction_root(
                output_dir=output_dir,
                players_path=players_path,
            )
            backup_output = transaction_root / "previous-output"
            staged_output = transaction_root / "next-output"
            shutil.copytree(output_dir, backup_output)
            (backup_output / "state.json").write_text('{"checkpoint": "attacker"}\n')
            journal_path = _publication_journal_path(output_dir)
            journal_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "transaction_root": str(transaction_root),
                        "output_dir": str(output_dir),
                        "players_path": str(players_path),
                        "committed": False,
                        "targets": [
                            {
                                "name": "output",
                                "final": str(output_dir),
                                "staged": str(staged_output),
                                "backup": str(backup_output),
                                "had_original": True,
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            root.chmod(0o777)
            try:
                with self.assertRaisesRegex(DataError, "could not be validated"):
                    _recover_interrupted_publication(
                        output_dir=output_dir,
                        players_path=players_path,
                    )
            finally:
                root.chmod(0o700)

            self.assertEqual(_tree_contents(output_dir), original_output)
            self.assertTrue(transaction_root.is_dir())
            self.assertTrue(journal_path.is_file())


if __name__ == "__main__":
    unittest.main()

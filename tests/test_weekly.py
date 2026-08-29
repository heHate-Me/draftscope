from __future__ import annotations

from argparse import Namespace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from draftscope.analysis import ProspectEvaluator
from draftscope.cli import _lookup_command, _parser
from draftscope.model import DraftPrediction
from draftscope.records import DataError, load_records, write_records
from draftscope.tracking import TrackingError, TrackingStore, artifact_fingerprint
from draftscope.weekly import (
    _merge_discovered_player,
    _refresh_candidate_pool,
    _reuse_saved_checkpoint,
    _track_weekly_forecasts,
    run_weekly_update,
)


class MixedRefreshClient:
    def refresh_candidate(self, candidate, *, season, week):
        if candidate.get("name") == "Stale Player":
            raise DataError("temporary CFBD failure")
        updated = dict(candidate)
        updated["season"] = season
        updated["as_of_week"] = week
        return updated, {"name": candidate.get("name")}


class SuccessfulRefreshClient:
    def refresh_candidate(self, candidate, *, season, week):
        updated = dict(candidate)
        updated["season"] = season
        updated["as_of_week"] = week
        return updated, {"name": candidate.get("name")}


class FakeEvaluation:
    def __init__(self, name: str, prediction: DraftPrediction, *, profile_score: float = 60.0):
        self.player = {
            "name": name,
            "position": "WR",
            "school": "Example U",
            "as_of_week": 4,
        }
        self.profile_score = profile_score
        self.draft_prediction = prediction
        self.evidence_coverage = 0.75
        self.profile_tier = "Synthetic tier"
        self.team_fits = []

    def to_dict(self):
        return {"player": self.player}


class WeeklyTests(unittest.TestCase):
    def test_free_provider_reuses_same_week_release_gap_without_cfbd(self) -> None:
        class MissingCurrentBulk:
            def current_week(self, year, refresh=None):
                return 0

            def discover_candidates(self, **_kwargs):
                return []

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "output"
            players_path = output_dir / "players.csv"
            write_records(
                players_path,
                [
                    {
                        "name": "Saved Prospect",
                        "position": "WR",
                        "school": "Example U",
                        "season": 2026,
                        "as_of_week": 0,
                        "data_stale": False,
                    }
                ],
            )
            with (
                patch(
                    "draftscope.sportsdataverse.SportsDataverseClient",
                    return_value=MissingCurrentBulk(),
                ),
                patch(
                    "draftscope.weekly.CFBDClient",
                    side_effect=AssertionError("CFBD must remain optional"),
                ),
                patch(
                    "draftscope.weekly.load_nflverse_history",
                    return_value=([], {"population": "synthetic pool"}),
                ),
                patch("draftscope.weekly.ProspectEvaluator") as mocked_evaluator,
            ):
                mocked_evaluator.return_value.rank.return_value = []
                result = run_weekly_update(
                    {
                        "season": 2026,
                        "players_file": str(players_path),
                        "output_dir": str(output_dir),
                        "cache_dir": str(root / "cache"),
                        "college_data_provider": "sportsdataverse",
                        "cfbd_fallback": False,
                        "build_weekly_history": False,
                        "auto_team_needs": False,
                    },
                    refresh_sources=False,
                )

            row = load_records(players_path)[0]

        self.assertFalse(row["data_stale"])
        self.assertEqual(result.players_failed, 0)
        self.assertTrue(any("Reused 1 saved Week 0" in item for item in result.warnings))

    def test_invalid_college_provider_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "data"
            players = output_dir / "players.csv"
            write_records(players, [])
            with self.assertRaisesRegex(DataError, "college_data_provider"):
                run_weekly_update(
                    {
                        "season": 2026,
                        "players_file": str(players),
                        "output_dir": str(output_dir),
                        "college_data_provider": "mystery",
                    }
                )

    def test_reports_only_reuses_same_week_checkpoint_but_keeps_older_rows_stale(self) -> None:
        rows, failed, reused = _reuse_saved_checkpoint(
            [
                {
                    "name": "Saved Player",
                    "as_of_week": 4,
                    "data_stale": True,
                    "refresh_error": "rate limited",
                },
                {
                    "name": "Older Player",
                    "as_of_week": 3,
                    "data_stale": True,
                    "refresh_error": "old checkpoint",
                },
                {
                    "name": "Fresh Player",
                    "as_of_week": 4,
                    "data_stale": False,
                },
            ],
            week=4,
        )

        by_name = {row["name"]: row for row in rows}
        self.assertFalse(by_name["Saved Player"]["data_stale"])
        self.assertIsNone(by_name["Saved Player"]["refresh_error"])
        self.assertTrue(by_name["Older Player"]["data_stale"])
        self.assertIn("not requested Week 4", by_name["Older Player"]["refresh_error"])
        self.assertFalse(by_name["Fresh Player"]["data_stale"])
        self.assertEqual(failed, 1)
        self.assertEqual(reused, 2)

    def test_release_gap_withholds_healthy_row_from_an_older_week(self) -> None:
        rows, failed, reused = _reuse_saved_checkpoint(
            [
                {
                    "name": "Older Healthy Player",
                    "as_of_week": 0,
                    "data_stale": False,
                }
            ],
            week=1,
        )

        self.assertEqual(reused, 0)
        self.assertEqual(failed, 1)
        self.assertTrue(rows[0]["data_stale"])
        self.assertIn("not requested Week 1", rows[0]["refresh_error"])

    def test_failed_refresh_retains_old_week_and_stale_player_is_not_ranked(self) -> None:
        tracked = [
            {
                "name": "Stale Player",
                "position": "WR",
                "school": "Example U",
                "season": 2026,
                "as_of_week": 2,
                "prod_receiving_yards": 240,
            },
            {
                "name": "Fresh Player",
                "position": "WR",
                "school": "Example U",
                "season": 2026,
                "as_of_week": 2,
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            raw_dir = Path(temporary)
            refreshed, failed, warnings = _refresh_candidate_pool(
                MixedRefreshClient(),
                tracked,
                discovered=None,
                season=2026,
                week=4,
                raw_dir=raw_dir,
            )

        by_name = {row["name"]: row for row in refreshed}
        stale = by_name["Stale Player"]
        self.assertEqual(stale["as_of_week"], 2)
        self.assertEqual(stale["prod_receiving_yards"], 240)
        self.assertTrue(stale["data_stale"])
        self.assertEqual(failed, 1)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(by_name["Fresh Player"]["as_of_week"], 4)

        evaluator = ProspectEvaluator([], refreshed)
        evaluation = SimpleNamespace(
            draft_prediction=DraftPrediction(
                available=True,
                probability=0.5,
                conditional_probability=0.5,
            ),
            profile_score=50.0,
        )
        with patch.object(evaluator, "evaluate", return_value=evaluation) as evaluate:
            ranked = evaluator.rank()
        self.assertEqual(ranked, [evaluation])
        evaluate.assert_called_once_with("Fresh Player", school="Example U", bootstrap=0)

    def test_discovery_merge_preserves_verified_measurements_traits_and_entry_inputs(self) -> None:
        existing = {
            "player_id": "local-player",
            "cfbd_player_id": "123",
            "name": "Tracked Prospect",
            "position": "WR",
            "school": "Example U",
            "as_of_week": 3,
            "height_in": 74,
            "weight_lb": 205,
            "forty_s": 4.39,
            "measurement_source": "verified_workout",
            "measurement_date": "2026-08-01",
            "measurements_verified": True,
            "trait_route_running": 88,
            "draft_entry_probability": 0.65,
            "prod_receiving_yards": 900,
            "prod_yards_per_route": 3.1,
        }
        discovered = {
            "player_id": "123",
            "cfbd_player_id": "123",
            "name": "Tracked Prospect",
            "position": "WR",
            "school": "Example U",
            "season": 2026,
            "as_of_week": 4,
            "height_in": 72,
            "weight_lb": 190,
            "measurement_source": "school_roster",
            "measurements_verified": False,
            "prod_receiving_yards": 410,
        }

        merged = _merge_discovered_player(discovered, existing, week=4)

        self.assertEqual(merged["player_id"], "local-player")
        self.assertEqual(merged["height_in"], 74)
        self.assertEqual(merged["weight_lb"], 205)
        self.assertEqual(merged["forty_s"], 4.39)
        self.assertEqual(merged["measurement_source"], "verified_workout")
        self.assertTrue(merged["measurements_verified"])
        self.assertEqual(merged["trait_route_running"], 88)
        self.assertEqual(merged["draft_entry_probability"], 0.65)
        self.assertEqual(merged["prod_receiving_yards"], 410)
        self.assertIsNone(merged.get("prod_yards_per_route"))
        self.assertEqual(merged["as_of_week"], 4)

    def test_board_keeps_probability_scopes_separate_and_only_compares_like_scope(self) -> None:
        predictions = {
            "Actual Player": DraftPrediction(
                available=True,
                probability=0.30,
                conditional_probability=0.60,
                entry_probability=0.50,
                probability_scope="P(drafted next draft)",
                projected_pick=None,
            ),
            "Conditional Player": DraftPrediction(
                available=True,
                probability=None,
                conditional_probability=0.65,
                conditional_scope="synthetic conditional risk set",
                entry_probability=None,
                probability_scope="P(drafted | enters next NFL draft)",
                projected_pick=None,
            ),
            "Changed Scope Player": DraftPrediction(
                available=True,
                probability=None,
                conditional_probability=0.70,
                conditional_scope="synthetic conditional risk set",
                entry_probability=None,
                probability_scope="P(drafted | enters next NFL draft)",
                projected_pick=None,
            ),
        }
        evaluations = [FakeEvaluation(name, prediction) for name, prediction in predictions.items()]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "output"
            players_path = output_dir / "players.csv"
            write_records(
                players_path,
                [
                    {"name": name, "position": "WR", "school": "Example U", "season": 2026}
                    for name in predictions
                ],
            )
            previous_board = root / "previous_board.csv"
            write_records(
                previous_board,
                [
                    {
                        "rank": 1,
                        "name": "Actual Player",
                        "school": "Example U",
                        "profile_score": 55,
                        "ranking_probability": 0.25,
                        "ranking_probability_scope": "next_draft",
                    },
                    {
                        "rank": 2,
                        "name": "Conditional Player",
                        "school": "Example U",
                        "profile_score": 55,
                        "ranking_probability": 0.55,
                        "ranking_probability_scope": "synthetic conditional risk set",
                    },
                    {
                        "rank": 3,
                        "name": "Changed Scope Player",
                        "school": "Example U",
                        "profile_score": 55,
                        "ranking_probability": 0.20,
                        "ranking_probability_scope": "next_draft",
                    },
                ],
            )
            output_dir.mkdir(exist_ok=True)
            (output_dir / "state.json").write_text(
                json.dumps({"latest_board": str(previous_board)}), encoding="utf-8"
            )
            evaluator_class = patch("draftscope.weekly.ProspectEvaluator")
            with (
                patch("draftscope.weekly.CFBDClient", return_value=SuccessfulRefreshClient()),
                patch(
                    "draftscope.weekly.load_nflverse_history",
                    return_value=([], {"population": "synthetic pool"}),
                ),
                patch("draftscope.weekly.render_evaluation", return_value="synthetic report\n"),
                evaluator_class as mocked_evaluator,
            ):
                mocked_evaluator.return_value.rank.return_value = evaluations
                result = run_weekly_update(
                    {
                        "season": 2026,
                        "week": 4,
                        "players_file": str(players_path),
                        "output_dir": str(output_dir),
                        "cache_dir": str(root / "cache"),
                        "discover_candidates": False,
                        "build_weekly_history": False,
                        "auto_team_needs": False,
                    },
                    refresh_sources=False,
                )

            board = {row["name"]: row for row in load_records(result.board_path)}
            actual = board["Actual Player"]
            self.assertAlmostEqual(float(actual["draft_probability"]), 0.30)
            self.assertAlmostEqual(float(actual["conditional_probability"]), 0.60)
            self.assertAlmostEqual(float(actual["entry_probability"]), 0.50)
            self.assertEqual(actual["ranking_probability_scope"], "next_draft")
            self.assertAlmostEqual(float(actual["draft_probability_delta"]), 0.05)

            conditional = board["Conditional Player"]
            self.assertIsNone(conditional["draft_probability"])
            self.assertAlmostEqual(float(conditional["conditional_probability"]), 0.65)
            self.assertEqual(
                conditional["ranking_probability_scope"], "synthetic conditional risk set"
            )
            self.assertEqual(
                conditional["conditional_scope"], "synthetic conditional risk set"
            )
            self.assertAlmostEqual(float(conditional["draft_probability_delta"]), 0.10)

            changed = board["Changed Scope Player"]
            self.assertIsNone(changed["draft_probability_delta"])
            board_text = (result.snapshot_dir / "board.txt").read_text(encoding="utf-8")
            self.assertIn("Next draft", board_text)
            self.assertIn("Model P", board_text)

    def test_lookup_failure_preserves_existing_checkpoint_and_marks_row_stale(self) -> None:
        class FailedLookupClient:
            def player_record(self, name, *, year, team=None):
                return {
                    "cfbd_player_id": "123",
                    "player_id": "123",
                    "name": name,
                    "position": "WR",
                    "school": team,
                    "season": year,
                }

            def refresh_candidate(self, candidate, *, season, week):
                raise DataError("bounded stats unavailable")

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "players.csv"
            write_records(
                destination,
                [
                    {
                        "cfbd_player_id": "123",
                        "name": "Lookup Player",
                        "position": "WR",
                        "school": "Example U",
                        "season": 2026,
                        "as_of_week": 2,
                        "prod_receiving_yards": 250,
                    }
                ],
            )
            args = Namespace(
                name="Lookup Player",
                season=2026,
                team="Example U",
                out=str(destination),
                week=4,
            )
            with (
                patch("draftscope.cli.CFBDClient", return_value=FailedLookupClient()),
                patch("builtins.print"),
            ):
                self.assertEqual(_lookup_command(args), 0)
            row = load_records(destination)[0]

        self.assertEqual(row["as_of_week"], 2)
        self.assertEqual(row["prod_receiving_yards"], 250)
        self.assertTrue(row["data_stale"])
        self.assertIn("bounded stats unavailable", row["refresh_error"])

    def test_zero_discovery_limit_keeps_the_full_national_pool(self) -> None:
        discovered = [
            {
                "player_id": str(index),
                "cfbd_player_id": str(index),
                "name": f"Prospect {index}",
                "position": "WR",
                "school": "Example U",
                "season": 2026,
                "as_of_week": 4,
                "prod_receiving_yards": 500 + index,
            }
            for index in range(55)
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "output"
            players_path = output_dir / "players.csv"
            write_records(players_path, [])
            with (
                patch("draftscope.weekly.CFBDClient", return_value=object()),
                patch("draftscope.weekly.discover_cfbd_candidates", return_value=discovered),
                patch(
                    "draftscope.weekly.load_nflverse_history",
                    return_value=([], {"population": "synthetic pool"}),
                ),
                patch("draftscope.weekly.ProspectEvaluator") as mocked_evaluator,
            ):
                mocked_evaluator.return_value.rank.return_value = []
                run_weekly_update(
                    {
                        "season": 2026,
                        "week": 4,
                        "players_file": str(players_path),
                        "output_dir": str(output_dir),
                        "cache_dir": str(root / "cache"),
                        "college_data_provider": "cfbd",
                        "discover_candidates": True,
                        "discovery_per_position": 0,
                        "build_weekly_history": False,
                        "auto_team_needs": False,
                    },
                    refresh_sources=False,
                )
            rows = load_records(players_path)

        self.assertEqual(len(rows), 55)

    def test_cli_parser_exposes_discover(self) -> None:
        args = _parser().parse_args(
            [
                "discover",
                "--season",
                "2026",
                "--week",
                "4",
                "--per-position",
                "25",
                "--raw-dir",
                "raw",
                "--out",
                "players.csv",
            ]
        )
        self.assertEqual(args.command, "discover")
        self.assertEqual(args.season, 2026)
        self.assertEqual(args.week, 4)
        self.assertEqual(args.per_position, 25)
        self.assertEqual(args.raw_dir, "raw")
        self.assertEqual(args.out, "players.csv")

    def test_weekly_tracking_registers_release_snapshots_input_and_is_idempotent(self) -> None:
        contract_id = "college-contract"
        validation = SimpleNamespace(
            passes_quality_gate=True,
            validation_scheme=(
                "expanding-window past-only by draft year; 2-year warmup; "
                "prequential calibration"
            ),
            rows=120,
            positives=30,
            years=(2020, 2021, 2022, 2023),
            evaluated_years=(2022, 2023),
            per_year=(),
            brier=0.08,
            baseline_brier=0.12,
            log_loss=0.31,
            average_precision=0.44,
            roc_auc=0.79,
            expected_calibration_error=0.03,
            board_scope="within-position per year",
            board_metrics=({"cutoff": 10, "precision": 0.60},),
            simple_baselines=(
                {
                    "name": "production_only",
                    "available": True,
                    "rows": 120,
                    "brier": 0.09,
                },
            ),
            quality_message="passed",
        )
        raw_model = SimpleNamespace(
            feature_names=("height_in", "weight_lb"),
            l2=0.12,
            learning_rate=0.035,
            medians={"height_in": 72.0, "weight_lb": 195.0},
            means={"height_in": 72.0, "weight_lb": 195.0},
            scales={"height_in": 2.0, "weight_lb": 15.0},
            missing_features=(),
            weights=[-1.0, 0.2, 0.3],
        )
        model_rows = [
            {
                "name": f"History {index}",
                "position": "WR",
                "draft_year": 2020 + index,
                "drafted": index % 2 == 0,
            }
            for index in range(4)
        ]
        model = SimpleNamespace(
            validation=validation,
            contract=SimpleNamespace(contract_id=contract_id),
            position="WR",
            model_stage="college_precombine",
            probability_kind="unconditional_next_draft",
            population="FBS roster checkpoint",
            features=("height_in", "weight_lb"),
            rows=model_rows,
            raw_model=raw_model,
            calibrator=None,
            _row_transformer=None,
        )
        prediction = DraftPrediction(
            available=True,
            model_stage="college_precombine",
            probability_kind="unconditional_next_draft",
            contract_id=contract_id,
            probability=0.42,
            conditional_probability=0.42,
            probability_scope="P(drafted next draft)",
        )
        evaluation = FakeEvaluation("Tracked Prospect", prediction)
        evaluation.player["cfbd_player_id"] = "12345"
        evaluation.college_prediction = prediction
        evaluation.combine_prediction = None
        evaluator = SimpleNamespace(_model_cache={"wr": model})

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "data"
            players_path = root / "players.csv"
            history_path = root / "college_history.csv"
            write_records(players_path, [evaluation.player])
            write_records(history_path, model_rows)
            arguments = {
                "output_dir": output_dir,
                "evaluator": evaluator,
                "evaluations": [evaluation],
                "season": 2026,
                "week": 4,
                "players_path": players_path,
                "model_history": model_rows,
                "model_metadata": {
                    "schema_version": "1.1",
                    "nested": {"path": str(root / "private" / "model.json")},
                },
                "model_history_path": history_path,
                "benchmark_history": [],
                "benchmark_metadata": {
                    "source": "test",
                    "nested": {"path": str(root / "private" / "benchmark.json")},
                },
                "team_profiles": [],
            }
            first = _track_weekly_forecasts(**arguments)
            second = _track_weekly_forecasts(**arguments)
            store = TrackingStore(output_dir)
            release = store.get_model_release(first.model_release_ids[0])
            artifact_path = Path(release.content["training_artifact"]["path"])
            self.assertFalse(artifact_path.is_absolute())
            immutable_history = output_dir / artifact_path
            immutable_fingerprint = artifact_fingerprint(immutable_history)

            write_records(history_path, [{"name": "replacement"}])

            self.assertEqual(first.forecast_run_id, second.forecast_run_id)
            self.assertEqual(prediction.model_release_id, first.model_release_ids[0])
            self.assertEqual(store.status()["champion_count"], 1)
            self.assertEqual(store.status()["counts"]["forecast_runs"], 1)
            self.assertEqual(
                release.content["validation"]["validation_schema_version"],
                2,
            )
            self.assertEqual(
                release.content["validation"]["expected_calibration_error"],
                0.03,
            )
            self.assertEqual(
                release.content["validation"]["board_metrics"][0]["cutoff"],
                10,
            )
            self.assertEqual(
                release.content["validation"]["simple_baselines"][0]["name"],
                "production_only",
            )
            self.assertIn("model_history/checkpoints", str(immutable_history))
            self.assertEqual(
                artifact_fingerprint(immutable_history)["sha256"],
                immutable_fingerprint["sha256"],
            )
            for generated in output_dir.rglob("*.json"):
                self.assertNotIn(str(root), generated.read_text(encoding="utf-8"))

    def test_withheld_model_is_registered_for_audit_but_not_promoted(self) -> None:
        validation = SimpleNamespace(
            passes_quality_gate=False,
            validation_scheme=(
                "expanding-window past-only by draft year; 2-year warmup; "
                "prequential calibration"
            ),
            rows=120,
            positives=12,
            years=(2020, 2021, 2022, 2023),
            evaluated_years=(2022, 2023),
            per_year=(),
            brier=0.12,
            baseline_brier=0.11,
            log_loss=0.40,
            average_precision=0.20,
            roc_auc=0.60,
            quality_message="withheld for insufficient support",
        )
        model = SimpleNamespace(
            validation=validation,
            contract=SimpleNamespace(contract_id="withheld"),
            position="WR",
            model_stage="college_precombine",
            probability_kind="unconditional_next_draft",
            population="FBS roster checkpoint",
            features=("height_in", "weight_lb"),
            rows=[{"position": "WR", "drafted": True}],
            raw_model=None,
            calibrator=None,
            _row_transformer=None,
        )
        evaluator = SimpleNamespace(_model_cache={"wr": model})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            players_path = root / "players.csv"
            write_records(players_path, [])
            result = _track_weekly_forecasts(
                output_dir=root / "data",
                evaluator=evaluator,
                evaluations=[],
                season=2026,
                week=4,
                players_path=players_path,
                model_history=[],
                model_metadata={},
                model_history_path=None,
                benchmark_history=[],
                benchmark_metadata={},
                team_profiles=[],
            )
            status = TrackingStore(root / "data").status()

        self.assertIsNone(result.forecast_run_id)
        self.assertEqual(len(result.model_release_ids), 1)
        self.assertEqual(status["counts"]["model_releases"], 1)
        self.assertEqual(status["champion_count"], 0)
        self.assertEqual(
            status["latest_validation_report"]["withheld_models"][0]["position"],
            "WR",
        )

    def test_tracking_failure_warns_without_suppressing_weekly_board(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "data"
            players_path = output_dir / "players.csv"
            write_records(
                players_path,
                [
                    {
                        "name": "Tracked Prospect",
                        "position": "WR",
                        "school": "Example U",
                        "season": 2026,
                    }
                ],
            )
            with (
                patch("draftscope.weekly.CFBDClient", return_value=SuccessfulRefreshClient()),
                patch(
                    "draftscope.weekly.load_nflverse_history",
                    return_value=([], {"population": "synthetic pool"}),
                ),
                patch("draftscope.weekly.ProspectEvaluator") as mocked_evaluator,
                patch(
                    "draftscope.weekly._track_weekly_forecasts",
                    side_effect=TrackingError("synthetic integrity failure"),
                ),
            ):
                mocked_evaluator.return_value.rank.return_value = []
                result = run_weekly_update(
                    {
                        "season": 2026,
                        "week": 4,
                        "players_file": str(players_path),
                        "output_dir": str(output_dir),
                        "cache_dir": str(root / "cache"),
                        "discover_candidates": False,
                        "build_weekly_history": False,
                        "auto_team_needs": False,
                    },
                    refresh_sources=False,
                )
            state = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))
            board_exists = result.board_path.is_file()

        self.assertTrue(board_exists)
        self.assertIsNone(result.forecast_run_id)
        self.assertIsNone(state["latest_forecast_run_id"])
        self.assertTrue(any("tracking failed" in warning.lower() for warning in result.warnings))

    def test_cli_parser_exposes_model_status(self) -> None:
        args = _parser().parse_args(
            ["model-status", "--output-dir", "tracked-data", "--json"]
        )
        self.assertEqual(args.command, "model-status")
        self.assertEqual(args.output_dir, "tracked-data")
        self.assertTrue(args.json)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from draftscope.records import DataError


_SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "replay_retrospective_case.py"
_SPEC = importlib.util.spec_from_file_location("replay_retrospective_case", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
replay_tool = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = replay_tool
_SPEC.loader.exec_module(replay_tool)


def _history() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for draft_year in range(2019, 2027):
        for index in range(80):
            talent = 4.0 - index / 12.0
            drafted = index < 12
            carries = 220.0 + talent * 18.0
            yards_per_carry = 5.2 + talent * 0.25
            rows.append(
                {
                    "player_id": f"rb-{draft_year}-{index}",
                    "name": f"Replay Player {draft_year} {index}",
                    "position": "RB",
                    "school": "Replay University",
                    "conference": "Replay Conference",
                    "season": draft_year - 1,
                    "draft_year": draft_year,
                    "checkpoint": "through_week_16",
                    "as_of_week": 16,
                    "feature_cutoff_season": draft_year - 1,
                    "model_stage": "college_precombine",
                    "probability_kind": "unconditional_next_draft",
                    "population": "FBS roster through completed Week 16",
                    "drafted": drafted,
                    "draft_round": 1 if drafted else None,
                    "draft_ovr": index + 1 if drafted else None,
                    "height_in": 69.0 + talent * 0.4,
                    "weight_lb": 195.0 + talent * 4.0,
                    "age_at_draft": 21.0 + (index % 4) * 0.25,
                    "class_year": 3 if drafted else 2,
                    "has_recorded_stats": True,
                    "prod_carries": carries,
                    "prod_rush_yards": carries * yards_per_carry,
                    "prod_scrimmage_yards": carries * yards_per_carry + 120.0,
                    "prod_touchdowns": max(1.0, 12.0 + talent * 3.0),
                    "prod_yards_per_carry": yards_per_carry,
                    "prod_yards_per_touch": yards_per_carry - 0.05,
                }
            )
    return rows


def _args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "player": "Replay Player 2026 0",
        "school": "Replay University",
        "position": "RB",
        "holdout_year": 2026,
        "week": 16,
        "training_draft_years": 7,
        "history": None,
        "cache_dir": ".draftscope-cache",
        "refresh": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class RetrospectiveReplayToolTests(unittest.TestCase):
    def test_row_audit_hash_ignores_manual_film_grades_and_provenance(self) -> None:
        baseline = _history()
        changed = [dict(row) for row in baseline]
        changed[0].update(
            {
                "trait_vision": 99,
                "film_grade_status": "complete",
                "film_grader": "Synthetic Grader",
                "film_graded_at": "2026-09-01",
                "film_future_confidence": 0.99,
            }
        )

        self.assertEqual(
            replay_tool._canonical_rows_sha256(baseline),
            replay_tool._canonical_rows_sha256(changed),
        )

    def test_fresh_custom_cache_is_passed_to_history_builder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "fresh-cache"
            args = _args(cache_dir=str(cache))
            with mock.patch.object(
                replay_tool,
                "SportsDataverseClient",
            ) as college_client, mock.patch.object(
                replay_tool,
                "SportsDataverseHistoricalClient",
            ) as historical_client, mock.patch.object(
                replay_tool,
                "build_historical_college_training_data",
                return_value=([], {}),
            ) as builder:
                replay_tool._load_or_build_history(args)

            college_client.assert_called_once_with(
                cache.resolve(),
                nflverse_cache_dir=cache.resolve(),
                refresh=False,
            )
            historical_client.assert_called_once_with(
                college_client.return_value,
                nflverse_cache_dir=cache.resolve(),
                refresh=False,
            )
            self.assertEqual(builder.call_args.kwargs["cache_dir"], cache.resolve())
            self.assertNotIn("nflverse_draft_path", builder.call_args.kwargs)

    def test_forecast_is_hashed_before_holdout_labels_are_accessed(self) -> None:
        gate = {"forecast_hashed": False}

        class GuardedHoldout(dict[str, object]):
            def get(self, key, default=None):
                if key in {"drafted", "draft_round", "draft_ovr"} and not gate[
                    "forecast_hashed"
                ]:
                    raise AssertionError(f"Outcome field accessed before forecast hash: {key}")
                return super().get(key, default)

        rows = _history()
        guarded_rows = [
            GuardedHoldout(row) if row["draft_year"] == 2026 else row
            for row in rows
        ]
        metadata = {
            "as_of_week": 16,
            "row_population": "FBS roster through completed Week 16",
            "outcome_blind_alias_draft_years": [2026],
            "quality_gate_status": "pass",
            "positive_match_coverage": 1.0,
            "source_artifacts": [],
        }
        original_hash = replay_tool._canonical_json_sha256

        def hash_then_reveal(value):
            digest = original_hash(value)
            gate["forecast_hashed"] = True
            return digest

        with mock.patch.object(
            replay_tool,
            "_load_or_build_history",
            return_value=(guarded_rows, metadata),
        ), mock.patch.object(
            replay_tool,
            "_canonical_json_sha256",
            side_effect=hash_then_reveal,
        ):
            result = replay_tool.build_case_result(_args())

        self.assertTrue(gate["forecast_hashed"])
        self.assertFalse(result["forecast_artifact"]["outcomes_in_payload"])
        self.assertEqual(
            result["model_before_holdout"]["training_draft_years"],
            list(range(2019, 2026)),
        )
        self.assertEqual(result["prospect"]["within_position_rank"], 1)
        self.assertTrue(result["outcome_reveal"]["drafted"])

    def test_replay_rejects_history_without_outcome_blind_alias_audit(self) -> None:
        metadata = {
            "as_of_week": 16,
            "row_population": "FBS roster through completed Week 16",
        }
        with mock.patch.object(
            replay_tool,
            "_load_or_build_history",
            return_value=(_history(), metadata),
        ):
            with self.assertRaisesRegex(DataError, "outcome-blind holdout"):
                replay_tool.build_case_result(_args())


if __name__ == "__main__":
    unittest.main()

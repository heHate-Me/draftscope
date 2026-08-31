from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import random
import tempfile
import unittest

from draftscope.model import (
    DECISION_THRESHOLD,
    CollegeDraftProbabilityModel,
    DraftProbabilityModel,
    ModelError,
    college_candidate_features,
)
from draftscope.records import load_records, write_records


def synthetic_history(position: str = "WR", rows_per_year: int = 36) -> list[dict[str, object]]:
    rng = random.Random(20260825)
    rows: list[dict[str, object]] = []
    pick = 1
    for year in range(2022, 2027):
        for index in range(rows_per_year):
            talent = rng.gauss(0.0, 1.0)
            drafted = talent + rng.gauss(0.0, 0.65) > -0.05
            forty = 4.56 - talent * 0.09 + rng.gauss(0.0, 0.035)
            vertical = 34.0 + talent * 2.8 + rng.gauss(0.0, 1.2)
            production = 1.8 + talent * 0.45 + rng.gauss(0.0, 0.18)
            trait = 60.0 + talent * 9.0 + rng.gauss(0.0, 3.0)
            row: dict[str, object] = {
                "player_id": f"{year}-{index}",
                "name": f"Player {year} {index}",
                "position": position,
                "draft_year": year,
                "drafted": drafted,
                "draft_ovr": pick if drafted else None,
                "draft_round": min(7, (pick - 1) // 32 + 1) if drafted else None,
                "elite": drafted and pick <= 64,
                "height_in": 73 + rng.gauss(0, 1.2),
                "weight_lb": 198 + rng.gauss(0, 8),
                "forty_s": forty,
                "vertical_in": vertical,
                "broad_jump_in": 120 + talent * 4 + rng.gauss(0, 2),
                "three_cone_s": 7.05 - talent * 0.09 + rng.gauss(0, 0.05),
                "shuttle_s": 4.30 - talent * 0.06 + rng.gauss(0, 0.04),
                "prod_yards_per_route": production,
                "trait_route_running": trait,
                # This field intentionally exists only for positives and must be rejected.
                "age_at_draft": 22.0 if drafted else None,
                "population": "synthetic eligible pool",
            }
            rows.append(row)
            if drafted:
                pick += 1
    return rows


def unlearnable_history(position: str = "TE") -> list[dict[str, object]]:
    """Balanced labels with exactly identical feature distributions."""

    rows: list[dict[str, object]] = []
    for year in range(2022, 2027):
        for pair in range(18):
            for drafted in (False, True):
                rows.append(
                    {
                        "player_id": f"{year}-{pair}-{int(drafted)}",
                        "name": f"Noise Player {year} {pair} {int(drafted)}",
                        "position": position,
                        "draft_year": year,
                        "drafted": drafted,
                        "feature_a": float(pair),
                        "feature_b": float((pair * 7) % 19),
                        "population": "synthetic noise pool",
                    }
                )
    return rows


def synthetic_college_roster_history() -> list[dict[str, object]]:
    """Low-base-rate roster history with valid production on fewer than 25% of rows."""

    rng = random.Random(9907)
    rows: list[dict[str, object]] = []
    for draft_year in range(2022, 2027):
        talents = [rng.gauss(0.0, 1.0) for _ in range(160)]
        draft_cutoff = sorted(talents, reverse=True)[11]
        for index, talent in enumerate(talents):
            drafted = talent >= draft_cutoff
            has_stats = talent > 0.9 or drafted
            rows.append(
                {
                    "player_id": f"college-{draft_year}-{index}",
                    "name": f"College Player {draft_year} {index}",
                    "position": "WR",
                    "school": "Same University",
                    "conference": "Same Conference",
                    "season": draft_year - 1,
                    "draft_year": draft_year,
                    "as_of_week": 0,
                    "model_stage": "college_precombine",
                    "probability_kind": "unconditional_next_draft",
                    "population": "FBS roster preseason; prior-season stats",
                    "drafted": drafted,
                    "height_in": 73.0 + talent * 0.3 + rng.gauss(0.0, 0.3),
                    "weight_lb": 195.0 + talent * 3.0 + rng.gauss(0.0, 2.0),
                    "class_year": 4 if drafted else rng.choice((1, 2, 3, 4)),
                    "has_recorded_stats": has_stats,
                    "prod_receptions": 35.0 + talent * 12.0 + rng.gauss(0.0, 3.0) if has_stats else None,
                    "prod_receiving_yards": 500.0 + talent * 220.0 + rng.gauss(0.0, 50.0) if has_stats else None,
                    "prod_receiving_tds": 4.0 + talent * 2.0 + rng.gauss(0.0, 1.0) if has_stats else None,
                }
            )
    return rows


class RecordingDraftProbabilityModel(DraftProbabilityModel):
    def __init__(self, *args, **kwargs):
        self.feature_selection_years: list[frozenset[int]] = []
        super().__init__(*args, **kwargs)

    def _select_features(self, rows, labels):
        years = frozenset(int(row["draft_year"]) for row in rows)
        self.feature_selection_years.append(years)
        return super()._select_features(rows, labels)


class RecordingCollegeDraftProbabilityModel(CollegeDraftProbabilityModel):
    def __init__(self, *args, **kwargs):
        self.fold_missingness_features: list[tuple[str, ...]] = []
        super().__init__(*args, **kwargs)

    def _fit_probability_model(self, rows, labels, features, *, max_iter):
        model = super()._fit_probability_model(
            rows,
            labels,
            features,
            max_iter=max_iter,
        )
        self.fold_missingness_features.append(model.missing_features)
        return model


class ModelTests(unittest.TestCase):
    def test_college_feature_contract_includes_stable_recruiting_pedigree(self) -> None:
        features = college_candidate_features("WR")
        self.assertIn("recruit_rating", features)
        self.assertIn("recruit_stars", features)
        self.assertIn("recruit_national_rank", features)

    def test_college_validation_is_identical_after_csv_round_trip(self) -> None:
        rows = synthetic_college_roster_history()
        for row in rows:
            row["position"] = "K"
            has_stats = bool(row["has_recorded_stats"])
            row["prod_field_goal_attempts"] = 22.0 if has_stats else None
            row["prod_field_goals_made"] = (
                16.0 + float(row["prod_receiving_tds"] or 0.0) / 2.0
                if has_stats
                else None
            )
        fresh = CollegeDraftProbabilityModel(
            rows, position="K", population="FBS roster preseason; prior-season stats"
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "college_history.csv"
            write_records(path, rows)
            reloaded = CollegeDraftProbabilityModel(
                load_records(path),
                position="K",
                population="FBS roster preseason; prior-season stats",
            )

        self.assertEqual(fresh.features, reloaded.features)
        self.assertEqual(fresh.raw_model.weights, reloaded.raw_model.weights)
        self.assertEqual(asdict(fresh.validation), asdict(reloaded.validation))

    def test_college_model_audits_source_era_gaps_without_learning_missingness(self) -> None:
        rows = synthetic_college_roster_history()
        for row in rows:
            if row["draft_year"] in {2022, 2023} and row["drafted"]:
                row["weight_lb"] = None
                row["class_year"] = None

        model = RecordingCollegeDraftProbabilityModel(
            rows,
            position="WR",
            population="FBS roster preseason; prior-season stats",
        )

        self.assertIn("has_recorded_stats", model.features)
        self.assertIn("weight_lb", model.features)
        self.assertIn("class_year_numeric", model.features)
        self.assertTrue(model.validation.passes_quality_gate)
        self.assertTrue(model.fold_missingness_features)
        self.assertTrue(all(not features for features in model.fold_missingness_features))
        self.assertIn(
            "implicit missing-value indicators are disabled",
            model.validation.missingness_policy.lower(),
        )
        audit = " ".join(model.validation.missingness_audit_warnings)
        self.assertIn("weight_lb", audit)
        self.assertIn("class_year_numeric", audit)
        self.assertIn("2022", audit)
        self.assertIn("2023", audit)

        prediction = model.predict(
            {
                "position": "WR",
                "school": "Same University",
                "conference": "Same Conference",
                "height_in": 74,
                "weight_lb": 205,
                "class_year": "Senior",
                "has_recorded_stats": True,
                "prod_receptions": 70,
                "prod_receiving_yards": 1_300,
                "prod_receiving_tds": 12,
            },
            bootstrap=0,
        )
        self.assertTrue(
            any("source-availability audit" in warning.lower() for warning in prediction.warnings)
        )

    def test_college_offensive_line_model_pools_generic_ol_and_explicit_tackle_rows(self) -> None:
        rows = synthetic_college_roster_history()
        for index, row in enumerate(rows):
            row["position"] = "OT" if index % 20 == 0 else "IOL"
        model = CollegeDraftProbabilityModel(
            rows,
            position="OT",
            population="FBS roster preseason; prior-season stats",
        )
        self.assertEqual(model.source_position_pool, ("OT", "IOL"))
        self.assertEqual(model.validation.rows, 3 * 160)
        self.assertEqual(model.validation.evaluated_years, (2024, 2025, 2026))
        self.assertIn("past-only", model.validation.validation_scheme)
        prediction = model.predict(
            {
                "position": "OT",
                "school": "Same University",
                "conference": "Same Conference",
                "height_in": 78,
                "weight_lb": 320,
                "class_year": 4,
            },
            bootstrap=0,
        )
        self.assertTrue(any("shared offensive-line cohort" in warning for warning in prediction.warnings))

    def test_college_model_keeps_sparse_checkpoint_production_and_publishes_only_after_grouped_qa(self) -> None:
        rows = synthetic_college_roster_history()
        csv_shaped_rows = [
            {
                **row,
                "drafted": str(row["drafted"]),
                "draft_year": str(row["draft_year"]),
                "has_recorded_stats": str(row["has_recorded_stats"]),
            }
            for row in rows
        ]
        model = CollegeDraftProbabilityModel(
            csv_shaped_rows,
            position="WR",
            population="FBS roster preseason; prior-season stats",
        )
        self.assertTrue(model.validation.passes_quality_gate)
        self.assertEqual(model.validation.years, (2022, 2023, 2024, 2025, 2026))
        self.assertIn("prod_receiving_yards", model.features)
        self.assertLess(
            sum(row["prod_receiving_yards"] is not None for row in rows) / len(rows),
            0.25,
        )
        strong = model.predict(
            {
                "position": "WR",
                "school": "Same University",
                "conference": "Same Conference",
                "height_in": 74,
                "weight_lb": 205,
                "class_year": 4,
                "prod_receptions": 70,
                "prod_receiving_yards": 1_300,
                "prod_receiving_tds": 12,
            },
            bootstrap=0,
        )
        ordinary = model.predict(
            {
                "position": "WR",
                "school": "Same University",
                "conference": "Same Conference",
                "height_in": 72,
                "weight_lb": 190,
                "class_year": 3,
                "prod_receptions": 30,
                "prod_receiving_yards": 420,
                "prod_receiving_tds": 3,
            },
            bootstrap=0,
        )
        self.assertTrue(strong.available)
        self.assertTrue(ordinary.available)
        self.assertEqual(strong.model_stage, "college_precombine")
        self.assertEqual(strong.probability_kind, "unconditional_next_draft")
        self.assertGreater(strong.probability or 0.0, ordinary.probability or 1.0)
        prepared = model._prepare_external_row(
            {
                "position": "WR",
                "school": "Same University",
                "conference": "Same Conference",
                "height_in": 73,
                "weight_lb": 195,
                "class_year": 3,
                "prod_receptions": 45,
                "prod_receiving_yards": 700,
                "prod_receiving_tds": 6,
            },
            model._row_transformer,
        )
        baseline_ood = model._ood_score(prepared)
        prepared["context_school_draft_rate"] = 1.0
        self.assertAlmostEqual(model._ood_score(prepared) or 0.0, baseline_ood or 0.0)

    def test_retrospective_holdout_is_past_only_and_outcome_blind(self) -> None:
        class OutcomeGuard(dict[str, object]):
            forbidden = {
                "drafted",
                "draft_round",
                "draft_ovr",
                "elite",
                "nfl_team",
                "reference_only",
                "nfl_hof",
                "nfl_all_pro_selections",
                "nfl_pro_bowls",
                "active_roster_status",
            }

            def get(self, key, default=None):
                if key in self.forbidden or str(key).startswith("outcome_"):
                    raise AssertionError(f"Retrospective scorer read prohibited field {key}")
                return super().get(key, default)

        rows = synthetic_college_roster_history()
        prior_rows = [row for row in rows if int(row["draft_year"]) < 2026]
        holdout_rows = [row for row in rows if int(row["draft_year"]) == 2026]
        model = CollegeDraftProbabilityModel(
            prior_rows,
            position="WR",
            population="FBS roster through completed week",
        )

        paired_rows: list[dict[str, object]] = []
        for row in holdout_rows:
            original = dict(row)
            changed = {
                **row,
                "drafted": not bool(row["drafted"]),
                "draft_round": 1 if not bool(row["drafted"]) else None,
                "draft_ovr": 1 if not bool(row["drafted"]) else None,
                "elite": not bool(row["drafted"]),
                "outcome_label_known": False,
                "outcome_match_method": "deliberately changed in test",
                "nfl_team": "POST-DRAFT TEAM",
                "reference_only": True,
                "nfl_hof": True,
                "nfl_all_pro_selections": 99,
                "nfl_pro_bowls": 99,
                "active_roster_status": "ACT",
            }
            paired_rows.extend((OutcomeGuard(original), OutcomeGuard(changed)))

        replay = model.score_retrospective_holdout(
            paired_rows,
            holdout_year=2026,
        )
        self.assertEqual(replay.training_years, (2022, 2023, 2024, 2025))
        self.assertEqual(replay.training_rows, 640)
        self.assertEqual(replay.training_positives, 48)
        self.assertEqual(replay.calibration_years, (2023, 2024, 2025))
        self.assertEqual(replay.calibration_rows, 480)
        self.assertEqual(replay.calibration_positives, 36)
        self.assertTrue(replay.scores)
        for index in range(0, len(replay.scores), 2):
            original = replay.scores[index]
            changed = replay.scores[index + 1]
            self.assertEqual(original.raw_probability, changed.raw_probability)
            self.assertEqual(original.probability, changed.probability)
            self.assertEqual(original.within_position_rank, changed.within_position_rank)
        self.assertFalse(
            {"drafted", "draft_round", "draft_ovr", "elite"} & set(replay.features)
        )

        wrong_checkpoint = dict(holdout_rows[0])
        wrong_checkpoint["as_of_week"] = 7
        with self.assertRaisesRegex(ModelError, "as_of_week contract"):
            model.score_retrospective_holdout(
                [wrong_checkpoint],
                holdout_year=2026,
            )

        full_model = CollegeDraftProbabilityModel(
            rows,
            position="WR",
            population="FBS roster through completed week",
        )
        with self.assertRaisesRegex(ModelError, "must all precede"):
            full_model.score_retrospective_holdout(
                holdout_rows,
                holdout_year=2026,
            )

    def test_year_held_out_model_orders_strong_above_weak(self) -> None:
        rows = synthetic_history()
        features = (
            "height_in", "weight_lb", "forty_s", "vertical_in", "broad_jump_in",
            "three_cone_s", "shuttle_s", "prod_yards_per_route", "trait_route_running", "age_at_draft",
        )
        model = DraftProbabilityModel(
            rows,
            position="WR",
            candidate_features=features,
            population="synthetic eligible pool",
        )
        self.assertNotIn("age_at_draft", model.features)
        self.assertTrue(model.validation.passes_quality_gate)
        self.assertEqual(model.validation.threshold, DECISION_THRESHOLD)
        self.assertIsNotNone(model.calibrator)
        strong = {
            "forty_s": 4.36, "vertical_in": 40, "broad_jump_in": 130, "three_cone_s": 6.82,
            "shuttle_s": 4.10, "height_in": 74, "weight_lb": 205,
            "prod_yards_per_route": 2.8, "trait_route_running": 84,
        }
        weak = {
            "forty_s": 4.72, "vertical_in": 29, "broad_jump_in": 108, "three_cone_s": 7.35,
            "shuttle_s": 4.55, "height_in": 72, "weight_lb": 190,
            "prod_yards_per_route": 1.0, "trait_route_running": 45,
        }
        strong_prediction = model.predict(strong, bootstrap=0)
        weak_prediction = model.predict(weak, bootstrap=0)
        self.assertTrue(strong_prediction.available)
        self.assertIsNotNone(strong_prediction.probability)
        self.assertIsNotNone(weak_prediction.probability)
        self.assertGreater(strong_prediction.probability, weak_prediction.probability)
        self.assertIsNotNone(model.validation.roc_auc)
        self.assertGreater(model.validation.roc_auc or 0, 0.70)

        same_profile = {
            "forty_s": 4.48, "vertical_in": 35, "broad_jump_in": 121, "three_cone_s": 7.02,
            "shuttle_s": 4.28, "height_in": 73, "weight_lb": 198, "trait_route_running": 65,
        }
        hot_week = model.predict({**same_profile, "prod_yards_per_route": 2.8}, bootstrap=0)
        cold_week = model.predict({**same_profile, "prod_yards_per_route": 1.0}, bootstrap=0)
        self.assertIsNotNone(hot_week.probability)
        self.assertIsNotNone(cold_week.probability)
        self.assertGreater(hot_week.probability, cold_week.probability)

    def test_forward_validation_publishes_position_board_and_calibration_metrics(self) -> None:
        rows = synthetic_history(rows_per_year=36)
        model = DraftProbabilityModel(
            rows,
            position="WR",
            candidate_features=(
                "forty_s",
                "vertical_in",
                "broad_jump_in",
                "prod_yards_per_route",
            ),
            population="synthetic eligible pool",
        )

        validation = model.validation
        self.assertIsNotNone(validation.expected_calibration_error)
        self.assertGreaterEqual(validation.expected_calibration_error or 0.0, 0.0)
        self.assertLessEqual(validation.expected_calibration_error or 1.0, 1.0)
        self.assertIn("Within-position", validation.board_scope)
        self.assertEqual(
            [row["cutoff"] for row in validation.board_metrics],
            [10, 25, 50],
        )
        top_ten = validation.board_metrics[0]
        self.assertEqual(top_ten["cutoff_scope"], "top-k per position per evaluated draft year")
        self.assertEqual(top_ten["evaluated_years"], (2024, 2025, 2026))
        self.assertEqual(top_ten["selected_rows"], 30)
        self.assertGreater(top_ten["precision"], validation.prevalence)
        self.assertIsNotNone(top_ten["mean_binary_ndcg"])
        self.assertEqual(top_ten["draft_round_outcome_coverage"], 1.0)
        self.assertIsNotNone(top_ten["early_round_recall"])
        self.assertTrue(
            all("expected_calibration_error" in row for row in validation.per_year)
        )

    def test_simple_baselines_are_past_only_and_use_the_same_evaluation_set(self) -> None:
        rows = synthetic_history(rows_per_year=36)
        for row in rows:
            production = float(row["prod_yards_per_route"])
            trait = float(row["trait_route_running"])
            row["recruit_rating"] = 0.80 + trait / 1_000.0
            row["recruit_stars"] = 2.0 + trait / 30.0
            row["recruit_national_rank"] = 500.0 - trait * 5.0
            row["prod_receiving_yards"] = production * 400.0
            row["prod_receiving_tds"] = production * 4.0
        features = (
            "forty_s",
            "vertical_in",
            "recruit_rating",
            "recruit_stars",
            "recruit_national_rank",
            "prod_receiving_yards",
            "prod_receiving_tds",
        )
        model = DraftProbabilityModel(
            rows,
            position="WR",
            candidate_features=features,
            population="synthetic eligible pool",
        )
        by_name = {row["name"]: row for row in model.validation.simple_baselines}
        self.assertEqual(set(by_name), {"recruiting_only", "production_only"})
        for baseline in by_name.values():
            self.assertTrue(baseline["available"])
            self.assertTrue(baseline["evaluation_set_matches_primary"])
            self.assertEqual(baseline["rows"], model.validation.rows)
            self.assertEqual(baseline["evaluated_years"], (2024, 2025, 2026))
            self.assertIsNotNone(baseline["brier"])
            self.assertIsNotNone(baseline["expected_calibration_error"])
            self.assertTrue(baseline["board_metrics"])
            for year in baseline["per_year"]:
                self.assertTrue(all(train_year < year["year"] for train_year in year["train_years"]))

        future_changed = [dict(row) for row in rows]
        for row in future_changed:
            if row["draft_year"] == 2026:
                row["recruit_rating"] = 0.1
                row["recruit_stars"] = 1.0
                row["recruit_national_rank"] = 9_999.0
                row["prod_receiving_yards"] = 1.0
                row["prod_receiving_tds"] = 0.0
        changed = DraftProbabilityModel(
            future_changed,
            position="WR",
            candidate_features=features,
            population="synthetic eligible pool",
        )
        changed_by_name = {
            row["name"]: row for row in changed.validation.simple_baselines
        }
        for name, baseline in by_name.items():
            original_early = [
                row for row in baseline["per_year"] if row["year"] in {2024, 2025}
            ]
            changed_early = [
                row
                for row in changed_by_name[name]["per_year"]
                if row["year"] in {2024, 2025}
            ]
            self.assertEqual(original_early, changed_early)

    def test_feature_selection_and_calibration_use_only_prior_years(self) -> None:
        model = RecordingDraftProbabilityModel(
            synthetic_history(),
            position="WR",
            candidate_features=("forty_s", "vertical_in", "prod_yards_per_route"),
            population="synthetic eligible pool",
        )
        all_years = frozenset(range(2022, 2027))
        # The all-year call fits the final deployment model. Every validation
        # call must be an expanding prefix and therefore can contain no class
        # later than the one being predicted.
        validation_calls = [years for years in model.feature_selection_years if years != all_years]
        self.assertEqual(
            validation_calls,
            [
                frozenset({2022}),
                frozenset({2022, 2023}),
                frozenset({2022, 2023, 2024}),
                frozenset({2022, 2023, 2024, 2025}),
            ],
        )
        self.assertEqual(model.validation.evaluated_years, (2024, 2025, 2026))
        self.assertIn("expanding-window", model.validation.validation_scheme)

    def test_quality_gate_withholds_unlearnable_position_model(self) -> None:
        model = DraftProbabilityModel(
            unlearnable_history(),
            position="TE",
            candidate_features=("feature_a", "feature_b"),
            population="synthetic noise pool",
        )
        self.assertFalse(model.validation.passes_quality_gate)
        self.assertEqual(model.validation.threshold, DECISION_THRESHOLD)
        prediction = model.predict({"feature_a": 10.0, "feature_b": 4.0}, bootstrap=0)
        self.assertFalse(prediction.available)
        self.assertIsNone(prediction.probability)
        self.assertIn("withheld", prediction.label.lower())


if __name__ == "__main__":
    unittest.main()

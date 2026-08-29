from __future__ import annotations

import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from draftscope.analysis import Comparable, ProspectEvaluator
from draftscope.mathstats import robust_scale
from draftscope.records import normalize_name, parse_bool, parse_number, write_records
from draftscope.reporting import render_evaluation
from draftscope.schema import MetricSpec, all_specs, normalize_position
from test_model import synthetic_college_roster_history, synthetic_history


def _uncached_comparables(
    evaluator: ProspectEvaluator,
    candidate: dict[str, object],
    history: list[dict[str, object]],
    *,
    categories: set[str],
    limit: int = 5,
    use_benchmark_cohort: bool = True,
    reference: str | None = None,
) -> list[Comparable]:
    """Pre-index implementation retained here as an output regression oracle."""

    position = normalize_position(candidate.get("position"))
    specs = [spec for spec in all_specs(position) if spec.category in categories]
    comparison_history = [dict(row) for row in history]
    explicit_cohort = False
    if use_benchmark_cohort:
        comparison_history, cohort_reference, explicit_cohort = (
            evaluator._benchmark_cohort_rows(history)
        )
        reference = reference or cohort_reference
    reference = reference or (
        "full drafted history"
        if not explicit_cohort
        else "current-active drafted benchmark cohort"
    )
    drafted = [
        row
        for row in comparison_history
        if normalize_position(row.get("position")) == position
        and parse_bool(row.get("drafted")) is True
    ]
    minimum_history = max(5, math.ceil(len(drafted) * 0.20))
    available_specs = [
        spec
        for spec in specs
        if parse_number(candidate.get(spec.key)) is not None
        and sum(parse_number(row.get(spec.key)) is not None for row in drafted)
        >= minimum_history
    ]
    if len(available_specs) < 3:
        return []
    distributions = {
        spec.key: [parse_number(row.get(spec.key)) for row in drafted]
        for spec in available_specs
    }
    ranked: list[
        tuple[float, float, dict[str, object], list[str], int]
    ] = []
    for row in drafted:
        squared = 0.0
        weight_sum = 0.0
        differences: list[tuple[float, str]] = []
        compared = 0
        for spec in available_specs:
            left = parse_number(candidate.get(spec.key))
            right = parse_number(row.get(spec.key))
            if left is None or right is None:
                continue
            delta = (left - right) / robust_scale(distributions[spec.key])
            squared += spec.weight * delta * delta
            weight_sum += spec.weight
            compared += 1
            differences.append(
                (abs(delta), f"{spec.label}: {left:g} vs {right:g}")
            )
        if compared < 3 or weight_sum <= 0:
            continue
        overlap = compared / len(available_specs)
        if overlap < 0.35:
            continue
        distance = math.sqrt(squared / weight_sum) + (1.0 - overlap) * 0.80
        similarity = 100.0 * math.exp(-0.45 * distance)
        differences.sort(reverse=True)
        ranked.append(
            (
                similarity,
                overlap,
                row,
                [text for _, text in differences[:3]],
                compared,
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    output: list[Comparable] = []
    for similarity, overlap, row, differences, compared in ranked[:limit]:
        year = parse_number(row.get("draft_year") or row.get("season"))
        draft_round = parse_number(row.get("draft_round"))
        draft_pick = parse_number(row.get("draft_ovr"))
        output.append(
            Comparable(
                name=str(row.get("name") or ""),
                position=position,
                school=str(row.get("school") or ""),
                draft_year=int(year) if year is not None else None,
                draft_round=int(draft_round) if draft_round is not None else None,
                draft_pick=int(draft_pick) if draft_pick is not None else None,
                nfl_team=str(row.get("nfl_team") or ""),
                similarity=similarity,
                feature_overlap=overlap,
                features_compared=compared,
                biggest_differences=differences,
                reference=reference,
            )
        )
    return output


def _uncached_active_reference_rows(
    evaluator: ProspectEvaluator,
    candidate: dict[str, object],
    spec: MetricSpec,
) -> list[dict[str, object]]:
    """Original per-call registry join, used to check the cached join exactly."""

    registry = evaluator.history_metadata.get("active_drafted_players", [])
    if not registry or spec.category != "production":
        return []
    pfr_ids = {
        str(row.get("pfr_player_id") or "").strip()
        for row in registry
        if str(row.get("pfr_player_id") or "").strip()
    }
    cfb_ids = {
        str(row.get("cfb_player_id") or "").strip()
        for row in registry
        if str(row.get("cfb_player_id") or "").strip()
    }
    name_year_counts: dict[tuple[int | None, str], int] = {}
    for row in registry:
        year = parse_number(row.get("draft_year"))
        key = (int(year) if year is not None else None, normalize_name(row.get("name")))
        if key[0] is not None and key[1]:
            name_year_counts[key] = name_year_counts.get(key, 0) + 1
    unique_name_year = {key for key, count in name_year_counts.items() if count == 1}
    position = normalize_position(candidate.get("position"))
    rows = []
    for row in evaluator._aligned_model_history(candidate):
        if (
            normalize_position(row.get("position")) != position
            or parse_bool(row.get("drafted")) is not True
            or parse_number(row.get(spec.key)) is None
        ):
            continue
        pfr_id = str(row.get("outcome_nflverse_pfr_player_id") or "").strip()
        cfb_id = str(row.get("outcome_nflverse_cfb_player_id") or "").strip()
        year = parse_number(row.get("draft_year") or row.get("season"))
        name_year = (
            int(year) if year is not None else None,
            normalize_name(row.get("name")),
        )
        if (
            (pfr_id and pfr_id in pfr_ids)
            or (cfb_id and cfb_id in cfb_ids)
            or (not pfr_id and not cfb_id and name_year in unique_name_year)
        ):
            rows.append(dict(row))
    return rows


class AnalysisTests(unittest.TestCase):
    @staticmethod
    def _player(name: str = "Current Prospect", **updates: object) -> dict[str, object]:
        player: dict[str, object] = {
            "name": name,
            "position": "WR",
            "school": "Example University",
            "season": 2026,
            "as_of_week": 6,
            "height_in": 74,
            "weight_lb": 204,
            "forty_s": 4.39,
            "vertical_in": 39,
            "broad_jump_in": 128,
            "three_cone_s": 6.88,
            "shuttle_s": 4.12,
            "prod_yards_per_route": 2.7,
            "trait_route_running": 84,
            "trait_separation": 80,
            "measurement_source": "verified_workout",
        }
        player.update(updates)
        return player

    def test_report_contains_benchmarks_comps_and_team_fit(self) -> None:
        history = synthetic_history(rows_per_year=30)
        benchmark_history = [
            {key: value for key, value in row.items() if not key.startswith("prod_") and not key.startswith("trait_")}
            for row in history
        ]
        player = self._player()
        profiles = [
            {
                "team": "Example AFC",
                "position": "WR",
                "need_score": 85,
                "starter_quality_need_score": 78,
                "contract_need_score": 74,
                "coaching_stability_score": 82,
                "timeline_score": 70,
                "draft_access_min": 20,
                "draft_access_max": 100,
                "importance_trait_route_running": 1.0,
                "importance_forty_s": 0.5,
                "ideal_forty_s": 4.40,
                "scheme": "timing / separation",
                "profile_source": "manual_plus_nflverse",
                "profile_as_of_date": "2026-10-01",
            }
        ]
        evaluator = ProspectEvaluator(
            benchmark_history,
            [player],
            history_metadata={"population": "synthetic measurement benchmark"},
            model_history=history,
            model_metadata={
                "population": "synthetic entrants",
                "probability_kind": "conditional_on_entry",
                "probability_condition": "P(drafted | enters next draft)",
            },
            team_profiles=profiles,
            as_of_date="2026-10-05",
        )
        result = evaluator.evaluate("Current Prospect", bootstrap=0)
        self.assertIsNotNone(result.profile_score)
        self.assertTrue(result.draft_prediction.available)
        self.assertIsNone(result.draft_prediction.probability)
        self.assertIsNotNone(result.draft_prediction.conditional_probability)
        self.assertTrue(any(item.metric == "forty_s" for item in result.benchmarks))
        self.assertTrue(any(item.metric == "prod_yards_per_route" for item in result.benchmarks))
        self.assertGreaterEqual(len(result.physical_comps), 3)
        self.assertEqual(result.team_fits[0].team, "Example AFC")
        self.assertGreater(result.team_fits[0].score, 60)
        report = render_evaluation(result)
        self.assertIn("Next-draft likelihood: unavailable", report)
        self.assertIn("Model-conditional draft probability:", report)
        self.assertIn("Conditional scope: P(drafted | enters next draft)", report)
        self.assertIn("Features (", report)
        self.assertIn("prod_yards_per_route", report)
        self.assertIn("Feature coverage:", report)
        self.assertIn("Quality gate: PASS", report)
        self.assertIn("Brier=", report)
        self.assertIn("baseline=", report)
        self.assertIn("Validation design: expanding-window past-only", report)
        self.assertIn("Forward metrics by evaluated draft year", report)
        self.assertIn("Past-only forward metrics", report)
        self.assertIn("ECE=", report)
        self.assertIn("Draft-board ranking checks", report)
        self.assertIn("not cross-position overall-board cutoffs", report)
        self.assertIn("Simple past-only baselines", report)
        self.assertIn("Starter", report)
        self.assertIn("Contract", report)
        self.assertIn("Coach", report)
        self.assertIn("2026-10-01", report)
        self.assertIn("Dated supplemental evidence shown", report)

    def test_historical_career_elite_comps_include_honors_and_full_window(self) -> None:
        history = synthetic_history(rows_per_year=40)
        drafted = [row for row in history if row["drafted"]]
        active_names = {str(row["name"]) for row in drafted[:8]}
        career_elite_names: set[str] = set()
        for index, row in enumerate(drafted[:14]):
            row["benchmark_cohort_eligible"] = str(row["name"]) in active_names
            row["nfl_hof"] = index == 0
            row["nfl_all_pro_selections"] = 1 if index % 3 == 1 else 0
            row["nfl_pro_bowls"] = 3 if index % 3 == 2 else 0
            row["nfl_career_elite"] = bool(
                row["nfl_hof"]
                or row["nfl_all_pro_selections"]
                or row["nfl_pro_bowls"] >= 3
            )
            career_elite_names.add(str(row["name"]))
        for row in history:
            row.setdefault("benchmark_cohort_eligible", False)

        evaluator = ProspectEvaluator(history, [self._player()])
        result = evaluator.evaluate("Current Prospect", bootstrap=0)

        self.assertEqual(len(result.physical_comps), 8)
        self.assertEqual(len(result.overall_comps), 8)
        self.assertEqual(len(result.historical_elite_comps), 8)
        self.assertTrue(
            all(comp.name in career_elite_names for comp in result.historical_elite_comps)
        )
        self.assertTrue(
            any(comp.name not in active_names for comp in result.historical_elite_comps)
        )
        self.assertTrue(
            all(comp.features_compared >= 2 for comp in result.historical_elite_comps)
        )
        self.assertTrue(
            all(
                comp.nfl_hof is not None
                and comp.nfl_all_pro_selections is not None
                and comp.nfl_pro_bowls is not None
                for comp in result.historical_elite_comps
            )
        )
        self.assertIn("first-team All-Pro", result.historical_elite_comps[0].reference)

    def test_all_history_elite_pool_is_isolated_from_models_and_recent_comps(self) -> None:
        history = synthetic_history(rows_per_year=40)
        candidate = self._player()
        career_elite = []
        for index in range(6):
            career_elite.append(
                {
                    "player_id": f"historic-{index}",
                    "name": "Old Undrafted Legend" if index == 0 else f"Historic Elite {index}",
                    "position": "WR",
                    "height_in": 74 + index * 0.2,
                    "weight_lb": 204 + index,
                    "draft_year": 1965 + index,
                    "drafted": None,
                    "nfl_hof": True,
                    "nfl_career_elite": True,
                    "nfl_all_pro_selections": None,
                    "nfl_pro_bowls": None,
                    "reference_only": True,
                    "comparison_positions": "WR;K",
                }
            )

        baseline = ProspectEvaluator(history, [candidate]).evaluate(
            "Current Prospect", bootstrap=0
        )
        with tempfile.TemporaryDirectory() as temporary:
            catalog_path = Path(temporary) / "career_elite_history.csv"
            write_records(catalog_path, career_elite)
            expanded = ProspectEvaluator(
                history,
                [candidate],
                history_metadata={
                    "historical_career_elite_catalog_path": str(catalog_path),
                    "historical_career_elite_catalog": {
                        "rows": len(career_elite),
                        "scope_label": "all NFL eras for Hall of Fame inductees",
                        "model_feature_eligible": False,
                    },
                },
            ).evaluate("Current Prospect", bootstrap=0)

        self.assertEqual(
            expanded.historical_elite_comps[0].name, "Old Undrafted Legend"
        )
        self.assertTrue(
            all(comp.draft_year < 1980 for comp in expanded.historical_elite_comps)
        )
        self.assertEqual(expanded.draft_prediction, baseline.draft_prediction)
        self.assertEqual(expanded.college_prediction, baseline.college_prediction)
        self.assertEqual(expanded.combine_prediction, baseline.combine_prediction)
        self.assertEqual(expanded.benchmarks, baseline.benchmarks)
        self.assertEqual(expanded.profile_score, baseline.profile_score)
        self.assertEqual(expanded.projected_pick_range, baseline.projected_pick_range)
        self.assertEqual(expanded.physical_comps, baseline.physical_comps)
        self.assertEqual(expanded.overall_comps, baseline.overall_comps)
        self.assertEqual(expanded.pick_comps, baseline.pick_comps)
        self.assertEqual(expanded.team_fits, baseline.team_fits)
        forbidden = {
            "nfl_hof",
            "nfl_all_pro_selections",
            "nfl_pro_bowls",
            "nfl_career_elite",
        }
        self.assertTrue(
            forbidden.isdisjoint(expanded.draft_prediction.features_used)
        )
        self.assertFalse(
            expanded.benchmark_context[
                "historical_career_elite_model_feature_eligible"
            ]
        )
        elite_pool = ProspectEvaluator(
            history,
            [candidate],
            career_elite_history=career_elite,
        )._comparable_pool(
            career_elite,
            use_benchmark_cohort=False,
            require_drafted=False,
        )
        self.assertEqual(len(elite_pool.rows_by_position["WR"]), 6)
        self.assertEqual(len(elite_pool.rows_by_position["K"]), 6)

    def test_missing_career_elite_catalog_fails_closed(self) -> None:
        history = synthetic_history(rows_per_year=40)
        candidate = self._player()
        evaluator = ProspectEvaluator(
            history,
            [candidate],
            history_metadata={
                "historical_career_elite_catalog_path": "/does/not/exist.csv",
                "historical_career_elite_catalog": {
                    "rows": 999,
                    "scope_label": "all NFL eras",
                },
            },
        )

        result = evaluator.evaluate("Current Prospect", bootstrap=0)

        self.assertFalse(result.historical_elite_comps)
        self.assertEqual(
            result.benchmark_context["historical_career_elite_scope"],
            "career-elite catalog unavailable",
        )
        self.assertEqual(
            result.benchmark_context["historical_career_elite_rows"], 0
        )

    def test_header_only_career_elite_catalog_fails_closed(self) -> None:
        history = synthetic_history(rows_per_year=40)
        candidate = self._player()
        with tempfile.TemporaryDirectory() as temporary:
            catalog_path = Path(temporary) / "career_elite_history.csv"
            write_records(catalog_path, [])
            evaluator = ProspectEvaluator(
                history,
                [candidate],
                history_metadata={
                    "historical_career_elite_catalog_path": str(catalog_path),
                    "historical_career_elite_catalog": {
                        "rows": 955,
                        "scope_label": "all NFL eras",
                    },
                },
            )

            result = evaluator.evaluate("Current Prospect", bootstrap=0)

        self.assertFalse(result.historical_elite_comps)
        self.assertEqual(
            result.benchmark_context["historical_career_elite_scope"],
            "career-elite catalog unavailable",
        )
        self.assertEqual(
            result.benchmark_context["historical_career_elite_rows"], 0
        )

    def test_size_only_comps_are_displayed_but_not_used_for_pick_access(self) -> None:
        history = synthetic_history(rows_per_year=40)
        candidate = {
            "name": "Size Only Prospect",
            "position": "WR",
            "school": "Example",
            "height_in": 73,
            "weight_lb": 200,
        }
        result = ProspectEvaluator(history, [candidate]).evaluate(
            "Size Only Prospect", bootstrap=0
        )

        self.assertEqual(len(result.physical_comps), 8)
        self.assertTrue(all(comp.features_compared == 2 for comp in result.physical_comps))
        self.assertFalse(result.overall_comps)
        self.assertFalse(result.pick_comps)
        self.assertIsNone(result.projected_pick_range)

    def test_reference_only_evaluation_skips_model_team_fit_and_self_comp(self) -> None:
        history = synthetic_history(rows_per_year=40)
        drafted = next(row for row in history if row["drafted"])
        drafted["pfr_id"] = "stable-reference-id"
        candidate = dict(drafted)
        candidate.pop("player_id", None)
        candidate["reference_only"] = True
        candidate["nfl_hof"] = False
        candidate["nfl_all_pro_selections"] = 1
        candidate["nfl_pro_bowls"] = 0
        candidate["nfl_career_elite"] = True
        profiles = [{"team": "Never Ranked", "position": "WR", "need_score": 100}]
        evaluator = ProspectEvaluator(
            history,
            [candidate],
            team_profiles=profiles,
        )

        with patch.object(
            ProspectEvaluator,
            "_stage_prediction",
            side_effect=AssertionError("reference-only lookup fitted a model"),
        ):
            result = evaluator.evaluate(
                str(candidate["name"]), bootstrap=0, reference_only=True
            )

        self.assertFalse(result.draft_prediction.available)
        self.assertIn("already known", result.draft_prediction.label)
        self.assertFalse(result.team_fits)
        self.assertIsNone(result.projected_pick_range)
        for comps in (
            result.physical_comps,
            result.overall_comps,
            result.pick_comps,
            result.historical_elite_comps,
        ):
            self.assertNotIn(candidate["name"], {comp.name for comp in comps})

    def test_active_roster_cohort_drives_benchmarks_and_displayed_comps(self) -> None:
        full_history = synthetic_history(rows_per_year=30)
        active_model_rows = [row for row in full_history if row["drafted"]][:8]
        active_names = {str(row["name"]) for row in active_model_rows}
        active_registry = []
        for index, row in enumerate(active_model_rows):
            player_id = f"active-pfr-{index}"
            row["outcome_nflverse_pfr_player_id"] = player_id
            active_registry.append(
                {
                    "draft_year": row["draft_year"],
                    "draft_pick": row["draft_ovr"],
                    "pfr_player_id": player_id,
                    "name": row["name"],
                    "college": row.get("school", "Synthetic University"),
                    "active_roster_status": "ACT",
                }
            )
        benchmark_history = [
            {key: value for key, value in row.items() if not key.startswith("prod_")}
            for row in full_history
        ]
        active_rows = [row for row in benchmark_history if str(row["name"]) in active_names]
        for row in benchmark_history:
            row["benchmark_cohort_eligible"] = row in active_rows
            row["comparison_cohort_eligible"] = row in active_rows
            row["active_roster_season"] = 2026
            row["active_roster_status"] = "ACT" if row in active_rows else "RET"

        expected_forty = sum(float(row["forty_s"]) for row in active_rows) / len(active_rows)
        expected_production = sum(
            float(row["prod_yards_per_route"]) for row in active_model_rows
        ) / len(active_model_rows)
        player = self._player()
        evaluator = ProspectEvaluator(
            benchmark_history,
            [player],
            history_metadata={
                "model_stage": "combine",
                "population": "NFL Scouting Combine participants",
                "probability_kind": "conditional_on_combine_invitation",
                "probability_condition": "P(drafted | NFL Scouting Combine participant)",
                "benchmark_population": (
                    "active on nflverse 2026 roster; drafted 2022-2026; "
                    "combine measurement available"
                ),
                "benchmark_source": "fixture combine + draft + roster",
                "benchmark_rows": len(active_rows),
                "benchmark_elite_rows": sum(bool(row["elite"]) for row in active_rows),
                "active_drafted_window_rows": 10,
                "benchmark_cohort_coverage": 0.8,
                "active_roster_season": 2026,
                "active_roster_snapshot_week": 1,
                "active_roster_source_as_of": "2026-08-25T17:52:37-04:00",
                "active_roster_source_as_of_basis": "local artifact download timestamp",
                "active_roster_included_statuses": ["ACT", "RES"],
                "active_roster_status_counts": {"ACT": 8, "RET": 2},
                "window_start": 2022,
                "window_end": 2026,
                "active_drafted_players": active_registry,
            },
            model_history=full_history,
            model_metadata={
                "population": "synthetic entrants",
                "probability_kind": "conditional_on_entry",
                "probability_condition": "P(drafted | enters next draft)",
            },
        )

        result = evaluator.evaluate("Current Prospect", bootstrap=0)
        forty = next(item for item in result.benchmarks if item.metric == "forty_s")
        self.assertEqual(forty.drafted_n, len(active_rows))
        self.assertAlmostEqual(forty.drafted_mean or 0.0, expected_forty)
        self.assertIn("active on nflverse 2026 roster", forty.reference)
        production = next(
            item for item in result.benchmarks if item.metric == "prod_yards_per_route"
        )
        self.assertEqual(production.drafted_n, len(active_model_rows))
        self.assertAlmostEqual(production.drafted_mean or 0.0, expected_production)
        self.assertIn("active on nflverse 2026 roster", production.reference)
        self.assertIn("college production", production.reference)
        self.assertTrue(result.overall_comps)
        self.assertTrue(all(comp.name in active_names for comp in result.overall_comps))
        self.assertTrue(result.pick_comps)
        self.assertTrue(
            all(comp.reference == "full drafted history for pick-band support" for comp in result.pick_comps)
        )
        self.assertEqual(result.benchmark_context["rows"], 8)
        self.assertEqual(result.benchmark_context["coverage"], 0.8)
        report = render_evaluation(result)
        self.assertIn("Benchmark cohort: active on nflverse 2026 roster", report)
        self.assertIn("80% coverage", report)
        self.assertIn("Active drafted avg", report)
        self.assertIn("Elite active top-64 avg", report)
        self.assertIn("CURRENT-ACTIVE COMPARABLE DRAFTED PLAYERS", report)
        self.assertIn("Full-history comparable pick band", report)

    def test_established_active_contributors_replace_top64_elite_comparison(self) -> None:
        full_history = synthetic_history(rows_per_year=30)
        active_model_rows = [row for row in full_history if row["drafted"]][:10]
        contributor_model_rows = active_model_rows[:5]
        active_names = {str(row["name"]) for row in active_model_rows}
        contributor_names = {str(row["name"]) for row in contributor_model_rows}
        active_registry: list[dict[str, object]] = []
        contributor_registry: list[dict[str, object]] = []
        for index, row in enumerate(active_model_rows):
            player_id = f"contributor-pfr-{index}"
            row["outcome_nflverse_pfr_player_id"] = player_id
            registry_row: dict[str, object] = {
                "draft_year": row["draft_year"],
                "draft_pick": row["draft_ovr"],
                "pfr_player_id": player_id,
                "name": row["name"],
                "active_roster_status": "ACT",
            }
            active_registry.append(registry_row)
            if row in contributor_model_rows:
                contributor_registry.append(
                    {
                        **registry_row,
                        "nfl_three_year_outcome_known": True,
                        "nfl_three_year_contributor": True,
                    }
                )

        benchmark_history = [
            {
                key: value
                for key, value in row.items()
                if not key.startswith("prod_")
            }
            for row in full_history
        ]
        active_rows = [
            row for row in benchmark_history if str(row["name"]) in active_names
        ]
        for row in benchmark_history:
            active = row in active_rows
            contributor = str(row["name"]) in contributor_names
            row["benchmark_cohort_eligible"] = active
            row["comparison_cohort_eligible"] = active
            row["benchmark_active_contributor_cohort_eligible"] = bool(
                active and contributor
            )
            row["nfl_three_year_outcome_known"] = bool(active)
            row["nfl_three_year_contributor"] = bool(active and contributor)

        contributor_benchmark_rows = [
            row
            for row in active_rows
            if row["benchmark_active_contributor_cohort_eligible"]
        ]
        expected_forty = sum(
            float(row["forty_s"]) for row in contributor_benchmark_rows
        ) / len(contributor_benchmark_rows)
        expected_production = sum(
            float(row["prod_yards_per_route"]) for row in contributor_model_rows
        ) / len(contributor_model_rows)
        outcome_definition = (
            "at least 750 offense+defense snaps or 450 special-teams snaps across "
            "the first 3 regular seasons after the draft"
        )
        evaluator = ProspectEvaluator(
            benchmark_history,
            [self._player()],
            history_metadata={
                "model_stage": "combine",
                "population": "NFL Scouting Combine participants",
                "probability_kind": "conditional_on_combine_invitation",
                "benchmark_population": "active drafted fixture cohort",
                "benchmark_rows": len(active_rows),
                "benchmark_elite_rows": sum(bool(row["elite"]) for row in active_rows),
                "benchmark_active_contributor_rows": len(contributor_benchmark_rows),
                "active_drafted_window_rows": len(active_rows),
                "active_roster_season": 2026,
                "window_start": 2017,
                "window_end": 2026,
                "active_drafted_players": active_registry,
                "active_contributor_players": contributor_registry,
                "nfl_three_year_contributor_outcome": {
                    "status": "pass",
                    "outcome_definition": outcome_definition,
                    "known_outcomes": 200,
                    "contributors": 90,
                    "right_censored_outcomes": 60,
                    "completed_nfl_season": 2025,
                },
            },
            model_history=full_history,
            model_metadata={
                "population": "synthetic entrants",
                "probability_kind": "conditional_on_entry",
                "probability_condition": "P(drafted | enters next draft)",
            },
        )

        result = evaluator.evaluate("Current Prospect", bootstrap=0)
        forty = next(item for item in result.benchmarks if item.metric == "forty_s")
        production = next(
            item
            for item in result.benchmarks
            if item.metric == "prod_yards_per_route"
        )
        self.assertEqual(forty.elite_kind, "active_contributor")
        self.assertEqual(production.elite_kind, "active_contributor")
        self.assertEqual(forty.elite_n, len(contributor_benchmark_rows))
        self.assertAlmostEqual(forty.elite_mean or 0.0, expected_forty)
        self.assertEqual(production.elite_n, len(contributor_model_rows))
        self.assertAlmostEqual(production.elite_mean or 0.0, expected_production)
        self.assertEqual(result.benchmark_context["elite_kind"], "active_contributor")
        self.assertEqual(result.benchmark_context["elite_rows"], len(contributor_benchmark_rows))
        report = render_evaluation(result)
        self.assertIn("established active-contributor cohort", report)
        self.assertIn("Established active contributor avg", report)
        self.assertIn(outcome_definition, report)
        self.assertIn("right-censored n=60 (excluded)", report)

    def test_comparable_index_reuses_cohorts_without_changing_results(self) -> None:
        history = synthetic_history(rows_per_year=40)
        for index, row in enumerate(history):
            row["benchmark_cohort_eligible"] = bool(row["drafted"]) and index % 2 == 0

        first = self._player("First Prospect")
        second = self._player(
            "Second Prospect",
            forty_s=4.48,
            vertical_in=36,
            broad_jump_in=122,
            prod_yards_per_route=2.2,
            trait_route_running=76,
        )
        evaluator = ProspectEvaluator(history, [first, second])

        expected_first = _uncached_comparables(
            evaluator,
            first,
            evaluator.history,
            categories={"physical", "production", "skills", "age"},
        )
        actual_first = evaluator._comparables(
            first,
            evaluator.history,
            categories={"physical", "production", "skills", "age"},
        )
        self.assertEqual(actual_first, expected_first)

        active_pool = evaluator._comparable_pool(
            evaluator.history, use_benchmark_cohort=True
        )
        first_values = evaluator._comparable_metric_values(
            active_pool, "WR", "forty_s"
        )
        cache_size = len(evaluator._comparable_pool_cache)

        expected_second = _uncached_comparables(
            evaluator,
            second,
            evaluator.history,
            categories={"physical", "production", "skills", "age"},
        )
        actual_second = evaluator._comparables(
            second,
            evaluator.history,
            categories={"physical", "production", "skills", "age"},
        )
        reused_pool = evaluator._comparable_pool(
            evaluator.history, use_benchmark_cohort=True
        )
        self.assertEqual(actual_second, expected_second)
        self.assertIs(reused_pool, active_pool)
        self.assertIs(
            evaluator._comparable_metric_values(reused_pool, "WR", "forty_s"),
            first_values,
        )
        self.assertEqual(len(evaluator._comparable_pool_cache), cache_size)

        expected_full = _uncached_comparables(
            evaluator,
            first,
            evaluator.history,
            categories={"physical"},
            use_benchmark_cohort=False,
            reference="full drafted history for pick-band support",
        )
        actual_full = evaluator._comparables(
            first,
            evaluator.history,
            categories={"physical"},
            use_benchmark_cohort=False,
            reference="full drafted history for pick-band support",
        )
        full_pool = evaluator._comparable_pool(
            evaluator.history, use_benchmark_cohort=False
        )
        self.assertEqual(actual_full, expected_full)
        self.assertIsNot(full_pool, active_pool)
        self.assertGreater(
            len(full_pool.rows_by_position["WR"]),
            len(active_pool.rows_by_position["WR"]),
        )

    def test_active_reference_cache_preserves_membership_metrics_and_weeks(self) -> None:
        rows: list[dict[str, object]] = []
        for index in range(10):
            for week in (0, 4):
                row: dict[str, object] = {
                    "player_id": f"player-{index}",
                    "name": f"Active Test {index}",
                    "position": "WR" if index < 8 else "RB",
                    "drafted": index != 7,
                    "draft_year": 2024,
                    "as_of_week": week,
                    "prod_receptions": None if index == 6 else 20 + index + week,
                    "prod_receiving_yards": 200 + index * 10 + week * 30,
                }
                if index in {0, 6, 7, 8}:
                    row["outcome_nflverse_pfr_player_id"] = f"pfr-{index}"
                elif index == 1:
                    row["outcome_nflverse_cfb_player_id"] = "cfb-1"
                elif index == 3:
                    row["outcome_nflverse_pfr_player_id"] = "unknown-pfr"
                rows.append(row)

        registry = [
            {"name": f"Active Test {index}", "draft_year": 2024, "pfr_player_id": f"pfr-{index}"}
            for index in (0, 6, 7, 8)
        ]
        registry.extend(
            [
                {"name": "Active Test 1", "draft_year": 2024, "cfb_player_id": "cfb-1"},
                {"name": "Active Test 2", "draft_year": 2024},
                {"name": "Active Test 3", "draft_year": 2024},
                {"name": "Active Test 4", "draft_year": 2024},
                {"name": "Active Test 4", "draft_year": 2024},
            ]
        )
        evaluator = ProspectEvaluator(
            [{"name": "Benchmark", "benchmark_cohort_eligible": True}],
            [],
            model_history=rows,
            history_metadata={
                "active_drafted_players": registry,
                "active_roster_season": 2026,
                "window_start": 2017,
                "window_end": 2026,
            },
        )
        early = self._player(as_of_week=0)
        later = self._player(as_of_week=4)
        running_back = self._player(position="RB", as_of_week=0)
        receptions = MetricSpec("prod_receptions", "Receptions", category="production")
        yards = MetricSpec("prod_receiving_yards", "Yards", category="production")

        early_rows, reference = evaluator._active_model_reference_rows(early, receptions)
        self.assertEqual(early_rows, _uncached_active_reference_rows(evaluator, early, receptions))
        self.assertEqual(
            [row["name"] for row in early_rows],
            ["Active Test 0", "Active Test 1", "Active Test 2"],
        )
        self.assertIn("drafted 2017-2026", reference)
        registry_keys = evaluator._active_registry_keys()
        early_history = evaluator._aligned_model_history(early)
        early_pool = evaluator._active_model_pool(early_history)

        repeated_rows, repeated_reference = evaluator._active_model_reference_rows(early, receptions)
        yard_rows, _ = evaluator._active_model_reference_rows(early, yards)
        back_rows, _ = evaluator._active_model_reference_rows(running_back, yards)
        self.assertIs(repeated_rows, early_rows)
        self.assertEqual(repeated_reference, reference)
        self.assertEqual(yard_rows, _uncached_active_reference_rows(evaluator, early, yards))
        self.assertEqual(back_rows, _uncached_active_reference_rows(evaluator, running_back, yards))
        self.assertIn("Active Test 6", [row["name"] for row in yard_rows])
        self.assertEqual([row["name"] for row in back_rows], ["Active Test 8"])
        self.assertIs(evaluator._active_registry_keys(), registry_keys)
        self.assertIs(evaluator._active_model_pool(early_history), early_pool)
        self.assertEqual(len(evaluator._active_model_pool_cache), 1)

        later_rows, _ = evaluator._active_model_reference_rows(later, receptions)
        self.assertEqual(later_rows, _uncached_active_reference_rows(evaluator, later, receptions))
        self.assertTrue(all(row["as_of_week"] == 4 for row in later_rows))
        self.assertIsNot(later_rows, early_rows)
        self.assertEqual(len(evaluator._active_model_pool_cache), 2)

        first_cohort = evaluator._benchmark_cohort_rows(evaluator.history)
        second_cohort = evaluator._benchmark_cohort_rows(evaluator.history)
        self.assertEqual(first_cohort, second_cohort)
        self.assertIs(first_cohort[0], second_cohort[0])

    def test_college_stage_is_primary_and_combine_stage_stays_separate(self) -> None:
        benchmark_history = synthetic_history(rows_per_year=30)
        college_history = synthetic_college_roster_history()
        player = self._player(
            class_year=4,
            conference="Same Conference",
            school="Same University",
            prod_receptions=70,
            prod_receiving_yards=1_300,
            prod_receiving_tds=12,
        )
        evaluator = ProspectEvaluator(
            benchmark_history,
            [player],
            history_metadata={
                "model_stage": "combine",
                "population": "NFL Scouting Combine participants",
                "probability_kind": "conditional_on_combine_invitation",
                "probability_condition": "P(drafted | NFL Scouting Combine participant)",
            },
            model_history=college_history,
            model_metadata={
                "model_stage": "college_precombine",
                "population": "FBS roster preseason; prior-season stats",
                "probability_kind": "unconditional_next_draft",
                "probability_condition": "P(drafted next draft | FBS roster player at checkpoint)",
            },
        )
        result = evaluator.evaluate("Current Prospect", school="Same University", bootstrap=0)
        self.assertIs(result.draft_prediction, result.college_prediction)
        self.assertTrue(result.draft_prediction.available)
        self.assertEqual(result.draft_prediction.model_stage, "college_precombine")
        self.assertIsNotNone(result.draft_prediction.probability)
        self.assertEqual(result.draft_prediction.feature_coverage, 1.0)
        self.assertIsNotNone(result.combine_prediction)
        self.assertEqual(result.combine_prediction.model_stage, "combine")
        self.assertIsNone(result.combine_prediction.probability)
        self.assertNotIn(
            "At least three model features and 30% feature coverage are required.",
            result.draft_prediction.warnings,
        )
        report = render_evaluation(result)
        self.assertIn("Model stage: college pre-combine", report)
        self.assertIn("binary draft threshold: not published", report)
        self.assertIn("COMBINE-STAGE CROSS-CHECK", report)
        self.assertIn("not a second next-draft probability", report)

    def test_entry_probability_scopes_conditional_model_output(self) -> None:
        history = synthetic_history(rows_per_year=30)
        players = [
            self._player("Estimated Entrant", draft_entry_probability=0.40),
            self._player("Declared Entrant", draft_declared=True),
            self._player("Unknown Entrant"),
            self._player("Ineligible Entrant", draft_eligible=False),
        ]
        evaluator = ProspectEvaluator(
            history,
            players,
            model_history=history,
            model_metadata={
                "population": "synthetic entrants",
                "probability_kind": "conditional_on_entry",
                "probability_condition": "P(drafted | enters next draft)",
            },
        )
        estimated = evaluator.evaluate("Estimated Entrant", bootstrap=0).draft_prediction
        self.assertIsNotNone(estimated.conditional_probability)
        self.assertAlmostEqual(
            estimated.probability or -1,
            (estimated.conditional_probability or 0) * 0.40,
        )
        self.assertEqual(estimated.entry_probability, 0.40)
        self.assertIn("P(drafted next draft)", estimated.probability_scope)

        declared = evaluator.evaluate("Declared Entrant", bootstrap=0).draft_prediction
        self.assertEqual(declared.entry_probability, 1.0)
        self.assertAlmostEqual(declared.probability or -1, declared.conditional_probability or -2)

        unknown = evaluator.evaluate("Unknown Entrant", bootstrap=0).draft_prediction
        self.assertIsNone(unknown.probability)
        self.assertIsNotNone(unknown.conditional_probability)
        self.assertIsNone(unknown.entry_probability)

        ineligible = evaluator.evaluate("Ineligible Entrant", bootstrap=0).draft_prediction
        self.assertEqual(ineligible.entry_probability, 0.0)
        self.assertEqual(ineligible.probability, 0.0)
        self.assertIsNone(ineligible.projected_pick)

        combine_conditioned = ProspectEvaluator(
            history,
            [self._player("Combine-Unknown", draft_declared=True)],
            model_history=history,
            model_metadata={
                "population": "NFL Scouting Combine participants",
                "probability_kind": "conditional_on_combine_invitation",
                "probability_condition": "P(drafted | NFL Scouting Combine participant)",
            },
        ).evaluate("Combine-Unknown", bootstrap=0).draft_prediction
        self.assertEqual(combine_conditioned.entry_probability, 1.0)
        self.assertIsNone(combine_conditioned.probability)
        self.assertIsNotNone(combine_conditioned.conditional_probability)
        self.assertIn("Combine participant", combine_conditioned.conditional_scope)

    def test_board_ranking_prioritizes_next_draft_likelihood_over_profile(self) -> None:
        history = synthetic_history(rows_per_year=30)
        star_but_ineligible = self._player("Ineligible Star", draft_eligible=False)
        lower_profile_entrant = self._player(
            "Eligible Developmental",
            draft_declared=True,
            forty_s=4.60,
            vertical_in=32,
            broad_jump_in=112,
            three_cone_s=7.20,
            shuttle_s=4.45,
            prod_yards_per_route=1.4,
            trait_route_running=55,
            trait_separation=50,
        )
        evaluator = ProspectEvaluator(
            history,
            [star_but_ineligible, lower_profile_entrant],
            model_history=history,
            model_metadata={
                "population": "synthetic entrants",
                "probability_kind": "conditional_on_entry",
                "probability_condition": "P(drafted | enters next draft)",
            },
        )
        star = evaluator.evaluate("Ineligible Star", bootstrap=0)
        developmental = evaluator.evaluate("Eligible Developmental", bootstrap=0)
        self.assertGreater(star.profile_score or 0, developmental.profile_score or 100)
        self.assertEqual(evaluator.rank(bootstrap=0)[0].player["name"], "Eligible Developmental")

    def test_team_draft_access_is_withheld_for_weak_evidence(self) -> None:
        history = synthetic_history(rows_per_year=30)
        sparse = self._player(
            "Sparse Prospect",
            draft_declared=True,
            weight_lb=None,
            broad_jump_in=None,
            three_cone_s=None,
            shuttle_s=None,
            prod_yards_per_route=None,
            trait_route_running=None,
            trait_separation=None,
        )
        profiles = [
            {
                "team": "Access Team",
                "position": "WR",
                "need_score": 80,
                "timeline_score": 70,
                "draft_access_min": 20,
                "draft_access_max": 100,
            }
        ]
        evaluator = ProspectEvaluator(
            history,
            [sparse],
            model_history=history,
            model_metadata={
                "population": "synthetic entrants",
                "probability_kind": "conditional_on_entry",
                "probability_condition": "P(drafted | enters next draft)",
            },
            team_profiles=profiles,
        )
        result = evaluator.evaluate("Sparse Prospect", bootstrap=0)
        self.assertTrue(result.team_fits)
        self.assertIsNone(result.team_fits[0].draft_access_score)
        self.assertTrue(any("Team draft-access scoring was withheld" in warning for warning in result.warnings))


if __name__ == "__main__":
    unittest.main()

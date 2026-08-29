from __future__ import annotations

import unittest

from draftscope.reporting import render_player_summary


class PlayerSummaryTests(unittest.TestCase):
    @staticmethod
    def _result(*, drafted: bool = False) -> dict[str, object]:
        player: dict[str, object] = {
            "name": "Example Receiver",
            "position": "WR",
            "school": "Example State",
            "season": 2026,
            "projected_draft_year": 2027,
            "as_of_week": 4,
        }
        if drafted:
            player.update(
                {
                    "drafted": True,
                    "draft_year": 2026,
                    "draft_round": 1,
                    "draft_ovr": 12,
                    "nfl_team": "DAL",
                }
            )
        return {
            "player": player,
            "as_of_date": "2026-09-30",
            "profile_score": 58.0,
            "profile_tier": "Watch-list / developmental profile",
            "evidence_coverage": 0.42,
            "projected_pick_range": [20, 48, 96],
            "draft_prediction": {
                "available": not drafted,
                "probability": None if drafted else 0.31,
                "label": "Draft outcome known" if drafted else "Available",
                "features_used": ["height_in", "weight_lb", "prod_receiving_yards"],
                "feature_coverage": 1.0,
                "out_of_distribution_score": 2.0,
                "validation": {
                    "passes_quality_gate": True,
                    "prevalence": 0.025,
                },
            },
            "categories": [
                {
                    "category": "production",
                    "score": 84,
                    "observed_metrics": 4,
                    "expected_metrics": 13,
                },
                {
                    "category": "skills",
                    "score": None,
                    "observed_metrics": 0,
                    "expected_metrics": 7,
                },
            ],
            "benchmarks": [
                {
                    "label": "Receiving Yards",
                    "value": 1200,
                    "drafted_mean": 760,
                    "elite_mean": 850,
                    "favorable_score": 92,
                }
            ],
            "overall_comps": [
                {
                    "name": "Active Comp",
                    "similarity": 88,
                    "draft_pick": 25,
                    "draft_year": 2023,
                    "features_compared": 6,
                }
            ],
            "historical_elite_comps": [
                {
                    "name": "Elite Comp",
                    "similarity": 82,
                    "draft_pick": 9,
                    "draft_year": 2018,
                    "features_compared": 5,
                    "nfl_all_pro_selections": 2,
                    "nfl_pro_bowls": 4,
                    "measurement_source": "wikidata_biographical_measurement",
                }
            ],
            "benchmark_context": {
                "historical_career_elite_scope": (
                    "all NFL eras for Hall of Fame inductees; canonical first-team "
                    "All-Pro selections 1920–2025; Pro Bowl selections 1950–2025"
                ),
                "historical_career_elite_full_history_criteria_available": True,
                "historical_career_elite_full_history_sources_available": True,
                "historical_career_elite_source_population_complete": True,
                "historical_career_elite_full_position_coverage_complete": True,
            },
            "pick_comps": [],
            "team_fits": [
                {
                    "team": "NYG",
                    "score": 68,
                    "confidence_label": "moderate",
                    "reasons": ["thin listed room"],
                }
            ],
        }

    def test_summary_leads_with_decision_and_separates_comparison_groups(self) -> None:
        report = render_player_summary(
            self._result(),
            board_row={
                "rank": 7,
                "rank_delta": 3,
                "draft_probability_delta": 0.04,
            },
        )

        self.assertIn("Draft chance: 31%", report)
        self.assertIn("Historical position baseline: 2.5%", report)
        self.assertIn("Draft board: #7 overall | up 3 spots", report)
        self.assertIn("Overall scouting grade: withheld", report)
        self.assertIn("ACTIVE NFL COMPARISONS", report)
        self.assertIn("Active Comp", report)
        self.assertIn("CAREER-ELITE NFL COMPARISONS", report)
        self.assertIn("ALL NFL HISTORY", report)
        self.assertIn("All-Pro selections 1920–2025", report)
        self.assertIn("Elite Comp", report)
        self.assertIn("2x first-team All-Pro", report)
        self.assertIn("measurements: Wikidata biography", report)
        self.assertNotIn("Brier=", report)
        self.assertIn("When the weekly updater is run", report)
        self.assertNotIn("will automatically change", report)

    def test_unavailable_prediction_does_not_print_a_stale_positive_label(self) -> None:
        result = self._result()
        prediction = result["draft_prediction"]
        prediction["available"] = False
        prediction["probability"] = None
        prediction["label"] = "Likely draft pick within this model population"

        report = render_player_summary(result)

        self.assertIn(
            "Draft chance: unavailable — the model did not return a publishable probability",
            report,
        )
        self.assertNotIn("unavailable — Likely draft pick", report)

    def test_summary_discloses_partial_historical_position_coverage(self) -> None:
        result = self._result()
        context = result["benchmark_context"]
        context["historical_career_elite_full_position_coverage_complete"] = False
        context["historical_career_elite_coverage_note"] = (
            "3 qualifying source rows could not be assigned a supported comparison position."
        )

        report = render_player_summary(result)

        self.assertIn("ALL-HISTORY SOURCES — PARTIAL POSITION COVERAGE", report)
        self.assertIn("3 qualifying source rows", report)

    def test_summary_discloses_partial_historical_source_coverage(self) -> None:
        result = self._result()
        context = result["benchmark_context"]
        context["historical_career_elite_full_position_coverage_complete"] = False
        context["historical_career_elite_full_history_criteria_available"] = False
        context["historical_career_elite_source_population_complete"] = False
        context["historical_career_elite_coverage_note"] = (
            "Some historical Pro Bowl seasons do not publish a complete annual roster."
        )

        report = render_player_summary(result)

        self.assertIn("ALL-HISTORY SOURCES — PARTIAL SOURCE COVERAGE", report)
        self.assertIn("do not publish a complete annual roster", report)

    def test_known_recent_draftee_shows_actual_result_not_probability(self) -> None:
        report = render_player_summary(self._result(drafted=True))

        self.assertIn("2026 result: selected #12 | Round 1 | by DAL", report)
        self.assertIn("Draft chance: not shown because the draft outcome is already known", report)
        self.assertNotIn("Draft chance: 31%", report)


if __name__ == "__main__":
    unittest.main()

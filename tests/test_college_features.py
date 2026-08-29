from __future__ import annotations

import unittest

from draftscope.data_sources import (
    _clear_cfbd_generated_features,
    _merge_overview_features,
)
from draftscope.historical_college import _derive_production


class CollegeFeatureParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.values = {
            ("passing", "c_att"): "20/30",
            ("passing", "yds"): 280,
            ("passing", "td"): 3,
            ("passing", "int"): 1,
            ("rushing", "car"): 5,
            ("rushing", "yds"): 25,
            ("rushing", "td"): 1,
            ("receiving", "rec"): 4,
            ("receiving", "yds"): 50,
            ("receiving", "td"): 1,
            ("defensive", "sacks"): 2,
            ("defensive", "tfl"): 3,
            ("defensive", "tot"): 8,
            ("defensive", "solo"): 5,
            ("defensive", "pd"): 2,
            ("defensive", "qb_hur"): 4,
            # CFBD publishes defensive interceptions in this separate category.
            ("interceptions", "int"): 1,
            ("kicking", "fga"): 5,
            ("kicking", "fgm"): 4,
            ("kicking", "xpa"): 3,
            ("kicking", "xpm"): 3,
            ("kicking", "pts"): 15,
            ("kicking", "long"): 52,
            ("punting", "no"): 4,
            ("punting", "yds"): 180,
            ("punting", "in_20"): 2,
            ("punting", "tb"): 1,
            ("kickreturns", "no"): 2,
            ("kickreturns", "yds"): 50,
            ("kickreturns", "td"): 0,
            ("puntreturns", "no"): 3,
            ("puntreturns", "yds"): 27,
            ("puntreturns", "td"): 0,
        }

    def _overview(self, *, games: int = 0) -> dict[str, object]:
        categories = []
        for category in sorted({name for name, _stat in self.values}):
            categories.append(
                {
                    "name": category,
                    "stats": [
                        {"name": stat, "value": value}
                        for (name, stat), value in self.values.items()
                        if name == category
                    ],
                }
            )
        return {"games": games, "boxScoreStats": {"categories": categories}}

    def test_historical_and_live_paths_share_every_box_score_derivative(self) -> None:
        historical = _derive_production(self.values)
        live: dict[str, object] = {}
        _merge_overview_features(live, self._overview())

        self.assertEqual(
            historical,
            {key: live[key] for key in historical},
        )
        self.assertEqual(live["prod_pass_completions"], 20.0)
        self.assertEqual(live["prod_pass_interceptions"], 1.0)
        self.assertEqual(live["prod_interceptions"], 1.0)
        self.assertEqual(live["prod_field_goal_attempts"], 5.0)
        self.assertEqual(live["prod_field_goals_made"], 4.0)
        self.assertAlmostEqual(float(live["prod_field_goal_pct"]), 0.8)
        self.assertEqual(live["prod_punts"], 4.0)
        self.assertEqual(live["prod_yards_per_punt"], 45.0)
        self.assertEqual(live["prod_kick_return_yards"], 50.0)

    def test_game_rates_use_the_same_separate_interception_category(self) -> None:
        live: dict[str, object] = {}
        _merge_overview_features(live, self._overview(games=2))

        self.assertEqual(live["prod_sacks_per_game"], 1.0)
        self.assertEqual(live["prod_tfl_per_game"], 1.5)
        self.assertEqual(live["prod_takeaways_per_game"], 0.5)
        self.assertEqual(live["prod_passes_defended_per_game"], 1.0)

    def test_refresh_clear_owns_all_shared_outputs_but_not_manual_advanced_stats(self) -> None:
        record = {
            "cfbd_games": 4,
            "observed_interception_rate": 0.02,
            "prod_pass_completions": 100,
            "prod_pass_interceptions": 2,
            "prod_interceptions": 3,
            "prod_field_goal_attempts": 10,
            "prod_punts": 20,
            "prod_kick_return_yards": 200,
            "prod_yards_per_route": 3.2,
        }

        _clear_cfbd_generated_features(record)

        self.assertEqual(record, {"prod_yards_per_route": 3.2})


if __name__ == "__main__":
    unittest.main()

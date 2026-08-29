from __future__ import annotations

import unittest

from draftscope.recruiting import build_recruiting_index, match_recruiting_profile


class RecruitingFeatureTests(unittest.TestCase):
    def test_stable_recruit_id_match_is_preferred_and_normalized(self) -> None:
        index = build_recruiting_index(
            [
                {
                    "id": "r1",
                    "athleteId": "a1",
                    "year": 2022,
                    "name": "Correct Player",
                    "rating": 0.9234,
                    "stars": 4,
                    "ranking": 188,
                    "recruitType": "HighSchool",
                }
            ]
        )
        feature = match_recruiting_profile(
            {
                "id": "different-athlete-id",
                "firstName": "Correct",
                "lastName": "Player",
                "recruitIds": ["r1"],
            },
            index,
            roster_season=2026,
        )
        self.assertEqual(feature["recruit_match_method"], "recruit_id")
        self.assertEqual(feature["recruit_rating"], 0.9234)
        self.assertEqual(feature["recruit_stars"], 4)
        self.assertTrue(feature["recruit_name_agrees"])

    def test_athlete_id_is_safe_fallback_but_name_only_never_matches(self) -> None:
        index = build_recruiting_index(
            [
                {
                    "id": "r2",
                    "athleteId": "a2",
                    "year": 2023,
                    "name": "Same Name",
                    "rating": 0.81,
                    "stars": 3,
                    "ranking": 900,
                }
            ]
        )
        matched = match_recruiting_profile(
            {"id": "a2", "name": "Same Name"}, index, roster_season=2025
        )
        self.assertEqual(matched["recruit_match_method"], "athlete_id")
        self.assertEqual(
            match_recruiting_profile({"id": "other", "name": "Same Name"}, index),
            {},
        )

    def test_future_and_invalid_source_values_are_not_used(self) -> None:
        index = build_recruiting_index(
            [
                {"id": "bad", "athleteId": "a3", "year": 2030, "rating": 9, "stars": 7, "ranking": 0},
            ]
        )
        self.assertEqual(
            match_recruiting_profile({"id": "a3"}, index, roster_season=2026),
            {},
        )
        self.assertEqual(index.audit["invalid_recruit_rating_values_set_missing"], 1)
        self.assertEqual(index.audit["invalid_recruit_stars_values_set_missing"], 1)
        self.assertEqual(index.audit["invalid_recruit_rank_values_set_missing"], 1)


if __name__ == "__main__":
    unittest.main()

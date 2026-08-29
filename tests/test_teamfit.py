from __future__ import annotations

import unittest

from draftscope.data_sources import build_nfl_team_profiles, merge_team_profiles
from draftscope.teamfit import rank_team_fits


def _source_rows() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    teams = [f"T{index:02d}" for index in range(32)]
    roster: list[dict[str, object]] = []
    for index, team in enumerate(teams):
        room_size = 1 if team == "T00" else (4 if team == "T01" else 3)
        for player_index in range(room_size):
            if team == "T00":
                birth_date, experience = "1995-01-01", 9
            else:
                birth_date, experience = "2002-01-01", 2
            roster.append(
                {
                    "season": 2026,
                    "week": 1,
                    "team": team,
                    "position": "WR",
                    "status": "ACT",
                    "gsis_id": f"{team}-wr-{player_index}",
                    "full_name": f"Receiver {index} {player_index}",
                    "birth_date": birth_date,
                    "years_exp": experience,
                }
            )

    draft: list[dict[str, object]] = []
    for year in (2024, 2025, 2026):
        for pick in range(1, 251):
            # T01 invested an early selection at WR in each recent draft. T00
            # did not. The remaining rows make each source class complete.
            is_t01_receiver = pick == 10
            draft.append(
                {
                    "season": year,
                    "pick": pick,
                    "team": "T01" if is_t01_receiver else teams[pick % len(teams)],
                    "position": "WR" if is_t01_receiver else "QB",
                }
            )
    return roster, draft


class TeamProfileEvidenceTests(unittest.TestCase):
    def test_automatic_profiles_use_room_transition_and_original_team_draft_evidence(self) -> None:
        roster, draft = _source_rows()
        profiles = build_nfl_team_profiles(roster, draft, season=2026)
        wr = {
            row["team"]: row
            for row in profiles
            if row["position"] == "WR"
        }
        thin_old = wr["T00"]
        invested = wr["T01"]

        self.assertEqual(thin_old["room_count"], 1)
        self.assertEqual(thin_old["league_room_median"], 3)
        self.assertEqual(thin_old["recent_position_picks"], 0)
        self.assertEqual(invested["recent_position_picks"], 3)
        self.assertEqual(invested["best_recent_position_pick"], 10)
        self.assertGreater(
            thin_old["lack_recent_draft_investment"],
            invested["lack_recent_draft_investment"],
        )
        self.assertGreater(thin_old["need_score"], invested["need_score"])
        self.assertGreater(thin_old["need_evidence_coverage"], 0.95)
        self.assertGreater(thin_old["need_confidence"], 0.75)
        self.assertIsNone(thin_old["draft_access_min"])
        self.assertIn("future_pick_ownership_not_in_source", thin_old["draft_access_source"])

    def test_fit_exposes_coverage_and_suppresses_unsupported_components(self) -> None:
        roster, draft = _source_rows()
        profiles = build_nfl_team_profiles(roster, draft, season=2026)
        wr_profiles = [
            row for row in profiles
            if row["position"] == "WR" and row["team"] in {"T00", "T01"}
        ]
        fits = rank_team_fits(
            {"name": "Prospect", "position": "WR"},
            wr_profiles,
            pick_range=None,
        )

        self.assertEqual(fits[0].team, "T00")
        self.assertIsNone(fits[0].scheme_score)
        self.assertIsNone(fits[0].draft_access_score)
        self.assertIn("scheme", fits[0].unsupported_components)
        self.assertIn("access", fits[0].unsupported_components)
        self.assertGreater(fits[0].evidence_coverage, 0.45)
        self.assertGreater(fits[0].confidence, 0.70)
        self.assertTrue(any("listed room" in reason for reason in fits[0].reasons))
        self.assertTrue(any("recent investment" in reason for reason in fits[0].reasons))

    def test_manual_overrides_are_labeled_and_do_not_inherit_automatic_confidence(self) -> None:
        automatic = [
            {
                "team": "T00",
                "position": "WR",
                "need_score": 40,
                "need_confidence": 0.95,
                "need_evidence_coverage": 0.90,
                "profile_source": "nflverse_roster_and_draft_derived",
            }
        ]
        merged = merge_team_profiles(
            automatic,
            [
                {
                    "team": "T00",
                    "position": "WR",
                    "need_score": 88,
                    "draft_access_min": 20,
                    "draft_access_max": 80,
                }
            ],
        )[0]

        self.assertEqual(merged["need_source"], "manual_review")
        self.assertEqual(merged["need_confidence"], 0.75)
        self.assertEqual(merged["need_evidence_coverage"], 1.0)
        self.assertEqual(merged["draft_access_source"], "manual_review")
        self.assertEqual(merged["draft_access_confidence"], 0.75)
        self.assertEqual(merged["profile_source"], "manual_plus_nflverse")


if __name__ == "__main__":
    unittest.main()

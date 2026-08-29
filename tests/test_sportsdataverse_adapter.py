from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from draftscope.historical_college import build_historical_college_training_data
from draftscope.records import DataError
from draftscope.sportsdataverse_adapter import (
    SportsDataverseHistoricalClient,
    _read_csv,
)


class FakeBulkClient:
    def load_teams(self, year, **_kwargs):
        return [
            {
                "team_id": "1",
                "is_fbs": "true",
                "school": "Alpha",
                "location": "Alpha",
                "alt_name1": "Alpha University",
                "conference_name": "Test Conference",
            },
            {"team_id": "2", "is_fbs": "false", "school": "FCS Team"},
        ]

    def load_roster(self, year, **_kwargs):
        return [
            {
                "athlete_id": "101",
                "division": "fbs",
                "team_id": "1",
                "full_name": "Free Prospect",
                "first_name": "Free",
                "last_name": "Prospect",
                "team_location": "Alpha",
                "position_abbreviation": "WR",
                "height": "74",
                "weight": "205",
                "experience_years": "4",
                "date_of_birth": "2002-01-02",
            },
            {
                "athlete_id": "202",
                "division": "fcs",
                "team_id": "2",
                "full_name": "Out Of Scope",
                "position_abbreviation": "WR",
            },
        ]

    def load_schedule(self, year, **_kwargs):
        return [
            {"game_id": "g1", "week": "1", "status": "STATUS_FINAL"},
            {"game_id": "g2", "week": "2", "status": "STATUS_FINAL"},
        ]

    def load_player_box(self, year, **_kwargs):
        return [
            {
                "game_id": "g1",
                "season": str(year),
                "athlete_id": "101",
                "athlete_name": "Free Prospect",
                "team_id": "1",
                "category": "receiving",
                "receptions": "4",
                "receivingYards": "60",
                "receivingTouchdowns": "1",
            },
            {
                "game_id": "g2",
                "season": str(year),
                "athlete_id": "101",
                "athlete_name": "Free Prospect",
                "team_id": "1",
                "category": "receiving",
                "receptions": "5",
                "receivingYards": "90",
                "receivingTouchdowns": "1",
            },
        ]


class SportsDataverseAdapterTests(unittest.TestCase):
    def test_nflverse_cache_rejects_oversize_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "players.csv"
            path.write_bytes(b"12345")
            with patch(
                "draftscope.sportsdataverse_adapter._MAX_NFLVERSE_CACHE_BYTES", 4
            ), self.assertRaisesRegex(DataError, "is 5 bytes; limit is 4"):
                _read_csv(path)

    def test_nflverse_cache_caps_csv_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "players.csv"
            path.write_text("id\n1\n2\n", encoding="utf-8")
            with patch(
                "draftscope.sportsdataverse_adapter._MAX_NFLVERSE_CACHE_ROWS", 1
            ), self.assertRaisesRegex(DataError, "exceeded 1 data rows"):
                _read_csv(path)

    def test_translates_fbs_roster_teams_and_week_bounded_stats(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adapter = SportsDataverseHistoricalClient(
                FakeBulkClient(), nflverse_cache_dir=temporary
            )
            roster = adapter.get("/roster", year=2024)
            teams = adapter.get("/teams/fbs", year=2024)
            stats = adapter.get("/stats/player/season", year=2024, endWeek=1)

        self.assertEqual([row["id"] for row in roster], ["101"])
        self.assertEqual(roster[0]["year"], 4)
        self.assertIn("Alpha University", teams[0]["alternateNames"])
        receiving_yards = next(
            row["stat"]
            for row in stats
            if row["category"] == "receiving" and row["statType"] == "yds"
        )
        self.assertEqual(receiving_yards, 60.0)

    def test_draft_crosswalk_uses_nflverse_espn_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adapter = SportsDataverseHistoricalClient(
                FakeBulkClient(), nflverse_cache_dir=temporary
            )
            adapter._draft_rows = [
                {
                    "season": "2025",
                    "pick": "1",
                    "round": "1",
                    "team": "AAA",
                    "gsis_id": "00-1",
                    "pfr_player_id": "FreePr00",
                    "pfr_player_name": "Free Prospect",
                    "college": "Alpha",
                }
            ]
            adapter._players = [
                {"gsis_id": "00-1", "pfr_id": "FreePr00", "espn_id": "101"}
            ]
            picks = adapter.get("/draft/picks", year=2025)

        self.assertEqual(picks[0]["collegeAthleteId"], "101")

    def test_strict_history_accepts_sportsdataverse_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adapter = SportsDataverseHistoricalClient(
                FakeBulkClient(), nflverse_cache_dir=temporary
            )
            adapter._draft_rows = [
                {
                    "season": "2025",
                    "pick": "1",
                    "round": "1",
                    "team": "AAA",
                    "gsis_id": "00-1",
                    "pfr_player_id": "FreePr00",
                    "pfr_player_name": "Free Prospect",
                    "college": "Alpha",
                }
            ]
            adapter._players = [
                {"gsis_id": "00-1", "pfr_id": "FreePr00", "espn_id": "101"}
            ]
            nfl_rows = [
                {
                    "season": "2025",
                    "pick": "1",
                    "round": "1",
                    "team": "AAA",
                    "gsis_id": "00-1",
                    "pfr_player_id": "FreePr00",
                    "cfb_player_id": "free-prospect-1",
                    "pfr_player_name": "Free Prospect",
                    "college": "Alpha",
                }
            ]
            rows, metadata = build_historical_college_training_data(
                adapter,
                college_seasons=(2024,),
                nflverse_draft_rows=nfl_rows,
                as_of_week=1,
                strict=True,
            )

        self.assertEqual(metadata["college_data_provider"], "sportsdataverse")
        self.assertEqual(metadata["quality_gate_status"], "pass")
        self.assertEqual(rows[0]["age_source"], "sportsdataverse_roster_date_of_birth")
        self.assertEqual(rows[0]["measurement_source"], "sportsdataverse_espn_school_roster")
        self.assertTrue(rows[0]["drafted"])


if __name__ == "__main__":
    unittest.main()

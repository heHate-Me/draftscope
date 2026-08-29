from __future__ import annotations

import csv
import gzip
import io
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError, URLError

from draftscope.sportsdataverse import (
    SportsDataverseClient,
    _parse_csv_bytes,
    aggregate_player_box_by_week,
    aggregate_player_box_stats,
    completed_week,
    discover_sportsdataverse_candidates,
    sportsdataverse_urls,
)
from draftscope.records import DataError


def _csv_bytes(rows: list[dict[str, object]], *, compressed: bool = False) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    payload = output.getvalue().encode("utf-8")
    return gzip.compress(payload) if compressed else payload


class _Response(io.BytesIO):
    pass


class SportsDataverseTests(unittest.TestCase):
    def test_bulk_loader_supports_gzip_plain_cache_and_unavailable_partition(self) -> None:
        roster_url = sportsdataverse_urls("roster", 2025)[0]
        schedule_url = sportsdataverse_urls("schedule", 2025)[0]
        payloads = {
            roster_url: _csv_bytes(
                [{"season": 2025, "athlete_id": 1, "full_name": "One Player"}],
                compressed=True,
            ),
            schedule_url: _csv_bytes(
                [{"game_id": 10, "season": 2025, "week": 1, "status": "STATUS_FINAL"}]
            ),
        }

        def opener(request, *, timeout):
            url = request.full_url
            if url not in payloads:
                raise HTTPError(url, 404, "not found", None, None)
            return _Response(payloads[url])

        with tempfile.TemporaryDirectory() as directory:
            client = SportsDataverseClient(directory, opener=opener)
            self.assertEqual(client.load_roster(2025)[0]["full_name"], "One Player")
            self.assertEqual(client.load_schedule(2025)[0]["status"], "STATUS_FINAL")
            self.assertEqual(client.current_week(year=2025, refresh=False), 1)

            def offline(request, *, timeout):
                raise URLError("offline")

            client._opener = offline
            self.assertEqual(client.load_roster(2025, refresh=True)[0]["athlete_id"], "1")
            self.assertEqual(client.last_status["roster:2025"], "stale_cache")
            self.assertEqual(
                client.load_player_box(2026, refresh=True, allow_unavailable=True),
                [],
            )
            self.assertEqual(client.last_status["player_box:2026"], "unavailable")
            self.assertTrue(
                (Path(directory) / "sportsdataverse" / "roster" / "cfb_rosters_2025.csv.gz").exists()
            )

    def test_download_limit_rejects_oversize_response_without_caching(self) -> None:
        def opener(request, *, timeout):
            del request, timeout
            return _Response(b"game_id,status\n1,FINAL\n")

        with tempfile.TemporaryDirectory() as directory:
            client = SportsDataverseClient(
                directory,
                opener=opener,
                max_download_bytes=8,
            )
            with self.assertRaisesRegex(DataError, "exceeded 8 bytes"):
                client.load_schedule(2025)
            self.assertFalse(
                Path(directory, "sportsdataverse", "schedule", "cfb_schedule_2025.csv").exists()
            )

    def test_gzip_expansion_and_row_materialization_are_bounded(self) -> None:
        compressed = gzip.compress(b"name\n" + b"x" * 100)
        with self.assertRaisesRegex(DataError, "exceeded 32 bytes"):
            _parse_csv_bytes(
                compressed,
                source="fixture",
                max_compressed_bytes=1024,
                max_decompressed_bytes=32,
            )

        with self.assertRaisesRegex(DataError, "exceeded 1 rows"):
            _parse_csv_bytes(
                b"name\none\ntwo\n",
                source="fixture",
                max_rows=1,
            )

    def test_completed_week_requires_explicit_final_status(self) -> None:
        schedule = [
            {"game_id": "a", "week": "1", "status": "STATUS_FINAL"},
            {"game_id": "b", "week": "2", "status": "STATUS_SCHEDULED"},
            {"game_id": "c", "week": "3", "status": "completed"},
        ]
        self.assertEqual(completed_week(schedule), 3)
        self.assertEqual(completed_week(schedule[:2]), 1)
        self.assertEqual(completed_week([]), 0)

    def test_player_box_aggregation_is_player_week_bounded_and_category_aware(self) -> None:
        schedule = [
            {"game_id": "g1", "week": "1", "status": "STATUS_FINAL"},
            {"game_id": "g2", "week": "2", "status": "STATUS_SCHEDULED"},
            {"game_id": "g3", "week": "3", "status": "STATUS_FINAL"},
        ]
        rows = [
            {
                "athlete_id": "q1",
                "athlete_name": "Quarter Back",
                "team_id": "10",
                "season": "2026",
                "game_id": "g1",
                "category": "passing",
                "stat_1": "20/30",
                "stat_2": "300",
                "stat_4": "3",
                "stat_5": "1",
            },
            {
                "athlete_id": "q1",
                "athlete_name": "Quarter Back",
                "team_id": "10",
                "season": "2026",
                "game_id": "g1",
                "category": "rushing",
                "rushingAttempts": "8",
                "rushingYards": "40",
                "rushingTouchdowns": "1",
            },
            {
                "athlete_id": "q1",
                "athlete_name": "Quarter Back",
                "team_id": "10",
                "season": "2026",
                "game_id": "g2",
                "category": "passing",
                "stat_1": "99/99",
                "stat_2": "9999",
                "stat_4": "9",
                "stat_5": "0",
            },
            {
                "athlete_id": "q1",
                "athlete_name": "Quarter Back",
                "team_id": "10",
                "season": "2026",
                "game_id": "g3",
                "category": "passing",
                "stat_1": "10/20",
                "stat_2": "100",
                "stat_4": "1",
                "stat_5": "2",
            },
            {
                "athlete_id": "d1",
                "athlete_name": "Defender",
                "team_id": "10",
                "season": "2026",
                "game_id": "g1",
                "category": "defensive",
                "totalTackles": "7",
                "soloTackles": "4",
                "sacks": "2",
                "tacklesForLoss": "3",
                "passesDefended": "1",
                "hurries": "2",
            },
            {
                "athlete_id": "d1",
                "athlete_name": "Defender",
                "team_id": "10",
                "season": "2026",
                "game_id": "g1",
                "category": "interceptions",
                "interceptions": "1",
            },
        ]
        weekly = aggregate_player_box_by_week(rows, schedule, as_of_week=1)
        quarterback = next(row for row in weekly if row["athlete_id"] == "q1")
        self.assertEqual(quarterback["games"], 1)
        self.assertEqual(quarterback["production"]["prod_pass_yards"], 300)
        self.assertEqual(quarterback["production"]["prod_rush_yards"], 40)

        totals = aggregate_player_box_stats(rows, schedule, as_of_week=1)
        self.assertEqual(totals["q1"]["production"]["prod_pass_attempts"], 30)
        self.assertEqual(totals["d1"]["production"]["prod_sacks_per_game"], 2)
        self.assertEqual(totals["d1"]["production"]["prod_interceptions"], 1)

        through_three = aggregate_player_box_stats(rows, schedule, as_of_week=3)
        self.assertEqual(through_three["q1"]["games"], 2)
        self.assertEqual(through_three["q1"]["production"]["prod_pass_yards"], 400)

    def test_discovery_ranks_and_limits_each_position_with_preseason_prior(self) -> None:
        class FakeClient:
            def load_schedule(self, season, **kwargs):
                if season == 2026:
                    return [{"game_id": "future", "week": 1, "status": "STATUS_SCHEDULED"}]
                return [{"game_id": "played", "week": 15, "status": "STATUS_FINAL"}]

            def load_roster(self, season, **kwargs):
                return [
                    {
                        "season": season,
                        "athlete_id": "w1",
                        "full_name": "Top Receiver",
                        "position_abbreviation": "WR",
                        "team_id": "1",
                        "team_location": "New U",
                        "division": "fbs",
                        "height": "74",
                        "weight": "205",
                        "experience_abbreviation": "JR",
                        "active": "true",
                    },
                    {
                        "season": season,
                        "athlete_id": "w2",
                        "full_name": "Other Receiver",
                        "position_abbreviation": "WR",
                        "team_id": "1",
                        "team_location": "New U",
                        "division": "fbs",
                        "height": "71",
                        "weight": "185",
                        "experience_abbreviation": "SO",
                        "active": "true",
                    },
                    {
                        "season": season,
                        "athlete_id": "t1",
                        "full_name": "Senior Tackle",
                        "position_abbreviation": "OT",
                        "team_id": "1",
                        "team_location": "New U",
                        "division": "fbs",
                        "height": "78",
                        "weight": "320",
                        "experience_abbreviation": "SR",
                        "active": "true",
                    },
                ]

            def load_teams(self, season, **kwargs):
                return [
                    {
                        "team_id": "1",
                        "school": "New U",
                        "classification": "fbs",
                        "cfbd_conference": "X",
                    }
                ]

            def load_player_box(self, season, **kwargs):
                self.stats_season = season
                return [
                    {
                        "athlete_id": "w1",
                        "athlete_name": "Top Receiver",
                        "team_id": "1",
                        "season": str(season),
                        "game_id": "played",
                        "category": "receiving",
                        "receptions": "10",
                        "receivingYards": "200",
                        "receivingTouchdowns": "2",
                    },
                    {
                        "athlete_id": "w2",
                        "athlete_name": "Other Receiver",
                        "team_id": "1",
                        "season": str(season),
                        "game_id": "played",
                        "category": "receiving",
                        "receptions": "5",
                        "receivingYards": "30",
                        "receivingTouchdowns": "0",
                    },
                ]

        client = FakeClient()
        candidates = discover_sportsdataverse_candidates(
            client,
            season=2026,
            per_position=1,
            preseason_prior_year=True,
        )
        self.assertEqual(client.stats_season, 2025)
        self.assertEqual({row["player_id"] for row in candidates}, {"espn:w1", "espn:t1"})
        receiver = next(row for row in candidates if row["position"] == "WR")
        self.assertEqual(receiver["prod_receiving_yards"], 200)
        self.assertEqual(receiver["production_season"], 2025)
        self.assertEqual(receiver["as_of_week"], 0)
        self.assertTrue(receiver["discovery_preseason_prior_year"])
        tackle = next(row for row in candidates if row["position"] == "OT")
        self.assertFalse(tackle["has_recorded_stats"])

    def test_missing_current_roster_is_graceful(self) -> None:
        class EmptyClient:
            def load_schedule(self, season, **kwargs):
                return []

            def load_roster(self, season, **kwargs):
                return []

        self.assertEqual(
            discover_sportsdataverse_candidates(EmptyClient(), season=2026), []
        )


if __name__ == "__main__":
    unittest.main()

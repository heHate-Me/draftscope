from __future__ import annotations

import gzip
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from draftscope.nflverse_free import (
    build_nflverse_team_enrichment,
    _download_optional,
    _read_gzip_csv,
    load_nflverse_team_enrichment,
    merge_nflverse_team_enrichment,
)
from draftscope.records import DataError


def _depth(
    team: str,
    player_id: str,
    name: str,
    *,
    rank: int = 1,
    timestamp: str = "2026-08-26T07:41:22Z",
    position: str = "WR",
    scheme: str = "3WR 1TE",
) -> dict[str, object]:
    return {
        "dt": timestamp,
        "team": team,
        "player_name": name,
        "gsis_id": player_id,
        "espn_id": "",
        "pos_abb": position,
        "pos_rank": rank,
        "pos_grp": scheme,
    }


def _stats(
    player_id: str,
    team: str,
    *,
    receiving_yards: int,
    season: int = 2025,
    week: int = 18,
) -> dict[str, object]:
    return {
        "player_id": player_id,
        "season": season,
        "week": week,
        "season_type": "REG",
        "game_id": f"{season}_{week}_{team}_{player_id}",
        "team": team,
        "position": "WR",
        "receptions": 5,
        "receiving_yards": receiving_yards,
        "receiving_tds": 1 if receiving_yards >= 100 else 0,
    }


def _contract(
    team: str,
    player: str,
    *,
    year_signed: int,
    years: int,
) -> dict[str, object]:
    return {
        "player": player,
        "position": "WR",
        "team": team,
        "is_active": "TRUE",
        "year_signed": year_signed,
        "years": years,
    }


class NflverseFreeTeamContextTests(unittest.TestCase):
    def test_gzip_csv_caps_decompressed_bytes(self) -> None:
        payload = b"dt,team\n2026-08-26,BUF\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.csv.gz"
            path.write_bytes(gzip.compress(payload))
            with patch(
                "draftscope.nflverse_free._MAX_NFLVERSE_DECOMPRESSED_BYTES",
                len(payload) - 1,
            ):
                with self.assertRaisesRegex(DataError, "Decompressed .* exceeded"):
                    list(_read_gzip_csv(path, required={"dt", "team"}))

    def test_gzip_csv_caps_data_rows(self) -> None:
        payload = b"dt,team\n2026-08-25,BUF\n2026-08-26,MIA\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.csv.gz"
            path.write_bytes(gzip.compress(payload))
            with patch("draftscope.nflverse_free._MAX_NFLVERSE_CSV_ROWS", 1):
                with self.assertRaisesRegex(DataError, "exceeded 1 data rows"):
                    list(_read_gzip_csv(path, required={"dt", "team"}))

    def test_invalid_download_does_not_replace_valid_gzip_cache(self) -> None:
        cached = gzip.compress(b"dt,team\n2026-08-26,BUF\n")
        response = io.BytesIO(b"<html>upstream error</html>")
        response.headers = {"Content-Type": "text/html"}
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "feed.csv.gz"
            destination.write_bytes(cached)
            with patch("draftscope.nflverse_free.urlopen", return_value=response):
                path, metadata = _download_optional(
                    "https://example.test/feed.csv.gz",
                    destination,
                    refresh=True,
                    required={"dt", "team"},
                )

            self.assertEqual(path, destination)
            self.assertEqual(metadata["status"], "cached_after_invalid_download")
            self.assertIn("invalid gzip CSV", metadata["error"])
            self.assertEqual(destination.read_bytes(), cached)

    def test_invalid_download_without_cache_is_rejected(self) -> None:
        response = io.BytesIO(b"<html>upstream error</html>")
        response.headers = {"Content-Type": "text/html"}
        with tempfile.TemporaryDirectory() as directory, patch(
            "draftscope.nflverse_free.urlopen", return_value=response
        ):
            destination = Path(directory) / "feed.csv.gz"
            path, metadata = _download_optional(
                "https://example.test/feed.csv.gz",
                destination,
                refresh=True,
                required={"dt", "team"},
            )

            self.assertIsNone(path)
            self.assertEqual(metadata["status"], "unavailable")
            self.assertFalse(destination.exists())

    def test_optional_download_rejects_announced_oversize_response(self) -> None:
        response = io.BytesIO(b"x")
        response.headers = {"Content-Length": "5"}
        with tempfile.TemporaryDirectory() as directory, patch(
            "draftscope.nflverse_free._MAX_NFLVERSE_DOWNLOAD_BYTES", 4
        ), patch("draftscope.nflverse_free.urlopen", return_value=response):
            path, metadata = _download_optional(
                "https://example.test/feed.csv.gz",
                Path(directory) / "feed.csv.gz",
                refresh=True,
            )

        self.assertIsNone(path)
        self.assertEqual(metadata["status"], "unavailable")
        self.assertIn("announced 5 bytes", metadata["error"])

    def test_optional_download_stream_limit_preserves_existing_cache(self) -> None:
        response = io.BytesIO(b"12345")
        response.headers = {}
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "feed.csv.gz"
            destination.write_bytes(b"cached")
            with patch(
                "draftscope.nflverse_free._MAX_NFLVERSE_DOWNLOAD_BYTES", 4
            ), patch("draftscope.nflverse_free.urlopen", return_value=response):
                path, metadata = _download_optional(
                    "https://example.test/feed.csv.gz",
                    destination,
                    refresh=True,
                )

            self.assertEqual(path, destination)
            self.assertEqual(metadata["status"], "cached_after_error")
            self.assertIn("exceeded 4 bytes", metadata["error"])
            self.assertEqual(destination.read_bytes(), b"cached")

    def test_depth_and_prior_stats_create_relative_starter_opportunity(self) -> None:
        depth = [
            _depth("AAA", "old-a", "Old A", timestamp="2026-08-20T00:00:00Z"),
            _depth("AAA", "a", "Starter A"),
            _depth("BBB", "b", "Starter B"),
        ]
        stats = [
            _stats("a", "AAA", receiving_yards=20),
            _stats("b", "BBB", receiving_yards=140),
        ]

        profiles = build_nflverse_team_enrichment(
            depth,
            stats,
            [],
            season=2026,
        )
        by_team = {row["team"]: row for row in profiles}

        self.assertEqual(by_team["AAA"]["depth_chart_room_count"], 1)
        self.assertNotIn("Base 3-4 D", by_team["AAA"]["scheme"])
        self.assertGreater(
            by_team["AAA"]["starter_quality_need_score"],
            by_team["BBB"]["starter_quality_need_score"],
        )
        self.assertEqual(
            by_team["AAA"]["starter_quality_evidence_coverage"], 0.9
        )
        self.assertEqual(
            by_team["AAA"]["starter_quality_evidence_seasons"], "2025"
        )

    def test_current_week_blends_with_prior_and_evidence_matures(self) -> None:
        depth = [_depth("AAA", "a", "Starter A"), _depth("BBB", "b", "Starter B")]
        prior = [_stats("a", "AAA", receiving_yards=20), _stats("b", "BBB", receiving_yards=140)]
        early_current = [
            _stats("a", "AAA", receiving_yards=200, season=2026, week=1),
            _stats("b", "BBB", receiving_yards=10, season=2026, week=1),
        ]
        late_current = [
            _stats("a", "AAA", receiving_yards=200, season=2026, week=8),
            _stats("b", "BBB", receiving_yards=10, season=2026, week=8),
        ]

        early = build_nflverse_team_enrichment(
            depth, prior + early_current, [], season=2026
        )
        late = build_nflverse_team_enrichment(
            depth, prior + late_current, [], season=2026
        )
        early_by_team = {row["team"]: row for row in early}
        late_by_team = {row["team"]: row for row in late}

        self.assertLess(
            early_by_team["AAA"]["starter_quality_evidence_coverage"],
            late_by_team["AAA"]["starter_quality_evidence_coverage"],
        )
        self.assertGreater(
            early_by_team["AAA"]["starter_quality_need_score"],
            late_by_team["AAA"]["starter_quality_need_score"],
        )

    def test_stale_contract_release_is_never_used_for_2026(self) -> None:
        profiles = build_nflverse_team_enrichment(
            [_depth("GB", "a", "Starter A")],
            [_stats("a", "GB", receiving_yards=80)],
            [_contract("Packers", "Starter A", year_signed=2022, years=5)],
            season=2026,
        )

        self.assertIsNone(profiles[0]["contract_need_score"])
        self.assertEqual(profiles[0]["contract_evidence_coverage"], 0.0)
        self.assertIn("stale_2022", profiles[0]["contract_source"])
        self.assertIn("withheld", profiles[0]["notes"])

    def test_three_four_front_maps_ends_inside_and_outside_backers_to_edge(self) -> None:
        profiles = build_nflverse_team_enrichment(
            [
                _depth(
                    "AAA",
                    "end",
                    "Three Four End",
                    position="LDE",
                    scheme="Base 3-4 D",
                ),
                _depth(
                    "AAA",
                    "backer",
                    "Three Four Edge",
                    position="SLB",
                    scheme="Base 3-4 D",
                ),
            ],
            [],
            [],
            season=2026,
        )

        by_position = {row["position"]: row for row in profiles}
        self.assertEqual(by_position["IDL"]["depth_chart_room_count"], 1)
        self.assertEqual(by_position["EDGE"]["depth_chart_room_count"], 1)

    def test_fresh_dated_contract_inputs_can_compute_auditable_window_score(self) -> None:
        depth = [
            _depth("AAA", "a", "Starter A"),
            _depth("BBB", "b", "Starter B"),
        ]
        contracts = [
            _contract("AAA", "Starter A", year_signed=2023, years=4),
            _contract("BBB", "Starter B", year_signed=2026, years=5),
        ]
        profiles = build_nflverse_team_enrichment(
            depth,
            [],
            contracts,
            season=2026,
            contract_data_as_of="2026-08-01",
        )
        by_team = {row["team"]: row for row in profiles}

        self.assertEqual(by_team["AAA"]["contract_need_score"], 100.0)
        self.assertEqual(by_team["BBB"]["contract_need_score"], 0.0)
        self.assertEqual(by_team["AAA"]["contract_evidence_coverage"], 1.0)
        self.assertIn("exact_name", by_team["AAA"]["contract_source"])

    def test_merge_preserves_base_evidence_and_adds_supported_fields(self) -> None:
        merged = merge_nflverse_team_enrichment(
            [
                {
                    "team": "AAA",
                    "season": 2026,
                    "position": "WR",
                    "need_score": 70,
                    "profile_source": "nflverse_roster_and_draft_derived",
                    "notes": "Roster evidence.",
                }
            ],
            [
                {
                    "team": "AAA",
                    "season": 2026,
                    "position": "WR",
                    "starter_quality_need_score": 75,
                    "starter_quality_evidence_coverage": 0.9,
                    "profile_as_of_date": "2026-08-26",
                    "profile_source": "nflverse_depth_stats_contract_context",
                    "notes": "Depth evidence.",
                }
            ],
        )[0]

        self.assertEqual(merged["need_score"], 70)
        self.assertEqual(merged["starter_quality_need_score"], 75)
        self.assertEqual(merged["notes"], "Roster evidence. Depth evidence.")
        self.assertIn("plus_depth_stats", merged["profile_source"])

    def test_all_remote_feeds_unavailable_returns_metadata_instead_of_raising(self) -> None:
        missing = HTTPError("https://example.test", 404, "Not Found", {}, None)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            missing, "close", wraps=missing.close
        ) as close, patch("draftscope.nflverse_free.urlopen", side_effect=missing):
            profiles, metadata = load_nflverse_team_enrichment(
                Path(directory), season=2026, refresh=True
            )

        self.assertEqual(profiles, [])
        self.assertEqual(metadata["status"], "unavailable")
        self.assertEqual(metadata["feeds"]["depth_charts"]["status"], "unavailable")
        self.assertEqual(metadata["feeds"]["contracts"]["status"], "stale_withheld")
        self.assertFalse(metadata["feeds"]["contracts"]["scoring_allowed"])
        self.assertEqual(close.call_count, 4)


if __name__ == "__main__":
    unittest.main()

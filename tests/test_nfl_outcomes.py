from __future__ import annotations

import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from draftscope.nfl_outcomes import (
    MIN_COMPLETE_REGULAR_SEASON_GAMES,
    _download_csv,
    build_three_year_contributor_outcomes,
    load_nflverse_snap_counts,
)
from draftscope.records import DataError


def snap(
    season: int,
    game: str,
    player: str,
    *,
    offense: int = 0,
    defense: int = 0,
    special_teams: int = 0,
    game_type: str = "REG",
) -> dict[str, object]:
    return {
        "season": season,
        "game_id": game,
        "game_type": game_type,
        "pfr_player_id": player,
        "offense_snaps": offense,
        "defense_snaps": defense,
        "st_snaps": special_teams,
    }


class NflContributorOutcomeTests(unittest.TestCase):
    def test_existing_snap_cache_rejects_oversize_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "snap_counts_2020.csv"
            destination.write_bytes(b"12345")
            with patch(
                "draftscope.nfl_outcomes._MAX_SNAP_COUNT_DOWNLOAD_BYTES", 4
            ), self.assertRaisesRegex(DataError, "is 5 bytes; limit is 4"):
                load_nflverse_snap_counts(
                    temporary,
                    seasons=(2020,),
                    refresh=False,
                )

    def test_existing_snap_cache_caps_csv_rows(self) -> None:
        payload = (
            "game_id,season,game_type,pfr_player_id,offense_snaps,"
            "defense_snaps,st_snaps\n"
            "g1,2020,REG,player,1,0,0\n"
            "g2,2020,REG,player,1,0,0\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "snap_counts_2020.csv"
            destination.write_text(payload, encoding="utf-8")
            with patch(
                "draftscope.nfl_outcomes._MAX_SNAP_COUNT_CSV_ROWS", 1
            ), self.assertRaisesRegex(DataError, "exceeded 1 data rows"):
                load_nflverse_snap_counts(
                    temporary,
                    seasons=(2020,),
                    refresh=False,
                )

    def test_invalid_download_does_not_replace_valid_snap_cache(self) -> None:
        cached = (
            "game_id,season,game_type,pfr_player_id,offense_snaps,"
            "defense_snaps,st_snaps\n"
            "g1,2020,REG,player,1,0,0\n"
        ).encode()
        response = io.BytesIO(b"<html>upstream error</html>")
        response.headers = {"Content-Type": "text/html"}
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "snap_counts.csv"
            destination.write_bytes(cached)
            sidecar = destination.with_suffix(".csv.source.json")
            sidecar.write_text('{"marker": "old"}\n', encoding="utf-8")
            with patch("draftscope.nfl_outcomes.urlopen", return_value=response):
                path = _download_csv(
                    "https://example.test/snap_counts.csv",
                    destination,
                    refresh=True,
                )

            self.assertEqual(path, destination)
            self.assertEqual(destination.read_bytes(), cached)
            self.assertEqual(sidecar.read_text(encoding="utf-8"), '{"marker": "old"}\n')

    def test_invalid_download_without_snap_cache_is_rejected(self) -> None:
        response = io.BytesIO(b"<html>upstream error</html>")
        response.headers = {"Content-Type": "text/html"}
        with tempfile.TemporaryDirectory() as temporary, patch(
            "draftscope.nfl_outcomes.urlopen", return_value=response
        ):
            destination = Path(temporary) / "snap_counts.csv"
            with self.assertRaisesRegex(DataError, "snap-count CSV was invalid"):
                _download_csv(
                    "https://example.test/snap_counts.csv",
                    destination,
                    refresh=True,
                )
            self.assertFalse(destination.exists())

    def test_download_rejects_announced_oversize_response(self) -> None:
        response = io.BytesIO(b"x")
        response.headers = {"Content-Length": "5"}
        with tempfile.TemporaryDirectory() as temporary, patch(
            "draftscope.nfl_outcomes._MAX_SNAP_COUNT_DOWNLOAD_BYTES", 4
        ), patch("draftscope.nfl_outcomes.urlopen", return_value=response):
            destination = Path(temporary) / "snap_counts.csv"
            with self.assertRaisesRegex(DataError, "announced 5 bytes"):
                _download_csv(
                    "https://example.test/snap_counts.csv",
                    destination,
                    refresh=True,
                )
            self.assertFalse(destination.exists())

    def test_download_stream_limit_preserves_existing_cache(self) -> None:
        response = io.BytesIO(b"12345")
        response.headers = {}
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "snap_counts.csv"
            destination.write_bytes(b"cached")
            with patch(
                "draftscope.nfl_outcomes._MAX_SNAP_COUNT_DOWNLOAD_BYTES", 4
            ), patch("draftscope.nfl_outcomes.urlopen", return_value=response):
                path = _download_csv(
                    "https://example.test/snap_counts.csv",
                    destination,
                    refresh=True,
                )
            self.assertEqual(path, destination)
            self.assertEqual(destination.read_bytes(), b"cached")

    def test_download_closes_http_error_before_fallback(self) -> None:
        missing = HTTPError("https://example.test", 404, "Not Found", {}, None)
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "snap_counts.csv"
            destination.write_bytes(b"cached")
            with patch.object(missing, "close", wraps=missing.close) as close, patch(
                "draftscope.nfl_outcomes.urlopen", side_effect=missing
            ):
                path = _download_csv(
                    "https://example.test/snap_counts.csv",
                    destination,
                    refresh=True,
                )
            self.assertEqual(path, destination)
            close.assert_called_once_with()

    def test_cached_annual_csv_is_hashed_and_requires_complete_game_coverage(self) -> None:
        fields = (
            "game_id",
            "season",
            "game_type",
            "pfr_player_id",
            "offense_snaps",
            "defense_snaps",
            "st_snaps",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "snap_counts_2020.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for index in range(MIN_COMPLETE_REGULAR_SEASON_GAMES):
                    writer.writerow(
                        snap(2020, f"g{index}", "player", offense=1)
                    )

            rows, metadata = load_nflverse_snap_counts(
                temporary,
                seasons=(2020,),
                refresh=False,
            )

            self.assertEqual(len(rows[2020]), MIN_COMPLETE_REGULAR_SEASON_GAMES)
            self.assertEqual(metadata["status"], "pass")
            artifact = metadata["source_artifacts"][0]
            self.assertTrue(artifact["complete"])
            self.assertEqual(
                artifact["regular_season_games"],
                MIN_COMPLETE_REGULAR_SEASON_GAMES,
            )
            self.assertEqual(len(artifact["sha256"]), 64)
            self.assertEqual(artifact["bytes"], path.stat().st_size)

    def test_fixed_horizon_labels_primary_special_teams_zero_and_censoring(self) -> None:
        identities = [
            {"draft_year": 2020, "pfr_player_id": "primary"},
            {"draft_year": 2020, "pfr_player_id": "specialist"},
            {"draft_year": 2020, "pfr_player_id": "never_played"},
            {"draft_year": 2021, "pfr_player_id": "incomplete"},
            {"draft_year": 2023, "pfr_player_id": "censored"},
            {"draft_year": 2020, "pfr_player_id": ""},
        ]
        snaps = {
            2020: [
                snap(2020, "g1", "primary", offense=200),
                snap(2020, "g2", "specialist", special_teams=50),
                snap(2020, "post", "primary", offense=999, game_type="POST"),
            ],
            2021: [
                snap(2021, "g3", "primary", offense=150),
                snap(2021, "g4", "specialist", special_teams=50),
            ],
            2022: [
                snap(2022, "g5", "primary", defense=150),
                snap(2022, "g6", "specialist", special_teams=50),
            ],
            2023: [snap(2023, "g7", "censored", offense=600)],
            2024: [],
        }

        outcomes, metadata = build_three_year_contributor_outcomes(
            identities,
            snaps,
            completed_season=2024,
            complete_seasons=(2020, 2021, 2022, 2024),
        )

        primary = outcomes[(2020, "primary")]
        self.assertTrue(primary["nfl_three_year_outcome_known"])
        self.assertTrue(primary["nfl_three_year_contributor"])
        self.assertEqual(primary["nfl_three_year_primary_snaps"], 500)
        self.assertEqual(primary["nfl_three_year_total_snaps"], 500)
        self.assertEqual(primary["nfl_three_year_games"], 3)

        specialist = outcomes[(2020, "specialist")]
        self.assertTrue(specialist["nfl_three_year_contributor"])
        self.assertEqual(specialist["nfl_three_year_special_teams_snaps"], 150)

        never_played = outcomes[(2020, "never_played")]
        self.assertTrue(never_played["nfl_three_year_outcome_known"])
        self.assertFalse(never_played["nfl_three_year_contributor"])
        self.assertEqual(never_played["nfl_three_year_total_snaps"], 0)

        incomplete = outcomes[(2021, "incomplete")]
        self.assertFalse(incomplete["nfl_three_year_outcome_known"])
        self.assertFalse(incomplete["nfl_three_year_right_censored"])
        self.assertIn("2023", incomplete["nfl_three_year_outcome_unknown_reason"])

        censored = outcomes[(2023, "censored")]
        self.assertFalse(censored["nfl_three_year_outcome_known"])
        self.assertTrue(censored["nfl_three_year_right_censored"])
        self.assertIsNone(censored["nfl_three_year_contributor"])

        self.assertEqual(metadata["known_outcomes"], 3)
        self.assertEqual(metadata["contributors"], 2)
        self.assertEqual(metadata["zero_snap_outcomes"], 1)
        self.assertEqual(metadata["incomplete_source_outcomes"], 1)
        self.assertEqual(metadata["right_censored_outcomes"], 1)
        self.assertEqual(metadata["missing_pfr_identity_rows"], 1)

    def test_identical_duplicate_game_rows_collapse_but_conflicts_fail(self) -> None:
        row = snap(2020, "g1", "player", offense=500)
        outcomes, metadata = build_three_year_contributor_outcomes(
            [{"draft_year": 2020, "pfr_player_id": "player"}],
            {2020: [row, dict(row)], 2021: [], 2022: []},
            completed_season=2022,
            complete_seasons=(2020, 2021, 2022),
        )
        self.assertEqual(outcomes[(2020, "player")]["nfl_three_year_primary_snaps"], 500)
        self.assertEqual(metadata["identical_duplicate_snap_rows_collapsed"], 1)

        with self.assertRaisesRegex(DataError, "conflicting snap-count rows"):
            build_three_year_contributor_outcomes(
                [{"draft_year": 2020, "pfr_player_id": "player"}],
                {
                    2020: [row, snap(2020, "g1", "player", offense=501)],
                    2021: [],
                    2022: [],
                },
                completed_season=2022,
                complete_seasons=(2020, 2021, 2022),
            )


if __name__ == "__main__":
    unittest.main()

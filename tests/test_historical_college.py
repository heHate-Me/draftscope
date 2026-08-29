from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from draftscope.historical_college import (
    CHECKPOINT,
    HISTORICAL_COLLEGE_SCHEMA,
    MODEL_FEATURE_FIELDS,
    MODEL_STAGE,
    PROBABILITY_CONDITION,
    PROBABILITY_KIND,
    _load_nflverse_rows,
    audit_historical_college_training_data,
    build_historical_college_training_data,
)
from draftscope.records import DataError


def nflverse_pick(
    *,
    year: int = 2022,
    pick: int = 1,
    name: str = "Drafted Player",
    college: str = "Alpha",
) -> dict[str, object]:
    return {
        "season": year,
        "round": 1,
        "pick": pick,
        "team": "NFL",
        "pfr_player_id": "PfrId00",
        "cfb_player_id": "drafted-player-1",
        "pfr_player_name": name,
        "college": college,
        # Positive-only age must never become a college model feature.
        "age": 23,
    }


class FullDenominatorClient:
    def __init__(self, *, wrong_stats_season: bool = False, stats_season: int | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.wrong_stats_season = wrong_stats_season
        self.stats_season = stats_season

    def get(self, path: str, **params: object):
        self.calls.append((path, dict(params)))
        if path == "/roster":
            return [
                {
                    "id": "drafted",
                    "firstName": "Drafted",
                    "lastName": "Player",
                    "team": "Alpha",
                    "position": "QB",
                    "height": 75,
                    "weight": 220,
                    "year": 4,
                },
                {
                    "id": "returning",
                    "firstName": "Returning",
                    "lastName": "Backup",
                    "team": "Alpha",
                    "position": "QB",
                    "height": 74,
                    "weight": 210,
                    "year": 2,
                },
                {
                    "id": "athlete",
                    "firstName": "Roster",
                    "lastName": "Athlete",
                    "team": "Alpha",
                    "position": "ATH",
                    "height": 72,
                    "weight": 195,
                    "year": 1,
                },
            ]
        if path == "/teams/fbs":
            return [
                {
                    "school": "Alpha",
                    "alternateNames": ["Alpha University"],
                    "conference": "Test Conference",
                    "classification": "fbs",
                }
            ]
        if path == "/stats/player/season":
            season = self.stats_season if self.stats_season is not None else (2019 if self.wrong_stats_season else 2020)
            base = {
                "season": season,
                "playerId": "drafted",
                "player": "Drafted Player",
                "team": "Alpha",
                "conference": "Test Conference",
                "position": "QB",
            }
            return [
                {**base, "category": "passing", "statType": "COMPLETIONS", "stat": "200"},
                {**base, "category": "passing", "statType": "ATT", "stat": "300"},
                {**base, "category": "passing", "statType": "YDS", "stat": "2700"},
                {**base, "category": "passing", "statType": "TD", "stat": "25"},
                {**base, "category": "passing", "statType": "INT", "stat": "8"},
            ]
        if path == "/draft/picks":
            return [
                {
                    "collegeAthleteId": "drafted",
                    "collegeTeam": "Alpha",
                    "year": 2022,
                    "overall": 1,
                    "round": 1,
                    "name": "Drafted Player",
                    "position": "Quarterback",
                    "preDraftGrade": 99,
                    "height": 76,
                    "weight": 225,
                    "nflTeam": "NFL",
                }
            ]
        raise AssertionError(path)


class HistoricalCollegePipelineTests(unittest.TestCase):
    def test_existing_draft_cache_rejects_oversize_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "draft_picks.csv"
            path.write_bytes(b"12345")
            with patch(
                "draftscope.historical_college._MAX_NFLVERSE_DRAFT_BYTES", 4
            ), self.assertRaisesRegex(DataError, "is 5 bytes; limit is 4"):
                _load_nflverse_rows(
                    None,
                    draft_path=path,
                    cache_root=None,
                    refresh=False,
                )

    def test_existing_draft_cache_caps_csv_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "draft_picks.csv"
            path.write_text(
                "season,pick\n2020,1\n2020,2\n",
                encoding="utf-8",
            )
            with patch(
                "draftscope.historical_college._MAX_NFLVERSE_DRAFT_ROWS", 1
            ), self.assertRaisesRegex(DataError, "exceeded 1 data rows"):
                _load_nflverse_rows(
                    None,
                    draft_path=path,
                    cache_root=None,
                    refresh=False,
                )

    def test_direct_draft_download_rejects_announced_and_streamed_oversize(self) -> None:
        cases = (
            ({"Content-Length": "5"}, b"x", "announced 5 bytes"),
            ({}, b"12345", "exceeded 4 bytes"),
        )
        for headers, payload, expected in cases:
            with self.subTest(expected=expected):
                response = io.BytesIO(payload)
                response.headers = headers
                with patch(
                    "draftscope.historical_college._MAX_NFLVERSE_DRAFT_BYTES", 4
                ), patch(
                    "draftscope.historical_college.urlopen", return_value=response
                ), self.assertRaisesRegex(DataError, expected):
                    _load_nflverse_rows(
                        None,
                        draft_path=None,
                        cache_root=None,
                        refresh=True,
                    )

    def test_direct_draft_download_closes_http_error(self) -> None:
        missing = HTTPError("https://example.test", 404, "Not Found", {}, None)
        with patch.object(missing, "close", wraps=missing.close) as close, patch(
            "draftscope.historical_college.urlopen", side_effect=missing
        ), self.assertRaisesRegex(DataError, "Could not download"):
            _load_nflverse_rows(
                None,
                draft_path=None,
                cache_root=None,
                refresh=True,
            )
        close.assert_called_once_with()

    def test_stable_recruiting_pedigree_is_checkpoint_safe_model_evidence(self) -> None:
        class RecruitingClient(FullDenominatorClient):
            supports_recruiting = True

            def get(self, path: str, **params: object):
                if path == "/recruiting/players":
                    if params.get("year") == 2018:
                        return [
                            {
                                "id": "recruit-1",
                                "athleteId": "drafted",
                                "year": 2018,
                                "name": "Drafted Player",
                                "recruitType": "HighSchool",
                                "rating": 0.9721,
                                "stars": 4,
                                "ranking": 75,
                            }
                        ]
                    return []
                if path == "/roster":
                    rows = super().get(path, **params)
                    rows[0]["recruitIds"] = ["recruit-1"]
                    return rows
                return super().get(path, **params)

        rows, metadata = build_historical_college_training_data(
            RecruitingClient(),
            college_seasons=(2021,),
            cache_dir=None,
            nflverse_draft_rows=[nflverse_pick()],
        )
        drafted = next(row for row in rows if row["player_id"] == "drafted")

        self.assertEqual(drafted["recruit_rating"], 0.9721)
        self.assertEqual(drafted["recruit_stars"], 4)
        self.assertEqual(drafted["recruit_national_rank"], 75)
        self.assertEqual(drafted["recruit_match_method"], "recruit_id")
        self.assertIn("recruit_rating", MODEL_FEATURE_FIELDS)
        self.assertEqual(
            metadata["per_season"][0]["roster_players_with_recruiting_profile"],
            1,
        )
        self.assertEqual(metadata["schema_version"], "1.2")

    def test_keeps_complete_roster_denominator_and_bounds_every_source_year(self) -> None:
        client = FullDenominatorClient()
        rows, metadata = build_historical_college_training_data(
            client,
            college_seasons=(2021,),
            cache_dir=None,
            nflverse_draft_rows=[nflverse_pick()],
        )

        self.assertEqual(len(rows), 3)
        by_id = {row["player_id"]: row for row in rows}
        self.assertTrue(by_id["drafted"]["drafted"])
        self.assertFalse(by_id["returning"]["drafted"])
        self.assertFalse(by_id["athlete"]["drafted"])
        self.assertFalse(by_id["returning"]["has_recorded_stats"])
        self.assertTrue(by_id["drafted"]["has_recorded_stats"])
        self.assertEqual(by_id["drafted"]["prod_pass_yards"], 2700.0)
        self.assertAlmostEqual(by_id["drafted"]["prod_completion_pct"], 215 / 325)
        self.assertEqual(by_id["drafted"]["conference"], "Test Conference")
        self.assertEqual(by_id["returning"]["conference"], "Test Conference")
        self.assertIsNone(by_id["drafted"]["age_at_draft"])
        self.assertNotIn("preDraftGrade", by_id["drafted"])
        self.assertNotIn("forty_s", by_id["drafted"])

        for row in rows:
            self.assertEqual(tuple(row), HISTORICAL_COLLEGE_SCHEMA)
            self.assertEqual(row["season"], 2021)
            self.assertEqual(row["draft_year"], 2022)
            self.assertEqual(row["checkpoint"], CHECKPOINT)
            self.assertEqual(row["as_of_week"], 0)
            self.assertEqual(row["feature_cutoff_season"], 2020)
            self.assertEqual(row["stats_source_year"], 2020)
            self.assertEqual(row["model_stage"], MODEL_STAGE)
            self.assertEqual(row["population"], "FBS roster preseason; prior-season stats")
            self.assertEqual(row["probability_kind"], PROBABILITY_KIND)
            self.assertEqual(row["probability_condition"], PROBABILITY_CONDITION)

        self.assertEqual(metadata["rows"], 3)
        self.assertEqual(metadata["drafted_rows"], 1)
        self.assertEqual(metadata["positive_match_coverage"], 1.0)
        self.assertEqual(metadata["contract_audit"]["status"], "pass")
        self.assertEqual(metadata["probability_kind"], PROBABILITY_KIND)
        self.assertEqual(metadata["probability_condition"], PROBABILITY_CONDITION)
        self.assertEqual(metadata["row_population"], "FBS roster preseason; prior-season stats")
        self.assertIn("preseason checkpoint", metadata["population"])
        self.assertIn("prior completed-season production", metadata["population"])
        self.assertEqual(metadata["per_season"][0]["position_counts"], {"ATH": 1, "QB": 2})
        self.assertEqual(metadata["per_season"][0]["roster_players_with_stats"], 1)
        self.assertEqual(metadata["per_season"][0]["unsupported_position_rows_retained"], 1)
        self.assertFalse(set(metadata["outcome_fields"]) & set(MODEL_FEATURE_FIELDS))

        self.assertIn(
            ("/roster", {"year": 2021, "classification": "fbs"}),
            client.calls,
        )
        self.assertIn(
            ("/stats/player/season", {"year": 2020, "seasonType": "both"}),
            client.calls,
        )
        self.assertIn(("/teams/fbs", {"year": 2021}), client.calls)
        self.assertIn(("/draft/picks", {"year": 2022}), client.calls)

    def test_complete_crosswalk_id_resolves_an_incomplete_same_school_nickname_stub(self) -> None:
        class AliasClient(FullDenominatorClient):
            def get(self, path: str, **params: object):
                if path == "/roster":
                    return [
                        {
                            "id": "stable-id",
                            "firstName": "Quientrail",
                            "lastName": "Jamison-Travis",
                            "team": "Alpha",
                            "position": "DL",
                            "height": 76,
                            "weight": 310,
                            "year": 4,
                        },
                        {
                            "id": "nickname-stub",
                            "firstName": "Bobby",
                            "lastName": "Jamison-Travis",
                            "team": "Alpha",
                            "position": "?",
                            "height": None,
                            "weight": None,
                            "year": 0,
                        },
                    ]
                if path == "/stats/player/season":
                    return [
                        {
                            "season": 2020,
                            "playerId": "stable-id",
                            "player": "Quientrail Jamison-Travis",
                            "team": "Alpha",
                            "position": "DL",
                            "category": "defensive",
                            "statType": "TFL",
                            "stat": 8,
                        }
                    ]
                if path == "/draft/picks":
                    return [
                        {
                            "collegeAthleteId": "stable-id",
                            "collegeTeam": "Alpha",
                            "year": 2022,
                            "overall": 1,
                            "round": 1,
                            "name": "Bobby Jamison-Travis",
                            "position": "Defensive Line",
                            "nflTeam": "NFL",
                        }
                    ]
                return super().get(path, **params)

        rows, metadata = build_historical_college_training_data(
            AliasClient(),
            college_seasons=(2021,),
            cache_dir=None,
            nflverse_draft_rows=[
                nflverse_pick(name="Bobby Jamison-Travis", college="Alpha")
            ],
        )

        by_id = {row["player_id"]: row for row in rows}
        self.assertTrue(by_id["stable-id"]["drafted"])
        self.assertNotIn("nickname-stub", by_id)
        self.assertEqual(
            by_id["stable-id"]["outcome_match_method"],
            "cfbd_player_id_resolved_incomplete_name_alias",
        )
        audit = metadata["per_season"][0]
        self.assertEqual(audit["identity_conflicts"], [])
        self.assertEqual(len(audit["resolved_incomplete_name_aliases"]), 1)
        self.assertEqual(audit["positive_match_coverage"], 1.0)

    def test_duplicate_transfer_roster_is_resolved_by_stats_and_stale_pick_id_falls_back(self) -> None:
        class TransferClient:
            def get(self, path: str, **params: object):
                if path == "/roster":
                    return [
                        {"id": "real", "firstName": "Fallback", "lastName": "Player", "team": "Old", "position": "WR", "height": 72, "weight": 190, "year": 4},
                        {"id": "real", "firstName": "Fallback", "lastName": "Player", "team": "New", "position": "WR", "height": 73, "weight": 195, "year": 4},
                    ]
                if path == "/teams/fbs":
                    return [
                        {"school": "Old", "alternateNames": [], "conference": "A"},
                        {"school": "New", "alternateNames": [], "conference": "B"},
                    ]
                if path == "/stats/player/season":
                    return [
                        {"season": 2020, "playerId": "real", "player": "Fallback Player", "team": "Old", "conference": "A", "position": "WR", "category": "receiving", "statType": "REC", "stat": 50},
                        {"season": 2020, "playerId": "real", "player": "Fallback Player", "team": "Old", "conference": "A", "position": "WR", "category": "receiving", "statType": "YDS", "stat": 700},
                    ]
                if path == "/draft/picks":
                    return [
                        {"collegeAthleteId": "stale", "collegeTeam": "New", "year": 2022, "overall": 1, "round": 1, "name": "Fallback Player", "position": "Wide Receiver", "nflTeam": "NFL"}
                    ]
                raise AssertionError(path)

        rows, metadata = build_historical_college_training_data(
            TransferClient(),
            college_seasons=(2021,),
            cache_dir=None,
            nflverse_draft_rows=[nflverse_pick(name="Fallback Player", college="New")],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["school"], "New")
        self.assertEqual(rows[0]["height_in"], 73.0)
        self.assertTrue(rows[0]["drafted"])
        self.assertEqual(rows[0]["outcome_match_method"], "exact_name_team")
        audit = metadata["per_season"][0]
        self.assertEqual(audit["duplicate_roster_rows_dropped"], 1)
        self.assertEqual(audit["duplicate_roster_groups_resolved_by_stat_team"], 0)
        self.assertEqual(audit["duplicate_roster_groups_resolved_away_from_prior_stat_team"], 1)
        self.assertEqual(audit["positive_match_methods"], {"exact_name_team": 1})

    def test_stale_cfbd_nickname_uses_unique_exact_nflverse_name_and_school(self) -> None:
        class NflverseNameClient(FullDenominatorClient):
            def get(self, path: str, **params: object):
                if path == "/roster":
                    return [
                        {
                            "id": "roster-id",
                            "firstName": "Cameron",
                            "lastName": "Sutton",
                            "team": "Alpha",
                            "position": "DB",
                            "height": 71,
                            "weight": 188,
                            "year": 4,
                        }
                    ]
                if path == "/stats/player/season":
                    return []
                if path == "/draft/picks":
                    return [
                        {
                            "collegeAthleteId": "stale-id",
                            "collegeTeam": "Alpha",
                            "year": 2022,
                            "overall": 1,
                            "round": 1,
                            "name": "Cam Sutton",
                            "position": "Cornerback",
                            "nflTeam": "NFL",
                        }
                    ]
                return super().get(path, **params)

        rows, metadata = build_historical_college_training_data(
            NflverseNameClient(),
            college_seasons=(2021,),
            cache_dir=None,
            nflverse_draft_rows=[nflverse_pick(name="Cameron Sutton")],
        )

        self.assertTrue(rows[0]["drafted"])
        self.assertEqual(rows[0]["outcome_match_method"], "exact_nflverse_name_team")
        self.assertEqual(metadata["positive_match_coverage"], 1.0)

    def test_complete_exact_name_identity_replaces_an_incomplete_crosswalk_stub(self) -> None:
        class IncompleteCrosswalkClient(FullDenominatorClient):
            def get(self, path: str, **params: object):
                if path == "/roster":
                    return [
                        {"id": "crosswalk-stub", "firstName": "Zay", "lastName": "Jones", "team": "Alpha", "position": None, "height": None, "weight": None, "year": 0},
                        {"id": "complete-id", "firstName": "Zay", "lastName": "Jones", "team": "Alpha", "position": "WR", "height": 73, "weight": 200, "year": 4},
                    ]
                if path == "/stats/player/season":
                    return [
                        {"season": 2020, "playerId": "complete-id", "player": "Zay Jones", "team": "Alpha", "position": "WR", "category": "receiving", "statType": "YDS", "stat": 1200}
                    ]
                if path == "/draft/picks":
                    return [
                        {"collegeAthleteId": "crosswalk-stub", "collegeTeam": "Alpha", "year": 2022, "overall": 1, "round": 1, "name": "Zay Jones", "position": "Wide Receiver", "nflTeam": "NFL"}
                    ]
                return super().get(path, **params)

        rows, _metadata = build_historical_college_training_data(
            IncompleteCrosswalkClient(),
            college_seasons=(2021,),
            cache_dir=None,
            nflverse_draft_rows=[nflverse_pick(name="Zay Jones")],
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["player_id"], "complete-id")
        self.assertTrue(rows[0]["drafted"])
        self.assertEqual(
            rows[0]["outcome_match_method"],
            "exact_name_team_resolved_incomplete_crosswalk_stub",
        )

    def test_strong_alias_evidence_handles_adjacent_positions_and_two_stat_ids(self) -> None:
        class AdjacentPositionClient(FullDenominatorClient):
            def get(self, path: str, **params: object):
                common = {"firstName": "Andrew", "lastName": "Van Ginkel", "team": "Alpha", "height": 76, "weight": 233, "jersey": 17, "year": 4, "homeCity": "Rock Valley", "homeState": "IA"}
                if path == "/roster":
                    return [
                        {"id": "direct-id", "position": "LB", **common},
                        {"id": "alias-id", "position": "OLB", **common},
                    ]
                if path == "/stats/player/season":
                    return [
                        {"season": 2020, "playerId": player_id, "player": "Andrew Van Ginkel", "team": "Alpha", "position": position, "category": "defensive", "statType": "TACKLES", "stat": 50}
                        for player_id, position in (("direct-id", "LB"), ("alias-id", "OLB"))
                    ]
                if path == "/draft/picks":
                    return [
                        {"collegeAthleteId": "direct-id", "collegeTeam": "Alpha", "year": 2022, "overall": 1, "round": 1, "name": "Andrew Van Ginkel", "position": "Linebacker", "nflTeam": "NFL"}
                    ]
                return super().get(path, **params)

        rows, metadata = build_historical_college_training_data(
            AdjacentPositionClient(),
            college_seasons=(2021,),
            cache_dir=None,
            nflverse_draft_rows=[nflverse_pick(name="Andrew Van Ginkel")],
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["player_id"], "direct-id")
        self.assertEqual(rows[0]["outcome_match_method"], "cfbd_player_id_strong_duplicate_alias")
        self.assertEqual(metadata["per_season"][0]["cross_id_duplicate_alias_rows_dropped"], 1)

    def test_ambiguous_exact_roster_aliases_use_unique_checkpoint_stat_identity(self) -> None:
        class StatIdentityClient(FullDenominatorClient):
            def get(self, path: str, **params: object):
                if path == "/roster":
                    return [
                        {"id": "alias", "firstName": "Anthony", "lastName": "Walker, Jr.", "team": "Alpha", "position": "LB", "height": 73, "weight": 238, "year": 4, "homeCity": "Miami", "homeState": "FL"},
                        {"id": "stat-id", "firstName": "Anthony", "lastName": "Walker", "team": "Alpha", "position": "LB", "height": 73, "weight": 238, "year": 4, "homeCity": "Miami", "homeState": "FL"},
                    ]
                if path == "/stats/player/season":
                    return [
                        {"season": 2020, "playerId": "stat-id", "player": "Anthony Walker", "team": "Alpha", "position": "LB", "category": "defensive", "statType": "TACKLES", "stat": 100}
                    ]
                if path == "/draft/picks":
                    return [
                        {"collegeAthleteId": "stale-id", "collegeTeam": "Alpha", "year": 2022, "overall": 1, "round": 1, "name": "Anthony Walker Jr.", "position": "Linebacker", "nflTeam": "NFL"}
                    ]
                return super().get(path, **params)

        rows, metadata = build_historical_college_training_data(
            StatIdentityClient(),
            college_seasons=(2021,),
            cache_dir=None,
            nflverse_draft_rows=[nflverse_pick(name="Anthony Walker")],
        )

        by_id = {row["player_id"]: row for row in rows}
        self.assertTrue(by_id["stat-id"]["drafted"])
        self.assertNotIn("alias", by_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(by_id["stat-id"]["outcome_match_method"], "exact_name_team_prior_stat")
        self.assertEqual(len(metadata["per_season"][0]["resolved_name_aliases"]), 1)

    def test_crosswalk_id_beats_complete_nickname_alias_only_with_unique_stat_evidence(self) -> None:
        class DirectStatClient(FullDenominatorClient):
            def get(self, path: str, **params: object):
                if path == "/roster":
                    return [
                        {"id": "direct-id", "firstName": "Joshua", "lastName": "Jackson", "team": "Alpha", "position": "DB", "height": 72, "weight": 196, "year": 4, "jersey": 15, "homeCity": "Corinth", "homeState": "TX"},
                        {"id": "nickname-id", "firstName": "Josh", "lastName": "Jackson", "team": "Alpha", "position": "DB", "height": 72, "weight": 196, "year": 4, "jersey": 15, "homeCity": "Corinth", "homeState": "TX"},
                    ]
                if path == "/stats/player/season":
                    return [
                        {"season": 2020, "playerId": "direct-id", "player": "Joshua Jackson", "team": "Alpha", "position": "DB", "category": "defensive", "statType": "INTERCEPTIONS", "stat": 8}
                    ]
                if path == "/draft/picks":
                    return [
                        {"collegeAthleteId": "direct-id", "collegeTeam": "Alpha", "year": 2022, "overall": 1, "round": 1, "name": "Josh Jackson", "position": "Cornerback", "nflTeam": "NFL"}
                    ]
                return super().get(path, **params)

        rows, metadata = build_historical_college_training_data(
            DirectStatClient(),
            college_seasons=(2021,),
            cache_dir=None,
            nflverse_draft_rows=[nflverse_pick(name="Josh Jackson")],
        )

        by_id = {row["player_id"]: row for row in rows}
        self.assertTrue(by_id["direct-id"]["drafted"])
        self.assertNotIn("nickname-id", by_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            by_id["direct-id"]["outcome_match_method"],
            "cfbd_player_id_strong_duplicate_alias",
        )
        self.assertEqual(metadata["per_season"][0]["identity_conflicts"], [])

    def test_strong_negative_alias_uses_unique_checkpoint_stat_identity(self) -> None:
        class NegativeAliasClient(FullDenominatorClient):
            def get(self, path: str, **params: object):
                if path == "/roster":
                    return super().get(path, **params) + [
                        {
                            "id": "negative-stat-id",
                            "firstName": "Alex",
                            "lastName": "Duplicate",
                            "team": "Alpha",
                            "position": "WR",
                            "height": 73,
                            "weight": 205,
                            "year": 3,
                            "jersey": 18,
                            "homeCity": "Austin",
                            "homeState": "TX",
                        },
                        {
                            "id": "negative-alias-id",
                            "firstName": "Alex",
                            "lastName": "Duplicate, Jr.",
                            "team": "Alpha",
                            "position": "WR",
                            "height": 73,
                            "weight": 205,
                            "year": 3,
                            "jersey": 18,
                            "homeCity": "Austin",
                            "homeState": "TX",
                        },
                    ]
                if path == "/stats/player/season":
                    return super().get(path, **params) + [
                        {
                            "season": 2020,
                            "playerId": "negative-stat-id",
                            "player": "Alex Duplicate",
                            "team": "Alpha",
                            "position": "WR",
                            "category": "receiving",
                            "statType": "YDS",
                            "stat": 600,
                        }
                    ]
                return super().get(path, **params)

        rows, metadata = build_historical_college_training_data(
            NegativeAliasClient(),
            college_seasons=(2021,),
            cache_dir=None,
            nflverse_draft_rows=[nflverse_pick()],
        )

        by_id = {row["player_id"]: row for row in rows}
        self.assertIn("negative-stat-id", by_id)
        self.assertNotIn("negative-alias-id", by_id)
        self.assertFalse(by_id["negative-stat-id"]["drafted"])
        self.assertTrue(by_id["negative-stat-id"]["has_recorded_stats"])
        audit = metadata["per_season"][0]
        self.assertEqual(audit["negative_alias_candidate_groups"], 1)
        self.assertEqual(audit["negative_alias_groups_with_unique_checkpoint_stat_id"], 1)
        self.assertEqual(audit["negative_alias_groups_collapsed"], 1)
        self.assertEqual(audit["negative_cross_id_alias_rows_dropped"], 1)
        self.assertEqual(
            audit["negative_cross_id_alias_player_ids_dropped"],
            ["negative-alias-id"],
        )
        self.assertEqual(metadata["drops"]["negative_cross_id_alias_rows_dropped"], 1)

    def test_same_name_negative_teammates_without_identity_agreement_are_retained(self) -> None:
        class SameNameTeammatesClient(FullDenominatorClient):
            def get(self, path: str, **params: object):
                if path == "/roster":
                    return super().get(path, **params) + [
                        {
                            "id": "jordan-one",
                            "firstName": "Jordan",
                            "lastName": "Smith",
                            "team": "Alpha",
                            "position": "WR",
                            "height": 73,
                            "weight": 200,
                            "year": 3,
                            "jersey": 10,
                            "homeCity": "Austin",
                            "homeState": "TX",
                        },
                        {
                            "id": "jordan-two",
                            "firstName": "Jordan",
                            "lastName": "Smith",
                            "team": "Alpha",
                            "position": "WR",
                            "height": 73,
                            "weight": 200,
                            "year": 3,
                            "jersey": 11,
                            "homeCity": "Fresno",
                            "homeState": "CA",
                        },
                    ]
                if path == "/stats/player/season":
                    return super().get(path, **params) + [
                        {
                            "season": 2020,
                            "playerId": "jordan-one",
                            "player": "Jordan Smith",
                            "team": "Alpha",
                            "position": "WR",
                            "category": "receiving",
                            "statType": "YDS",
                            "stat": 500,
                        }
                    ]
                return super().get(path, **params)

        rows, metadata = build_historical_college_training_data(
            SameNameTeammatesClient(),
            college_seasons=(2021,),
            cache_dir=None,
            nflverse_draft_rows=[nflverse_pick()],
        )

        by_id = {row["player_id"]: row for row in rows}
        self.assertIn("jordan-one", by_id)
        self.assertIn("jordan-two", by_id)
        audit = metadata["per_season"][0]
        self.assertEqual(audit["negative_alias_groups_with_unique_checkpoint_stat_id"], 1)
        self.assertEqual(audit["negative_alias_pairs_rejected_weak_identity"], 1)
        self.assertEqual(audit["negative_alias_groups_collapsed"], 0)
        self.assertEqual(audit["negative_cross_id_alias_rows_dropped"], 0)

    def test_strong_negative_aliases_with_two_stat_ids_are_not_merged(self) -> None:
        class TwoStatIdsClient(FullDenominatorClient):
            def get(self, path: str, **params: object):
                common = {
                    "firstName": "Taylor",
                    "lastName": "Duplicated",
                    "team": "Alpha",
                    "position": "LB",
                    "height": 74,
                    "weight": 232,
                    "year": 4,
                    "jersey": 22,
                    "homeCity": "Mobile",
                    "homeState": "AL",
                }
                if path == "/roster":
                    return super().get(path, **params) + [
                        {"id": "two-stat-a", **common},
                        {"id": "two-stat-b", **common},
                    ]
                if path == "/stats/player/season":
                    return super().get(path, **params) + [
                        {
                            "season": 2020,
                            "playerId": player_id,
                            "player": "Taylor Duplicated",
                            "team": "Alpha",
                            "position": "LB",
                            "category": "defensive",
                            "statType": "TACKLES",
                            "stat": tackles,
                        }
                        for player_id, tackles in (("two-stat-a", 40), ("two-stat-b", 41))
                    ]
                return super().get(path, **params)

        rows, metadata = build_historical_college_training_data(
            TwoStatIdsClient(),
            college_seasons=(2021,),
            cache_dir=None,
            nflverse_draft_rows=[nflverse_pick()],
        )

        by_id = {row["player_id"]: row for row in rows}
        self.assertIn("two-stat-a", by_id)
        self.assertIn("two-stat-b", by_id)
        audit = metadata["per_season"][0]
        self.assertEqual(audit["negative_alias_groups_with_multiple_checkpoint_stat_ids"], 1)
        self.assertEqual(audit["negative_alias_groups_collapsed"], 0)
        self.assertEqual(audit["negative_cross_id_alias_rows_dropped"], 0)

    def test_only_exact_reviewed_roster_omissions_bypass_the_strict_gate(self) -> None:
        class OmissionClient:
            def get(self, path: str, **params: object):
                if path == "/roster":
                    return [
                        {"id": "other", "firstName": "Other", "lastName": "Player", "team": "Penn State", "position": "LB", "height": 74, "weight": 230, "year": 2}
                    ]
                if path == "/stats/player/season":
                    return []
                if path == "/teams/fbs":
                    return [{"school": "Penn State", "alternateNames": [], "conference": "Big Ten"}]
                if path == "/draft/picks":
                    return [
                        {"collegeAthleteId": f"missing-{pick}", "collegeTeam": "Non-FBS", "year": 2021, "overall": pick, "round": 1, "name": f"Other Pick {pick}", "position": "Linebacker", "nflTeam": "NFL"}
                        for pick in range(1, 12)
                    ] + [
                        {"collegeAthleteId": "4361423", "collegeTeam": "Penn State", "year": 2021, "overall": 12, "round": 1, "name": "Micah Parsons", "position": "Linebacker", "nflTeam": "NFL"}
                    ]
                raise AssertionError(path)

        nfl_rows = [
            nflverse_pick(year=2021, pick=pick, name=f"Other Pick {pick}", college="Non-FBS")
            for pick in range(1, 12)
        ] + [nflverse_pick(year=2021, pick=12, name="Micah Parsons", college="Penn State")]
        rows, metadata = build_historical_college_training_data(
            OmissionClient(),
            college_seasons=(2020,),
            cache_dir=None,
            nflverse_draft_rows=nfl_rows,
        )

        self.assertFalse(rows[0]["drafted"])
        self.assertEqual(metadata["reviewed_roster_source_omission_count"], 1)
        self.assertEqual(metadata["per_season"][0]["source_fbs_school_draft_picks"], 1)
        self.assertEqual(metadata["per_season"][0]["unmatched_expected_fbs_picks"], [])
        self.assertEqual(metadata["quality_gate_status"], "pass")

        weekly_rows, weekly_metadata = build_historical_college_training_data(
            OmissionClient(),
            college_seasons=(2020,),
            cache_dir=None,
            nflverse_draft_rows=nfl_rows,
            as_of_week=4,
        )
        self.assertFalse(weekly_rows[0]["drafted"])
        self.assertEqual(weekly_metadata["reviewed_roster_source_omission_count"], 1)
        self.assertEqual(weekly_metadata["quality_gate_status"], "pass")

    def test_stat_identity_never_overrides_non_equivalent_roster_players(self) -> None:
        class WrongDirectClient(FullDenominatorClient):
            def get(self, path: str, **params: object):
                if path == "/roster":
                    return [
                        {"id": "direct-id", "firstName": "Wrong", "lastName": "Athlete", "team": "Alpha", "position": "WR", "height": 74, "weight": 205, "year": 4, "homeCity": "Wrongtown", "homeState": "TX"},
                        {"id": "correct-id", "firstName": "Correct", "lastName": "Player", "team": "Alpha", "position": "WR", "height": 72, "weight": 190, "year": 4, "homeCity": "Righttown", "homeState": "CA"},
                    ]
                if path == "/stats/player/season":
                    return [
                        {"season": 2020, "playerId": "direct-id", "player": "Wrong Athlete", "team": "Alpha", "position": "WR", "category": "receiving", "statType": "YDS", "stat": 500}
                    ]
                if path == "/draft/picks":
                    return [
                        {"collegeAthleteId": "direct-id", "collegeTeam": "Alpha", "year": 2022, "overall": 1, "round": 1, "name": "Correct Player", "position": "Wide Receiver", "nflTeam": "NFL"}
                    ]
                return super().get(path, **params)

        with self.assertRaisesRegex(DataError, "quality gate failed"):
            build_historical_college_training_data(
                WrongDirectClient(),
                college_seasons=(2021,),
                cache_dir=None,
                nflverse_draft_rows=[nflverse_pick(name="Correct Player")],
            )

        class SameNameDifferentPlayerClient(WrongDirectClient):
            def get(self, path: str, **params: object):
                if path == "/roster":
                    return [
                        {"id": "lb-id", "firstName": "Alex", "lastName": "Smith", "team": "Alpha", "position": "LB", "height": 74, "weight": 235, "year": 4, "homeCity": "One", "homeState": "TX"},
                        {"id": "wr-id", "firstName": "Alex", "lastName": "Smith", "team": "Alpha", "position": "WR", "height": 70, "weight": 180, "year": 4, "homeCity": "Two", "homeState": "FL"},
                    ]
                if path == "/stats/player/season":
                    return [
                        {"season": 2020, "playerId": "lb-id", "player": "Alex Smith", "team": "Alpha", "position": "LB", "category": "defensive", "statType": "TACKLES", "stat": 80}
                    ]
                if path == "/draft/picks":
                    return [
                        {"collegeAthleteId": "stale-id", "collegeTeam": "Alpha", "year": 2022, "overall": 1, "round": 1, "name": "Alex Smith", "position": "Linebacker", "nflTeam": "NFL"}
                    ]
                return FullDenominatorClient.get(self, path, **params)

        with self.assertRaisesRegex(DataError, "quality gate failed"):
            build_historical_college_training_data(
                SameNameDifferentPlayerClient(),
                college_seasons=(2021,),
                cache_dir=None,
                nflverse_draft_rows=[nflverse_pick(name="Alex Smith")],
            )

    def test_week_checkpoint_uses_current_season_only_through_end_week(self) -> None:
        client = FullDenominatorClient(stats_season=2021)
        rows, metadata = build_historical_college_training_data(
            client,
            college_seasons=(2021,),
            cache_dir=None,
            nflverse_draft_rows=[nflverse_pick()],
            as_of_week=4,
        )
        self.assertIn(
            ("/stats/player/season", {"year": 2021, "seasonType": "both", "endWeek": 4}),
            client.calls,
        )
        self.assertEqual(metadata["checkpoint"], "through_week_04")
        self.assertEqual(metadata["as_of_week"], 4)
        self.assertTrue(all(row["feature_cutoff_season"] == 2021 for row in rows))
        self.assertTrue(all(row["stats_source_year"] == 2021 for row in rows))
        self.assertTrue(all(row["checkpoint"] == "through_week_04" for row in rows))
        self.assertTrue(all(row["population"] == "FBS roster through completed Week 4" for row in rows))
        self.assertIn("completed Week 4 checkpoint", metadata["population"])
        self.assertIn("bounded through Week 4", metadata["population"])

    def test_strict_gate_rejects_cross_season_stat_rows(self) -> None:
        with self.assertRaisesRegex(DataError, "quality gate failed"):
            build_historical_college_training_data(
                FullDenominatorClient(wrong_stats_season=True),
                college_seasons=(2021,),
                cache_dir=None,
                nflverse_draft_rows=[nflverse_pick()],
            )

    def test_standalone_audit_detects_post_checkpoint_feature_and_alignment(self) -> None:
        rows, _metadata = build_historical_college_training_data(
            FullDenominatorClient(),
            college_seasons=(2021,),
            cache_dir=None,
            nflverse_draft_rows=[nflverse_pick()],
        )
        contaminated = dict(rows[0])
        contaminated["draft_year"] = 2023
        contaminated["forty_s"] = 4.4
        result = audit_historical_college_training_data([contaminated])
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["invalid_immediate_draft_alignment_rows"], 1)
        self.assertEqual(result["forbidden_non_null_fields"], {"forty_s": 1})


if __name__ == "__main__":
    unittest.main()

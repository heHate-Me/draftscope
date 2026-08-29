from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

from draftscope.data_sources import (
    CFBDClient,
    _apply_hof_position_override,
    _build_nfl_career_elite_rows,
    _build_nflverse_history_rows,
    _enrich_wikimedia_profiles,
    _hof_registry_audit,
    _historical_positions,
    _load_wikimedia_hof_profiles,
    _parse_hof_table,
    _wikidata_measurement_resolution,
    _wikidata_quantity,
    _wikidata_scalar_resolution,
    build_cfbd_weekly_history,
    discover_cfbd_candidates,
    load_latest_completed_draft_class,
)


class FakeCFBD:
    def get(self, path: str, **params):
        if path == "/roster":
            year = params["year"]
            rosters = {
                2022: [
                    {"id": "a", "firstName": "Drafted", "lastName": "QB", "team": "Alpha", "position": "QB", "height": 75, "weight": 220},
                    {"id": "stay", "firstName": "Still", "lastName": "There", "team": "Alpha", "position": "WR", "height": 72, "weight": 190},
                ],
                2023: [
                    {"id": "stay", "firstName": "Still", "lastName": "There", "team": "Alpha", "position": "WR", "height": 72, "weight": 190},
                    {"id": "b", "firstName": "Undrafted", "lastName": "QB", "team": "Beta", "position": "QB", "height": 74, "weight": 215},
                ],
                2024: [],
            }
            return rosters[year]
        if path == "/draft/picks":
            if params["year"] == 2023:
                return [{"collegeAthleteId": "a", "name": "Drafted QB", "collegeTeam": "Alpha", "overall": 40, "round": 2, "nflTeam": "AAA"}]
            return []
        if path == "/stats/player/season":
            player_id = "a" if params["year"] == 2022 else "b"
            name = "Drafted QB" if player_id == "a" else "Undrafted QB"
            team = "Alpha" if player_id == "a" else "Beta"
            return [
                {"playerId": player_id, "player": name, "team": team, "conference": "X", "position": "QB", "category": "passing", "statType": "C/ATT", "stat": "20/30"},
                {"playerId": player_id, "player": name, "team": team, "conference": "X", "position": "QB", "category": "passing", "statType": "YDS", "stat": 280},
                {"playerId": player_id, "player": name, "team": team, "conference": "X", "position": "QB", "category": "passing", "statType": "TD", "stat": 3},
                {"playerId": player_id, "player": name, "team": team, "conference": "X", "position": "QB", "category": "passing", "statType": "INT", "stat": 1},
            ]
        raise AssertionError(path)


class DataSourceTests(unittest.TestCase):
    def test_active_nfl_benchmark_join_is_deterministic_and_audited(self) -> None:
        def combine(
            season: int,
            name: str,
            school: str,
            *,
            pfr: str = "",
            cfb: str = "",
            measurements: bool = True,
        ) -> dict[str, object]:
            return {
                "season": season,
                "pfr_id": pfr,
                "cfb_id": cfb,
                "player_name": name,
                "pos": "WR",
                "school": school,
                "ht": "6-1" if measurements else "",
                "wt": 200 if measurements else "",
                "forty": 4.50 if measurements else "",
            }

        def draft(
            season: int,
            name: str,
            college: str,
            pick: int,
            *,
            pfr: str = "",
            gsis: str = "",
            cfb: str = "",
            hof: bool = False,
            allpro: int = 0,
            probowls: int = 0,
        ) -> dict[str, object]:
            return {
                "season": season,
                "round": (pick - 1) // 32 + 1,
                "pick": pick,
                "team": "NFL",
                "pfr_player_id": pfr,
                "gsis_id": gsis,
                "cfb_player_id": cfb,
                "pfr_player_name": name,
                "college": college,
                "age": 22,
                "hof": hof,
                "allpro": allpro,
                "probowls": probowls,
            }

        combine_rows = [
            combine(2022, "Pfr Player", "Alpha", pfr="p1"),
            combine(2023, "Gsis Player", "Beta"),
            combine(2024, "Cfb Player", "Gamma", cfb="c3"),
            combine(2025, "Name Player", "Delta"),
            combine(2026, "Retired Player", "Echo", pfr="p5"),
            combine(2026, "Free Agent", "Foxtrot", pfr="p6"),
            combine(2026, "Ambiguous Player", "Same College"),
            combine(2026, "Conflict Player", "Hotel", pfr="p8"),
            combine(2026, "Missing Measure", "India", pfr="p9", measurements=False),
            combine(2026, "Active Undrafted", "Juliet", pfr="u1"),
        ]
        draft_rows = [
            draft(2022, "Pfr Player", "Alpha", 10, pfr="p1", gsis="g1", allpro=1),
            draft(2023, "Gsis Player", "Beta", 70, gsis="g2", probowls=3),
            draft(2024, "Cfb Player", "Gamma", 30, cfb="c3", hof=True),
            draft(2025, "Name Player", "Delta", 100),
            draft(2026, "Retired Player", "Echo", 20, pfr="p5"),
            draft(2026, "Free Agent", "Foxtrot", 90, pfr="p6"),
            draft(2026, "Ambiguous Player", "Same College", 150),
            draft(2026, "Conflict Player", "Hotel", 40, pfr="p8", gsis="wrong-gsis"),
            draft(2026, "Missing Measure", "India", 120, pfr="p9"),
        ]
        roster_rows = [
            {"season": 2026, "week": 1, "full_name": "Pfr Player", "college": "Alpha", "pfr_id": "p1", "gsis_id": "g1", "status": "ACT", "team": "AAA"},
            {"season": 2026, "week": 1, "full_name": "Gsis Player", "college": "Beta", "gsis_id": "g2", "status": "RES", "team": "BBB"},
            {"season": 2026, "week": 1, "full_name": "Cfb Player", "college": "Gamma", "cfb_id": "c3", "status": "PUP", "team": "CCC"},
            {"season": 2026, "week": 1, "full_name": "Name Player", "college": "Delta; Other College", "status": "E14", "team": "DDD"},
            {"season": 2026, "week": 1, "full_name": "Retired Player", "college": "Echo", "pfr_id": "p5", "status": "RET", "team": "EEE"},
            {"season": 2026, "week": 1, "full_name": "Free Agent", "college": "Foxtrot", "pfr_id": "p6", "status": "UFA", "team": "FFF"},
            {"season": 2026, "week": 1, "full_name": "Ambiguous Player", "college": "Same College", "status": "ACT", "team": "GGG"},
            {"season": 2026, "week": 1, "full_name": "Ambiguous Player", "college": "Same College", "status": "ACT", "team": "HHH"},
            {"season": 2026, "week": 1, "full_name": "Conflict Player", "college": "Hotel", "pfr_id": "p8", "gsis_id": "right-gsis", "status": "ACT", "team": "PFR"},
            {"season": 2026, "week": 1, "full_name": "Other Identity", "college": "Other", "gsis_id": "wrong-gsis", "status": "ACT", "team": "GSIS"},
            {"season": 2026, "week": 1, "full_name": "Missing Measure", "college": "India", "pfr_id": "p9", "status": "DEV", "team": "III"},
            {"season": 2026, "week": 1, "full_name": "Active Undrafted", "college": "Juliet", "pfr_id": "u1", "status": "INA", "team": "JJJ"},
            # An older row must not affect the current snapshot.
            {"season": 2026, "week": 0, "full_name": "Pfr Player", "college": "Alpha", "pfr_id": "p1", "status": "CUT", "team": "OLD"},
        ]

        rows, metadata = _build_nflverse_history_rows(
            combine_rows,
            draft_rows,
            roster_rows,
            start=2022,
            latest=2026,
            lookback_years=5,
            active_roster_season=2026,
            roster_source_metadata={
                "downloaded_at": "2026-08-25T17:52:37-04:00",
                "sha256": "fixture-sha",
            },
            contributor_outcomes={
                (2022, "p1"): {
                    "nfl_three_year_horizon_start": 2022,
                    "nfl_three_year_horizon_end": 2024,
                    "nfl_three_year_outcome_known": True,
                    "nfl_three_year_right_censored": False,
                    "nfl_three_year_contributor": True,
                    "nfl_three_year_primary_snaps": 700,
                    "nfl_three_year_special_teams_snaps": 0,
                    "nfl_three_year_total_snaps": 700,
                    "nfl_three_year_outcome_source": "nflverse_pfr_snap_counts",
                }
            },
            contributor_metadata={
                "status": "pass",
                "completed_nfl_season": 2024,
                "horizon_seasons": 3,
                "known_outcomes": 1,
                "contributors": 1,
            },
        )
        by_name = {row["name"]: row for row in rows}

        self.assertEqual(len(rows), 10)  # Full outcome history is preserved.
        self.assertTrue(by_name["Active Undrafted"]["active_roster_status_eligible"])
        self.assertFalse(by_name["Active Undrafted"]["benchmark_cohort_eligible"])
        self.assertFalse(by_name["Retired Player"]["benchmark_cohort_eligible"])
        self.assertFalse(by_name["Free Agent"]["benchmark_cohort_eligible"])
        self.assertFalse(by_name["Ambiguous Player"]["benchmark_cohort_eligible"])
        self.assertFalse(by_name["Missing Measure"]["benchmark_cohort_eligible"])
        self.assertTrue(by_name["Pfr Player"]["active_three_year_contributor_eligible"])
        self.assertEqual(by_name["Pfr Player"]["nfl_all_pro_selections"], 1)
        self.assertTrue(by_name["Pfr Player"]["nfl_career_elite"])
        self.assertEqual(by_name["Gsis Player"]["nfl_pro_bowls"], 3)
        self.assertTrue(by_name["Gsis Player"]["nfl_career_elite"])
        self.assertTrue(by_name["Cfb Player"]["nfl_hof"])
        self.assertTrue(by_name["Cfb Player"]["nfl_career_elite"])
        self.assertTrue(
            by_name["Pfr Player"]["benchmark_active_contributor_cohort_eligible"]
        )
        self.assertFalse(
            by_name["Missing Measure"]["benchmark_active_contributor_cohort_eligible"]
        )
        self.assertTrue(by_name["Name Player"]["nfl_three_year_right_censored"])
        self.assertEqual(by_name["Pfr Player"]["active_roster_match"], "pfr_id")
        self.assertEqual(by_name["Gsis Player"]["active_roster_match"], "gsis_id")
        self.assertEqual(by_name["Cfb Player"]["active_roster_match"], "cfb_id")
        self.assertEqual(by_name["Name Player"]["active_roster_match"], "unique_name_college")
        self.assertEqual(by_name["Conflict Player"]["active_roster_team"], "PFR")
        self.assertTrue(by_name["Conflict Player"]["active_roster_join_conflict"])
        self.assertFalse(by_name["Conflict Player"]["active_roster_identity_eligible"])
        self.assertFalse(by_name["Conflict Player"]["benchmark_cohort_eligible"])
        self.assertEqual(metadata["active_roster_snapshot_week"], 1)
        self.assertEqual(metadata["active_roster_source_as_of"], "2026-08-25T17:52:37-04:00")
        self.assertEqual(metadata["benchmark_rows"], 4)
        self.assertEqual(metadata["benchmark_elite_rows"], 2)
        self.assertEqual(metadata["historical_career_elite_rows"], 3)
        self.assertIn("first-team All-Pro", metadata["historical_career_elite_definition"])
        self.assertEqual(metadata["active_drafted_window_rows"], 5)
        self.assertEqual(len(metadata["active_drafted_players"]), 5)
        self.assertEqual(metadata["active_contributor_window_rows"], 1)
        self.assertEqual(metadata["benchmark_active_contributor_rows"], 1)
        self.assertEqual(len(metadata["active_contributor_players"]), 1)
        self.assertEqual(
            metadata["nfl_three_year_contributor_outcome"]["known_outcomes"], 1
        )
        self.assertEqual(
            {row["name"] for row in metadata["active_drafted_players"]},
            {"Pfr Player", "Gsis Player", "Cfb Player", "Name Player", "Missing Measure"},
        )
        self.assertAlmostEqual(metadata["benchmark_cohort_coverage"], 4 / 5)
        self.assertEqual(metadata["active_roster_status_counts"]["RET"], 1)
        self.assertEqual(metadata["active_roster_status_counts"]["UFA"], 1)
        self.assertEqual(metadata["draft_window_roster_join_conflicts"], 1)

    def test_latest_completed_draft_class_is_complete_and_reference_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir)
            draft_rows = [
                {
                    "season": 2025,
                    "round": (pick - 1) // 32 + 1,
                    "pick": pick,
                    "team": "NFL",
                    "pfr_player_id": f"p{pick}",
                    "cfb_player_id": f"c{pick}",
                    "pfr_player_name": f"Drafted Player {pick}",
                    "position": "WR",
                    "college": "Example",
                    "age": 22,
                    "hof": "TRUE" if pick == 1 else "FALSE",
                    "allpro": 1 if pick == 2 else 0,
                    "probowls": 3 if pick == 3 else 0,
                }
                for pick in range(1, 201)
            ]
            draft_rows.extend(
                {
                    **draft_rows[pick - 1],
                    "season": 2026,
                    "pfr_player_id": f"future-{pick}",
                    "cfb_player_id": f"future-cfb-{pick}",
                    "pfr_player_name": f"Incomplete Player {pick}",
                }
                for pick in range(1, 11)
            )
            combine_rows = [
                {
                    "season": 2025,
                    "pfr_id": "p1",
                    "cfb_id": "c1",
                    "player_name": "Drafted Player 1",
                    "pos": "WR",
                    "school": "Example",
                    "ht": "6-2",
                    "wt": 205,
                    "forty": 4.4,
                    "bench": "",
                    "vertical": "",
                    "broad_jump": "",
                    "cone": "",
                    "shuttle": "",
                }
            ]

            for path, source_rows in (
                (cache / "draft_picks.csv", draft_rows),
                (cache / "combine.csv", combine_rows),
            ):
                with path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=list(source_rows[0]))
                    writer.writeheader()
                    writer.writerows(source_rows)

            rows, metadata = load_latest_completed_draft_class(cache)

        self.assertEqual(metadata["draft_year"], 2025)
        self.assertEqual(len(rows), 200)
        self.assertTrue(all(row["reference_only"] for row in rows))
        self.assertTrue(all(row["drafted"] for row in rows))
        first = rows[0]
        self.assertEqual(first["draft_ovr"], 1)
        self.assertEqual(first["nfl_team"], "NFL")
        self.assertTrue(first["nfl_hof"])
        self.assertTrue(first["nfl_career_elite"])
        self.assertTrue(first["combine_measurement_available"])
        missing_combine = rows[-1]
        self.assertEqual(missing_combine["combine_match_method"], "missing_combine_row")
        self.assertFalse(missing_combine["combine_measurement_available"])
        self.assertIsNone(missing_combine.get("height_in"))

    def test_build_week_matched_leaver_history(self) -> None:
        rows, metadata = build_cfbd_weekly_history(
            FakeCFBD(), target_season=2024, as_of_week=4, lookback_years=2
        )
        self.assertEqual(len(rows), 2)
        by_id = {row["player_id"]: row for row in rows}
        self.assertTrue(by_id["a"]["drafted"])
        self.assertFalse(by_id["b"]["drafted"])
        self.assertAlmostEqual(by_id["a"]["observed_completion_pct"], 2 / 3)
        self.assertAlmostEqual(by_id["a"]["prod_completion_pct"], (20 + 0.60 * 25) / 55)
        self.assertAlmostEqual(by_id["a"]["prod_yards_per_attempt"], (280 + 7.0 * 25) / 55)
        self.assertEqual(metadata["as_of_week"], 4)
        self.assertEqual(metadata["probability_kind"], "conditional_on_entry")
        self.assertEqual(by_id["a"]["probability_kind"], "conditional_on_entry")

    def test_current_week_returns_only_fully_completed_week(self) -> None:
        client = CFBDClient(api_key="test")
        client.get = lambda path, **params: [  # type: ignore[method-assign]
            {"week": 1, "startDate": "2026-09-01T00:00:00Z", "endDate": "2026-09-07T23:59:59Z"},
            {"week": 2, "startDate": "2026-09-08T00:00:00Z", "endDate": "2026-09-14T23:59:59Z"},
            {"week": 3, "startDate": "2026-09-15T00:00:00Z", "endDate": "2026-09-21T23:59:59Z"},
        ]
        self.assertEqual(
            client.current_week(year=2026, now=datetime(2026, 9, 10, tzinfo=timezone.utc)),
            1,
        )
        self.assertEqual(
            client.current_week(year=2026, now=datetime(2026, 9, 7, 23, 59, 59, tzinfo=timezone.utc)),
            1,
        )
        self.assertEqual(client.current_week(year=2026, now=datetime(2026, 8, 30)), 0)

    def test_refresh_candidate_uses_week_bounded_player_stats(self) -> None:
        client = CFBDClient(api_key="test")
        calls: list[tuple[str, dict]] = []

        def fake_get(path: str, **params):
            calls.append((path, params))
            if path == "/player/search":
                return [
                    {
                        "id": "p1",
                        "name": "Bounded Quarterback",
                        "team": "Alpha",
                        "position": "QB",
                        "height": 75,
                        "weight": 220,
                    }
                ]
            if path == "/stats/player/season":
                return [
                    {"playerId": "p1", "player": "Bounded Quarterback", "team": "Alpha", "conference": "X", "position": "QB", "category": "passing", "statType": "C/ATT", "stat": "30/45"},
                    {"playerId": "p1", "player": "Bounded Quarterback", "team": "Alpha", "conference": "X", "position": "QB", "category": "passing", "statType": "YDS", "stat": 400},
                    {"playerId": "p1", "player": "Bounded Quarterback", "team": "Alpha", "conference": "X", "position": "QB", "category": "passing", "statType": "TD", "stat": 4},
                    {"playerId": "other", "player": "Other Player", "team": "Alpha", "conference": "X", "position": "QB", "category": "passing", "statType": "YDS", "stat": 9999},
                ]
            if path == "/stats/player/success":
                return [
                    {
                        "id": "p1",
                        "team": "Alpha",
                        "passing": {"plays": 45, "successes": 24, "successRate": 24 / 45},
                        "rushing": {"plays": 8, "successes": 5, "successRate": 0.625},
                    }
                ]
            raise AssertionError(path)

        client.get = fake_get  # type: ignore[method-assign]
        refreshed, raw = client.refresh_candidate(
            {
                "name": "Bounded Quarterback",
                "school": "Alpha",
                "prod_pass_yards": 9999,
                "prod_usage_rate": 0.95,
            },
            season=2026,
            week=4,
        )

        stats_calls = [(path, params) for path, params in calls if path == "/stats/player/season"]
        self.assertEqual(len(stats_calls), 1)
        self.assertEqual(
            stats_calls[0][1],
            {"year": 2026, "endWeek": 4, "seasonType": "both", "team": "Alpha"},
        )
        self.assertNotIn("/player/season/overview", [path for path, _params in calls])
        success_call = next(params for path, params in calls if path == "/stats/player/success")
        self.assertEqual(
            success_call,
            {
                "year": 2026,
                "endWeek": 4,
                "seasonType": "both",
                "team": "Alpha",
                "playerId": "p1",
                "excludeGarbageTime": False,
                "threshold": 0,
            },
        )
        self.assertEqual(refreshed["prod_pass_yards"], 400)
        self.assertEqual(refreshed["prod_passing_success_plays"], 45)
        self.assertAlmostEqual(refreshed["prod_passing_success_rate"], 24 / 45)
        self.assertEqual(refreshed["prod_rushing_success_plays"], 8)
        self.assertNotIn("prod_usage_rate", refreshed)
        self.assertEqual(refreshed["height_in"], 75)
        self.assertEqual(refreshed["weight_lb"], 220)
        self.assertEqual(refreshed["as_of_week"], 4)
        self.assertEqual(raw["name"], "Bounded Quarterback")

    def test_discovers_and_transparently_ranks_national_fbs_pool(self) -> None:
        class DiscoveryClient:
            supports_player_success = True
            supports_recruiting = True

            def __init__(self) -> None:
                self.calls: list[tuple[str, dict]] = []

            def get(self, path: str, **params):
                self.calls.append((path, params))
                if path == "/roster":
                    return [
                        {"id": "q1", "firstName": "Top", "lastName": "Quarterback", "team": "Alpha", "position": "QB", "height": 76, "weight": 225},
                        {"id": "q2", "firstName": "Lower", "lastName": "Quarterback", "team": "Beta", "position": "QB", "height": 74, "weight": 210},
                    ]
                if path == "/stats/player/season":
                    return [
                        {"playerId": "q1", "player": "Top Quarterback", "team": "Alpha", "conference": "X", "position": "QB", "category": "passing", "statType": "C/ATT", "stat": "80/100"},
                        {"playerId": "q1", "player": "Top Quarterback", "team": "Alpha", "conference": "X", "position": "QB", "category": "passing", "statType": "YDS", "stat": 1200},
                        {"playerId": "q1", "player": "Top Quarterback", "team": "Alpha", "conference": "X", "position": "QB", "category": "passing", "statType": "TD", "stat": 12},
                        {"playerId": "q1", "player": "Top Quarterback", "team": "Alpha", "conference": "X", "position": "QB", "category": "passing", "statType": "INT", "stat": 1},
                        {"playerId": "q2", "player": "Lower Quarterback", "team": "Beta", "conference": "Y", "position": "QB", "category": "passing", "statType": "C/ATT", "stat": "40/80"},
                        {"playerId": "q2", "player": "Lower Quarterback", "team": "Beta", "conference": "Y", "position": "QB", "category": "passing", "statType": "YDS", "stat": 500},
                        {"playerId": "q2", "player": "Lower Quarterback", "team": "Beta", "conference": "Y", "position": "QB", "category": "passing", "statType": "TD", "stat": 3},
                        {"playerId": "q2", "player": "Lower Quarterback", "team": "Beta", "conference": "Y", "position": "QB", "category": "passing", "statType": "INT", "stat": 5},
                        {"playerId": "not-fbs", "player": "Excluded", "team": "Other", "conference": "Z", "position": "QB", "category": "passing", "statType": "YDS", "stat": 5000},
                    ]
                if path == "/stats/player/success":
                    return [
                        {"id": "q1", "team": "Alpha", "passing": {"plays": 100, "successes": 62, "successRate": 0.62}, "rushing": {"plays": 10, "successes": 6, "successRate": 0.60}},
                        {"id": "q2", "team": "Beta", "passing": {"plays": 80, "successes": 30, "successRate": 0.375}, "rushing": {"plays": 8, "successes": 3, "successRate": 0.375}},
                    ]
                if path == "/recruiting/players":
                    if params.get("year") == 2023:
                        return [
                            {
                                "id": "recruit-q1",
                                "athleteId": "q1",
                                "year": 2023,
                                "name": "Top Quarterback",
                                "rating": 0.955,
                                "stars": 4,
                                "ranking": 120,
                            }
                        ]
                    return []

        client = DiscoveryClient()
        candidates = discover_cfbd_candidates(client, season=2026, as_of_week=3)
        self.assertEqual([row["player_id"] for row in candidates], ["q1", "q2"])
        self.assertGreater(candidates[0]["discovery_score"], candidates[1]["discovery_score"])
        self.assertEqual(candidates[0]["discovery_rank"], 1)
        self.assertEqual(candidates[0]["height_in"], 76)
        self.assertIn("pass_yards", candidates[0]["discovery_metric_scores"])
        self.assertIn("passing_success_rate", candidates[0]["discovery_metric_scores"])
        self.assertEqual(candidates[0]["prod_passing_success_plays"], 100)
        self.assertEqual(candidates[0]["recruit_rating"], 0.955)
        self.assertEqual(candidates[0]["recruit_match_method"], "athlete_id")
        stats_call = next(params for path, params in client.calls if path == "/stats/player/season")
        self.assertEqual(stats_call, {"year": 2026, "seasonType": "both", "endWeek": 3})
        success_call = next(params for path, params in client.calls if path == "/stats/player/success")
        self.assertEqual(
            success_call,
            {
                "year": 2026,
                "seasonType": "both",
                "excludeGarbageTime": False,
                "threshold": 0,
                "endWeek": 3,
            },
        )

    def test_preseason_discovery_uses_prior_stats_with_current_roster_identity(self) -> None:
        class PreseasonClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict]] = []

            def get(self, path: str, **params):
                self.calls.append((path, params))
                if path == "/roster":
                    return [
                        {"id": "transfer", "firstName": "Portal", "lastName": "Player", "team": "New U", "position": "WR", "height": 73, "weight": 195}
                    ]
                if path == "/stats/player/season":
                    return [
                        {"playerId": "transfer", "player": "Portal Player", "team": "Old U", "conference": "Old", "position": "WR", "category": "receiving", "statType": "REC", "stat": 50},
                        {"playerId": "transfer", "player": "Portal Player", "team": "Old U", "conference": "Old", "position": "WR", "category": "receiving", "statType": "YDS", "stat": 700},
                        {"playerId": "transfer", "player": "Portal Player", "team": "Old U", "conference": "Old", "position": "WR", "category": "receiving", "statType": "TD", "stat": 6},
                    ]

        client = PreseasonClient()
        candidates = discover_cfbd_candidates(client, season=2026, as_of_week=0)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["school"], "New U")
        self.assertEqual(candidates[0]["production_school"], "Old U")
        self.assertEqual(candidates[0]["production_season"], 2025)
        self.assertTrue(candidates[0]["discovery_preseason_prior_year"])
        stats_call = next(params for path, params in client.calls if path == "/stats/player/season")
        self.assertEqual(stats_call, {"year": 2025, "seasonType": "both"})

    def test_discovery_keeps_roster_only_linemen_and_specialists(self) -> None:
        class RosterCompleteClient:
            def get(self, path: str, **params):
                if path == "/roster":
                    return [
                        {
                            "id": "qb",
                            "firstName": "Productive",
                            "lastName": "Quarterback",
                            "team": "Alpha",
                            "position": "QB",
                            "height": 75,
                            "weight": 220,
                            "year": "JR",
                        },
                        {
                            "id": "ot-sr",
                            "firstName": "Senior",
                            "lastName": "Tackle",
                            "team": "Alpha",
                            "position": "OT",
                            "height": 78,
                            "weight": 320,
                            "year": "SR",
                        },
                        {
                            "id": "ot-so",
                            "firstName": "Young",
                            "lastName": "Tackle",
                            "team": "Beta",
                            "position": "OT",
                            "height": 77,
                            "year": "SO",
                        },
                        {
                            "id": "iol",
                            "firstName": "Graduate",
                            "lastName": "Guard",
                            "team": "Gamma",
                            "position": "OL",
                            "height": 75,
                            "weight": 315,
                            "year": "GR",
                        },
                        {
                            "id": "k",
                            "firstName": "Roster",
                            "lastName": "Kicker",
                            "team": "Alpha",
                            "position": "PK",
                            "height": 72,
                            "weight": 190,
                            "year": "SR",
                        },
                        {
                            "id": "p",
                            "firstName": "Roster",
                            "lastName": "Punter",
                            "team": "Beta",
                            "position": "P",
                            "height": 74,
                            "weight": 205,
                            "year": "SR",
                        },
                        {
                            "id": "ls",
                            "firstName": "Roster",
                            "lastName": "Snapper",
                            "team": "Gamma",
                            "position": "LS",
                            "height": 74,
                            "weight": 235,
                            "year": "SR",
                        },
                        {
                            "id": "ath",
                            "firstName": "Unsupported",
                            "lastName": "Athlete",
                            "team": "Gamma",
                            "position": "ATH",
                        },
                    ]
                if path == "/stats/player/season":
                    return [
                        {
                            "playerId": "qb",
                            "player": "Productive Quarterback",
                            "team": "Alpha",
                            "conference": "X",
                            "position": "QB",
                            "category": "passing",
                            "statType": "YDS",
                            "stat": 1200,
                        },
                        # A recorded CFBD row still does not provide a mapped
                        # offensive-line production metric.
                        {
                            "playerId": "ot-sr",
                            "player": "Senior Tackle",
                            "team": "Alpha",
                            "conference": "X",
                            "position": "OT",
                            "category": "blocking",
                            "statType": "SNAPS",
                            "stat": 700,
                        },
                    ]
                raise AssertionError(path)

        candidates = discover_cfbd_candidates(
            RosterCompleteClient(), season=2026, as_of_week=4
        )
        by_id = {row["player_id"]: row for row in candidates}

        self.assertEqual(set(by_id), {"qb", "ot-sr", "ot-so", "iol", "k", "p", "ls"})
        self.assertEqual(candidates[0]["player_id"], "qb")
        self.assertEqual(by_id["iol"]["position"], "IOL")
        self.assertEqual(by_id["k"]["position"], "K")
        self.assertEqual(by_id["ot-so"]["discovery_match"], "current_roster_only")
        self.assertEqual(by_id["ot-so"]["has_recorded_stats"], 0.0)
        self.assertEqual(by_id["ot-so"]["discovery_fallback_reason"], "no_player_season_box_score_rows")
        self.assertEqual(by_id["ot-sr"]["has_recorded_stats"], 1.0)
        self.assertEqual(
            by_id["ot-sr"]["discovery_fallback_reason"],
            "no_mapped_position_production_metrics",
        )
        self.assertEqual(
            by_id["ot-sr"]["discovery_score_kind"],
            "roster_metadata_fallback_not_draft_probability",
        )
        self.assertEqual(by_id["ot-sr"]["discovery_metric_count"], 0)
        self.assertLess(
            by_id["ot-sr"]["discovery_position_rank"],
            by_id["ot-so"]["discovery_position_rank"],
        )
        self.assertGreater(
            by_id["ot-sr"]["discovery_score"], by_id["ot-so"]["discovery_score"]
        )

    def test_career_elite_catalog_extends_beyond_window_and_keeps_reference_only(self) -> None:
        draft_rows = [
            {
                "season": "1980",
                "pfr_player_id": "OldEl00",
                "pfr_player_name": "Old Elite",
                "position": "DB",
                "college": "Legacy State",
                "round": "2",
                "pick": "40",
                "allpro": "1",
                "probowls": "0",
                "hof": "false",
            },
            {
                "season": "1981",
                "pfr_player_id": "Ordinary00",
                "pfr_player_name": "Ordinary Player",
                "position": "DB",
                "allpro": "0",
                "probowls": "2",
                "hof": "false",
            },
        ]
        player_rows = [
            {
                "pfr_id": "OldEl00",
                "display_name": "Old Elite",
                "position": "CB",
                "height": "72",
                "weight": "195",
            },
            {
                "pfr_id": "WrongLe00",
                "display_name": "Undrafted Legend",
                "position": "DT",
                "height": "78",
                "weight": "310",
            },
        ]
        hof_profiles = [
            {
                "name": "Undrafted Legend",
                "position": "QB",
                "position_raw": "Quarterback",
                "height_in": 75,
                "weight_lb": 220,
                "pfr_id": "RightLe00",
                "wikidata_id": "Q-test",
                "wikipedia_url": "https://en.wikipedia.org/wiki/Undrafted_Legend",
                "nfl_career_start": 1950,
                "nfl_career_end": 1960,
            }
        ]

        rows, metadata = _build_nfl_career_elite_rows(
            [],
            draft_rows,
            player_rows,
            hof_profiles,
            end_year=2026,
            all_era_hof_complete=True,
        )
        by_name = {row["name"]: row for row in rows}

        self.assertEqual(set(by_name), {"Old Elite", "Undrafted Legend"})
        self.assertEqual(by_name["Old Elite"]["position"], "CB")
        self.assertEqual(
            by_name["Old Elite"]["measurement_source"],
            "nflverse_player_registry",
        )
        self.assertFalse(by_name["Old Elite"]["measurements_verified"])
        self.assertIsNone(by_name["Undrafted Legend"]["drafted"])
        self.assertEqual(by_name["Undrafted Legend"]["position"], "QB")
        self.assertEqual(by_name["Undrafted Legend"]["weight_lb"], 220)
        self.assertEqual(by_name["Undrafted Legend"]["pfr_id"], "RightLe00")
        self.assertTrue(by_name["Undrafted Legend"]["nfl_hof"])
        self.assertTrue(by_name["Undrafted Legend"]["nfl_career_elite"])
        self.assertFalse(by_name["Undrafted Legend"]["measurements_verified"])
        self.assertTrue(all(row["reference_only"] for row in rows))
        self.assertFalse(metadata["model_feature_eligible"])
        self.assertIn("all NFL eras", metadata["scope_label"])

        _partial_rows, partial_metadata = _build_nfl_career_elite_rows(
            [], draft_rows, player_rows, [], end_year=2026
        )
        self.assertFalse(partial_metadata["all_era_hof_available"])
        self.assertNotIn("all NFL eras", partial_metadata["scope_label"])
        self.assertIn("must not be described as all-era", partial_metadata["coverage_note"])

    def test_hof_table_parser_excludes_non_player_induction_roles(self) -> None:
        html = """
        <table><tr><th>Legend</th></tr></table>
        <table class="wikitable sortable">
        <tr><th>Inductee</th><th>Class</th><th>Position</th><th>Team(s)</th><th>Years</th></tr>
        <tr><td rowspan="2"><span class="fn"><a href="/wiki/Jim_Legend">Jim Legend</a></span></td>
        <td rowspan="2">1971</td><td rowspan="2" data-sort-value="Fullback 1971">Fullback/placekicker</td><td>Team A</td><td>1957–1965</td></tr>
        <tr><td>Team B</td><td>1966–1968</td></tr>
        <tr><td><span class="fn"><a href="/wiki/Jim_Legend">Jim Legend</a></span></td>
        <td>1971</td><td>Safety</td><td>Team C</td><td>1969</td></tr>
        <tr><td><span class="fn"><a href="/wiki/Pat_Executive">Pat Executive</a></span></td>
        <td>2000</td><td>Team owner</td><td>Team</td><td>1960–1990</td></tr></table>
        <table class="wikitable sortable">
        <tr><th>Inductee</th><th>Class</th><th>Position</th><th>League(s)</th><th>Team(s)</th><th>Years</th></tr>
        <tr><td><span class="fn"><a href="/wiki/Pat_Coach">Pat Coach</a></span></td>
        <td>2001</td><td>Guard</td><td>Minor league</td><td>Team</td><td>1930</td></tr></table>
        """

        rows = _parse_hof_table(html)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Jim Legend")
        self.assertEqual(rows[0]["position"], "RB")
        self.assertEqual(rows[0]["comparison_positions"], "RB;K;S")
        self.assertEqual(rows[0]["nfl_career_start"], 1957)
        self.assertEqual(rows[0]["nfl_career_end"], 1969)

    def test_hof_registry_completeness_is_not_a_loose_row_floor(self) -> None:
        rows = []
        category_pages: dict[str, dict[str, str]] = {}
        page_index = 0
        for year in range(1963, 2026):
            for member in range(4):
                title = f"Hall Player {year} {member}"
                rows.append(
                    f'<tr><td><span class="fn"><a href="/wiki/{title.replace(" ", "_")}">{title}</a></span></td>'
                    f"<td>{year}</td><td>Quarterback</td><td>Team</td><td>1950–1960</td></tr>"
                )
                category_pages[str(page_index)] = {"title": title}
                page_index += 1
        for missing in range(1):
            category_pages[str(page_index)] = {"title": f"Missing Member {missing}"}
            page_index += 1
        html = (
            '<table class="wikitable sortable"><tr><th>Inductee</th><th>Class</th>'
            "<th>Position</th><th>Team(s)</th><th>Years</th></tr>"
            + "".join(rows)
            + '<tr><td><span class="fn"><a href="/wiki/Broken_Row">Broken Row</a>'
            "</span></td><td>2025</td></tr>"
            + "</table>"
        )
        profiles = _parse_hof_table(html)

        audit = _hof_registry_audit(
            html,
            profiles,
            {"query": {"pages": category_pages}},
            expected_latest_class=2025,
        )

        self.assertGreater(len(profiles), 250)
        self.assertTrue(audit["profile_key_match"])
        self.assertTrue(audit["class_audit_complete"])
        self.assertFalse(audit["category_complete"])
        self.assertFalse(audit["registry_complete"])
        self.assertEqual(audit["malformed_rows"][0]["logical_cell_count"], 2)

    def test_source_backed_hof_position_overrides_keep_legacy_safeties(self) -> None:
        bobby = {"name": "Bobby Dillon"}
        sammy = {"name": "Sammy Baugh", "position": "QB"}

        self.assertEqual(_apply_hof_position_override(bobby, []), ["S"])
        self.assertEqual(_apply_hof_position_override(sammy, ["QB", "P"]), ["QB", "P", "S"])
        self.assertEqual(bobby["position"], "S")
        self.assertEqual(sammy["position"], "QB")
        self.assertIn("profootballhof.com", bobby["position_source"])

    def test_historical_honors_overlay_adds_pre_1980_and_undrafted_elites(self) -> None:
        draft_rows = [
            {
                "season": "1980",
                "pfr_player_id": "Covered00",
                "pfr_player_name": "Covered Disagreement",
                "position": "OT",
                "allpro": "0",
                "probowls": "0",
                "hof": "false",
            }
        ]
        honors = [
            {
                "name": "Old All Pro",
                "pfr_id": "OldAP00",
                "position": "OT",
                "comparison_positions": ["OT"],
                "height_in": 76,
                "weight_lb": 275,
                "nfl_all_pro_selections": 2,
                "nfl_pro_bowls": 1,
                "nfl_first_team_all_pro_years": [1968, 1969],
                "wikipedia_page": "Old All Pro",
            },
            {
                "name": "Undrafted Bowl Star",
                "pfr_id": "UndrPB00",
                "position": "S",
                "comparison_positions": ["S"],
                "height_in": 72,
                "weight_lb": 200,
                "nfl_all_pro_selections": 0,
                "nfl_pro_bowls": 3,
                "nfl_pro_bowl_years": [1970, 1971, 1972],
                "wikipedia_page": "Undrafted Bowl Star",
            },
            {
                "name": "Covered Disagreement",
                "pfr_id": "Covered00",
                "position": "OT",
                "comparison_positions": ["OT"],
                "nfl_all_pro_selections": 1,
                "nfl_pro_bowls": 0,
                "wikipedia_page": "Covered Disagreement",
            },
        ]
        honors_metadata = {
            "first_team_all_pro": {
                "published": True,
                "source_population_complete": True,
                "coverage_start": 1920,
                "coverage_end": 2025,
            },
            "pro_bowl": {
                "published": True,
                "source_population_complete": True,
                "coverage_start": 1950,
                "coverage_end": 2025,
            },
        }

        rows, metadata = _build_nfl_career_elite_rows(
            [],
            draft_rows,
            [],
            [],
            honors,
            end_year=2026,
            all_era_hof_complete=True,
            historical_honors_metadata=honors_metadata,
        )
        by_name = {row["name"]: row for row in rows}

        self.assertEqual(
            set(by_name),
            {"Old All Pro", "Undrafted Bowl Star", "Covered Disagreement"},
        )
        self.assertEqual(by_name["Old All Pro"]["nfl_all_pro_selections"], 2)
        self.assertEqual(by_name["Undrafted Bowl Star"]["nfl_pro_bowls"], 3)
        self.assertEqual(
            by_name["Covered Disagreement"]["nfl_all_pro_selections"], 1
        )
        self.assertTrue(by_name["Covered Disagreement"]["nfl_career_elite"])
        self.assertEqual(
            by_name["Covered Disagreement"]["nfl_honors_source"],
            "nflverse_and_wikimedia_historical_honors_reconciled",
        )
        self.assertFalse(by_name["Old All Pro"]["nfl_hof"])
        self.assertTrue(all(row["reference_only"] for row in rows))
        self.assertEqual(metadata["historical_honors_rows_added"], 3)
        self.assertEqual(
            len(metadata["historical_honors_covered_source_disagreements"]), 1
        )
        self.assertIn(
            "qualifying evidence included",
            metadata["historical_honors_covered_source_disagreements"][0][
                "resolution"
            ],
        )
        self.assertIn("1920–2025", metadata["scope_label"])
        self.assertTrue(metadata["full_history_sources_available"])
        self.assertTrue(metadata["source_population_complete"])
        self.assertTrue(metadata["full_history_criteria_available"])

        _, partial_metadata = _build_nfl_career_elite_rows(
            [],
            [],
            [],
            [],
            honors[:1],
            end_year=2026,
            all_era_hof_complete=True,
            historical_honors_metadata={
                "first_team_all_pro": {
                    "published": True,
                    "coverage_start": 1920,
                    "coverage_end": 2025,
                },
                "pro_bowl": {"published": False},
            },
        )
        self.assertIn(
            "Pro Bowl component was unavailable",
            partial_metadata["coverage_note"],
        )

        _, incomplete_population_metadata = _build_nfl_career_elite_rows(
            [],
            [],
            [],
            [],
            honors[:1],
            end_year=2026,
            all_era_hof_complete=True,
            historical_honors_metadata={
                "first_team_all_pro": {
                    "published": True,
                    "source_population_complete": True,
                    "coverage_start": 1920,
                    "coverage_end": 2025,
                },
                "pro_bowl": {
                    "published": True,
                    "source_population_complete": False,
                    "coverage_start": 1950,
                    "coverage_end": 2025,
                    "population_coverage_note": (
                        "Some historical Pro Bowl seasons lack complete annual rosters."
                    ),
                },
            },
        )
        self.assertTrue(
            incomplete_population_metadata["full_history_sources_available"]
        )
        self.assertFalse(
            incomplete_population_metadata["source_population_complete"]
        )
        self.assertFalse(
            incomplete_population_metadata["full_history_criteria_available"]
        )
        self.assertFalse(
            incomplete_population_metadata[
                "full_history_position_coverage_complete"
            ]
        )
        self.assertIn(
            "all NFL eras",
            incomplete_population_metadata["scope_label"],
        )
        self.assertIn(
            "without proving full recall",
            incomplete_population_metadata["coverage_note"],
        )

    def test_combine_size_without_drills_keeps_combine_provenance(self) -> None:
        rows, _metadata = _build_nfl_career_elite_rows(
            [
                {
                    "season": "2010",
                    "pfr_id": "SizeOn00",
                    "player_name": "Size Only Elite",
                    "pos": "WR",
                    "ht": "73",
                    "wt": "205",
                }
            ],
            [
                {
                    "season": "2010",
                    "pfr_player_id": "SizeOn00",
                    "pfr_player_name": "Size Only Elite",
                    "position": "WR",
                    "allpro": "1",
                    "probowls": "0",
                    "hof": "false",
                }
            ],
            [],
        )

        self.assertEqual(rows[0]["measurement_source"], "nfl_combine")
        self.assertTrue(rows[0]["measurements_verified"])

    def test_invalid_combine_size_uses_valid_registry_values_and_field_provenance(self) -> None:
        rows, _metadata = _build_nfl_career_elite_rows(
            [
                {
                    "season": "2021",
                    "pfr_id": "Fallback00",
                    "player_name": "Fallback Elite",
                    "pos": "S",
                    "ht": "NA",
                    "wt": "NaN",
                    "forty": "4.50",
                }
            ],
            [
                {
                    "season": "2021",
                    "pfr_player_id": "Fallback00",
                    "pfr_player_name": "Fallback Elite",
                    "position": "S",
                    "allpro": "1",
                    "probowls": "0",
                    "hof": "false",
                }
            ],
            [
                {
                    "pfr_id": "Fallback00",
                    "display_name": "Fallback Elite",
                    "position": "S",
                    "height": "72",
                    "weight": "200",
                }
            ],
            [
                {
                    "name": "Fallback Elite",
                    "pfr_id": "Fallback00",
                    "position": "S",
                    "comparison_positions": "S",
                    "height_in": 72.0472,
                    "weight_lb": 200.6207,
                    "wikidata_measurement_provenance": "unselected Wikidata values",
                }
            ],
        )

        row = rows[0]
        provenance = json.loads(row["measurement_provenance"])
        self.assertEqual(row["height_in"], 72.0)
        self.assertEqual(row["weight_lb"], 200.0)
        self.assertEqual(
            row["measurement_source"],
            "pro_day_nonstandard_with_biographical_fallback",
        )
        self.assertEqual(
            provenance["height_in"]["source"], "nflverse_player_registry"
        )
        self.assertEqual(
            provenance["weight_lb"]["source"], "nflverse_player_registry"
        )
        self.assertEqual(provenance["testing"]["source"], "pro_day_nonstandard")
        self.assertNotIn("Wikidata", row["measurement_provenance"])

    def test_historical_honor_uses_specific_identity_linked_registry_role(self) -> None:
        rows, metadata = _build_nfl_career_elite_rows(
            [],
            [],
            [
                {
                    "pfr_id": "ReavJe00",
                    "display_name": "Jeremy Reaves",
                    "position": "DB",
                    "pff_position": "S",
                }
            ],
            [],
            [
                {
                    "name": "Jeremy Reaves",
                    "pfr_id": "R/ReavJe00",
                    "position_raw": "ST; Special teamer",
                    "comparison_positions": [],
                    "nfl_all_pro_selections": 1,
                    "nfl_pro_bowls": 1,
                    "wikipedia_page": "Jeremy Reaves",
                }
            ],
            all_era_hof_complete=True,
            historical_honors_metadata={
                "first_team_all_pro": {"published": True},
                "pro_bowl": {"published": True},
            },
        )

        self.assertEqual(rows[0]["position"], "S")
        self.assertEqual(rows[0]["comparison_positions"], "S")
        self.assertEqual(metadata["historical_honors_unpositioned_rows"], 0)

    def test_defensive_slash_shorthand_does_not_create_offensive_tackle_comps(self) -> None:
        self.assertEqual(
            _historical_positions("Defensive end/tackle"), ("EDGE", "IDL")
        )
        self.assertEqual(
            _historical_positions("Defensive tackle/end"), ("IDL", "EDGE")
        )

    def test_hof_table_parser_ignores_embedded_styles_and_separates_list_roles(self) -> None:
        html = """
        <table class="wikitable sortable">
        <tr><th>Inductee</th><th>Class</th><th>Position</th><th>Team(s)</th><th>Years</th></tr>
        <tr><td><span class="fn"><a href="/wiki/Bruce_Legend">Bruce Legend</a></span></td>
        <td>2007</td><td><style>.noise .guard { color: red; }</style>
        <div><ul><li>Guard</li><li>Center</li><li>Tackle</li></ul></div></td>
        <td>Team</td><td>1983–2001</td></tr></table>
        """

        rows = _parse_hof_table(html)

        self.assertEqual(rows[0]["position_raw"], "Guard Center Tackle")
        self.assertEqual(rows[0]["comparison_positions"], "IOL;OT")

    def test_wikidata_measurement_units_are_converted_explicitly(self) -> None:
        self.assertAlmostEqual(
            _wikidata_quantity(
                {
                    "value": "188",
                    "unit": "http://www.wikidata.org/entity/Q174728",
                },
                kind="height",
            ),
            74.015748,
            places=5,
        )
        self.assertAlmostEqual(
            _wikidata_quantity(
                {
                    "value": "105",
                    "unit": "http://www.wikidata.org/entity/Q11570",
                },
                kind="weight",
            ),
            231.485375,
            places=5,
        )

    def test_conflicting_wikidata_bio_measurements_are_withheld(self) -> None:
        resolution = _wikidata_measurement_resolution(
            [
                {
                    "height": {"value": "1.85"},
                    "heightUnit": {
                        "value": "http://www.wikidata.org/entity/Q11573"
                    },
                },
                {
                    "height": {"value": "1.88"},
                    "heightUnit": {
                        "value": "http://www.wikidata.org/entity/Q11573"
                    },
                },
            ],
            amount_field="height",
            unit_field="heightUnit",
            kind="height",
        )

        self.assertEqual(resolution["status"], "conflicting_statements_withheld")
        self.assertIsNone(resolution["selected_value"])
        self.assertEqual(len(resolution["source_statements"]), 2)

    def test_wikidata_scalar_resolution_normalizes_unique_values(self) -> None:
        dob = _wikidata_scalar_resolution(
            [
                {"dob": {"value": "+1988-04-12T00:00:00Z"}},
                {"dob": {"value": "1988-04-12"}},
            ],
            source_field="dob",
            kind="date",
        )
        pfr = _wikidata_scalar_resolution(
            [
                {"pfr": {"value": " https://www.pro-football-reference.com/players/R/RiceJe00.htm "}},
                {"pfr": {"value": "r/RiceJe00"}},
            ],
            source_field="pfr",
            kind="identifier",
        )

        self.assertEqual(dob["selected_value"], "1988-04-12")
        self.assertEqual(pfr["selected_value"], "RiceJe00")

    def test_historical_honors_wikidata_conflicts_and_positions_are_order_stable(self) -> None:
        bindings = [
            {
                "player": {"value": "http://www.wikidata.org/entity/Q1"},
                "pfr": {"value": "AlphaAa00"},
                "dob": {"value": "1980-01-01T00:00:00Z"},
                "positionLabel": {"value": "safety"},
            },
            {
                "player": {"value": "http://www.wikidata.org/entity/Q1"},
                "pfr": {"value": "BetaBb00"},
                "dob": {"value": "1981-02-02T00:00:00Z"},
                "positionLabel": {"value": "cornerback"},
            },
        ]

        def enrich(binding_order):
            profiles = [
                {
                    "name": "Conflict Player",
                    "wikidata_id": "Q1",
                    "comparison_positions": [],
                    "position_raw": "",
                }
            ]
            with tempfile.TemporaryDirectory() as directory, patch(
                "draftscope.data_sources._json_request",
                return_value={"results": {"bindings": binding_order}},
            ):
                metadata = _enrich_wikimedia_profiles(
                    profiles,
                    Path(directory),
                    refresh=False,
                    cache_prefix="fixture",
                )
            return profiles[0], metadata

        forward_profile, forward_metadata = enrich(bindings)
        reverse_profile, reverse_metadata = enrich(list(reversed(bindings)))

        self.assertNotIn("pfr_id", forward_profile)
        self.assertNotIn("date_of_birth", forward_profile)
        self.assertEqual(forward_profile["comparison_positions"], "CB;S")
        self.assertEqual(
            forward_profile["wikidata_identity_provenance"],
            reverse_profile["wikidata_identity_provenance"],
        )
        self.assertEqual(
            forward_profile["wikidata_position_provenance"],
            reverse_profile["wikidata_position_provenance"],
        )
        self.assertEqual(
            forward_metadata["identity_conflicts_withheld"],
            reverse_metadata["identity_conflicts_withheld"],
        )
        self.assertEqual(
            {row["field"] for row in forward_metadata["identity_conflicts_withheld"]},
            {"pfr_id", "date_of_birth"},
        )

    def test_hof_wikidata_conflicts_and_positions_are_order_stable(self) -> None:
        html = """
        <table class="wikitable sortable">
        <tr><th>Inductee</th><th>Class</th><th>Position</th><th>Team(s)</th><th>Years</th></tr>
        <tr><td><span class="fn"><a href="/wiki/Test_Legend">Test Legend</a></span></td>
        <td>2025</td><td>Defensive back</td><td>Test Team</td><td>1990–2000</td></tr>
        </table>
        """
        bindings = [
            {
                "player": {"value": "http://www.wikidata.org/entity/Q1"},
                "pfr": {"value": "AlphaAa00"},
                "dob": {"value": "1960-01-01T00:00:00Z"},
                "positionLabel": {"value": "safety"},
            },
            {
                "player": {"value": "http://www.wikidata.org/entity/Q1"},
                "pfr": {"value": "BetaBb00"},
                "dob": {"value": "1961-02-02T00:00:00Z"},
                "positionLabel": {"value": "cornerback"},
            },
        ]

        def load(binding_order):
            def response(_url, destination, *, refresh):
                if destination.name == "wikipedia_pfhof_list.json":
                    return {"parse": {"text": {"*": html}}}
                if destination.name == "wikipedia_pfhof_category.json":
                    return {
                        "query": {
                            "pages": {
                                "1": {
                                    "title": "Test Legend",
                                    "pageprops": {"wikibase_item": "Q1"},
                                }
                            }
                        }
                    }
                return {"results": {"bindings": binding_order}}

            with tempfile.TemporaryDirectory() as directory, patch(
                "draftscope.data_sources._json_request", side_effect=response
            ):
                return _load_wikimedia_hof_profiles(
                    Path(directory), refresh=False
                )

        forward_rows, forward_metadata = load(bindings)
        reverse_rows, reverse_metadata = load(list(reversed(bindings)))
        forward = forward_rows[0]
        reverse = reverse_rows[0]

        self.assertNotIn("pfr_id", forward)
        self.assertNotIn("date_of_birth", forward)
        self.assertEqual(forward["comparison_positions"], "CB;S")
        self.assertEqual(forward["position"], "CB")
        self.assertEqual(
            forward["wikidata_identity_provenance"],
            reverse["wikidata_identity_provenance"],
        )
        self.assertEqual(
            forward["wikidata_position_provenance"],
            reverse["wikidata_position_provenance"],
        )
        self.assertEqual(
            forward_metadata["identity_conflicts_withheld"],
            reverse_metadata["identity_conflicts_withheld"],
        )
        self.assertEqual(
            {row["field"] for row in forward_metadata["identity_conflicts_withheld"]},
            {"pfr_id", "date_of_birth"},
        )

    def test_enrichment_preserves_source_multi_position_memberships(self) -> None:
        profiles = [
            {
                "name": "Eddie Anderson",
                "wikipedia_page": "Eddie Anderson",
                "wikidata_id": "Q1",
                "comparison_positions": ["K", "P", "WR", "EDGE"],
                "position_raw": "End, kicker, punter",
                "nfl_all_pro_selections": 1,
                "nfl_pro_bowls": 0,
            },
            {
                "name": "Dicky Moegle",
                "wikipedia_page": "Dicky Moegle",
                "wikidata_id": "Q2",
                "comparison_positions": ["S"],
                "position_raw": "RS",
                "nfl_all_pro_selections": 1,
                "nfl_pro_bowls": 0,
            },
            {
                "name": "Frank Gifford",
                "wikipedia_page": "Frank Gifford",
                "wikidata_id": "Q3",
                "comparison_positions": ["WR"],
                "position_raw": "DB, FL",
                "nfl_all_pro_selections": 1,
                "nfl_pro_bowls": 0,
            },
        ]
        bindings = [
            {
                "player": {"value": "http://www.wikidata.org/entity/Q1"},
                "pfr": {"value": "AndeEd00"},
                "positionLabel": {"value": "quarterback"},
            },
            {
                "player": {"value": "http://www.wikidata.org/entity/Q2"},
                "pfr": {"value": "MoegDi00"},
                "positionLabel": {"value": "running back"},
            },
            {
                "player": {"value": "http://www.wikidata.org/entity/Q3"},
                "pfr": {"value": "GiffFr00"},
                "positionLabel": {"value": "safety"},
            },
        ]
        with tempfile.TemporaryDirectory() as directory, patch(
            "draftscope.data_sources._json_request",
            return_value={"results": {"bindings": bindings}},
        ):
            _enrich_wikimedia_profiles(
                profiles,
                Path(directory),
                refresh=False,
                cache_prefix="fixture",
            )

        rows, _metadata = _build_nfl_career_elite_rows(
            [],
            [],
            [],
            [],
            profiles,
            all_era_hof_complete=True,
            historical_honors_metadata={
                "first_team_all_pro": {"published": True},
                "pro_bowl": {"published": True},
            },
        )
        by_name = {row["name"]: row for row in rows}
        self.assertEqual(
            by_name["Eddie Anderson"]["comparison_positions"],
            "K;P;WR;EDGE",
        )
        self.assertEqual(by_name["Dicky Moegle"]["comparison_positions"], "S")
        self.assertEqual(
            by_name["Frank Gifford"]["comparison_positions"], "WR;S"
        )

    def test_reviewed_historical_position_override_is_identity_specific(self) -> None:
        profiles = [
            {
                "name": "Don Paul",
                "wikidata_id": "Q5293290",
                "comparison_positions": [],
                "position_raw": "DB",
            },
            {
                "name": "Don Paul",
                "wikidata_id": "Q5293291",
                "comparison_positions": ["LB"],
                "position_raw": "Linebacker",
            },
        ]
        with tempfile.TemporaryDirectory() as directory, patch(
            "draftscope.data_sources._json_request",
            return_value={"results": {"bindings": []}},
        ):
            _enrich_wikimedia_profiles(
                profiles,
                Path(directory),
                refresh=False,
                cache_prefix="fixture",
            )

        self.assertEqual(profiles[0]["position"], "CB")
        self.assertEqual(profiles[0]["comparison_positions"], "CB")
        self.assertIn("nfl.com", profiles[0]["position_source"])
        self.assertEqual(profiles[1]["position"], "LB")
        self.assertEqual(profiles[1]["comparison_positions"], "LB")

    def test_hof_mixed_roles_disclose_each_unresolved_family(self) -> None:
        html = """
        <table class="wikitable sortable">
        <tr><th>Inductee</th><th>Class</th><th>Position</th><th>Team(s)</th><th>Years</th></tr>
        <tr><td><span class="fn"><a href="/wiki/Mixed_Back">Mixed Back</a></span></td>
        <td>2025</td><td>Halfback; Flanker; Defensive back</td><td>Team</td><td>1950–1960</td></tr>
        <tr><td><span class="fn"><a href="/wiki/Mixed_Lineman">Mixed Lineman</a></span></td>
        <td>2025</td><td>Offensive tackle; Defensive lineman</td><td>Team</td><td>1950–1960</td></tr>
        </table>
        """
        category = {
            "query": {
                "pages": {
                    "1": {
                        "title": "Mixed Back",
                        "pageprops": {"wikibase_item": "Q1"},
                    },
                    "2": {
                        "title": "Mixed Lineman",
                        "pageprops": {"wikibase_item": "Q2"},
                    },
                }
            }
        }
        bindings = [
            {
                "player": {"value": "http://www.wikidata.org/entity/Q1"},
                "positionLabel": {"value": "safety"},
            },
            {
                "player": {"value": "http://www.wikidata.org/entity/Q2"},
                "positionLabel": {"value": "defensive tackle"},
            },
        ]

        def response(_url, destination, *, refresh):
            if destination.name == "wikipedia_pfhof_list.json":
                return {"parse": {"text": {"*": html}}}
            if destination.name == "wikipedia_pfhof_category.json":
                return category
            return {"results": {"bindings": bindings}}

        with tempfile.TemporaryDirectory() as directory, patch(
            "draftscope.data_sources._json_request", side_effect=response
        ):
            rows, metadata = _load_wikimedia_hof_profiles(
                Path(directory), refresh=False
            )

        by_name = {row["name"]: row for row in rows}
        self.assertEqual(by_name["Mixed Back"]["comparison_positions"], "RB;WR;S")
        self.assertEqual(by_name["Mixed Lineman"]["comparison_positions"], "OT;IDL")
        self.assertFalse(metadata["unresolved_position_family_rows"])


if __name__ == "__main__":
    unittest.main()

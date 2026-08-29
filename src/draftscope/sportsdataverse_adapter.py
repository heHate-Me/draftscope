"""SportsDataverse-to-DraftScope historical data compatibility layer."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Mapping

from .data_sources import NFLVERSE_DRAFT_URL, _download
from .records import DataError, normalize_name, parse_number
from .sportsdataverse import SportsDataverseClient, aggregate_player_box_stats


NFLVERSE_PLAYERS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/players/players.csv"
)
_MAX_NFLVERSE_CACHE_BYTES = 256 * 1024 * 1024
_MAX_NFLVERSE_CACHE_ROWS = 2_000_000


class SportsDataverseHistoricalClient:
    """Expose public bulk files through the historical builder's row contract."""

    source_name = "SportsDataverse ESPN college football releases"
    source_namespace = "sportsdataverse"
    supports_recruiting = False
    supports_player_success = False
    measurement_source = "sportsdataverse_espn_school_roster"
    age_source = "sportsdataverse_roster_date_of_birth"
    source_description = (
        "SportsDataverse ESPN roster, team, schedule, and checkpoint-bounded "
        "player-box release files"
    )
    identity_crosswalk_description = (
        "SportsDataverse roster ESPN athlete_id matched by exact normalized "
        "player name and college; nflverse players.csv espn_id is accepted only "
        "when that identity also agrees with the college roster"
    )

    def __init__(
        self,
        college_client: SportsDataverseClient,
        *,
        nflverse_cache_dir: str | Path,
        refresh: bool = False,
    ) -> None:
        self.college_client = college_client
        self.nflverse_cache_dir = Path(nflverse_cache_dir).expanduser().resolve()
        self.refresh = bool(refresh)
        self._teams: dict[int, list[dict[str, str]]] = {}
        self._draft_rows: list[dict[str, str]] | None = None
        self._players: list[dict[str, str]] | None = None

    def current_week(self, *, year: int) -> int:
        return self.college_client.current_week(year=year, refresh=self.refresh)

    def get(self, path: str, **params: Any) -> list[dict[str, Any]]:
        year = _integer(params.get("year"))
        if year is None:
            raise DataError(f"SportsDataverse compatibility request lacks year: {path}")
        if path == "/roster":
            return self._roster(year)
        if path == "/stats/player/season":
            return self._player_stats(year, end_week=_integer(params.get("endWeek")))
        if path == "/teams/fbs":
            return self._fbs_teams(year)
        if path == "/draft/picks":
            return self._draft_picks(year)
        raise DataError(f"SportsDataverse does not implement compatibility endpoint {path}")

    def _load_teams(self, year: int) -> list[dict[str, str]]:
        if year not in self._teams:
            self._teams[year] = self.college_client.load_teams(
                year,
                refresh=self.refresh,
                allow_unavailable=False,
            )
        return self._teams[year]

    def _team_indexes(
        self, year: int
    ) -> tuple[dict[str, Mapping[str, Any]], set[str]]:
        rows = self._load_teams(year)
        by_id: dict[str, Mapping[str, Any]] = {}
        fbs_ids: set[str] = set()
        for row in rows:
            team_id = str(row.get("team_id") or row.get("id") or "").strip()
            if not team_id:
                continue
            by_id[team_id] = row
            if (
                not _truthy(row.get("is_all_star"))
                and not _truthy(row.get("is_exhibition"))
                and (
                    _truthy(row.get("is_fbs"))
                    or str(row.get("classification") or "").lower() == "fbs"
                )
            ):
                fbs_ids.add(team_id)
        return by_id, fbs_ids

    def _roster(self, year: int) -> list[dict[str, Any]]:
        raw = self.college_client.load_roster(
            year,
            refresh=self.refresh,
            allow_unavailable=False,
        )
        teams, fbs_ids = self._team_indexes(year)
        output: list[dict[str, Any]] = []
        for row in raw:
            team_id = str(row.get("team_id") or "").strip()
            division = str(row.get("division") or "").strip().lower()
            if fbs_ids and team_id not in fbs_ids:
                continue
            if not fbs_ids and division != "fbs":
                continue
            athlete_id = str(row.get("athlete_id") or "").strip()
            if not athlete_id:
                continue
            team = teams.get(team_id, {})
            first_name = str(row.get("first_name") or "").strip()
            last_name = str(row.get("last_name") or "").strip()
            name = str(
                row.get("full_name")
                or row.get("athlete_display_name")
                or f"{first_name} {last_name}"
            ).strip()
            output.append(
                {
                    "id": athlete_id,
                    "name": name,
                    "firstName": first_name,
                    "lastName": last_name,
                    "team": row.get("team_location")
                    or team.get("school")
                    or team.get("location"),
                    "conference": team.get("cfbd_conference")
                    or team.get("conference_short_name")
                    or team.get("conference_name"),
                    "position": row.get("position_abbreviation")
                    or row.get("position_name")
                    or row.get("position"),
                    "height": row.get("height"),
                    "weight": row.get("weight"),
                    "year": _class_year(row),
                    "jersey": row.get("jersey"),
                    "gamesRostered": row.get("games_rostered"),
                    "active": row.get("active"),
                    "dateOfBirth": row.get("date_of_birth"),
                    "homeCity": row.get("birth_place_city"),
                    "homeState": row.get("birth_place_state"),
                    "homeCountry": row.get("birth_place_country"),
                }
            )
        output = _collapse_source_roster_aliases(output)
        if not output:
            raise DataError(f"SportsDataverse FBS roster is empty for {year}")
        return output

    def _fbs_teams(self, year: int) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for row in self._load_teams(year):
            if _truthy(row.get("is_all_star")) or _truthy(
                row.get("is_exhibition")
            ):
                continue
            if not (
                _truthy(row.get("is_fbs"))
                or str(row.get("classification") or "").lower() == "fbs"
            ):
                continue
            school = str(
                row.get("school") or row.get("location") or row.get("display_name") or ""
            ).strip()
            if not school:
                continue
            alternate_names = []
            for key in (
                "alt_name1",
                "alt_name2",
                "alt_name3",
                "location",
                "display_name",
                "short_display_name",
                "abbreviation",
            ):
                value = str(row.get(key) or "").strip()
                if value and normalize_name(value) != normalize_name(school):
                    alternate_names.append(value)
            output.append(
                {
                    "id": row.get("team_id"),
                    "school": school,
                    "alternateNames": list(dict.fromkeys(alternate_names)),
                    "conference": row.get("cfbd_conference")
                    or row.get("conference_short_name")
                    or row.get("conference_name"),
                }
            )
        if not output:
            raise DataError(f"SportsDataverse FBS teams are empty for {year}")
        return output

    def _player_stats(
        self,
        year: int,
        *,
        end_week: int | None,
    ) -> list[dict[str, Any]]:
        box_rows = self.college_client.load_player_box(
            year,
            refresh=self.refresh,
            allow_unavailable=False,
        )
        schedule = self.college_client.load_schedule(
            year,
            refresh=self.refresh,
            allow_unavailable=False,
        )
        teams, _fbs_ids = self._team_indexes(year)
        totals = aggregate_player_box_stats(
            box_rows,
            schedule,
            as_of_week=end_week,
        )
        output: list[dict[str, Any]] = []
        for athlete_id, profile in totals.items():
            team = teams.get(str(profile.get("last_team_id") or ""), {})
            school = team.get("school") or team.get("location")
            conference = team.get("cfbd_conference") or team.get(
                "conference_short_name"
            ) or team.get("conference_name")
            for (category, stat_type), value in (
                profile.get("stat_values") or {}
            ).items():
                output.append(
                    {
                        "playerId": athlete_id,
                        "player": profile.get("athlete_name"),
                        "team": school,
                        "conference": conference,
                        "season": year,
                        "category": category,
                        "statType": stat_type,
                        "stat": value,
                    }
                )
        return output

    def _draft_picks(self, year: int) -> list[dict[str, Any]]:
        draft_rows, players = self._nflverse_identity_files()
        players_by_gsis = {
            str(row.get("gsis_id") or "").strip(): row
            for row in players
            if str(row.get("gsis_id") or "").strip()
        }
        players_by_pfr = {
            str(row.get("pfr_id") or "").strip(): row
            for row in players
            if str(row.get("pfr_id") or "").strip()
        }
        roster_rows = self._roster(year - 1)
        team_aliases: dict[str, str] = {}
        for team in self._fbs_teams(year - 1):
            canonical = normalize_name(team.get("school"))
            if not canonical:
                continue
            for value in (team.get("school"), *(team.get("alternateNames") or [])):
                key = normalize_name(value)
                if key:
                    team_aliases.setdefault(key, canonical)
        roster_by_id = {
            str(row.get("id") or "").strip(): row
            for row in roster_rows
            if str(row.get("id") or "").strip()
        }
        roster_by_name_team: dict[tuple[str, str], list[str]] = {}
        roster_by_name: dict[str, list[str]] = {}
        for row in roster_rows:
            athlete_id = str(row.get("id") or "").strip()
            key = (
                normalize_name(row.get("name")),
                team_aliases.get(
                    normalize_name(row.get("team")), normalize_name(row.get("team"))
                ),
            )
            if athlete_id and all(key):
                roster_by_name_team.setdefault(key, []).append(athlete_id)
                roster_by_name.setdefault(key[0], []).append(athlete_id)
        output: list[dict[str, Any]] = []
        for pick in draft_rows:
            if _integer(pick.get("season")) != year:
                continue
            player = players_by_gsis.get(str(pick.get("gsis_id") or "").strip())
            if player is None:
                player = players_by_pfr.get(
                    str(pick.get("pfr_player_id") or "").strip()
                )
            college_key = team_aliases.get(
                normalize_name(pick.get("college")),
                normalize_name(pick.get("college")),
            )
            player_names = _nflverse_player_name_aliases(pick, player)
            exact_ids = sorted(
                {
                    athlete_id
                    for name in player_names
                    for athlete_id in roster_by_name_team.get((name, college_key), ())
                }
            )
            player_espn_id = str((player or {}).get("espn_id") or "").strip()
            verified_id = ""
            direct_roster = roster_by_id.get(player_espn_id)
            if direct_roster is not None:
                direct_team = team_aliases.get(
                    normalize_name(direct_roster.get("team")),
                    normalize_name(direct_roster.get("team")),
                )
                accepted_teams = {college_key}
                for college in str((player or {}).get("college_name") or "").split(";"):
                    key = team_aliases.get(normalize_name(college), normalize_name(college))
                    if key:
                        accepted_teams.add(key)
                direct_surname = _surname(direct_roster.get("name"))
                expected_surname = normalize_name((player or {}).get("last_name")) or _surname(
                    pick.get("pfr_player_name")
                )
                if direct_team in accepted_teams and _surname_matches(
                    direct_surname, expected_surname
                ):
                    verified_id = player_espn_id
            if not verified_id and len(exact_ids) == 1:
                verified_id = exact_ids[0]
            if not verified_id:
                name_only_ids = sorted(
                    {
                        athlete_id
                        for name in player_names
                        for athlete_id in roster_by_name.get(name, ())
                    }
                )
                if len(name_only_ids) == 1:
                    verified_id = name_only_ids[0]
            output.append(
                {
                    "year": year,
                    "overall": pick.get("pick"),
                    "round": pick.get("round"),
                    "name": pick.get("pfr_player_name"),
                    "collegeTeam": pick.get("college"),
                    "nflTeam": pick.get("team"),
                    "collegeAthleteId": verified_id,
                }
            )
        if not output:
            raise DataError(f"nflverse draft class is empty for {year}")
        return output

    def _nflverse_identity_files(
        self,
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        if self._draft_rows is None:
            draft_path = _download(
                NFLVERSE_DRAFT_URL,
                self.nflverse_cache_dir / "draft_picks.csv",
                refresh=self.refresh,
            )
            self._draft_rows = _read_csv(draft_path)
        if self._players is None:
            players_path = _download(
                NFLVERSE_PLAYERS_URL,
                self.nflverse_cache_dir / "players.csv",
                refresh=self.refresh,
            )
            self._players = _read_csv(players_path)
        return self._draft_rows, self._players


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        size = path.stat().st_size
        if size > _MAX_NFLVERSE_CACHE_BYTES:
            raise DataError(
                f"Bulk data file {path} is {size} bytes; "
                f"limit is {_MAX_NFLVERSE_CACHE_BYTES}"
            )
        with path.open(newline="", encoding="utf-8-sig") as handle:
            rows: list[dict[str, str]] = []
            for row in csv.DictReader(handle):
                if len(rows) >= _MAX_NFLVERSE_CACHE_ROWS:
                    raise DataError(
                        f"Bulk data file {path} exceeded "
                        f"{_MAX_NFLVERSE_CACHE_ROWS} data rows"
                    )
                rows.append(dict(row))
    except (OSError, csv.Error) as exc:
        raise DataError(f"Could not read bulk data file {path}: {exc}") from exc
    if not rows:
        raise DataError(f"Bulk data file is empty: {path}")
    return rows


def _collapse_source_roster_aliases(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse ESPN duplicate IDs only when several identity facts agree."""

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            normalize_name(row.get("name")),
            normalize_name(row.get("team")),
        )
        groups.setdefault(key, []).append(row)
    output: list[dict[str, Any]] = []
    for candidates in groups.values():
        if len(candidates) == 1:
            output.extend(candidates)
            continue
        ordered = sorted(candidates, key=_roster_identity_priority, reverse=True)
        chosen = ordered[0]
        output.append(chosen)
        for candidate in ordered[1:]:
            if not _same_source_roster_identity(chosen, candidate):
                output.append(candidate)
    return output


def _roster_identity_priority(row: Mapping[str, Any]) -> tuple[float, int, int, str]:
    games = parse_number(row.get("gamesRostered")) or 0.0
    completeness = sum(
        row.get(key) not in (None, "")
        for key in ("height", "weight", "year", "jersey", "dateOfBirth")
    )
    active = int(_truthy(row.get("active")))
    return games, completeness, active, str(row.get("id") or "")


def _same_source_roster_identity(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> bool:
    details = 0
    contradictions = 0
    for field, tolerance in (("height", 1.0), ("weight", 15.0), ("year", 0.0)):
        left_value = parse_number(left.get(field))
        right_value = parse_number(right.get(field))
        if left_value is None or right_value is None:
            continue
        if abs(left_value - right_value) <= tolerance:
            details += 1
        else:
            contradictions += 1
    left_jersey = str(left.get("jersey") or "").strip()
    right_jersey = str(right.get("jersey") or "").strip()
    if left_jersey and right_jersey:
        if left_jersey == right_jersey:
            details += 1
        else:
            contradictions += 1
    right_completeness = sum(
        right.get(key) not in (None, "")
        for key in ("height", "weight", "year", "jersey", "dateOfBirth")
    )
    left_games = parse_number(left.get("gamesRostered")) or 0.0
    right_games = parse_number(right.get("gamesRostered")) or 0.0
    stub = right_completeness <= 1 and left_games > right_games
    return contradictions == 0 and (details >= 2 or stub)


def _nflverse_player_name_aliases(
    pick: Mapping[str, Any], player: Mapping[str, Any] | None
) -> set[str]:
    values = [pick.get("pfr_player_name")]
    if player:
        values.extend(
            (
                player.get("display_name"),
                _joined_name(player.get("first_name"), player.get("last_name")),
                _joined_name(player.get("football_name"), player.get("last_name")),
                _joined_name(player.get("common_first_name"), player.get("last_name")),
            )
        )
    return {normalize_name(value) for value in values if normalize_name(value)}


def _joined_name(first: Any, last: Any) -> str:
    return f"{str(first or '').strip()} {str(last or '').strip()}".strip()


def _surname(value: Any) -> str:
    tokens = str(value or "").strip().split()
    return normalize_name(tokens[-1]) if tokens else ""


def _surname_matches(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return left == right or (
        min(len(left), len(right)) >= 5
        and (left.startswith(right) or right.startswith(left))
    )


def _class_year(row: Mapping[str, Any]) -> int | None:
    years = _integer(row.get("experience_years"))
    if years is not None and 1 <= years <= 6:
        return years
    abbreviation = str(row.get("experience_abbreviation") or "").strip().upper()
    return {"FR": 1, "SO": 2, "JR": 3, "SR": 4, "GR": 5}.get(abbreviation)


def _integer(value: Any) -> int | None:
    try:
        number = parse_number(value)
    except DataError:
        return None
    if number is None or not float(number).is_integer():
        return None
    return int(number)


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"true", "1", "yes", "y"}


__all__ = ["NFLVERSE_PLAYERS_URL", "SportsDataverseHistoricalClient"]

"""Credential-free college football ingestion from SportsDataverse releases.

The release files are ordinary CSV assets hosted by GitHub.  This module keeps
the network and schema translation deliberately small: it downloads/cache raw
season files, aggregates ESPN player box-score rows at a completed-week
checkpoint, and emits records accepted by DraftScope's existing normalizer.
"""

from __future__ import annotations

import csv
import gzip
import io
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .college_features import derive_college_production, parse_college_stat_number
from .mathstats import percentile_rank
from .records import DataError, normalize_name, normalize_record, parse_number
from .schema import POSITION_GROUPS, PRODUCTION, normalize_position


RELEASE_BASE = (
    "https://github.com/sportsdataverse/sportsdataverse-data/releases/download"
)
_READ_CHUNK_BYTES = 64 * 1024
_DEFAULT_MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
_DEFAULT_MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024
_DEFAULT_MAX_ROWS = 1_000_000


def sportsdataverse_urls(dataset: str, season: int) -> tuple[str, ...]:
    """Return the ordered CSV asset URLs for one public release partition."""

    year = int(season)
    paths = {
        "roster": (
            f"espn_cfb_rosters/cfb_rosters_{year}.csv.gz",
            f"espn_cfb_rosters/cfb_rosters_{year}.csv",
        ),
        "player_box": (
            f"espn_cfb_player_box/player_box_{year}.csv.gz",
            f"espn_cfb_player_box/player_box_{year}.csv",
        ),
        # New schedule assets are currently plain CSV; older partitions may be
        # gzip-compressed, so both names remain supported.
        "schedule": (
            f"espn_cfb_schedules/cfb_schedule_{year}.csv",
            f"espn_cfb_schedules/cfb_schedule_{year}.csv.gz",
        ),
        "teams": (
            f"espn_cfb_teams/cfb_teams_{year}.csv",
            f"espn_cfb_teams/cfb_teams_{year}.csv.gz",
        ),
    }
    try:
        return tuple(f"{RELEASE_BASE}/{path}" for path in paths[dataset])
    except KeyError as exc:
        raise DataError(f"Unknown SportsDataverse dataset {dataset!r}") from exc


class SportsDataverseClient:
    """Download and cache public SportsDataverse college-football CSV files."""

    source_name = "SportsDataverse ESPN college football releases"
    source_namespace = "sportsdataverse"
    supports_recruiting = False
    supports_player_success = False

    def __init__(
        self,
        cache_dir: str | os.PathLike[str],
        *,
        nflverse_cache_dir: str | os.PathLike[str] | None = None,
        refresh: bool = False,
        timeout: float = 45.0,
        opener: Callable[..., Any] | None = None,
        max_download_bytes: int = _DEFAULT_MAX_DOWNLOAD_BYTES,
        max_decompressed_bytes: int = _DEFAULT_MAX_DECOMPRESSED_BYTES,
        max_rows: int = _DEFAULT_MAX_ROWS,
    ) -> None:
        self.cache_dir = Path(cache_dir).expanduser().resolve() / "sportsdataverse"
        self.nflverse_cache_dir = (
            Path(nflverse_cache_dir).expanduser().resolve()
            if nflverse_cache_dir is not None
            else None
        )
        self.default_refresh = bool(refresh)
        self.timeout = float(timeout)
        self._opener = opener or urlopen
        self.max_download_bytes = max(1, int(max_download_bytes))
        self.max_decompressed_bytes = max(1, int(max_decompressed_bytes))
        self.max_rows = max(1, int(max_rows))
        self.last_status: dict[str, str] = {}

    def load_roster(
        self,
        season: int,
        *,
        refresh: bool | None = None,
        allow_unavailable: bool = False,
    ) -> list[dict[str, str]]:
        return self._load(
            "roster",
            season,
            refresh=self.default_refresh if refresh is None else refresh,
            allow_unavailable=allow_unavailable,
        )

    def load_player_box(
        self,
        season: int,
        *,
        refresh: bool | None = None,
        allow_unavailable: bool = False,
    ) -> list[dict[str, str]]:
        return self._load(
            "player_box",
            season,
            refresh=self.default_refresh if refresh is None else refresh,
            allow_unavailable=allow_unavailable,
        )

    def load_schedule(
        self,
        season: int,
        *,
        refresh: bool | None = None,
        allow_unavailable: bool = False,
    ) -> list[dict[str, str]]:
        return self._load(
            "schedule",
            season,
            refresh=self.default_refresh if refresh is None else refresh,
            allow_unavailable=allow_unavailable,
        )

    def load_teams(
        self,
        season: int,
        *,
        refresh: bool | None = None,
        allow_unavailable: bool = False,
    ) -> list[dict[str, str]]:
        return self._load(
            "teams",
            season,
            refresh=self.default_refresh if refresh is None else refresh,
            allow_unavailable=allow_unavailable,
        )

    def current_week(self, year: int, refresh: bool | None = None) -> int:
        schedule = self.load_schedule(
            year, refresh=refresh, allow_unavailable=True
        )
        return completed_week(schedule)

    def discover_candidates(
        self,
        *,
        season: int,
        as_of_week: int | None = None,
        per_position: int | None = None,
        preseason_prior_year: bool = True,
        refresh: bool | None = None,
        raw_dir: str | os.PathLike[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Convenience wrapper used by the weekly updater.

        ``raw_dir`` is accepted for provider compatibility.  The original raw
        CSV bytes are already retained beneath ``cache_dir/sportsdataverse``.
        """

        del raw_dir
        return discover_sportsdataverse_candidates(
            self,
            season=season,
            as_of_week=as_of_week,
            per_position=per_position,
            preseason_prior_year=preseason_prior_year,
            refresh=self.default_refresh if refresh is None else bool(refresh),
        )

    def _cache_path(self, dataset: str, season: int) -> Path:
        names = {
            "roster": f"cfb_rosters_{season}.csv.gz",
            "player_box": f"player_box_{season}.csv.gz",
            "schedule": f"cfb_schedule_{season}.csv",
            "teams": f"cfb_teams_{season}.csv",
        }
        return self.cache_dir / dataset / names[dataset]

    def _load(
        self,
        dataset: str,
        season: int,
        *,
        refresh: bool,
        allow_unavailable: bool,
    ) -> list[dict[str, str]]:
        year = int(season)
        cache_path = self._cache_path(dataset, year)
        status_key = f"{dataset}:{year}"
        if cache_path.exists() and not refresh:
            rows = self._parse_cache(cache_path)
            self.last_status[status_key] = "cache"
            return rows

        errors: list[str] = []
        for url in sportsdataverse_urls(dataset, year):
            try:
                request = Request(
                    url,
                    headers={"User-Agent": "DraftScope/1.0 (+public bulk data)"},
                )
                response = self._opener(request, timeout=self.timeout)
                try:
                    payload = _read_limited(
                        response,
                        max_bytes=self.max_download_bytes,
                        source=url,
                    )
                finally:
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()
                rows = _parse_csv_bytes(
                    payload,
                    source=url,
                    max_compressed_bytes=self.max_download_bytes,
                    max_decompressed_bytes=self.max_decompressed_bytes,
                    max_rows=self.max_rows,
                )
                _atomic_write_bytes(cache_path, payload)
                self.last_status[status_key] = "remote"
                return rows
            except HTTPError as exc:
                errors.append(f"{url}: HTTP {exc.code}")
                close = getattr(exc, "close", None)
                if callable(close):
                    close()
                if exc.code not in {404, 410}:
                    break
            except (URLError, OSError, EOFError, UnicodeError, csv.Error) as exc:
                errors.append(f"{url}: {exc}")
                break
            except DataError as exc:
                errors.append(str(exc))
                break

        # A transient network/source failure never invalidates a previously
        # downloaded partition.
        if cache_path.exists():
            rows = self._parse_cache(cache_path)
            self.last_status[status_key] = "stale_cache"
            return rows
        if allow_unavailable:
            self.last_status[status_key] = "unavailable"
            return []
        detail = "; ".join(errors) or "no release asset was returned"
        raise DataError(
            f"SportsDataverse {dataset} data is unavailable for {year}: {detail}"
        )

    def _parse_cache(self, cache_path: Path) -> list[dict[str, str]]:
        with cache_path.open("rb") as handle:
            payload = _read_limited(
                handle,
                max_bytes=self.max_download_bytes,
                source=str(cache_path),
            )
        return _parse_csv_bytes(
            payload,
            source=str(cache_path),
            max_compressed_bytes=self.max_download_bytes,
            max_decompressed_bytes=self.max_decompressed_bytes,
            max_rows=self.max_rows,
        )


def _content_length(response: Any) -> int | None:
    headers = getattr(response, "headers", None)
    raw = headers.get("Content-Length") if headers is not None else None
    try:
        value = int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return value if value is not None and value >= 0 else None


def _read_limited(response: Any, *, max_bytes: int, source: str) -> bytes:
    limit = max(1, int(max_bytes))
    announced = _content_length(response)
    if announced is not None and announced > limit:
        raise DataError(
            f"SportsDataverse response {source} announced {announced} bytes; "
            f"limit is {limit}"
        )
    payload = bytearray()
    while True:
        chunk = response.read(min(_READ_CHUNK_BYTES, limit - len(payload) + 1))
        if not chunk:
            break
        payload.extend(chunk)
        if len(payload) > limit:
            raise DataError(
                f"SportsDataverse response {source} exceeded {limit} bytes"
            )
    return bytes(payload)


def _parse_csv_bytes(
    payload: bytes,
    *,
    source: str,
    max_compressed_bytes: int = _DEFAULT_MAX_DOWNLOAD_BYTES,
    max_decompressed_bytes: int = _DEFAULT_MAX_DECOMPRESSED_BYTES,
    max_rows: int = _DEFAULT_MAX_ROWS,
) -> list[dict[str, str]]:
    if len(payload) > max_compressed_bytes:
        raise DataError(
            f"SportsDataverse payload {source} is {len(payload)} bytes; "
            f"compressed/input limit is {max_compressed_bytes}"
        )
    try:
        if payload[:2] == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=io.BytesIO(payload)) as compressed:
                decoded = _read_limited(
                    compressed,
                    max_bytes=max_decompressed_bytes,
                    source=f"decompressed {source}",
                )
        else:
            if len(payload) > max_decompressed_bytes:
                raise DataError(
                    f"SportsDataverse CSV {source} exceeded "
                    f"{max_decompressed_bytes} decompressed bytes"
                )
            decoded = payload
        text = decoded.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if not reader.fieldnames:
            raise DataError(f"SportsDataverse CSV has no header: {source}")
        rows: list[dict[str, str]] = []
        for row in reader:
            if len(rows) >= max_rows:
                raise DataError(
                    f"SportsDataverse CSV {source} exceeded {max_rows} rows"
                )
            rows.append(dict(row))
        return rows
    except (OSError, EOFError, UnicodeError, csv.Error) as exc:
        raise DataError(f"Cannot parse SportsDataverse CSV {source}: {exc}") from exc


def _atomic_write_bytes(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def completed_week(schedule_rows: Iterable[Mapping[str, Any]]) -> int:
    """Return the latest week whose entire listed slate is explicitly final."""

    week_states: dict[int, list[bool]] = {}
    for row in schedule_rows:
        week = _integer(row.get("week"))
        if week is not None and week >= 0:
            week_states.setdefault(week, []).append(_game_is_complete(row))
    return max(
        (week for week, states in week_states.items() if states and all(states)),
        default=0,
    )


def _game_is_complete(row: Mapping[str, Any]) -> bool:
    status = str(
        _first(row, "status", "status_type", "status_name", "status_state") or ""
    ).strip().upper()
    normalized = re.sub(r"[^A-Z]", "", status)
    return normalized in {
        "FINAL",
        "STATUSFINAL",
        "COMPLETE",
        "STATUSCOMPLETE",
        "COMPLETED",
        "STATUSCOMPLETED",
    }


# Raw count fields consumed by derive_college_production.  Efficiency/rate
# columns in the source are intentionally not summed across games; the shared
# transformer recomputes them from the checkpoint-bounded count totals.
_CATEGORY_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "rushing": (
        ("rushingAttempts", "att"),
        ("rushingYards", "yds"),
        ("rushingTouchdowns", "td"),
    ),
    "receiving": (
        ("receptions", "rec"),
        ("receivingYards", "yds"),
        ("receivingTouchdowns", "td"),
    ),
    "defensive": (
        ("totalTackles", "total"),
        ("soloTackles", "solo"),
        ("sacks", "sacks"),
        ("tacklesForLoss", "tfl"),
        ("passesDefended", "pd"),
        ("hurries", "qb_hur"),
    ),
    "interceptions": (("interceptions", "int"),),
    "kickreturns": (
        ("kickReturns", "no"),
        ("kickReturnYards", "yds"),
        ("kickReturnTouchdowns", "td"),
    ),
    "puntreturns": (
        ("puntReturns", "no"),
        ("puntReturnYards", "yds"),
        ("puntReturnTouchdowns", "td"),
    ),
    "punting": (
        ("punts", "no"),
        ("puntYards", "yds"),
        ("touchbacks", "tb"),
        ("puntsInside20", "in_20"),
    ),
}

_MAX_STATS = {("kicking", "long")}


def aggregate_player_box_by_week(
    player_box_rows: Iterable[Mapping[str, Any]],
    schedule_rows: Iterable[Mapping[str, Any]] = (),
    *,
    as_of_week: int | None = None,
) -> list[dict[str, Any]]:
    """Aggregate category rows into one leakage-safe player/week record."""

    if as_of_week is not None and int(as_of_week) < 0:
        raise DataError("as_of_week must be zero or greater")
    schedule = {
        str(_first(row, "game_id", "id")): {
            "week": _integer(row.get("week")),
            "complete": _game_is_complete(row),
        }
        for row in schedule_rows
        if _first(row, "game_id", "id") not in (None, "")
    }
    bounded_week = int(as_of_week) if as_of_week is not None else None
    groups: dict[tuple[str, int], dict[str, Any]] = {}
    for raw in player_box_rows:
        athlete_id = str(_first(raw, "athlete_id", "athleteId", "player_id") or "").strip()
        game_id = str(_first(raw, "game_id", "gameId") or "").strip()
        if not athlete_id or not game_id:
            continue
        game = schedule.get(game_id)
        if schedule and game is None:
            # When a checkpoint schedule is supplied, unknown games cannot be
            # proved to have occurred before it and are excluded.
            continue
        if game is not None and not game["complete"]:
            continue
        week = game["week"] if game is not None else _integer(raw.get("week"))
        if week is None:
            week = 0
        if bounded_week is not None and week > bounded_week:
            continue
        key = (athlete_id, week)
        group = groups.setdefault(
            key,
            {
                "athlete_id": athlete_id,
                "athlete_name": _first(raw, "athlete_name", "athleteName", "player"),
                "season": _integer(raw.get("season")),
                "week": week,
                "_games": set(),
                "_team_ids": set(),
                "last_team_id": None,
                "stat_values": {},
            },
        )
        group["_games"].add(game_id)
        team_id = str(_first(raw, "team_id", "teamId") or "").strip()
        if team_id:
            group["_team_ids"].add(team_id)
            group["last_team_id"] = team_id
        if not group.get("athlete_name"):
            group["athlete_name"] = _first(
                raw, "athlete_name", "athleteName", "player"
            )
        for stat_key, value in _player_box_stat_values(raw).items():
            _accumulate_stat(group["stat_values"], stat_key, value)

    result: list[dict[str, Any]] = []
    for group in groups.values():
        values = group["stat_values"]
        games = len(group.pop("_games"))
        team_ids = sorted(group.pop("_team_ids"))
        group["games"] = games
        group["team_ids"] = team_ids
        group["production"] = derive_college_production(values, games=float(games))
        result.append(group)
    result.sort(key=lambda row: (int(row["week"]), str(row["athlete_id"])))
    return result


def aggregate_player_box_stats(
    player_box_rows: Iterable[Mapping[str, Any]],
    schedule_rows: Iterable[Mapping[str, Any]] = (),
    *,
    as_of_week: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Return checkpoint-bounded season totals keyed by ESPN athlete id."""

    weekly = aggregate_player_box_by_week(
        player_box_rows, schedule_rows, as_of_week=as_of_week
    )
    totals: dict[str, dict[str, Any]] = {}
    for row in weekly:
        athlete_id = str(row["athlete_id"])
        target = totals.setdefault(
            athlete_id,
            {
                "athlete_id": athlete_id,
                "athlete_name": row.get("athlete_name"),
                "season": row.get("season"),
                "weeks": [],
                "games": 0,
                "team_ids": set(),
                "last_team_id": None,
                "stat_values": {},
            },
        )
        target["weeks"].append(int(row["week"]))
        target["games"] += int(row["games"])
        target["team_ids"].update(row.get("team_ids") or ())
        if row.get("last_team_id"):
            target["last_team_id"] = row["last_team_id"]
        for stat_key, value in row["stat_values"].items():
            _accumulate_stat(target["stat_values"], stat_key, value)
    for target in totals.values():
        target["weeks"] = sorted(set(target["weeks"]))
        target["team_ids"] = sorted(target["team_ids"])
        target["production"] = derive_college_production(
            target["stat_values"], games=float(target["games"])
        )
    return totals


def _player_box_stat_values(row: Mapping[str, Any]) -> dict[tuple[str, str], float]:
    category = _slug(_first(row, "category", "stat_category"))
    values: dict[tuple[str, str], float] = {}
    if category == "passing":
        completions = _number(_first(row, "completions"))
        attempts = _number(_first(row, "passingAttempts"))
        combined = _first(row, "stat_1", "completions/passingAttempts")
        if combined and "/" in str(combined):
            left, right = str(combined).split("/", 1)
            completions = _number(left)
            attempts = _number(right)
        _put(values, ("passing", "completions"), completions)
        _put(values, ("passing", "att"), attempts)
        _put(values, ("passing", "yds"), _number(_first(row, "passingYards", "stat_2")))
        _put(values, ("passing", "td"), _number(_first(row, "passingTouchdowns", "stat_4")))
        _put(values, ("passing", "int"), _number(_first(row, "stat_5")))
    elif category == "kicking":
        made, attempts = _split_ratio(_first(row, "fieldGoalsMade/fieldGoalAttempts"))
        _put(values, ("kicking", "fgm"), made)
        _put(values, ("kicking", "fga"), attempts)
        made, attempts = _split_ratio(_first(row, "extraPointsMade/extraPointAttempts"))
        _put(values, ("kicking", "xpm"), made)
        _put(values, ("kicking", "xpa"), attempts)
        _put(values, ("kicking", "pts"), _number(row.get("totalKickingPoints")))
        _put(values, ("kicking", "long"), _number(row.get("longFieldGoalMade")))
    else:
        for source, stat_name in _CATEGORY_FIELDS.get(category, ()):
            _put(values, (category, stat_name), _number(row.get(source)))
    return values


def _accumulate_stat(
    values: dict[tuple[str, str], float],
    key: tuple[str, str],
    value: Any,
) -> None:
    number = _number(value)
    if number is None:
        return
    if key in _MAX_STATS:
        values[key] = max(values.get(key, number), number)
    else:
        values[key] = values.get(key, 0.0) + number


def discover_sportsdataverse_candidates(
    client: SportsDataverseClient,
    *,
    season: int,
    as_of_week: int | None = None,
    per_position: int | None = None,
    preseason_prior_year: bool = True,
    refresh: bool = True,
) -> list[dict[str, Any]]:
    """Build and rank the current FBS roster from public bulk release data.

    A missing current-season roster or player-box partition returns an empty or
    roster-only result instead of requiring a credentialed fallback.  Before
    Week 1, prior-season production is joined by stable ESPN athlete id (then a
    unique normalized-name fallback) while current roster identity is retained.
    """

    if per_position is not None and int(per_position) < 0:
        raise DataError("per_position must be zero or greater")
    schedule = client.load_schedule(
        season, refresh=refresh, allow_unavailable=True
    )
    week = completed_week(schedule) if as_of_week is None else int(as_of_week)
    if week < 0:
        raise DataError("as_of_week must be zero or greater")
    roster = client.load_roster(season, refresh=refresh, allow_unavailable=True)
    if not roster:
        return []
    teams = client.load_teams(season, refresh=refresh, allow_unavailable=True)
    team_index = {
        str(_first(row, "team_id", "id")): row
        for row in teams
        if _first(row, "team_id", "id") not in (None, "")
    }

    stats_year = season
    stats_schedule = schedule
    stats_week: int | None = week
    preseason = week == 0
    if preseason:
        if not preseason_prior_year:
            stats_rows: list[dict[str, str]] = []
        else:
            stats_year = season - 1
            stats_rows = client.load_player_box(
                stats_year, refresh=refresh, allow_unavailable=True
            )
            stats_schedule = client.load_schedule(
                stats_year, refresh=refresh, allow_unavailable=True
            )
            stats_week = completed_week(stats_schedule) or None
    else:
        stats_rows = client.load_player_box(
            stats_year, refresh=refresh, allow_unavailable=True
        )
    stats = aggregate_player_box_stats(
        stats_rows,
        stats_schedule,
        as_of_week=stats_week,
    ) if stats_rows else {}
    stats_by_name: dict[str, list[dict[str, Any]]] = {}
    for item in stats.values():
        name = normalize_name(item.get("athlete_name"))
        if name:
            stats_by_name.setdefault(name, []).append(item)

    selected_roster = _select_fbs_roster_rows(roster, team_index)
    candidates: list[dict[str, Any]] = []
    for item in selected_roster:
        athlete_id = str(_first(item, "athlete_id", "athleteId", "id") or "").strip()
        name = _first(item, "full_name", "athlete_display_name", "display_name")
        position = normalize_position(
            _first(item, "position_abbreviation", "position", "position_name")
        )
        if not athlete_id or not name or position not in POSITION_GROUPS:
            continue
        team_id = str(_first(item, "team_id", "teamId") or "").strip()
        team = team_index.get(team_id, {})
        production = stats.get(athlete_id)
        match_method = "espn_athlete_id"
        if production is None:
            matches = stats_by_name.get(normalize_name(name), ())
            if len(matches) == 1:
                production = matches[0]
                match_method = "unique_normalized_name"
        record: dict[str, Any] = {
            "player_id": f"espn:{athlete_id}",
            "espn_player_id": athlete_id,
            "cfbd_player_id": None,
            "name": name,
            "position": position,
            "school": _first(item, "team_location") or _first(team, "school", "location"),
            "conference": _first(
                team,
                "cfbd_conference",
                "conference_short_name",
                "conference_name",
            ),
            "season": season,
            "as_of_week": week,
            "projected_draft_year": season + 1,
            "height_in": _first(item, "height"),
            "weight_lb": _first(item, "weight"),
            "date_of_birth": _first(item, "date_of_birth"),
            "class_year": _first(
                item,
                "experience_abbreviation",
                "experience_display_value",
                "experience_years",
            ),
            "jersey": _first(item, "jersey"),
            "measurement_source": "sportsdataverse_espn_roster",
            "measurements_verified": False,
            "source_status": _first(item, "status_name", "status_type"),
            "has_recorded_stats": bool(production),
            "production_source": (
                "sportsdataverse_espn_player_box" if production else None
            ),
            "production_season": stats_year if production else None,
            "production_as_of_week": (
                max(production.get("weeks") or [0]) if production else None
            ),
            "production_school": None,
            "production_conference": None,
            "discovery_match": match_method if production else "current_roster_only",
            "discovery_preseason_prior_year": preseason,
        }
        if production:
            production_team_id = str(production.get("last_team_id") or "")
            production_team = team_index.get(production_team_id, {})
            if stats_year != season and production_team_id:
                # The current teams table usually retains stable team ids and
                # is sufficient for transfer provenance; absence stays explicit.
                record["production_school"] = _first(
                    production_team, "school", "location"
                )
                record["production_conference"] = _first(
                    production_team,
                    "cfbd_conference",
                    "conference_short_name",
                    "conference_name",
                )
            else:
                record["production_school"] = _first(
                    production_team, "school", "location"
                ) or record["school"]
                record["production_conference"] = _first(
                    production_team,
                    "cfbd_conference",
                    "conference_short_name",
                    "conference_name",
                ) or record["conference"]
            record.update(production["production"])
        candidates.append(normalize_record(record))

    ranked = _rank_candidates(candidates)
    if per_position is None or int(per_position) == 0:
        return ranked
    counts: dict[str, int] = {}
    selected: list[dict[str, Any]] = []
    for row in ranked:
        position = str(row.get("position") or "")
        if counts.get(position, 0) >= int(per_position):
            continue
        counts[position] = counts.get(position, 0) + 1
        selected.append(row)
    return selected


def _select_fbs_roster_rows(
    roster: Iterable[Mapping[str, Any]],
    team_index: Mapping[str, Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    by_id: dict[str, Mapping[str, Any]] = {}
    for row in roster:
        athlete_id = str(_first(row, "athlete_id", "athleteId", "id") or "").strip()
        if not athlete_id:
            continue
        team_id = str(_first(row, "team_id", "teamId") or "").strip()
        division = str(_first(row, "division") or "").strip().lower()
        team = team_index.get(team_id, {})
        team_fbs = str(_first(team, "is_fbs") or "").strip().lower() in {
            "true",
            "1",
            "yes",
        } or str(_first(team, "classification") or "").strip().lower() == "fbs"
        team_all_star = any(
            str(team.get(key) or "").strip().lower() in {"true", "1", "yes"}
            for key in ("is_all_star", "is_exhibition")
        )
        if team and (not team_fbs or team_all_star):
            continue
        if not team and division != "fbs":
            continue
        current = by_id.get(athlete_id)
        if current is None or _roster_priority(row) > _roster_priority(current):
            by_id[athlete_id] = row
    return list(by_id.values())


def _roster_priority(row: Mapping[str, Any]) -> tuple[int, float, str]:
    active = str(_first(row, "active", "is_active", "status_type") or "").lower()
    active_score = int(active in {"true", "1", "yes", "active"})
    games = _number(row.get("games_rostered")) or 0.0
    return active_score, games, str(_first(row, "team_location", "team_id") or "")


def _rank_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    peer_values: dict[tuple[str, str], list[float]] = {}
    for candidate in candidates:
        position = normalize_position(candidate.get("position"))
        for key, _direction in PRODUCTION.get(position, ()):
            value = _number(candidate.get(f"prod_{key}"))
            if value is not None:
                peer_values.setdefault((position, key), []).append(value)

    for candidate in candidates:
        position = normalize_position(candidate.get("position"))
        metric_scores: dict[str, float] = {}
        for key, direction in PRODUCTION.get(position, ()):
            value = _number(candidate.get(f"prod_{key}"))
            if value is None:
                continue
            score = percentile_rank(value, peer_values.get((position, key), ()))
            if score is None:
                continue
            if direction == "lower":
                score = 100.0 - score
            metric_scores[key] = round(score, 3)
        candidate["discovery_metric_scores"] = metric_scores
        candidate["discovery_metrics"] = ",".join(metric_scores)
        candidate["discovery_metric_count"] = len(metric_scores)
        if metric_scores:
            candidate["discovery_has_usable_production"] = 1.0
            candidate["discovery_evidence_tier"] = "position_production"
            candidate["discovery_score_kind"] = (
                "position_production_peer_percentile_not_draft_probability"
            )
            candidate["discovery_score_method"] = (
                "unweighted_mean_available_position_metric_empirical_midrank_percentiles"
            )
            candidate["discovery_fallback_reason"] = None
            candidate["discovery_score"] = round(
                sum(metric_scores.values()) / len(metric_scores), 3
            )
        else:
            class_score = _class_readiness(candidate.get("class_year"))
            measurement_completeness = 50.0 * sum(
                _number(candidate.get(key)) is not None
                for key in ("height_in", "weight_lb")
            )
            candidate["discovery_has_usable_production"] = 0.0
            candidate["discovery_evidence_tier"] = "roster_metadata_only"
            candidate["discovery_score_kind"] = (
                "roster_metadata_fallback_not_draft_probability"
            )
            candidate["discovery_score_method"] = (
                "80pct_class_readiness_plus_20pct_height_weight_completeness"
            )
            candidate["discovery_fallback_reason"] = (
                "no_mapped_position_production_metrics"
                if candidate.get("has_recorded_stats")
                else "no_player_box_score_rows"
            )
            candidate["discovery_score"] = round(
                0.80 * class_score + 0.20 * measurement_completeness, 3
            )

    candidates.sort(
        key=lambda row: (
            -int(bool(_number(row.get("discovery_has_usable_production")))),
            -float(row.get("discovery_score") or 0.0),
            -int(row.get("discovery_metric_count") or 0),
            normalize_name(row.get("name")),
        )
    )
    position_ranks: dict[str, int] = {}
    for rank, row in enumerate(candidates, start=1):
        row["discovery_rank"] = rank
        position = normalize_position(row.get("position"))
        position_ranks[position] = position_ranks.get(position, 0) + 1
        row["discovery_position_rank"] = position_ranks[position]
    return candidates


def _class_readiness(value: Any) -> float:
    text = re.sub(r"[^a-z0-9]", "", str(value or "").lower())
    number = _number(value)
    if number is None:
        number = {
            "fr": 1.0,
            "freshman": 1.0,
            "rfr": 1.5,
            "rsfr": 1.5,
            "redshirtfreshman": 1.5,
            "so": 2.0,
            "sophomore": 2.0,
            "rso": 2.5,
            "rsso": 2.5,
            "redshirtsophomore": 2.5,
            "jr": 3.0,
            "junior": 3.0,
            "rjr": 3.5,
            "rsjr": 3.5,
            "redshirtjunior": 3.5,
            "sr": 4.0,
            "senior": 4.0,
            "rsr": 4.5,
            "rssr": 4.5,
            "redshirtsenior": 4.5,
            "gr": 5.0,
            "grad": 5.0,
            "graduate": 5.0,
            "5th": 5.0,
            "fifthyear": 5.0,
            "sixthyear": 5.0,
        }.get(text)
    if number is None:
        return 0.0
    anchors = ((1.0, 0.0), (2.0, 25.0), (3.0, 55.0), (4.0, 82.0), (5.0, 100.0))
    if number <= 1.0:
        return 0.0
    if number >= 5.0:
        return 100.0
    for (low_year, low_score), (high_year, high_score) in zip(anchors, anchors[1:]):
        if low_year <= number <= high_year:
            fraction = (number - low_year) / (high_year - low_year)
            return low_score + fraction * (high_score - low_score)
    return 0.0


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return parse_college_stat_number(value)
    except (DataError, TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _split_ratio(value: Any) -> tuple[float | None, float | None]:
    if value in (None, "") or "/" not in str(value):
        return None, None
    left, right = str(value).split("/", 1)
    return _number(left), _number(right)


def _put(
    values: dict[tuple[str, str], float],
    key: tuple[str, str],
    value: float | None,
) -> None:
    if value is not None:
        values[key] = value


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


__all__ = [
    "SportsDataverseClient",
    "aggregate_player_box_by_week",
    "aggregate_player_box_stats",
    "completed_week",
    "discover_sportsdataverse_candidates",
    "sportsdataverse_urls",
]

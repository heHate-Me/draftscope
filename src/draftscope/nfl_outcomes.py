from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .records import DataError, parse_number, timestamp_utc


NFLVERSE_SNAP_COUNTS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "snap_counts/snap_counts_{season}.csv"
)
NFL_CONTRIBUTOR_HORIZON_SEASONS = 3
NFL_CONTRIBUTOR_PRIMARY_SNAPS = 500
NFL_CONTRIBUTOR_SPECIAL_TEAMS_SNAPS = 150
MIN_COMPLETE_REGULAR_SEASON_GAMES = 240

_NETWORK_CHUNK_BYTES = 64 * 1024
_MAX_SNAP_COUNT_DOWNLOAD_BYTES = 256 * 1024 * 1024
_MAX_SNAP_COUNT_CSV_ROWS = 1_000_000

_REQUIRED_SNAP_FIELDS = {
    "game_id",
    "season",
    "game_type",
    "pfr_player_id",
    "offense_snaps",
    "defense_snaps",
    "st_snaps",
}


def load_nflverse_snap_counts(
    cache_dir: str | os.PathLike[str],
    *,
    seasons: Iterable[int],
    refresh: bool = False,
) -> tuple[dict[int, list[dict[str, str]]], dict[str, Any]]:
    """Load and audit annual nflverse/PFR regular-season snap-count CSVs."""

    requested = tuple(sorted({int(season) for season in seasons}))
    if any(season < 2012 for season in requested):
        raise DataError("nflverse/PFR snap counts are available only from 2012 onward")
    cache = Path(cache_dir)
    rows_by_season: dict[int, list[dict[str, str]]] = {}
    artifacts: list[dict[str, Any]] = []
    complete_seasons: list[int] = []

    for season in requested:
        url = NFLVERSE_SNAP_COUNTS_URL.format(season=season)
        destination = cache / f"snap_counts_{season}.csv"
        path = _download_csv(url, destination, refresh=refresh)
        rows = [
            row
            for row in _read_snap_count_csv(path)
            if _as_int(row.get("season")) == season
            and str(row.get("game_type") or "").upper() == "REG"
        ]
        game_count = len({str(row.get("game_id") or "") for row in rows if row.get("game_id")})
        complete = game_count >= MIN_COMPLETE_REGULAR_SEASON_GAMES
        if complete:
            complete_seasons.append(season)
        rows_by_season[season] = rows
        sidecar = _source_sidecar(path)
        artifacts.append(
            {
                "season": season,
                "url": url,
                "rows": len(rows),
                "regular_season_games": game_count,
                "complete": complete,
                "bytes": path.stat().st_size,
                "sha256": _file_sha256(path),
                "downloaded_at": sidecar.get("downloaded_at"),
            }
        )

    status = "pass" if len(complete_seasons) == len(requested) else "partial"
    return rows_by_season, {
        "source": "nflverse/PFR snap counts",
        "source_url_template": NFLVERSE_SNAP_COUNTS_URL,
        "license": "CC BY 4.0; upstream data terms also apply",
        "requested_seasons": list(requested),
        "complete_seasons": complete_seasons,
        "minimum_complete_regular_season_games": MIN_COMPLETE_REGULAR_SEASON_GAMES,
        "source_artifacts": artifacts,
        "status": status,
    }


def build_three_year_contributor_outcomes(
    identities: Iterable[Mapping[str, Any]],
    snap_rows_by_season: Mapping[int, Iterable[Mapping[str, Any]]],
    *,
    completed_season: int,
    complete_seasons: Iterable[int] | None = None,
    horizon_seasons: int = NFL_CONTRIBUTOR_HORIZON_SEASONS,
    primary_snap_threshold: int = NFL_CONTRIBUTOR_PRIMARY_SNAPS,
    special_teams_snap_threshold: int = NFL_CONTRIBUTOR_SPECIAL_TEAMS_SNAPS,
) -> tuple[dict[tuple[int, str], dict[str, Any]], dict[str, Any]]:
    """Create fixed first-three-season contributor labels without right-censor leakage.

    A contributor reaches either the primary-unit or special-teams threshold in
    regular-season snaps during exactly the draft season and following two NFL
    seasons. A label is known only when all horizon seasons are declared complete
    and a stable PFR player ID is available. No snap rows is then a valid zero.
    """

    if horizon_seasons < 1:
        raise DataError("NFL contributor horizon must be at least one season")
    if primary_snap_threshold < 1 or special_teams_snap_threshold < 1:
        raise DataError("NFL contributor snap thresholds must be positive")

    declared_complete = {
        int(season)
        for season in (
            complete_seasons
            if complete_seasons is not None
            else snap_rows_by_season.keys()
        )
        if int(season) <= int(completed_season)
    }
    player_games: dict[tuple[int, str], dict[str, tuple[int, int, int]]] = {}
    identical_duplicate_rows = 0
    for source_season, raw_rows in snap_rows_by_season.items():
        season = int(source_season)
        for raw in raw_rows:
            if str(raw.get("game_type") or "").upper() != "REG":
                continue
            row_season = _as_int(raw.get("season"))
            if row_season != season:
                raise DataError(
                    f"snap-count row season {row_season!r} does not match source partition {season}"
                )
            pfr_id = str(raw.get("pfr_player_id") or "").strip()
            game_id = str(raw.get("game_id") or "").strip()
            if not pfr_id or not game_id:
                continue
            values = tuple(
                _nonnegative_int(raw.get(field), field=field)
                for field in ("offense_snaps", "defense_snaps", "st_snaps")
            )
            games = player_games.setdefault((season, pfr_id), {})
            previous = games.get(game_id)
            if previous is not None:
                if previous != values:
                    raise DataError(
                        f"conflicting snap-count rows for {season} {game_id} {pfr_id}"
                    )
                identical_duplicate_rows += 1
                continue
            games[game_id] = values

    materialized = [dict(row) for row in identities]
    unique_identities: dict[tuple[int, str], dict[str, Any]] = {}
    missing_pfr_ids = 0
    for row in materialized:
        draft_year = _as_int(row.get("draft_year") or row.get("season"))
        pfr_id = str(row.get("pfr_player_id") or row.get("pfr_id") or "").strip()
        if draft_year is None:
            continue
        if not pfr_id:
            missing_pfr_ids += 1
            continue
        unique_identities.setdefault((draft_year, pfr_id), row)

    outcomes: dict[tuple[int, str], dict[str, Any]] = {}
    right_censored = 0
    incomplete_source = 0
    known = 0
    contributors = 0
    zero_snap_outcomes = 0
    for draft_year, pfr_id in sorted(unique_identities):
        horizon = tuple(range(draft_year, draft_year + horizon_seasons))
        horizon_end = horizon[-1]
        base = {
            "nfl_three_year_horizon_start": draft_year,
            "nfl_three_year_horizon_end": horizon_end,
            "nfl_three_year_outcome_source": "nflverse_pfr_snap_counts",
        }
        if horizon_end > completed_season:
            outcomes[(draft_year, pfr_id)] = {
                **base,
                "nfl_three_year_outcome_known": False,
                "nfl_three_year_right_censored": True,
                "nfl_three_year_contributor": None,
                "nfl_three_year_outcome_unknown_reason": "right_censored",
            }
            right_censored += 1
            continue
        missing_partitions = sorted(set(horizon) - declared_complete)
        if missing_partitions:
            outcomes[(draft_year, pfr_id)] = {
                **base,
                "nfl_three_year_outcome_known": False,
                "nfl_three_year_right_censored": False,
                "nfl_three_year_contributor": None,
                "nfl_three_year_outcome_unknown_reason": (
                    "incomplete_snap_partitions:" + ",".join(map(str, missing_partitions))
                ),
            }
            incomplete_source += 1
            continue

        offense = defense = special_teams = 0
        game_ids: set[str] = set()
        for season in horizon:
            for game_id, values in player_games.get((season, pfr_id), {}).items():
                game_ids.add(f"{season}:{game_id}")
                offense += values[0]
                defense += values[1]
                special_teams += values[2]
        primary = offense + defense
        contributor = bool(
            primary >= primary_snap_threshold
            or special_teams >= special_teams_snap_threshold
        )
        outcomes[(draft_year, pfr_id)] = {
            **base,
            "nfl_three_year_outcome_known": True,
            "nfl_three_year_right_censored": False,
            "nfl_three_year_contributor": contributor,
            "nfl_three_year_outcome_unknown_reason": None,
            "nfl_three_year_games": len(game_ids),
            "nfl_three_year_offense_snaps": offense,
            "nfl_three_year_defense_snaps": defense,
            "nfl_three_year_primary_snaps": primary,
            "nfl_three_year_special_teams_snaps": special_teams,
            "nfl_three_year_total_snaps": primary + special_teams,
        }
        known += 1
        contributors += int(contributor)
        zero_snap_outcomes += int(primary + special_teams == 0)

    return outcomes, {
        "status": "pass" if not incomplete_source else "partial",
        "outcome_name": "NFL first-three-season contributor",
        "outcome_definition": (
            f"at least {primary_snap_threshold} offense+defense snaps or "
            f"{special_teams_snap_threshold} special-teams snaps across the first "
            f"{horizon_seasons} regular seasons after the draft"
        ),
        "outcome_population": "records with a stable PFR ID and a complete fixed NFL horizon",
        "horizon_seasons": horizon_seasons,
        "completed_nfl_season": int(completed_season),
        "primary_snap_threshold": primary_snap_threshold,
        "special_teams_snap_threshold": special_teams_snap_threshold,
        "identity_rows": len(materialized),
        "unique_pfr_identities": len(unique_identities),
        "missing_pfr_identity_rows": missing_pfr_ids,
        "known_outcomes": known,
        "contributors": contributors,
        "noncontributors": known - contributors,
        "zero_snap_outcomes": zero_snap_outcomes,
        "right_censored_outcomes": right_censored,
        "incomplete_source_outcomes": incomplete_source,
        "identical_duplicate_snap_rows_collapsed": identical_duplicate_rows,
        "complete_snap_seasons": sorted(declared_complete),
    }


def _download_csv(url: str, destination: Path, *, refresh: bool) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    existing = destination.exists() and destination.stat().st_size > 0
    if existing and not refresh:
        return destination
    request = Request(url, headers={"User-Agent": "DraftScope/0.1 (+local research tool)"})
    try:
        with urlopen(request, timeout=60) as response:
            payload = _read_response_limited(
                response,
                max_bytes=_MAX_SNAP_COUNT_DOWNLOAD_BYTES,
                source=url,
            )
    except HTTPError as exc:
        try:
            if existing:
                return destination
            raise DataError(f"Could not download {url}: {exc}") from exc
        finally:
            exc.close()
    except (DataError, URLError, TimeoutError, OSError) as exc:
        if existing:
            return destination
        if isinstance(exc, DataError):
            raise
        raise DataError(f"Could not download {url}: {exc}") from exc
    if not payload:
        raise DataError(f"Downloaded snap-count file was empty: {url}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        try:
            _validate_snap_count_csv(temp_path)
        except (OSError, csv.Error, UnicodeError, ValueError) as exc:
            if existing:
                return destination
            raise DataError(f"Downloaded snap-count CSV was invalid: {exc}") from exc
        os.replace(temp_path, destination)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
    destination.with_suffix(destination.suffix + ".source.json").write_text(
        json.dumps(
            {
                "url": url,
                "downloaded_at": timestamp_utc(),
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return destination


def _validate_snap_count_csv(path: Path) -> None:
    for _ in _read_snap_count_csv(path):
        pass


def _read_snap_count_csv(path: Path) -> Iterable[dict[str, str]]:
    size = path.stat().st_size
    if size > _MAX_SNAP_COUNT_DOWNLOAD_BYTES:
        raise DataError(
            f"{path.name} is {size} bytes; limit is {_MAX_SNAP_COUNT_DOWNLOAD_BYTES}"
        )
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle, strict=True)
        fields = set(reader.fieldnames or ())
        missing = _REQUIRED_SNAP_FIELDS - fields
        if missing:
            raise DataError(
                f"snap-count CSV is missing required fields: {sorted(missing)}"
            )
        for row_number, row in enumerate(reader, start=1):
            if row_number > _MAX_SNAP_COUNT_CSV_ROWS:
                raise DataError(
                    f"{path.name} exceeded {_MAX_SNAP_COUNT_CSV_ROWS} data rows"
                )
            if None in row or any(value is None for value in row.values()):
                raise DataError(
                    f"snap-count CSV has a malformed row at data row {row_number}"
                )
            yield dict(row)


def _content_length(response: Any) -> int | None:
    headers = getattr(response, "headers", None)
    raw = headers.get("Content-Length") if headers is not None else None
    try:
        value = int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return value if value is not None and value >= 0 else None


def _read_response_limited(
    response: Any,
    *,
    max_bytes: int,
    source: str,
) -> bytes:
    limit = max(1, int(max_bytes))
    announced = _content_length(response)
    if announced is not None and announced > limit:
        raise DataError(
            f"Response from {source} announced {announced} bytes; limit is {limit}"
        )
    payload = bytearray()
    while True:
        chunk = response.read(min(_NETWORK_CHUNK_BYTES, limit - len(payload) + 1))
        if not chunk:
            break
        payload.extend(chunk)
        if len(payload) > limit:
            raise DataError(f"Response from {source} exceeded {limit} bytes")
    return bytes(payload)


def _source_sidecar(path: Path) -> dict[str, Any]:
    metadata_path = path.with_suffix(path.suffix + ".source.json")
    try:
        value = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _as_int(value: object) -> int | None:
    number = parse_number(value)
    if number is None or not float(number).is_integer():
        return None
    return int(number)


def _nonnegative_int(value: object, *, field: str) -> int:
    number = parse_number(value)
    if number is None:
        return 0
    if number < 0 or not float(number).is_integer():
        raise DataError(f"{field} must be a nonnegative integer, got {value!r}")
    return int(number)


__all__ = [
    "NFLVERSE_SNAP_COUNTS_URL",
    "NFL_CONTRIBUTOR_HORIZON_SEASONS",
    "NFL_CONTRIBUTOR_PRIMARY_SNAPS",
    "NFL_CONTRIBUTOR_SPECIAL_TEAMS_SNAPS",
    "MIN_COMPLETE_REGULAR_SEASON_GAMES",
    "build_three_year_contributor_outcomes",
    "load_nflverse_snap_counts",
]

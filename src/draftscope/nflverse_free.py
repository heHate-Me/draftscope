from __future__ import annotations

import csv
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .mathstats import percentile_rank
from .records import DataError, normalize_name, parse_bool, parse_number
from .schema import POSITION_GROUPS, normalize_position


NFLVERSE_DEPTH_CHART_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "depth_charts/depth_charts_{season}.csv.gz"
)
NFLVERSE_PLAYER_STATS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats_player/stats_player_week_{season}.csv.gz"
)
NFLVERSE_CONTRACTS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "contracts/historical_contracts.csv.gz"
)

# nflverse's OverTheCap release has not been refreshed since 2022. Its
# historical rows are useful research data, but its is_active/team values are
# not valid evidence of a 2026 roster need.
NFLVERSE_CONTRACT_DATA_AS_OF = "2022-05-29"

_NETWORK_CHUNK_BYTES = 64 * 1024
_MAX_NFLVERSE_DOWNLOAD_BYTES = 256 * 1024 * 1024
_MAX_NFLVERSE_DECOMPRESSED_BYTES = 512 * 1024 * 1024
_MAX_NFLVERSE_CSV_ROWS = 2_000_000

_DEPTH_REQUIRED = {"dt", "team", "player_name", "pos_abb", "pos_rank"}
_STATS_REQUIRED = {
    "player_id",
    "season",
    "week",
    "season_type",
    "game_id",
}
_CONTRACT_REQUIRED = {
    "player",
    "position",
    "team",
    "is_active",
    "year_signed",
    "years",
}

_STATS_NUMERIC_FIELDS = (
    "attempts",
    "passing_yards",
    "passing_tds",
    "passing_interceptions",
    "carries",
    "rushing_yards",
    "rushing_tds",
    "receptions",
    "targets",
    "receiving_yards",
    "receiving_tds",
    "def_tackles_solo",
    "def_tackles_with_assist",
    "def_tackle_assists",
    "def_tackles_for_loss",
    "def_sacks",
    "def_qb_hits",
    "def_interceptions",
    "def_pass_defended",
    "fg_made",
    "fg_att",
    "pat_made",
    "pat_att",
    "pt_att",
    "pt_inside_20",
)

_DEPTH_POSITION_ALIASES = {
    "LDE": "EDGE",
    "RDE": "EDGE",
    "LDT": "IDL",
    "RDT": "IDL",
    "LILB": "LB",
    "RILB": "LB",
    "LCB": "CB",
    "RCB": "CB",
    "NB": "CB",
}

_NON_ROOM_DEPTH_POSITIONS = frozenset({"H", "KR", "PR"})

_TEAM_ALIASES = {
    "49ERS": "SF",
    "BEARS": "CHI",
    "BENGALS": "CIN",
    "BILLS": "BUF",
    "BRONCOS": "DEN",
    "BROWNS": "CLE",
    "BUCCANEERS": "TB",
    "CARDINALS": "ARI",
    "CHARGERS": "LAC",
    "CHIEFS": "KC",
    "COLTS": "IND",
    "COMMANDERS": "WAS",
    "COWBOYS": "DAL",
    "DOLPHINS": "MIA",
    "EAGLES": "PHI",
    "FALCONS": "ATL",
    "FOOTBALL TEAM": "WAS",
    "GIANTS": "NYG",
    "JAGUARS": "JAX",
    "JETS": "NYJ",
    "LIONS": "DET",
    "PACKERS": "GB",
    "PANTHERS": "CAR",
    "PATRIOTS": "NE",
    "RAIDERS": "LV",
    "RAMS": "LAR",
    "RAVENS": "BAL",
    "REDSKINS": "WAS",
    "SAINTS": "NO",
    "SEAHAWKS": "SEA",
    "STEELERS": "PIT",
    "TEXANS": "HOU",
    "TITANS": "TEN",
    "VIKINGS": "MIN",
}


def load_nflverse_team_enrichment(
    cache_dir: str | os.PathLike[str],
    *,
    season: int,
    refresh: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load free NFL room context without making any feed mandatory.

    Depth charts are reduced while streaming to each team's latest timestamp.
    Weekly player statistics retain the requested and previous seasons so early
    current-season evidence can be blended with a stable prior. The old
    contracts release is audited but automatically withheld for modern seasons.
    """

    cache = Path(cache_dir)
    feeds: dict[str, Any] = {}

    depth_url = NFLVERSE_DEPTH_CHART_URL.format(season=season)
    depth_path, depth_feed = _download_optional(
        depth_url,
        cache / f"depth_charts_{season}.csv.gz",
        refresh=refresh,
        required=_DEPTH_REQUIRED,
    )
    feeds["depth_charts"] = depth_feed
    depth_rows: list[dict[str, Any]] = []
    if depth_path is not None:
        try:
            depth_rows = _compact_latest_depth_rows(
                _read_gzip_csv(depth_path, required=_DEPTH_REQUIRED)
            )
            depth_feed["rows_in_latest_snapshots"] = len(depth_rows)
            depth_feed["latest_snapshot"] = max(
                (str(row.get("dt") or "") for row in depth_rows), default=""
            )
        except (OSError, EOFError, csv.Error, ValueError) as exc:
            depth_feed.update(status="invalid", error=str(exc))
            depth_rows = []

    stats_rows: list[dict[str, Any]] = []
    stats_feeds: list[dict[str, Any]] = []
    for stats_season in dict.fromkeys((season, season - 1)):
        if stats_season < 1999:
            continue
        stats_url = NFLVERSE_PLAYER_STATS_URL.format(season=stats_season)
        stats_path, stats_feed = _download_optional(
            stats_url,
            cache / f"stats_player_week_{stats_season}.csv.gz",
            refresh=refresh,
            required=_STATS_REQUIRED,
        )
        stats_feed["season"] = stats_season
        stats_feeds.append(stats_feed)
        if stats_path is None:
            continue
        try:
            compact = _compact_stats_rows(
                _read_gzip_csv(stats_path, required=_STATS_REQUIRED),
                season=stats_season,
            )
            stats_rows.extend(compact)
            stats_feed["regular_season_rows"] = len(compact)
        except (OSError, EOFError, csv.Error, ValueError) as exc:
            stats_feed.update(status="invalid", error=str(exc))
    feeds["weekly_player_stats"] = stats_feeds

    contract_path, contract_feed = _download_optional(
        NFLVERSE_CONTRACTS_URL,
        cache / "historical_contracts.csv.gz",
        refresh=refresh,
        required=_CONTRACT_REQUIRED,
    )
    contract_feed["data_as_of"] = NFLVERSE_CONTRACT_DATA_AS_OF
    contract_rows: list[dict[str, Any]] = []
    if contract_path is not None:
        try:
            contract_rows = _compact_contract_rows(
                _read_gzip_csv(contract_path, required=_CONTRACT_REQUIRED)
            )
            contract_feed["rows"] = len(contract_rows)
        except (OSError, EOFError, csv.Error, ValueError) as exc:
            contract_feed.update(status="invalid", error=str(exc))
    if _contract_source_is_stale(NFLVERSE_CONTRACT_DATA_AS_OF, season):
        contract_feed["status"] = "stale_withheld"
        contract_feed["scoring_allowed"] = False
        contract_feed["reason"] = (
            "nflverse historical contracts have not been refreshed since 2022; "
            f"old team and active flags cannot support {season} needs"
        )
    feeds["contracts"] = contract_feed

    enrichments = build_nflverse_team_enrichment(
        depth_rows,
        stats_rows,
        contract_rows,
        season=season,
        contract_data_as_of=NFLVERSE_CONTRACT_DATA_AS_OF,
    )
    usable_stats_seasons = sorted(
        {
            int(value)
            for row in stats_rows
            if (value := parse_number(row.get("season"))) is not None
        }
    )
    if depth_rows and usable_stats_seasons:
        status = "pass"
    elif depth_rows:
        status = "partial"
    else:
        status = "unavailable"
    return enrichments, {
        "source": "nflverse public bulk releases",
        "license": "CC BY 4.0; upstream data terms also apply",
        "status": status,
        "season": season,
        "rows": len(enrichments),
        "stats_seasons": usable_stats_seasons,
        "feeds": feeds,
        "limitations": [
            "The contracts release is historical and is withheld when stale.",
            "The nflverse injury feed ended after 2024 and is intentionally not used.",
            "Starter-quality need is a depth plus usage/output proxy, not a film grade.",
        ],
    }


def build_nflverse_team_enrichment(
    depth_rows: Iterable[Mapping[str, Any]],
    stats_rows: Iterable[Mapping[str, Any]],
    contract_rows: Iterable[Mapping[str, Any]],
    *,
    season: int,
    contract_data_as_of: str = NFLVERSE_CONTRACT_DATA_AS_OF,
) -> list[dict[str, Any]]:
    """Compute dated team-position fields consumed by DraftScope team fit."""

    depth = _index_latest_depth(depth_rows)
    if not depth:
        return []
    stats, latest_stats_week = _index_stats(stats_rows, season=season)
    contracts = _index_contracts(contract_rows)
    contract_stale = _contract_source_is_stale(contract_data_as_of, season)

    raw_room_values: dict[tuple[str, str], dict[str, Any]] = {}
    for team, team_state in depth.items():
        for position, players in team_state["rooms"].items():
            starters = _depth_starters(
                players,
                position=position,
                schemes=team_state["schemes"],
            )
            production: list[float] = []
            matched_starters = 0
            evidence_seasons: set[int] = set()
            for player in starters:
                player_id = str(player.get("gsis_id") or "").strip()
                value, used_seasons = _blended_player_production(
                    stats.get(player_id, {}),
                    position=position,
                    season=season,
                    latest_current_week=latest_stats_week.get(season, 0),
                )
                if value is None:
                    continue
                matched_starters += 1
                production.append(value)
                evidence_seasons.update(used_seasons)
            room_value = sum(production) / len(production) if production else None
            stats_maturity = _stats_maturity(
                evidence_seasons,
                season=season,
                current_week=latest_stats_week.get(season, 0),
            )

            contract_score: float | None = None
            contract_coverage = 0.0
            matched_contracts = 0
            if not contract_stale and players:
                urgencies: list[float] = []
                for player in players.values():
                    key = (
                        team,
                        position,
                        normalize_name(player.get("name")),
                    )
                    contract = contracts.get(key)
                    if contract is None:
                        continue
                    urgency = _contract_urgency(contract, season=season)
                    if urgency is None:
                        continue
                    matched_contracts += 1
                    urgencies.append(urgency)
                contract_coverage = matched_contracts / len(players)
                contract_score = (
                    sum(urgencies) / len(urgencies) if urgencies else None
                )

            raw_room_values[(team, position)] = {
                "players": players,
                "starters": starters,
                "room_production": room_value,
                "matched_starters": matched_starters,
                "stats_maturity": stats_maturity,
                "evidence_seasons": evidence_seasons,
                "contract_score": contract_score,
                "contract_coverage": contract_coverage,
                "matched_contracts": matched_contracts,
            }

    by_position: dict[str, list[float]] = {}
    for (_team, position), values in raw_room_values.items():
        room_value = values["room_production"]
        if room_value is not None:
            by_position.setdefault(position, []).append(room_value)

    results: list[dict[str, Any]] = []
    for (team, position), values in sorted(raw_room_values.items()):
        state = depth[team]
        players = values["players"]
        starters = values["starters"]
        room_value = values["room_production"]
        production_rank = (
            percentile_rank(room_value, by_position.get(position, ()))
            if room_value is not None
            else None
        )
        starter_need = 100.0 - production_rank if production_rank is not None else None
        starter_match_coverage = (
            values["matched_starters"] / len(starters) if starters else 0.0
        )
        starter_coverage = starter_match_coverage * values["stats_maturity"]
        stats_seasons = sorted(values["evidence_seasons"])
        stats_label = ", ".join(str(value) for value in stats_seasons) or "unavailable"

        schemes = sorted(state["schemes"])
        scheme = " / ".join(schemes)
        snapshot = str(state["dt"] or "")
        snapshot_date = snapshot[:10]

        contract_score = values["contract_score"]
        contract_coverage = values["contract_coverage"]
        if contract_stale:
            contract_source = "unavailable_nflverse_contract_feed_stale_2022"
            contract_confidence = 0.0
        else:
            contract_source = "nflverse_otc_exact_name_team_position_derived"
            contract_confidence = 0.78 * contract_coverage

        notes = (
            f"Latest depth chart: {len(players)} listed {position}, "
            f"{len(starters)} projected starter(s), snapshot {snapshot or 'unknown'}. "
        )
        if starter_need is not None:
            notes += (
                f"Starter usage/output opportunity uses {stats_label} regular-season "
                f"evidence with {starter_match_coverage:.0%} starter identity coverage. "
            )
        else:
            notes += "Starter usage/output evidence unavailable for this room. "
        if contract_stale:
            notes += "Contract scoring withheld: the free nflverse file is stale (2022)."
        elif contract_score is not None:
            notes += (
                f"Contract-window evidence exactly matched {values['matched_contracts']} "
                f"of {len(players)} listed players."
            )
        else:
            notes += "Contract-window evidence unavailable for this room."

        results.append(
            {
                "team": team,
                "season": season,
                "position": position,
                "profile_as_of_date": snapshot_date,
                "scheme": scheme,
                "scheme_source": "nflverse_latest_depth_chart",
                "scheme_as_of_date": snapshot_date,
                "scheme_confidence": 0.95 if scheme else None,
                "starter_quality_need_score": (
                    round(starter_need, 3) if starter_need is not None else None
                ),
                "starter_quality_confidence": (
                    round(0.68 * starter_coverage, 4)
                    if starter_need is not None
                    else None
                ),
                "starter_quality_evidence_coverage": round(starter_coverage, 4),
                "starter_quality_source": (
                    "nflverse_depth_chart_and_weekly_stats_usage_output_proxy"
                    if starter_need is not None
                    else "unavailable_no_matched_weekly_stats"
                ),
                "starter_quality_as_of_date": snapshot_date,
                "starter_quality_evidence_seasons": stats_label,
                "starter_quality_latest_week": max(
                    (latest_stats_week.get(value, 0) for value in stats_seasons),
                    default=None,
                ),
                "contract_need_score": (
                    round(contract_score, 3) if contract_score is not None else None
                ),
                "contract_confidence": (
                    round(contract_confidence, 4)
                    if contract_score is not None
                    else None
                ),
                "contract_evidence_coverage": (
                    round(contract_coverage, 4) if contract_score is not None else 0.0
                ),
                "contract_source": contract_source,
                "contract_as_of_date": contract_data_as_of,
                "depth_chart_room_count": len(players),
                "depth_chart_starter_count": len(starters),
                "depth_chart_backup_count": max(0, len(players) - len(starters)),
                "depth_chart_snapshot": snapshot,
                "depth_chart_source": NFLVERSE_DEPTH_CHART_URL.format(season=season),
                "profile_source": "nflverse_depth_stats_contract_context",
                "notes": notes,
            }
        )
    return results


def merge_nflverse_team_enrichment(
    profiles: Iterable[Mapping[str, Any]],
    enrichments: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Overlay supported free-feed evidence onto existing roster/draft profiles."""

    additions = {
        (
            str(row.get("team") or "").strip().upper(),
            normalize_position(row.get("position")),
        ): dict(row)
        for row in enrichments
        if row.get("team") and row.get("position")
    }
    merged: list[dict[str, Any]] = []
    for raw in profiles:
        row = dict(raw)
        key = (
            str(row.get("team") or "").strip().upper(),
            normalize_position(row.get("position")),
        )
        enrichment = additions.get(key)
        if enrichment is None:
            merged.append(row)
            continue
        original_notes = str(row.get("notes") or "").strip()
        enrichment_notes = str(enrichment.get("notes") or "").strip()
        for field, value in enrichment.items():
            if field in {"team", "season", "position", "notes"}:
                continue
            if value not in (None, ""):
                row[field] = value
        if original_notes and enrichment_notes:
            row["notes"] = f"{original_notes} {enrichment_notes}"
        elif enrichment_notes:
            row["notes"] = enrichment_notes
        previous_source = str(raw.get("profile_source") or "").strip()
        row["profile_source"] = (
            f"{previous_source}_plus_depth_stats"
            if previous_source
            else enrichment["profile_source"]
        )
        merged.append(row)
    return merged


def _download_optional(
    url: str,
    destination: Path,
    *,
    refresh: bool,
    required: set[str] | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    existing = destination.exists() and destination.stat().st_size > 0
    previous = _read_sidecar(destination)
    if existing and not refresh:
        return destination, {
            **previous,
            "url": url,
            "path": str(destination),
            "status": "cached",
            "bytes": destination.stat().st_size,
        }

    headers = {"User-Agent": "DraftScope/0.1 (+local research tool)"}
    if existing and previous.get("etag"):
        headers["If-None-Match"] = str(previous["etag"])
    if existing and previous.get("last_modified"):
        headers["If-Modified-Since"] = str(previous["last_modified"])
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=60) as response:
            payload = _read_response_limited(
                response,
                max_bytes=_MAX_NFLVERSE_DOWNLOAD_BYTES,
                source=url,
            )
            response_headers = response.headers
    except HTTPError as exc:
        try:
            if exc.code == 304 and existing:
                return destination, {
                    **previous,
                    "url": url,
                    "path": str(destination),
                    "status": "not_modified",
                    "checked_at": _timestamp_utc(),
                    "bytes": destination.stat().st_size,
                }
            if existing:
                return destination, {
                    **previous,
                    "url": url,
                    "path": str(destination),
                    "status": "cached_after_error",
                    "error": f"HTTP {exc.code}",
                    "bytes": destination.stat().st_size,
                }
            return None, {
                "url": url,
                "status": "unavailable",
                "error": f"HTTP {exc.code}",
            }
        finally:
            exc.close()
    except (DataError, URLError, TimeoutError, OSError) as exc:
        if existing:
            return destination, {
                **previous,
                "url": url,
                "path": str(destination),
                "status": "cached_after_error",
                "error": str(exc),
                "bytes": destination.stat().st_size,
            }
        return None, {"url": url, "status": "unavailable", "error": str(exc)}

    if not payload:
        if existing:
            return destination, {
                **previous,
                "url": url,
                "path": str(destination),
                "status": "cached_after_empty_response",
                "bytes": destination.stat().st_size,
            }
        return None, {"url": url, "status": "unavailable", "error": "empty response"}

    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        try:
            _validate_gzip_csv(temporary_path, required=required or set())
        except (OSError, EOFError, csv.Error, UnicodeError, ValueError) as exc:
            error = f"invalid gzip CSV: {exc}"
            if existing:
                return destination, {
                    **previous,
                    "url": url,
                    "path": str(destination),
                    "status": "cached_after_invalid_download",
                    "error": error,
                    "bytes": destination.stat().st_size,
                }
            return None, {
                "url": url,
                "status": "unavailable",
                "error": error,
            }
        os.replace(temporary_path, destination)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass

    metadata = {
        "url": url,
        "path": str(destination),
        "status": "downloaded",
        "downloaded_at": _timestamp_utc(),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "etag": response_headers.get("ETag"),
        "last_modified": response_headers.get("Last-Modified"),
    }
    _write_sidecar(destination, metadata)
    return destination, metadata


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


def _read_gzip_csv(
    path: Path,
    *,
    required: set[str],
) -> Iterable[dict[str, str]]:
    with path.open("rb") as compressed:
        with gzip.GzipFile(fileobj=compressed, mode="rb") as decompressed:
            limited = io.BufferedReader(
                _LimitedBinaryReader(
                    decompressed,
                    limit=_MAX_NFLVERSE_DECOMPRESSED_BYTES,
                    source_name=path.name,
                ),
                buffer_size=_NETWORK_CHUNK_BYTES,
            )
            with io.TextIOWrapper(
                limited,
                newline="",
                encoding="utf-8-sig",
            ) as handle:
                reader = csv.DictReader(handle, strict=True)
                fields = set(reader.fieldnames or ())
                if not fields:
                    raise ValueError(f"{path.name} has no CSV header")
                missing = required - fields
                if missing:
                    raise ValueError(
                        f"{path.name} is missing required fields: {sorted(missing)}"
                    )
                for row_number, row in enumerate(reader, start=1):
                    if row_number > _MAX_NFLVERSE_CSV_ROWS:
                        raise DataError(
                            f"{path.name} exceeded {_MAX_NFLVERSE_CSV_ROWS} data rows"
                        )
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError(
                            f"{path.name} has a malformed CSV row at data row {row_number}"
                        )
                    yield dict(row)


class _LimitedBinaryReader(io.RawIOBase):
    """Expose at most ``limit`` bytes from a decompression stream."""

    def __init__(
        self,
        stream: Any,
        *,
        limit: int,
        source_name: str = "gzip stream",
    ) -> None:
        super().__init__()
        self._source = stream
        self._limit = max(1, int(limit))
        self._source_name = source_name
        self._count = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if not buffer:
            return 0
        remaining = self._limit - self._count
        chunk = self._source.read(min(len(buffer), remaining + 1))
        if not chunk:
            return 0
        self._count += len(chunk)
        if self._count > self._limit:
            raise DataError(
                f"Decompressed {self._source_name} exceeded {self._limit} bytes"
            )
        buffer[: len(chunk)] = chunk
        return len(chunk)


def _validate_gzip_csv(path: Path, *, required: set[str]) -> None:
    for _ in _read_gzip_csv(path, required=required):
        pass


def _compact_latest_depth_rows(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    state = _index_latest_depth(rows)
    compact: list[dict[str, Any]] = []
    for team, team_state in state.items():
        for players in team_state["rooms"].values():
            for player in players.values():
                compact.append(
                    {
                        "dt": team_state["dt"],
                        "team": team,
                        "player_name": player["name"],
                        "espn_id": player["espn_id"],
                        "gsis_id": player["gsis_id"],
                        "pos_abb": player["raw_position"],
                        "pos_rank": player["rank"],
                        "pos_grp": " / ".join(sorted(team_state["schemes"])),
                    }
                )
    return compact


def _compact_stats_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    season: int,
) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for raw in rows:
        row_season = parse_number(raw.get("season"))
        if row_season is None or int(row_season) != season:
            continue
        if str(raw.get("season_type") or "").strip().upper() != "REG":
            continue
        row = {
            key: raw.get(key)
            for key in (
                "player_id",
                "season",
                "week",
                "season_type",
                "game_id",
                "team",
                "position",
            )
        }
        row.update({key: raw.get(key) for key in _STATS_NUMERIC_FIELDS})
        compact.append(row)
    return compact


def _compact_contract_rows(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    fields = (
        "player",
        "position",
        "team",
        "is_active",
        "year_signed",
        "years",
        "apy",
        "apy_cap_pct",
        "otc_id",
    )
    return [{key: raw.get(key) for key in fields} for raw in rows]


def _index_latest_depth(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    teams: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(rows):
        team = _team_abbreviation(raw.get("team"))
        timestamp = str(raw.get("dt") or "").strip()
        if not team or not timestamp:
            continue
        current = teams.get(team)
        if current is None or timestamp > current["dt"]:
            current = {"dt": timestamp, "rooms": {}, "schemes": set()}
            teams[team] = current
        elif timestamp < current["dt"]:
            continue

        scheme = str(raw.get("pos_grp") or "").strip()
        if scheme and scheme.lower() != "special teams":
            for item in (part.strip() for part in scheme.split(" / ")):
                if item and item.lower() != "special teams":
                    current["schemes"].add(item)

        raw_position = str(
            raw.get("pos_abb") or raw.get("position") or ""
        ).strip().upper()
        position = _depth_position(raw_position, scheme=scheme)
        if position not in POSITION_GROUPS:
            continue
        identity = str(
            raw.get("gsis_id")
            or raw.get("espn_id")
            or normalize_name(raw.get("player_name"))
            or f"row-{index}"
        ).strip()
        rank_value = parse_number(raw.get("pos_rank"))
        rank = int(rank_value) if rank_value is not None and rank_value > 0 else None
        room = current["rooms"].setdefault(position, {})
        previous = room.get(identity)
        player = {
            "name": str(raw.get("player_name") or "").strip(),
            "gsis_id": str(raw.get("gsis_id") or "").strip(),
            "espn_id": str(raw.get("espn_id") or "").strip(),
            "raw_position": raw_position,
            "rank": rank,
        }
        if previous is None:
            room[identity] = player
        elif rank is not None and (
            previous.get("rank") is None or rank < previous["rank"]
        ):
            room[identity] = player
    return teams


def _index_stats(
    rows: Iterable[Mapping[str, Any]],
    *,
    season: int,
) -> tuple[dict[str, dict[int, dict[str, Any]]], dict[int, int]]:
    players: dict[str, dict[int, dict[str, Any]]] = {}
    latest_week: dict[int, int] = {}
    seen: set[tuple[str, int, str]] = set()
    for raw in rows:
        if str(raw.get("season_type") or "REG").strip().upper() != "REG":
            continue
        row_season_value = parse_number(raw.get("season"))
        if row_season_value is None:
            continue
        row_season = int(row_season_value)
        if row_season not in {season, season - 1}:
            continue
        player_id = str(raw.get("player_id") or "").strip()
        if not player_id:
            continue
        week_value = parse_number(raw.get("week"))
        week = int(week_value) if week_value is not None else 0
        game_id = str(raw.get("game_id") or f"week-{week}").strip()
        duplicate_key = (player_id, row_season, game_id)
        if duplicate_key in seen:
            continue
        seen.add(duplicate_key)
        latest_week[row_season] = max(latest_week.get(row_season, 0), week)
        record = players.setdefault(player_id, {}).setdefault(
            row_season,
            {"games": 0.0, **{field: 0.0 for field in _STATS_NUMERIC_FIELDS}},
        )
        record["games"] += 1.0
        for field in _STATS_NUMERIC_FIELDS:
            value = parse_number(raw.get(field))
            if value is not None:
                record[field] += value
    return players, latest_week


def _index_contracts(
    rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str, str], dict[str, Any]]:
    contracts: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw in rows:
        if parse_bool(raw.get("is_active")) is not True:
            continue
        team = _team_abbreviation(raw.get("team"))
        position = normalize_position(raw.get("position"))
        name = normalize_name(raw.get("player"))
        if not team or position not in POSITION_GROUPS or not name:
            continue
        year_signed = parse_number(raw.get("year_signed"))
        years = parse_number(raw.get("years"))
        if year_signed is None or years is None or year_signed < 1990 or years <= 0:
            continue
        key = (team, position, name)
        normalized = {
            "year_signed": int(year_signed),
            "years": int(years),
            "apy": parse_number(raw.get("apy")),
            "apy_cap_pct": parse_number(raw.get("apy_cap_pct")),
        }
        previous = contracts.get(key)
        if previous is None or normalized["year_signed"] >= previous["year_signed"]:
            contracts[key] = normalized
    return contracts


def _blended_player_production(
    seasons: Mapping[int, Mapping[str, Any]],
    *,
    position: str,
    season: int,
    latest_current_week: int,
) -> tuple[float | None, set[int]]:
    current = _production_per_game(seasons.get(season), position)
    prior = _production_per_game(seasons.get(season - 1), position)
    if current is not None and prior is not None:
        current_weight = min(0.75, max(0.15, latest_current_week / 8.0))
        return current_weight * current + (1.0 - current_weight) * prior, {
            season - 1,
            season,
        }
    if current is not None:
        return current, {season}
    if prior is not None:
        return prior, {season - 1}
    return None, set()


def _production_per_game(
    stats: Mapping[str, Any] | None,
    position: str,
) -> float | None:
    if stats is None:
        return None
    games = parse_number(stats.get("games"))
    if games is None or games <= 0:
        return None

    def number(field: str) -> float:
        return parse_number(stats.get(field)) or 0.0

    if position == "QB":
        value = (
            number("passing_yards") / 25.0
            + 4.0 * number("passing_tds")
            - 3.0 * number("passing_interceptions")
            + number("rushing_yards") / 10.0
            + 6.0 * number("rushing_tds")
        )
    elif position == "RB":
        value = (
            (number("rushing_yards") + number("receiving_yards")) / 10.0
            + 6.0 * (number("rushing_tds") + number("receiving_tds"))
            + 0.5 * number("receptions")
        )
    elif position in {"WR", "TE"}:
        value = (
            number("receiving_yards") / 10.0
            + 6.0 * number("receiving_tds")
            + 0.5 * number("receptions")
        )
    elif position in {"EDGE", "IDL", "LB", "CB", "S"}:
        tackles = number("def_tackles_solo") + max(
            number("def_tackle_assists"), number("def_tackles_with_assist")
        )
        value = (
            tackles
            + 4.0 * number("def_sacks")
            + 3.0 * number("def_tackles_for_loss")
            + 5.0 * number("def_interceptions")
            + 1.5 * number("def_pass_defended")
            + 1.5 * number("def_qb_hits")
        )
    elif position == "K":
        value = (
            3.0 * number("fg_made")
            - max(0.0, number("fg_att") - number("fg_made"))
            + number("pat_made")
        )
    elif position == "P":
        value = number("pt_att") + 0.5 * number("pt_inside_20")
    else:
        return None
    return max(0.0, value / games)


def _stats_maturity(
    evidence_seasons: set[int],
    *,
    season: int,
    current_week: int,
) -> float:
    if season in evidence_seasons:
        return 0.35 + 0.65 * min(1.0, max(0, current_week) / 8.0)
    if season - 1 in evidence_seasons:
        return 0.90
    return 0.0


def _contract_urgency(
    contract: Mapping[str, Any],
    *,
    season: int,
) -> float | None:
    year_signed = parse_number(contract.get("year_signed"))
    years = parse_number(contract.get("years"))
    if year_signed is None or years is None or years <= 0:
        return None
    final_season = int(year_signed + years - 1)
    if final_season <= season:
        return 100.0
    if final_season == season + 1:
        return 65.0
    if final_season == season + 2:
        return 25.0
    return 0.0


def _contract_source_is_stale(as_of: str, season: int) -> bool:
    try:
        source_year = int(str(as_of).strip()[:4])
    except (TypeError, ValueError):
        return True
    return source_year < season - 1


def _depth_starters(
    players: Mapping[str, Mapping[str, Any]],
    *,
    position: str,
    schemes: Iterable[str],
) -> list[Mapping[str, Any]]:
    materialized = list(players.values())
    if position in {"WR", "TE"}:
        pattern = re.compile(rf"(\d+)\s*{position}\b", re.IGNORECASE)
        counts = [
            int(match.group(1))
            for scheme in schemes
            if (match := pattern.search(str(scheme))) is not None
        ]
        if counts:
            ordered = sorted(
                materialized,
                key=lambda row: (
                    row.get("rank") is None,
                    row.get("rank") if row.get("rank") is not None else 10_000,
                    normalize_name(row.get("name")),
                ),
            )
            return ordered[: min(max(counts), len(ordered))]
    return [row for row in materialized if row.get("rank") == 1]


def _depth_position(value: object, *, scheme: str = "") -> str:
    raw = str(value or "").strip().upper()
    if raw in _NON_ROOM_DEPTH_POSITIONS:
        return ""
    if "3-4" in scheme:
        if raw in {"LDE", "RDE"}:
            return "IDL"
        if raw in {"SLB", "WLB"}:
            return "EDGE"
    return _DEPTH_POSITION_ALIASES.get(raw, normalize_position(raw))


def _team_abbreviation(value: object) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    return _TEAM_ALIASES.get(raw, raw if 2 <= len(raw) <= 3 else "")


def _timestamp_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sidecar_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".source.json")


def _read_sidecar(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(_sidecar_path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _write_sidecar(path: Path, metadata: Mapping[str, Any]) -> None:
    sidecar = _sidecar_path(path)
    sidecar.write_text(json.dumps(dict(metadata), indent=2) + "\n", encoding="utf-8")


__all__ = [
    "NFLVERSE_CONTRACTS_URL",
    "NFLVERSE_CONTRACT_DATA_AS_OF",
    "NFLVERSE_DEPTH_CHART_URL",
    "NFLVERSE_PLAYER_STATS_URL",
    "build_nflverse_team_enrichment",
    "load_nflverse_team_enrichment",
    "merge_nflverse_team_enrichment",
]

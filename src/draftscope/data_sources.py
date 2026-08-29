from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
import hashlib
from html.parser import HTMLParser
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

from .college_features import (
    CFBD_GENERATED_PRODUCTION_FIELDS,
    derive_college_production,
    derive_player_success,
)
from .credentials import resolve_cfbd_api_key
from .records import (
    DataError,
    derive_features,
    normalize_name,
    normalize_record,
    parse_bool,
    parse_height,
    parse_number,
    slug,
    timestamp_utc,
    write_records,
)
from .mathstats import median, percentile_rank, weighted_mean
from .historical_honors import load_historical_honors
from .nfl_outcomes import (
    NFL_CONTRIBUTOR_HORIZON_SEASONS,
    build_three_year_contributor_outcomes,
    load_nflverse_snap_counts,
)
from .recruiting import build_recruiting_index, match_recruiting_profile
from .schema import POSITION_GROUPS, PRODUCTION, normalize_position


NFLVERSE_COMBINE_URL = "https://github.com/nflverse/nflverse-data/releases/download/combine/combine.csv"
NFLVERSE_DRAFT_URL = "https://github.com/nflverse/nflverse-data/releases/download/draft_picks/draft_picks.csv"
NFLVERSE_PLAYERS_URL = "https://github.com/nflverse/nflverse-data/releases/download/players/players.csv"
NFLVERSE_ROSTER_URL = "https://github.com/nflverse/nflverse-data/releases/download/rosters/roster_{season}.csv"
WIKIPEDIA_API_URL = "https://en.wikipedia.org/w/api.php"
WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
WIKIPEDIA_HOF_LIST_PAGE = "List of Pro Football Hall of Fame inductees"
WIKIPEDIA_HOF_CATEGORY = "Category:Pro Football Hall of Fame inductees"
CFBD_BASE_URL = "https://api.collegefootballdata.com"

_NETWORK_CHUNK_BYTES = 64 * 1024
_MAX_BULK_DOWNLOAD_BYTES = 256 * 1024 * 1024
_MAX_BULK_CSV_ROWS = 2_000_000
_MAX_JSON_RESPONSE_BYTES = 32 * 1024 * 1024
_MAX_CFBD_RESPONSE_BYTES = 64 * 1024 * 1024
_MAX_HTTP_ERROR_BYTES = 4 * 1024
_WIKIMEDIA_MAX_RETRIES = 4
_WIKIMEDIA_MAX_RETRY_DELAY_SECONDS = 30.0
_WIKIMEDIA_MIN_REQUEST_INTERVAL_SECONDS = 0.10
_WIKIMEDIA_PACING_LOCK = threading.Lock()
_WIKIMEDIA_LAST_REQUEST_AT: dict[str, float] = {}

ACTIVE_NFL_ROSTER_STATUSES = frozenset(
    {"ACT", "RES", "E14", "INA", "PUP", "SUS", "EXE", "DEV"}
)
_COMBINE_MEASUREMENT_FIELDS = (
    "height_in",
    "weight_lb",
    "forty_s",
    "bench_reps",
    "vertical_in",
    "broad_jump_in",
    "three_cone_s",
    "shuttle_s",
)

NFL_CAREER_ELITE_DEFINITION = (
    "Pro Football Hall of Fame OR at least 1 first-team All-Pro selection "
    "OR at least 3 Pro Bowl selections"
)

_CFBD_PLACEHOLDER_KEYS = {
    "yourkey",
    "yourapikey",
    "yourcfbdapikey",
    "replacewithyourkey",
    "replaceme",
    "changeme",
    "placeholder",
    "examplekey",
}


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
    """Read a response incrementally and reject it before unbounded growth."""

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


def _read_path_limited(path: Path, *, max_bytes: int, source: str) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise DataError(f"Could not inspect {source}: {exc}") from exc
    if size > max_bytes:
        raise DataError(f"{source} is {size} bytes; limit is {max_bytes}")
    with path.open("rb") as handle:
        return _read_response_limited(handle, max_bytes=max_bytes, source=source)


def _close_http_error(error: HTTPError) -> None:
    close = getattr(error, "close", None)
    if callable(close):
        close()


def _http_error_detail(error: HTTPError) -> str:
    """Consume and close a small diagnostic body without leaking a socket."""

    try:
        body = _read_response_limited(
            error,
            max_bytes=_MAX_HTTP_ERROR_BYTES,
            source="HTTP error body",
        )
    except (DataError, OSError):
        body = b""
    finally:
        _close_http_error(error)
    return body.decode("utf-8", errors="replace")[:_MAX_HTTP_ERROR_BYTES]


def _wikimedia_origin(url: str) -> str | None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    if host == "wikidata.org" or host.endswith(".wikidata.org"):
        return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"
    if host == "wikipedia.org" or host.endswith(".wikipedia.org"):
        return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"
    return None


def _pace_wikimedia_request(url: str) -> None:
    origin = _wikimedia_origin(url)
    if origin is None:
        return
    with _WIKIMEDIA_PACING_LOCK:
        previous = _WIKIMEDIA_LAST_REQUEST_AT.get(origin)
        if previous is not None:
            remaining = (
                _WIKIMEDIA_MIN_REQUEST_INTERVAL_SECONDS
                - (time.monotonic() - previous)
            )
            if remaining > 0:
                time.sleep(remaining)
        _WIKIMEDIA_LAST_REQUEST_AT[origin] = time.monotonic()


def cfbd_api_key_status(api_key: str | None = None) -> str:
    """Return ``missing``, ``placeholder``, or ``configured`` without exposing a key."""

    raw = resolve_cfbd_api_key(api_key)
    value = str(raw or "").strip()
    if not value:
        return "missing"
    normalized = re.sub(r"[^a-z0-9]", "", value.lower())
    if (
        not normalized
        or normalized in _CFBD_PLACEHOLDER_KEYS
        or ("your" in normalized and "key" in normalized)
        or value.startswith("<")
        or value.endswith(">")
    ):
        return "placeholder"
    return "configured"


def _download(url: str, destination: Path, *, refresh: bool = False) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size > 0 and not refresh:
        return destination
    request = Request(url, headers={"User-Agent": "DraftScope/0.1 (+local research tool)"})
    temp_name: str | None = None
    try:
        with urlopen(request, timeout=60) as response:
            announced = _content_length(response)
            if announced is not None and announced > _MAX_BULK_DOWNLOAD_BYTES:
                raise DataError(
                    f"Response from {url} announced {announced} bytes; "
                    f"limit is {_MAX_BULK_DOWNLOAD_BYTES}"
                )
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", dir=destination.parent
            )
            digest = hashlib.sha256()
            total = 0
            try:
                with os.fdopen(fd, "wb") as handle:
                    while True:
                        chunk = response.read(_NETWORK_CHUNK_BYTES)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > _MAX_BULK_DOWNLOAD_BYTES:
                            raise DataError(
                                f"Response from {url} exceeded "
                                f"{_MAX_BULK_DOWNLOAD_BYTES} bytes"
                            )
                        digest.update(chunk)
                        handle.write(chunk)
            except Exception:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
                temp_name = None
                raise
    except HTTPError as exc:
        _close_http_error(exc)
        if destination.exists() and destination.stat().st_size > 0:
            _mark_cache_fallback(destination, url=url, error=exc)
            return destination
        raise DataError(f"Could not download {url}: {exc}") from exc
    except (URLError, TimeoutError, DataError) as exc:
        if destination.exists() and destination.stat().st_size > 0:
            _mark_cache_fallback(destination, url=url, error=exc)
            return destination
        raise DataError(f"Could not download {url}: {exc}") from exc
    if total <= 0 or temp_name is None:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
        raise DataError(f"Downloaded file was empty: {url}")
    try:
        os.replace(temp_name, destination)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    metadata = {
        "url": url,
        "downloaded_at": timestamp_utc(),
        "bytes": total,
        "sha256": digest.hexdigest(),
        "cache_fallback": False,
    }
    destination.with_suffix(destination.suffix + ".source.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )
    return destination


def _mark_cache_fallback(
    destination: Path,
    *,
    url: str,
    error: object,
) -> None:
    """Record that a requested refresh fell back to an older cached artifact."""

    metadata_path = destination.with_suffix(destination.suffix + ".source.json")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        metadata = {"url": url}
    if not isinstance(metadata, dict):
        metadata = {"url": url}
    metadata.update(
        {
            "cache_fallback": True,
            "last_refresh_attempt_at": timestamp_utc(),
            "last_refresh_error": str(error),
        }
    )
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


def _read_csv(path: Path) -> list[dict[str, str]]:
    size = path.stat().st_size
    if size > _MAX_BULK_DOWNLOAD_BYTES:
        raise DataError(
            f"CSV {path} is {size} bytes; limit is {_MAX_BULK_DOWNLOAD_BYTES}"
        )
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows: list[dict[str, str]] = []
        for row in csv.DictReader(handle):
            if len(rows) >= _MAX_BULK_CSV_ROWS:
                raise DataError(
                    f"CSV {path} exceeded {_MAX_BULK_CSV_ROWS} data rows"
                )
            rows.append(dict(row))
        return rows


def _nfl_career_outcome_fields(
    draft_row: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Normalize nflverse career honors without treating draft capital as elite."""

    if draft_row is None:
        return {
            "nfl_hof": None,
            "nfl_all_pro_selections": None,
            "nfl_pro_bowls": None,
            "nfl_career_elite": None,
        }
    hof = parse_bool(draft_row.get("hof"))
    all_pro = parse_number(draft_row.get("allpro"))
    pro_bowls = parse_number(draft_row.get("probowls"))
    evidence_available = any(
        value not in (None, "")
        for value in (
            draft_row.get("hof"),
            draft_row.get("allpro"),
            draft_row.get("probowls"),
        )
    )
    career_elite = (
        bool(
            hof is True
            or (all_pro is not None and all_pro >= 1)
            or (pro_bowls is not None and pro_bowls >= 3)
        )
        if evidence_available
        else None
    )
    return {
        "nfl_hof": hof,
        "nfl_all_pro_selections": all_pro,
        "nfl_pro_bowls": pro_bowls,
        "nfl_career_elite": career_elite,
    }


class _HallOfFameTableParser(HTMLParser):
    """Read the player/position rows from Wikipedia's rendered HOF table."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[dict[str, Any]]]] = []
        self._table_rows: list[list[dict[str, Any]]] | None = None
        self._table_depth = 0
        self._row: list[dict[str, Any]] | None = None
        self._cell: dict[str, Any] | None = None
        self._fn_depth = 0
        self._ignored_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "table":
            if self._table_depth == 0:
                self._table_rows = []
            self._table_depth += 1
            return
        if self._table_depth != 1:
            return
        if self._ignored_depth:
            self._ignored_depth += 1
            return
        if tag in {"style", "script"}:
            self._ignored_depth = 1
            return
        if tag == "tr":
            self._row = []
            return
        if tag in {"td", "th"} and self._row is not None:
            self._cell = {
                "tag": tag,
                "attrs": attributes,
                "text_parts": [],
                "name_parts": [],
                "links": [],
            }
            return
        if self._cell is None:
            return
        if tag == "span" and "fn" in attributes.get("class", "").split():
            self._fn_depth += 1
        elif tag == "a":
            self._cell["links"].append(
                (attributes.get("href"), attributes.get("title"))
            )
        elif tag in {"br", "li", "p", "div"}:
            self._cell["text_parts"].append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            if self._table_depth == 1 and self._table_rows is not None:
                self.tables.append(self._table_rows)
                self._table_rows = None
            if self._table_depth:
                self._table_depth -= 1
            self._row = None
            self._cell = None
            self._fn_depth = 0
            return
        if self._table_depth != 1:
            return
        if self._ignored_depth:
            self._ignored_depth -= 1
            return
        if tag == "span" and self._fn_depth:
            self._fn_depth -= 1
            return
        if tag in {"td", "th"} and self._cell is not None:
            self._cell["text"] = " ".join(
                "".join(self._cell.pop("text_parts")).split()
            )
            self._cell["name"] = " ".join(
                "".join(self._cell.pop("name_parts")).split()
            )
            if self._row is not None:
                self._row.append(self._cell)
            self._cell = None
            self._fn_depth = 0
            self._ignored_depth = 0
            return
        if tag == "tr" and self._row is not None:
            if self._row and self._table_rows is not None:
                self._table_rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is None or self._ignored_depth:
            return
        self._cell["text_parts"].append(data)
        if self._fn_depth:
            self._cell["name_parts"].append(data)


_HISTORICAL_POSITION_PHRASES = (
    ("quarterback", "QB"),
    ("running back", "RB"),
    ("halfback", "RB"),
    ("fullback", "RB"),
    ("wide receiver", "WR"),
    ("flanker", "WR"),
    ("split end", "WR"),
    ("offensive end", "WR"),
    ("tight end", "TE"),
    ("defensive tackle", "IDL"),
    ("nose tackle", "IDL"),
    ("defensive middle guard", "IDL"),
    ("defensive end", "EDGE"),
    ("outside linebacker", "EDGE"),
    ("middle linebacker", "LB"),
    ("inside linebacker", "LB"),
    ("linebacker", "LB"),
    ("cornerback", "CB"),
    ("safety", "S"),
    ("offensive tackle", "OT"),
    ("guard", "IOL"),
    ("center", "IOL"),
    ("tackle", "OT"),
    ("placekicker", "K"),
    ("kicker", "K"),
    ("punter", "P"),
    ("long snapper", "LS"),
)

_HOF_POSITION_OVERRIDES: dict[str, dict[str, Any]] = {
    normalize_name("Bobby Dillon"): {
        "positions": ("S",),
        "source": "https://www.profootballhof.com/players/bobby-dillon",
        "note": "Official Hall of Fame profile identifies Bobby Dillon as a safety",
    },
    normalize_name("Sammy Baugh"): {
        "positions": ("S",),
        "source": "https://www.profootballhof.com/news/the-passing-of-a-legend",
        "note": "Official Hall of Fame biography identifies Sammy Baugh as a defensive halfback",
    },
}

# Some early honor tables use only the umbrella label "defensive back" and
# Wikidata repeats that umbrella rather than resolving a modern comparison
# role. These reviewed identity-specific mappings use primary/official NFL
# sources and apply only to the historical comparison catalog.
_HISTORICAL_HONORS_POSITION_OVERRIDES: dict[str, dict[str, Any]] = {
    "Q4895281": {
        "positions": ("CB",),
        "source": "https://static.clubs.nfl.com/image/upload/colts/j6mk3eatgrawmdswxz5n.pdf",
        "note": "Colts historical lineup identifies Bert Rechichar as left cornerback in addition to kicker duty",
    },
    "Q5293290": {
        "positions": ("CB",),
        "source": "https://static.www.nfl.com/image/upload/v1601829276/league/yiioaooxijulqvxigsjo.pdf",
        "note": "Browns historical lineup identifies Don Paul as right cornerback",
    },
    "Q16105791": {
        "positions": ("S",),
        "source": "https://static.clubs.nfl.com/image/upload/cardinals/zwtdvmaygjndyhmdvohu.pdf",
        "note": "Cardinals historical guide identifies Jerry Stovall as a safety",
    },
    "Q6184428": {
        "positions": (),
        "source": "https://www.steelers.com/news/former-lb-jerry-shipkey-84-1042365",
        "note": "Official Steelers history supports Jerry Shipkey at linebacker; the 1951 AP table's defensive-halfback label remains unresolved rather than guessed as CB or S",
    },
    "Q6194539": {
        "positions": ("CB",),
        "source": "https://www.nfl.com/news/former-lions-db-david-dies-at-79-09000d5d80087a4f",
        "note": "NFL biography identifies Jim David as Detroit's left cornerback",
    },
    "Q6860778": {
        "positions": ("CB",),
        "source": "https://static.clubs.nfl.com/image/upload/colts/bcma9beed9onri27hvm2.pdf",
        "note": "Colts historical guide identifies Milt Davis as a cornerback",
    },
    "Q3530668": {
        "positions": ("CB",),
        "source": "https://media.eagles.1rmg.com/wp-content/uploads/2020/06/12025527/2016-Philadelphia-Eagles-Media-Guide.pdf",
        "note": "Eagles historical guide identifies Tom Brookshier as a cornerback",
    },
    "Q2021033": {
        "positions": ("CB",),
        "source": "https://www.pro-football-reference.com/players/M/MatsOl00.htm",
        "note": "Ollie Matson's 1952 defensive-halfback role is indexed in the modern cornerback family",
    },
    "Q2040505": {
        "positions": ("S",),
        "source": "https://www.pro-football-reference.com/teams/nyg/1951.htm",
        "note": "Otto Schnellbacher's 1951 lineup role is listed as free safety",
    },
    "Q1720726": {
        "positions": ("S",),
        "source": "https://library.sfo2.cdn.digitaloceanspaces.com/publications/football/yearbooks/FWASRMG-1981-washington-commanders-media-guide.pdf",
        "note": "Washington's 1981 media guide lists Mike Nelms as safety/kick returner; his qualifying honors were earned as a returner",
    },
}

_HOF_CATEGORY_TITLE_ALIASES = {
    normalize_name("Dan M. Rooney"): normalize_name("Dan Rooney"),
}
_HOF_CATEGORY_NONMEMBER_TITLES = {
    normalize_name("Kathy Roth-Douquet"),
    normalize_name("List of Pro Football Hall of Fame inductees"),
}


def _apply_hof_position_override(
    profile: dict[str, Any], positions: Sequence[str]
) -> list[str]:
    resolved = list(positions)
    override = _HOF_POSITION_OVERRIDES.get(normalize_name(profile.get("name")))
    if not override:
        return resolved
    for position in override["positions"]:
        if position not in resolved:
            resolved.append(position)
    profile["position_source"] = override["source"]
    existing_note = str(profile.get("position_mapping_note") or "").strip()
    profile["position_mapping_note"] = "; ".join(
        value for value in (existing_note, str(override["note"])) if value
    )
    if not profile.get("position") and resolved:
        profile["position"] = resolved[0]
    return resolved


def _apply_historical_honors_position_override(
    profile: dict[str, Any], positions: Sequence[str]
) -> list[str]:
    """Apply a reviewed role only to the exact Wikidata player identity."""

    resolved = list(positions)
    override = _HISTORICAL_HONORS_POSITION_OVERRIDES.get(
        str(profile.get("wikidata_id") or "")
    )
    if not override:
        return resolved
    for position in override["positions"]:
        if position not in resolved:
            resolved.append(position)
    if not profile.get("position") and resolved:
        profile["position"] = resolved[0]
    profile["position_source"] = override["source"]
    existing_note = str(profile.get("position_mapping_note") or "").strip()
    profile["position_mapping_note"] = "; ".join(
        value for value in (existing_note, str(override["note"])) if value
    )
    return resolved


def _unresolved_historical_position_families(
    value: object, positions: Sequence[str]
) -> dict[str, frozenset[str]]:
    """Return umbrella source roles not yet mapped to a supported sub-position."""

    text = " ".join(str(value or "").casefold().replace("-", " ").split())
    resolved = {
        position
        for raw_position in positions
        if (position := normalize_position(raw_position)) in POSITION_GROUPS
    }
    families: dict[str, frozenset[str]] = {}
    if re.search(
        r"\b(?:defensive\s+(?:backs?|halfbacks?)|db|dhb)\b", text
    ) and not ({"CB", "S"} & resolved):
        families["defensive_back"] = frozenset({"CB", "S"})
    if re.search(
        r"\b(?:defensive\s+linem(?:a|e)n|dl)\b", text
    ) and not ({"EDGE", "IDL"} & resolved):
        families["defensive_line"] = frozenset({"EDGE", "IDL"})
    generic_end_text = re.sub(
        r"\b(?:defensive|offensive|tight|split)\s+ends?\b", " ", text
    )
    if re.search(r"\bends?\b", generic_end_text) and not (
        {"WR", "TE", "EDGE"} & resolved
    ):
        families["end"] = frozenset({"WR", "TE", "EDGE"})
    return families


def _historical_positions(value: object) -> tuple[str, ...]:
    """Map every explicit HOF role without double-counting nested phrases."""

    raw = re.sub(
        r"pre-modern era\s*:?\s*two-way performer",
        " ",
        str(value or ""),
        flags=re.IGNORECASE,
    )
    raw = re.sub(
        r"\bdefensive\s+halfbacks?\b",
        "defensive back",
        raw,
        flags=re.IGNORECASE,
    )
    text = " ".join(raw.lower().replace("-", " ").split())
    text = re.sub(
        r"\bdefensive end\s*/\s*tackle\b",
        "defensive end/defensive tackle",
        text,
    )
    text = re.sub(
        r"\bdefensive tackle\s*/\s*end\b",
        "defensive tackle/defensive end",
        text,
    )
    matches: list[tuple[int, int, str]] = []
    for phrase, position in _HISTORICAL_POSITION_PHRASES:
        for match in re.finditer(rf"\b{re.escape(phrase)}\b", text):
            matches.append((match.start(), match.end(), position))
    matches.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    occupied: list[tuple[int, int]] = []
    positions: list[str] = []
    for start, end, position in matches:
        if any(start < used_end and end > used_start for used_start, used_end in occupied):
            continue
        occupied.append((start, end))
        if position not in positions:
            positions.append(position)
    return tuple(positions)


def _historical_position(value: object) -> str:
    """Map a HOF role to DraftScope without guessing an ambiguous old-time end."""

    positions = _historical_positions(value)
    return positions[0] if positions else ""


def _expanded_table_rows(
    rows: Sequence[Sequence[Mapping[str, Any]]],
) -> list[list[Mapping[str, Any]]]:
    """Expand HTML row/column spans into logical table columns."""

    active: dict[int, tuple[Mapping[str, Any], int]] = {}
    expanded: list[list[Mapping[str, Any]]] = []
    for row_index, physical_cells in enumerate(rows):
        logical: dict[int, Mapping[str, Any]] = {
            column: cell
            for column, (cell, end_row) in active.items()
            if end_row >= row_index
        }
        column = 0
        for cell in physical_cells:
            while column in logical:
                column += 1
            attrs = cell.get("attrs")
            attrs = attrs if isinstance(attrs, Mapping) else {}
            try:
                colspan = max(1, int(str(attrs.get("colspan") or "1")))
                rowspan = max(1, int(str(attrs.get("rowspan") or "1")))
            except ValueError:
                colspan = rowspan = 1
            for offset in range(colspan):
                target = column + offset
                logical[target] = cell
                if rowspan > 1:
                    active[target] = (cell, row_index + rowspan - 1)
            column += colspan
        if logical:
            expanded.append([logical[index] for index in range(max(logical) + 1)])
    return expanded


def _main_hof_table(
    tables: Sequence[Sequence[Sequence[Mapping[str, Any]]]],
) -> list[list[Mapping[str, Any]]]:
    """Select only the main NFL inductee table and reject similarly shaped appendices."""

    for table in tables:
        if _is_main_hof_table(table):
            return _expanded_table_rows(table)
    return []


def _is_main_hof_table(
    table: Sequence[Sequence[Mapping[str, Any]]],
) -> bool:
    for row in table:
        headers = [
            " ".join(str(cell.get("text") or "").casefold().split())
            for cell in row
            if cell.get("tag") == "th"
        ]
        if (
            len(headers) >= 5
            and headers[:3] == ["inductee", "class", "position"]
            and any(header.startswith("team") for header in headers)
            and "years" in headers
            and not any(header.startswith("league") for header in headers)
        ):
            return True
    return False


def _parse_hof_table(html_text: str) -> list[dict[str, Any]]:
    parser = _HallOfFameTableParser()
    parser.feed(html_text)
    players: dict[str, dict[str, Any]] = {}
    for cells in _main_hof_table(parser.tables):
        if len(cells) < 3:
            continue
        name = str(cells[0].get("name") or "").strip()
        if not name:
            continue
        raw_position = str(cells[2].get("text") or "").strip()
        comparison_positions = _historical_positions(raw_position)
        position = comparison_positions[0] if comparison_positions else ""
        unresolved_player_role = bool(
            re.search(
                r"\b(?:(?:offensive\s+)?end|defensive\s+back)\b",
                raw_position,
                re.IGNORECASE,
            )
        )
        if not position and not unresolved_player_role:
            continue
        href = next(
            (
                str(link)
                for link, _title in cells[0].get("links", ())
                if str(link or "").startswith("/wiki/")
            ),
            "",
        )
        if not href:
            continue
        page_title = unquote(href.split("/wiki/", 1)[1]).replace("_", " ")
        career_text = str(cells[-1].get("text") or "") if len(cells) >= 5 else ""
        career_years = [int(value) for value in re.findall(r"(?:19|20)\d{2}", career_text)]
        induction_year = (
            int(cells[1]["text"])
            if str(cells[1].get("text") or "").isdigit()
            else None
        )
        key = f"{normalize_name(name)}:{induction_year or ''}"
        existing = players.get(key)
        if existing is not None:
            merged_positions = [
                value
                for value in str(existing.get("comparison_positions") or "").split(";")
                if value
            ]
            for comparison_position in comparison_positions:
                if comparison_position not in merged_positions:
                    merged_positions.append(comparison_position)
            existing["comparison_positions"] = ";".join(merged_positions)
            if not existing.get("position") and comparison_positions:
                existing["position"] = comparison_positions[0]
            raw_roles = [
                value.strip()
                for value in str(existing.get("position_raw") or "").split(";")
                if value.strip()
            ]
            if raw_position and raw_position not in raw_roles:
                raw_roles.append(raw_position)
            existing["position_raw"] = "; ".join(raw_roles)
            starts = [
                value
                for value in (
                    existing.get("nfl_career_start"),
                    min(career_years) if career_years else None,
                )
                if value is not None
            ]
            ends = [
                value
                for value in (
                    existing.get("nfl_career_end"),
                    max(career_years) if career_years else None,
                )
                if value is not None
            ]
            existing["nfl_career_start"] = min(starts) if starts else None
            existing["nfl_career_end"] = max(ends) if ends else None
            continue
        players[key] = {
            "name": name,
            "position": position,
            "comparison_positions": ";".join(comparison_positions),
            "position_raw": raw_position,
            "wikipedia_page": page_title,
            "wikipedia_url": f"https://en.wikipedia.org/wiki/{href.split('/wiki/', 1)[1]}",
            "hof_induction_year": induction_year,
            "nfl_career_start": min(career_years) if career_years else None,
            "nfl_career_end": max(career_years) if career_years else None,
        }
    return list(players.values())


_NONPLAYER_HOF_ROLE = re.compile(
    r"\b(?:coach|owner|founder|organizer|administrator|commissioner|president|"
    r"general manager|personnel|contributor|officials?|scout|advisor|nfl films)\b",
    re.IGNORECASE,
)


def _hof_registry_audit(
    html_text: str,
    profiles: Sequence[Mapping[str, Any]],
    category_payload: Mapping[str, Any],
    *,
    expected_latest_class: int | None = None,
) -> dict[str, Any]:
    """Verify the registry against its table shape, roles, classes, and category."""

    parser = _HallOfFameTableParser()
    parser.feed(html_text)
    matching_tables = [table for table in parser.tables if _is_main_hof_table(table)]
    rows = _expanded_table_rows(matching_tables[0]) if len(matching_tables) == 1 else []
    eligible_keys: set[tuple[str, int]] = set()
    all_table_titles: set[str] = set()
    table_title_years: dict[str, set[int]] = {}
    class_years: set[int] = set()
    class_members: dict[int, set[str]] = {}
    malformed_rows: list[dict[str, Any]] = []
    unknown_roles: list[dict[str, Any]] = []
    nonplayer_keys: set[tuple[str, int]] = set()
    for cells in rows:
        if cells and all(cell.get("tag") == "th" for cell in cells):
            continue
        if len(cells) < 3:
            if any(
                str(cell.get("name") or cell.get("text") or "").strip()
                or cell.get("links")
                for cell in cells
            ):
                malformed_rows.append(
                    {
                        "name": str(cells[0].get("name") or "").strip()
                        if cells
                        else "",
                        "class": "",
                        "position": "",
                        "href": "",
                        "logical_cell_count": len(cells),
                    }
                )
            continue
        name = str(cells[0].get("name") or "").strip()
        class_match = re.search(
            r"\b(?:19|20)\d{2}\b", str(cells[1].get("text") or "")
        )
        role = str(cells[2].get("text") or "").strip()
        href = next(
            (
                str(link)
                for link, _title in cells[0].get("links", ())
                if str(link or "").startswith("/wiki/")
            ),
            "",
        )
        if not any((name, class_match, role, href)):
            continue
        if not (name and class_match and role and href):
            malformed_rows.append(
                {
                    "name": name,
                    "class": class_match.group(0) if class_match else "",
                    "position": role,
                    "href": href,
                }
            )
            continue
        induction_year = int(class_match.group(0))
        title = unquote(href.split("/wiki/", 1)[1]).replace("_", " ")
        title_key = title.casefold()
        key = (title_key, induction_year)
        all_table_titles.add(title)
        class_years.add(induction_year)
        class_members.setdefault(induction_year, set()).add(title_key)
        positions = _historical_positions(role)
        generic_end = bool(
            re.search(r"\b(?:offensive\s+)?end\b", role, re.IGNORECASE)
        )
        generic_defensive_back = bool(
            re.search(r"\bdefensive\s+backs?\b", role, re.IGNORECASE)
        )
        generic_defensive_lineman = bool(
            re.search(r"\bdefensive\s+linem(?:a|e)n\b", role, re.IGNORECASE)
        )
        if (
            positions
            or generic_end
            or generic_defensive_back
            or generic_defensive_lineman
        ):
            eligible_keys.add(key)
        elif _NONPLAYER_HOF_ROLE.search(role):
            nonplayer_keys.add(key)
        else:
            unknown_roles.append(
                {"name": name, "class": induction_year, "position": role}
            )

    profile_keys = {
        (
            str(profile.get("wikipedia_page") or "").casefold(),
            int(parse_number(profile.get("hof_induction_year")) or 0),
        )
        for profile in profiles
        if profile.get("wikipedia_page")
        and parse_number(profile.get("hof_induction_year")) is not None
    }
    latest_required = (
        int(expected_latest_class)
        if expected_latest_class is not None
        else datetime.now(timezone.utc).year - 1
    )
    class_start = min(class_years) if class_years else None
    class_end = max(class_years) if class_years else None
    expected_years = (
        set(range(1963, class_end + 1)) if class_end is not None else set()
    )
    class_counts = {year: len(values) for year, values in class_members.items()}
    class_audit_complete = bool(
        class_start == 1963
        and class_end is not None
        and class_end >= latest_required
        and class_years == expected_years
        and all(3 <= count <= 25 for count in class_counts.values())
    )

    query = category_payload.get("query")
    category_pages = query.get("pages") if isinstance(query, Mapping) else None
    category_titles = {
        str(page.get("title") or "")
        for page in category_pages.values()
        if isinstance(page, Mapping) and page.get("title")
    } if isinstance(category_pages, Mapping) else set()

    def comparable_title(value: str) -> str:
        without_disambiguator = re.sub(r"\s*\([^)]*\)\s*$", "", value)
        return normalize_name(without_disambiguator)

    table_title_keys = {comparable_title(value) for value in all_table_titles}
    for title, year in (
        (title, year)
        for title, year in (
            (key[0], key[1]) for key in eligible_keys | nonplayer_keys
        )
    ):
        comparable = comparable_title(title)
        table_title_years.setdefault(comparable, set()).add(year)
    raw_category_title_keys = {comparable_title(value) for value in category_titles}
    category_title_keys = {
        _HOF_CATEGORY_TITLE_ALIASES.get(value, value)
        for value in raw_category_title_keys
        if value not in _HOF_CATEGORY_NONMEMBER_TITLES
    }
    category_only_unclassified = sorted(category_title_keys - table_title_keys)
    table_only = table_title_keys - category_title_keys
    latest_class_category_lag = sorted(
        value
        for value in table_only
        if class_end is not None and class_end in table_title_years.get(value, set())
    )
    table_only_unclassified = sorted(table_only - set(latest_class_category_lag))
    category_overlap = len(table_title_keys & category_title_keys)
    category_complete = bool(
        category_title_keys
        and "continue" not in category_payload
        and not category_only_unclassified
        and not table_only_unclassified
    )
    registry_complete = bool(
        len(matching_tables) == 1
        and not malformed_rows
        and not unknown_roles
        and profile_keys == eligible_keys
        and class_audit_complete
        and category_complete
    )
    return {
        "registry_complete": registry_complete,
        "matching_main_tables": len(matching_tables),
        "logical_rows": len(rows),
        "eligible_player_rows": len(eligible_keys),
        "nonplayer_rows": len(nonplayer_keys),
        "profile_key_match": profile_keys == eligible_keys,
        "missing_profile_keys": sorted(eligible_keys - profile_keys),
        "unexpected_profile_keys": sorted(profile_keys - eligible_keys),
        "malformed_rows": malformed_rows,
        "unknown_roles": unknown_roles,
        "class_start": class_start,
        "class_end": class_end,
        "class_counts": class_counts,
        "class_audit_complete": class_audit_complete,
        "category_rows": len(category_title_keys),
        "category_overlap_rows": category_overlap,
        "category_only_unclassified": category_only_unclassified,
        "table_only_unclassified": table_only_unclassified,
        "latest_class_category_lag": latest_class_category_lag,
        "excluded_category_nonmembers": sorted(
            raw_category_title_keys & _HOF_CATEGORY_NONMEMBER_TITLES
        ),
        "category_complete": category_complete,
    }


def _json_request(
    url: str,
    destination: Path,
    *,
    refresh: bool,
) -> dict[str, Any]:
    def cached_payload() -> dict[str, Any] | None:
        if not destination.exists() or destination.stat().st_size <= 0:
            return None
        metadata_path = destination.with_suffix(destination.suffix + ".source.json")
        try:
            source_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            source_metadata = {}
        if source_metadata.get("url") != url:
            return None
        try:
            cached = json.loads(
                _read_path_limited(
                    destination,
                    max_bytes=_MAX_JSON_RESPONSE_BYTES,
                    source=f"cached JSON {destination}",
                )
            )
        except (OSError, DataError, json.JSONDecodeError):
            return None
        if isinstance(cached, Mapping) and "error" not in cached:
            return dict(cached)
        return None

    if destination.exists() and destination.stat().st_size > 0 and not refresh:
        cached = cached_payload()
        if cached is not None:
            return cached
    request = Request(
        url,
        headers={
            "User-Agent": (
                "DraftScope/0.1 (local football research; "
                "contact: https://github.com/drewcecala)"
            )
        },
    )
    payload: bytes | None = None
    last_error: Exception | None = None
    for attempt in range(_WIKIMEDIA_MAX_RETRIES + 1):
        if attempt == 0:
            _pace_wikimedia_request(url)
        try:
            with urlopen(request, timeout=90) as response:
                payload = _read_response_limited(
                    response,
                    max_bytes=_MAX_JSON_RESPONSE_BYTES,
                    source=url,
                )
            last_error = None
            break
        except HTTPError as exc:
            retry_after = _retry_after_seconds(exc)
            detail = _http_error_detail(exc)
            retryable = exc.code == 429 or 500 <= exc.code <= 504
            last_error = DataError(
                f"HTTP {exc.code} from {url}"
                + (f": {detail}" if detail else "")
            )
            if retryable and attempt < _WIKIMEDIA_MAX_RETRIES:
                base = 2.0 if exc.code == 429 else 1.0
                delay = (
                    retry_after
                    if retry_after is not None
                    else base * (2**attempt)
                )
                time.sleep(
                    max(0.0, min(_WIKIMEDIA_MAX_RETRY_DELAY_SECONDS, delay))
                )
                continue
            break
        except (URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < _WIKIMEDIA_MAX_RETRIES:
                time.sleep(
                    min(
                        _WIKIMEDIA_MAX_RETRY_DELAY_SECONDS,
                        1.0 * (2**attempt),
                    )
                )
                continue
            break
        except DataError as exc:
            last_error = exc
            break
    if last_error is not None:
        cached = cached_payload()
        if cached is not None:
            _mark_cache_fallback(destination, url=url, error=last_error)
            return cached
        raise DataError(f"Could not download {url}: {last_error}") from last_error
    if payload is None:  # pragma: no cover - defensive invariant
        raise DataError(f"Could not download {url}: no response was returned")
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        cached = cached_payload()
        if cached is not None:
            _mark_cache_fallback(destination, url=url, error=exc)
            return cached
        raise DataError(f"Downloaded JSON was invalid: {url}") from exc
    if not isinstance(parsed, Mapping) or "error" in parsed:
        cached = cached_payload()
        if cached is not None:
            _mark_cache_fallback(
                destination,
                url=url,
                error="downloaded JSON had an unexpected shape",
            )
            return cached
        raise DataError(f"Downloaded JSON had an unexpected shape: {url}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(temp_name, destination)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    destination.with_suffix(destination.suffix + ".source.json").write_text(
        json.dumps(
            {
                "url": url,
                "downloaded_at": timestamp_utc(),
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "cache_fallback": False,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return dict(parsed)


def _wikidata_quantity(value: Mapping[str, Any], *, kind: str) -> float | None:
    amount = parse_number(value.get("value"))
    unit = str(value.get("unit") or "").rsplit("/", 1)[-1]
    if amount is None:
        return None
    if kind == "height":
        if unit == "Q174728":  # centimetre
            return amount / 2.54
        if unit == "Q11573":  # metre
            return amount * 39.37007874
        if unit in {"Q218593", "Q3710"}:  # inch / foot
            return amount if unit == "Q218593" else amount * 12.0
    if kind == "weight":
        if unit == "Q11570":  # kilogram
            return amount * 2.2046226218
        if unit == "Q100995":  # pound
            return amount
    return None


def _wikidata_measurement_resolution(
    bindings: Sequence[Mapping[str, Any]],
    *,
    amount_field: str,
    unit_field: str,
    kind: str,
) -> dict[str, Any]:
    """Select only an unambiguous Wikidata bio value; withhold conflicts."""

    statements: list[dict[str, Any]] = []
    seen_statements: set[tuple[str, str]] = set()
    for binding in bindings:
        amount = binding.get(amount_field)
        unit = binding.get(unit_field)
        if not isinstance(amount, Mapping) or not isinstance(unit, Mapping):
            continue
        raw_amount = str(amount.get("value") or "")
        raw_unit = str(unit.get("value") or "")
        statement_key = (raw_amount, raw_unit)
        if not raw_amount or not raw_unit or statement_key in seen_statements:
            continue
        seen_statements.add(statement_key)
        converted = _wikidata_quantity(
            {"value": raw_amount, "unit": raw_unit}, kind=kind
        )
        if converted is None:
            continue
        statements.append(
            {
                "amount": raw_amount,
                "unit": raw_unit,
                "converted_value": round(converted, 6),
            }
        )
    unique_values = sorted(
        {round(float(statement["converted_value"]), 4) for statement in statements}
    )
    if len(unique_values) == 1:
        return {
            "status": "selected_unique_converted_value",
            "selected_value": unique_values[0],
            "source_statements": statements,
        }
    if len(unique_values) > 1:
        return {
            "status": "conflicting_statements_withheld",
            "selected_value": None,
            "source_statements": statements,
        }
    return {
        "status": "unavailable",
        "selected_value": None,
        "source_statements": [],
    }


def _wikidata_scalar_resolution(
    bindings: Sequence[Mapping[str, Any]],
    *,
    source_field: str,
    kind: str,
) -> dict[str, Any]:
    """Resolve a scalar only when every usable binding agrees after normalization."""

    normalized_to_sources: dict[str, set[str]] = {}
    normalized_to_selected: dict[str, set[str]] = {}
    for binding in bindings:
        value = binding.get(source_field)
        if not isinstance(value, Mapping):
            continue
        raw_value = str(value.get("value") or "").strip()
        if not raw_value:
            continue
        normalized_value = raw_value
        selected_value = raw_value
        if kind == "identifier":
            selected_value = raw_value.rstrip("/").rsplit("/", 1)[-1]
            selected_value = re.sub(r"\.html?$", "", selected_value, flags=re.I)
            normalized_value = selected_value.casefold()
        elif kind == "date":
            date_match = re.fullmatch(
                r"\+?(\d{4}-\d{2}-\d{2})(?:T.*)?", raw_value
            )
            if date_match:
                selected_value = date_match.group(1)
                normalized_value = selected_value
        normalized_to_sources.setdefault(normalized_value, set()).add(raw_value)
        normalized_to_selected.setdefault(normalized_value, set()).add(selected_value)

    normalized_values = sorted(normalized_to_sources)
    source_values = sorted(
        {
            source_value
            for values in normalized_to_sources.values()
            for source_value in values
        }
    )
    if len(normalized_values) == 1:
        selected_values = sorted(normalized_to_selected[normalized_values[0]])
        return {
            "status": "selected_unique_normalized_value",
            "selected_value": selected_values[0],
            "source_values": source_values,
        }
    if len(normalized_values) > 1:
        return {
            "status": "conflicting_values_withheld",
            "selected_value": None,
            "source_values": source_values,
        }
    return {
        "status": "unavailable",
        "selected_value": None,
        "source_values": [],
    }


def _wikidata_position_resolution(
    bindings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Collect every mapped Wikidata position in a stable schema order."""

    source_values: set[str] = set()
    positions: set[str] = set()
    for binding in bindings:
        value = binding.get("positionLabel")
        if not isinstance(value, Mapping):
            continue
        raw_value = " ".join(str(value.get("value") or "").split())
        if not raw_value:
            continue
        source_values.add(raw_value)
        positions.update(
            position
            for position in _historical_positions(raw_value)
            if position in POSITION_GROUPS
        )
    position_order = {position: index for index, position in enumerate(POSITION_GROUPS)}
    return {
        "status": "resolved" if positions else "unavailable",
        "positions": sorted(
            positions,
            key=lambda position: (position_order.get(position, len(position_order)), position),
        ),
        "source_values": sorted(source_values, key=lambda value: (value.casefold(), value)),
    }


def _apply_wikidata_identity_resolution(
    profile: dict[str, Any],
    bindings: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Apply unambiguous Wikidata identity scalars and return conflict disclosures."""

    resolutions = {
        "pfr_id": _wikidata_scalar_resolution(
            bindings, source_field="pfr", kind="identifier"
        ),
        "date_of_birth": _wikidata_scalar_resolution(
            bindings, source_field="dob", kind="date"
        ),
    }
    conflicts: list[dict[str, Any]] = []
    for field, resolution in resolutions.items():
        if not profile.get(field) and resolution.get("selected_value") is not None:
            profile[field] = resolution["selected_value"]
        if resolution.get("status") == "conflicting_values_withheld":
            conflicts.append(
                {
                    "name": profile.get("name"),
                    "wikidata_id": profile.get("wikidata_id"),
                    "field": field,
                    "source_values": resolution.get("source_values"),
                }
            )
    profile["wikidata_identity_provenance"] = json.dumps(
        resolutions, sort_keys=True, separators=(",", ":")
    )
    return conflicts


def _enrich_wikimedia_profiles(
    profiles: Sequence[dict[str, Any]],
    cache: Path,
    *,
    refresh: bool,
    cache_prefix: str,
) -> dict[str, Any]:
    """Resolve linked Wikipedia players to Wikidata identity and bio fields."""

    errors: list[str] = []
    titles = sorted(
        {
            unquote(str(profile.get("wikipedia_page") or ""))
            .replace("_", " ")
            .strip()
            for profile in profiles
            if profile.get("wikipedia_page") and not profile.get("wikidata_id")
        }
    )
    title_to_qid: dict[str, str] = {}
    title_aliases: dict[str, str] = {}
    pageprops_paths: list[Path] = []
    for batch_index in range(0, len(titles), 40):
        batch = titles[batch_index : batch_index + 40]
        query_url = WIKIPEDIA_API_URL + "?" + urlencode(
            {
                "action": "query",
                "titles": "|".join(batch),
                "redirects": 1,
                "prop": "pageprops",
                "ppprop": "wikibase_item",
                "format": "json",
            }
        )
        path = cache / f"{cache_prefix}_pageprops_{batch_index // 40 + 1}.json"
        pageprops_paths.append(path)
        try:
            payload = _json_request(query_url, path, refresh=refresh)
        except (DataError, OSError) as exc:
            errors.append(str(exc))
            continue
        query = payload.get("query")
        if not isinstance(query, Mapping):
            errors.append(f"Wikipedia pageprops batch {batch_index // 40 + 1} was incomplete")
            continue
        for key in ("normalized", "redirects"):
            aliases = query.get(key)
            if not isinstance(aliases, list):
                continue
            for alias in aliases:
                if not isinstance(alias, Mapping):
                    continue
                source = str(alias.get("from") or "").casefold()
                target = str(alias.get("to") or "").casefold()
                if source and target:
                    title_aliases[source] = target
        pages = query.get("pages")
        if not isinstance(pages, Mapping):
            continue
        for page in pages.values():
            if not isinstance(page, Mapping):
                continue
            page_title = str(page.get("title") or "").casefold()
            props = page.get("pageprops")
            qid = str(props.get("wikibase_item") or "") if isinstance(props, Mapping) else ""
            if page_title and qid:
                title_to_qid[page_title] = qid

    def resolved_title(value: object) -> str:
        title = unquote(str(value or "")).replace("_", " ").strip().casefold()
        seen: set[str] = set()
        while title in title_aliases and title not in seen:
            seen.add(title)
            title = title_aliases[title]
        return title

    for profile in profiles:
        if not profile.get("wikidata_id"):
            profile["wikidata_id"] = title_to_qid.get(
                resolved_title(profile.get("wikipedia_page")), ""
            )

    qids = sorted(
        {
            str(profile.get("wikidata_id") or "")
            for profile in profiles
            if str(profile.get("wikidata_id") or "").startswith("Q")
        }
    )
    bindings: list[Mapping[str, Any]] = []
    wikidata_paths: list[Path] = []

    def fetch_wikidata_batch(batch: Sequence[str], token: str) -> None:
        values = " ".join(f"wd:{qid}" for qid in batch)
        query = f"""SELECT ?player ?pfr ?height ?heightUnit ?weight ?weightUnit ?positionLabel ?dob WHERE {{
  VALUES ?player {{ {values} }}
  OPTIONAL {{ ?player wdt:P3561 ?pfr. }}
  OPTIONAL {{ ?player p:P2048/psv:P2048 ?heightNode. ?heightNode wikibase:quantityAmount ?height; wikibase:quantityUnit ?heightUnit. }}
  OPTIONAL {{ ?player p:P2067/psv:P2067 ?weightNode. ?weightNode wikibase:quantityAmount ?weight; wikibase:quantityUnit ?weightUnit. }}
  OPTIONAL {{ ?player wdt:P413 ?position. }}
  OPTIONAL {{ ?player wdt:P569 ?dob. }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language \"en\". }}
}}"""
        sparql_url = WIKIDATA_SPARQL_URL + "?" + urlencode(
            {"format": "json", "query": query}
        )
        path = cache / f"{cache_prefix}_wikidata_{token}.json"
        wikidata_paths.append(path)
        try:
            payload = _json_request(sparql_url, path, refresh=refresh)
        except (DataError, OSError) as exc:
            if len(batch) > 25:
                midpoint = len(batch) // 2
                fetch_wikidata_batch(batch[:midpoint], token + "a")
                fetch_wikidata_batch(batch[midpoint:], token + "b")
                return
            errors.append(str(exc))
            return
        results = payload.get("results")
        raw_bindings = results.get("bindings") if isinstance(results, Mapping) else None
        if not isinstance(raw_bindings, list):
            if len(batch) > 25:
                midpoint = len(batch) // 2
                fetch_wikidata_batch(batch[:midpoint], token + "a")
                fetch_wikidata_batch(batch[midpoint:], token + "b")
                return
            errors.append(f"Wikidata batch {token} was incomplete")
            return
        bindings.extend(
            binding for binding in raw_bindings if isinstance(binding, Mapping)
        )

    for batch_index in range(0, len(qids), 175):
        fetch_wikidata_batch(
            qids[batch_index : batch_index + 175],
            str(batch_index // 175 + 1),
        )

    bindings_by_qid: dict[str, list[Mapping[str, Any]]] = {}
    for binding in bindings:
        player = binding.get("player")
        uri = str(player.get("value") or "") if isinstance(player, Mapping) else ""
        if uri:
            bindings_by_qid.setdefault(uri.rsplit("/", 1)[-1], []).append(binding)
    identity_conflicts: list[dict[str, Any]] = []
    measurement_conflicts: list[dict[str, Any]] = []
    for profile in profiles:
        raw_comparison_positions = profile.get("comparison_positions") or ()
        if isinstance(raw_comparison_positions, set):
            comparison_values = sorted(
                raw_comparison_positions, key=lambda value: str(value)
            )
        elif isinstance(raw_comparison_positions, (list, tuple)):
            comparison_values = list(raw_comparison_positions)
        else:
            comparison_values = re.split(
                r"[;,|/]", str(raw_comparison_positions)
            )
        comparison_positions = [
            normalize_position(value) for value in comparison_values if value
        ]
        source_positions_present = bool(comparison_positions)
        unresolved_position_families = _unresolved_historical_position_families(
            profile.get("position_raw"), comparison_positions
        )
        allowed_refinements = {
            position
            for family in unresolved_position_families.values()
            for position in family
        }
        profile_bindings = bindings_by_qid.get(
            str(profile.get("wikidata_id") or ""), ()
        )
        identity_conflicts.extend(
            _apply_wikidata_identity_resolution(profile, profile_bindings)
        )
        position_resolution = _wikidata_position_resolution(profile_bindings)
        profile["wikidata_position_provenance"] = json.dumps(
            position_resolution, sort_keys=True, separators=(",", ":")
        )
        for position in position_resolution["positions"]:
            if (
                not source_positions_present
                or position in allowed_refinements
            ) and position not in comparison_positions:
                comparison_positions.append(position)
                existing_source = str(profile.get("position_source") or "").strip()
                if not existing_source:
                    profile["position_source"] = (
                        "https://www.wikidata.org/wiki/Property:P413"
                    )
                existing_note = str(
                    profile.get("position_mapping_note") or ""
                ).strip()
                note = (
                    "Wikidata playing-position statement refined an "
                    "unresolved historical position family"
                    if source_positions_present
                    else "Wikidata playing-position statement supplied "
                    "a missing historical position"
                )
                if note not in existing_note:
                    profile["position_mapping_note"] = "; ".join(
                        value for value in (existing_note, note) if value
                    )
        comparison_positions = _apply_historical_honors_position_override(
            profile, comparison_positions
        )
        measurement_resolution = {
            "height": _wikidata_measurement_resolution(
                profile_bindings,
                amount_field="height",
                unit_field="heightUnit",
                kind="height",
            ),
            "weight": _wikidata_measurement_resolution(
                profile_bindings,
                amount_field="weight",
                unit_field="weightUnit",
                kind="weight",
            ),
        }
        if profile.get("height_in") is None:
            profile["height_in"] = measurement_resolution["height"].get(
                "selected_value"
            )
        if profile.get("weight_lb") is None:
            profile["weight_lb"] = measurement_resolution["weight"].get(
                "selected_value"
            )
        profile["wikidata_measurement_provenance"] = json.dumps(
            measurement_resolution, sort_keys=True, separators=(",", ":")
        )
        for field, resolution in measurement_resolution.items():
            if resolution.get("status") == "conflicting_statements_withheld":
                measurement_conflicts.append(
                    {
                        "name": profile.get("name"),
                        "wikidata_id": profile.get("wikidata_id"),
                        "field": field,
                        "source_statements": resolution.get("source_statements"),
                    }
                )
        comparison_positions = [
            position
            for position in dict.fromkeys(comparison_positions)
            if position in POSITION_GROUPS
        ]
        if not profile.get("position") and comparison_positions:
            profile["position"] = comparison_positions[0]
        profile["comparison_positions"] = ";".join(comparison_positions)
        remaining_families = _unresolved_historical_position_families(
            profile.get("position_raw"), comparison_positions
        )
        profile["unresolved_position_families"] = ";".join(
            sorted(remaining_families)
        )

    artifacts = [
        metadata
        for path in (*pageprops_paths, *wikidata_paths)
        if (metadata := _artifact_source_metadata(path))
    ]
    stale = [artifact for artifact in artifacts if artifact.get("cache_fallback") is True]
    return {
        "status": "partial" if errors or stale else "pass",
        "rows": len(profiles),
        "wikidata_matches": sum(bool(row.get("wikidata_id")) for row in profiles),
        "pfr_id_matches": sum(bool(row.get("pfr_id")) for row in profiles),
        "measurement_rows": sum(
            parse_number(row.get("height_in")) is not None
            and parse_number(row.get("weight_lb")) is not None
            for row in profiles
        ),
        "identity_conflicts_withheld": identity_conflicts,
        "measurement_conflicts_withheld": measurement_conflicts,
        "unresolved_position_family_rows": [
            {
                "name": row.get("name"),
                "wikidata_id": row.get("wikidata_id"),
                "position_raw": row.get("position_raw"),
                "unresolved_position_families": row.get(
                    "unresolved_position_families"
                ),
            }
            for row in profiles
            if row.get("unresolved_position_families")
        ],
        "errors": errors,
        "source_artifacts": artifacts,
        "source_refresh_stale": bool(stale),
    }


def _load_wikimedia_hof_profiles(
    cache: Path,
    *,
    refresh: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load all-era HOF player roles through licensed Wikimedia APIs."""

    parse_url = WIKIPEDIA_API_URL + "?" + urlencode(
        {
            "action": "parse",
            "page": WIKIPEDIA_HOF_LIST_PAGE,
            "prop": "text",
            "format": "json",
        }
    )
    category_url = WIKIPEDIA_API_URL + "?" + urlencode(
        {
            "action": "query",
            "generator": "categorymembers",
            "gcmtitle": WIKIPEDIA_HOF_CATEGORY,
            "gcmlimit": 500,
            "gcmnamespace": 0,
            "prop": "pageprops",
            "ppprop": "wikibase_item",
            "format": "json",
        }
    )
    parsed_page = _json_request(
        parse_url, cache / "wikipedia_pfhof_list.json", refresh=refresh
    )
    try:
        html_text = str(parsed_page["parse"]["text"]["*"])
    except (KeyError, TypeError) as exc:
        raise DataError("Wikipedia Hall of Fame list response was incomplete") from exc
    profiles = _parse_hof_table(html_text)
    enrichment_errors: list[str] = []
    pages: Mapping[str, Any] = {}
    category_payload: Mapping[str, Any] = {}
    try:
        category = _json_request(
            category_url, cache / "wikipedia_pfhof_category.json", refresh=refresh
        )
        category_payload = category
        query_payload = category.get("query")
        raw_pages = (
            query_payload.get("pages") if isinstance(query_payload, Mapping) else None
        )
        if not isinstance(raw_pages, Mapping):
            raise DataError("Wikipedia Hall of Fame category response was incomplete")
        pages = raw_pages
    except (DataError, OSError) as exc:
        enrichment_errors.append(str(exc))
    title_to_qid: dict[str, str] = {}
    for page in pages.values():
        if not isinstance(page, Mapping):
            continue
        title = str(page.get("title") or "").casefold()
        props = page.get("pageprops")
        qid = str(props.get("wikibase_item") or "") if isinstance(props, Mapping) else ""
        if title and qid:
            title_to_qid[title] = qid
    qids: list[str] = []
    for profile in profiles:
        qid = title_to_qid.get(str(profile.get("wikipedia_page") or "").casefold())
        profile["wikidata_id"] = qid
        if qid:
            qids.append(qid)

    bindings: list[Mapping[str, Any]] = []
    unique_qids = sorted(set(qids))
    for batch_index in range(0, len(unique_qids), 175):
        batch = unique_qids[batch_index : batch_index + 175]
        values = " ".join(f"wd:{qid}" for qid in batch)
        query = f"""SELECT ?player ?pfr ?height ?heightUnit ?weight ?weightUnit ?positionLabel ?dob WHERE {{
  VALUES ?player {{ {values} }}
  OPTIONAL {{ ?player wdt:P3561 ?pfr. }}
  OPTIONAL {{ ?player p:P2048/psv:P2048 ?heightNode. ?heightNode wikibase:quantityAmount ?height; wikibase:quantityUnit ?heightUnit. }}
  OPTIONAL {{ ?player p:P2067/psv:P2067 ?weightNode. ?weightNode wikibase:quantityAmount ?weight; wikibase:quantityUnit ?weightUnit. }}
  OPTIONAL {{ ?player wdt:P413 ?position. }}
  OPTIONAL {{ ?player wdt:P569 ?dob. }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language \"en\". }}
}}"""
        sparql_url = WIKIDATA_SPARQL_URL + "?" + urlencode(
            {"format": "json", "query": query}
        )
        try:
            sparql = _json_request(
                sparql_url,
                cache / f"wikidata_pfhof_profiles_{batch_index // 175 + 1}.json",
                refresh=refresh,
            )
        except (DataError, OSError) as exc:
            enrichment_errors.append(str(exc))
            continue
        results = sparql.get("results")
        raw_bindings = results.get("bindings") if isinstance(results, Mapping) else None
        if isinstance(raw_bindings, list):
            bindings.extend(
                item for item in raw_bindings if isinstance(item, Mapping)
            )

    by_qid: dict[str, list[Mapping[str, Any]]] = {}
    for binding in bindings:
        player = binding.get("player")
        uri = str(player.get("value") or "") if isinstance(player, Mapping) else ""
        if uri:
            by_qid.setdefault(uri.rsplit("/", 1)[-1], []).append(binding)
    identity_conflicts: list[dict[str, Any]] = []
    measurement_conflicts: list[dict[str, Any]] = []
    for profile in profiles:
        rows = by_qid.get(str(profile.get("wikidata_id") or ""), ())
        identity_conflicts.extend(_apply_wikidata_identity_resolution(profile, rows))
        position_resolution = _wikidata_position_resolution(rows)
        profile["wikidata_position"] = ";".join(position_resolution["positions"])
        profile["wikidata_position_provenance"] = json.dumps(
            position_resolution, sort_keys=True, separators=(",", ":")
        )
        measurement_resolution = {
            "height": _wikidata_measurement_resolution(
                rows,
                amount_field="height",
                unit_field="heightUnit",
                kind="height",
            ),
            "weight": _wikidata_measurement_resolution(
                rows,
                amount_field="weight",
                unit_field="weightUnit",
                kind="weight",
            ),
        }
        if profile.get("height_in") is None:
            profile["height_in"] = measurement_resolution["height"].get(
                "selected_value"
            )
        if profile.get("weight_lb") is None:
            profile["weight_lb"] = measurement_resolution["weight"].get(
                "selected_value"
            )
        profile["wikidata_measurement_provenance"] = json.dumps(
            measurement_resolution, sort_keys=True, separators=(",", ":")
        )
        for field, resolution in measurement_resolution.items():
            if resolution.get("status") == "conflicting_statements_withheld":
                measurement_conflicts.append(
                    {
                        "name": profile.get("name"),
                        "wikidata_id": profile.get("wikidata_id"),
                        "field": field,
                        "source_statements": resolution.get("source_statements"),
                    }
                )
        raw_role = str(profile.get("position_raw") or "").strip()
        listed_positions = list(_historical_positions(raw_role))
        generic_end_text = re.sub(
            r"\b(?:defensive|offensive|tight|split)\s+ends?\b",
            " ",
            raw_role,
            flags=re.IGNORECASE,
        )
        generic_end_role = bool(
            re.search(r"\bends?\b", generic_end_text, re.IGNORECASE)
        )
        if generic_end_role:
            if "pre-modern era" in raw_role.casefold():
                # Before platoon football, an HOF-listed End explicitly played
                # both sides. Index that source role under both modern families.
                listed_positions.extend(("WR", "EDGE"))
                profile["position_mapping_note"] = (
                    "HOF-listed pre-modern two-way End indexed under WR and EDGE"
                )
        unresolved_families = _unresolved_historical_position_families(
            raw_role, listed_positions
        )
        allowed_refinements = {
            position
            for family in unresolved_families.values()
            for position in family
        }
        resolved_positions = [
            position
            for position in position_resolution["positions"]
            if position in allowed_refinements
        ]
        if resolved_positions:
            for resolved_position in resolved_positions:
                if resolved_position not in listed_positions:
                    listed_positions.append(resolved_position)
            if not profile.get("position"):
                profile["position"] = resolved_positions[0]
            profile["position_source"] = (
                "https://www.wikidata.org/wiki/Property:P413"
            )
            existing_note = str(profile.get("position_mapping_note") or "").strip()
            refinement_note = (
                "Wikidata playing-position statement refined an unresolved "
                "Hall of Fame position family"
            )
            if refinement_note not in existing_note:
                profile["position_mapping_note"] = "; ".join(
                    value
                    for value in (existing_note, refinement_note)
                    if value
                )
        if generic_end_role and not (
            {"WR", "TE", "EDGE"} & set(listed_positions)
        ):
            listed_positions.append("WR")
            profile["position_mapping_note"] = (
                "HOF-listed End indexed under the modern receiving-end family"
            )
        listed_positions = _apply_hof_position_override(profile, listed_positions)
        normalized_positions: list[str] = []
        for listed in listed_positions:
            normalized = normalize_position(listed)
            if normalized in POSITION_GROUPS and normalized not in normalized_positions:
                normalized_positions.append(normalized)
        if profile.get("position") and profile["position"] not in normalized_positions:
            normalized_positions.insert(0, str(profile["position"]))
        if not profile.get("position") and normalized_positions:
            profile["position"] = normalized_positions[0]
        profile["comparison_positions"] = ";".join(normalized_positions)
        remaining_families = _unresolved_historical_position_families(
            raw_role, normalized_positions
        )
        profile["unresolved_position_families"] = ";".join(
            sorted(remaining_families)
        )
    source_paths = [
        cache / "wikipedia_pfhof_list.json",
        cache / "wikipedia_pfhof_category.json",
        *sorted(cache.glob("wikidata_pfhof_profiles_*.json")),
    ]
    source_artifacts = [
        metadata
        for path in source_paths
        if (metadata := _artifact_source_metadata(path))
    ]
    stale_source_artifacts = [
        artifact for artifact in source_artifacts if artifact.get("cache_fallback") is True
    ]
    registry_audit = _hof_registry_audit(
        html_text,
        profiles,
        category_payload,
    )
    metadata = {
        "status": (
            "partial"
            if enrichment_errors
            or stale_source_artifacts
            or not registry_audit.get("registry_complete")
            else "pass"
        ),
        "source": "Wikipedia Hall of Fame inductee list plus Wikidata measurements",
        "wikipedia_page": f"https://en.wikipedia.org/wiki/{WIKIPEDIA_HOF_LIST_PAGE.replace(' ', '_')}",
        "wikidata_endpoint": WIKIDATA_SPARQL_URL,
        "licenses": {
            "wikipedia": {
                "name": "CC BY-SA 4.0",
                "url": "https://creativecommons.org/licenses/by-sa/4.0/",
            },
            "wikidata": {
                "name": "CC0 1.0",
                "url": "https://creativecommons.org/publicdomain/zero/1.0/",
            },
        },
        "changes_made": (
            "Inductee identities, positions, measurements, and source records were "
            "normalized and merged into DraftScope's comparison schema."
        ),
        "source_artifacts": source_artifacts,
        "source_refresh_stale": bool(stale_source_artifacts),
        "stale_source_artifacts": stale_source_artifacts,
        "rows": len(profiles),
        "registry_complete": bool(registry_audit.get("registry_complete")),
        "registry_audit": registry_audit,
        "wikidata_matches": sum(bool(row.get("wikidata_id")) for row in profiles),
        "measurement_rows": sum(
            parse_number(row.get("height_in")) is not None
            and parse_number(row.get("weight_lb")) is not None
            for row in profiles
        ),
        "identity_conflicts_withheld": identity_conflicts,
        "measurement_conflicts_withheld": measurement_conflicts,
        "unresolved_position_rows": sum(
            not str(row.get("position") or "").strip() for row in profiles
        ),
        "unresolved_position_family_rows": [
            {
                "name": row.get("name"),
                "wikidata_id": row.get("wikidata_id"),
                "position_raw": row.get("position_raw"),
                "unresolved_position_families": row.get(
                    "unresolved_position_families"
                ),
            }
            for row in profiles
            if row.get("unresolved_position_families")
        ],
        "enrichment_errors": enrichment_errors,
    }
    return profiles, metadata


def _draft_match_maps(draft_rows: Iterable[Mapping[str, str]]) -> tuple[dict[Any, Mapping[str, str]], ...]:
    by_pfr: dict[tuple[str, str], Mapping[str, str]] = {}
    by_cfb: dict[tuple[str, str], Mapping[str, str]] = {}
    by_name_school: dict[tuple[str, str, str], Mapping[str, str]] = {}
    by_name: dict[tuple[str, str], list[Mapping[str, str]]] = {}
    for row in draft_rows:
        year = str(row.get("season") or "")
        pfr = str(row.get("pfr_player_id") or "").strip()
        cfb = str(row.get("cfb_player_id") or "").strip()
        name = normalize_name(row.get("pfr_player_name"))
        school = normalize_name(row.get("college"))
        if pfr:
            by_pfr[(year, pfr)] = row
        if cfb:
            by_cfb[(year, cfb)] = row
        by_name_school[(year, name, school)] = row
        by_name.setdefault((year, name), []).append(row)
    return by_pfr, by_cfb, by_name_school, by_name


def _match_draft_row(
    combine: Mapping[str, str], maps: tuple[dict[Any, Mapping[str, str]], ...]
) -> tuple[Mapping[str, str] | None, str]:
    by_pfr, by_cfb, by_name_school, by_name = maps
    year = str(combine.get("season") or "")
    pfr = str(combine.get("pfr_id") or "").strip()
    cfb = str(combine.get("cfb_id") or "").strip()
    name = normalize_name(combine.get("player_name"))
    school = normalize_name(combine.get("school"))
    if pfr and (year, pfr) in by_pfr:
        return by_pfr[(year, pfr)], "pfr_id"
    if cfb and (year, cfb) in by_cfb:
        return by_cfb[(year, cfb)], "cfb_id"
    if (year, name, school) in by_name_school:
        return by_name_school[(year, name, school)], "name_school"
    candidates = by_name.get((year, name), [])
    if len(candidates) == 1:
        return candidates[0], "unique_name"
    return None, "unmatched"


def _normalized_pfr_id(value: object) -> str:
    return str(value or "").strip().rsplit("/", 1)[-1].casefold()


def _career_elite_identity_maps(
    rows: Iterable[Mapping[str, Any]],
    *,
    pfr_field: str,
    name_field: str,
) -> tuple[dict[str, Mapping[str, Any]], dict[str, list[Mapping[str, Any]]]]:
    by_pfr: dict[str, Mapping[str, Any]] = {}
    by_name: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        pfr_id = _normalized_pfr_id(row.get(pfr_field))
        name = normalize_name(row.get(name_field))
        if pfr_id:
            by_pfr[pfr_id] = row
        if name:
            by_name.setdefault(name, []).append(row)
    return by_pfr, by_name


def _unique_name_match(
    rows: Mapping[str, list[Mapping[str, Any]]], name: object
) -> Mapping[str, Any] | None:
    matches = rows.get(normalize_name(name), ())
    return matches[0] if len(matches) == 1 else None


def _specific_historical_position(
    *values: object,
    fallback: object = "",
) -> str:
    """Prefer a specific identity-linked role over old generic draft labels."""

    generic = {"", "OL", "DL", "DB", "LB", "B", "E"}
    for value in values:
        raw = str(value or "").upper().strip()
        position = normalize_position(raw)
        if raw not in generic and position in POSITION_GROUPS:
            return position
    for value in values:
        if str(value or "").upper().strip() == "DB":
            continue
        position = normalize_position(value)
        if position in POSITION_GROUPS:
            return position
    historical = _historical_position(fallback)
    return historical if historical in POSITION_GROUPS else ""


def _build_nfl_career_elite_rows(
    combine_rows: Sequence[Mapping[str, Any]],
    draft_rows: Sequence[Mapping[str, Any]],
    player_rows: Sequence[Mapping[str, Any]],
    hof_profiles: Sequence[Mapping[str, Any]] = (),
    historical_honors_profiles: Sequence[Mapping[str, Any]] = (),
    *,
    end_year: int | None = None,
    all_era_hof_complete: bool | None = None,
    historical_honors_metadata: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build a comparison-only catalog; outcome fields never enter a risk set."""

    selected_draft = [
        row
        for row in draft_rows
        if end_year is None or int(parse_number(row.get("season")) or 0) <= end_year
    ]
    combine_by_pfr, combine_by_name = _career_elite_identity_maps(
        combine_rows, pfr_field="pfr_id", name_field="player_name"
    )
    player_by_pfr, player_by_name = _career_elite_identity_maps(
        player_rows, pfr_field="pfr_id", name_field="display_name"
    )
    draft_by_pfr, draft_by_name = _career_elite_identity_maps(
        selected_draft, pfr_field="pfr_player_id", name_field="pfr_player_name"
    )

    def matches_for(
        *, pfr_id: object, name: object, allow_name_fallback: bool = True
    ) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None, Mapping[str, Any] | None]:
        stable = _normalized_pfr_id(pfr_id)
        if stable:
            return (
                combine_by_pfr.get(stable),
                player_by_pfr.get(stable),
                draft_by_pfr.get(stable),
            )
        if not allow_name_fallback:
            return None, None, None
        return (
            _unique_name_match(combine_by_name, name),
            _unique_name_match(player_by_name, name),
            _unique_name_match(draft_by_name, name),
        )

    def base_row(
        draft: Mapping[str, Any] | None,
        *,
        name: object,
        pfr_id: object,
        hof: Mapping[str, Any] | None = None,
        hof_designation: bool = True,
        allow_name_fallback: bool = True,
    ) -> dict[str, Any]:
        combine, player, matched_draft = matches_for(
            pfr_id=pfr_id,
            name=name,
            allow_name_fallback=allow_name_fallback,
        )
        draft = draft or matched_draft
        raw_player_position = player.get("position") if player else None
        raw_player_pff_position = player.get("pff_position") if player else None
        raw_player_ngs_position = player.get("ngs_position") if player else None
        raw_combine_position = combine.get("pos") if combine else None
        raw_draft_position = draft.get("position") if draft else None
        position = _specific_historical_position(
            raw_player_pff_position,
            raw_player_ngs_position,
            raw_player_position,
            raw_combine_position,
            raw_draft_position,
            (hof or {}).get("position"),
            fallback=(hof or {}).get("position_raw"),
        )
        combine_year = int(parse_number(combine.get("season")) or 0) if combine else 0
        draft_year = int(parse_number(draft.get("season")) or 0) if draft else 0
        player_draft_year = int(parse_number(player.get("draft_year")) or 0) if player else 0
        combine_height = combine.get("ht") if combine else None
        combine_weight = combine.get("wt") if combine else None
        player_height = player.get("height") if player else None
        player_weight = player.get("weight") if player else None
        hof_height = (hof or {}).get("height_in")
        hof_weight = (hof or {}).get("weight_lb")

        def first_valid_measurement(
            candidates: Sequence[tuple[object, str]],
            parser: Callable[[object], float | None],
        ) -> tuple[object | None, str, float | None]:
            for value, source in candidates:
                parsed = parser(value)
                if parsed is not None:
                    return value, source, parsed
            return None, "unavailable", None

        selected_height, height_source, parsed_height = first_valid_measurement(
            (
                (combine_height, "nfl_combine"),
                (player_height, "nflverse_player_registry"),
                (hof_height, "wikidata_biographical_measurement"),
            ),
            parse_height,
        )
        selected_weight, weight_source, parsed_weight = first_valid_measurement(
            (
                (combine_weight, "nfl_combine"),
                (player_weight, "nflverse_player_registry"),
                (hof_weight, "wikidata_biographical_measurement"),
            ),
            parse_number,
        )
        has_combine_measurements = bool(
            combine
            and any(
                (
                    parse_height(combine.get(field))
                    if field == "ht"
                    else parse_number(combine.get(field))
                )
                is not None
                for field in (
                    "ht",
                    "wt",
                    "forty",
                    "bench",
                    "vertical",
                    "broad_jump",
                    "cone",
                    "shuttle",
                )
            )
        )
        biographical_fallback = bool(
            has_combine_measurements
            and (
                (parsed_height is not None and height_source != "nfl_combine")
                or (parsed_weight is not None and weight_source != "nfl_combine")
            )
        )
        if has_combine_measurements:
            measurement_source = (
                "pro_day_nonstandard" if combine_year == 2021 else "nfl_combine"
            )
            if biographical_fallback:
                measurement_source += "_with_biographical_fallback"
            measurements_verified = not biographical_fallback
        elif "nflverse_player_registry" in {height_source, weight_source}:
            measurement_source = "nflverse_player_registry"
            if "wikidata_biographical_measurement" in {
                height_source,
                weight_source,
            }:
                measurement_source += "_with_wikidata_fallback"
            measurements_verified = False
        elif "wikidata_biographical_measurement" in {
            height_source,
            weight_source,
        }:
            measurement_source = "wikidata_biographical_measurement"
            measurements_verified = False
        else:
            measurement_source = "unavailable"
            measurements_verified = False
        field_measurement_provenance: dict[str, Any] = {
            "height_in": {
                "source": height_source,
                "selected_value": parsed_height,
            },
            "weight_lb": {
                "source": weight_source,
                "selected_value": parsed_weight,
            },
        }
        if has_combine_measurements:
            field_measurement_provenance["testing"] = {
                "source": (
                    "pro_day_nonstandard" if combine_year == 2021 else "nfl_combine"
                ),
                "season": combine_year or None,
            }
        career = _nfl_career_outcome_fields(draft)
        if hof is not None and hof_designation:
            career["nfl_hof"] = True
            career["nfl_career_elite"] = True
        pick = parse_number(draft.get("pick")) if draft else None
        stable_id = (
            (player.get("pfr_id") if player else None)
            or (draft.get("pfr_player_id") if draft else None)
            or pfr_id
            or (hof or {}).get("wikidata_id")
            or f"career-elite:{normalize_name(name)}"
        )
        comparison_positions: list[str] = []
        for raw in (
            position,
            (hof or {}).get("comparison_positions"),
        ):
            values = raw if isinstance(raw, (list, tuple, set)) else re.split(r"[;,|/]", str(raw or ""))
            for value in values:
                normalized = normalize_position(value)
                if normalized in POSITION_GROUPS and normalized not in comparison_positions:
                    comparison_positions.append(normalized)
        row = {
            "player_id": stable_id,
            "pfr_id": (player.get("pfr_id") if player else None)
            or (draft.get("pfr_player_id") if draft else None)
            or pfr_id,
            "gsis_id": (player.get("gsis_id") if player else None)
            or (draft.get("gsis_id") if draft else None),
            "name": (player.get("display_name") if player else None)
            or (draft.get("pfr_player_name") if draft else None)
            or name,
            "position_raw": raw_player_position
            or raw_player_pff_position
            or raw_player_ngs_position
            or raw_combine_position
            or raw_draft_position
            or (hof or {}).get("position_raw"),
            "position": position,
            "comparison_positions": ";".join(comparison_positions),
            "position_mapping_note": (hof or {}).get("position_mapping_note"),
            "position_source": (hof or {}).get("position_source"),
            "school": (draft.get("college") if draft else None)
            or (player.get("college_name") if player else None)
            or (combine.get("school") if combine else None),
            "season": draft_year or player_draft_year or combine_year or None,
            "draft_year": draft_year or player_draft_year or combine_year or None,
            "height_in": selected_height,
            "weight_lb": selected_weight,
            "forty_s": combine.get("forty") if combine else None,
            "bench_reps": combine.get("bench") if combine else None,
            "vertical_in": combine.get("vertical") if combine else None,
            "broad_jump_in": combine.get("broad_jump") if combine else None,
            "three_cone_s": combine.get("cone") if combine else None,
            "shuttle_s": combine.get("shuttle") if combine else None,
            "age_at_draft": draft.get("age") if draft else None,
            "date_of_birth": (player.get("birth_date") if player else None)
            or (hof or {}).get("date_of_birth"),
            "drafted": True if draft else None,
            "draft_round": draft.get("round") if draft else None,
            "draft_ovr": pick,
            "nfl_team": draft.get("team") if draft else None,
            "elite": bool(pick is not None and pick <= 64),
            **career,
            "reference_only": True,
            "career_elite_comparison_eligible": True,
            "population": "career-elite NFL comparison references",
            "measurement_source": measurement_source,
            "measurements_verified": measurements_verified,
            "measurement_provenance": json.dumps(
                field_measurement_provenance,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "nfl_career_start": (hof or {}).get("nfl_career_start"),
            "nfl_career_end": (hof or {}).get("nfl_career_end"),
            "hof_induction_year": (hof or {}).get("hof_induction_year"),
            "hof_source": (hof or {}).get("wikipedia_url"),
            "wikipedia_page": (hof or {}).get("wikipedia_page"),
            "wikidata_id": (hof or {}).get("wikidata_id"),
        }
        return normalize_record(row)

    catalog: list[dict[str, Any]] = []
    by_pfr: dict[str, int] = {}
    by_name: dict[str, list[int]] = {}
    by_wikipedia_page: dict[str, int] = {}

    def register(index: int, row: Mapping[str, Any]) -> None:
        stable = _normalized_pfr_id(row.get("pfr_id"))
        if stable:
            by_pfr[stable] = index
        name_key = normalize_name(row.get("name"))
        if name_key:
            indexes = by_name.setdefault(name_key, [])
            if index not in indexes:
                indexes.append(index)
        page_key = str(row.get("wikipedia_page") or "").strip().casefold()
        if page_key:
            by_wikipedia_page[page_key] = index

    def unique_name_index(name: object) -> int | None:
        indexes = by_name.get(normalize_name(name), ())
        return indexes[0] if len(indexes) == 1 else None

    for draft in selected_draft:
        career = _nfl_career_outcome_fields(draft)
        if career.get("nfl_career_elite") is not True:
            continue
        row = base_row(
            draft,
            name=draft.get("pfr_player_name"),
            pfr_id=draft.get("pfr_player_id"),
        )
        if row.get("position") not in POSITION_GROUPS:
            continue
        index = len(catalog)
        catalog.append(row)
        register(index, row)

    hof_added = 0
    hof_merged = 0
    hof_unpositioned_candidates: list[dict[str, Any]] = []
    for hof in hof_profiles:
        stable = _normalized_pfr_id(hof.get("pfr_id"))
        index = by_pfr.get(stable) if stable else None
        if index is None and not stable:
            page_key = str(hof.get("wikipedia_page") or "").strip().casefold()
            index = by_wikipedia_page.get(page_key) if page_key else None
        if index is None and not stable:
            index = unique_name_index(hof.get("name"))
        if index is not None:
            replacement = base_row(
                draft_by_pfr.get(stable) if stable else None,
                name=hof.get("name"),
                pfr_id=hof.get("pfr_id"),
                hof=hof,
            )
            original = catalog[index]
            catalog[index] = {
                **original,
                **{
                    key: value
                    for key, value in replacement.items()
                    if value is not None and value != ""
                },
                "nfl_hof": True,
                "nfl_career_elite": True,
            }
            register(index, catalog[index])
            hof_merged += 1
            continue
        row = base_row(
            None,
            name=hof.get("name"),
            pfr_id=hof.get("pfr_id"),
            hof=hof,
        )
        if row.get("position") not in POSITION_GROUPS:
            hof_unpositioned_candidates.append(
                {
                    "name": hof.get("name"),
                    "pfr_id": hof.get("pfr_id"),
                    "wikidata_id": hof.get("wikidata_id"),
                    "wikipedia_page": hof.get("wikipedia_page"),
                    "position_raw": hof.get("position_raw"),
                }
            )
            continue
        index = len(catalog)
        catalog.append(row)
        register(index, row)
        hof_added += 1

    honors_added = 0
    honors_merged = 0
    honors_covered_disagreements: list[dict[str, Any]] = []
    honors_unpositioned_rows: list[dict[str, Any]] = []

    def maximum_known_count(*values: object) -> float | None:
        counts = [parsed for value in values if (parsed := parse_number(value)) is not None]
        return max(counts) if counts else None

    for honors in historical_honors_profiles:
        overlay_all_pro = parse_number(honors.get("nfl_all_pro_selections"))
        overlay_pro_bowls = parse_number(honors.get("nfl_pro_bowls"))
        if not (
            (overlay_all_pro is not None and overlay_all_pro >= 1)
            or (overlay_pro_bowls is not None and overlay_pro_bowls >= 3)
        ):
            continue
        stable = _normalized_pfr_id(honors.get("pfr_id"))
        matched_draft = draft_by_pfr.get(stable) if stable else None
        draft_career = _nfl_career_outcome_fields(matched_draft)
        index = by_pfr.get(stable) if stable else None
        page_key = str(honors.get("wikipedia_page") or "").strip().casefold()
        if index is None and page_key:
            index = by_wikipedia_page.get(page_key)
        if index is None and not stable:
            index = unique_name_index(honors.get("name"))
        replacement = base_row(
            matched_draft,
            name=honors.get("name"),
            pfr_id=honors.get("pfr_id"),
            hof=honors,
            hof_designation=False,
            allow_name_fallback=False,
        )
        if replacement.get("position") not in POSITION_GROUPS:
            original_position = (
                str(catalog[index].get("position") or "")
                if index is not None
                else ""
            )
            if original_position in POSITION_GROUPS:
                replacement["position"] = original_position
                replacement["comparison_positions"] = catalog[index].get(
                    "comparison_positions"
                ) or original_position
            else:
                honors_unpositioned_rows.append(
                    {
                        "name": honors.get("name"),
                        "pfr_id": honors.get("pfr_id"),
                        "wikidata_id": honors.get("wikidata_id"),
                        "wikipedia_page": honors.get("wikipedia_page"),
                        "position_raw": honors.get("position_raw"),
                        "nfl_all_pro_selections": overlay_all_pro,
                        "nfl_pro_bowls": overlay_pro_bowls,
                    }
                )
                continue
        honor_fields = {
            "nfl_all_pro_selections": overlay_all_pro,
            "nfl_pro_bowls": overlay_pro_bowls,
            "nfl_career_elite": True,
            "nfl_honors_source": "wikimedia_historical_honors",
            "nfl_first_team_all_pro_years": ",".join(
                str(value)
                for value in honors.get("nfl_first_team_all_pro_years") or ()
            ),
            "nfl_pro_bowl_years": ",".join(
                str(value) for value in honors.get("nfl_pro_bowl_years") or ()
            ),
            "nfl_first_team_all_pro_source_urls": ";".join(
                str(value)
                for value in honors.get("nfl_first_team_all_pro_source_urls") or ()
            ),
            "nfl_pro_bowl_source_urls": ";".join(
                str(value)
                for value in honors.get("nfl_pro_bowl_source_urls") or ()
            ),
        }
        original = catalog[index] if index is not None else draft_career
        original_all_pro = parse_number(original.get("nfl_all_pro_selections"))
        original_pro_bowls = parse_number(original.get("nfl_pro_bowls"))
        honor_fields["nfl_all_pro_selections"] = maximum_known_count(
            original_all_pro, overlay_all_pro
        )
        honor_fields["nfl_pro_bowls"] = maximum_known_count(
            original_pro_bowls, overlay_pro_bowls
        )
        if matched_draft is not None or index is not None:
            honor_fields["nfl_honors_source"] = (
                "nflverse_and_wikimedia_historical_honors_reconciled"
            )
        count_conflict = any(
            left is not None and right is not None and left != right
            for left, right in (
                (original_all_pro, overlay_all_pro),
                (original_pro_bowls, overlay_pro_bowls),
            )
        )
        if count_conflict:
            honors_covered_disagreements.append(
                {
                    "name": honors.get("name"),
                    "pfr_id": honors.get("pfr_id"),
                    "nflverse_all_pro": original_all_pro,
                    "wikimedia_all_pro": overlay_all_pro,
                    "nflverse_pro_bowls": original_pro_bowls,
                    "wikimedia_pro_bowls": overlay_pro_bowls,
                    "resolution": (
                        "maximum sourced count retained; qualifying evidence included"
                    ),
                }
            )
        if index is not None:
            merged_positions: list[str] = []
            for raw_positions in (
                original.get("comparison_positions"),
                replacement.get("comparison_positions"),
            ):
                for value in re.split(r"[;,|/]", str(raw_positions or "")):
                    position_value = normalize_position(value)
                    if (
                        position_value in POSITION_GROUPS
                        and position_value not in merged_positions
                    ):
                        merged_positions.append(position_value)
            replacement["comparison_positions"] = ";".join(merged_positions)
            catalog[index] = {
                **original,
                **{
                    key: value
                    for key, value in replacement.items()
                    if value is not None and value != ""
                },
                **honor_fields,
                "nfl_hof": original.get("nfl_hof"),
            }
            register(index, catalog[index])
            honors_merged += 1
            continue
        row = {
            **replacement,
            **honor_fields,
            "nfl_hof": False if all_era_hof_complete else None,
            "reference_only": True,
            "career_elite_comparison_eligible": True,
        }
        index = len(catalog)
        catalog.append(row)
        register(index, row)
        honors_added += 1

    def catalog_contains_identity(source: Mapping[str, Any]) -> bool:
        stable = _normalized_pfr_id(source.get("pfr_id"))
        if stable and stable in by_pfr:
            return True
        page_key = str(source.get("wikipedia_page") or "").strip().casefold()
        if page_key and page_key in by_wikipedia_page:
            return True
        return unique_name_index(source.get("name")) is not None

    hof_unpositioned_rows = [
        row
        for row in hof_unpositioned_candidates
        if not catalog_contains_identity(row)
    ]

    catalog.sort(
        key=lambda row: (
            str(row.get("position") or ""),
            int(parse_number(row.get("draft_year")) or 0),
            str(row.get("name") or ""),
        )
    )
    draft_years = [
        int(parse_number(row.get("season")) or 0)
        for row in selected_draft
        if int(parse_number(row.get("season")) or 0) > 0
    ]
    primary_by_position: dict[str, int] = {}
    by_position: dict[str, int] = {}
    for row in catalog:
        position = str(row.get("position") or "")
        primary_by_position[position] = primary_by_position.get(position, 0) + 1
        comparison_positions = [
            normalize_position(value)
            for value in re.split(r"[;,|/]", str(row.get("comparison_positions") or position))
        ]
        for comparison_position in dict.fromkeys(comparison_positions):
            if comparison_position in POSITION_GROUPS:
                by_position[comparison_position] = by_position.get(comparison_position, 0) + 1
    all_era_hof_available = all_era_hof_complete is True
    honors_metadata = dict(historical_honors_metadata or {})
    all_pro_metadata = honors_metadata.get("first_team_all_pro")
    all_pro_metadata = (
        dict(all_pro_metadata) if isinstance(all_pro_metadata, Mapping) else {}
    )
    pro_bowl_metadata = honors_metadata.get("pro_bowl")
    pro_bowl_metadata = (
        dict(pro_bowl_metadata) if isinstance(pro_bowl_metadata, Mapping) else {}
    )
    all_pro_history_available = bool(all_pro_metadata.get("published"))
    pro_bowl_history_available = bool(pro_bowl_metadata.get("published"))
    all_pro_population_complete = bool(
        all_pro_metadata.get("source_population_complete")
    )
    pro_bowl_population_complete = bool(
        pro_bowl_metadata.get("source_population_complete")
    )
    historical_honors_available = bool(
        historical_honors_profiles
        and (all_pro_history_available or pro_bowl_history_available)
    )
    if historical_honors_available:
        scope_parts = [
            "all NFL eras for Hall of Fame inductees"
            if all_era_hof_available
            else "partial Hall of Fame registry"
        ]
        if all_pro_history_available:
            scope_parts.append(
                "canonical first-team All-Pro selections "
                f"{all_pro_metadata.get('coverage_start') or 1920}–"
                f"{all_pro_metadata.get('coverage_end') or 'latest'}"
            )
        if pro_bowl_history_available:
            scope_parts.append(
                "Pro Bowl selections "
                f"{pro_bowl_metadata.get('coverage_start') or 1950}–"
                f"{pro_bowl_metadata.get('coverage_end') or 'latest'}"
            )
        scope_parts.append(
            "nflverse drafted-player count reconciliation "
            f"{min(draft_years) if draft_years else 1980}–"
            f"{max(draft_years) if draft_years else end_year or 'latest'}"
        )
        scope_label = "; ".join(scope_parts)
        coverage_parts: list[str] = []
        if all_pro_history_available:
            coverage_parts.append(
                "All-Pro coverage uses one canonical first-team selector per season."
            )
        else:
            coverage_parts.append(
                "The all-history first-team All-Pro component was unavailable and was "
                "discarded; All-Pro qualification therefore falls back to nflverse's "
                "drafted-player coverage."
            )
        if pro_bowl_history_available:
            if not pro_bowl_population_complete:
                unverified_years = list(
                    pro_bowl_metadata.get(
                        "annual_roster_unverified_selection_years"
                    )
                    or ()
                )
                unverified_count: int | str = len(unverified_years) or "some"
                season_label = (
                    "season" if len(unverified_years) == 1 else "seasons"
                )
                coverage_parts.append(
                    f"Pro Bowl data span the full 1950–{pro_bowl_metadata.get('coverage_end') or 'latest'} era, "
                    f"but {unverified_count} {season_label} lack a complete "
                    "annual roster; A–Z lists cover those years without proving full recall."
                )
            else:
                coverage_parts.append(
                    "Pro Bowl annual-roster population coverage is complete."
                )
        else:
            coverage_parts.append(
                "The all-history Pro Bowl component was unavailable and was discarded; "
                "Pro Bowl qualification therefore falls back to nflverse's drafted-player "
                "coverage."
            )
        coverage_parts.append(
            "The catalog is comparison-only and never trains the draft model."
        )
        coverage_note = " ".join(coverage_parts)
        source_label = (
            "nflverse career honors plus Wikimedia all-era Hall of Fame and historical honors"
        )
    else:
        scope_label = (
            (
                "all NFL eras for Hall of Fame inductees; drafted players "
                f"{min(draft_years) if draft_years else 1980}–{max(draft_years) if draft_years else end_year or 'latest'} "
                "for first-team All-Pro and Pro Bowl qualification"
            )
            if all_era_hof_available
            else (
                "drafted players "
                f"{min(draft_years) if draft_years else 1980}–{max(draft_years) if draft_years else end_year or 'latest'}; "
                "all-era Hall of Fame source unavailable"
            )
        )
        coverage_note = (
            (
                "Hall of Fame membership covers the full league era through the source date. "
                "First-team All-Pro and Pro Bowl counts are complete only for drafted players "
                "in nflverse draft history (1980 onward); older and undrafted non-HOF honorees "
                "remain outside the free structured honors source."
            )
            if all_era_hof_available
            else (
                "The all-era Hall of Fame source was unavailable for this build. Only drafted-player "
                "honors in nflverse history (1980 onward) are represented; the output must not be "
                "described as all-era coverage."
            )
        )
        source_label = (
            "nflverse career honors plus Wikimedia all-era Hall of Fame registry"
            if all_era_hof_available
            else (
                "nflverse career honors plus a partial Wikimedia Hall of Fame registry"
                if hof_profiles
                else "nflverse drafted-player career honors"
            )
        )
    full_history_sources_available = bool(
        all_era_hof_available
        and all_pro_history_available
        and pro_bowl_history_available
    )
    source_population_complete = bool(
        full_history_sources_available
        and all_pro_population_complete
        and pro_bowl_population_complete
    )
    full_history_criteria_available = source_population_complete
    hof_unresolved_family_rows = [
        {
            "name": row.get("name"),
            "pfr_id": row.get("pfr_id"),
            "wikidata_id": row.get("wikidata_id"),
            "wikipedia_page": row.get("wikipedia_page"),
            "position_raw": row.get("position_raw"),
            "unresolved_position_families": row.get(
                "unresolved_position_families"
            ),
        }
        for row in hof_profiles
        if row.get("unresolved_position_families")
    ]
    honors_unresolved_family_rows = [
        {
            "name": row.get("name"),
            "pfr_id": row.get("pfr_id"),
            "wikidata_id": row.get("wikidata_id"),
            "wikipedia_page": row.get("wikipedia_page"),
            "position_raw": row.get("position_raw"),
            "unresolved_position_families": row.get(
                "unresolved_position_families"
            ),
        }
        for row in historical_honors_profiles
        if row.get("unresolved_position_families")
        and (
            (parse_number(row.get("nfl_all_pro_selections")) or 0) >= 1
            or (parse_number(row.get("nfl_pro_bowls")) or 0) >= 3
        )
    ]
    total_unpositioned_qualifiers = len(hof_unpositioned_rows) + len(
        honors_unpositioned_rows
    )
    unresolved_position_family_rows = (
        hof_unresolved_family_rows + honors_unresolved_family_rows
    )
    total_position_coverage_issues = (
        total_unpositioned_qualifiers + len(unresolved_position_family_rows)
    )
    full_history_position_coverage_complete = bool(
        full_history_criteria_available and total_position_coverage_issues == 0
    )
    if total_unpositioned_qualifiers:
        row_label = "row" if total_unpositioned_qualifiers == 1 else "rows"
        verb = "lacks" if total_unpositioned_qualifiers == 1 else "lack"
        listing_verb = "is" if total_unpositioned_qualifiers == 1 else "are"
        coverage_note = (
            coverage_note.rstrip()
            + f" {total_unpositioned_qualifiers} qualifying source {row_label} {verb} a "
            f"supported comparison position and {listing_verb} listed in catalog metadata."
        )
    if unresolved_position_family_rows:
        family_issue_count = len(unresolved_position_family_rows)
        row_label = "row" if family_issue_count == 1 else "rows"
        verb = "retains" if family_issue_count == 1 else "retain"
        coverage_note = (
            coverage_note.rstrip()
            + f" {family_issue_count} qualifying source {row_label} {verb} an "
            "unresolved historical position family; details are in catalog metadata."
        )
    metadata = {
        "source": source_label,
        "historical_career_elite_definition": NFL_CAREER_ELITE_DEFINITION,
        "rows": len(catalog),
        "by_position": by_position,
        "primary_by_position": primary_by_position,
        "position_memberships": sum(by_position.values()),
        "hof_rows": sum(row.get("nfl_hof") is True for row in catalog),
        "hof_rows_added_outside_draft_honors": hof_added,
        "hof_rows_merged": hof_merged,
        "draft_honors_start": min(draft_years) if draft_years else None,
        "draft_honors_end": max(draft_years) if draft_years else end_year,
        "scope_label": scope_label,
        "coverage_note": coverage_note,
        "all_era_hof_available": all_era_hof_available,
        "historical_all_pro_available": all_pro_history_available,
        "historical_pro_bowl_available": pro_bowl_history_available,
        "historical_all_pro_population_complete": all_pro_population_complete,
        "historical_pro_bowl_population_complete": pro_bowl_population_complete,
        "full_history_sources_available": full_history_sources_available,
        "source_population_complete": source_population_complete,
        "full_history_criteria_available": full_history_criteria_available,
        "full_history_position_coverage_complete": (
            full_history_position_coverage_complete
        ),
        "unpositioned_qualifier_rows": total_unpositioned_qualifiers,
        "position_coverage_issue_rows": total_position_coverage_issues,
        "unresolved_position_family_rows": len(
            unresolved_position_family_rows
        ),
        "unresolved_position_family_qualifiers": (
            unresolved_position_family_rows
        ),
        "hof_unpositioned_rows": len(hof_unpositioned_rows),
        "hof_unpositioned_qualifiers": hof_unpositioned_rows,
        "historical_honors_rows_added": honors_added,
        "historical_honors_rows_merged": honors_merged,
        "historical_honors_unpositioned_rows": len(honors_unpositioned_rows),
        "historical_honors_unpositioned_qualifiers": honors_unpositioned_rows,
        "historical_honors_covered_source_disagreements": (
            honors_covered_disagreements
        ),
        "comparison_only": True,
        "model_feature_eligible": False,
        "position_mapping_note": (
            "Explicit multi-position HOF roles are indexed in every listed position. "
            "HOF-listed pre-modern two-way Ends are indexed under WR and EDGE."
        ),
        "measurement_sources": {
            source: sum(row.get("measurement_source") == source for row in catalog)
            for source in sorted({str(row.get("measurement_source")) for row in catalog})
        },
    }
    return catalog, metadata


def load_nfl_career_elite_history(
    cache_dir: str | os.PathLike[str] = ".draftscope-cache",
    *,
    end_year: int | None = None,
    refresh: bool = False,
    _source_files_refreshed: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load the separately scoped, comparison-only career-elite catalog."""

    cache = Path(cache_dir)
    source_refresh = refresh and not _source_files_refreshed
    combine_path = _download(
        NFLVERSE_COMBINE_URL, cache / "combine.csv", refresh=source_refresh
    )
    draft_path = _download(
        NFLVERSE_DRAFT_URL, cache / "draft_picks.csv", refresh=source_refresh
    )
    player_rows: list[dict[str, str]] = []
    player_error: str | None = None
    try:
        players_path = _download(
            NFLVERSE_PLAYERS_URL, cache / "players.csv", refresh=refresh
        )
        player_rows = _read_csv(players_path)
    except (DataError, OSError) as exc:
        player_error = str(exc)
    hof_profiles: list[dict[str, Any]] = []
    hof_metadata: dict[str, Any]
    try:
        hof_profiles, hof_metadata = _load_wikimedia_hof_profiles(
            cache, refresh=refresh
        )
    except (DataError, OSError) as exc:
        hof_metadata = {"status": "unavailable", "error": str(exc), "rows": 0}
    historical_honors_profiles: list[dict[str, Any]] = []
    historical_honors_metadata: dict[str, Any]
    try:
        historical_honors_profiles, historical_honors_metadata = (
            load_historical_honors(
                cache,
                request_json=_json_request,
                refresh=refresh,
                latest_completed_season=(end_year - 1) if end_year else None,
            )
        )
        honors_identity_metadata = _enrich_wikimedia_profiles(
            historical_honors_profiles,
            cache,
            refresh=refresh,
            cache_prefix="wikimedia_historical_honors",
        )
        historical_honors_metadata["identity_enrichment"] = (
            honors_identity_metadata
        )
        honors_source_artifacts = [
            metadata
            for path in sorted((cache / "historical_honors").glob("*.json"))
            if (metadata := _artifact_source_metadata(path))
        ]
        historical_honors_metadata["source_artifacts"] = honors_source_artifacts
        historical_honors_metadata["source_refresh_stale"] = bool(
            honors_identity_metadata.get("source_refresh_stale")
            or any(
                artifact.get("cache_fallback") is True
                for artifact in honors_source_artifacts
            )
        )
    except (DataError, OSError, ValueError) as exc:
        historical_honors_metadata = {
            "status": "unavailable",
            "error": str(exc),
            "row_count": 0,
        }
    rows, metadata = _build_nfl_career_elite_rows(
        _read_csv(combine_path),
        _read_csv(draft_path),
        player_rows,
        hof_profiles,
        historical_honors_profiles,
        end_year=end_year,
        all_era_hof_complete=bool(hof_metadata.get("registry_complete")),
        historical_honors_metadata=historical_honors_metadata,
    )
    nflverse_source_artifacts = {
        "combine": _artifact_source_metadata(combine_path),
        "draft_picks": _artifact_source_metadata(draft_path),
        "players": _artifact_source_metadata(cache / "players.csv"),
    }
    source_refresh_stale = bool(
        hof_metadata.get("source_refresh_stale")
        or historical_honors_metadata.get("source_refresh_stale")
        or any(
            artifact.get("cache_fallback") is True
            for artifact in nflverse_source_artifacts.values()
        )
    )
    metadata.update(
        {
            "nflverse_combine_url": NFLVERSE_COMBINE_URL,
            "nflverse_draft_url": NFLVERSE_DRAFT_URL,
            "nflverse_players_url": NFLVERSE_PLAYERS_URL,
            "nflverse_license": {
                "name": "CC BY 4.0",
                "url": "https://creativecommons.org/licenses/by/4.0/",
                "note": "Upstream data terms also apply.",
            },
            "changes_made": (
                "Source fields were normalized, position-mapped, and merged by stable "
                "identity into a comparison-only DraftScope catalog."
            ),
            "nflverse_source_artifacts": nflverse_source_artifacts,
            "wikimedia": hof_metadata,
            "historical_honors": historical_honors_metadata,
            "player_registry_error": player_error,
            "source_refresh_stale": source_refresh_stale,
            "loaded_at": timestamp_utc(),
        }
    )
    if source_refresh_stale:
        metadata["coverage_note"] = (
            str(metadata.get("coverage_note") or "").rstrip()
            + " One or more requested source refreshes failed, so this build uses the "
            "last valid cached artifact and preserves its original download timestamp."
        ).strip()
    return rows, metadata


def _artifact_source_metadata(path: Path) -> dict[str, Any]:
    metadata_path = path.with_suffix(path.suffix + ".source.json")
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _college_keys(value: object) -> set[str]:
    text = str(value or "").strip()
    if not text:
        return set()
    pieces = [text, *re.split(r"\s*;\s*", text)]
    return {key for piece in pieces if (key := normalize_name(piece))}


def _latest_roster_snapshot(
    roster_rows: Iterable[Mapping[str, Any]], *, season: int
) -> tuple[list[dict[str, Any]], int | None]:
    selected = [
        dict(row)
        for row in roster_rows
        if str(row.get("season") or season) == str(season)
    ]
    weeks = [
        int(value)
        for row in selected
        if (value := parse_number(row.get("week"))) is not None
    ]
    latest_week = max(weeks) if weeks else None
    if latest_week is not None:
        selected = [
            row
            for row in selected
            if parse_number(row.get("week")) == float(latest_week)
        ]
    return selected, latest_week


def _unique_roster_indexes(
    roster_rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    collected: dict[str, dict[str, set[int]]] = {
        "pfr_id": {},
        "gsis_id": {},
        "cfb_id": {},
        "name_college": {},
    }

    def add(method: str, key: str, index: int) -> None:
        if key:
            collected[method].setdefault(key, set()).add(index)

    for index, row in enumerate(roster_rows):
        add("pfr_id", str(_first(row, "pfr_id", "pfr_player_id") or "").strip(), index)
        add("gsis_id", str(_first(row, "gsis_id") or "").strip(), index)
        add(
            "cfb_id",
            str(_first(row, "cfb_id", "cfb_player_id", "college_football_id") or "").strip(),
            index,
        )
        name = normalize_name(_first(row, "full_name", "football_name", "player_name", "name"))
        for college in _college_keys(_first(row, "college", "school")):
            add("name_college", f"{name}|{college}" if name else "", index)

    unique: dict[str, dict[str, int]] = {}
    ambiguous: dict[str, int] = {}
    for method, values in collected.items():
        unique[method] = {
            key: next(iter(indices))
            for key, indices in values.items()
            if len(indices) == 1
        }
        ambiguous[method] = sum(len(indices) > 1 for indices in values.values())
    return unique, ambiguous


def _match_active_roster(
    record: Mapping[str, Any],
    roster_rows: Sequence[Mapping[str, Any]],
    indexes: Mapping[str, Mapping[str, int]],
) -> tuple[Mapping[str, Any] | None, str, bool]:
    candidates: list[tuple[str, int]] = []
    pfr_id = str(_first(record, "pfr_id", "pfr_player_id") or "").strip()
    gsis_id = str(_first(record, "gsis_id") or "").strip()
    cfb_id = str(
        _first(record, "cfb_id", "cfb_player_id", "college_football_id") or ""
    ).strip()
    for method, key in (("pfr_id", pfr_id), ("gsis_id", gsis_id), ("cfb_id", cfb_id)):
        if key and key in indexes.get(method, {}):
            candidates.append((method, indexes[method][key]))

    name = normalize_name(
        _first(record, "full_name", "player_name", "pfr_player_name", "name")
    )
    name_college_matches = {
        indexes["name_college"][f"{name}|{college}"]
        for college in _college_keys(_first(record, "college", "school"))
        if name and f"{name}|{college}" in indexes.get("name_college", {})
    }
    if len(name_college_matches) == 1:
        candidates.append(("unique_name_college", next(iter(name_college_matches))))

    if not candidates:
        return None, "unmatched", False
    selected_method, selected_index = candidates[0]
    conflict = len({index for _method, index in candidates}) > 1
    return roster_rows[selected_index], selected_method, conflict


def _history_row_join_record(
    combine_row: Mapping[str, Any], draft_row: Mapping[str, Any] | None
) -> dict[str, Any]:
    joined = dict(combine_row)
    if not draft_row:
        return joined
    joined.update(
        {
            "pfr_player_id": _first(draft_row, "pfr_player_id")
            or _first(combine_row, "pfr_id"),
            "gsis_id": _first(draft_row, "gsis_id"),
            "cfb_player_id": _first(draft_row, "cfb_player_id")
            or _first(combine_row, "cfb_id"),
            "pfr_player_name": _first(draft_row, "pfr_player_name")
            or _first(combine_row, "player_name"),
            "college": _first(draft_row, "college") or _first(combine_row, "school"),
        }
    )
    return joined


def _build_nflverse_history_rows(
    combine_raw: Sequence[Mapping[str, Any]],
    draft_raw: Sequence[Mapping[str, Any]],
    roster_raw: Sequence[Mapping[str, Any]],
    *,
    start: int,
    latest: int,
    lookback_years: int,
    active_roster_season: int,
    roster_source_metadata: Mapping[str, Any] | None = None,
    contributor_outcomes: Mapping[tuple[int, str], Mapping[str, Any]] | None = None,
    contributor_metadata: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    years = {str(year) for year in range(start, latest + 1)}
    selected_draft = [row for row in draft_raw if str(row.get("season")) in years]
    draft_maps = _draft_match_maps(selected_draft)
    roster_snapshot, snapshot_week = _latest_roster_snapshot(
        roster_raw, season=active_roster_season
    )
    if not roster_snapshot:
        raise DataError(
            f"NFL roster source contained no rows for active benchmark season {active_roster_season}"
        )
    roster_indexes, ambiguous_index_counts = _unique_roster_indexes(roster_snapshot)
    status_counts: dict[str, int] = {}
    for roster in roster_snapshot:
        status = str(roster.get("status") or "UNKNOWN").upper().strip()
        status_counts[status] = status_counts.get(status, 0) + 1

    draft_join_counts: dict[str, int] = {}
    draft_join_conflicts = 0
    active_drafted_window = 0
    active_drafted_players: list[dict[str, Any]] = []
    for draft in selected_draft:
        roster, method, conflict = _match_active_roster(draft, roster_snapshot, roster_indexes)
        draft_join_counts[method] = draft_join_counts.get(method, 0) + 1
        draft_join_conflicts += int(conflict)
        status = str(roster.get("status") or "").upper().strip() if roster else ""
        is_active_drafted = bool(
            roster and status in ACTIVE_NFL_ROSTER_STATUSES and not conflict
        )
        active_drafted_window += int(is_active_drafted)
        if is_active_drafted:
            active_drafted_players.append(
                {
                    "draft_year": int(parse_number(draft.get("season")) or 0),
                    "draft_pick": int(parse_number(draft.get("pick")) or 0),
                    "pfr_player_id": _first(draft, "pfr_player_id", "pfr_id"),
                    "gsis_id": _first(draft, "gsis_id"),
                    "cfb_player_id": _first(draft, "cfb_player_id", "cfb_id"),
                    "name": _first(draft, "pfr_player_name", "player_name", "name"),
                    "college": _first(draft, "college", "school"),
                    "active_roster_status": status,
                    "active_roster_team": roster.get("team") if roster else None,
                    "active_roster_match": method,
                    "active_roster_join_conflict": conflict,
                }
            )

    rows: list[dict[str, Any]] = []
    outcome_match_counts: dict[str, int] = {}
    roster_join_counts: dict[str, int] = {}
    roster_join_conflicts = 0
    cohort_label = (
        f"active on nflverse {active_roster_season} roster; drafted {start}-{latest}; "
        "combine measurement available"
    )
    source_metadata = dict(roster_source_metadata or {})
    roster_source_as_of = source_metadata.get("downloaded_at")
    outcome_index = dict(contributor_outcomes or {})
    outcome_metadata = dict(contributor_metadata or {})
    completed_nfl_season = int(
        parse_number(outcome_metadata.get("completed_nfl_season"))
        or active_roster_season - 1
    )
    outcome_horizon = int(
        parse_number(outcome_metadata.get("horizon_seasons"))
        or NFL_CONTRIBUTOR_HORIZON_SEASONS
    )

    def contributor_outcome_for(draft_year: int, pfr_id: object) -> dict[str, Any]:
        stable_id = str(pfr_id or "").strip()
        known = outcome_index.get((draft_year, stable_id)) if stable_id else None
        if known is not None:
            return dict(known)
        horizon_end = draft_year + outcome_horizon - 1
        right_censored = horizon_end > completed_nfl_season
        if right_censored:
            reason = "right_censored"
        elif not stable_id:
            reason = "missing_pfr_id"
        elif outcome_metadata.get("status") == "unavailable":
            reason = "snap_source_unavailable"
        else:
            reason = "identity_not_in_outcome_index"
        return {
            "nfl_three_year_horizon_start": draft_year,
            "nfl_three_year_horizon_end": horizon_end,
            "nfl_three_year_outcome_known": False,
            "nfl_three_year_right_censored": right_censored,
            "nfl_three_year_contributor": None,
            "nfl_three_year_outcome_unknown_reason": reason,
            "nfl_three_year_outcome_source": "nflverse_pfr_snap_counts",
        }

    for raw in combine_raw:
        try:
            season = int(raw.get("season") or 0)
        except ValueError:
            continue
        if not start <= season <= latest:
            continue
        draft, outcome_match = _match_draft_row(raw, draft_maps)
        outcome_match_counts[outcome_match] = outcome_match_counts.get(outcome_match, 0) + 1
        existing_pick = parse_number(raw.get("draft_ovr"))
        pick = parse_number(draft.get("pick")) if draft else existing_pick
        round_number = (
            parse_number(draft.get("round"))
            if draft
            else parse_number(raw.get("draft_round"))
        )
        drafted = pick is not None
        career_outcomes = _nfl_career_outcome_fields(draft)
        joined_pfr_id = raw.get("pfr_id") or (
            draft.get("pfr_player_id") if draft else None
        )
        contributor_outcome = contributor_outcome_for(season, joined_pfr_id)
        roster, roster_match, roster_conflict = _match_active_roster(
            _history_row_join_record(raw, draft), roster_snapshot, roster_indexes
        )
        roster_join_counts[roster_match] = roster_join_counts.get(roster_match, 0) + 1
        roster_join_conflicts += int(roster_conflict)
        roster_status = str(roster.get("status") or "").upper().strip() if roster else ""
        status_eligible = bool(roster and roster_status in ACTIVE_NFL_ROSTER_STATUSES)
        identity_eligible = bool(roster and not roster_conflict)
        active = status_eligible and identity_eligible

        merged: dict[str, Any] = {
            "player_id": raw.get("pfr_id")
            or raw.get("cfb_id")
            or f"{season}:{normalize_name(raw.get('player_name'))}",
            "pfr_id": joined_pfr_id,
            "cfb_player_id": raw.get("cfb_id")
            or (draft.get("cfb_player_id") if draft else None),
            "gsis_id": draft.get("gsis_id") if draft else None,
            "name": raw.get("player_name"),
            "position_raw": raw.get("pos"),
            "position": normalize_position(raw.get("pos")),
            "school": raw.get("school"),
            "season": season,
            "draft_year": season,
            "height_in": raw.get("ht"),
            "weight_lb": raw.get("wt"),
            "forty_s": raw.get("forty"),
            "bench_reps": raw.get("bench"),
            "vertical_in": raw.get("vertical"),
            "broad_jump_in": raw.get("broad_jump"),
            "three_cone_s": raw.get("cone"),
            "shuttle_s": raw.get("shuttle"),
            "drafted": drafted,
            "draft_round": round_number,
            "draft_ovr": pick,
            "elite": bool(pick is not None and pick <= 64),
            **career_outcomes,
            "nfl_team": draft.get("team") if draft else raw.get("draft_team"),
            "age_at_draft": draft.get("age") if draft else None,
            "measurement_source": "pro_day_nonstandard" if season == 2021 else "nfl_combine",
            "measurements_verified": True,
            "population": "combine_invitee",
            "probability_kind": "conditional_on_combine_invitation",
            "probability_condition": (
                "P(drafted | NFL Scouting Combine participant, available combine measurements)"
            ),
            "outcome_match": outcome_match
            if draft
            else ("embedded_combine" if existing_pick else "undrafted_or_unmatched"),
            "active_roster_season": active_roster_season,
            "active_roster_snapshot_week": snapshot_week,
            "active_roster_present": roster is not None,
            "active_roster_status": roster_status or None,
            "active_roster_status_eligible": status_eligible,
            "active_roster_identity_eligible": identity_eligible,
            "active_roster_eligible": active,
            "active_roster_team": roster.get("team") if roster else None,
            "active_roster_match": roster_match,
            "active_roster_join_conflict": roster_conflict,
            "active_roster_source": NFLVERSE_ROSTER_URL.format(
                season=active_roster_season
            ),
            "active_roster_source_as_of": roster_source_as_of,
            "benchmark_population": cohort_label,
            "full_history_population": "NFL Scouting Combine participants",
            **contributor_outcome,
        }
        normalized = normalize_record(merged)
        measurement_available = any(
            parse_number(normalized.get(field)) is not None
            for field in _COMBINE_MEASUREMENT_FIELDS
        )
        cohort_eligible = drafted and active and measurement_available
        active_contributor_eligible = bool(
            drafted
            and active
            and contributor_outcome.get("nfl_three_year_outcome_known") is True
            and contributor_outcome.get("nfl_three_year_contributor") is True
        )
        normalized.update(
            {
                "combine_measurement_available": measurement_available,
                "benchmark_cohort_eligible": cohort_eligible,
                "benchmark_elite_cohort_eligible": bool(
                    cohort_eligible and pick is not None and pick <= 64
                ),
                "comparison_cohort_eligible": cohort_eligible,
                "active_three_year_contributor_eligible": active_contributor_eligible,
                "benchmark_active_contributor_cohort_eligible": bool(
                    active_contributor_eligible and measurement_available
                ),
                "benchmark_career_elite_cohort_eligible": bool(
                    drafted
                    and measurement_available
                    and normalized.get("nfl_career_elite") is True
                ),
            }
        )
        rows.append(normalized)

    drafted_count = sum(bool(row.get("drafted")) for row in rows)
    benchmark_rows = [row for row in rows if row.get("benchmark_cohort_eligible")]
    elite_rows = [row for row in rows if row.get("benchmark_elite_cohort_eligible")]
    benchmark_active_contributor_rows = [
        row
        for row in rows
        if row.get("benchmark_active_contributor_cohort_eligible")
    ]
    historical_career_elite_rows = [
        row for row in rows if row.get("benchmark_career_elite_cohort_eligible")
    ]
    metric_coverage = {
        field: {
            "rows": sum(parse_number(row.get(field)) is not None for row in benchmark_rows),
            "coverage": (
                sum(parse_number(row.get(field)) is not None for row in benchmark_rows)
                / len(benchmark_rows)
                if benchmark_rows
                else None
            ),
        }
        for field in _COMBINE_MEASUREMENT_FIELDS
    }
    cohort_by_position: dict[str, int] = {}
    elite_by_position: dict[str, int] = {}
    for row in benchmark_rows:
        position = normalize_position(row.get("position"))
        cohort_by_position[position] = cohort_by_position.get(position, 0) + 1
    for row in elite_rows:
        position = normalize_position(row.get("position"))
        elite_by_position[position] = elite_by_position.get(position, 0) + 1

    active_contributor_players: list[dict[str, Any]] = []
    for player in active_drafted_players:
        draft_year = int(parse_number(player.get("draft_year")) or 0)
        outcome = contributor_outcome_for(draft_year, player.get("pfr_player_id"))
        if (
            outcome.get("nfl_three_year_outcome_known") is True
            and outcome.get("nfl_three_year_contributor") is True
        ):
            active_contributor_players.append({**player, **outcome})
    contributor_by_position: dict[str, int] = {}
    for row in benchmark_active_contributor_rows:
        position = normalize_position(row.get("position"))
        contributor_by_position[position] = contributor_by_position.get(position, 0) + 1
    career_elite_by_position: dict[str, int] = {}
    for row in historical_career_elite_rows:
        position = normalize_position(row.get("position"))
        career_elite_by_position[position] = career_elite_by_position.get(position, 0) + 1

    active_status_rows = sum(
        count for status, count in status_counts.items() if status in ACTIVE_NFL_ROSTER_STATUSES
    )
    benchmark_coverage = (
        len(benchmark_rows) / active_drafted_window if active_drafted_window else None
    )
    metadata = {
        "source": "nflverse",
        "combine_url": NFLVERSE_COMBINE_URL,
        "draft_url": NFLVERSE_DRAFT_URL,
        "active_roster_url": NFLVERSE_ROSTER_URL.format(season=active_roster_season),
        "license": "CC BY 4.0; upstream data terms also apply",
        "window_start": start,
        "window_end": latest,
        "lookback_years": lookback_years,
        "population": "NFL Scouting Combine participants",
        "probability_kind": "conditional_on_combine_invitation",
        "probability_condition": (
            "P(drafted | NFL Scouting Combine participant, available combine measurements)"
        ),
        "rows": len(rows),
        "drafted_rows": drafted_count,
        "base_rate": drafted_count / len(rows) if rows else None,
        "match_counts": outcome_match_counts,
        "benchmark_population": cohort_label,
        "benchmark_source": "nflverse combine, draft picks, and active roster",
        "benchmark_rows": len(benchmark_rows),
        "benchmark_elite_rows": len(elite_rows),
        "benchmark_active_contributor_rows": len(benchmark_active_contributor_rows),
        "benchmark_active_contributor_by_position": contributor_by_position,
        "historical_career_elite_definition": NFL_CAREER_ELITE_DEFINITION,
        "historical_career_elite_rows": len(historical_career_elite_rows),
        "historical_career_elite_by_position": career_elite_by_position,
        "benchmark_cohort_coverage": benchmark_coverage,
        "benchmark_cohort_by_position": cohort_by_position,
        "benchmark_elite_by_position": elite_by_position,
        "benchmark_measurement_coverage": metric_coverage,
        "active_roster_season": active_roster_season,
        "active_roster_snapshot_week": snapshot_week,
        "active_roster_source_as_of": roster_source_as_of,
        "active_roster_source_as_of_basis": "local artifact download timestamp",
        "active_roster_source_sha256": source_metadata.get("sha256"),
        "active_roster_rows": len(roster_snapshot),
        "active_roster_status_rows": active_status_rows,
        "active_roster_status_counts": status_counts,
        "active_roster_included_statuses": sorted(ACTIVE_NFL_ROSTER_STATUSES),
        "active_roster_excluded_status_rows": len(roster_snapshot) - active_status_rows,
        "active_roster_ambiguous_index_keys": ambiguous_index_counts,
        "draft_window_rows": len(selected_draft),
        "draft_window_roster_match_counts": draft_join_counts,
        "draft_window_roster_join_conflicts": draft_join_conflicts,
        "active_drafted_window_rows": active_drafted_window,
        "active_drafted_players": active_drafted_players,
        "active_contributor_window_rows": len(active_contributor_players),
        "active_contributor_players": active_contributor_players,
        "combine_window_roster_match_counts": roster_join_counts,
        "combine_window_roster_join_conflicts": roster_join_conflicts,
        "nfl_three_year_contributor_outcome": outcome_metadata,
        "loaded_at": timestamp_utc(),
    }
    return rows, metadata


def load_nflverse_history(
    cache_dir: str | os.PathLike[str] = ".draftscope-cache",
    *,
    lookback_years: int = 10,
    end_year: int | None = None,
    active_roster_season: int = 2026,
    nfl_completed_season: int | None = None,
    refresh: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load the latest completed draft window from nflverse.

    The source population is combine participants, including undrafted players.
    That supports a conditional probability, not a probability for every NCAA player.
    """
    cache = Path(cache_dir)
    combine_path = _download(NFLVERSE_COMBINE_URL, cache / "combine.csv", refresh=refresh)
    draft_path = _download(NFLVERSE_DRAFT_URL, cache / "draft_picks.csv", refresh=refresh)
    roster_url = NFLVERSE_ROSTER_URL.format(season=active_roster_season)
    roster_path = _download(
        roster_url,
        cache / f"roster_{active_roster_season}.csv",
        refresh=refresh,
    )
    combine_raw = _read_csv(combine_path)
    draft_raw = _read_csv(draft_path)
    draft_counts: dict[int, int] = {}
    combine_counts: dict[int, int] = {}
    for row in draft_raw:
        try:
            year = int(row.get("season") or 0)
        except ValueError:
            continue
        draft_counts[year] = draft_counts.get(year, 0) + 1
    for row in combine_raw:
        try:
            year = int(row.get("season") or 0)
        except ValueError:
            continue
        combine_counts[year] = combine_counts.get(year, 0) + 1
    completed = sorted(year for year, count in draft_counts.items() if count >= 200 and combine_counts.get(year, 0) >= 100)
    if not completed:
        raise DataError("Could not identify a completed draft class in nflverse files")
    latest = end_year or completed[-1]
    start = latest - lookback_years + 1
    completed_nfl_season = (
        int(nfl_completed_season)
        if nfl_completed_season is not None
        else active_roster_season - 1
    )
    if completed_nfl_season > active_roster_season:
        raise DataError("completed NFL season cannot exceed the active roster season")

    contributor_outcomes: dict[tuple[int, str], dict[str, Any]] = {}
    contributor_metadata: dict[str, Any]
    try:
        snap_end = min(latest + NFL_CONTRIBUTOR_HORIZON_SEASONS - 1, completed_nfl_season)
        snap_seasons = tuple(range(start, snap_end + 1)) if snap_end >= start else ()
        snap_rows, snap_metadata = load_nflverse_snap_counts(
            cache,
            seasons=snap_seasons,
            refresh=refresh,
        )
        selected_draft = [
            row
            for row in draft_raw
            if start <= int(parse_number(row.get("season")) or 0) <= latest
        ]
        draft_maps = _draft_match_maps(selected_draft)
        outcome_identities: list[dict[str, Any]] = [
            {
                "draft_year": int(parse_number(row.get("season")) or 0),
                "pfr_player_id": row.get("pfr_player_id"),
            }
            for row in selected_draft
        ]
        for row in combine_raw:
            draft_year = int(parse_number(row.get("season")) or 0)
            if not start <= draft_year <= latest:
                continue
            draft, _method = _match_draft_row(row, draft_maps)
            outcome_identities.append(
                {
                    "draft_year": draft_year,
                    "pfr_player_id": row.get("pfr_id")
                    or (draft.get("pfr_player_id") if draft else None),
                }
            )
        contributor_outcomes, outcome_metadata = build_three_year_contributor_outcomes(
            outcome_identities,
            snap_rows,
            completed_season=completed_nfl_season,
            complete_seasons=snap_metadata.get("complete_seasons", ()),
        )
        contributor_metadata = {
            **outcome_metadata,
            "source": snap_metadata.get("source"),
            "source_url_template": snap_metadata.get("source_url_template"),
            "license": snap_metadata.get("license"),
            "source_status": snap_metadata.get("status"),
            "source_artifacts": snap_metadata.get("source_artifacts", []),
            "requested_snap_seasons": snap_metadata.get("requested_seasons", []),
        }
    except (DataError, OSError) as exc:
        contributor_metadata = {
            "status": "unavailable",
            "source": "nflverse/PFR snap counts",
            "horizon_seasons": NFL_CONTRIBUTOR_HORIZON_SEASONS,
            "completed_nfl_season": completed_nfl_season,
            "known_outcomes": 0,
            "contributors": 0,
            "right_censored_outcomes": 0,
            "error": str(exc),
        }
    rows, metadata = _build_nflverse_history_rows(
        combine_raw,
        draft_raw,
        _read_csv(roster_path),
        start=start,
        latest=latest,
        lookback_years=lookback_years,
        active_roster_season=active_roster_season,
        roster_source_metadata=_artifact_source_metadata(roster_path),
        contributor_outcomes=contributor_outcomes,
        contributor_metadata=contributor_metadata,
    )
    try:
        career_elite_rows, career_elite_metadata = load_nfl_career_elite_history(
            cache,
            end_year=latest,
            refresh=refresh,
            _source_files_refreshed=True,
        )
        career_elite_path = cache / "career_elite_history.csv"
        write_records(career_elite_path, career_elite_rows)
        career_elite_metadata_path = career_elite_path.with_suffix(".metadata.json")
        career_elite_metadata_path.write_text(
            json.dumps(career_elite_metadata, indent=2, sort_keys=True, default=str)
            + "\n",
            encoding="utf-8",
        )
        metadata.update(
            {
                "historical_career_elite_catalog_path": str(
                    career_elite_path.resolve()
                ),
                "historical_career_elite_catalog_metadata_path": str(
                    career_elite_metadata_path.resolve()
                ),
                "historical_career_elite_catalog": career_elite_metadata,
                "historical_career_elite_catalog_rows": len(career_elite_rows),
                "historical_career_elite_catalog_by_position": dict(
                    career_elite_metadata.get("by_position") or {}
                ),
            }
        )
    except (DataError, OSError) as exc:
        metadata["historical_career_elite_catalog"] = {
            "status": "unavailable",
            "error": str(exc),
            "comparison_only": True,
            "model_feature_eligible": False,
        }
    return rows, metadata


def load_latest_completed_draft_class(
    cache_dir: str | os.PathLike[str] = ".draftscope-cache",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read the latest complete draft class as reference-only scouting rows.

    This helper performs no download. Every selection is returned, including
    players without a matching Combine record; missing measurements remain
    explicit instead of silently dropping the player. Career honors and the
    known draft result are descriptive outcomes, never scouting features.
    """

    cache = Path(cache_dir)
    combine_path = cache / "combine.csv"
    draft_path = cache / "draft_picks.csv"
    missing = [path for path in (combine_path, draft_path) if not path.exists()]
    if missing:
        raise DataError(
            "Cached nflverse reference files are missing: "
            + ", ".join(str(path) for path in missing)
        )

    combine_raw = _read_csv(combine_path)
    draft_raw = _read_csv(draft_path)
    draft_counts: dict[int, int] = {}
    for row in draft_raw:
        year = int(parse_number(row.get("season")) or 0)
        draft_counts[year] = draft_counts.get(year, 0) + 1
    completed = sorted(year for year, count in draft_counts.items() if count >= 200)
    if not completed:
        raise DataError("Could not identify a completed NFL draft class in cached nflverse data")
    latest = completed[-1]
    selected_draft = [
        row for row in draft_raw if int(parse_number(row.get("season")) or 0) == latest
    ]
    selected_combine = [
        row for row in combine_raw if int(parse_number(row.get("season")) or 0) == latest
    ]

    combine_by_pfr: dict[str, list[Mapping[str, Any]]] = {}
    combine_by_cfb: dict[str, list[Mapping[str, Any]]] = {}
    combine_by_name_school: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    combine_by_name: dict[str, list[Mapping[str, Any]]] = {}
    for row in selected_combine:
        pfr = str(row.get("pfr_id") or "").strip()
        cfb = str(row.get("cfb_id") or "").strip()
        name = normalize_name(row.get("player_name"))
        school = normalize_name(row.get("school"))
        if pfr:
            combine_by_pfr.setdefault(pfr, []).append(row)
        if cfb:
            combine_by_cfb.setdefault(cfb, []).append(row)
        if name and school:
            combine_by_name_school.setdefault((name, school), []).append(row)
        if name:
            combine_by_name.setdefault(name, []).append(row)

    def unique(values: Sequence[Mapping[str, Any]] | None) -> Mapping[str, Any] | None:
        return values[0] if values and len(values) == 1 else None

    def combine_for(draft: Mapping[str, Any]) -> tuple[Mapping[str, Any] | None, str]:
        pfr = str(draft.get("pfr_player_id") or "").strip()
        cfb = str(draft.get("cfb_player_id") or "").strip()
        name = normalize_name(draft.get("pfr_player_name"))
        school = normalize_name(draft.get("college"))
        for method, match in (
            ("pfr_id", unique(combine_by_pfr.get(pfr)) if pfr else None),
            ("cfb_id", unique(combine_by_cfb.get(cfb)) if cfb else None),
            (
                "name_school",
                unique(combine_by_name_school.get((name, school)))
                if name and school
                else None,
            ),
            ("unique_name", unique(combine_by_name.get(name)) if name else None),
        ):
            if match is not None:
                return match, method
        return None, "missing_combine_row"

    rows: list[dict[str, Any]] = []
    match_counts: dict[str, int] = {}
    for draft in sorted(
        selected_draft,
        key=lambda row: int(parse_number(row.get("pick")) or 10_000),
    ):
        combine, match_method = combine_for(draft)
        match_counts[match_method] = match_counts.get(match_method, 0) + 1
        pick = parse_number(draft.get("pick"))
        raw: dict[str, Any] = {
            "player_id": draft.get("pfr_player_id")
            or draft.get("cfb_player_id")
            or f"draft:{latest}:{int(pick or 0)}",
            "pfr_id": draft.get("pfr_player_id"),
            "pfr_player_id": draft.get("pfr_player_id"),
            "cfb_player_id": draft.get("cfb_player_id"),
            "gsis_id": draft.get("gsis_id"),
            "name": draft.get("pfr_player_name"),
            "position_raw": draft.get("position")
            or (combine.get("pos") if combine else None),
            "position": normalize_position(
                draft.get("position") or (combine.get("pos") if combine else None)
            ),
            "school": draft.get("college")
            or (combine.get("school") if combine else None),
            "season": latest,
            "draft_year": latest,
            "height_in": combine.get("ht") if combine else None,
            "weight_lb": combine.get("wt") if combine else None,
            "forty_s": combine.get("forty") if combine else None,
            "bench_reps": combine.get("bench") if combine else None,
            "vertical_in": combine.get("vertical") if combine else None,
            "broad_jump_in": combine.get("broad_jump") if combine else None,
            "three_cone_s": combine.get("cone") if combine else None,
            "shuttle_s": combine.get("shuttle") if combine else None,
            "age_at_draft": draft.get("age"),
            "drafted": True,
            "outcome_label_known": True,
            "draft_round": draft.get("round"),
            "draft_ovr": draft.get("pick"),
            "nfl_team": draft.get("team"),
            "elite": bool(pick is not None and pick <= 64),
            **_nfl_career_outcome_fields(draft),
            "combine_measurement_available": combine is not None
            and any(
                str(combine.get(field) or "").strip().lower()
                not in {"", "na", "n/a", "nan", "none", "null", "unknown", "-"}
                for field in ("ht", "wt", "forty", "bench", "vertical", "broad_jump", "cone", "shuttle")
            ),
            "combine_match_method": match_method,
            "measurement_source": "nfl_combine" if combine else "unavailable",
            "measurements_verified": combine is not None,
            "reference_only": True,
            "population": "completed NFL draft class reference players",
        }
        rows.append(normalize_record(raw))

    metadata = {
        "source": "cached nflverse combine and draft picks",
        "draft_year": latest,
        "rows": len(rows),
        "combine_rows": len(selected_combine),
        "combine_match_counts": match_counts,
        "reference_only": True,
        "career_outcome_definition": NFL_CAREER_ELITE_DEFINITION,
        "career_elite_rows": sum(row.get("nfl_career_elite") is True for row in rows),
        "outcome_fields": [
            "draft_year",
            "draft_round",
            "draft_ovr",
            "nfl_team",
            "nfl_hof",
            "nfl_all_pro_selections",
            "nfl_pro_bowls",
            "nfl_career_elite",
        ],
    }
    return rows, metadata


def load_nflverse_team_profiles(
    cache_dir: str | os.PathLike[str] = ".draftscope-cache",
    *,
    season: int,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    """Build evidence-labeled NFL position profiles from nflverse public data.

    The automatic evidence is intentionally limited to facts these files can
    support: current listed room size, room age/experience, and the team's
    position-specific selections in the three latest completed drafts.  It does
    not estimate contracts, starter quality, scheme, or future pick ownership.
    """

    cache = Path(cache_dir)
    roster_url = NFLVERSE_ROSTER_URL.format(season=season)
    roster_path = _download(roster_url, cache / f"roster_{season}.csv", refresh=refresh)
    draft_path = _download(NFLVERSE_DRAFT_URL, cache / "draft_picks.csv", refresh=refresh)
    return build_nfl_team_profiles(
        _read_csv(roster_path),
        _read_csv(draft_path),
        season=season,
        roster_source=roster_url,
        draft_source=NFLVERSE_DRAFT_URL,
        profile_as_of_date=str(
            _artifact_source_metadata(roster_path).get("downloaded_at") or ""
        ),
    )


def build_nfl_team_profiles(
    roster_rows: Iterable[Mapping[str, Any]],
    draft_rows: Iterable[Mapping[str, Any]],
    *,
    season: int,
    roster_source: str = "nflverse roster",
    draft_source: str = "nflverse draft picks",
    profile_as_of_date: str = "",
) -> list[dict[str, Any]]:
    """Turn roster and draft rows into auditable, confidence-scored profiles."""

    evaluation_date = date(season, 9, 1)
    latest_by_player: dict[str, tuple[float, Mapping[str, Any]]] = {}
    for index, raw in enumerate(roster_rows):
        row_season = parse_number(raw.get("season"))
        if row_season is not None and int(row_season) != season:
            continue
        identity = str(
            raw.get("gsis_id")
            or raw.get("pfr_id")
            or raw.get("espn_id")
            or normalize_name(raw.get("full_name") or raw.get("football_name"))
            or f"row-{index}"
        )
        week = parse_number(raw.get("week")) or 0.0
        previous = latest_by_player.get(identity)
        if previous is None or week >= previous[0]:
            latest_by_player[identity] = (week, raw)

    players_by_room: dict[tuple[str, str], list[dict[str, float | None]]] = {}
    teams: set[str] = set()
    snapshot_week: float | None = None
    for week, raw in latest_by_player.values():
        team = str(raw.get("team") or "").strip().upper()
        position = normalize_position(raw.get("position"))
        status = str(raw.get("status") or "").strip().upper()
        if not team or position not in POSITION_GROUPS or status in {"CUT", "RET"}:
            continue
        teams.add(team)
        snapshot_week = week if snapshot_week is None else max(snapshot_week, week)
        age: float | None = None
        try:
            born = date.fromisoformat(str(raw.get("birth_date") or "")[:10])
            candidate_age = (evaluation_date - born).days / 365.2425
            if 18.0 <= candidate_age <= 50.0:
                age = candidate_age
        except (TypeError, ValueError):
            pass
        experience = parse_number(raw.get("years_exp"))
        if experience is not None and not 0.0 <= experience <= 30.0:
            experience = None
        players_by_room.setdefault((team, position), []).append(
            {"age": age, "years_exp": experience}
        )

    if not teams:
        raise DataError("NFL roster source contained no usable team-position rows")

    draft_materialized = list(draft_rows)
    draft_class_counts: dict[int, int] = {}
    for row in draft_materialized:
        year = parse_number(row.get("season"))
        pick = parse_number(row.get("pick"))
        if year is not None and pick is not None and int(year) <= season:
            draft_class_counts[int(year)] = draft_class_counts.get(int(year), 0) + 1
    # A normal modern class has 250-plus picks. Requiring 200 prevents a partial
    # download from turning missing selections into false "lack of investment."
    completed_drafts = sorted(year for year, count in draft_class_counts.items() if count >= 200)[-3:]
    draft_window = (
        f"{completed_drafts[0]}–{completed_drafts[-1]}" if completed_drafts else "unavailable"
    )
    draft_completeness = (
        sum(min(1.0, draft_class_counts[year] / 250.0) for year in completed_drafts)
        / len(completed_drafts)
        if completed_drafts
        else 0.0
    )
    investment_by_room: dict[tuple[str, str], list[float]] = {}
    picks_by_room: dict[tuple[str, str], list[int]] = {}
    for raw in draft_materialized:
        year = parse_number(raw.get("season"))
        pick = parse_number(raw.get("pick"))
        team = str(raw.get("team") or "").strip().upper()
        position = normalize_position(raw.get("position"))
        if (
            year is None
            or int(year) not in completed_drafts
            or pick is None
            or pick <= 0
            or not team
            or position not in POSITION_GROUPS
        ):
            continue
        pick_number = int(pick)
        picks_by_room.setdefault((team, position), []).append(pick_number)
        # Smooth, transparent draft-capital proxy: pick 1=1.00, pick 64=.46,
        # and pick 257=.04. It is used only for within-position NFL percentiles.
        investment_by_room.setdefault((team, position), []).append(
            math.exp(-max(0.0, pick_number - 1.0) / 80.0)
        )

    roster_team_coverage = min(1.0, len(teams) / 32.0)
    profiles: list[dict[str, Any]] = []
    for position in POSITION_GROUPS:
        room_metrics: dict[str, dict[str, float | None]] = {}
        for team in teams:
            room = players_by_room.get((team, position), [])
            ages = [float(player["age"]) for player in room if player["age"] is not None]
            experiences = [
                float(player["years_exp"])
                for player in room
                if player["years_exp"] is not None
            ]
            room_metrics[team] = {
                "count": float(len(room)),
                "average_age": sum(ages) / len(ages) if ages else None,
                "average_experience": (
                    sum(experiences) / len(experiences) if experiences else None
                ),
                "age_coverage": len(ages) / len(room) if room else 0.0,
                "experience_coverage": len(experiences) / len(room) if room else 0.0,
                "veteran_share": (
                    sum(value >= 5.0 for value in experiences) / len(experiences)
                    if experiences
                    else None
                ),
                "draft_capital": sum(investment_by_room.get((team, position), [])),
            }

        counts = [metric["count"] for metric in room_metrics.values()]
        average_ages = [metric["average_age"] for metric in room_metrics.values()]
        average_experience = [metric["average_experience"] for metric in room_metrics.values()]
        veteran_shares = [metric["veteran_share"] for metric in room_metrics.values()]
        draft_capitals = [metric["draft_capital"] for metric in room_metrics.values()]
        league_room_median = median(counts) or 0.0

        for team in sorted(teams):
            metric = room_metrics[team]
            count = int(metric["count"] or 0)
            room_percentile = percentile_rank(float(count), counts)
            depth_pressure = 100.0 - room_percentile if room_percentile is not None else None

            transition_parts: list[tuple[float | None, float]] = []
            age = metric["average_age"]
            experience = metric["average_experience"]
            veteran_share = metric["veteran_share"]
            if age is not None:
                transition_parts.append((percentile_rank(age, average_ages), 0.45))
            if experience is not None:
                transition_parts.append((percentile_rank(experience, average_experience), 0.35))
            if veteran_share is not None:
                transition_parts.append((percentile_rank(veteran_share, veteran_shares), 0.20))
            transition_pressure = weighted_mean(transition_parts)
            transition_coverage = roster_team_coverage * (
                0.5 * float(metric["age_coverage"] or 0.0)
                + 0.5 * float(metric["experience_coverage"] or 0.0)
            )

            capital = float(metric["draft_capital"] or 0.0)
            investment_percentile = (
                percentile_rank(capital, draft_capitals) if completed_drafts else None
            )
            lack_investment = (
                100.0 - investment_percentile if investment_percentile is not None else None
            )

            need_components = (
                (depth_pressure, 0.40, roster_team_coverage, 0.78),
                (transition_pressure, 0.30, transition_coverage, 0.72),
                (lack_investment, 0.30, draft_completeness, 0.95),
            )
            need_pairs = [
                (value, weight * coverage)
                for value, weight, coverage, _confidence in need_components
                if value is not None and coverage > 0
            ]
            need = weighted_mean(need_pairs)
            need_evidence = sum(
                weight * coverage
                for value, weight, coverage, _confidence in need_components
                if value is not None
            )
            need_confidence = weighted_mean(
                (
                    confidence,
                    weight * coverage,
                )
                for value, weight, coverage, confidence in need_components
                if value is not None
            )
            picks = sorted(picks_by_room.get((team, position), []))
            age_text = f"; avg age {age:.1f}" if age is not None else ""
            exp_text = f"; avg exp {experience:.1f}" if experience is not None else ""
            draft_text = (
                f"; {len(picks)} {position} pick(s) in {draft_window}"
                if completed_drafts
                else "; recent draft evidence unavailable"
            )
            profiles.append(
                {
                    "team": team,
                    "season": season,
                    "position": position,
                    "profile_as_of_date": profile_as_of_date,
                    "scheme": "",
                    "need_score": round(need, 3) if need is not None else None,
                    "need_evidence_coverage": round(need_evidence, 4),
                    "need_confidence": round(need_confidence, 4) if need_confidence is not None else None,
                    "need_source": "nflverse_roster_and_recent_draft_derived",
                    "timeline_score": (
                        round(transition_pressure, 3)
                        if transition_pressure is not None
                        else None
                    ),
                    "timeline_evidence_coverage": round(transition_coverage, 4),
                    "timeline_confidence": 0.72 if transition_pressure is not None else None,
                    "timeline_source": "nflverse_roster_age_and_experience_derived",
                    "timeline_as_of_date": profile_as_of_date,
                    "draft_access_min": None,
                    "draft_access_max": None,
                    "draft_access_source": "unavailable_future_pick_ownership_not_in_source",
                    "draft_access_as_of_date": profile_as_of_date,
                    "room_count": count,
                    "league_room_median": round(league_room_median, 2),
                    "room_count_percentile": round(room_percentile, 2) if room_percentile is not None else None,
                    "room_depth_pressure": round(depth_pressure, 2) if depth_pressure is not None else None,
                    "average_age": round(age, 2) if age is not None else None,
                    "average_experience": round(experience, 2) if experience is not None else None,
                    "age_data_coverage": round(float(metric["age_coverage"] or 0.0), 4),
                    "experience_data_coverage": round(float(metric["experience_coverage"] or 0.0), 4),
                    "age_experience_pressure": (
                        round(transition_pressure, 2)
                        if transition_pressure is not None
                        else None
                    ),
                    "recent_draft_window": draft_window,
                    "recent_position_picks": len(picks) if completed_drafts else None,
                    "best_recent_position_pick": picks[0] if picks else None,
                    "recent_draft_capital": round(capital, 4) if completed_drafts else None,
                    "recent_draft_investment_percentile": (
                        round(investment_percentile, 2)
                        if investment_percentile is not None
                        else None
                    ),
                    "lack_recent_draft_investment": (
                        round(lack_investment, 2) if lack_investment is not None else None
                    ),
                    "roster_snapshot_week": int(snapshot_week) if snapshot_week is not None else None,
                    "roster_source": roster_source,
                    "draft_investment_source": draft_source,
                    "profile_source": "nflverse_roster_and_draft_derived",
                    "notes": (
                        f"Evidence: {count} listed {position} vs NFL median {league_room_median:g}"
                        f"{age_text}{exp_text}{draft_text}."
                    ),
                }
            )
    return profiles


def merge_team_profiles(
    automatic: Iterable[Mapping[str, Any]], manual: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for row in automatic:
        key = (str(row.get("team") or "").upper(), normalize_position(row.get("position")))
        merged[key] = dict(row)
    for row in manual:
        key = (str(row.get("team") or "").upper(), normalize_position(row.get("position")))
        had_automatic = key in merged
        existing = merged.get(key, {})
        for field, value in row.items():
            if value not in (None, ""):
                existing[field] = value
        existing["team"] = row.get("team") or existing.get("team")
        existing["position"] = key[1]
        if row.get("need_score") not in (None, ""):
            existing["need_source"] = "manual_review"
            existing["need_evidence_coverage"] = (
                parse_number(row.get("need_evidence_coverage"))
                if row.get("need_evidence_coverage") not in (None, "")
                else 1.0
            )
            existing["need_confidence"] = (
                parse_number(row.get("need_confidence"))
                if row.get("need_confidence") not in (None, "")
                else 0.75
            )
        if row.get("timeline_score") not in (None, ""):
            existing["timeline_evidence_coverage"] = (
                parse_number(row.get("timeline_evidence_coverage"))
                if row.get("timeline_evidence_coverage") not in (None, "")
                else 1.0
            )
            existing["timeline_confidence"] = (
                parse_number(row.get("timeline_confidence"))
                if row.get("timeline_confidence") not in (None, "")
                else 0.75
            )
            existing["timeline_source"] = str(
                row.get("timeline_source") or "manual_review"
            )
        if row.get("draft_access_min") not in (None, "") and row.get("draft_access_max") not in (None, ""):
            existing["draft_access_source"] = "manual_review"
            existing["draft_access_confidence"] = (
                parse_number(row.get("draft_access_confidence"))
                if row.get("draft_access_confidence") not in (None, "")
                else 0.75
            )
        for component in ("starter_quality", "contract", "coaching"):
            score_field = (
                "coaching_stability_score"
                if component == "coaching"
                else f"{component}_need_score"
            )
            if row.get(score_field) in (None, ""):
                continue
            coverage_field = f"{component}_evidence_coverage"
            confidence_field = f"{component}_confidence"
            source_field = f"{component}_source"
            existing[coverage_field] = (
                parse_number(row.get(coverage_field))
                if row.get(coverage_field) not in (None, "")
                else 1.0
            )
            existing[confidence_field] = (
                parse_number(row.get(confidence_field))
                if row.get(confidence_field) not in (None, "")
                else 0.75
            )
            existing[source_field] = str(row.get(source_field) or "manual_review")
        if any(
            str(field).startswith(("importance_", "ideal_", "tolerance_"))
            and value not in (None, "")
            for field, value in row.items()
        ):
            existing["scheme_source"] = "manual_prototype"
            existing["scheme_confidence"] = (
                parse_number(row.get("scheme_confidence"))
                if row.get("scheme_confidence") not in (None, "")
                else 0.75
            )
        existing["profile_source"] = "manual_plus_nflverse" if had_automatic else "manual"
        merged[key] = existing
    return list(merged.values())


def build_cfbd_weekly_history(
    client: "CFBDClient",
    *,
    target_season: int,
    as_of_week: int,
    lookback_years: int = 10,
    raw_dir: str | os.PathLike[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Construct a week-matched labeled FBS leaver cohort.

    The risk set is players with recorded production who appeared on an FBS roster
    in the college season before a draft and did not appear on the following FBS
    season roster. It estimates selection conditional on leaving/declaring.
    """
    if as_of_week < 1:
        raise DataError("Week-matched history requires as_of_week >= 1")
    first_draft = target_season - lookback_years + 1
    draft_years = list(range(first_draft, target_season + 1))
    roster_years = list(range(first_draft - 1, target_season + 1))
    raw_path = Path(raw_dir) if raw_dir else None
    if raw_path:
        raw_path.mkdir(parents=True, exist_ok=True)

    rosters: dict[int, list[dict[str, Any]]] = {}
    for year in roster_years:
        response = client.get("/roster", year=year, classification="fbs")
        if not isinstance(response, list):
            raise DataError(f"Unexpected CFBD roster response for {year}")
        rosters[year] = response
        _write_raw_json(raw_path, f"roster_{year}.json", response)

    rows: list[dict[str, Any]] = []
    drafted_matches = 0
    for draft_year in draft_years:
        college_year = draft_year - 1
        stats_response = client.get(
            "/stats/player/season",
            year=college_year,
            endWeek=as_of_week,
            seasonType="both",
        )
        picks_response = client.get("/draft/picks", year=draft_year)
        if not isinstance(stats_response, list) or not isinstance(picks_response, list):
            raise DataError(f"Unexpected CFBD history response for draft {draft_year}")
        _write_raw_json(raw_path, f"stats_{college_year}_through_week_{as_of_week:02d}.json", stats_response)
        _write_raw_json(raw_path, f"draft_picks_{draft_year}.json", picks_response)

        prior_roster = {_record_id(item): item for item in rosters[college_year] if _record_id(item)}
        following_ids = {_record_id(item) for item in rosters[draft_year] if _record_id(item)}
        leaver_ids = set(prior_roster) - following_ids
        pick_by_id = {
            str(_first(item, "collegeAthleteId", "college_athlete_id") or ""): item
            for item in picks_response
            if _first(item, "collegeAthleteId", "college_athlete_id") not in (None, "")
        }
        pick_by_name_team = {
            (normalize_name(_first(item, "name")), normalize_name(_first(item, "collegeTeam", "college_team"))): item
            for item in picks_response
        }
        grouped = _group_player_season_stats(stats_response, allowed_ids=leaver_ids)

        for player_id, group in grouped.items():
            roster = prior_roster[player_id]
            name = group.get("name") or _full_roster_name(roster)
            team = group.get("team") or _first(roster, "team")
            position = normalize_position(group.get("position") or _first(roster, "position"))
            if position not in POSITION_GROUPS:
                continue
            overview = _overview_from_stat_group(group)
            record: dict[str, Any] = {
                "player_id": player_id,
                "cfbd_player_id": player_id,
                "name": name,
                "position": position,
                "school": team,
                "conference": group.get("conference"),
                "season": college_year,
                "draft_year": draft_year,
                "as_of_week": as_of_week,
                "height_in": _first(roster, "height"),
                "weight_lb": _first(roster, "weight"),
                "measurement_source": "school_roster",
                "measurements_verified": False,
                "population": "FBS players with recorded production who left college after the season",
                "probability_kind": "conditional_on_entry",
                "probability_condition": (
                    "P(drafted | leaves or declares after season, recorded production)"
                ),
            }
            _merge_overview_features(record, overview)
            if not any(key.startswith("prod_") and parse_number(value) is not None for key, value in record.items()):
                continue
            pick = pick_by_id.get(player_id) or pick_by_name_team.get((normalize_name(name), normalize_name(team)))
            overall = parse_number(_first(pick, "overall")) if pick else None
            round_number = parse_number(_first(pick, "round")) if pick else None
            record.update(
                {
                    "drafted": pick is not None,
                    "draft_ovr": overall,
                    "draft_round": round_number,
                    "elite": bool(overall is not None and overall <= 64),
                    "nfl_team": _first(pick, "nflTeam", "nfl_team") if pick else None,
                    "outcome_match": "cfbd_player_id" if player_id in pick_by_id else ("name_team" if pick else "undrafted"),
                }
            )
            if pick:
                drafted_matches += 1
            rows.append(normalize_record(record))
    metadata = {
        "source": "CollegeFootballData week-matched history",
        "population": "FBS players with recorded production who left college after the season",
        "probability_kind": "conditional_on_entry",
        "probability_condition": "P(drafted | leaves or declares after season, recorded production)",
        "window_start": first_draft,
        "window_end": target_season,
        "as_of_week": as_of_week,
        "rows": len(rows),
        "drafted_rows": sum(bool(row.get("drafted")) for row in rows),
        "drafted_id_or_name_matches": drafted_matches,
        "built_at": timestamp_utc(),
        "limitations": [
            "Risk-set membership is inferred from absence on the following FBS roster.",
            "Players without a recorded box-score statistic are excluded.",
            "The probability is conditional on leaving/declaring, not the probability of declaring.",
        ],
    }
    return rows, metadata


def _first(row: Mapping[str, Any] | None, *keys: str) -> Any:
    if not row:
        return None
    for key in keys:
        if row.get(key) not in (None, ""):
            return row.get(key)
    return None


def _record_id(row: Mapping[str, Any]) -> str:
    return str(_first(row, "id", "playerId", "player_id") or "")


def _full_roster_name(row: Mapping[str, Any]) -> str:
    first = _first(row, "firstName", "first_name") or ""
    last = _first(row, "lastName", "last_name") or ""
    return f"{first} {last}".strip()


def _games_from_categories(categories: Mapping[str, list[Mapping[str, Any]]]) -> float:
    for category, stats in categories.items():
        for stat in stats:
            if slug(stat.get("name")) in {"g", "gp", "games", "games_played"}:
                value = _parse_stat_value(stat.get("value"))
                if value is not None:
                    return value
    return 0.0


def _group_player_season_stats(
    rows: Iterable[Mapping[str, Any]],
    *,
    allowed_ids: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Convert CFBD's long player-season rows into one auditable group per player."""
    grouped: dict[str, dict[str, Any]] = {}
    for stat_row in rows:
        player_id = str(_first(stat_row, "playerId", "player_id") or "")
        if not player_id or (allowed_ids is not None and player_id not in allowed_ids):
            continue
        group = grouped.setdefault(
            player_id,
            {
                "player_id": player_id,
                "name": _first(stat_row, "player", "name"),
                "team": _first(stat_row, "team"),
                "conference": _first(stat_row, "conference"),
                "position": _first(stat_row, "position"),
                "categories": {},
            },
        )
        category = str(_first(stat_row, "category") or "unknown")
        stat_name = str(_first(stat_row, "statType", "stat_type") or "unknown")
        group["categories"].setdefault(category, []).append(
            {"name": stat_name, "value": _first(stat_row, "stat", "value")}
        )
    return grouped


def _group_player_success(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Index CFBD success rows without guessing across player identities."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw in rows:
        player_id = str(_first(raw, "id", "playerId", "player_id") or "").strip()
        if not player_id:
            continue
        grouped.setdefault(player_id, []).append(dict(raw))
    return grouped


def _select_player_success(
    grouped: Mapping[str, list[dict[str, Any]]],
    player_id: str | int,
    *,
    team: object = None,
) -> dict[str, Any]:
    """Choose the same-team row, then the row with the most opportunities."""

    options = list(grouped.get(str(player_id), ()))
    if not options:
        return {}
    wanted_team = normalize_name(team)
    if wanted_team:
        same_team = [row for row in options if normalize_name(row.get("team")) == wanted_team]
        if same_team:
            options = same_team

    def opportunities(row: Mapping[str, Any]) -> float:
        passing = row.get("passing") or {}
        rushing = row.get("rushing") or {}
        return float(parse_number(passing.get("plays")) or 0.0) + float(
            parse_number(rushing.get("plays")) or 0.0
        )

    return max(options, key=opportunities)


def _overview_from_stat_group(group: Mapping[str, Any] | None) -> dict[str, Any]:
    categories = dict(group.get("categories") or {}) if group else {}
    return {
        "id": group.get("player_id") if group else None,
        "name": group.get("name") if group else None,
        "team": group.get("team") if group else None,
        "conference": group.get("conference") if group else None,
        "position": group.get("position") if group else None,
        "games": _games_from_categories(categories),
        "boxScoreStats": {
            "categories": [
                {"name": category, "stats": stats}
                for category, stats in categories.items()
            ]
        },
    }


_CFBD_GENERATED_PRODUCTION_FIELDS = CFBD_GENERATED_PRODUCTION_FIELDS


def _clear_cfbd_generated_features(record: dict[str, Any]) -> None:
    """Prevent a prior, later snapshot from leaking into a bounded refresh."""
    for key in list(record):
        if key.startswith("cfbd_") or key.startswith("observed_") or key in _CFBD_GENERATED_PRODUCTION_FIELDS:
            record.pop(key, None)


def _write_raw_json(directory: Path | None, filename: str, payload: Any) -> None:
    if directory is None:
        return
    (directory / filename).write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8"
    )


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _retry_after_seconds(error: HTTPError) -> float | None:
    raw = error.headers.get("Retry-After") if error.headers is not None else None
    try:
        parsed = float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(raw))
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(
            0.0,
            (retry_at - datetime.now(timezone.utc)).total_seconds(),
        )
    return max(0.0, parsed) if parsed is not None else None


def _validate_cfbd_base_url(
    base_url: str,
    *,
    allow_insecure_localhost: bool,
) -> str:
    value = str(base_url or "").strip().rstrip("/")
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if not host or parsed.username is not None or parsed.password is not None:
        raise DataError("CFBD base URL must contain a host and no embedded credentials")
    if parsed.query or parsed.fragment:
        raise DataError("CFBD base URL cannot contain a query string or fragment")
    if parsed.scheme.casefold() == "https":
        return value
    loopback = host.casefold() == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if (
        parsed.scheme.casefold() == "http"
        and allow_insecure_localhost
        and loopback
    ):
        return value
    raise DataError(
        "CFBD base URL must use HTTPS. Plain HTTP is allowed only for an "
        "explicitly enabled localhost test endpoint."
    )


class CFBDClient:
    supports_player_success = True
    supports_recruiting = True

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = CFBD_BASE_URL,
        timeout: int = 45,
        max_retries: int = 4,
        min_request_interval: float = 0.12,
        max_response_bytes: int = _MAX_CFBD_RESPONSE_BYTES,
        allow_insecure_localhost: bool = False,
        opener: Callable[..., Any] | None = None,
    ):
        raw_key = resolve_cfbd_api_key(api_key)
        key_status = cfbd_api_key_status(raw_key)
        if key_status == "missing":
            raise DataError(
                "CFBD_API_KEY is not set. The CFBD-only `lookup` command, `discover --provider cfbd`, "
                "and an update configured for CFBD require a CollegeFootballData key. On macOS run "
                "`draftscope configure-key`, or set CFBD_API_KEY for this shell, then run `draftscope doctor`. "
                "The default SportsDataverse discovery and weekly update need no credential, and neither "
                "does `draftscope add`."
            )
        if key_status == "placeholder":
            raise DataError(
                "CFBD_API_KEY still contains a placeholder, not a usable credential. Replace it with your "
                "real CollegeFootballData key, then run `draftscope doctor`, or use the default free "
                "SportsDataverse provider."
            )
        self.api_key = str(raw_key).strip()
        self.base_url = _validate_cfbd_base_url(
            base_url,
            allow_insecure_localhost=allow_insecure_localhost,
        )
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.min_request_interval = max(0.0, float(min_request_interval))
        self.max_response_bytes = max(1, int(max_response_bytes))
        self._opener = opener
        self._last_request_at: float | None = None

    def get(self, path: str, **params: Any) -> Any:
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise DataError("CFBD request path must be an origin-relative path")
        cleaned = {key: value for key, value in params.items() if value is not None}
        url = f"{self.base_url}{path}"
        if cleaned:
            url += "?" + urlencode(cleaned)
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "DraftScope/0.1 (+local research tool)",
            },
        )
        # urllib intentionally omits unredirected headers when it constructs a
        # redirect request, so a bearer credential cannot cross origins.
        request.add_unredirected_header(
            "Authorization", f"Bearer {self.api_key}"
        )
        for attempt in range(self.max_retries + 1):
            self._pace_request()
            try:
                open_request = self._opener or urlopen
                with open_request(request, timeout=self.timeout) as response:
                    payload = _read_response_limited(
                        response,
                        max_bytes=self.max_response_bytes,
                        source="CFBD API",
                    )
                return json.loads(payload.decode("utf-8"))
            except HTTPError as exc:
                retry_after = _retry_after_seconds(exc)
                detail = _http_error_detail(exc)[:400]
                retryable = exc.code == 429 or 500 <= exc.code <= 504
                if retryable and attempt < self.max_retries:
                    base = 2.0 if exc.code == 429 else 1.0
                    delay = retry_after if retry_after is not None else min(8.0, base * (2**attempt))
                    time.sleep(max(0.0, min(15.0, delay)))
                    continue
                raise DataError(
                    f"CFBD request failed ({exc.code}) for {path} after {attempt + 1} attempt(s): {detail}"
                ) from exc
            except (URLError, TimeoutError) as exc:
                if attempt < self.max_retries:
                    time.sleep(min(8.0, 1.0 * (2**attempt)))
                    continue
                raise DataError(
                    f"CFBD request failed for {path} after {attempt + 1} attempt(s): {exc}"
                ) from exc
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DataError(f"CFBD returned invalid JSON for {path}: {exc}") from exc
        raise DataError(f"CFBD request failed for {path}")

    def _pace_request(self) -> None:
        if self._last_request_at is not None and self.min_request_interval:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.min_request_interval:
                time.sleep(self.min_request_interval - elapsed)
        self._last_request_at = time.monotonic()

    def search_player(self, name: str, *, year: int, team: str | None = None) -> dict[str, Any]:
        results = self.get("/player/search", searchTerm=name, year=year, team=team)
        if not isinstance(results, list) or not results:
            raise DataError(f"No CFBD player matched {name!r} for {year}")
        wanted = normalize_name(name)
        ranked: list[tuple[float, dict[str, Any]]] = []
        for item in results:
            score = SequenceMatcher(None, wanted, normalize_name(item.get("name"))).ratio()
            if team and normalize_name(item.get("team")) == normalize_name(team):
                score += 0.25
            ranked.append((score, item))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.03 and not team:
            choices = ", ".join(f"{item.get('name')} ({item.get('team')})" for _, item in ranked[:5])
            raise DataError(f"Player name is ambiguous; add --team. Matches: {choices}")
        if ranked[0][0] < 0.55:
            raise DataError(f"No sufficiently close CFBD player match for {name!r}")
        return ranked[0][1]

    def season_overview(self, player_id: str | int, *, year: int) -> dict[str, Any]:
        result = self.get("/player/season/overview", year=year, playerId=player_id)
        if not isinstance(result, dict):
            raise DataError(f"Unexpected CFBD season-overview response for player {player_id}")
        return result

    def player_stats_through_week(
        self,
        player_id: str | int,
        *,
        year: int,
        end_week: int,
        team: str | None = None,
    ) -> dict[str, Any]:
        """Return one player's long-form season stats aggregated through a completed week."""
        if end_week < 0:
            raise DataError("end_week must be zero or greater")
        if end_week == 0:
            return _overview_from_stat_group(None)
        response = self.get(
            "/stats/player/season",
            year=year,
            endWeek=end_week,
            seasonType="both",
            team=team,
        )
        if not isinstance(response, list):
            raise DataError(f"Unexpected CFBD player-season stats response for {year}")
        wanted_id = str(player_id)
        grouped = _group_player_season_stats(response, allowed_ids={wanted_id})
        return _overview_from_stat_group(grouped.get(wanted_id))

    def player_success_through_week(
        self,
        player_id: str | int,
        *,
        year: int,
        end_week: int,
        team: str | None = None,
    ) -> dict[str, Any]:
        """Return one player's checkpoint-bounded passing/rushing success row."""

        if end_week < 0:
            raise DataError("end_week must be zero or greater")
        if end_week == 0:
            return {}
        response = self.get(
            "/stats/player/success",
            year=year,
            endWeek=end_week,
            seasonType="both",
            team=team,
            playerId=player_id,
            excludeGarbageTime=False,
            threshold=0,
        )
        if not isinstance(response, list):
            raise DataError(f"Unexpected CFBD player-success response for {year}")
        wanted_id = str(player_id)
        matches = [
            dict(row)
            for row in response
            if str(row.get("id") or row.get("playerId") or "") == wanted_id
        ]
        if team:
            same_team = [
                row
                for row in matches
                if normalize_name(row.get("team")) == normalize_name(team)
            ]
            if same_team:
                matches = same_team
        return matches[0] if matches else {}

    def current_week(self, *, year: int, now: datetime | None = None) -> int:
        calendar = self.get("/calendar", year=year)
        if not isinstance(calendar, list) or not calendar:
            return 0
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        else:
            moment = moment.astimezone(timezone.utc)
        last_completed = 0
        for item in calendar:
            try:
                end = datetime.fromisoformat(str(item["endDate"]).replace("Z", "+00:00"))
                week = int(item["week"])
            except (KeyError, TypeError, ValueError):
                continue
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            else:
                end = end.astimezone(timezone.utc)
            if week >= 0 and end <= moment:
                last_completed = max(last_completed, week)
        return last_completed

    def player_record(self, name: str, *, year: int, team: str | None = None) -> dict[str, Any]:
        item = self.search_player(name, year=year, team=team)
        record = {
            "cfbd_player_id": item.get("id"),
            "player_id": item.get("id"),
            "name": item.get("name"),
            "position": item.get("position"),
            "school": item.get("team"),
            "season": year,
            "projected_draft_year": year + 1,
            "height_in": item.get("height"),
            "weight_lb": item.get("weight"),
            "measurement_source": "school_roster",
            "measurements_verified": False,
            "last_refreshed_at": timestamp_utc(),
        }
        return normalize_record(record)

    def refresh_candidate(
        self,
        candidate: Mapping[str, Any],
        *,
        season: int,
        week: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        current = dict(candidate)
        resolved_week = int(week) if week is not None else self.current_week(year=season)
        if resolved_week < 0:
            raise DataError("week must be zero or greater")
        player_id = current.get("cfbd_player_id")
        roster: dict[str, Any] | None = None
        if not player_id:
            roster = self.search_player(str(current.get("name") or ""), year=season, team=current.get("school"))
            player_id = roster.get("id")
            if player_id in (None, ""):
                raise DataError(f"CFBD player match for {current.get('name')!r} did not include an id")
            current["cfbd_player_id"] = player_id
            current["player_id"] = current.get("player_id") or player_id
        stats_team = _first(roster, "team") or current.get("school")
        overview = self.player_stats_through_week(
            player_id,
            year=season,
            end_week=resolved_week,
            team=str(stats_team) if stats_team else None,
        )
        success: dict[str, Any] = {}
        success_warning: str | None = None
        try:
            success = self.player_success_through_week(
                player_id,
                year=season,
                end_week=resolved_week,
                team=str(stats_team) if stats_team else None,
            )
        except DataError as exc:
            # This endpoint is valuable but optional. A temporary outage must
            # not make otherwise checkpoint-safe box-score production stale.
            success_warning = str(exc)
        current["name"] = _first(roster, "name") or current.get("name") or overview.get("name")
        current["position"] = normalize_position(
            _first(roster, "position") or current.get("position") or overview.get("position")
        )
        current["school"] = _first(roster, "team") or overview.get("team") or current.get("school")
        current["conference"] = overview.get("conference") or current.get("conference")
        current["season"] = season
        current["projected_draft_year"] = current.get("projected_draft_year") or season + 1
        current["as_of_week"] = resolved_week
        current["last_refreshed_at"] = timestamp_utc()
        if roster and str(current.get("measurement_source") or "") in {"", "school_roster"}:
            current["height_in"] = _first(roster, "height") or current.get("height_in")
            current["weight_lb"] = _first(roster, "weight") or current.get("weight_lb")
            current["measurement_source"] = "school_roster"
            current["measurements_verified"] = False
        _clear_cfbd_generated_features(current)
        for key in ("player_success_source", "player_success_warning"):
            current.pop(key, None)
        _merge_overview_features(current, overview)
        success_features = derive_player_success(success)
        current.update(success_features)
        if success_features:
            current["player_success_source"] = "cfbd_stats_player_success"
        elif success_warning:
            current["player_success_warning"] = success_warning
        current["production_source"] = "cfbd_stats_player_season"
        current["production_season"] = season
        current["production_as_of_week"] = resolved_week
        raw = dict(overview)
        raw["playerSuccess"] = success
        if success_warning:
            raw["playerSuccessWarning"] = success_warning
        return normalize_record(current), raw


def _parse_stat_value(value: object) -> float | None:
    if isinstance(value, str) and "/" in value:
        return None
    try:
        return parse_number(value)
    except DataError:
        match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
        return float(match.group()) if match else None


def _box_stats(overview: Mapping[str, Any]) -> dict[tuple[str, str], object]:
    result: dict[tuple[str, str], object] = {}
    box = overview.get("boxScoreStats") or {}
    for category in box.get("categories") or []:
        category_name = slug(category.get("name"))
        for stat in category.get("stats") or []:
            result[(category_name, slug(stat.get("name")))] = stat.get("value")
    return result


def _merge_overview_features(target: dict[str, Any], overview: Mapping[str, Any]) -> None:
    games = parse_number(overview.get("games")) or 0.0
    target["cfbd_games"] = games
    stats = _box_stats(overview)
    for (category, key), value in stats.items():
        target[f"cfbd_{category}_{key}"] = value
    target.update(
        derive_college_production(
            stats,
            games=games,
            usage=overview.get("usage") or {},
            ppa=overview.get("ppa") or {},
        )
    )
    target.update(derive_features(target))


def _discovery_class_readiness_score(value: object) -> tuple[float, bool]:
    """Return a transparent eligibility/readiness prior for roster-only triage."""

    try:
        class_number = parse_number(value)
    except DataError:
        class_number = None
    if class_number is None:
        class_number = {
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
        }.get(re.sub(r"[^a-z0-9]", "", str(value or "").lower()))
    if class_number is None:
        return 0.0, False

    # Piecewise interpolation keeps the assumption readable: this is an
    # eligibility/readiness prior, not a claim about on-field quality.
    anchors = ((1.0, 0.0), (2.0, 25.0), (3.0, 55.0), (4.0, 82.0), (5.0, 100.0))
    if class_number <= anchors[0][0]:
        return anchors[0][1], True
    if class_number >= anchors[-1][0]:
        return anchors[-1][1], True
    for (lower_year, lower_score), (upper_year, upper_score) in zip(anchors, anchors[1:]):
        if lower_year <= class_number <= upper_year:
            fraction = (class_number - lower_year) / (upper_year - lower_year)
            return lower_score + fraction * (upper_score - lower_score), True
    return 0.0, False


def discover_cfbd_candidates(
    client: "CFBDClient",
    *,
    season: int,
    as_of_week: int | None = None,
    limit: int | None = None,
    preseason_prior_year: bool = True,
    raw_dir: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    """Discover and rank the national FBS prospect pool from CFBD bulk data.

    The discovery score is the unweighted mean of the player's available
    position-specific production metric percentiles among current-roster FBS
    peers when those metrics exist. Percentiles use empirical midranks;
    lower-is-better metrics are inverted. Players without usable box-score
    production remain in the pool and sort after production-ranked peers using
    an explicitly labeled roster-only fallback: 80% class readiness and 20%
    height/weight completeness. Neither score is a draft probability.

    Before the first completed week, ``preseason_prior_year=True`` joins the
    current roster to the prior completed season's stats by CFBD player id (with
    a unique normalized-name fallback for source id gaps).
    """
    if limit is not None and limit < 0:
        raise DataError("limit must be zero or greater")
    resolved_week = int(as_of_week) if as_of_week is not None else client.current_week(year=season)
    if resolved_week < 0:
        raise DataError("as_of_week must be zero or greater")

    roster_response = client.get("/roster", year=season, classification="fbs")
    if not isinstance(roster_response, list):
        raise DataError(f"Unexpected CFBD FBS roster response for {season}")
    raw_path = Path(raw_dir) if raw_dir else None
    if raw_path:
        raw_path.mkdir(parents=True, exist_ok=True)
    _write_raw_json(raw_path, f"roster_fbs_{season}.json", roster_response)

    stats_year = season
    stats_week: int | None = resolved_week
    preseason = resolved_week == 0
    if preseason:
        if not preseason_prior_year:
            return []
        stats_year = season - 1
        stats_week = None

    stats_params: dict[str, Any] = {"year": stats_year, "seasonType": "both"}
    if stats_week is not None:
        stats_params["endWeek"] = stats_week
    stats_response = client.get("/stats/player/season", **stats_params)
    if not isinstance(stats_response, list):
        raise DataError(f"Unexpected CFBD player-season stats response for {stats_year}")
    checkpoint = "complete" if stats_week is None else f"week_{stats_week:02d}"
    _write_raw_json(raw_path, f"player_stats_{stats_year}_{checkpoint}.json", stats_response)
    grouped = _group_player_season_stats(stats_response)
    success_by_id: dict[str, list[dict[str, Any]]] = {}
    if getattr(client, "supports_player_success", False):
        success_params: dict[str, Any] = {
            "year": stats_year,
            "seasonType": "both",
            "excludeGarbageTime": False,
            "threshold": 0,
        }
        if stats_week is not None:
            success_params["endWeek"] = stats_week
        try:
            success_response = client.get("/stats/player/success", **success_params)
            if not isinstance(success_response, list):
                raise DataError(
                    f"Unexpected CFBD player-success response for {stats_year}"
                )
            _write_raw_json(
                raw_path,
                f"player_success_{stats_year}_{checkpoint}.json",
                success_response,
            )
            success_by_id = _group_player_success(success_response)
        except DataError:
            # Discovery remains complete when the optional success tier is
            # unavailable; the absent metrics simply do not affect ranking.
            success_by_id = {}

    recruiting_rows: list[dict[str, Any]] = []
    if getattr(client, "supports_recruiting", False):
        # A six-class lookback covers the NCAA eligibility tail. Recruiting
        # grades predate the checkpoint, so they are safe historical inputs.
        for recruit_year in range(season - 6, season + 1):
            try:
                response = client.get("/recruiting/players", year=recruit_year)
                if not isinstance(response, list):
                    raise DataError(
                        f"Unexpected CFBD recruiting response for {recruit_year}"
                    )
                recruiting_rows.extend(dict(row) for row in response)
                _write_raw_json(
                    raw_path,
                    f"recruiting_players_{recruit_year}.json",
                    response,
                )
            except DataError:
                # Optional pedigree evidence cannot make production discovery
                # stale when a recruiting partition is unavailable.
                continue
    recruiting_index = build_recruiting_index(recruiting_rows)

    roster_by_id = {
        _record_id(item): item
        for item in roster_response
        if isinstance(item, Mapping) and _record_id(item)
    }
    roster_by_name: dict[str, list[Mapping[str, Any]]] = {}
    for item in roster_response:
        if not isinstance(item, Mapping):
            continue
        roster_name = _first(item, "name") or _full_roster_name(item)
        if roster_name:
            roster_by_name.setdefault(normalize_name(roster_name), []).append(item)

    candidates: list[dict[str, Any]] = []
    used_roster_ids: set[str] = set()
    for stats_player_id, group in grouped.items():
        roster = roster_by_id.get(stats_player_id)
        match_method = "cfbd_player_id"
        if roster is None:
            name_matches = roster_by_name.get(normalize_name(group.get("name")), [])
            if len(name_matches) != 1:
                continue
            roster = name_matches[0]
            match_method = "unique_normalized_name"
        roster_id = _record_id(roster)
        if not roster_id or roster_id in used_roster_ids:
            continue
        position = normalize_position(_first(roster, "position") or group.get("position"))
        if position not in POSITION_GROUPS:
            continue
        used_roster_ids.add(roster_id)
        current_school = _first(roster, "team", "school")
        production_school = group.get("team")
        same_school = normalize_name(current_school) == normalize_name(production_school)
        overview = _overview_from_stat_group(group)
        record: dict[str, Any] = {
            "player_id": roster_id,
            "cfbd_player_id": roster_id,
            "name": _first(roster, "name") or _full_roster_name(roster) or group.get("name"),
            "position": position,
            "school": current_school or production_school,
            "conference": group.get("conference") if same_school else None,
            "season": season,
            "projected_draft_year": season + 1,
            "as_of_week": resolved_week,
            "height_in": _first(roster, "height"),
            "weight_lb": _first(roster, "weight"),
            "class_year": _first(roster, "year", "class", "classYear", "class_year"),
            "jersey": _first(roster, "jersey"),
            "measurement_source": "school_roster",
            "measurements_verified": False,
            "production_source": "cfbd_stats_player_season",
            "production_season": stats_year,
            "production_as_of_week": stats_week,
            "production_school": production_school,
            "production_conference": group.get("conference"),
            "has_recorded_stats": 1.0,
            "discovery_match": match_method,
            "discovery_preseason_prior_year": preseason,
        }
        record.update(
            match_recruiting_profile(
                roster,
                recruiting_index,
                roster_season=season,
            )
        )
        _merge_overview_features(record, overview)
        success_features = derive_player_success(
            _select_player_success(
                success_by_id,
                stats_player_id,
                team=production_school,
            )
        )
        record.update(success_features)
        if success_features:
            record["player_success_source"] = "cfbd_stats_player_success"
        candidates.append(normalize_record(record))

    # CFBD's player-season endpoint has no universal row for offensive linemen,
    # specialists, backups, and some transfers. The current FBS roster is the
    # discovery denominator, so retain those players instead of silently
    # treating a missing box-score row as evidence that they are not prospects.
    for roster in roster_response:
        if not isinstance(roster, Mapping):
            continue
        roster_id = _record_id(roster)
        if not roster_id or roster_id in used_roster_ids:
            continue
        position = normalize_position(_first(roster, "position"))
        if position not in POSITION_GROUPS:
            continue
        name = _first(roster, "name") or _full_roster_name(roster)
        if not name:
            continue
        used_roster_ids.add(roster_id)
        roster_only_record: dict[str, Any] = {
            "player_id": roster_id,
            "cfbd_player_id": roster_id,
            "name": name,
            "position": position,
            "school": _first(roster, "team", "school"),
            "conference": _first(roster, "conference"),
            "season": season,
            "projected_draft_year": season + 1,
            "as_of_week": resolved_week,
            "height_in": _first(roster, "height"),
            "weight_lb": _first(roster, "weight"),
            "class_year": _first(
                roster, "year", "class", "classYear", "class_year"
            ),
            "jersey": _first(roster, "jersey"),
            "measurement_source": "school_roster",
            "measurements_verified": False,
            "production_source": None,
            "production_season": None,
            "production_as_of_week": None,
            "production_school": None,
            "production_conference": None,
            "has_recorded_stats": 0.0,
            "discovery_match": "current_roster_only",
            "discovery_preseason_prior_year": preseason,
        }
        roster_only_record.update(
            match_recruiting_profile(
                roster,
                recruiting_index,
                roster_season=season,
            )
        )
        candidates.append(normalize_record(roster_only_record))

    peer_values: dict[tuple[str, str], list[float]] = {}
    for candidate in candidates:
        position = normalize_position(candidate.get("position"))
        for key, _direction in PRODUCTION.get(position, ()):
            value = parse_number(candidate.get(f"prod_{key}"))
            if value is not None:
                peer_values.setdefault((position, key), []).append(value)

    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        position = normalize_position(candidate.get("position"))
        metric_scores: dict[str, float] = {}
        for key, direction in PRODUCTION.get(position, ()):
            value = parse_number(candidate.get(f"prod_{key}"))
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
            candidate["discovery_fallback_components"] = None
            candidate["discovery_score"] = round(
                sum(metric_scores.values()) / len(metric_scores), 3
            )
        else:
            class_score, class_known = _discovery_class_readiness_score(
                candidate.get("class_year")
            )
            measurement_count = sum(
                parse_number(candidate.get(key)) is not None
                for key in ("height_in", "weight_lb")
            )
            measurement_completeness = measurement_count * 50.0
            candidate["discovery_has_usable_production"] = 0.0
            candidate["discovery_evidence_tier"] = "roster_metadata_only"
            candidate["discovery_score_kind"] = "roster_metadata_fallback_not_draft_probability"
            candidate["discovery_score_method"] = (
                "80pct_class_readiness_plus_20pct_height_weight_completeness"
            )
            candidate["discovery_fallback_reason"] = (
                "no_mapped_position_production_metrics"
                if parse_number(candidate.get("has_recorded_stats"))
                else "no_player_season_box_score_rows"
            )
            candidate["discovery_fallback_components"] = {
                "class_readiness": round(class_score, 3) if class_known else None,
                "height_weight_completeness": measurement_completeness,
            }
            candidate["discovery_score"] = round(
                0.80 * class_score + 0.20 * measurement_completeness,
                3,
            )
        ranked.append(candidate)

    ranked.sort(
        key=lambda item: (
            -int(bool(parse_number(item.get("discovery_has_usable_production")))),
            -float(item["discovery_score"]),
            -int(item["discovery_metric_count"]),
            normalize_name(item.get("name")),
        )
    )
    position_ranks: dict[str, int] = {}
    for rank, candidate in enumerate(ranked, start=1):
        candidate["discovery_rank"] = rank
        position = normalize_position(candidate.get("position"))
        position_ranks[position] = position_ranks.get(position, 0) + 1
        candidate["discovery_position_rank"] = position_ranks[position]
    return ranked if limit is None else ranked[:limit]

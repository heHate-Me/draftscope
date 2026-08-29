"""Comparison-only historical NFL honor data from licensed Wikipedia pages.

The module deliberately has no dependency on DraftScope's model or networking
code.  Callers supply the same small JSON-request callback used by their data
layer.  Parsed honors are reference outcomes: they must never be used as model
features or training labels.

Wikipedia's annual All-Pro pages contain several selectors.  This loader uses
Rock Island Argus (1920), Buffalo Evening News (1921), Canton Daily News
(1922), Green Bay Press-Gazette (1923-30), United Press/UPI (1931-39),
Associated Press (1940-45), the NFL-only United Press team while AP combined
NFL and AAFC players (1946-49), and Associated Press again from 1950 onward.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote, unquote, urlencode


WIKIPEDIA_API_URL = "https://en.wikipedia.org/w/api.php"
WIKIPEDIA_BASE_URL = "https://en.wikipedia.org"

PRO_BOWL_PAGE_TITLES = (
    "List of Pro Bowl players, A",
    "List of Pro Bowl players, B",
    "List of Pro Bowl players, C–F",
    "List of Pro Bowl players, G–H",
    "List of Pro Bowl players, I–K",
    "List of Pro Bowl players, L–M",
    "List of Pro Bowl players, N–R",
    "List of Pro Bowl players, S–V",
    "List of Pro Bowl players, W–Z",
)

_PRO_BOWL_EXPECTED_TABLES = {
    "List of Pro Bowl players, A": 1,
    "List of Pro Bowl players, B": 1,
    "List of Pro Bowl players, C–F": 4,
    "List of Pro Bowl players, G–H": 2,
    "List of Pro Bowl players, I–K": 3,
    "List of Pro Bowl players, L–M": 2,
    "List of Pro Bowl players, N–R": 5,
    "List of Pro Bowl players, S–V": 4,
    "List of Pro Bowl players, W–Z": 3,
}

# Wikipedia's annual roster pages use several distinct layouts.  Only years
# whose pages publish a complete roster are included here; the intervening
# gaps are intentionally left unverified rather than inferred from game logs.
_ANNUAL_PRO_BOWL_SCHEMAS: dict[int, tuple[str, int, int, int]] = {
    1950: ("conference_columns", 1, 60, 68),
    **{
        year: ("conference_tables", 6, 75, 105)
        for year in range(1971, 1975)
    },
    **{
        year: ("conference_columns", 3, 75, 100)
        for year in range(1975, 1977)
    },
    **{
        year: ("grouped_tables", 2, 70, 100)
        for year in range(1977, 1979)
    },
    **{
        year: ("conference_tables", 6, 70, 115)
        for year in range(1980, 1994)
    },
    1994: ("column_lists", 1, 85, 105),
    1995: ("conference_tables", 6, 75, 110),
    1996: ("column_lists", 1, 80, 100),
    **{
        year: ("conference_tables", 6, 80, 135)
        for year in range(1999, 2013)
    },
    **{
        year: ("grouped_tables", 3, 100, 145)
        for year in range(2013, 2016)
    },
    **{
        year: ("conference_tables", 6, 80, 140)
        for year in range(2016, 2022)
    },
}

_ANNUAL_PRO_BOWL_KNOWN_UNAVAILABLE = frozenset(
    {
        *range(1951, 1971),
        1979,
        1997,
        1998,
    }
)

ALL_PRO_FIRST_SEASON = 1920
_EXPLICIT_BOLD_AP_YEARS = frozenset({1962, 1963, 1964, 1965, 1967})
_INFERRED_BOLD_AP_YEARS = frozenset({1966})

_AAFC_TEAMS_BY_YEAR = {
    1946: frozenset(
        {
            "brooklyn dodgers",
            "buffalo bisons",
            "chicago rockets",
            "cleveland browns",
            "los angeles dons",
            "miami seahawks",
            "new york yankees",
            "san francisco 49ers",
        }
    ),
    1947: frozenset(
        {
            "baltimore colts",
            "brooklyn dodgers",
            "buffalo bills",
            "chicago rockets",
            "cleveland browns",
            "los angeles dons",
            "new york yankees",
            "san francisco 49ers",
        }
    ),
    1948: frozenset(
        {
            "baltimore colts",
            "brooklyn dodgers",
            "buffalo bills",
            "chicago rockets",
            "cleveland browns",
            "los angeles dons",
            "new york yankees",
            "san francisco 49ers",
        }
    ),
    1949: frozenset(
        {
            "baltimore colts",
            "buffalo bills",
            "chicago hornets",
            "cleveland browns",
            "new york yankees",
            "san francisco 49ers",
        }
    ),
}


class HistoricalHonorsError(ValueError):
    """Raised when a source page cannot be interpreted without guessing."""


@dataclass
class _Link:
    href: str
    title: str
    text: str
    bold: bool


@dataclass
class _Segment:
    parts: list[str] = field(default_factory=list)
    links: list[_Link] = field(default_factory=list)
    has_bold_text: bool = False
    reference_labels: set[str] = field(default_factory=set)

    @property
    def text(self) -> str:
        return " ".join("".join(self.parts).split())


@dataclass
class _Cell:
    tag: str
    attrs: dict[str, str]
    parts: list[str] = field(default_factory=list)
    links: list[_Link] = field(default_factory=list)
    segments: list[_Segment] = field(default_factory=lambda: [_Segment()])

    @property
    def text(self) -> str:
        return " ".join("".join(self.parts).split())

    def boundary(self) -> None:
        if self.segments[-1].text or self.segments[-1].links:
            self.segments.append(_Segment())

    def nonempty_segments(self) -> list[_Segment]:
        return [segment for segment in self.segments if segment.text or segment.links]


@dataclass
class _Table:
    heading: str
    classes: frozenset[str] = field(default_factory=frozenset)
    rows: list[list[_Cell]] = field(default_factory=list)
    row: list[_Cell] | None = None
    cell: _Cell | None = None


@dataclass
class _ListItem:
    heading: str
    parts: list[str] = field(default_factory=list)
    links: list[_Link] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join("".join(self.parts).split())


class _WikipediaHonorsHTMLParser(HTMLParser):
    """Collect semantic table/list records while retaining link and bold state."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[_Table] = []
        self.list_items: list[_ListItem] = []
        self.document_parts: list[str] = []
        self._table_stack: list[_Table | None] = []
        self._heading_tag = ""
        self._heading_level = 0
        self._heading_parts: list[str] = []
        self._headings: dict[int, str] = {}
        self._list_item: _ListItem | None = None
        self._bold_depth = 0
        self._ignored_depth = 0
        self._anchor: dict[str, Any] | None = None

    @property
    def _table(self) -> _Table | None:
        return self._table_stack[-1] if self._table_stack else None

    @property
    def _heading(self) -> str:
        return " / ".join(
            self._headings[level]
            for level in sorted(self._headings)
            if self._headings[level]
        )

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag in {"style", "script", "sup"}:
            if tag == "sup":
                table = self._table
                if table is not None and table.cell is not None:
                    reference = " ".join(
                        str(attributes.get(key) or "")
                        for key in ("id", "class", "title")
                    ).casefold()
                    if reference:
                        table.cell.segments[-1].reference_labels.add(reference)
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag in {"b", "strong"}:
            self._bold_depth += 1
        if tag in {"h2", "h3", "h4"}:
            self._heading_tag = tag
            self._heading_level = int(tag[1])
            self._heading_parts = []
            return
        if tag == "table":
            classes = set(attributes.get("class", "").split())
            table = (
                _Table(self._heading, classes=frozenset(classes))
                if classes.intersection({"wikitable", "toccolours", "col-begin"})
                else None
            )
            self._table_stack.append(table)
            return
        table = self._table
        if tag == "tr" and table is not None:
            table.row = []
            return
        if tag in {"td", "th"} and table is not None and table.row is not None:
            table.cell = _Cell(tag=tag, attrs=attributes)
            return
        if table is not None and table.cell is not None and tag in {"br", "p", "li"}:
            table.cell.boundary()
        if tag == "li" and not self._table_stack:
            self._list_item = _ListItem(self._heading)
        if tag == "a":
            self._anchor = {
                "href": attributes.get("href", ""),
                "title": attributes.get("title", ""),
                "parts": [],
                "bold": self._bold_depth > 0,
            }

    def handle_endtag(self, tag: str) -> None:
        if tag in {"style", "script", "sup"}:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag == "a" and self._anchor is not None:
            link = _Link(
                href=str(self._anchor.get("href") or ""),
                title=str(self._anchor.get("title") or ""),
                text=" ".join("".join(self._anchor.get("parts") or ()).split()),
                bold=bool(self._anchor.get("bold")),
            )
            table = self._table
            if table is not None and table.cell is not None:
                table.cell.links.append(link)
                table.cell.segments[-1].links.append(link)
            elif self._list_item is not None:
                self._list_item.links.append(link)
            self._anchor = None
        if tag in {"b", "strong"} and self._bold_depth:
            self._bold_depth -= 1
        if tag == self._heading_tag:
            value = " ".join("".join(self._heading_parts).split())
            self._headings[self._heading_level] = value
            for level in tuple(self._headings):
                if level > self._heading_level:
                    self._headings.pop(level, None)
            self._heading_tag = ""
            self._heading_level = 0
            self._heading_parts = []
            return
        table = self._table
        if tag in {"td", "th"} and table is not None and table.cell is not None:
            if table.row is not None:
                table.row.append(table.cell)
            table.cell = None
            return
        if tag == "tr" and table is not None and table.row is not None:
            if table.row:
                table.rows.append(table.row)
            table.row = None
            return
        if tag == "table":
            if not self._table_stack:
                return
            finished = self._table_stack.pop()
            if finished is not None and finished.rows:
                self.tables.append(finished)
            return
        if tag == "li" and self._list_item is not None and not self._table_stack:
            if self._list_item.text or self._list_item.links:
                self.list_items.append(self._list_item)
            self._list_item = None

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        self.document_parts.append(data)
        if self._heading_tag:
            self._heading_parts.append(data)
        table = self._table
        if table is not None and table.cell is not None:
            table.cell.parts.append(data)
            table.cell.segments[-1].parts.append(data)
            if self._bold_depth and data.strip():
                table.cell.segments[-1].has_bold_text = True
        elif self._list_item is not None:
            self._list_item.parts.append(data)
        if self._anchor is not None:
            self._anchor["parts"].append(data)


def _normalized_header(value: object) -> str:
    text = str(value or "").casefold().replace("(s)", "s")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _integer_attr(cell: _Cell, name: str) -> int:
    try:
        return max(1, int(cell.attrs.get(name) or "1"))
    except ValueError:
        return 1


def _expanded_rows(table: _Table) -> list[list[_Cell]]:
    active: dict[int, tuple[_Cell, int]] = {}
    expanded: list[list[_Cell]] = []
    for row_index, cells in enumerate(table.rows):
        logical = {
            column: cell
            for column, (cell, final_row) in active.items()
            if final_row >= row_index
        }
        column = 0
        for cell in cells:
            while column in logical:
                column += 1
            colspan = _integer_attr(cell, "colspan")
            rowspan = _integer_attr(cell, "rowspan")
            for offset in range(colspan):
                target = column + offset
                logical[target] = cell
                if rowspan > 1:
                    active[target] = (cell, row_index + rowspan - 1)
            column += colspan
        if logical:
            expanded.append([logical[index] for index in range(max(logical) + 1)])
    return expanded


def _header_row(
    rows: Sequence[Sequence[_Cell]], required: Sequence[str]
) -> tuple[int, dict[str, int]] | None:
    for row_index, row in enumerate(rows):
        values = [_normalized_header(cell.text) for cell in row]
        found: dict[str, int] = {}
        for requirement in required:
            for column, value in enumerate(values):
                if requirement == "years selected":
                    matches = "year" in value and "selected" in value
                elif requirement == "selector":
                    matches = value.startswith("selector")
                else:
                    matches = value == requirement
                if matches:
                    found[requirement] = column
                    break
        if len(found) == len(required):
            return row_index, found
    return None


def _page_title_from_link(link: _Link) -> str:
    href = link.href
    if href.startswith(f"{WIKIPEDIA_BASE_URL}/wiki/"):
        raw_href = href.split(f"{WIKIPEDIA_BASE_URL}/wiki/", 1)[1]
    elif href.startswith("/wiki/"):
        raw_href = href.split("/wiki/", 1)[1]
    else:
        return ""
    raw = unquote(raw_href).replace("_", " ")
    title = link.title.strip() or raw.strip()
    if not title or ":" in title.split(" ", 1)[0]:
        return ""
    return title


def _identity_from_links(links: Sequence[_Link]) -> dict[str, str] | None:
    for link in links:
        title = _page_title_from_link(link)
        if not title:
            continue
        name = link.text.strip() or re.sub(r"\s*\([^)]*\)\s*$", "", title)
        return {
            "name": name,
            "wikipedia_page": title,
            "wikipedia_url": (
                link.href
                if link.href.startswith(f"{WIKIPEDIA_BASE_URL}/wiki/")
                else WIKIPEDIA_BASE_URL + link.href
            ),
        }
    return None


def _identity_name_key(value: object) -> str:
    return " ".join(
        re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).split()
    )


def _page_identity_index(
    parser: _WikipediaHonorsHTMLParser,
) -> dict[str, dict[str, str]]:
    """Index unique linked identities for resolving a same-page plain-text repeat."""

    candidates: dict[str, dict[str, dict[str, str]]] = {}
    links: list[_Link] = []
    for table in parser.tables:
        for row in table.rows:
            for cell in row:
                links.extend(cell.links)
    for item in parser.list_items:
        links.extend(item.links)
    for link in links:
        identity = _identity_from_links((link,))
        if identity is None:
            continue
        key = _identity_name_key(identity["name"])
        if not key:
            continue
        by_page = candidates.setdefault(key, {})
        by_page[str(identity["wikipedia_page"]).casefold()] = identity
    return {
        key: next(iter(by_page.values()))
        for key, by_page in candidates.items()
        if len(by_page) == 1
    }


def _identity_from_segment_text(
    text: str, identity_index: Mapping[str, Mapping[str, str]]
) -> dict[str, str] | None:
    name = text.split(",", 1)[0].strip()
    identity = identity_index.get(_identity_name_key(name))
    return dict(identity) if identity is not None else None


_POSITION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bquarterbacks?\b|\bqb\b", "QB"),
    (r"\brunning backs?\b|(?<!defensive )\bhalfbacks?\b|\bfullbacks?\b|\btailbacks?\b|\b(?:rb|hb|fb|tb|hbfb|fbhb)\b", "RB"),
    (r"\bwide receivers?\b|\bflankers?\b|\bsplit ends?\b|\b(?:wr|fl|se|oe)\b", "WR"),
    (r"\btight ends?\b|\bte\b", "TE"),
    (r"\bdefensive tackles?\b|\bdefensive guards?\b|\bnose tackles?\b|\bmiddle guards?\b|\binterior linem(?:a|e)n\b|\b(?:[lr]?dt|nt|ng|mg)\b", "IDL"),
    (r"\bdefensive ends?\b|\boutside linebackers?\b|\b(?:[lr]?de|olb)\b", "EDGE"),
    (r"\bmiddle linebackers?\b|\binside linebackers?\b|\blinebackers?\b|\b(?:mlb|ilb|[lr]lb|lb)\b", "LB"),
    (r"\bcornerbacks?\b|\bcb\b", "CB"),
    (r"\bsafet(?:y|ies)\b|\b(?:s|fs|ss)\b", "S"),
    (r"\boffensive tackles?\b|\b(?:left|right) tackles?\b|\btackles?\b|\b(?:ot|lt|rt|t)\b", "OT"),
    (r"\bguards?\b|\boffensive linem(?:a|e)n\b|\b(?:g|og|lg|rg|ol)\b", "IOL"),
    (r"\bcenters?\b|\bc\b", "IOL"),
    (r"\bplacekickers?\b|\bkickers?\b|\b(?:pk|k)\b", "K"),
    (r"\bpunters?\b|\bp\b", "P"),
    (r"\blong snappers?\b|\bls\b", "LS"),
)


def comparison_positions(
    position_text: object, *, selection_year: int | None = None
) -> list[str]:
    """Map unambiguous source roles to DraftScope comparison position groups."""

    text = " ".join(str(position_text or "").casefold().replace("-", " ").split())
    text = re.sub(
        r"\bdefensive ends?\s*/\s*tackles?\b",
        "defensive end/defensive tackle",
        text,
    )
    text = re.sub(
        r"\bdefensive tackles?\s*/\s*ends?\b",
        "defensive tackle/defensive end",
        text,
    )
    matches: list[tuple[int, int, str]] = []
    if selection_year is not None and selection_year < 1970:
        for match in re.finditer(r"\b(?:ls|rs)\b", text):
            matches.append((match.start(), match.end(), "S"))
    for pattern, position in _POSITION_PATTERNS:
        if position == "LS" and selection_year is not None and selection_year < 1970:
            continue
        for match in re.finditer(pattern, text):
            matches.append((match.start(), match.end(), position))
    matches.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    occupied: list[tuple[int, int]] = []
    positions: list[str] = []
    for start, end, position in matches:
        if any(
            start < occupied_end and end > occupied_start
            for occupied_start, occupied_end in occupied
        ):
            continue
        occupied.append((start, end))
        if position not in positions:
            positions.append(position)
    return positions


def _dated_end_positions(position_text: object, year: int) -> tuple[list[str], str]:
    """Resolve a source's otherwise-generic End role without using honors outcomes."""

    positions = comparison_positions(position_text, selection_year=year)
    text = " ".join(str(position_text or "").casefold().replace("-", " ").split())
    generic_end = bool(re.search(r"\bends?\b", text)) and not bool(
        re.search(r"\b(?:defensive|offensive|tight|split)\s+ends?\b", text)
    )
    if not generic_end:
        return positions, ""
    if year <= 1949:
        for position in ("WR", "EDGE"):
            if position not in positions:
                positions.append(position)
        return positions, "pre-1950 generic End indexed under both modern two-way families"
    if "WR" not in positions:
        positions.append("WR")
    return positions, "post-1949 generic End indexed under the receiving-end family"


def _parse_document(html_text: str) -> _WikipediaHonorsHTMLParser:
    parser = _WikipediaHonorsHTMLParser()
    parser.feed(html_text)
    parser.close()
    return parser


def parse_pro_bowl_page(
    html_text: str,
    *,
    expected_table_count: int | None = None,
    minimum_rows: int = 1,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse one alphabetic Pro Bowl table using unique selection seasons."""

    parser = _parse_document(html_text)
    parsed: list[dict[str, Any]] = []
    rejected_rows = 0
    blank_placeholder_rows = 0
    shifted_identity_repairs: list[dict[str, str]] = []
    excluded_afl_all_star_rows = 0
    excluded_afl_all_star_selections = 0
    matching_tables = 0
    for table in parser.tables:
        rows = _expanded_rows(table)
        header = _header_row(rows, ("name", "position", "years selected"))
        if header is None:
            continue
        matching_tables += 1
        header_index, columns = header
        pending_blank_identity: dict[str, str] | None = None
        for row in rows[header_index + 1 :]:
            if max(columns.values()) >= len(row):
                rejected_rows += 1
                continue
            identity = _identity_from_links(row[columns["name"]].links)
            raw_position = row[columns["position"]].text
            years_cell = row[columns["years selected"]]
            all_years = {
                int(value)
                for value in re.findall(
                    r"(?<!\d)(?:19|20)\d{2}(?!\d)", years_cell.text
                )
            }
            afl_link_years = {
                int(value)
                for link in years_cell.links
                if _page_title_from_link(link)
                == "American Football League All-Star game"
                for value in re.findall(
                    r"(?<!\d)(?:19|20)\d{2}(?!\d)", link.text
                )
            }
            afl_marker_years = {
                int(value)
                for value in re.findall(
                    r"(?<!\d)((?:19|20)\d{2})(?!\d)\s*\(AFL\)",
                    years_cell.text,
                    flags=re.IGNORECASE,
                )
            }
            if afl_link_years != afl_marker_years:
                rejected_rows += 1
                continue
            years = sorted(all_years - afl_link_years)
            excluded_afl_all_star_selections += len(afl_link_years)
            if identity is not None and not raw_position.strip() and not years:
                if pending_blank_identity is not None:
                    rejected_rows += 1
                pending_blank_identity = identity
                blank_placeholder_rows += 1
                continue
            if pending_blank_identity is not None:
                if identity is None or not raw_position.strip() or not years:
                    rejected_rows += 1
                    pending_blank_identity = None
                    continue
                shifted_identity_repairs.append(
                    {
                        "kept_name": pending_blank_identity["name"],
                        "kept_page": pending_blank_identity["wikipedia_page"],
                        "discarded_shifted_name": identity["name"],
                        "discarded_shifted_page": identity["wikipedia_page"],
                    }
                )
                identity = pending_blank_identity
                pending_blank_identity = None
            if identity is not None and all_years and not years and afl_link_years:
                excluded_afl_all_star_rows += 1
                continue
            if identity is None or not years:
                rejected_rows += 1
                continue
            mapped_positions, mapping_note = _dated_end_positions(
                raw_position, min(years)
            )
            parsed.append(
                {
                    **identity,
                    "position_raw": raw_position,
                    "comparison_positions": mapped_positions,
                    "position_mapping_note": mapping_note,
                    "pro_bowl_years": years,
                    "pro_bowl_selections": len(years),
                }
            )
        if pending_blank_identity is not None:
            rejected_rows += 1
    expected = expected_table_count if expected_table_count is not None else matching_tables
    if matching_tables < 1 or matching_tables != expected:
        raise HistoricalHonorsError(
            f"expected {expected_table_count or 'at least one'} Pro Bowl player "
            f"table(s); found {matching_tables}"
        )
    if rejected_rows:
        raise HistoricalHonorsError(
            f"Pro Bowl player tables contained {rejected_rows} unparseable row(s)"
        )
    if len(parsed) < minimum_rows:
        raise HistoricalHonorsError(
            f"Pro Bowl page parsed {len(parsed)} rows; minimum is {minimum_rows}"
        )
    return parsed, {
        "matching_tables": matching_tables,
        "parsed_rows": len(parsed),
        "rejected_rows": rejected_rows,
        "blank_placeholder_rows": blank_placeholder_rows,
        "shifted_identity_repairs": shifted_identity_repairs,
        "excluded_afl_all_star_rows": excluded_afl_all_star_rows,
        "excluded_afl_all_star_selections": excluded_afl_all_star_selections,
        "coverage_start": min(min(row["pro_bowl_years"]) for row in parsed),
        "coverage_end": max(max(row["pro_bowl_years"]) for row in parsed),
    }


_GROUPED_PRO_BOWL_POSITION_HEADERS = {
    "quarterback": "Quarterback",
    "quarterbacks": "Quarterback",
    "qb": "QB",
    "running back": "Running back",
    "running backs": "Running back",
    "rb": "RB",
    "halfback": "Halfback",
    "halfbacks": "Halfback",
    "fullback": "Fullback",
    "fullbacks": "Fullback",
    "fb": "FB",
    "wide receiver": "Wide receiver",
    "wide receivers": "Wide receiver",
    "wr": "WR",
    "tight end": "Tight end",
    "tight ends": "Tight end",
    "te": "TE",
    "offensive line": "Offensive linemen",
    "offensive linemen": "Offensive linemen",
    "ol": "OL",
    "offensive tackle": "Offensive tackle",
    "offensive tackles": "Offensive tackle",
    "tackle": "Tackle",
    "tackles": "Tackle",
    "guard": "Guard",
    "guards": "Guard",
    "offensive guard": "Offensive guard",
    "offensive guards": "Offensive guard",
    "center": "Center",
    "centers": "Center",
    "defensive line": "Defensive linemen",
    "defensive linemen": "Defensive linemen",
    "dl": "DL",
    "defensive end": "Defensive end",
    "defensive ends": "Defensive end",
    "defensive tackle": "Defensive tackle",
    "defensive tackles": "Defensive tackle",
    "linebacker": "Linebacker",
    "linebackers": "Linebacker",
    "lb": "LB",
    "inside linebacker": "Inside linebacker",
    "inside linebackers": "Inside linebacker",
    "outside linebacker": "Outside linebacker",
    "outside linebackers": "Outside linebacker",
    "defensive back": "Defensive back",
    "defensive backs": "Defensive back",
    "db": "DB",
    "cornerback": "Cornerback",
    "cornerbacks": "Cornerback",
    "safety": "Safety",
    "safeties": "Safety",
    "placekicker": "Placekicker",
    "placekickers": "Placekicker",
    "kicker": "Kicker",
    "kickers": "Kicker",
    "k": "K",
    "punter": "Punter",
    "punters": "Punter",
    "p": "P",
    "long snapper": "Long snapper",
    "long snappers": "Long snapper",
    "kick returner": "Kick returner",
    "kick returners": "Kick returner",
    "punt returner": "Punt returner",
    "punt returners": "Punt returner",
    "return specialist": "Return specialist",
    "return specialists": "Return specialist",
    "special teamer": "Special teamer",
    "special teamers": "Special teamer",
    "special teams": "Special teams",
}


def _annual_pro_bowl_schema(
    selection_year: int,
) -> tuple[str, int, int, int]:
    schema = _ANNUAL_PRO_BOWL_SCHEMAS.get(selection_year)
    if schema is not None:
        return schema
    if selection_year in _ANNUAL_PRO_BOWL_KNOWN_UNAVAILABLE or (
        1950 <= selection_year <= 2021
    ):
        raise HistoricalHonorsError(
            f"{selection_year} Pro Bowl page has no demonstrably complete annual roster"
        )
    if selection_year >= 2022:
        return ("conference_tables", 6, 80, 140)
    raise HistoricalHonorsError(
        f"{selection_year} predates the annual NFL Pro Bowl roster population"
    )


def _is_roster_sentinel(segment: _Segment) -> bool:
    if segment.links:
        return False
    value = _normalized_header(segment.text)
    return value in {"", "none", "n a", "not applicable", "no selection"}


def _grouped_roster_header(value: object) -> tuple[str, str] | None:
    normalized = _normalized_header(value)
    position = _GROUPED_PRO_BOWL_POSITION_HEADERS.get(normalized)
    if position is not None:
        return ("position", position)
    if normalized in {"head coach", "head coaches", "coach", "coaches"}:
        return ("exclude", normalized)
    if (
        normalized in {"offense", "defense"}
        or normalized.startswith("team ")
        or normalized == "selected but did not participate"
        or normalized.endswith(" roster")
        or " pro bowl roster" in normalized
    ):
        return ("section", normalized)
    return None


def _grouped_position_with_suffix(position: str, text: str) -> str:
    """Retain explicit old-roster role suffixes without scanning player names."""

    if ")" not in text:
        return position
    suffix = text.rsplit(")", 1)[1]
    tokens = re.findall(
        r"(?<![A-Z])(?:QB|RB|HB|FB|WR|TE|OT|LT|RT|T|G|C|DE|DT|NT|"
        r"OLB|ILB|MLB|LB|CB|FS|SS|S|K|P|LS|KR|PR)(?![A-Z])",
        suffix.upper(),
    )
    return "; ".join((position, *dict.fromkeys(tokens)))


def _annual_roster_identity(
    segment: _Segment, selection_year: int
) -> tuple[dict[str, str] | None, str]:
    identity = _identity_from_links(segment.links)
    if identity is not None:
        return identity, ""
    # The 1977 game page contains one known red link under the name
    # "Cleveland Elam Sr.".  It identifies the same 49ers defensive tackle as
    # the existing Cleveland Elam biography; keep the repair narrow and
    # visible instead of treating arbitrary unlinked text as an identity.
    name = re.split(r"\s+[\-\u2013]\s+", segment.text, maxsplit=1)[0].strip()
    if selection_year == 1976 and _identity_name_key(name) == "cleveland elam sr":
        return (
            {
                "name": "Cleveland Elam",
                "wikipedia_page": "Cleveland Elam",
                "wikipedia_url": f"{WIKIPEDIA_BASE_URL}/wiki/Cleveland_Elam",
            },
            "1977 game-page red link 'Cleveland Elam Sr.' repaired to Cleveland Elam",
        )
    return None, ""


def parse_pro_bowl_roster_page(
    html_text: str,
    selection_year: int,
    *,
    expected_table_count: int | None = None,
    minimum_players: int | None = None,
    maximum_players: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse one complete annual Pro Bowl roster under an explicit schema."""

    schema, schema_table_count, schema_minimum, schema_maximum = (
        _annual_pro_bowl_schema(selection_year)
    )
    if (
        expected_table_count is not None
        and expected_table_count != schema_table_count
    ):
        raise HistoricalHonorsError(
            f"{selection_year} Pro Bowl schema requires {schema_table_count} "
            f"roster table(s), not {expected_table_count}"
        )
    expected_tables = schema_table_count
    minimum = schema_minimum if minimum_players is None else minimum_players
    maximum = schema_maximum if maximum_players is None else maximum_players
    parser = _parse_document(html_text)
    document_text = _normalized_header(" ".join(parser.document_parts))
    bold_participant_legend = bool(
        re.search(r"\bbold player who participated in (?:the )?game\b", document_text)
    )
    selections: list[dict[str, Any]] = []
    roster_tables = 0
    replacement_alternates = 0
    participating_alternates = 0
    missing_identities = 0
    sentinels_ignored = 0
    position_groups = 0
    table_player_counts: list[int] = []
    conference_table_counts = {"afc": 0, "nfc": 0}
    identity_repairs: list[str] = []

    def add_identity(identity: Mapping[str, str], raw_position: str) -> None:
        selections.append(
            {
                **identity,
                "position_raw": raw_position,
                "comparison_positions": comparison_positions(
                    raw_position, selection_year=selection_year
                ),
                "pro_bowl_years": [selection_year],
                "pro_bowl_selections": 1,
            }
        )

    if schema == "conference_tables":
        for table in parser.tables:
            if "wikitable" not in table.classes or not re.search(
                r"\brosters?\b", table.heading, re.IGNORECASE
            ):
                continue
            conference = ""
            if re.search(
                r"\bafc\b|american football conference",
                table.heading,
                re.IGNORECASE,
            ):
                conference = "afc"
            elif re.search(
                r"\bnfc\b|national football conference",
                table.heading,
                re.IGNORECASE,
            ):
                conference = "nfc"
            if not conference:
                continue
            rows = _expanded_rows(table)
            header = None
            position_key = "position"
            selected_key = "starters"
            for candidate_position in ("position", "positions"):
                for candidate_selected in (
                    "starters",
                    "starter",
                    "player",
                    "players",
                ):
                    header = _header_row(
                        rows, (candidate_position, candidate_selected)
                    )
                    if header is not None:
                        position_key = candidate_position
                        selected_key = candidate_selected
                        break
                if header is not None:
                    break
            if header is None:
                continue
            roster_tables += 1
            conference_table_counts[conference] += 1
            before_table = len(selections)
            header_index, columns = header
            header_values = [
                _normalized_header(cell.text) for cell in rows[header_index]
            ]
            reserve_column = next(
                (
                    index
                    for index, value in enumerate(header_values)
                    if value in {"reserve", "reserves"}
                ),
                None,
            )
            alternate_column = next(
                (
                    index
                    for index, value in enumerate(header_values)
                    if value in {"alternate", "alternates"}
                ),
                None,
            )
            selected_columns = [columns[selected_key]]
            if reserve_column is not None:
                selected_columns.append(reserve_column)
            if alternate_column is not None:
                selected_columns.append(alternate_column)
            selected_columns = list(dict.fromkeys(selected_columns))
            for row in rows[header_index + 1 :]:
                position_column = columns[position_key]
                if position_column >= len(row):
                    continue
                raw_position = row[position_column].text
                for column in selected_columns:
                    if column >= len(row):
                        continue
                    for segment in row[column].nonempty_segments():
                        if _is_roster_sentinel(segment):
                            sentinels_ignored += 1
                            continue
                        if column == alternate_column:
                            is_replacement = any(
                                "replacement" in label
                                for label in segment.reference_labels
                            )
                            is_participant = any(
                                link.bold and bool(_page_title_from_link(link))
                                for link in segment.links
                            ) and bold_participant_legend
                            if not is_replacement and not is_participant:
                                continue
                            if is_replacement:
                                replacement_alternates += 1
                            else:
                                participating_alternates += 1
                        identity, repair = _annual_roster_identity(
                            segment, selection_year
                        )
                        if identity is None:
                            missing_identities += 1
                            continue
                        if repair:
                            identity_repairs.append(repair)
                        add_identity(identity, raw_position)
            table_player_counts.append(len(selections) - before_table)

    elif schema == "conference_columns":
        conference_headers = (
            ("american conference", "national conference")
            if selection_year == 1950
            else ("afc", "nfc")
        )
        for table in parser.tables:
            if "wikitable" not in table.classes or not re.search(
                r"\brosters?\b", table.heading, re.IGNORECASE
            ):
                continue
            rows = _expanded_rows(table)
            header = None
            position_key = "position"
            for candidate_position in ("position", "positions"):
                header = _header_row(
                    rows, (candidate_position, *conference_headers)
                )
                if header is not None:
                    position_key = candidate_position
                    break
            if header is None:
                continue
            roster_tables += 1
            before_table = len(selections)
            header_index, columns = header
            conference_counts = {name: 0 for name in conference_headers}
            for row in rows[header_index + 1 :]:
                if columns[position_key] >= len(row):
                    continue
                raw_position = row[columns[position_key]].text
                for conference_header in conference_headers:
                    column = columns[conference_header]
                    if column >= len(row):
                        continue
                    for segment in row[column].nonempty_segments():
                        if _is_roster_sentinel(segment):
                            sentinels_ignored += 1
                            continue
                        identity, repair = _annual_roster_identity(
                            segment, selection_year
                        )
                        if identity is None:
                            missing_identities += 1
                            continue
                        if repair:
                            identity_repairs.append(repair)
                        add_identity(identity, raw_position)
                        conference_counts[conference_header] += 1
            if any(count == 0 for count in conference_counts.values()):
                raise HistoricalHonorsError(
                    f"{selection_year} Pro Bowl combined roster had an empty conference column"
                )
            table_player_counts.append(len(selections) - before_table)

    else:
        required_class = "toccolours" if schema == "grouped_tables" else "col-begin"
        for table in parser.tables:
            if required_class not in table.classes or not re.search(
                r"\brosters?\b", table.heading, re.IGNORECASE
            ):
                continue
            roster_tables += 1
            before_table = len(selections)
            for row in table.rows:
                for cell in row:
                    current_position = ""
                    excluded_group = False
                    for segment in cell.nonempty_segments():
                        header = _grouped_roster_header(segment.text)
                        if header is not None:
                            kind, value = header
                            if kind == "position":
                                current_position = value
                                excluded_group = False
                                position_groups += 1
                            elif kind == "exclude":
                                current_position = ""
                                excluded_group = True
                            else:
                                current_position = ""
                                excluded_group = False
                            continue
                        identity, repair = _annual_roster_identity(
                            segment, selection_year
                        )
                        if identity is not None:
                            if excluded_group:
                                continue
                            if not current_position:
                                missing_identities += 1
                                continue
                            raw_position = _grouped_position_with_suffix(
                                current_position, segment.text
                            )
                            if repair:
                                identity_repairs.append(repair)
                            add_identity(identity, raw_position)
                            continue
                        if _is_roster_sentinel(segment):
                            sentinels_ignored += 1
                            continue
                        looks_like_player = bool(
                            re.match(r"^\s*\d{1,3}\b", segment.text)
                            or re.search(r"\s[\-\u2013]\s", segment.text)
                        )
                        if current_position and looks_like_player:
                            missing_identities += 1
            table_player_counts.append(len(selections) - before_table)

    if roster_tables != expected_tables:
        raise HistoricalHonorsError(
            f"{selection_year} Pro Bowl {schema} schema expected "
            f"{expected_tables} roster table(s); found {roster_tables}"
        )
    if schema == "conference_tables" and conference_table_counts != {
        "afc": 3,
        "nfc": 3,
    }:
        raise HistoricalHonorsError(
            f"{selection_year} Pro Bowl roster conference/table symmetry failed: "
            f"{conference_table_counts}"
        )
    if schema in {"grouped_tables", "column_lists"} and (
        position_groups < expected_tables * 8
    ):
        raise HistoricalHonorsError(
            f"{selection_year} Pro Bowl {schema} schema found only "
            f"{position_groups} position groups"
        )
    if any(count < 2 for count in table_player_counts):
        raise HistoricalHonorsError(
            f"{selection_year} Pro Bowl roster contained an implausibly sparse table: "
            f"{table_player_counts}"
        )
    if missing_identities:
        raise HistoricalHonorsError(
            f"{selection_year} Pro Bowl roster had {missing_identities} selected "
            "player(s) without stable Wikipedia links or position groups"
        )

    by_page: dict[str, dict[str, Any]] = {}
    for selection in selections:
        key = str(selection["wikipedia_page"]).casefold()
        existing = by_page.get(key)
        if existing is None:
            by_page[key] = selection
            continue
        raw_positions = [
            value.strip()
            for value in (
                str(existing.get("position_raw") or "")
                + ";"
                + str(selection.get("position_raw") or "")
            ).split(";")
            if value.strip()
        ]
        existing["position_raw"] = "; ".join(dict.fromkeys(raw_positions))
        for position in selection.get("comparison_positions") or ():
            if position not in existing["comparison_positions"]:
                existing["comparison_positions"].append(position)
    result = list(by_page.values())
    if not minimum <= len(result) <= maximum:
        raise HistoricalHonorsError(
            f"{selection_year} Pro Bowl selected-player count {len(result)} is "
            f"outside the fail-closed range {minimum}-{maximum}"
        )
    return result, {
        "selection_year": selection_year,
        "schema": schema,
        "roster_tables": roster_tables,
        "expected_roster_tables": expected_tables,
        "table_player_counts": table_player_counts,
        "position_groups": position_groups,
        "conference_table_counts": conference_table_counts,
        "selected_players": len(result),
        "missing_identities": missing_identities,
        "identity_repairs": list(dict.fromkeys(identity_repairs)),
        "sentinels_ignored": sentinels_ignored,
        "replacement_alternates": replacement_alternates,
        "participating_alternates": participating_alternates,
        "bold_participant_legend": bold_participant_legend,
        "annual_roster_complete": True,
    }


def _selector_token(year: int) -> str:
    if year == 1922:
        return r"(?<![A-Z0-9-])CDN(?![A-Z0-9-])"
    if 1923 <= year <= 1930:
        return r"(?<![A-Z0-9-])GB-1(?![A-Z0-9-])"
    if 1931 <= year <= 1939:
        return r"(?<![A-Z0-9-])(?:UP|UPI)-1(?![A-Z0-9-])"
    if 1946 <= year <= 1949:
        return r"(?<![A-Z0-9-])UP-1(?![A-Z0-9-])"
    if 1940 <= year <= 1953:
        return r"(?<![A-Z0-9-])AP(?:-1)?(?![A-Z0-9-])"
    raise HistoricalHonorsError(f"no table selector token is defined for {year}")


def _selector_name(year: int) -> str:
    if year == 1920:
        return "Rock Island Argus / Bruce Copeland first team"
    if year == 1921:
        return "Buffalo Evening News team"
    if year == 1922:
        return "Canton Daily News team"
    if 1923 <= year <= 1930:
        return "Green Bay Press-Gazette first team"
    if 1931 <= year <= 1939:
        return "United Press/UPI first team"
    if 1946 <= year <= 1949:
        return "United Press NFL-only first team"
    return "Associated Press first team"


def _selection_row(
    identity: Mapping[str, str],
    position_raw: str,
    year: int,
    method: str,
) -> dict[str, Any]:
    positions, mapping_note = _dated_end_positions(position_raw, year)
    return {
        **identity,
        "position_raw": position_raw,
        "comparison_positions": positions,
        "position_mapping_note": mapping_note,
        "selection_year": year,
        "selector": _selector_name(year),
        "selector_method": method,
        "source_url": wikipedia_page_url(f"{year} All-Pro Team"),
    }


def _parse_single_team_year(parser: _WikipediaHonorsHTMLParser, year: int) -> list[dict[str, Any]]:
    required = ("position", "player", "team") if year == 1920 else ("player", "position", "team")
    for table in parser.tables:
        rows = _expanded_rows(table)
        header = _header_row(rows, required)
        if header is None:
            continue
        header_index, columns = header
        if year == 1920:
            preceding = rows[: header_index + 1]
            if not any(
                "first team" in _normalized_header(cell.text)
                for row in preceding
                for cell in row
            ):
                continue
        selections: list[dict[str, Any]] = []
        missing_identities = 0
        for row in rows[header_index + 1 :]:
            if max(columns.values()) >= len(row):
                continue
            identity = _identity_from_links(row[columns["player"]].links)
            if identity is None:
                if row[columns["player"]].text.strip():
                    missing_identities += 1
                continue
            selections.append(
                _selection_row(
                    identity,
                    row[columns["position"]].text,
                    year,
                    "first_team_table" if year == 1920 else "single_team_table",
                )
            )
        if missing_identities:
            raise HistoricalHonorsError(
                f"{year} canonical table had {missing_identities} player(s) without stable Wikipedia links"
            )
        if selections:
            return selections
    raise HistoricalHonorsError(f"{year} canonical team table was not found")


def _parse_selector_table_year(
    parser: _WikipediaHonorsHTMLParser, year: int
) -> tuple[list[dict[str, Any]], int]:
    token = re.compile(_selector_token(year), re.IGNORECASE)
    selections: list[dict[str, Any]] = []
    candidate_tables = 0
    missing_identities = 0
    excluded_non_nfl_league_selections = 0
    for table in parser.tables:
        rows = _expanded_rows(table)
        header = _header_row(rows, ("player", "selector"))
        if header is None:
            continue
        candidate_tables += 1
        header_index, columns = header
        position_column = None
        team_column = None
        for column, cell in enumerate(rows[header_index]):
            normalized = _normalized_header(cell.text)
            if normalized == "position":
                position_column = column
            elif normalized == "team":
                team_column = column
        if year in _AAFC_TEAMS_BY_YEAR and team_column is None:
            raise HistoricalHonorsError(
                f"{year} combined-league selector table had no team column"
            )
        for row in rows[header_index + 1 :]:
            if max(columns.values()) >= len(row):
                continue
            if team_column is not None and team_column < len(row):
                team = " ".join(row[team_column].text.casefold().split())
                if team in _AAFC_TEAMS_BY_YEAR.get(year, ()):
                    excluded_non_nfl_league_selections += 1
                    continue
            if not token.search(row[columns["selector"]].text):
                continue
            identity = _identity_from_links(row[columns["player"]].links)
            if identity is None:
                missing_identities += 1
                continue
            raw_position = (
                row[position_column].text
                if position_column is not None and position_column < len(row)
                else table.heading
            )
            selections.append(
                _selection_row(identity, raw_position, year, "selector_token")
            )
    if not candidate_tables:
        raise HistoricalHonorsError(f"{year} selector table was not found")
    if missing_identities:
        raise HistoricalHonorsError(
            f"{year} canonical selector had {missing_identities} player(s) without stable Wikipedia links"
        )
    return selections, excluded_non_nfl_league_selections


_AP_FIRST_TOKEN = re.compile(
    r"(?<![A-Z0-9-])AP(?:-1|-t)?(?![A-Z0-9-])", re.IGNORECASE
)


def _parse_ap_list_year(
    parser: _WikipediaHonorsHTMLParser, year: int
) -> list[dict[str, Any]]:
    selections: list[dict[str, Any]] = []
    missing_identities = 0
    for item in parser.list_items:
        if not re.search(
            r"\b(?:offense|offensive|defense|defensive)\b",
            item.heading,
            re.IGNORECASE,
        ):
            continue
        if not _AP_FIRST_TOKEN.search(item.text):
            continue
        identity = _identity_from_links(item.links)
        if identity is None:
            missing_identities += 1
            continue
        selections.append(_selection_row(identity, item.heading, year, "list_token"))
    if missing_identities:
        raise HistoricalHonorsError(
            f"{year} AP list had {missing_identities} selection(s) without stable Wikipedia links"
        )
    if not selections:
        raise HistoricalHonorsError(f"{year} AP list selections were not found")
    return selections


def _parse_bold_ap_year(
    parser: _WikipediaHonorsHTMLParser, year: int
) -> list[dict[str, Any]]:
    selections: list[dict[str, Any]] = []
    missing_identities = 0
    for table in parser.tables:
        rows = _expanded_rows(table)
        header = _header_row(rows, ("position", "players"))
        if header is None:
            continue
        header_index, columns = header
        for row in rows[header_index + 1 :]:
            if max(columns.values()) >= len(row):
                continue
            raw_position = row[columns["position"]].text
            for segment in row[columns["players"]].nonempty_segments():
                identity_link = next(
                    (
                        link
                        for link in segment.links
                        if _page_title_from_link(link)
                    ),
                    None,
                )
                if identity_link is None or not identity_link.bold:
                    if segment.has_bold_text and identity_link is None:
                        missing_identities += 1
                    continue
                identity = _identity_from_links((identity_link,))
                if identity is not None:
                    selections.append(
                        _selection_row(
                            identity,
                            raw_position,
                            year,
                            "bold_inferred" if year in _INFERRED_BOLD_AP_YEARS else "bold_explicit",
                        )
                    )
    if missing_identities:
        raise HistoricalHonorsError(
            f"{year} bold AP list had {missing_identities} selection(s) without stable Wikipedia links"
        )
    return selections


def _parse_first_team_ap_year(
    parser: _WikipediaHonorsHTMLParser, year: int
) -> tuple[list[dict[str, Any]], int]:
    selections: list[dict[str, Any]] = []
    candidate_tables = 0
    missing_identities = 0
    excluded_non_nfl_league_selections = 0
    identity_index = _page_identity_index(parser)
    for table in parser.tables:
        rows = _expanded_rows(table)
        header = _header_row(rows, ("position", "first team"))
        if header is None:
            continue
        nfl_table = not year in {1968, 1969} or table.heading.casefold().startswith(
            "nfl all-pros"
        )
        if not nfl_table:
            header_index, columns = header
            for row in rows[header_index + 1 :]:
                if max(columns.values()) >= len(row):
                    continue
                excluded_non_nfl_league_selections += sum(
                    bool(_AP_FIRST_TOKEN.search(segment.text))
                    for segment in row[columns["first team"]].nonempty_segments()
                )
            continue
        candidate_tables += 1
        header_index, columns = header
        for row in rows[header_index + 1 :]:
            if max(columns.values()) >= len(row):
                continue
            raw_position = row[columns["position"]].text
            for segment in row[columns["first team"]].nonempty_segments():
                if not _AP_FIRST_TOKEN.search(segment.text):
                    continue
                identity = _identity_from_links(segment.links)
                identity_method = "page_link"
                if identity is None:
                    identity = _identity_from_segment_text(
                        segment.text, identity_index
                    )
                    identity_method = "same_page_unique_link_name"
                if identity is not None:
                    selection = _selection_row(
                        identity, raw_position, year, "first_team_ap_token"
                    )
                    selection["identity_method"] = identity_method
                    selections.append(selection)
                else:
                    missing_identities += 1
    if not candidate_tables:
        raise HistoricalHonorsError(f"{year} first-team tables were not found")
    if missing_identities:
        raise HistoricalHonorsError(
            f"{year} AP first team had {missing_identities} selection(s) without stable Wikipedia links"
        )
    return selections, excluded_non_nfl_league_selections


def parse_all_pro_page(
    html_text: str, year: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse the canonical first-team All-Pro selection for one NFL season."""

    if year < ALL_PRO_FIRST_SEASON:
        raise HistoricalHonorsError("All-Pro history begins with the 1920 season")
    parser = _parse_document(html_text)
    excluded_non_nfl_league_selections = 0
    if year in {1920, 1921}:
        raw = _parse_single_team_year(parser, year)
    elif 1922 <= year <= 1953:
        raw, excluded_non_nfl_league_selections = _parse_selector_table_year(
            parser, year
        )
    elif 1954 <= year <= 1961:
        raw = _parse_ap_list_year(parser, year)
    elif year in _EXPLICIT_BOLD_AP_YEARS or year in _INFERRED_BOLD_AP_YEARS:
        raw = _parse_bold_ap_year(parser, year)
    else:
        raw, excluded_non_nfl_league_selections = _parse_first_team_ap_year(
            parser, year
        )

    # Career All-Pro totals count seasons, not positions.  Preserve every role
    # when a player was selected at more than one position in the same season.
    by_page: dict[str, dict[str, Any]] = {}
    for selection in raw:
        page_key = str(selection["wikipedia_page"]).casefold()
        existing = by_page.get(page_key)
        if existing is None:
            by_page[page_key] = dict(selection)
            continue
        raw_positions = [
            value.strip()
            for value in (
                str(existing.get("position_raw") or "")
                + ";"
                + str(selection.get("position_raw") or "")
            ).split(";")
            if value.strip()
        ]
        existing["position_raw"] = "; ".join(dict.fromkeys(raw_positions))
        mapping_notes = [
            value.strip()
            for value in (
                str(existing.get("position_mapping_note") or "")
                + ";"
                + str(selection.get("position_mapping_note") or "")
            ).split(";")
            if value.strip()
        ]
        existing["position_mapping_note"] = "; ".join(
            dict.fromkeys(mapping_notes)
        )
        positions = list(existing.get("comparison_positions") or ())
        for position in selection.get("comparison_positions") or ():
            if position not in positions:
                positions.append(position)
        existing["comparison_positions"] = positions

    selections = list(by_page.values())
    if year <= 1950:
        minimum, maximum = 10, 13
    elif year <= 1967:
        minimum, maximum = 18, 26
    elif year <= 1969:
        minimum, maximum = 18, 30
    elif year <= 1975:
        minimum, maximum = 20, 28
    else:
        minimum, maximum = 20, 35
    if year == 1966:
        minimum, maximum = 18, 30
    if not (minimum <= len(selections) <= maximum):
        raise HistoricalHonorsError(
            f"{year} canonical first-team count {len(selections)} is outside "
            f"the fail-closed range {minimum}-{maximum}"
        )
    method = str(selections[0]["selector_method"])
    return selections, {
        "year": year,
        "selector": _selector_name(year),
        "selector_method": method,
        "selection_count": len(selections),
        "excluded_non_nfl_league_selections": (
            excluded_non_nfl_league_selections
        ),
        "bold_inferred": year in _INFERRED_BOLD_AP_YEARS,
        "same_page_identity_resolutions": sum(
            1
            for selection in selections
            if selection.get("identity_method") == "same_page_unique_link_name"
        ),
    }


def wikipedia_page_url(page_title: str) -> str:
    return WIKIPEDIA_BASE_URL + "/wiki/" + quote(
        page_title.replace(" ", "_"), safe="(),'-"
    )


def _parse_api_url(page_title: str) -> str:
    return WIKIPEDIA_API_URL + "?" + urlencode(
        {
            "action": "parse",
            "page": page_title,
            "prop": "text",
            "format": "json",
        }
    )


def _payload_html(payload: Mapping[str, Any], expected_title: str) -> str:
    if "error" in payload:
        raise HistoricalHonorsError(
            f"Wikipedia API error for {expected_title}: {payload['error']}"
        )
    parse_payload = payload.get("parse")
    text_payload = parse_payload.get("text") if isinstance(parse_payload, Mapping) else None
    html_text = text_payload.get("*") if isinstance(text_payload, Mapping) else None
    if not isinstance(html_text, str) or not html_text.strip():
        raise HistoricalHonorsError(
            f"Wikipedia parse response for {expected_title} did not contain HTML"
        )
    return html_text


def _safe_cache_name(page_title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", page_title.casefold()).strip("_") + ".json"


def _request_provenance(destination: Path, expected_url: str) -> dict[str, Any]:
    """Read optional cache provenance without assuming a callback implementation."""

    sidecar = destination.with_suffix(destination.suffix + ".source.json")
    try:
        value = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "status": "callback_provenance_unavailable",
            "cache_fallback_known": False,
        }
    if not isinstance(value, Mapping):
        return {
            "status": "invalid_sidecar",
            "cache_fallback_known": False,
        }
    source_url = str(value.get("url") or "")
    return {
        "status": "available" if source_url == expected_url else "url_mismatch",
        "url": source_url,
        "retrieved_at": value.get("downloaded_at") or value.get("retrieved_at"),
        "sha256": value.get("sha256"),
        # The callback contract returns only a payload.  A sidecar can prove
        # origin and age, but not whether this particular request fell back.
        "cache_fallback_known": "cache_fallback" in value,
        "cache_fallback": value.get("cache_fallback"),
    }


def _merge_honor_rows(
    pro_bowl_rows: Sequence[Mapping[str, Any]] | None,
    all_pro_rows: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    merged: dict[str, dict[str, Any]] = {}
    conflicts: list[dict[str, str]] = []

    def base(source: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "name": str(source.get("name") or ""),
            "wikipedia_page": str(source.get("wikipedia_page") or ""),
            "wikipedia_url": str(source.get("wikipedia_url") or ""),
            "wikipedia_aliases": [],
            "wikidata_id": "",
            "position_raw": "",
            "position_mapping_note": "",
            "comparison_positions": [],
            "nfl_pro_bowls": None,
            "nfl_pro_bowl_years": [],
            "nfl_pro_bowl_source_urls": [],
            "nfl_all_pro_selections": None,
            "nfl_first_team_all_pro_years": [],
            "nfl_first_team_all_pro_source_urls": [],
            "historical_honors_comparison_only": True,
            "model_feature_eligible": False,
        }

    def row_for(source: Mapping[str, Any]) -> dict[str, Any]:
        page = str(source.get("wikipedia_page") or "")
        key = page.casefold()
        row = merged.get(key)
        if row is None:
            row = base(source)
            merged[key] = row
        elif str(row.get("name") or "").casefold() != str(source.get("name") or "").casefold():
            conflicts.append(
                {
                    "wikipedia_page": page,
                    "first_name": str(row.get("name") or ""),
                    "other_name": str(source.get("name") or ""),
                }
            )
        raw = str(source.get("position_raw") or "").strip()
        existing_raw = [value.strip() for value in str(row["position_raw"]).split(";") if value.strip()]
        if raw and raw not in existing_raw:
            existing_raw.append(raw)
        row["position_raw"] = "; ".join(existing_raw)
        mapping_note = str(source.get("position_mapping_note") or "").strip()
        existing_notes = [
            value.strip()
            for value in str(row.get("position_mapping_note") or "").split(";")
            if value.strip()
        ]
        if mapping_note and mapping_note not in existing_notes:
            existing_notes.append(mapping_note)
        row["position_mapping_note"] = "; ".join(existing_notes)
        for position in source.get("comparison_positions") or ():
            if position not in row["comparison_positions"]:
                row["comparison_positions"].append(position)
        return row

    if pro_bowl_rows is not None:
        for source in pro_bowl_rows:
            row = row_for(source)
            years = set(row["nfl_pro_bowl_years"])
            years.update(int(value) for value in source.get("pro_bowl_years") or ())
            row["nfl_pro_bowl_years"] = sorted(years)
            row["nfl_pro_bowls"] = len(years)
            source_url = str(source.get("source_url") or "")
            if source_url and source_url not in row["nfl_pro_bowl_source_urls"]:
                row["nfl_pro_bowl_source_urls"].append(source_url)

    if all_pro_rows is not None:
        for source in all_pro_rows:
            row = row_for(source)
            years = set(row["nfl_first_team_all_pro_years"])
            years.add(int(source["selection_year"]))
            row["nfl_first_team_all_pro_years"] = sorted(years)
            row["nfl_all_pro_selections"] = len(years)
            source_url = str(source.get("source_url") or "")
            if source_url and source_url not in row["nfl_first_team_all_pro_source_urls"]:
                row["nfl_first_team_all_pro_source_urls"].append(source_url)

    rows = sorted(
        merged.values(),
        key=lambda row: (
            str(row.get("name") or "").casefold(),
            str(row.get("wikipedia_page") or "").casefold(),
        ),
    )
    return rows, conflicts


def _merge_rows_by_wikidata(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collapse redirect/page-title aliases after stable Wikidata resolution."""

    merged: dict[str, dict[str, Any]] = {}
    alias_merges: list[dict[str, Any]] = []
    for source in rows:
        qid = str(source.get("wikidata_id") or "")
        page = str(source.get("wikipedia_page") or "")
        key = f"qid:{qid}" if qid else f"page:{page.casefold()}"
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(source)
            continue
        aliases = list(existing.get("wikipedia_aliases") or ())
        for alias in (
            str(existing.get("wikipedia_page") or ""),
            page,
            *(source.get("wikipedia_aliases") or ()),
        ):
            if alias and alias != existing.get("wikipedia_page") and alias not in aliases:
                aliases.append(alias)
        existing["wikipedia_aliases"] = aliases
        raw_positions = [
            value.strip()
            for value in (
                str(existing.get("position_raw") or "")
                + ";"
                + str(source.get("position_raw") or "")
            ).split(";")
            if value.strip()
        ]
        existing["position_raw"] = "; ".join(dict.fromkeys(raw_positions))
        mapping_notes = [
            value.strip()
            for value in (
                str(existing.get("position_mapping_note") or "")
                + ";"
                + str(source.get("position_mapping_note") or "")
            ).split(";")
            if value.strip()
        ]
        existing["position_mapping_note"] = "; ".join(
            dict.fromkeys(mapping_notes)
        )
        positions = list(existing.get("comparison_positions") or ())
        for position in source.get("comparison_positions") or ():
            if position not in positions:
                positions.append(position)
        existing["comparison_positions"] = positions
        for years_field, count_field in (
            ("nfl_pro_bowl_years", "nfl_pro_bowls"),
            ("nfl_first_team_all_pro_years", "nfl_all_pro_selections"),
        ):
            years = {
                int(value)
                for value in (
                    list(existing.get(years_field) or ())
                    + list(source.get(years_field) or ())
                )
            }
            existing[years_field] = sorted(years)
            if existing.get(count_field) is not None or source.get(count_field) is not None:
                existing[count_field] = len(years)
        for urls_field in (
            "nfl_pro_bowl_source_urls",
            "nfl_first_team_all_pro_source_urls",
        ):
            urls = list(existing.get(urls_field) or ())
            for url in source.get(urls_field) or ():
                if url not in urls:
                    urls.append(url)
            existing[urls_field] = urls
        alias_merges.append(
            {
                "wikidata_id": qid,
                "kept_page": existing.get("wikipedia_page"),
                "merged_page": page,
            }
        )
    return sorted(
        merged.values(),
        key=lambda row: (
            str(row.get("name") or "").casefold(),
            str(row.get("wikipedia_page") or "").casefold(),
        ),
    ), alias_merges


def _enrich_wikidata_ids(
    rows: list[dict[str, Any]],
    cache_dir: Path,
    request_json: Callable[..., Mapping[str, Any]],
    *,
    refresh: bool,
) -> dict[str, Any]:
    errors: list[str] = []
    resolved = 0
    title_to_qid: dict[str, str] = {}
    titles = sorted({str(row["wikipedia_page"]) for row in rows})
    for batch_index in range(0, len(titles), 45):
        batch = titles[batch_index : batch_index + 45]
        url = WIKIPEDIA_API_URL + "?" + urlencode(
            {
                "action": "query",
                "titles": "|".join(batch),
                "prop": "pageprops",
                "ppprop": "wikibase_item",
                "redirects": 1,
                "format": "json",
            }
        )
        try:
            payload = request_json(
                url,
                cache_dir / f"wikipedia_honors_pageprops_{batch_index // 45:03d}.json",
                refresh=refresh,
            )
            query = payload.get("query") if isinstance(payload, Mapping) else None
            pages = query.get("pages") if isinstance(query, Mapping) else None
            if not isinstance(pages, Mapping):
                raise HistoricalHonorsError("pageprops response did not contain pages")
            canonical: dict[str, str] = {}
            for page in pages.values():
                if not isinstance(page, Mapping):
                    continue
                title = str(page.get("title") or "")
                props = page.get("pageprops")
                qid = str(props.get("wikibase_item") or "") if isinstance(props, Mapping) else ""
                if title and qid:
                    canonical[title.casefold()] = qid
            aliases: dict[str, str] = {}
            for key in ("normalized", "redirects"):
                values = query.get(key) if isinstance(query, Mapping) else None
                if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                    continue
                for value in values:
                    if isinstance(value, Mapping):
                        before = str(value.get("from") or "")
                        after = str(value.get("to") or "")
                        if before and after:
                            aliases[before.casefold()] = after.casefold()
            for title in batch:
                target = aliases.get(title.casefold(), title.casefold())
                target = aliases.get(target, target)
                qid = canonical.get(target, "")
                if qid:
                    title_to_qid[title.casefold()] = qid
        except Exception as exc:  # optional enrichment must not erase valid honors
            errors.append(f"batch {batch_index // 45}: {exc}")
    for row in rows:
        qid = title_to_qid.get(str(row["wikipedia_page"]).casefold(), "")
        row["wikidata_id"] = qid
        if qid:
            resolved += 1
    return {
        "status": "complete" if not errors else "partial",
        "resolved_rows": resolved,
        "unresolved_rows": len(rows) - resolved,
        "errors": errors,
    }


def load_historical_honors(
    cache_dir: str | Path,
    *,
    request_json: Callable[..., Mapping[str, Any]],
    refresh: bool = False,
    latest_completed_season: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load the all-era comparison-only Pro Bowl and AP1-equivalent overlay.

    Each component is published only when every required page parses.  A
    partial scrape is recorded in metadata but discarded, preventing missing
    pages from silently lowering a player's career count.
    """

    latest = (
        int(latest_completed_season)
        if latest_completed_season is not None
        else datetime.now(timezone.utc).year - 1
    )
    if latest < ALL_PRO_FIRST_SEASON:
        raise HistoricalHonorsError(
            f"latest completed season must be at least {ALL_PRO_FIRST_SEASON}"
        )
    cache = Path(cache_dir) / "historical_honors"
    cache.mkdir(parents=True, exist_ok=True)

    pro_bowl_rows: list[dict[str, Any]] = []
    pro_bowl_successes: list[dict[str, Any]] = []
    pro_bowl_annual_reconciliation_successes: list[dict[str, Any]] = []
    pro_bowl_annual_reconciliation_failures: list[dict[str, Any]] = []
    pro_bowl_supplement_successes: list[dict[str, Any]] = []
    pro_bowl_failures: list[dict[str, str]] = []
    for page_title in PRO_BOWL_PAGE_TITLES:
        source_url = wikipedia_page_url(page_title)
        request_url = _parse_api_url(page_title)
        destination = cache / _safe_cache_name(page_title)
        try:
            payload = request_json(
                request_url,
                destination,
                refresh=refresh,
            )
            html_text = _payload_html(payload, page_title)
            page_rows, audit = parse_pro_bowl_page(
                html_text,
                expected_table_count=_PRO_BOWL_EXPECTED_TABLES[page_title],
                minimum_rows=25,
            )
            for row in page_rows:
                row["source_page"] = page_title
                row["source_url"] = source_url
            pro_bowl_rows.extend(page_rows)
            pro_bowl_successes.append(
                {
                    "page": page_title,
                    "source_url": source_url,
                    "request_provenance": _request_provenance(destination, request_url),
                    **audit,
                }
            )
        except Exception as exc:
            pro_bowl_failures.append(
                {
                    "page": page_title,
                    "source_url": source_url,
                    "source_kind": "alphabetic_list",
                    "error": str(exc),
                }
            )
    pro_bowl_coverage_start = (
        min(item["coverage_start"] for item in pro_bowl_successes)
        if pro_bowl_successes
        else None
    )
    pro_bowl_coverage_end = (
        max(item["coverage_end"] for item in pro_bowl_successes)
        if pro_bowl_successes
        else None
    )
    pro_bowl_list_observed_common_latest = (
        min(item["coverage_end"] for item in pro_bowl_successes)
        if len(pro_bowl_successes) == len(PRO_BOWL_PAGE_TITLES)
        else None
    )
    # The community-maintained A-Z lists have known isolated omissions.  Union
    # every parseable modern annual roster so those omissions cannot silently
    # lower a career total.  Years whose pages do not publish roster tables are
    # retained as explicit audit gaps rather than guessed.
    annual_reconciliation_pages = [
        (selection_year, f"{selection_year + 1} Pro Bowl")
        for selection_year in sorted(_ANNUAL_PRO_BOWL_SCHEMAS)
        if selection_year <= min(latest, 2021)
    ]
    for selection_year, page_title in annual_reconciliation_pages:
        source_url = wikipedia_page_url(page_title)
        request_url = _parse_api_url(page_title)
        destination = cache / _safe_cache_name(page_title)
        try:
            payload = request_json(request_url, destination, refresh=refresh)
            html_text = _payload_html(payload, page_title)
            page_rows, audit = parse_pro_bowl_roster_page(
                html_text, selection_year
            )
            for row in page_rows:
                row["source_page"] = page_title
                row["source_url"] = source_url
            pro_bowl_rows.extend(page_rows)
            pro_bowl_annual_reconciliation_successes.append(
                {
                    "page": page_title,
                    "source_url": source_url,
                    "request_provenance": _request_provenance(
                        destination, request_url
                    ),
                    **audit,
                }
            )
        except Exception as exc:
            pro_bowl_annual_reconciliation_failures.append(
                {
                    "selection_year": selection_year,
                    "page": page_title,
                    "source_url": source_url,
                    "error": str(exc),
                }
            )
    # A per-letter page's maximum year does not prove population completeness.
    # Independently parse every annual roster from 2022 onward, including years
    # that partially appear in the alphabetic lists, then merge unique seasons.
    supplement_start = 2022
    supplemental_pages = [
        (selection_year, f"{selection_year + 1} Pro Bowl Games")
        for selection_year in range(supplement_start, latest + 1)
    ]
    for selection_year, page_title in supplemental_pages:
        source_url = wikipedia_page_url(page_title)
        request_url = _parse_api_url(page_title)
        destination = cache / _safe_cache_name(page_title)
        try:
            payload = request_json(
                request_url,
                destination,
                refresh=refresh,
            )
            html_text = _payload_html(payload, page_title)
            page_rows, audit = parse_pro_bowl_roster_page(
                html_text, selection_year
            )
            for row in page_rows:
                row["source_page"] = page_title
                row["source_url"] = source_url
            pro_bowl_rows.extend(page_rows)
            pro_bowl_supplement_successes.append(
                {
                    "page": page_title,
                    "source_url": source_url,
                    "request_provenance": _request_provenance(destination, request_url),
                    **audit,
                }
            )
        except Exception as exc:
            pro_bowl_failures.append(
                {
                    "page": page_title,
                    "source_url": source_url,
                    "source_kind": "recent_roster_supplement",
                    "error": str(exc),
                }
            )
    annual_complete_selection_years = sorted(
        {
            int(item["selection_year"])
            for item in (
                *pro_bowl_annual_reconciliation_successes,
                *pro_bowl_supplement_successes,
            )
        }
    )
    pro_bowl_population_years = list(range(1950, latest + 1))
    annual_unverified_selection_years = sorted(
        set(pro_bowl_population_years) - set(annual_complete_selection_years)
    )
    pro_bowl_source_population_complete = bool(pro_bowl_population_years) and not (
        annual_unverified_selection_years
    )
    pro_bowl_published = (
        not pro_bowl_failures
        and not pro_bowl_annual_reconciliation_failures
        and len(pro_bowl_successes) == len(PRO_BOWL_PAGE_TITLES)
        and len(pro_bowl_annual_reconciliation_successes)
        == len(annual_reconciliation_pages)
        and len(pro_bowl_supplement_successes) == len(supplemental_pages)
    )
    if pro_bowl_supplement_successes:
        pro_bowl_coverage_end = max(
            int(pro_bowl_coverage_end or 0),
            max(item["selection_year"] for item in pro_bowl_supplement_successes),
        )
    all_pro_rows: list[dict[str, Any]] = []
    all_pro_successes: list[dict[str, Any]] = []
    all_pro_failures: list[dict[str, Any]] = []
    for year in range(ALL_PRO_FIRST_SEASON, latest + 1):
        page_title = f"{year} All-Pro Team"
        source_url = wikipedia_page_url(page_title)
        request_url = _parse_api_url(page_title)
        destination = cache / _safe_cache_name(page_title)
        try:
            payload = request_json(
                request_url,
                destination,
                refresh=refresh,
            )
            html_text = _payload_html(payload, page_title)
            year_rows, audit = parse_all_pro_page(html_text, year)
            all_pro_rows.extend(year_rows)
            all_pro_successes.append(
                {
                    **audit,
                    "source_url": source_url,
                    "request_provenance": _request_provenance(destination, request_url),
                }
            )
        except Exception as exc:
            all_pro_failures.append(
                {"year": year, "source_url": source_url, "error": str(exc)}
            )
    all_pro_published = not all_pro_failures and len(all_pro_successes) == latest - ALL_PRO_FIRST_SEASON + 1

    rows, conflicts = _merge_honor_rows(
        pro_bowl_rows if pro_bowl_published else None,
        all_pro_rows if all_pro_published else None,
    )
    for row in rows:
        if pro_bowl_published and row.get("nfl_pro_bowls") is None:
            row["nfl_pro_bowls"] = 0
        if all_pro_published and row.get("nfl_all_pro_selections") is None:
            row["nfl_all_pro_selections"] = 0
    wikidata = _enrich_wikidata_ids(
        rows, cache, request_json, refresh=refresh
    ) if rows else {
        "status": "unavailable",
        "resolved_rows": 0,
        "unresolved_rows": 0,
        "errors": [],
    }
    rows, wikidata_alias_merges = _merge_rows_by_wikidata(rows)
    wikidata["premerge_resolved_rows"] = wikidata.get("resolved_rows", 0)
    wikidata["resolved_rows"] = sum(
        1 for row in rows if str(row.get("wikidata_id") or "")
    )
    wikidata["unresolved_rows"] = len(rows) - int(wikidata["resolved_rows"])
    wikidata["alias_merges"] = len(wikidata_alias_merges)
    identity_gate_published = bool(
        not rows
        or (
            wikidata.get("status") == "complete"
            and not wikidata.get("errors")
            and int(wikidata.get("unresolved_rows") or 0) == 0
        )
    )
    effective_pro_bowl_published = pro_bowl_published and identity_gate_published
    effective_all_pro_published = all_pro_published and identity_gate_published
    identity_discarded_rows = len(rows) if not identity_gate_published else 0
    if not identity_gate_published:
        rows = []
    published_components = int(effective_pro_bowl_published) + int(
        effective_all_pro_published
    )
    all_pro_source_population_complete = effective_all_pro_published
    source_population_complete = bool(
        effective_pro_bowl_published
        and effective_all_pro_published
        and pro_bowl_source_population_complete
        and all_pro_source_population_complete
    )
    metadata_status = (
        "unavailable"
        if not published_components
        else "partial"
        if published_components < 2
        else "complete"
        if source_population_complete
        else "partial_population"
    )
    pro_bowl_population_coverage_note = (
        "Annual roster population coverage is complete for every Pro Bowl selection season."
        if pro_bowl_source_population_complete
        else (
            f"Annual roster population coverage is unverified for "
            f"{len(annual_unverified_selection_years)} selection season(s); the "
            "alphabetic lists are still published as comparison evidence, but they "
            "are not treated as proof of complete population recall. Exact years are "
            "listed in annual_roster_unverified_selection_years."
        )
    )
    career_qualifiers = [
        row
        for row in rows
        if (row.get("nfl_all_pro_selections") or 0) >= 1
        or (row.get("nfl_pro_bowls") or 0) >= 3
    ]
    unsupported_qualifiers = [
        {
            "name": row.get("name"),
            "wikipedia_page": row.get("wikipedia_page"),
            "position_raw": row.get("position_raw"),
            "nfl_all_pro_selections": row.get("nfl_all_pro_selections"),
            "nfl_pro_bowls": row.get("nfl_pro_bowls"),
        }
        for row in career_qualifiers
        if not row.get("comparison_positions")
    ]
    metadata: dict[str, Any] = {
        "status": metadata_status,
        "source_population_complete": source_population_complete,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "comparison_only": True,
        "model_feature_eligible": False,
        "source": "English Wikipedia historical NFL honors pages",
        "source_license": "CC BY-SA 4.0",
        "source_license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
        "changes_made": "Tables were parsed, selector-filtered, position-normalized, identity-merged, and career counts were derived from unique seasons.",
        "cache_fallback_disclosure": (
            "The request callback does not report whether it returned a stale fallback. "
            "Per-artifact sidecar timestamps and hashes are included when the callback writes them."
        ),
        "row_count": len(rows),
        "identity_gate": {
            "published": identity_gate_published,
            "discarded_merged_rows": identity_discarded_rows,
            "reason": (
                "all merged player pages resolved to stable Wikidata identities"
                if identity_gate_published
                else "stable identity resolution was incomplete; historical career totals were discarded"
            ),
        },
        "pro_bowl": {
            "status": (
                "complete"
                if effective_pro_bowl_published
                and pro_bowl_source_population_complete
                else "partial_population"
                if effective_pro_bowl_published
                else "unavailable_identity_gate"
                if pro_bowl_published
                else "unavailable"
            ),
            "published": effective_pro_bowl_published,
            "parsed_complete": pro_bowl_published,
            "requested_pages": list(PRO_BOWL_PAGE_TITLES),
            "successful_pages": pro_bowl_successes,
            "annual_reconciliation_requested_pages": [
                {"selection_year": year, "page": page}
                for year, page in annual_reconciliation_pages
            ],
            "annual_reconciliation_successful_pages": (
                pro_bowl_annual_reconciliation_successes
            ),
            "annual_reconciliation_unavailable_pages": (
                pro_bowl_annual_reconciliation_failures
            ),
            "annual_reconciliation_complete": bool(
                len(pro_bowl_annual_reconciliation_successes)
                == len(annual_reconciliation_pages)
            ),
            "annual_roster_complete_selection_years": (
                annual_complete_selection_years
            ),
            "annual_roster_unverified_selection_years": (
                annual_unverified_selection_years
            ),
            "annual_roster_known_unavailable_selection_years": sorted(
                year
                for year in _ANNUAL_PRO_BOWL_KNOWN_UNAVAILABLE
                if year <= latest
            ),
            "source_population_complete": pro_bowl_source_population_complete,
            "population_coverage_note": pro_bowl_population_coverage_note,
            "supplemental_requested_pages": [
                {"selection_year": year, "page": page}
                for year, page in supplemental_pages
            ],
            "supplemental_successful_pages": pro_bowl_supplement_successes,
            "failed_pages": pro_bowl_failures,
            "coverage_start": pro_bowl_coverage_start,
            "coverage_end": pro_bowl_coverage_end,
            "alphabetic_lists_complete_through": None,
            "alphabetic_lists_observed_common_latest_season": (
                pro_bowl_list_observed_common_latest
            ),
            "alphabetic_list_maxima_are_not_completeness_evidence": True,
            "latest_completed_season": latest,
            "source_lag_seasons": (
                max(0, latest - int(pro_bowl_coverage_end))
                if pro_bowl_coverage_end is not None
                else None
            ),
            "parsed_rows": len(pro_bowl_rows),
            "discarded_rows": (
                0 if effective_pro_bowl_published else len(pro_bowl_rows)
            ),
            "source_urls": [
                *(wikipedia_page_url(page) for page in PRO_BOWL_PAGE_TITLES),
                *(
                    wikipedia_page_url(page)
                    for _year, page in annual_reconciliation_pages
                ),
                *(wikipedia_page_url(page) for _year, page in supplemental_pages),
            ],
        },
        "first_team_all_pro": {
            "status": (
                "complete"
                if effective_all_pro_published
                else "unavailable_identity_gate"
                if all_pro_published
                else "unavailable"
            ),
            "published": effective_all_pro_published,
            "parsed_complete": all_pro_published,
            "source_population_complete": all_pro_source_population_complete,
            "requested_start": ALL_PRO_FIRST_SEASON,
            "requested_end": latest,
            "coverage_start": (
                ALL_PRO_FIRST_SEASON if effective_all_pro_published else None
            ),
            "coverage_end": latest if effective_all_pro_published else None,
            "successful_years": all_pro_successes,
            "failed_years": all_pro_failures,
            "parsed_selection_rows": len(all_pro_rows),
            "discarded_selection_rows": (
                0 if effective_all_pro_published else len(all_pro_rows)
            ),
            "bold_inferred_years": [
                year
                for year in sorted(_INFERRED_BOLD_AP_YEARS)
                if ALL_PRO_FIRST_SEASON <= year <= latest
            ],
            "canonical_selectors": {
                "1920": "Rock Island Argus / Bruce Copeland first team",
                "1921": "Buffalo Evening News",
                "1922": "Canton Daily News",
                "1923-1930": "Green Bay Press-Gazette first team",
                "1931-1939": "United Press/UPI first team",
                "1940-present": "Associated Press first team",
            },
        },
        "wikidata_pageprops": wikidata,
        "audit": {
            "identity_name_conflicts": conflicts,
            "wikidata_alias_merges": wikidata_alias_merges,
            "rows_without_comparison_positions": sum(
                1 for row in rows if not row.get("comparison_positions")
            ),
            "career_qualifier_rows": len(career_qualifiers),
            "career_qualifiers_without_comparison_positions": len(
                unsupported_qualifiers
            ),
            "unsupported_career_qualifiers": unsupported_qualifiers,
            "position_coverage_note": (
                "Unmapped rows are retained and disclosed, not assigned to a guessed "
                "comparison position. They are primarily ambiguous pre-modern Ends and "
                "return/special-team roles outside DraftScope's position groups; a caller "
                "may refine them from a separate player registry."
            ),
        },
    }
    return rows, metadata


__all__ = [
    "ALL_PRO_FIRST_SEASON",
    "HistoricalHonorsError",
    "PRO_BOWL_PAGE_TITLES",
    "comparison_positions",
    "load_historical_honors",
    "parse_all_pro_page",
    "parse_pro_bowl_page",
    "parse_pro_bowl_roster_page",
    "wikipedia_page_url",
]

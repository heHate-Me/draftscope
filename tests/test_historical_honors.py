from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

from draftscope.historical_honors import (
    HistoricalHonorsError,
    PRO_BOWL_PAGE_TITLES,
    comparison_positions,
    load_historical_honors,
    parse_all_pro_page,
    parse_pro_bowl_page,
    parse_pro_bowl_roster_page,
)


def _link(page: str, name: str | None = None) -> str:
    label = name or page.split(" (", 1)[0]
    return f'<a href="/wiki/{page.replace(" ", "_")}" title="{page}">{label}</a>'


def _pro_bowl_html(
    page: str = "John Abraham (American football)",
    name: str = "John Abraham",
    position: str = "DE, OLB",
    years: str = (
        '<a href="/wiki/2002_Pro_Bowl" title="2002 Pro Bowl">2001</a>, '
        '<a href="/wiki/2003_Pro_Bowl" title="2003 Pro Bowl">2002</a>, '
        '<a href="/wiki/2005_Pro_Bowl" title="2005 Pro Bowl">2004</a>, '
        '<a href="/wiki/2011_Pro_Bowl" title="2011 Pro Bowl">2010</a>, '
        '<a href="/wiki/2014_Pro_Bowl" title="2014 Pro Bowl">2013</a>, 2013'
    ),
) -> str:
    return f"""
    <table class="wikitable"><tr><th>Name</th><th>Position</th>
    <th>Year(s) selected</th><th>Franchise(s) represented</th><th>Notes</th></tr>
    <tr><td>{_link(page, name)}</td><td>{position}</td><td>{years}</td>
    <td>Example Team</td><td></td></tr></table>
    <table class="wikitable"><tr><th>Other</th></tr><tr><td>Ignored</td></tr></table>
    """


def _single_team_html(year: int, *, shared_page: str = "Shared Player") -> str:
    heading = '<tr><th colspan="3">First team</th></tr>' if year == 1920 else ""
    columns = (
        "<tr><th>Position</th><th>Player</th><th>Team</th></tr>"
        if year == 1920
        else "<tr><th>Player</th><th>Position</th><th>Team</th></tr>"
    )
    rows = []
    for index in range(11):
        page = shared_page if index == 0 else f"Player {year} {index}"
        player = _link(page)
        rows.append(
            f"<tr><td>Quarterback</td><td>{player}</td><td>Team</td></tr>"
            if year == 1920
            else f"<tr><td>{player}</td><td>Quarterback</td><td>Team</td></tr>"
        )
    return '<table class="wikitable">' + heading + columns + "".join(rows) + "</table>"


def _1950_pro_bowl_roster_html() -> str:
    rows = []
    player_index = 0
    for row_index in range(8):
        conferences = []
        for conference in ("American", "National"):
            players = []
            for _ in range(4):
                players.append(_link(f"{conference} Roster Player {player_index}"))
                player_index += 1
            conferences.append("<br>".join(players))
        rows.append(
            f"<tr><td>{conferences[0]}</td><th>Quarterback</th>"
            f"<td>{conferences[1]}</td></tr>"
        )
    return (
        '<h2>Rosters</h2><table class="wikitable"><tr>'
        "<th>American Conference</th><th>Position</th>"
        f"<th>National Conference</th></tr>{''.join(rows)}</table>"
    )


class HistoricalHonorsParserTests(unittest.TestCase):
    def test_position_mapping_uses_longest_role_and_exact_abbreviations(self) -> None:
        self.assertEqual(comparison_positions("Outside linebacker"), ["EDGE"])
        self.assertEqual(comparison_positions("Defensive halfback"), [])
        self.assertEqual(comparison_positions("DHB"), [])
        self.assertEqual(
            comparison_positions("CB; Defensive halfback"), ["CB"]
        )
        self.assertEqual(comparison_positions("LDE"), ["EDGE"])
        self.assertEqual(comparison_positions("LDT; NG"), ["IDL"])
        self.assertEqual(comparison_positions("Defensive guard"), ["IDL"])
        self.assertEqual(comparison_positions("Defensive lineman"), [])
        self.assertEqual(comparison_positions("DL"), [])
        self.assertEqual(comparison_positions("LLB; RLB"), ["LB"])
        self.assertEqual(comparison_positions("T"), ["OT"])
        self.assertEqual(
            comparison_positions("LS; Defensive back", selection_year=1958),
            ["S"],
        )
        self.assertEqual(comparison_positions("RS", selection_year=1955), ["S"])
        self.assertEqual(comparison_positions("LS", selection_year=2005), ["LS"])
        self.assertEqual(comparison_positions("Defensive back"), [])
        self.assertEqual(comparison_positions("Back"), [])

    def test_pro_bowl_parser_uses_stable_title_and_unique_years(self) -> None:
        rows, audit = parse_pro_bowl_page(_pro_bowl_html())

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["wikipedia_page"], "John Abraham (American football)")
        self.assertEqual(rows[0]["name"], "John Abraham")
        self.assertEqual(rows[0]["pro_bowl_selections"], 5)
        self.assertEqual(rows[0]["pro_bowl_years"], [2001, 2002, 2004, 2010, 2013])
        self.assertEqual(rows[0]["comparison_positions"], ["EDGE"])
        self.assertEqual(audit["matching_tables"], 1)

    def test_pro_bowl_parser_repairs_a_split_identity_row_explicitly(self) -> None:
        html = f"""
        <table class="wikitable"><tr><th>Name</th><th>Position</th>
        <th>Year(s) selected</th><th>Franchise(s) represented</th><th>Notes</th></tr>
        <tr><td>{_link("Ed White (American football)", "Ed White")}</td>
        <td></td><td></td><td></td><td></td></tr>
        <tr><td>{_link("Lorenzo White", "Lorenzo White")}</td><td>G</td>
        <td>1975, 1976, 1977, 1979</td><td>Minnesota, San Diego</td><td></td></tr>
        </table>
        """

        rows, audit = parse_pro_bowl_page(html)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Ed White")
        self.assertEqual(rows[0]["wikipedia_page"], "Ed White (American football)")
        self.assertEqual(rows[0]["pro_bowl_selections"], 4)
        self.assertEqual(len(audit["shifted_identity_repairs"]), 1)
        self.assertEqual(
            audit["shifted_identity_repairs"][0]["discarded_shifted_name"],
            "Lorenzo White",
        )

    def test_pro_bowl_parser_excludes_afl_all_star_selections(self) -> None:
        html = f"""
        <table class="wikitable"><tr><th>Name</th><th>Position</th>
        <th>Year(s) selected</th><th>Franchise(s) represented</th><th>Notes</th></tr>
        <tr><td>{_link("AFL Only Player")}</td><td>QB</td><td>
        <a href="/wiki/American_Football_League_All-Star_game"
        title="American Football League All-Star game">1961</a> (AFL),
        <a href="/wiki/American_Football_League_All-Star_game"
        title="American Football League All-Star game">1962</a> (AFL),
        <a href="/wiki/American_Football_League_All-Star_game"
        title="American Football League All-Star game">1963</a> (AFL)
        </td><td>Team</td><td></td></tr>
        <tr><td>{_link("Mixed League Player")}</td><td>CB</td><td>
        <a href="/wiki/American_Football_League_All-Star_game"
        title="American Football League All-Star game">1968</a> (AFL),
        <a href="/wiki/1971_Pro_Bowl" title="1971 Pro Bowl">1970</a>
        </td><td>Team</td><td></td></tr></table>
        """

        rows, audit = parse_pro_bowl_page(html)

        self.assertEqual([row["name"] for row in rows], ["Mixed League Player"])
        self.assertEqual(rows[0]["pro_bowl_years"], [1970])
        self.assertEqual(audit["excluded_afl_all_star_rows"], 1)
        self.assertEqual(audit["excluded_afl_all_star_selections"], 4)

    def test_pro_bowl_parser_fails_when_afl_marker_and_link_disagree(self) -> None:
        html = _pro_bowl_html(
            years=(
                '<a href="/wiki/American_Football_League_All-Star_game" '
                'title="American Football League All-Star game">1962</a>'
            )
        )

        with self.assertRaises(HistoricalHonorsError):
            parse_pro_bowl_page(html)

    def test_recent_roster_counts_selected_replacements_not_unused_alternates(self) -> None:
        tables = []
        player_index = 0
        for conference in ("AFC", "NFC"):
            conference_tables = []
            for table_index in range(3):
                starters = []
                reserves = []
                for _ in range(10):
                    starters.append(_link(f"Roster Player {player_index}"))
                    player_index += 1
                for _ in range(4):
                    reserves.append(_link(f"Roster Player {player_index}"))
                    player_index += 1
                replacement = _link(f"Roster Player {player_index}")
                player_index += 1
                participating = _link(f"Roster Player {player_index}")
                player_index += 1
                unused = _link(f"Unused Alternate {conference} {table_index}")
                conference_tables.append(
                    '<table class="wikitable"><tr><th>Position</th><th>Starter(s)</th>'
                    '<th>Reserve(s)</th><th>Alternate(s)</th></tr><tr><td>Quarterback</td>'
                    f'<td>{"<br>".join(starters)}</td><td>{"<br>".join(reserves)}</td>'
                    f'<td>{replacement}<sup id="cite_ref-replacement_1-0">1</sup>'
                    f'<br><b>{participating}</b><br>{unused}</td>'
                    "</tr></table>"
                )
            tables.append(f"<h3>{conference}</h3>" + "".join(conference_tables))
        html_without_legend = "<h2>Rosters</h2>" + "".join(tables)
        without_legend, without_legend_audit = parse_pro_bowl_roster_page(
            html_without_legend, 2025
        )
        self.assertEqual(len(without_legend), 90)
        self.assertEqual(without_legend_audit["participating_alternates"], 0)

        html = (
            "<h2>Rosters</h2><p><b>bold</b> player who participated in game</p>"
            + "".join(tables)
        )

        rows, audit = parse_pro_bowl_roster_page(html, 2025)
        self.assertEqual(len(rows), 96)
        self.assertEqual(audit["roster_tables"], 6)
        self.assertEqual(audit["replacement_alternates"], 6)
        self.assertEqual(audit["participating_alternates"], 6)
        self.assertTrue(audit["bold_participant_legend"])
        self.assertFalse(any(row["name"].startswith("Unused Alternate") for row in rows))

    def test_annual_roster_parser_supports_1950_conference_columns(self) -> None:
        rows, audit = parse_pro_bowl_roster_page(
            _1950_pro_bowl_roster_html(), 1950
        )

        self.assertEqual(len(rows), 64)
        self.assertEqual(audit["schema"], "conference_columns")
        self.assertEqual(audit["roster_tables"], 1)

    def test_1992_roster_none_sentinels_are_not_player_failures(self) -> None:
        tables = []
        player_index = 0
        for conference in ("AFC", "NFC"):
            for group in ("Offense", "Defense", "Special teams"):
                players = []
                for _ in range(2):
                    players.append(_link(f"1992 Roster Player {player_index}"))
                    player_index += 1
                tables.append(
                    f"<h3>{conference}</h3><h4>{group}</h4>"
                    '<table class="wikitable"><tr><th>Position</th>'
                    "<th>Starter(s)</th><th>Reserve(s)</th></tr>"
                    f"<tr><td>Quarterback</td><td>{'<br>'.join(players)}</td>"
                    "<td>None</td></tr></table>"
                )
        rows, audit = parse_pro_bowl_roster_page(
            "<h2>Rosters</h2>" + "".join(tables),
            1992,
            minimum_players=1,
            maximum_players=20,
        )

        self.assertEqual(len(rows), 12)
        self.assertEqual(audit["sentinels_ignored"], 6)
        self.assertEqual(audit["conference_table_counts"], {"afc": 3, "nfc": 3})

    def test_column_list_and_grouped_roster_schemas(self) -> None:
        column_cells = []
        player_index = 0
        for conference in ("AFC Roster", "NFC Roster"):
            segments = [f"<b>{conference}</b><br>"]
            for position in ("QB", "RB", "WR", "TE", "OL", "DL", "LB", "DB"):
                segments.append(f"<b>{position}</b><br>")
                segments.append(
                    f"{_link(f'Column Player {player_index}')} – Team<br>"
                )
                player_index += 1
            column_cells.append("<td>" + "".join(segments) + "</td>")
        column_html = (
            '<h2>Rosters</h2><table class="col-begin"><tr>'
            + "".join(column_cells)
            + "</tr></table>"
        )
        column_rows, column_audit = parse_pro_bowl_roster_page(
            column_html, 1994, minimum_players=1, maximum_players=30
        )
        self.assertEqual(len(column_rows), 16)
        self.assertEqual(column_audit["schema"], "column_lists")
        linebacker_rows = [
            row for row in column_rows if row["name"] in {"Column Player 6", "Column Player 14"}
        ]
        self.assertEqual(
            [row["position_raw"] for row in linebacker_rows], ["LB", "LB"]
        )
        self.assertTrue(
            all(row["comparison_positions"] == ["LB"] for row in linebacker_rows)
        )

        grouped_tables = []
        player_index = 0
        for team in ("Team One", "Team Two", "Selected but did not participate"):
            groups = []
            for position in (
                "Quarterbacks",
                "Running backs",
                "Wide receivers",
                "Tight ends",
                "Offensive guards",
                "Defensive linemen",
                "Linebackers",
                "Defensive backs",
            ):
                groups.append(
                    f"<p><b>{position}</b></p><ul><li>1 "
                    f"{_link(f'Grouped Player {player_index}')}</li></ul>"
                )
                player_index += 1
            grouped_tables.append(
                f'<h3>{team}</h3><table class="toccolours"><tr><td>'
                + "".join(groups)
                + "</td></tr></table>"
            )
        grouped_rows, grouped_audit = parse_pro_bowl_roster_page(
            "<h2>Rosters</h2>" + "".join(grouped_tables),
            2015,
            minimum_players=1,
            maximum_players=30,
        )
        self.assertEqual(len(grouped_rows), 24)
        self.assertEqual(grouped_audit["schema"], "grouped_tables")
        self.assertEqual(grouped_audit["position_groups"], 24)

    def test_annual_roster_parser_fails_closed_for_gaps_and_partial_schema(self) -> None:
        with self.assertRaisesRegex(HistoricalHonorsError, "no demonstrably complete"):
            parse_pro_bowl_roster_page("<h2>Roster</h2>", 1997)

        tables = []
        for index in range(5):
            conference = "AFC" if index < 3 else "NFC"
            tables.append(
                f"<h3>{conference}</h3><h4>Group {index}</h4>"
                '<table class="wikitable"><tr><th>Position</th><th>Starter(s)</th>'
                f"</tr><tr><td>QB</td><td>{_link(f'Partial Player {index} A')}<br>"
                f"{_link(f'Partial Player {index} B')}</td></tr></table>"
            )
        with self.assertRaisesRegex(HistoricalHonorsError, "expected 6 roster table"):
            parse_pro_bowl_roster_page(
                "<h2>Rosters</h2>" + "".join(tables),
                2025,
                minimum_players=1,
                maximum_players=20,
            )

    def test_1968_parser_excludes_first_team_all_afl_selections(self) -> None:
        tables = []
        for league in ("NFL All-Pros", "AFL All-Pros"):
            entries = "".join(
                f"<tr><td>Quarterback</td><td>{_link(f'{league} Player {index}')}, "
                "Team (AP)</td><td>Other Player, Team (AP-2)</td></tr>"
                for index in range(20)
            )
            tables.append(
                f"<h2>{league}</h2><table class=\"wikitable\">"
                "<tr><th>Position</th><th>First team</th><th>Second team</th></tr>"
                f"{entries}</table>"
            )

        rows, audit = parse_all_pro_page("".join(tables), 1968)

        self.assertEqual(len(rows), 20)
        self.assertTrue(all(row["name"].startswith("NFL") for row in rows))
        self.assertEqual(audit["selection_count"], 20)
        self.assertEqual(audit["excluded_non_nfl_league_selections"], 20)

    def test_1947_parser_excludes_aafc_players_from_combined_ap_team(self) -> None:
        nfl_rows = "".join(
            f"<tr><td>Back</td><td>{_link(f'NFL Player {index}')}</td>"
            "<td>Chicago Bears</td><td>AP-1, UP-1</td></tr>"
            for index in range(10)
        )
        aafc_rows = "".join(
            f"<tr><td>Back</td><td>{_link(f'AAFC Player {index}')}</td>"
            "<td>New York Yankees</td><td>AP-1</td></tr>"
            for index in range(3)
        )
        html = (
            '<table class="wikitable"><tr><th>Position</th><th>Player</th>'
            f"<th>Team</th><th>Selector(s)</th></tr>{nfl_rows}{aafc_rows}</table>"
        )

        rows, audit = parse_all_pro_page(html, 1947)

        self.assertEqual(len(rows), 10)
        self.assertTrue(all(row["name"].startswith("NFL") for row in rows))
        self.assertEqual(audit["excluded_non_nfl_league_selections"], 3)

    def test_selector_table_uses_only_canonical_source(self) -> None:
        selected = "".join(
            f"<tr><td>{_link(f'Canonical End {index}')}</td><td>Team</td><td>GB-1, VD-1</td></tr>"
            for index in range(10)
        )
        html = f"""
        <h3>Ends</h3>
        <table class="wikitable"><tr><th>Player</th><th>Team</th><th>Selector(s)</th></tr>
        {selected}
        <tr><td>{_link("Second Team End")}</td><td>Team</td><td>GB-2, VD-1</td></tr>
        </table>
        """
        rows, audit = parse_all_pro_page(html, 1923)

        self.assertEqual(len(rows), 10)
        self.assertNotIn("Second Team End", {row["name"] for row in rows})
        self.assertEqual(rows[0]["selector_method"], "selector_token")
        self.assertEqual(audit["selector"], "Green Bay Press-Gazette first team")

    def test_pre_modern_generic_end_is_indexed_as_two_way_role(self) -> None:
        selected = "".join(
            f"<tr><td>{_link(f'Two Way End {index}')}</td><td>Team</td><td>GB-1</td></tr>"
            for index in range(10)
        )
        html = f"""
        <h3>Ends</h3>
        <table class="wikitable"><tr><th>Player</th><th>Team</th><th>Selector(s)</th></tr>
        {selected}</table>
        """

        rows, _audit = parse_all_pro_page(html, 1923)

        self.assertEqual(rows[0]["comparison_positions"], ["WR", "EDGE"])
        self.assertIn("two-way", rows[0]["position_mapping_note"])

    def test_pre_modern_generic_end_keeps_other_explicit_roles(self) -> None:
        selected = "".join(
            f'<tr><td>{_link(f"Mixed Role Player {index}")}</td><td>End, kicker, punter</td>'
            "<td>Team</td><td>CDN</td></tr>"
            for index in range(10)
        )
        html = (
            '<table class="wikitable"><tr><th>Player</th><th>Position</th>'
            f"<th>Team</th><th>Selector(s)</th></tr>{selected}</table>"
        )

        rows, _audit = parse_all_pro_page(html, 1922)

        self.assertEqual(rows[0]["comparison_positions"], ["K", "P", "WR", "EDGE"])

    def test_duplicate_same_season_roles_merge_without_double_counting(self) -> None:
        selected = "".join(
            f"<tr><td>{_link(f'Dual Role Player {index}')}</td><td>Team</td><td>GB-1</td></tr>"
            for index in range(10)
        )
        duplicate = (
            f"<tr><td>{_link('Dual Role Player 0')}</td><td>Team</td><td>GB-1</td></tr>"
        )
        html = f"""
        <h3>Ends</h3>
        <table class="wikitable"><tr><th>Player</th><th>Team</th><th>Selector(s)</th></tr>
        {selected}{duplicate}</table>
        """

        rows, audit = parse_all_pro_page(html, 1923)

        self.assertEqual(len(rows), 10)
        self.assertEqual(audit["selection_count"], 10)
        self.assertEqual(rows[0]["comparison_positions"], ["WR", "EDGE"])

    def test_ap_table_token_excludes_other_selectors(self) -> None:
        selected = "".join(
            f"<tr><td>QB</td><td>{_link(f'AP Player {index}')}</td><td>Team</td><td>AP-1, UP-1</td></tr>"
            for index in range(10)
        )
        html = f"""
        <table class="wikitable"><tr><th>Position</th><th>Player</th><th>Team</th><th>Selector(s)</th></tr>
        {selected}
        <tr><td>QB</td><td>{_link("UP Only")}</td><td>Team</td><td>UP-1</td></tr>
        </table>
        """
        rows, _ = parse_all_pro_page(html, 1940)
        self.assertEqual(len(rows), 10)
        self.assertNotIn("UP Only", {row["name"] for row in rows})

    def test_ap_list_token_excludes_second_team(self) -> None:
        items = "".join(
            f"<li>{_link(f'List Player {index}')}, Team (AP, UPI)</li>"
            for index in range(20)
        )
        html = f"""
        <h2>Offensive selections</h2><h3>Quarterbacks</h3><ul>{items}
        <li>{_link("Second Team")}, Team (AP-2)</li></ul>
        """
        rows, _ = parse_all_pro_page(html, 1959)
        self.assertEqual(len(rows), 20)
        self.assertNotIn("Second Team", {row["name"] for row in rows})

    def test_explicit_bold_page_only_counts_bold_players(self) -> None:
        bold = "<br>".join(
            f"<b>{_link(f'Bold Player {index}')}</b>, {_link('Example Team')}"
            for index in range(18)
        )
        html = f"""
        <table class="wikitable"><tr><th>Position</th><th>Players</th></tr>
        <tr><td>Defensive ends</td><td>{bold}<br>{_link("Second Team Player")}, {_link("Other Team")}</td></tr>
        </table>
        """
        rows, audit = parse_all_pro_page(html, 1962)
        self.assertEqual(len(rows), 18)
        self.assertEqual(audit["selector_method"], "bold_explicit")
        self.assertNotIn("Example Team", {row["name"] for row in rows})
        self.assertNotIn("Second Team Player", {row["name"] for row in rows})

    def test_1966_bold_inference_is_visible_and_plausibility_gated(self) -> None:
        def page(count: int) -> str:
            players = "<br>".join(
                f"<b>{_link(f'Inferred Player {index}')}</b>, Team"
                for index in range(count)
            )
            return (
                '<table class="wikitable"><tr><th>Position</th><th>Players</th></tr>'
                f"<tr><td>Safeties</td><td>{players}</td></tr></table>"
            )

        rows, audit = parse_all_pro_page(page(18), 1966)
        self.assertEqual(len(rows), 18)
        self.assertTrue(audit["bold_inferred"])
        self.assertEqual(audit["selector_method"], "bold_inferred")
        with self.assertRaisesRegex(HistoricalHonorsError, "fail-closed range 18-30"):
            parse_all_pro_page(page(3), 1966)

    def test_modern_first_team_filters_to_ap_marker_per_entry(self) -> None:
        ap_entries = "<br>".join(
            f"{_link(f'Modern AP Player {index}')}, Team (AP, PFWA)"
            for index in range(20)
        )
        html = f"""
        <table class="wikitable"><tr><th>Position</th><th>First team</th><th>Second team</th></tr>
        <tr><td>Wide receiver</td><td>{ap_entries}<br>{_link("PFWA Only")}, Team (PFWA)</td>
        <td>{_link("AP Second")}, Team (AP-2)</td></tr></table>
        """
        rows, audit = parse_all_pro_page(html, 2025)
        self.assertEqual(len(rows), 20)
        self.assertEqual(audit["selector_method"], "first_team_ap_token")
        self.assertNotIn("PFWA Only", {row["name"] for row in rows})
        self.assertNotIn("AP Second", {row["name"] for row in rows})


class HistoricalHonorsLoaderTests(unittest.TestCase):
    def _requester(
        self,
        *,
        fail_page: str = "",
        fail_year: int | None = None,
        fail_pageprops: bool = False,
    ):
        qids: dict[str, str] = {}

        def request(url: str, destination: Path, *, refresh: bool):
            del destination, refresh
            query = parse_qs(urlparse(url).query)
            if query.get("action") == ["query"]:
                if fail_pageprops:
                    raise RuntimeError("fixture identity source unavailable")
                titles = query["titles"][0].split("|")
                return {
                    "query": {
                        "pages": {
                            str(index): {
                                "title": title,
                                "pageprops": {
                                    "wikibase_item": qids.setdefault(
                                        title, f"Q{len(qids) + 1}"
                                    )
                                },
                            }
                            for index, title in enumerate(titles)
                        }
                    }
                }
            page = query["page"][0]
            if page == fail_page or (fail_year is not None and page == f"{fail_year} All-Pro Team"):
                raise RuntimeError("fixture source unavailable")
            if page in PRO_BOWL_PAGE_TITLES:
                index = PRO_BOWL_PAGE_TITLES.index(page)
                expected_tables = (1, 1, 4, 2, 3, 2, 5, 4, 3)[index]
                tables = []
                for table_index in range(expected_tables):
                    entries = []
                    for row_index in range(25):
                        player_page = (
                            "Shared Player"
                            if index == table_index == row_index == 0
                            else f"Pro Bowl Player {index} {table_index} {row_index}"
                        )
                        entries.append(
                            f"<tr><td>{_link(player_page)}</td><td>QB</td>"
                            f'<td><a href="/wiki/{2001 + index}_Pro_Bowl" '
                            f'title="{2001 + index} Pro Bowl">{2000 + index}</a></td>'
                            "<td>Team</td><td></td></tr>"
                        )
                    tables.append(
                        '<table class="wikitable"><tr><th>Name</th><th>Position</th>'
                        '<th>Year(s) selected</th><th>Franchise(s) represented</th>'
                        f'<th>Notes</th></tr>{"".join(entries)}</table>'
                    )
                html = "".join(tables)
            elif page == "1951 Pro Bowl":
                html = _1950_pro_bowl_roster_html()
            else:
                year = int(page[:4])
                html = _single_team_html(year)
            return {"parse": {"title": page, "text": {"*": html}}}

        return request

    def test_loader_merges_honors_and_reports_exact_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rows, metadata = load_historical_honors(
                directory,
                request_json=self._requester(),
                latest_completed_season=1921,
            )

        shared = next(row for row in rows if row["wikipedia_page"] == "Shared Player")
        self.assertEqual(shared["nfl_pro_bowls"], 1)
        self.assertEqual(shared["nfl_all_pro_selections"], 2)
        self.assertTrue(shared["historical_honors_comparison_only"])
        self.assertFalse(shared["model_feature_eligible"])
        self.assertEqual(metadata["status"], "partial_population")
        self.assertFalse(metadata["source_population_complete"])
        self.assertEqual(metadata["pro_bowl"]["status"], "partial_population")
        self.assertTrue(
            metadata["first_team_all_pro"]["source_population_complete"]
        )
        self.assertEqual(len(metadata["pro_bowl"]["successful_pages"]), 9)
        self.assertEqual(metadata["pro_bowl"]["coverage_start"], 2000)
        self.assertEqual(metadata["pro_bowl"]["coverage_end"], 2008)
        self.assertEqual(
            [item["year"] for item in metadata["first_team_all_pro"]["successful_years"]],
            [1920, 1921],
        )
        self.assertTrue(shared["wikidata_id"].startswith("Q"))

    def test_loader_discards_partial_pro_bowl_component(self) -> None:
        missing = PRO_BOWL_PAGE_TITLES[3]
        with tempfile.TemporaryDirectory() as directory:
            rows, metadata = load_historical_honors(
                directory,
                request_json=self._requester(fail_page=missing),
                latest_completed_season=1920,
            )

        self.assertEqual(metadata["status"], "partial")
        self.assertFalse(metadata["pro_bowl"]["published"])
        self.assertEqual(metadata["pro_bowl"]["failed_pages"][0]["page"], missing)
        self.assertGreater(metadata["pro_bowl"]["discarded_rows"], 0)
        self.assertTrue(all(row["nfl_pro_bowls"] is None for row in rows))
        self.assertTrue(all(row["nfl_all_pro_selections"] == 1 for row in rows))

    def test_loader_discards_partial_all_pro_component(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rows, metadata = load_historical_honors(
                directory,
                request_json=self._requester(fail_year=1921),
                latest_completed_season=1921,
            )

        self.assertEqual(metadata["status"], "partial")
        self.assertFalse(metadata["first_team_all_pro"]["published"])
        self.assertEqual(metadata["first_team_all_pro"]["failed_years"][0]["year"], 1921)
        self.assertGreater(metadata["first_team_all_pro"]["discarded_selection_rows"], 0)
        self.assertTrue(all(row["nfl_all_pro_selections"] is None for row in rows))
        self.assertTrue(all(row["nfl_pro_bowls"] == 1 for row in rows))

    def test_loader_discards_historical_totals_when_identity_gate_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rows, metadata = load_historical_honors(
                directory,
                request_json=self._requester(fail_pageprops=True),
                latest_completed_season=1920,
            )

        self.assertFalse(rows)
        self.assertEqual(metadata["status"], "unavailable")
        self.assertFalse(metadata["identity_gate"]["published"])
        self.assertGreater(metadata["identity_gate"]["discarded_merged_rows"], 0)
        self.assertFalse(metadata["pro_bowl"]["published"])
        self.assertTrue(metadata["pro_bowl"]["parsed_complete"])
        self.assertFalse(metadata["first_team_all_pro"]["published"])
        self.assertTrue(metadata["first_team_all_pro"]["parsed_complete"])

    def test_population_metadata_does_not_hide_absent_annual_rosters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _rows, metadata = load_historical_honors(
                directory,
                request_json=self._requester(),
                latest_completed_season=1951,
            )

        pro_bowl = metadata["pro_bowl"]
        self.assertEqual(pro_bowl["annual_roster_complete_selection_years"], [1950])
        self.assertEqual(pro_bowl["annual_roster_unverified_selection_years"], [1951])
        self.assertEqual(
            pro_bowl["annual_roster_known_unavailable_selection_years"], [1951]
        )
        self.assertFalse(pro_bowl["source_population_complete"])


if __name__ == "__main__":
    unittest.main()

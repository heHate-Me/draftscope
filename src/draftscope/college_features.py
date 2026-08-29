from __future__ import annotations

import re
from typing import Any, Mapping

from .records import DataError, parse_number, slug


# Fields owned by the CFBD box-score transformer.  Keeping this list beside the
# transformer prevents an older weekly snapshot from surviving merely because
# one ingestion path forgot to clear a field that another path can generate.
CFBD_GENERATED_PRODUCTION_FIELDS = frozenset(
    {
        "prod_usage_rate",
        "prod_ppa_per_play",
        "prod_pass_completions",
        "prod_pass_attempts",
        "prod_pass_yards",
        "prod_pass_tds",
        "prod_pass_interceptions",
        "prod_completion_pct",
        "prod_yards_per_attempt",
        "prod_td_rate",
        "prod_interception_rate",
        "prod_carries",
        "prod_rush_yards",
        "prod_rush_tds",
        "prod_yards_per_carry",
        "prod_receptions",
        "prod_receiving_yards",
        "prod_receiving_tds",
        "prod_yards_per_reception",
        "prod_scrimmage_yards",
        "prod_touchdowns",
        "prod_yards_per_touch",
        "prod_sacks",
        "prod_sacks_per_game",
        "prod_tackles_for_loss",
        "prod_tfl_per_game",
        "prod_tackles",
        "prod_solo_tackles",
        "prod_interceptions",
        "prod_takeaways_per_game",
        "prod_passes_defended",
        "prod_passes_defended_per_game",
        "prod_qb_hurries",
        "prod_field_goal_attempts",
        "prod_field_goals_made",
        "prod_field_goal_pct",
        "prod_extra_point_attempts",
        "prod_extra_points_made",
        "prod_kicking_points",
        "prod_kicking_long",
        "prod_punts",
        "prod_punt_yards",
        "prod_yards_per_punt",
        "prod_punts_inside_20",
        "prod_punt_touchbacks",
        "prod_kick_returns",
        "prod_kick_return_yards",
        "prod_kick_return_tds",
        "prod_yards_per_kick_return",
        "prod_punt_returns",
        "prod_punt_return_yards",
        "prod_punt_return_tds",
        "prod_yards_per_punt_return",
        "prod_passing_success_plays",
        "prod_passing_successes",
        "prod_passing_success_rate",
        "prod_rushing_success_plays",
        "prod_rushing_successes",
        "prod_rushing_success_rate",
    }
)


def derive_player_success(row: Mapping[str, Any] | None) -> dict[str, float]:
    """Normalize CFBD's week-bounded passing/rushing success response."""

    if not row:
        return {}
    out: dict[str, float] = {}
    for source, prefix in (("passing", "passing"), ("rushing", "rushing")):
        split = row.get(source) or {}
        plays = _mapping_number(split, "plays")
        successes = _mapping_number(split, "successes")
        success_rate = _mapping_number(split, "successRate")
        if success_rate is None and plays is not None and plays > 0 and successes is not None:
            success_rate = successes / plays
        _set(out, f"prod_{prefix}_success_plays", plays)
        _set(out, f"prod_{prefix}_successes", successes)
        _set(out, f"prod_{prefix}_success_rate", success_rate)
    return out


def parse_college_stat_number(value: Any) -> float | None:
    """Parse a numeric CFBD stat while tolerating decorated source strings."""

    try:
        return parse_number(value)
    except DataError:
        match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
        return float(match.group()) if match else None


def derive_college_production(
    values: Mapping[tuple[str, str], Any],
    *,
    games: float | None = None,
    usage: Mapping[str, Any] | None = None,
    ppa: Mapping[str, Any] | None = None,
) -> dict[str, float]:
    """Derive one checkpoint-safe production record from normalized CFBD stats.

    Both historical training rows and current weekly candidates call this exact
    transformer.  ``values`` may contain raw or already-slugged category/stat
    keys; no draft outcome, future-week value, or post-college measurement is
    accepted here.
    """

    normalized = {
        (slug(category), slug(stat_name)): value
        for (category, stat_name), value in values.items()
    }
    out: dict[str, float] = {}

    usage_values = usage or {}
    ppa_values = ppa or {}
    average_ppa = ppa_values.get("average") or {}
    _set(out, "prod_usage_rate", _mapping_number(usage_values, "overall"))
    _set(out, "prod_ppa_per_play", _mapping_number(average_ppa, "all"))

    completions = _stat(normalized, "passing", "completions", "cmp")
    attempts = _stat(normalized, "passing", "att", "attempts", "passing_attempts")
    combined = _raw_stat(
        normalized,
        "passing",
        "c_att",
        "cmp_att",
        "completions_attempts",
    )
    if isinstance(combined, str) and "/" in combined:
        left, right = combined.split("/", 1)
        completions = parse_college_stat_number(left)
        attempts = parse_college_stat_number(right)
    pass_yards = _stat(normalized, "passing", "yds", "yards", "passing_yards")
    pass_tds = _stat(normalized, "passing", "td", "touchdowns", "passing_touchdowns")
    pass_interceptions = _stat(normalized, "passing", "int", "interceptions")
    _set(out, "prod_pass_completions", completions)
    _set(out, "prod_pass_attempts", attempts)
    _set(out, "prod_pass_yards", pass_yards)
    _set(out, "prod_pass_tds", pass_tds)
    _set(out, "prod_pass_interceptions", pass_interceptions)
    if attempts is not None and attempts > 0:
        if completions is not None:
            out["observed_completion_pct"] = completions / attempts
            out["prod_completion_pct"] = (completions + 0.60 * 25.0) / (attempts + 25.0)
        if pass_yards is not None:
            out["observed_yards_per_attempt"] = pass_yards / attempts
            out["prod_yards_per_attempt"] = (pass_yards + 7.0 * 25.0) / (attempts + 25.0)
        if pass_tds is not None:
            out["observed_td_rate"] = pass_tds / attempts
            out["prod_td_rate"] = (pass_tds + 0.045 * 25.0) / (attempts + 25.0)
        if pass_interceptions is not None:
            out["observed_interception_rate"] = pass_interceptions / attempts
            out["prod_interception_rate"] = (
                pass_interceptions + 0.025 * 25.0
            ) / (attempts + 25.0)

    carries = _stat(normalized, "rushing", "car", "att", "attempts", "carries")
    rush_yards = _stat(normalized, "rushing", "yds", "yards", "rushing_yards")
    rush_tds = _stat(normalized, "rushing", "td", "touchdowns", "rushing_touchdowns")
    receptions = _stat(normalized, "receiving", "rec", "receptions")
    receiving_yards = _stat(
        normalized,
        "receiving",
        "yds",
        "yards",
        "receiving_yards",
    )
    receiving_tds = _stat(
        normalized,
        "receiving",
        "td",
        "touchdowns",
        "receiving_touchdowns",
    )
    _set(out, "prod_carries", carries)
    _set(out, "prod_rush_yards", rush_yards)
    _set(out, "prod_rush_tds", rush_tds)
    _set(out, "prod_receptions", receptions)
    _set(out, "prod_receiving_yards", receiving_yards)
    _set(out, "prod_receiving_tds", receiving_tds)
    if carries is not None and carries > 0 and rush_yards is not None:
        out["observed_yards_per_carry"] = rush_yards / carries
        out["prod_yards_per_carry"] = (rush_yards + 4.5 * 20.0) / (carries + 20.0)
    if receptions is not None and receptions > 0 and receiving_yards is not None:
        out["observed_yards_per_reception"] = receiving_yards / receptions
        out["prod_yards_per_reception"] = (
            receiving_yards + 12.0 * 8.0
        ) / (receptions + 8.0)
    if rush_yards is not None or receiving_yards is not None:
        scrimmage_yards = (rush_yards or 0.0) + (receiving_yards or 0.0)
        out["prod_scrimmage_yards"] = scrimmage_yards
        touches = (carries or 0.0) + (receptions or 0.0)
        if touches > 0:
            out["observed_yards_per_touch"] = scrimmage_yards / touches
            out["prod_yards_per_touch"] = (
                scrimmage_yards + 5.5 * 15.0
            ) / (touches + 15.0)
    if rush_tds is not None or receiving_tds is not None:
        out["prod_touchdowns"] = (rush_tds or 0.0) + (receiving_tds or 0.0)

    sacks = _stat(normalized, "defensive", "sacks", "sack", "sk")
    tackles_for_loss = _stat(normalized, "defensive", "tfl", "tackles_for_loss")
    tackles = _stat(
        normalized,
        "defensive",
        "tot",
        "total",
        "tackles",
        "total_tackles",
    )
    solo_tackles = _stat(normalized, "defensive", "solo")
    passes_defended = _stat(
        normalized,
        "defensive",
        "pd",
        "passes_defended",
        "pass_breakups",
    )
    qb_hurries = _stat(normalized, "defensive", "qb_hur", "hurries")
    forced_fumbles = _stat(normalized, "defensive", "ff", "forced_fumbles")
    defensive_interceptions = _first_number(
        _stat(normalized, "interceptions", "int", "interceptions"),
        _stat(normalized, "defensive", "int", "interceptions"),
    )
    _set(out, "prod_sacks", sacks)
    _set(out, "prod_tackles_for_loss", tackles_for_loss)
    _set(out, "prod_tackles", tackles)
    _set(out, "prod_solo_tackles", solo_tackles)
    _set(out, "prod_passes_defended", passes_defended)
    _set(out, "prod_qb_hurries", qb_hurries)
    _set(out, "prod_interceptions", defensive_interceptions)
    if games is not None and games > 0:
        if sacks is not None:
            out["prod_sacks_per_game"] = sacks / games
        if tackles_for_loss is not None:
            out["prod_tfl_per_game"] = tackles_for_loss / games
        if defensive_interceptions is not None or forced_fumbles is not None:
            out["prod_takeaways_per_game"] = (
                (defensive_interceptions or 0.0) + (forced_fumbles or 0.0)
            ) / games
        if passes_defended is not None:
            out["prod_passes_defended_per_game"] = passes_defended / games

    field_goal_attempts = _stat(normalized, "kicking", "fga")
    field_goals_made = _stat(normalized, "kicking", "fgm")
    _set(out, "prod_field_goal_attempts", field_goal_attempts)
    _set(out, "prod_field_goals_made", field_goals_made)
    if (
        field_goal_attempts is not None
        and field_goal_attempts > 0
        and field_goals_made is not None
    ):
        out["prod_field_goal_pct"] = field_goals_made / field_goal_attempts
    else:
        field_goal_pct = _stat(normalized, "kicking", "pct")
        if field_goal_pct is not None:
            out["prod_field_goal_pct"] = (
                field_goal_pct / 100.0 if field_goal_pct > 1.0 else field_goal_pct
            )
    _set(out, "prod_extra_point_attempts", _stat(normalized, "kicking", "xpa"))
    _set(out, "prod_extra_points_made", _stat(normalized, "kicking", "xpm"))
    _set(out, "prod_kicking_points", _stat(normalized, "kicking", "pts", "points"))
    _set(out, "prod_kicking_long", _stat(normalized, "kicking", "long"))

    punts = _stat(normalized, "punting", "no", "punts")
    punt_yards = _stat(normalized, "punting", "yds", "yards")
    _set(out, "prod_punts", punts)
    _set(out, "prod_punt_yards", punt_yards)
    yards_per_punt = _stat(normalized, "punting", "ypp", "avg")
    if yards_per_punt is None and punts is not None and punts > 0 and punt_yards is not None:
        yards_per_punt = punt_yards / punts
    _set(out, "prod_yards_per_punt", yards_per_punt)
    _set(
        out,
        "prod_punts_inside_20",
        _stat(normalized, "punting", "in_20", "inside_20"),
    )
    _set(out, "prod_punt_touchbacks", _stat(normalized, "punting", "tb", "touchbacks"))

    for categories, prefix in (
        (("kickreturns", "kick_returns"), "kick_return"),
        (("puntreturns", "punt_returns"), "punt_return"),
    ):
        returns = _stat_from_categories(normalized, categories, "no", "returns")
        yards = _stat_from_categories(normalized, categories, "yds", "yards")
        touchdowns = _stat_from_categories(normalized, categories, "td", "touchdowns")
        _set(out, f"prod_{prefix}s", returns)
        _set(out, f"prod_{prefix}_yards", yards)
        _set(out, f"prod_{prefix}_tds", touchdowns)
        average = _stat_from_categories(normalized, categories, "avg")
        if average is None and returns is not None and returns > 0 and yards is not None:
            average = yards / returns
        _set(out, f"prod_yards_per_{prefix}", average)

    return out


def _mapping_number(values: Mapping[str, Any], key: str) -> float | None:
    value = values.get(key)
    return parse_college_stat_number(value) if value not in (None, "") else None


def _stat(
    values: Mapping[tuple[str, str], Any],
    category: str,
    *names: str,
) -> float | None:
    raw = _raw_stat(values, category, *names)
    return parse_college_stat_number(raw)


def _stat_from_categories(
    values: Mapping[tuple[str, str], Any],
    categories: tuple[str, ...],
    *names: str,
) -> float | None:
    for category in categories:
        value = _stat(values, category, *names)
        if value is not None:
            return value
    return None


def _raw_stat(
    values: Mapping[tuple[str, str], Any],
    category: str,
    *names: str,
) -> Any:
    wanted_names = {slug(name) for name in names}
    wanted_category = slug(category)
    for (raw_category, raw_name), value in values.items():
        if raw_category == wanted_category and raw_name in wanted_names:
            return value
    return None


def _first_number(*values: float | None) -> float | None:
    return next((value for value in values if value is not None), None)


def _set(target: dict[str, float], field: str, value: float | None) -> None:
    if value is not None:
        target[field] = value

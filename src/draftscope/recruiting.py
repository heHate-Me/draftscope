from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .records import normalize_name, parse_number


RECRUITING_FEATURE_FIELDS = (
    "recruit_rating",
    "recruit_stars",
    "recruit_national_rank",
)


@dataclass(frozen=True, slots=True)
class RecruitingIndex:
    """Stable CFBD recruiting crosswalk; names are never used as identities."""

    by_recruit_id: Mapping[str, tuple[dict[str, Any], ...]]
    by_athlete_id: Mapping[str, tuple[dict[str, Any], ...]]
    audit: Mapping[str, Any]


def build_recruiting_index(rows: Iterable[Mapping[str, Any]]) -> RecruitingIndex:
    by_recruit: dict[str, list[dict[str, Any]]] = {}
    by_athlete: dict[str, list[dict[str, Any]]] = {}
    materialized = [dict(row) for row in rows]
    missing_identity = 0
    invalid_rating = 0
    invalid_stars = 0
    invalid_rank = 0
    duplicate_source_rows = 0
    seen: set[tuple[str, str, int | None]] = set()

    for raw in materialized:
        recruit_id = str(raw.get("id") or raw.get("recruitId") or "").strip()
        athlete_id = str(raw.get("athleteId") or raw.get("athlete_id") or "").strip()
        year = _integer(raw.get("year"))
        if not recruit_id and not athlete_id:
            missing_identity += 1
            continue
        source_key = (recruit_id, athlete_id, year)
        if source_key in seen:
            duplicate_source_rows += 1
            continue
        seen.add(source_key)

        rating = parse_number(raw.get("rating"))
        stars = parse_number(raw.get("stars"))
        ranking = parse_number(raw.get("ranking"))
        if rating is not None and not 0.0 <= rating <= 1.0:
            rating = None
            invalid_rating += 1
        if stars is not None and not 0.0 <= stars <= 5.0:
            stars = None
            invalid_stars += 1
        if ranking is not None and ranking < 1.0:
            ranking = None
            invalid_rank += 1
        row = {
            "recruit_id": recruit_id or None,
            "athlete_id": athlete_id or None,
            "name": raw.get("name"),
            "year": year,
            "recruit_type": raw.get("recruitType") or raw.get("recruit_type"),
            "committed_to": raw.get("committedTo") or raw.get("committed_to"),
            "rating": rating,
            "stars": stars,
            "ranking": ranking,
        }
        if recruit_id:
            by_recruit.setdefault(recruit_id, []).append(row)
        if athlete_id:
            by_athlete.setdefault(athlete_id, []).append(row)

    return RecruitingIndex(
        by_recruit_id={key: tuple(value) for key, value in by_recruit.items()},
        by_athlete_id={key: tuple(value) for key, value in by_athlete.items()},
        audit={
            "raw_recruiting_rows": len(materialized),
            "recruiting_rows_missing_stable_identity_dropped": missing_identity,
            "duplicate_recruiting_source_rows_collapsed": duplicate_source_rows,
            "invalid_recruit_rating_values_set_missing": invalid_rating,
            "invalid_recruit_stars_values_set_missing": invalid_stars,
            "invalid_recruit_rank_values_set_missing": invalid_rank,
            "recruit_id_keys": len(by_recruit),
            "recruit_athlete_id_keys": len(by_athlete),
        },
    )


def match_recruiting_profile(
    roster_row: Mapping[str, Any],
    index: RecruitingIndex,
    *,
    roster_season: int | None = None,
) -> dict[str, Any]:
    """Return a pre-college pedigree only when stable source IDs agree.

    CFBD roster ``recruitIds`` are preferred. The roster athlete id is a safe
    secondary crosswalk. Normalized names are retained only for an audit check;
    they never create a match because name-only joins can silently attach a
    recruiting grade to the wrong player.
    """

    candidates: list[tuple[dict[str, Any], str]] = []
    for value in roster_row.get("recruitIds") or roster_row.get("recruit_ids") or ():
        recruit_id = str(value or "").strip()
        candidates.extend((dict(row), "recruit_id") for row in index.by_recruit_id.get(recruit_id, ()))

    athlete_id = str(
        roster_row.get("id")
        or roster_row.get("playerId")
        or roster_row.get("player_id")
        or ""
    ).strip()
    candidates.extend(
        (dict(row), "athlete_id") for row in index.by_athlete_id.get(athlete_id, ())
    )
    if not candidates:
        return {}

    # Collapse a row found through both crosswalks while retaining the stronger
    # exact recruit-id method.
    unique: dict[tuple[str, str, int | None], tuple[dict[str, Any], str]] = {}
    for row, method in candidates:
        if roster_season is not None and row.get("year") is not None:
            if int(row["year"]) > roster_season:
                continue
        key = (
            str(row.get("recruit_id") or ""),
            str(row.get("athlete_id") or ""),
            _integer(row.get("year")),
        )
        previous = unique.get(key)
        if previous is None or method == "recruit_id":
            unique[key] = (row, method)
    if not unique:
        return {}

    def evidence_key(item: tuple[dict[str, Any], str]) -> tuple[int, float, float, int, int]:
        row, method = item
        rating = parse_number(row.get("rating"))
        stars = parse_number(row.get("stars"))
        ranking = parse_number(row.get("ranking"))
        # Prefer records with richer numeric evidence, then the highest rating,
        # highest star grade, better national rank, and latest pre-roster year.
        evidence = sum(value is not None for value in (rating, stars, ranking))
        return (
            evidence,
            rating if rating is not None else -1.0,
            stars if stars is not None else -1.0,
            -int(ranking) if ranking is not None else -10_000_000,
            int(row.get("year") or 0),
        )

    selected, method = max(unique.values(), key=evidence_key)
    roster_name = normalize_name(
        roster_row.get("name")
        or " ".join(
            str(roster_row.get(key) or "") for key in ("firstName", "lastName")
        )
    )
    recruit_name = normalize_name(selected.get("name"))
    return {
        "recruit_rating": selected.get("rating"),
        "recruit_stars": selected.get("stars"),
        "recruit_national_rank": selected.get("ranking"),
        "recruit_year": selected.get("year"),
        "recruit_type": selected.get("recruit_type"),
        "recruit_match_method": method,
        "recruit_name_agrees": bool(roster_name and recruit_name and roster_name == recruit_name),
    }


def _integer(value: object) -> int | None:
    number = parse_number(value)
    if number is None or not float(number).is_integer():
        return None
    return int(number)


__all__ = [
    "RECRUITING_FEATURE_FIELDS",
    "RecruitingIndex",
    "build_recruiting_index",
    "match_recruiting_profile",
]

from __future__ import annotations

import csv
from datetime import date, datetime
import hashlib
import json
import math
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import tempfile
import unicodedata
from typing import Any, Iterable, Mapping

from .schema import (
    BASE_PLAYER_FIELDS,
    COMMON_PHYSICAL,
    FILM_CONSENSUS_METHODS,
    FILM_GRADE_STATUSES,
    FILM_OPPONENT_MIX_VALUES,
    FILM_PROVENANCE_FIELDS,
    MIN_COMPLETE_FILM_GAMES,
    MIN_COMPLETE_FILM_SNAPS,
    PLAUSIBLE_RANGES,
    POSITION_GROUPS,
    PRODUCTION,
    TEAM_PROFILE_FIELDS,
    TRAITS,
    normalize_position,
)


class DataError(ValueError):
    pass


MISSING_TEXT = {"", "na", "n/a", "nan", "none", "null", "unknown", "-"}
OUTCOME_NUMERIC = {
    "season",
    "as_of_week",
    "projected_draft_year",
    "draft_entry_probability",
    "draft_year",
    "draft_round",
    "draft_ovr",
    "draft_pick",
    "age_at_draft",
    "need_score",
    "timeline_score",
    "draft_access_min",
    "draft_access_max",
    "nfl_three_year_horizon_start",
    "nfl_three_year_horizon_end",
    "nfl_three_year_games",
    "nfl_three_year_offense_snaps",
    "nfl_three_year_defense_snaps",
    "nfl_three_year_primary_snaps",
    "nfl_three_year_special_teams_snaps",
    "nfl_three_year_total_snaps",
    "nfl_all_pro_selections",
    "nfl_pro_bowls",
    "hof_induction_year",
    "nfl_career_start",
    "nfl_career_end",
    "recruit_rating",
    "recruit_stars",
    "recruit_national_rank",
    "recruit_year",
}
TEAM_PROFILE_NUMERIC = {
    "need_confidence",
    "need_evidence_coverage",
    "starter_quality_need_score",
    "starter_quality_confidence",
    "starter_quality_evidence_coverage",
    "contract_need_score",
    "contract_confidence",
    "contract_evidence_coverage",
    "timeline_confidence",
    "timeline_evidence_coverage",
    "coaching_stability_score",
    "coaching_confidence",
    "coaching_evidence_coverage",
    "draft_access_confidence",
    "scheme_confidence",
}
PHYSICAL_KEYS = {spec.key for spec in COMMON_PHYSICAL}
TRAIT_KEYS = {f"trait_{name}" for names in TRAITS.values() for name in names}
PRODUCTION_KEYS = {f"prod_{name}" for values in PRODUCTION.values() for name, _ in values}
FILM_NUMERIC_KEYS = {
    "film_games_reviewed",
    "film_snaps_reviewed",
    "film_second_grader_agreement",
}
KNOWN_NUMERIC = (
    PHYSICAL_KEYS
    | TRAIT_KEYS
    | PRODUCTION_KEYS
    | OUTCOME_NUMERIC
    | TEAM_PROFILE_NUMERIC
    | FILM_NUMERIC_KEYS
)
PROBABILITY_KINDS = {
    "conditional_on_entry",
    "conditional_on_combine_invitation",
    "unconditional_next_draft",
    "unknown_risk_set",
}
BOOLEAN_KEYS = {
    "drafted",
    "elite",
    "outcome_label_known",
    "has_recorded_stats",
    "measurements_verified",
    "draft_declared",
    "draft_eligible",
    "data_stale",
    "benchmark_cohort_eligible",
    "active_roster_status_eligible",
    "active_roster_identity_eligible",
    "active_roster_eligible",
    "nfl_three_year_outcome_known",
    "nfl_three_year_right_censored",
    "nfl_three_year_contributor",
    "active_three_year_contributor_eligible",
    "benchmark_active_contributor_cohort_eligible",
    "benchmark_career_elite_cohort_eligible",
    "career_elite_comparison_eligible",
    "nfl_hof",
    "nfl_career_elite",
    "reference_only",
    "recruit_name_agrees",
}


def normalize_name(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", text)
    return re.sub(r"[^a-z0-9]", "", text)


def slug(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def parse_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        numeric = float(value)
        if math.isfinite(numeric) and numeric in {0.0, 1.0}:
            return bool(numeric)
    text = str(value).strip().lower()
    if text in MISSING_TEXT:
        return None
    if text in {"1", "1.0", "true", "yes", "y", "drafted", "verified"}:
        return True
    if text in {"0", "0.0", "false", "no", "n", "undrafted", "unverified"}:
        return False
    raise DataError(f"Cannot parse boolean value {value!r}")


def parse_number(value: object, *, percent_ok: bool = True) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        value = float(value)
        return None if not math.isfinite(value) else value
    text = str(value).strip().lower().replace(",", "")
    if text in MISSING_TEXT:
        return None
    is_percent = text.endswith("%")
    if is_percent:
        if not percent_ok:
            raise DataError(f"Percentage not accepted for {value!r}")
        text = text[:-1]
    try:
        parsed = float(text)
    except ValueError as exc:
        raise DataError(f"Cannot parse numeric value {value!r}") from exc
    if not math.isfinite(parsed):
        return None
    return parsed / 100.0 if is_percent else parsed


def parse_height(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    text = str(value).strip().lower()
    if text in MISSING_TEXT:
        return None
    match = re.fullmatch(r"\s*(\d)\s*[-' ]\s*(\d{1,2})(?:\s*\"|\s*in)?\s*", text)
    if match:
        return float(int(match.group(1)) * 12 + int(match.group(2)))
    return parse_number(text)


def _age_on_draft_day(birth: str, draft_year: int) -> float | None:
    try:
        born = date.fromisoformat(birth[:10])
    except (TypeError, ValueError) as exc:
        raise DataError(f"date_of_birth must be ISO YYYY-MM-DD, got {birth!r}") from exc
    # April 30 is a stable comparison date across draft classes.
    target = date(draft_year, 4, 30)
    age = (target - born).days / 365.2425
    low, high = PLAUSIBLE_RANGES["age_at_draft"]
    return round(age, 3) if low <= age <= high else None


def derive_features(record: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(record)
    height = parse_height(out.get("height_in"))
    weight = parse_number(out.get("weight_lb"))
    forty = parse_number(out.get("forty_s"))
    vertical = parse_number(out.get("vertical_in"))
    broad = parse_number(out.get("broad_jump_in"))
    if height is not None:
        out["height_in"] = height
    if weight is not None:
        out["weight_lb"] = weight
    if height and weight:
        out["bmi"] = round(703.0 * weight / (height * height), 4)
    if weight and forty and forty > 0:
        out["speed_score"] = round(weight * 200.0 / (forty**4), 4)
    if vertical is not None and broad is not None:
        # Transparent composite; the broad jump is scaled to the vertical's range.
        out["explosion_index"] = round(vertical + broad / 4.0, 4)
    if out.get("date_of_birth") and not out.get("age_at_draft"):
        draft_year = out.get("projected_draft_year") or out.get("draft_year")
        if draft_year:
            out["age_at_draft"] = _age_on_draft_day(str(out["date_of_birth"]), int(float(draft_year)))
    return out


def normalize_record(record: Mapping[str, Any]) -> dict[str, Any]:
    aliases = {
        "player_name": "name",
        "pos": "position",
        "school_name": "school",
        "college": "school",
        "ht": "height_in",
        "wt": "weight_lb",
        "forty": "forty_s",
        "bench": "bench_reps",
        "vertical": "vertical_in",
        "broad_jump": "broad_jump_in",
        "cone": "three_cone_s",
        "shuttle": "shuttle_s",
        "draft_ovr": "draft_ovr",
    }
    out: dict[str, Any] = {}
    for raw_key, raw_value in record.items():
        key = aliases.get(str(raw_key).strip(), str(raw_key).strip())
        if isinstance(raw_value, str):
            raw_value = raw_value.strip()
        if raw_value is None or (isinstance(raw_value, str) and raw_value.lower() in MISSING_TEXT):
            out[key] = None
            continue
        if key == "height_in":
            out[key] = parse_height(raw_value)
        elif key.startswith("trait_") or key in FILM_NUMERIC_KEYS:
            if isinstance(raw_value, bool):
                raise DataError(f"Boolean value is not valid for numeric field {key}")
            out[key] = parse_number(raw_value, percent_ok=False)
        elif key in KNOWN_NUMERIC or key.startswith("prod_"):
            out[key] = parse_number(raw_value)
        elif key in BOOLEAN_KEYS:
            out[key] = parse_bool(raw_value)
        else:
            out[key] = raw_value
    if out.get("position"):
        out["position_raw"] = out.get("position_raw") or str(out["position"])
        out["position"] = normalize_position(out["position"])
    return derive_features(out)


def load_records(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.exists():
        raise DataError(f"Data file does not exist: {source}")
    if source.suffix.lower() == ".json":
        parsed = json.loads(source.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            parsed = parsed.get("players") or parsed.get("records") or [parsed]
        if not isinstance(parsed, list):
            raise DataError("JSON data must be a list or contain a players/records list")
        rows = parsed
    else:
        with source.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
    return [normalize_record(row) for row in rows]


def _ordered_fields(rows: Iterable[Mapping[str, Any]], preferred: Iterable[str] = ()) -> list[str]:
    seen: set[str] = set()
    fields: list[str] = []
    for key in preferred:
        if key not in seen:
            fields.append(key)
            seen.add(key)
    for row in rows:
        for key in row:
            if key not in seen and key != "position_raw":
                fields.append(key)
                seen.add(key)
    return fields


def write_records(
    path: str | os.PathLike[str],
    rows: Iterable[Mapping[str, Any]],
    *,
    preferred_fields: Iterable[str] = BASE_PLAYER_FIELDS,
) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    materialized = [dict(row) for row in rows]
    if destination.suffix.lower() == ".json":
        payload = json.dumps(materialized, indent=2, sort_keys=True, default=str) + "\n"
        _atomic_text(destination, payload)
        return
    fields = _ordered_fields(materialized, preferred_fields)
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent, text=True)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in materialized:
                writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in fields})
        os.replace(temp_name, destination)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def file_sha256(path: str | os.PathLike[str]) -> str:
    """Return a streaming SHA-256 digest for an on-disk data artifact."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_text(destination: Path, text: str) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp_name, destination)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def candidate_template_fields() -> list[str]:
    fields = list(BASE_PLAYER_FIELDS)
    for key in (
        "height_in", "weight_lb", "arm_length_in", "hand_size_in", "wingspan_in",
        "forty_s", "ten_split_s", "bench_reps", "vertical_in", "broad_jump_in",
        "three_cone_s", "shuttle_s",
    ):
        if key not in fields:
            fields.append(key)
    for group in POSITION_GROUPS:
        for name, _ in PRODUCTION.get(group, ()):
            key = f"prod_{name}"
            if key not in fields:
                fields.append(key)
        for name in TRAITS.get(group, ()):
            key = f"trait_{name}"
            if key not in fields:
                fields.append(key)
    return fields


def historical_template_fields() -> list[str]:
    return candidate_template_fields() + [
        "draft_year",
        "drafted",
        "draft_round",
        "draft_ovr",
        "elite",
        "nfl_hof",
        "nfl_all_pro_selections",
        "nfl_pro_bowls",
        "nfl_career_elite",
        "population",
        "probability_kind",
        "probability_condition",
    ]


def team_template_fields() -> list[str]:
    fields = list(TEAM_PROFILE_FIELDS)
    for key in sorted(PHYSICAL_KEYS | TRAIT_KEYS):
        fields.append(f"ideal_{key}")
        fields.append(f"tolerance_{key}")
        fields.append(f"importance_{key}")
    return fields


_FILM_GAME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,79}")
_REQUIRED_FILM_FIELDS = (
    "film_grade_status",
    "film_grader",
    "film_graded_at",
    "film_game_ids",
    "film_games_reviewed",
    "film_snaps_reviewed",
    "film_opponent_mix",
    "film_grade_source",
    "film_notes_path",
)


def _populated(value: Any) -> bool:
    return value is not None and not (
        isinstance(value, str) and value.strip().lower() in MISSING_TEXT
    )


def parse_film_game_ids(value: Any) -> tuple[str, ...]:
    """Parse portable game identifiers from JSON lists or CSV semicolon text."""

    if not _populated(value):
        return ()
    if isinstance(value, (list, tuple)):
        raw_values = list(value)
        if any(not isinstance(item, str) for item in raw_values):
            raise DataError("film_game_ids JSON values must be strings")
    elif isinstance(value, str):
        raw_values = value.split(";")
    else:
        raise DataError("film_game_ids must be a JSON list or semicolon-delimited text")
    identifiers = tuple(str(item).strip() for item in raw_values)
    if not identifiers or any(not item for item in identifiers):
        raise DataError("film_game_ids contains an empty identifier")
    if any(item.casefold() in MISSING_TEXT for item in identifiers):
        raise DataError("film_game_ids contains a missing-value placeholder")
    malformed = [item for item in identifiers if _FILM_GAME_ID.fullmatch(item) is None]
    if malformed:
        raise DataError(
            "film_game_ids must use stable readable identifiers containing only "
            "letters, numbers, periods, underscores, colons, or hyphens"
        )
    folded = [item.casefold() for item in identifiers]
    if len(folded) != len(set(folded)):
        raise DataError("film_game_ids contains a duplicate identifier")
    return identifiers


def parse_film_opponent_mix(value: Any) -> tuple[str, ...]:
    if not _populated(value):
        return ()
    if isinstance(value, (list, tuple)):
        raw_values = list(value)
        if any(not isinstance(item, str) for item in raw_values):
            raise DataError("film_opponent_mix JSON values must be strings")
    elif isinstance(value, str):
        raw_values = value.split(";")
    else:
        raise DataError("film_opponent_mix must be a JSON list or semicolon-delimited text")
    tags = tuple(str(item).strip().lower() for item in raw_values)
    if not tags or any(not item for item in tags):
        raise DataError("film_opponent_mix contains an empty tag")
    if len(tags) != len(set(tags)):
        raise DataError("film_opponent_mix contains a duplicate tag")
    unsupported = sorted(set(tags) - set(FILM_OPPONENT_MIX_VALUES))
    if unsupported:
        raise DataError(
            "film_opponent_mix contains unsupported tags: " + ", ".join(unsupported)
        )
    return tags


def _film_status(row: Mapping[str, Any]) -> str:
    return str(row.get("film_grade_status") or "").strip().lower()


def _portable_notes_path(value: Any) -> bool:
    text = str(value or "").strip()
    if (
        not isinstance(value, str)
        or not text
        or "\\" in text
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", text)
        or re.match(r"^[A-Za-z]:", text)
    ):
        return False
    path = PurePosixPath(text)
    return (
        not path.is_absolute()
        and text != "."
        and ".." not in path.parts
        and not text.startswith("~")
        and path.as_posix() == text
    )


def _portable_source(value: Any) -> bool:
    text = str(value or "").strip()
    if not isinstance(value, str) or not text:
        return False
    lowered = text.casefold()
    if lowered.startswith(("/", "~", "file:")) or "\\" in text:
        return False
    if re.match(r"^[A-Za-z]:", text):
        return False
    if re.match(r"^https?://[^\s]+$", text, flags=re.IGNORECASE):
        return True
    if "://" in text or "/" in text:
        return False
    return True


def _integer_field(row: Mapping[str, Any], key: str) -> tuple[int | None, str | None]:
    if isinstance(row.get(key), bool):
        return None, f"{key} must be a non-negative integer"
    try:
        value = parse_number(row.get(key), percent_ok=False)
    except DataError:
        return None, f"{key} must be a non-negative integer"
    if value is None:
        return None, None
    if value < 0 or not value.is_integer():
        return None, f"{key} must be a non-negative integer"
    return int(value), None


def _film_protocol_findings(
    row: Mapping[str, Any],
    *,
    row_number: int,
    include_provisional_note: bool = True,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []

    def add(field: str, message: str, severity: str = "high") -> None:
        findings.append(
            {
                "severity": severity,
                "row": str(row_number),
                "field": field,
                "message": message,
            }
        )

    numeric_traits: list[str] = []
    populated_traits: list[str] = []
    position = normalize_position(row.get("position"))
    allowed_traits = {f"trait_{name}" for name in TRAITS.get(position, ())}
    for key, value in row.items():
        if not key.startswith("trait_") or not _populated(value):
            continue
        populated_traits.append(key)
        if key not in allowed_traits:
            add(key, f"{key} is not a defined manual film trait for position {position or 'unknown'}")
            continue
        if isinstance(value, bool):
            add(key, "Trait grades must be numeric values from 0–100, not booleans")
            continue
        try:
            grade = parse_number(value, percent_ok=False)
        except DataError:
            add(key, "Trait grades must be numeric values from 0–100")
            continue
        if grade is None or not 0 <= grade <= 100:
            add(key, "Trait grades must be 0–100")
            continue
        numeric_traits.append(key)

    film_metadata_present = any(_populated(row.get(key)) for key in FILM_PROVENANCE_FIELDS)
    if not populated_traits and not film_metadata_present:
        return findings

    status = _film_status(row)
    if status and status not in FILM_GRADE_STATUSES:
        add(
            "film_grade_status",
            "Film grade status must be complete, provisional, or insufficient",
        )
    if populated_traits:
        for field in _REQUIRED_FILM_FIELDS:
            if not _populated(row.get(field)):
                add(field, "Required when any manual film trait grade is populated")

    for field in ("film_grader", "film_grade_source", "film_notes_path"):
        if _populated(row.get(field)) and not isinstance(row.get(field), str):
            add(field, f"{field} must be text")

    graded_at = str(row.get("film_graded_at") or "").strip()
    if graded_at:
        try:
            parsed_date = date.fromisoformat(graded_at)
        except ValueError:
            parsed_date = None
        if parsed_date is None or parsed_date.isoformat() != graded_at:
            add("film_graded_at", "Film grading date must use ISO YYYY-MM-DD")

    try:
        game_ids = parse_film_game_ids(row.get("film_game_ids"))
    except DataError as exc:
        game_ids = ()
        add("film_game_ids", str(exc))
    try:
        opponent_mix = parse_film_opponent_mix(row.get("film_opponent_mix"))
    except DataError as exc:
        opponent_mix = ()
        add("film_opponent_mix", str(exc))

    games_reviewed, games_error = _integer_field(row, "film_games_reviewed")
    snaps_reviewed, snaps_error = _integer_field(row, "film_snaps_reviewed")
    if games_error:
        add("film_games_reviewed", games_error)
    if snaps_error:
        add("film_snaps_reviewed", snaps_error)
    if games_reviewed is not None and game_ids and games_reviewed != len(game_ids):
        add(
            "film_games_reviewed",
            "film_games_reviewed must equal the number of unique film_game_ids",
        )

    if _populated(row.get("film_grade_source")) and not _portable_source(
        row.get("film_grade_source")
    ):
        add("film_grade_source", "Film source must not contain a local file path")
    if _populated(row.get("film_notes_path")) and not _portable_notes_path(
        row.get("film_notes_path")
    ):
        add("film_notes_path", "Film notes path must be a portable relative POSIX path")

    if status == "complete" and populated_traits:
        if len(game_ids) < MIN_COMPLETE_FILM_GAMES or (
            games_reviewed is not None and games_reviewed < MIN_COMPLETE_FILM_GAMES
        ):
            add(
                "film_games_reviewed",
                f"Complete film grades require at least {MIN_COMPLETE_FILM_GAMES} complete games",
            )
        if snaps_reviewed is None or snaps_reviewed < MIN_COMPLETE_FILM_SNAPS:
            add(
                "film_snaps_reviewed",
                f"Complete film grades require at least {MIN_COMPLETE_FILM_SNAPS} relevant snaps",
            )
        mix = set(opponent_mix)
        if "recent" not in mix:
            add("film_opponent_mix", "Complete film grades require one recent game")
        if "strongest_available" not in mix:
            add(
                "film_opponent_mix",
                "Complete film grades require the strongest available opponent",
            )
        if not mix.intersection(
            {"adversity", "lower_production", "different_game_script"}
        ):
            add(
                "film_opponent_mix",
                "Complete film grades require adversity, lower production, or a different game script",
            )
    elif status == "provisional" and numeric_traits:
        if len(game_ids) < 1 or games_reviewed is None or games_reviewed < 1:
            add(
                "film_games_reviewed",
                "Provisional film grades require at least one reviewed game",
            )
        if snaps_reviewed is None or snaps_reviewed < 1:
            add(
                "film_snaps_reviewed",
                "Provisional film grades require at least one relevant snap",
            )
        if include_provisional_note:
            add(
                "film_grade_status",
                "Provisional film grades are display-only and excluded from the overall profile and team fit",
                severity="info",
            )
    elif status == "insufficient" and numeric_traits:
        add(
            "film_grade_status",
            "Numeric trait grades cannot be published when film evidence is insufficient",
        )

    second_grader = str(row.get("film_second_grader") or "").strip()
    agreement_present = _populated(row.get("film_second_grader_agreement"))
    method = str(row.get("film_consensus_method") or "").strip().lower()
    agreement: float | None = None
    if agreement_present:
        try:
            agreement = (
                None
                if isinstance(row.get("film_second_grader_agreement"), bool)
                else parse_number(
                    row.get("film_second_grader_agreement"), percent_ok=False
                )
            )
        except DataError:
            agreement = None
        if agreement is None or not 0 <= agreement <= 1:
            add(
                "film_second_grader_agreement",
                "Second-grader agreement must be a number from 0–1",
            )
    if method and method not in FILM_CONSENSUS_METHODS:
        add(
            "film_consensus_method",
            "Consensus method must be independent_average, lead_grader, or discussion_consensus",
        )
    if _populated(row.get("film_second_grader")) and not isinstance(
        row.get("film_second_grader"), str
    ):
        add("film_second_grader", "film_second_grader must be text")
    if second_grader and second_grader.casefold() == str(
        row.get("film_grader") or ""
    ).strip().casefold():
        add("film_second_grader", "Second grader must differ from the primary grader")
    if second_grader:
        if not agreement_present:
            add(
                "film_second_grader_agreement",
                "Second-grader agreement is required when a second grader is named",
            )
        if not method:
            add(
                "film_consensus_method",
                "Consensus method is required when a second grader is named",
            )
    elif agreement_present or method:
        add(
            "film_second_grader",
            "A second grader must be named before agreement or consensus metadata is recorded",
        )
    return findings


def complete_film_grades_available(row: Mapping[str, Any]) -> bool:
    """Return whether manual traits may affect scouting/profile calculations."""

    if _film_status(row) != "complete":
        return False
    if not any(
        key.startswith("trait_") and _populated(value) for key, value in row.items()
    ):
        return False
    return not any(
        finding["severity"] == "high"
        for finding in _film_protocol_findings(
            row,
            row_number=0,
            include_provisional_note=False,
        )
    )


def film_grades_displayable(row: Mapping[str, Any]) -> bool:
    """Return whether manual trait values are safe to include in an output."""

    if _film_status(row) not in {"complete", "provisional"}:
        return False
    if not any(
        key.startswith("trait_") and _populated(value)
        for key, value in row.items()
    ):
        return False
    return not any(
        finding["severity"] == "high"
        for finding in _film_protocol_findings(
            row,
            row_number=0,
            include_provisional_note=False,
        )
    )


def film_grade_audit_findings(
    row: Mapping[str, Any], *, row_number: int = 2
) -> list[dict[str, str]]:
    """Return the film-only findings used by audits and output gates."""

    return _film_protocol_findings(row, row_number=row_number)


def audit_records(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    materialized = list(rows)
    findings: list[dict[str, str]] = []
    keys: dict[tuple[str, str, str], int] = {}
    for index, row in enumerate(materialized, start=2):
        name = str(row.get("name") or "").strip()
        position = str(row.get("position") or "").strip()
        if not name:
            findings.append({"severity": "high", "row": str(index), "field": "name", "message": "Missing player name"})
        if position not in POSITION_GROUPS:
            findings.append({"severity": "high", "row": str(index), "field": "position", "message": f"Unrecognized position {position!r}"})
        identity = (normalize_name(name), str(row.get("school") or "").lower(), str(row.get("season") or ""))
        if identity in keys:
            findings.append({"severity": "high", "row": str(index), "field": "name", "message": f"Duplicate player-season; first seen on row {keys[identity]}"})
        else:
            keys[identity] = index
        for key, (low, high) in PLAUSIBLE_RANGES.items():
            value = parse_number(row.get(key))
            if value is not None and not low <= value <= high:
                findings.append({"severity": "high", "row": str(index), "field": key, "message": f"{value:g} is outside plausible range {low:g}–{high:g}"})
        findings.extend(_film_protocol_findings(row, row_number=index))
        entry_probability = parse_number(row.get("draft_entry_probability"))
        declared = parse_bool(row.get("draft_declared"))
        eligible = parse_bool(row.get("draft_eligible"))
        if entry_probability is not None and not 0.0 <= entry_probability <= 1.0:
            findings.append(
                {
                    "severity": "high",
                    "row": str(index),
                    "field": "draft_entry_probability",
                    "message": "Draft entry probability must be between 0 and 1",
                }
            )
        if declared is True and eligible is False:
            findings.append(
                {
                    "severity": "high",
                    "row": str(index),
                    "field": "draft_declared",
                    "message": "A player cannot be both declared and ineligible for the next draft",
                }
            )
        if declared is True and entry_probability is not None and not math.isclose(entry_probability, 1.0):
            findings.append(
                {
                    "severity": "high",
                    "row": str(index),
                    "field": "draft_entry_probability",
                    "message": "A declared player's draft entry probability must be 1",
                }
            )
        if eligible is False and entry_probability is not None and not math.isclose(entry_probability, 0.0):
            findings.append(
                {
                    "severity": "high",
                    "row": str(index),
                    "field": "draft_entry_probability",
                    "message": "An ineligible player's next-draft entry probability must be 0",
                }
            )
        probability_kind = str(row.get("probability_kind") or "").strip()
        if probability_kind and probability_kind not in PROBABILITY_KINDS:
            findings.append(
                {
                    "severity": "high",
                    "row": str(index),
                    "field": "probability_kind",
                    "message": "Probability kind must name a supported risk-set contract",
                }
            )
    return findings


def timestamp_utc() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")

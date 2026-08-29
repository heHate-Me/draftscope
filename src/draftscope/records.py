from __future__ import annotations

import csv
from datetime import date, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import unicodedata
from typing import Any, Iterable, Mapping

from .schema import (
    BASE_PLAYER_FIELDS,
    COMMON_PHYSICAL,
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
KNOWN_NUMERIC = (
    PHYSICAL_KEYS
    | TRAIT_KEYS
    | PRODUCTION_KEYS
    | OUTCOME_NUMERIC
    | TEAM_PROFILE_NUMERIC
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
        elif key in KNOWN_NUMERIC or key.startswith("trait_") or key.startswith("prod_"):
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
        for key, value in row.items():
            if key.startswith("trait_"):
                parsed = parse_number(value)
                if parsed is not None and not 0 <= parsed <= 100:
                    findings.append({"severity": "high", "row": str(index), "field": key, "message": "Trait grades must be 0–100"})
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

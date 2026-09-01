from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Iterable, Mapping

from .mathstats import weighted_mean
from .records import complete_film_grades_available, parse_number
from .schema import PHYSICAL_KEYS_BY_POSITION, TRAITS, normalize_position


DEFAULT_TOLERANCE = {
    "height_in": 2.0,
    "weight_lb": 15.0,
    "forty_s": 0.18,
    "ten_split_s": 0.09,
    "bench_reps": 5.0,
    "vertical_in": 4.0,
    "broad_jump_in": 8.0,
    "three_cone_s": 0.25,
    "shuttle_s": 0.20,
    "arm_length_in": 1.5,
    "hand_size_in": 0.6,
    "wingspan_in": 3.0,
}


@dataclass(slots=True)
class TeamFit:
    team: str
    score: float
    evidence_coverage: float
    confidence: float
    confidence_label: str
    need_score: float | None
    starter_quality_need_score: float | None
    contract_need_score: float | None
    scheme_score: float | None
    draft_access_score: float | None
    timeline_score: float | None
    coaching_stability_score: float | None
    need_evidence_coverage: float | None = None
    need_confidence: float | None = None
    room_count: int | None = None
    league_room_median: float | None = None
    average_age: float | None = None
    average_experience: float | None = None
    age_experience_pressure: float | None = None
    recent_position_picks: int | None = None
    best_recent_position_pick: int | None = None
    recent_draft_window: str = ""
    profile_source: str = ""
    profile_as_of_date: str = ""
    scheme: str = ""
    reasons: list[str] = field(default_factory=list)
    unsupported_components: list[str] = field(default_factory=list)
    component_coverage: dict[str, float] = field(default_factory=dict)
    notes: str = ""


def _scheme_fit(player: Mapping[str, Any], profile: Mapping[str, Any], position: str) -> tuple[float | None, float, list[str]]:
    pairs: list[tuple[float | None, float]] = []
    evidence_weight = 0.0
    possible_weight = 0.0
    strengths: list[tuple[float, str]] = []
    keys = list(PHYSICAL_KEYS_BY_POSITION.get(position, ()))
    if complete_film_grades_available(player):
        keys.extend(f"trait_{name}" for name in TRAITS.get(position, ()))
    for key in keys:
        importance = parse_number(profile.get(f"importance_{key}"))
        if importance is None or importance <= 0:
            continue
        possible_weight += importance
        actual = parse_number(player.get(key))
        if actual is None:
            continue
        if key.startswith("trait_"):
            score = max(0.0, min(100.0, actual))
        else:
            ideal = parse_number(profile.get(f"ideal_{key}"))
            if ideal is None:
                continue
            tolerance = parse_number(profile.get(f"tolerance_{key}")) or DEFAULT_TOLERANCE.get(key, max(abs(ideal) * 0.08, 1.0))
            score = 100.0 * math.exp(-0.5 * ((actual - ideal) / tolerance) ** 2)
        pairs.append((score, importance))
        evidence_weight += importance
        strengths.append((score, key.replace("trait_", "").replace("_", " ")))
    strengths.sort(reverse=True)
    reasons = [f"scheme match: {label}" for score, label in strengths[:2] if score >= 65]
    return weighted_mean(pairs), (evidence_weight / possible_weight if possible_weight else 0.0), reasons


def _draft_access(profile: Mapping[str, Any], pick_range: tuple[float, float, float] | None) -> float | None:
    low = parse_number(profile.get("draft_access_min"))
    high = parse_number(profile.get("draft_access_max"))
    if low is None or high is None or pick_range is None:
        return None
    if low > high:
        low, high = high, low
    player_low, player_mid, player_high = pick_range
    if player_high >= low and player_low <= high:
        if low <= player_mid <= high:
            return 100.0
        return 80.0
    distance = low - player_high if player_high < low else player_low - high
    return max(0.0, 100.0 - 2.5 * distance)


def _fraction(value: Any, *, default: float) -> float:
    parsed = parse_number(value)
    if parsed is None:
        return default
    if 1.0 < parsed <= 100.0:
        parsed /= 100.0
    return max(0.0, min(1.0, parsed))


def _int_or_none(value: Any) -> int | None:
    parsed = parse_number(value)
    return int(parsed) if parsed is not None else None


def _confidence_label(coverage: float, confidence: float) -> str:
    effective = coverage * confidence
    if coverage >= 0.75 and confidence >= 0.80:
        return "strong"
    if effective >= 0.40:
        return "moderate"
    return "limited"


def _automatic_reasons(profile: Mapping[str, Any], position: str) -> list[str]:
    reasons: list[str] = []
    starter_quality = parse_number(profile.get("starter_quality_need_score"))
    if starter_quality is not None and starter_quality >= 65:
        reasons.append(f"starter-quality opportunity {starter_quality:.0f}/100")
    contract_need = parse_number(profile.get("contract_need_score"))
    if contract_need is not None and contract_need >= 65:
        reasons.append(f"contract-window opportunity {contract_need:.0f}/100")
    coaching = parse_number(profile.get("coaching_stability_score"))
    if coaching is not None and coaching >= 70:
        reasons.append(f"coaching continuity {coaching:.0f}/100")
    room = _int_or_none(profile.get("room_count"))
    league_median = parse_number(profile.get("league_room_median"))
    depth_pressure = parse_number(profile.get("room_depth_pressure"))
    if room is not None and league_median is not None and depth_pressure is not None and depth_pressure >= 55:
        reasons.append(f"listed room: {room} vs NFL median {league_median:g}")

    transition = parse_number(profile.get("age_experience_pressure"))
    average_age = parse_number(profile.get("average_age"))
    average_experience = parse_number(profile.get("average_experience"))
    if transition is not None and transition >= 65:
        details: list[str] = []
        if average_age is not None:
            details.append(f"age {average_age:.1f}")
        if average_experience is not None:
            details.append(f"exp {average_experience:.1f}y")
        reasons.append("veteran-room pressure" + (f": {', '.join(details)}" if details else ""))

    lack_investment = parse_number(profile.get("lack_recent_draft_investment"))
    recent_picks = _int_or_none(profile.get("recent_position_picks"))
    window = str(profile.get("recent_draft_window") or "").strip()
    if (
        lack_investment is not None
        and recent_picks is not None
        and (lack_investment >= 65 or recent_picks == 0)
    ):
        reasons.append(
            f"recent investment: {recent_picks} {position} pick(s)"
            + (f" in {window}" if window and window != "unavailable" else "")
        )
    return reasons


def rank_team_fits(
    player: Mapping[str, Any],
    profiles: Iterable[Mapping[str, Any]],
    *,
    pick_range: tuple[float, float, float] | None = None,
    limit: int = 8,
) -> list[TeamFit]:
    position = normalize_position(player.get("position"))
    results: list[TeamFit] = []
    for raw in profiles:
        if normalize_position(raw.get("position")) != position:
            continue
        team = str(raw.get("team") or "").strip()
        if not team:
            continue
        need = parse_number(raw.get("need_score"))
        starter_quality = parse_number(raw.get("starter_quality_need_score"))
        contract_need = parse_number(raw.get("contract_need_score"))
        timeline = parse_number(raw.get("timeline_score"))
        coaching = parse_number(raw.get("coaching_stability_score"))
        scheme, scheme_coverage, reasons = _scheme_fit(player, raw, position)
        access = _draft_access(raw, pick_range)
        component_weights = {
            "need": 0.45,
            "starter_quality": 0.10,
            "contract": 0.10,
            "scheme": 0.20,
            "access": 0.10,
            "timeline": 0.03,
            "coaching": 0.02,
        }
        component_values = {
            "need": need,
            "starter_quality": starter_quality,
            "contract": contract_need,
            "scheme": scheme,
            "access": access,
            "timeline": timeline,
            "coaching": coaching,
        }
        component_coverage = {
            "need": _fraction(raw.get("need_evidence_coverage"), default=1.0 if need is not None else 0.0),
            "starter_quality": _fraction(
                raw.get("starter_quality_evidence_coverage"),
                default=1.0 if starter_quality is not None else 0.0,
            ),
            "contract": _fraction(
                raw.get("contract_evidence_coverage"),
                default=1.0 if contract_need is not None else 0.0,
            ),
            "scheme": scheme_coverage if scheme is not None else 0.0,
            "access": 1.0 if access is not None else 0.0,
            "timeline": _fraction(
                raw.get("timeline_evidence_coverage"),
                default=1.0 if timeline is not None else 0.0,
            ),
            "coaching": _fraction(
                raw.get("coaching_evidence_coverage"),
                default=1.0 if coaching is not None else 0.0,
            ),
        }
        component_confidence = {
            "need": _fraction(raw.get("need_confidence"), default=0.75),
            "starter_quality": _fraction(
                raw.get("starter_quality_confidence"), default=0.75
            ),
            "contract": _fraction(raw.get("contract_confidence"), default=0.75),
            "scheme": _fraction(raw.get("scheme_confidence"), default=0.75),
            "access": _fraction(raw.get("draft_access_confidence"), default=0.75),
            "timeline": _fraction(raw.get("timeline_confidence"), default=0.75),
            "coaching": _fraction(raw.get("coaching_confidence"), default=0.75),
        }
        pairs = tuple(
            (
                component_values[name],
                component_weights[name] * component_coverage[name] * component_confidence[name],
            )
            for name in component_weights
        )
        raw_score = weighted_mean(pairs)
        if raw_score is None:
            continue
        coverage = sum(
            component_weights[name] * component_coverage[name]
            for name, value in component_values.items()
            if value is not None
        )
        confidence = weighted_mean(
            (
                component_confidence[name],
                component_weights[name] * component_coverage[name],
            )
            for name, value in component_values.items()
            if value is not None
        ) or 0.0
        coverage = max(0.0, min(1.0, coverage))
        # Sparse or lower-confidence profiles regress toward neutral instead of
        # creating false precision. Missing components are never imputed as 50.
        score = 50.0 + (raw_score - 50.0) * coverage * confidence
        reasons = _automatic_reasons(raw, position) + reasons
        if need is not None and need >= 70 and not reasons:
            reasons.append(f"position-need evidence {need:.0f}/100")
        if access is not None and access >= 80:
            reasons.append("projected draft range is accessible")
        results.append(
            TeamFit(
                team=team,
                score=score,
                evidence_coverage=coverage,
                confidence=confidence,
                confidence_label=_confidence_label(coverage, confidence),
                need_score=need,
                starter_quality_need_score=starter_quality,
                contract_need_score=contract_need,
                scheme_score=scheme,
                draft_access_score=access,
                timeline_score=timeline,
                coaching_stability_score=coaching,
                need_evidence_coverage=component_coverage["need"] if need is not None else None,
                need_confidence=component_confidence["need"] if need is not None else None,
                room_count=_int_or_none(raw.get("room_count")),
                league_room_median=parse_number(raw.get("league_room_median")),
                average_age=parse_number(raw.get("average_age")),
                average_experience=parse_number(raw.get("average_experience")),
                age_experience_pressure=parse_number(raw.get("age_experience_pressure")),
                recent_position_picks=_int_or_none(raw.get("recent_position_picks")),
                best_recent_position_pick=_int_or_none(raw.get("best_recent_position_pick")),
                recent_draft_window=str(raw.get("recent_draft_window") or ""),
                profile_source=str(raw.get("profile_source") or ""),
                profile_as_of_date=str(raw.get("profile_as_of_date") or ""),
                scheme=str(raw.get("scheme") or ""),
                reasons=reasons,
                unsupported_components=[
                    name for name, value in component_values.items() if value is None
                ],
                component_coverage={name: round(value, 4) for name, value in component_coverage.items()},
                notes=str(raw.get("notes") or ""),
            )
        )
    return sorted(results, key=lambda fit: (fit.score, fit.evidence_coverage), reverse=True)[:limit]

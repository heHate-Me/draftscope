from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


@dataclass(frozen=True, slots=True)
class MetricSpec:
    key: str
    label: str
    unit: str = ""
    direction: str = "higher"  # higher, lower, target, or neutral
    category: str = "physical"
    weight: float = 1.0


POSITION_ALIASES: dict[str, str] = {
    "QB": "QB",
    "RB": "RB",
    "HB": "RB",
    "TB": "RB",
    "FB": "RB",
    "WR": "WR",
    "SE": "WR",
    "FL": "WR",
    "TE": "TE",
    "OT": "OT",
    "T": "OT",
    "LT": "OT",
    "RT": "OT",
    "OL": "IOL",
    "IOL": "IOL",
    "OG": "IOL",
    "G": "IOL",
    "LG": "IOL",
    "RG": "IOL",
    "C": "IOL",
    "OC": "IOL",
    "EDGE": "EDGE",
    "DE": "EDGE",
    "OLB": "EDGE",
    "IDL": "IDL",
    "DL": "IDL",
    "DT": "IDL",
    "NT": "IDL",
    "LB": "LB",
    "ILB": "LB",
    "MLB": "LB",
    "WLB": "LB",
    "SLB": "LB",
    "CB": "CB",
    "DB": "S",
    "S": "S",
    "SAF": "S",
    "FS": "S",
    "SS": "S",
    "K": "K",
    "PK": "K",
    "P": "P",
    "LS": "LS",
}

POSITION_GROUPS = tuple(dict.fromkeys(POSITION_ALIASES.values()))


def normalize_position(value: object) -> str:
    raw = str(value or "").upper().strip()
    if raw in POSITION_ALIASES:
        return POSITION_ALIASES[raw]
    for part in re.split(r"[/\- ,]+", raw):
        if part in POSITION_ALIASES:
            return POSITION_ALIASES[part]
    return raw


COMMON_PHYSICAL = (
    MetricSpec("height_in", "Height", "in", "target", "physical", 0.65),
    MetricSpec("weight_lb", "Weight", "lb", "target", "physical", 0.75),
    MetricSpec("forty_s", "40-yard dash", "s", "lower", "physical", 1.30),
    MetricSpec("ten_split_s", "10-yard split", "s", "lower", "physical", 1.15),
    MetricSpec("bench_reps", "Bench press", "reps", "higher", "physical", 0.65),
    MetricSpec("vertical_in", "Vertical jump", "in", "higher", "physical", 0.95),
    MetricSpec("broad_jump_in", "Broad jump", "in", "higher", "physical", 0.95),
    MetricSpec("three_cone_s", "Three-cone", "s", "lower", "physical", 0.85),
    MetricSpec("shuttle_s", "Short shuttle", "s", "lower", "physical", 0.85),
    MetricSpec("arm_length_in", "Arm length", "in", "target", "physical", 0.75),
    MetricSpec("hand_size_in", "Hand size", "in", "target", "physical", 0.35),
    MetricSpec("wingspan_in", "Wingspan", "in", "target", "physical", 0.50),
    MetricSpec("bmi", "Body-mass index", "", "target", "physical", 0.45),
    MetricSpec("speed_score", "Weight-adjusted speed", "", "higher", "physical", 1.00),
    MetricSpec("explosion_index", "Explosion index", "", "higher", "physical", 0.70),
)


PHYSICAL_KEYS_BY_POSITION: dict[str, tuple[str, ...]] = {
    "QB": ("height_in", "weight_lb", "forty_s", "vertical_in", "broad_jump_in", "three_cone_s", "shuttle_s", "hand_size_in"),
    "RB": ("height_in", "weight_lb", "forty_s", "ten_split_s", "bench_reps", "vertical_in", "broad_jump_in", "three_cone_s", "shuttle_s", "bmi", "speed_score", "explosion_index"),
    "WR": ("height_in", "weight_lb", "forty_s", "ten_split_s", "vertical_in", "broad_jump_in", "three_cone_s", "shuttle_s", "arm_length_in", "hand_size_in", "speed_score", "explosion_index"),
    "TE": ("height_in", "weight_lb", "forty_s", "ten_split_s", "bench_reps", "vertical_in", "broad_jump_in", "three_cone_s", "shuttle_s", "arm_length_in", "speed_score", "explosion_index"),
    "OT": ("height_in", "weight_lb", "forty_s", "ten_split_s", "bench_reps", "vertical_in", "broad_jump_in", "three_cone_s", "shuttle_s", "arm_length_in", "wingspan_in", "bmi", "explosion_index"),
    "IOL": ("height_in", "weight_lb", "forty_s", "ten_split_s", "bench_reps", "vertical_in", "broad_jump_in", "three_cone_s", "shuttle_s", "arm_length_in", "bmi", "explosion_index"),
    "EDGE": ("height_in", "weight_lb", "forty_s", "ten_split_s", "bench_reps", "vertical_in", "broad_jump_in", "three_cone_s", "shuttle_s", "arm_length_in", "speed_score", "explosion_index"),
    "IDL": ("height_in", "weight_lb", "forty_s", "ten_split_s", "bench_reps", "vertical_in", "broad_jump_in", "three_cone_s", "shuttle_s", "arm_length_in", "bmi", "speed_score", "explosion_index"),
    "LB": ("height_in", "weight_lb", "forty_s", "ten_split_s", "bench_reps", "vertical_in", "broad_jump_in", "three_cone_s", "shuttle_s", "arm_length_in", "speed_score", "explosion_index"),
    "CB": ("height_in", "weight_lb", "forty_s", "ten_split_s", "vertical_in", "broad_jump_in", "three_cone_s", "shuttle_s", "arm_length_in", "speed_score", "explosion_index"),
    "S": ("height_in", "weight_lb", "forty_s", "ten_split_s", "bench_reps", "vertical_in", "broad_jump_in", "three_cone_s", "shuttle_s", "arm_length_in", "speed_score", "explosion_index"),
    "K": ("height_in", "weight_lb", "forty_s"),
    "P": ("height_in", "weight_lb", "forty_s"),
    "LS": ("height_in", "weight_lb", "forty_s", "bench_reps"),
}


TRAITS: dict[str, tuple[str, ...]] = {
    "QB": ("accuracy_short", "accuracy_intermediate", "accuracy_deep", "arm_strength", "processing", "decision_making", "pocket_presence", "playmaking"),
    "RB": ("vision", "burst", "contact_balance", "elusiveness", "receiving", "pass_protection", "ball_security"),
    "WR": ("release", "route_running", "separation", "ball_skills", "contested_catch", "yac", "blocking"),
    "TE": ("release", "route_running", "separation", "ball_skills", "yac", "inline_blocking", "pass_protection"),
    "OT": ("pass_protection", "run_blocking", "footwork", "anchor", "hand_usage", "recovery", "awareness"),
    "IOL": ("pass_protection", "run_blocking", "anchor", "leverage", "hand_usage", "pull_mobility", "awareness"),
    "EDGE": ("get_off", "bend", "pass_rush_plan", "power", "run_defense", "setting_edge", "motor"),
    "IDL": ("get_off", "hand_usage", "power", "pass_rush_plan", "run_defense", "gap_discipline", "motor"),
    "LB": ("instincts", "range", "tackling", "block_destruction", "coverage", "blitzing", "pursuit"),
    "CB": ("man_coverage", "zone_coverage", "press", "ball_skills", "recovery_speed", "tackling", "instincts"),
    "S": ("man_coverage", "zone_coverage", "range", "ball_skills", "tackling", "instincts", "versatility"),
    "K": ("accuracy", "leg_strength", "kickoff", "pressure_performance"),
    "P": ("distance", "hang_time", "placement", "consistency"),
    "LS": ("snap_accuracy", "snap_velocity", "blocking", "coverage"),
}


PRODUCTION: dict[str, tuple[tuple[str, str], ...]] = {
    "QB": (("pass_attempts", "higher"), ("pass_yards", "higher"), ("pass_tds", "higher"), ("rush_yards", "higher"), ("passing_success_plays", "higher"), ("passing_success_rate", "higher"), ("rushing_success_plays", "higher"), ("rushing_success_rate", "higher"), ("ppa_per_play", "higher"), ("usage_rate", "higher"), ("completion_pct", "higher"), ("completion_pct_over_expected", "higher"), ("yards_per_attempt", "higher"), ("td_rate", "higher"), ("interception_rate", "lower"), ("pressure_to_sack_rate", "lower"), ("explosive_play_rate", "higher"), ("games_started", "higher")),
    "RB": (("carries", "higher"), ("rush_yards", "higher"), ("scrimmage_yards", "higher"), ("touchdowns", "higher"), ("rushing_success_plays", "higher"), ("rushing_success_rate", "higher"), ("ppa_per_play", "higher"), ("usage_rate", "higher"), ("yards_per_carry", "higher"), ("yards_after_contact_per_attempt", "higher"), ("missed_tackles_forced_per_touch", "higher"), ("explosive_run_rate", "higher"), ("yards_per_touch", "higher"), ("target_share", "higher"), ("yards_per_route", "higher"), ("fumble_rate", "lower")),
    "WR": (("receptions", "higher"), ("receiving_yards", "higher"), ("receiving_tds", "higher"), ("ppa_per_play", "higher"), ("usage_rate", "higher"), ("yards_per_reception", "higher"), ("yards_per_route", "higher"), ("target_share", "higher"), ("receiving_yards_share", "higher"), ("ppa_per_target", "higher"), ("drop_rate", "lower"), ("contested_catch_rate", "higher"), ("games_started", "higher")),
    "TE": (("receptions", "higher"), ("receiving_yards", "higher"), ("receiving_tds", "higher"), ("ppa_per_play", "higher"), ("usage_rate", "higher"), ("yards_per_reception", "higher"), ("yards_per_route", "higher"), ("target_share", "higher"), ("receiving_yards_share", "higher"), ("ppa_per_target", "higher"), ("drop_rate", "lower"), ("contested_catch_rate", "higher"), ("games_started", "higher")),
    "OT": (("pressure_rate_allowed", "lower"), ("true_pass_set_pressure_rate_allowed", "lower"), ("sack_rate_allowed", "lower"), ("penalty_rate", "lower"), ("run_block_success_rate", "higher"), ("snaps", "higher")),
    "IOL": (("pressure_rate_allowed", "lower"), ("true_pass_set_pressure_rate_allowed", "lower"), ("sack_rate_allowed", "lower"), ("penalty_rate", "lower"), ("run_block_success_rate", "higher"), ("snaps", "higher")),
    "EDGE": (("sacks", "higher"), ("tackles_for_loss", "higher"), ("pressure_rate", "higher"), ("pass_rush_win_rate", "higher"), ("sacks_per_game", "higher"), ("run_stop_rate", "higher"), ("missed_tackle_rate", "lower"), ("tfl_per_game", "higher"), ("snaps", "higher")),
    "IDL": (("sacks", "higher"), ("tackles_for_loss", "higher"), ("pressure_rate", "higher"), ("pass_rush_win_rate", "higher"), ("sacks_per_game", "higher"), ("run_stop_rate", "higher"), ("missed_tackle_rate", "lower"), ("tfl_per_game", "higher"), ("snaps", "higher")),
    "LB": (("tackles", "higher"), ("sacks", "higher"), ("tackles_for_loss", "higher"), ("interceptions", "higher"), ("stop_rate", "higher"), ("coverage_ppa_per_target", "lower"), ("missed_tackle_rate", "lower"), ("pressure_rate", "higher"), ("sacks_per_game", "higher"), ("tfl_per_game", "higher"), ("takeaways_per_game", "higher"), ("snaps", "higher")),
    "CB": (("interceptions", "higher"), ("passes_defended", "higher"), ("coverage_ppa_per_target", "lower"), ("yards_per_target", "lower"), ("forced_incompletion_rate", "higher"), ("missed_tackle_rate", "lower"), ("takeaways_per_game", "higher"), ("passes_defended_per_game", "higher"), ("snaps", "higher")),
    "S": (("tackles", "higher"), ("interceptions", "higher"), ("passes_defended", "higher"), ("coverage_ppa_per_target", "lower"), ("yards_per_target", "lower"), ("forced_incompletion_rate", "higher"), ("missed_tackle_rate", "lower"), ("takeaways_per_game", "higher"), ("passes_defended_per_game", "higher"), ("snaps", "higher")),
    "K": (("field_goal_points_over_expected", "higher"), ("field_goal_pct", "higher"), ("touchback_rate", "higher"), ("attempts", "higher")),
    "P": (("net_yards_per_punt", "higher"), ("hang_time", "higher"), ("inside_20_rate", "higher"), ("return_rate", "lower")),
    "LS": (("games_played", "higher"), ("bad_snap_rate", "lower"), ("coverage_tackles_per_game", "higher")),
}


CATEGORY_WEIGHTS: dict[str, dict[str, float]] = {
    "QB": {"physical": 0.20, "production": 0.35, "skills": 0.40, "age": 0.05},
    "RB": {"physical": 0.30, "production": 0.30, "skills": 0.35, "age": 0.05},
    "WR": {"physical": 0.30, "production": 0.25, "skills": 0.40, "age": 0.05},
    "TE": {"physical": 0.30, "production": 0.25, "skills": 0.40, "age": 0.05},
    "OT": {"physical": 0.35, "production": 0.20, "skills": 0.40, "age": 0.05},
    "IOL": {"physical": 0.35, "production": 0.20, "skills": 0.40, "age": 0.05},
    "EDGE": {"physical": 0.30, "production": 0.25, "skills": 0.40, "age": 0.05},
    "IDL": {"physical": 0.35, "production": 0.20, "skills": 0.40, "age": 0.05},
    "LB": {"physical": 0.30, "production": 0.25, "skills": 0.40, "age": 0.05},
    "CB": {"physical": 0.30, "production": 0.25, "skills": 0.40, "age": 0.05},
    "S": {"physical": 0.30, "production": 0.25, "skills": 0.40, "age": 0.05},
    "K": {"physical": 0.10, "production": 0.45, "skills": 0.40, "age": 0.05},
    "P": {"physical": 0.10, "production": 0.45, "skills": 0.40, "age": 0.05},
    "LS": {"physical": 0.10, "production": 0.40, "skills": 0.45, "age": 0.05},
}


PLAUSIBLE_RANGES: dict[str, tuple[float, float]] = {
    "height_in": (60.0, 84.0),
    "weight_lb": (140.0, 430.0),
    "arm_length_in": (25.0, 40.0),
    "hand_size_in": (7.0, 13.0),
    "wingspan_in": (65.0, 95.0),
    "forty_s": (4.0, 7.0),
    "ten_split_s": (1.35, 2.25),
    "bench_reps": (0.0, 60.0),
    "vertical_in": (15.0, 50.0),
    "broad_jump_in": (65.0, 150.0),
    "three_cone_s": (6.0, 9.5),
    "shuttle_s": (3.5, 6.0),
    "age_at_draft": (18.0, 30.0),
    "recruit_rating": (0.0, 1.0),
    "recruit_stars": (0.0, 5.0),
    "recruit_national_rank": (1.0, 10000.0),
}


def physical_specs(position: str) -> tuple[MetricSpec, ...]:
    keys = set(PHYSICAL_KEYS_BY_POSITION.get(position, ()))
    return tuple(spec for spec in COMMON_PHYSICAL if spec.key in keys)


def trait_specs(position: str) -> tuple[MetricSpec, ...]:
    return tuple(
        MetricSpec(f"trait_{name}", name.replace("_", " ").title(), "grade", "higher", "skills", 1.0)
        for name in TRAITS.get(position, ())
    )


def production_specs(position: str) -> tuple[MetricSpec, ...]:
    return tuple(
        MetricSpec(f"prod_{name}", name.replace("_", " ").title(), "", direction, "production", 1.0)
        for name, direction in PRODUCTION.get(position, ())
    )


def all_specs(position: str) -> tuple[MetricSpec, ...]:
    return physical_specs(position) + production_specs(position) + trait_specs(position) + (
        MetricSpec("age_at_draft", "Age on projected draft day", "years", "lower", "age", 1.0),
    )


def metric_keys(position: str, categories: Iterable[str] | None = None) -> tuple[str, ...]:
    wanted = set(categories or ("physical", "production", "skills", "age"))
    return tuple(spec.key for spec in all_specs(position) if spec.category in wanted)


BASE_PLAYER_FIELDS = (
    "player_id",
    "cfbd_player_id",
    "name",
    "position",
    "school",
    "conference",
    "season",
    "as_of_week",
    "projected_draft_year",
    "draft_entry_probability",
    "draft_declared",
    "draft_eligible",
    "date_of_birth",
    "age_at_draft",
    "class_year",
    "recruit_rating",
    "recruit_stars",
    "recruit_national_rank",
    "recruit_year",
    "recruit_type",
    "recruit_match_method",
    "measurement_source",
    "measurement_date",
    "measurements_verified",
)


TEAM_PROFILE_FIELDS = (
    "team",
    "season",
    "position",
    "profile_as_of_date",
    "scheme",
    "scheme_source",
    "scheme_as_of_date",
    "need_score",
    "need_confidence",
    "need_evidence_coverage",
    "need_source",
    "need_as_of_date",
    "starter_quality_need_score",
    "starter_quality_confidence",
    "starter_quality_evidence_coverage",
    "starter_quality_source",
    "starter_quality_as_of_date",
    "contract_need_score",
    "contract_confidence",
    "contract_evidence_coverage",
    "contract_source",
    "contract_as_of_date",
    "timeline_score",
    "timeline_confidence",
    "timeline_evidence_coverage",
    "timeline_source",
    "timeline_as_of_date",
    "coaching_stability_score",
    "coaching_confidence",
    "coaching_evidence_coverage",
    "coaching_source",
    "coaching_as_of_date",
    "draft_access_min",
    "draft_access_max",
    "draft_access_confidence",
    "draft_access_source",
    "draft_access_as_of_date",
    "scheme_confidence",
    "notes",
)

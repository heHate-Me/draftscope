from __future__ import annotations

from collections import Counter, defaultdict
import csv
from datetime import date
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .college_features import (
    derive_college_production,
    derive_player_success,
    parse_college_stat_number,
)
from .data_sources import CFBDClient, NFLVERSE_DRAFT_URL, _download
from .records import DataError, normalize_name, parse_height, parse_number, slug, timestamp_utc
from .recruiting import (
    RECRUITING_FEATURE_FIELDS,
    RecruitingIndex,
    build_recruiting_index,
    match_recruiting_profile,
)
from .schema import PLAUSIBLE_RANGES, POSITION_GROUPS, normalize_position


DEFAULT_COLLEGE_SEASONS = (2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025)
HISTORICAL_PIPELINE_VERSION = "1.2"
MODEL_STAGE = "college_precombine"
CHECKPOINT = "preseason_prior_year"
POPULATION = "FBS roster at requested checkpoint"
PROBABILITY_KIND = "unconditional_next_draft"
PROBABILITY_CONDITION = (
    "P(drafted in immediately following NFL draft | FBS roster player at checkpoint)"
)

_NETWORK_CHUNK_BYTES = 64 * 1024
_MAX_NFLVERSE_DRAFT_BYTES = 256 * 1024 * 1024
_MAX_NFLVERSE_DRAFT_ROWS = 1_000_000

# These picks are absent from the corresponding CFBD FBS roster snapshot, so
# they cannot be outcome rows in a model whose denominator is that snapshot.
# The allow-list is deliberately exact: a new or changed omission remains a
# hard quality-gate failure until it is reviewed instead of silently becoming a
# false negative. Pick number is immutable and the normalized identity fields
# protect against accidentally exempting an unrelated row.
_REVIEWED_ROSTER_SOURCE_OMISSIONS = {
    (2017, 196, "alquadinmuhammad", "miami"),
    (2018, 20, "frankragnow", "arkansas"),
    (2018, 168, "jamarcojones", "ohiostate"),
    (2020, 152, "kennyrobinson", "westvirginia"),
    (2021, 12, "micahparsons", "pennstate"),
    (2021, 30, "gregoryrousseau", "miami"),
    (2021, 89, "nicocollins", "michigan"),
}

# Separately reviewed omissions from SportsDataverse's ESPN roster snapshots.
# Never reuse the CFBD list across providers: each tuple is an exact immutable
# draft year/pick/name/school identity verified absent from that source file.
_REVIEWED_SPORTSDATAVERSE_ROSTER_SOURCE_OMISSIONS = {
    (2017, 196, "alquadinmuhammad", "miamifl"),
    (2020, 152, "kennyrobinson", "westvirginia"),
    (2021, 13, "rashawnslater", "northwestern"),
    (2021, 30, "gregoryrousseau", "miamifl"),
    (2021, 32, "joetryonshoyinka", "washington"),
    (2021, 41, "levionwuzurike", "washington"),
    (2021, 89, "nicocollins", "michigan"),
    (2021, 106, "jaytufele", "usc"),
    (2021, 150, "kennethgainwell", "memphis"),
    (2021, 173, "tedarrellslaton", "florida"),
    (2023, 192, "brycebaringer", "michiganst"),
    (2025, 52, "oluwafemioladejo", "ucla"),
}

# The list is deliberately fixed. Raw provider statistics remain in the private
# cache; the training table exposes normalized, auditable derivatives.
PRODUCTION_FIELDS = (
    "prod_pass_attempts",
    "prod_pass_completions",
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
    "prod_tackles_for_loss",
    "prod_tackles",
    "prod_solo_tackles",
    "prod_interceptions",
    "prod_passes_defended",
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
)

OBSERVED_RATE_FIELDS = (
    "observed_completion_pct",
    "observed_yards_per_attempt",
    "observed_td_rate",
    "observed_interception_rate",
    "observed_yards_per_carry",
    "observed_yards_per_reception",
    "observed_yards_per_touch",
)

HISTORICAL_COLLEGE_SCHEMA = (
    "row_id",
    "player_id",
    "cfbd_player_id",
    "name",
    "position",
    "school",
    "conference",
    "season",
    "draft_year",
    "checkpoint",
    "as_of_week",
    "feature_cutoff_season",
    "model_stage",
    "class_year",
    "class_year_raw",
    "recruit_rating",
    "recruit_stars",
    "recruit_national_rank",
    "recruit_year",
    "recruit_type",
    "recruit_match_method",
    "recruit_name_agrees",
    "date_of_birth",
    "age_at_draft",
    "age_source",
    "height_in",
    "weight_lb",
    "measurement_source",
    "has_recorded_stats",
    "roster_source_year",
    "stats_source_year",
    "stats_match_method",
    *PRODUCTION_FIELDS,
    "population",
    "probability_kind",
    "probability_condition",
    "drafted",
    "draft_round",
    "draft_ovr",
    "elite",
    "outcome_label_known",
    "outcome_match_method",
    "outcome_source_draft_year",
    "outcome_nflverse_pfr_player_id",
    "outcome_nflverse_cfb_player_id",
)

# This is the only feature allow-list emitted for downstream training. Outcome
# columns, source IDs, school, conference, and post-season draft metadata are
# never eligible inputs merely because they exist in the joined table.
MODEL_FEATURE_FIELDS = (
    "position",
    "class_year",
    "age_at_draft",
    *RECRUITING_FEATURE_FIELDS,
    "height_in",
    "weight_lb",
    "has_recorded_stats",
    *PRODUCTION_FIELDS,
)

OUTCOME_FIELDS = (
    "drafted",
    "draft_round",
    "draft_ovr",
    "elite",
    "outcome_label_known",
    "outcome_match_method",
    "outcome_source_draft_year",
    "outcome_nflverse_pfr_player_id",
    "outcome_nflverse_cfb_player_id",
)

FORBIDDEN_SOURCE_FIELDS = frozenset(
    {
        "preDraftGrade",
        "preDraftRanking",
        "preDraftPositionRanking",
        "pre_draft_grade",
        "pre_draft_ranking",
        "combine_invited",
        "combine_invitation",
        "forty_s",
        "ten_split_s",
        "bench_reps",
        "vertical_in",
        "broad_jump_in",
        "three_cone_s",
        "shuttle_s",
    }
)


def build_historical_college_training_data(
    client: Any | None = None,
    *,
    college_seasons: Iterable[int] = DEFAULT_COLLEGE_SEASONS,
    cache_dir: str | os.PathLike[str] | None = None,
    nflverse_draft_rows: Iterable[Mapping[str, Any]] | None = None,
    nflverse_draft_path: str | os.PathLike[str] | None = None,
    as_of_week: int = 0,
    refresh: bool = False,
    strict: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build a leakage-bounded, full-FBS-roster next-draft cohort.

    Each college season is paired only with its immediately following NFL draft.
    Every unique player in the selected provider's FBS roster is retained, including
    backups, players without recorded box-score statistics, and players who later
    return to college. Consequently, non-selections are valid negatives for the
    unconditional next-draft probability contract rather than inferred entrants.

    nflverse supplies the outcome truth. The provider adapter supplies an audited
    identity bridge from each nflverse draft pick to a roster player. Draft
    measurements, rankings, grades, and nflverse draft-only age values are never
    copied to the model feature table.

    Checkpoints are candidate-aligned: Week 0 uses the current roster and the
    prior completed season's production; Week N uses current-season statistics
    through the requested completed week. A 2026 preseason prospect can therefore
    never be trained against historical rows containing that roster season's
    future complete production.
    """

    seasons = _validate_seasons(college_seasons)
    if as_of_week < 0:
        raise DataError("as_of_week must be zero or greater")
    checkpoint = _checkpoint_label(as_of_week)
    row_population = _row_population_label(as_of_week)
    population_description = _population_description(as_of_week)
    resolved_client = client or CFBDClient()
    source_name = str(
        getattr(resolved_client, "source_name", "CollegeFootballData")
    ).strip() or "CollegeFootballData"
    source_namespace = str(
        getattr(resolved_client, "source_namespace", "cfbd")
    ).strip() or "cfbd"
    measurement_source = str(
        getattr(
            resolved_client,
            "measurement_source",
            f"{source_namespace}_school_roster",
        )
    )
    age_source = str(
        getattr(
            resolved_client,
            "age_source",
            f"{source_namespace}_roster_date_of_birth",
        )
    )
    cache_root = Path(cache_dir) if cache_dir is not None else None
    if cache_root is not None:
        cache_root.mkdir(parents=True, exist_ok=True)

    nfl_rows, nfl_source = _load_nflverse_rows(
        nflverse_draft_rows,
        draft_path=Path(nflverse_draft_path) if nflverse_draft_path is not None else None,
        cache_root=cache_root,
        refresh=refresh,
    )
    nfl_by_year = _index_nflverse_drafts(nfl_rows, {season + 1 for season in seasons})

    all_rows: list[dict[str, Any]] = []
    per_season: list[dict[str, Any]] = []
    source_artifacts: list[dict[str, Any]] = [nfl_source]
    source_failures: list[str] = []
    optional_source_warnings: list[str] = []

    recruiting_rows: list[dict[str, Any]] = []
    recruiting_sources: list[dict[str, Any]] = []
    if getattr(resolved_client, "supports_recruiting", False):
        for recruit_year in range(min(seasons) - 6, max(seasons) + 1):
            try:
                rows, source = _load_cfbd_rows(
                    resolved_client,
                    "/recruiting/players",
                    {"year": recruit_year},
                    cache_root=cache_root,
                    filename=f"{source_namespace}_recruiting_players_{recruit_year}.json",
                    refresh=refresh,
                )
            except DataError as exc:
                optional_source_warnings.append(
                    f"{recruit_year} recruiting partition unavailable ({exc})"
                )
                continue
            recruiting_rows.extend(rows)
            recruiting_sources.append(source)
    recruiting_index = build_recruiting_index(recruiting_rows)
    source_artifacts.extend(recruiting_sources)

    for season in seasons:
        draft_year = season + 1
        feature_season = season - 1 if as_of_week == 0 else season
        stats_params: dict[str, Any] = {"year": feature_season, "seasonType": "both"}
        if as_of_week > 0:
            stats_params["endWeek"] = as_of_week
        roster_rows, roster_source = _load_cfbd_rows(
            resolved_client,
            "/roster",
            {"year": season, "classification": "fbs"},
            cache_root=cache_root,
            filename=f"{source_namespace}_roster_fbs_{season}.json",
            refresh=refresh,
        )
        stats_rows, stats_source = _load_cfbd_rows(
            resolved_client,
            "/stats/player/season",
            stats_params,
            cache_root=cache_root,
            filename=(
                f"{source_namespace}_player_stats_{feature_season}_complete_for_{season}_week_00.json"
                if as_of_week == 0
                else f"{source_namespace}_player_stats_{season}_through_week_{as_of_week:02d}.json"
            ),
            refresh=refresh,
        )
        success_rows: list[dict[str, Any]] = []
        success_source: dict[str, Any] | None = None
        if getattr(resolved_client, "supports_player_success", False):
            try:
                success_params: dict[str, Any] = {
                    "year": feature_season,
                    "seasonType": "both",
                    "excludeGarbageTime": False,
                    "threshold": 0,
                }
                if as_of_week > 0:
                    success_params["endWeek"] = as_of_week
                success_rows, success_source = _load_cfbd_rows(
                    resolved_client,
                    "/stats/player/success",
                    success_params,
                    cache_root=cache_root,
                    filename=(
                        f"{source_namespace}_player_success_{feature_season}_complete_for_{season}_week_00.json"
                        if as_of_week == 0
                        else f"{source_namespace}_player_success_{season}_through_week_{as_of_week:02d}.json"
                    ),
                    refresh=refresh,
                )
            except DataError as exc:
                optional_source_warnings.append(
                    f"{season} checkpoint: optional player-success source unavailable ({exc})"
                )
        team_rows, teams_source = _load_cfbd_rows(
            resolved_client,
            "/teams/fbs",
            {"year": season},
            cache_root=cache_root,
            filename=f"{source_namespace}_teams_fbs_{season}.json",
            refresh=refresh,
        )
        pick_rows, picks_source = _load_cfbd_rows(
            resolved_client,
            "/draft/picks",
            {"year": draft_year},
            cache_root=cache_root,
            filename=f"{source_namespace}_draft_picks_{draft_year}.json",
            refresh=refresh,
        )
        source_artifacts.extend((roster_source, stats_source, teams_source, picks_source))
        if success_source is not None:
            source_artifacts.append(success_source)

        stat_profiles, stat_audit = _index_stats(stats_rows, expected_season=feature_season)
        success_profiles, success_audit = _index_player_success(
            success_rows,
            expected_season=feature_season,
        )
        canonical_roster, roster_audit = _deduplicate_roster(
            roster_rows,
            season=season,
            stat_profiles=stat_profiles,
            stats_are_prior_year=as_of_week == 0,
        )
        team_aliases, team_audit = _index_fbs_teams(team_rows)
        draft_links, draft_audit = _reconcile_draft_sources(
            pick_rows,
            nfl_by_year.get(draft_year, {}),
            draft_year=draft_year,
            source_namespace=source_namespace,
        )
        outcomes, outcome_audit = _match_draft_outcomes(
            canonical_roster,
            draft_links,
            team_aliases=team_aliases,
            stat_profiles=stat_profiles,
            roster_source_label=source_name,
            source_namespace=source_namespace,
            allow_reviewed_roster_omissions=source_namespace
            in {"cfbd", "sportsdataverse"},
            reviewed_roster_source_omissions=(
                _REVIEWED_SPORTSDATAVERSE_ROSTER_SOURCE_OMISSIONS
                if source_namespace == "sportsdataverse"
                else _REVIEWED_ROSTER_SOURCE_OMISSIONS
            ),
        )
        canonical_roster, alias_collapse_audit = _collapse_resolved_roster_aliases(
            canonical_roster,
            outcome_audit.get("resolved_cross_id_roster_aliases", []),
            outcomes=outcomes,
        )
        outcome_audit.update(alias_collapse_audit)
        canonical_roster, negative_alias_audit = _collapse_negative_roster_aliases(
            canonical_roster,
            outcomes=outcomes,
            stat_profiles=stat_profiles,
            team_aliases=team_aliases,
        )
        outcome_audit.update(negative_alias_audit)

        season_rows, availability_audit = _build_season_rows(
            canonical_roster,
            stat_profiles=stat_profiles,
            success_profiles=success_profiles,
            recruiting_index=recruiting_index,
            outcomes=outcomes,
            team_aliases=team_aliases,
            season=season,
            as_of_week=as_of_week,
            feature_season=feature_season,
            row_population=row_population,
            measurement_source=measurement_source,
            age_source=age_source,
            player_id_namespace=source_namespace,
        )
        all_rows.extend(season_rows)

        season_audit = {
            "college_season": season,
            "draft_year": draft_year,
            **roster_audit,
            **stat_audit,
            **success_audit,
            **team_audit,
            **draft_audit,
            **outcome_audit,
            **availability_audit,
        }
        per_season.append(season_audit)

        if stat_audit["wrong_season_stat_rows_dropped"]:
            source_failures.append(
                f"{season} checkpoint: {stat_audit['wrong_season_stat_rows_dropped']} statistic rows belonged to another season"
            )
        if stat_audit["conflicting_duplicate_stat_values"]:
            source_failures.append(
                f"{season}: {stat_audit['conflicting_duplicate_stat_values']} conflicting duplicate statistic values"
            )
        if (
            draft_audit["nflverse_pick_count"]
            != draft_audit["college_provider_pick_count"]
        ):
            source_failures.append(
                f"{draft_year}: nflverse and college-provider draft crosswalk counts differ"
            )
        if draft_audit["unreconciled_pick_numbers"]:
            source_failures.append(
                f"{draft_year}: draft pick numbers are not fully reconciled across nflverse and the college-provider crosswalk"
            )
        if outcome_audit["unmatched_expected_fbs_picks"]:
            source_failures.append(
                f"{draft_year}: expected FBS draft picks did not link to the roster"
            )
        if outcome_audit["identity_conflicts"]:
            source_failures.append(
                f"{draft_year}: draft crosswalk ID and exact name/team resolved to different roster players"
            )
        if outcome_audit["cross_id_duplicate_alias_outcome_conflicts"]:
            source_failures.append(
                f"{draft_year}: a resolved roster alias was also linked to a separate draft outcome"
            )

    contract_audit = audit_historical_college_training_data(
        all_rows,
        expected_seasons=seasons,
    )
    if contract_audit["status"] == "fail":
        source_failures.extend(contract_audit["failures"])

    drafted_rows = sum(row["drafted"] is True for row in all_rows)
    expected_fbs = sum(item["expected_fbs_draft_picks"] for item in per_season)
    matched_fbs = sum(item["matched_expected_fbs_draft_picks"] for item in per_season)
    reviewed_roster_omissions = sum(
        item["reviewed_roster_source_omission_count"] for item in per_season
    )
    metadata: dict[str, Any] = {
        "schema_version": "1.2",
        "historical_pipeline_version": HISTORICAL_PIPELINE_VERSION,
        "college_data_provider": source_namespace,
        "college_source_name": source_name,
        "schema_fields": list(HISTORICAL_COLLEGE_SCHEMA),
        "model_feature_fields": list(MODEL_FEATURE_FIELDS),
        "outcome_fields": list(OUTCOME_FIELDS),
        "model_stage": MODEL_STAGE,
        "checkpoint": checkpoint,
        "as_of_week": as_of_week,
        "population": population_description,
        "row_population": row_population,
        "probability_kind": PROBABILITY_KIND,
        "probability_condition": PROBABILITY_CONDITION,
        "college_seasons": list(seasons),
        "draft_years": [season + 1 for season in seasons],
        "rows": len(all_rows),
        "drafted_rows": drafted_rows,
        "base_rate": drafted_rows / len(all_rows) if all_rows else None,
        "expected_fbs_draft_picks": expected_fbs,
        "matched_expected_fbs_draft_picks": matched_fbs,
        "positive_match_coverage": matched_fbs / expected_fbs if expected_fbs else None,
        "reviewed_roster_source_omission_count": reviewed_roster_omissions,
        "position_counts": dict(sorted(Counter(row["position"] for row in all_rows).items())),
        "per_season": per_season,
        "drops": _sum_drop_reasons(per_season),
        "contract_audit": contract_audit,
        "source_failures": sorted(set(source_failures)),
        "optional_source_warnings": sorted(set(optional_source_warnings)),
        "quality_gate_status": "fail" if source_failures else "pass",
        "source_artifacts": source_artifacts,
        "recruiting_audit": dict(recruiting_index.audit),
        "sources": {
            "college_features": str(
                getattr(
                    resolved_client,
                    "source_description",
                    "CollegeFootballData /roster, /teams/fbs, /stats/player/season, "
                    "optional week-bounded /stats/player/success, and optional "
                    "stable-ID /recruiting/players pedigree",
                )
            ),
            "outcome_truth": "nflverse draft_picks.csv (PFR-derived)",
            "identity_crosswalk": str(
                getattr(
                    resolved_client,
                    "identity_crosswalk_description",
                    "CollegeFootballData /draft/picks collegeAthleteId",
                )
            ),
            "nflverse_draft_url": NFLVERSE_DRAFT_URL,
        },
        "leakage_controls": {
            "feature_source_cutoff": (
                "current roster plus prior completed season statistics"
                if as_of_week == 0
                else f"current roster plus current-season statistics bounded through Week {as_of_week}"
            ),
            "outcomes_from_immediate_next_draft_only": True,
            "draft_measurements_or_pre_draft_grades_copied": False,
            "nflverse_positive_only_age_used_as_feature": False,
            "outcome_fields_in_model_feature_allow_list": bool(
                set(OUTCOME_FIELDS) & set(MODEL_FEATURE_FIELDS)
            ),
            "school_or_conference_in_model_feature_allow_list": bool(
                {"school", "conference"} & set(MODEL_FEATURE_FIELDS)
            ),
        },
        "limitations": [
            f"{source_name} class/experience values outside 1-6 are retained only in class_year_raw.",
            f"Date of birth is used only when it is present in the {source_name} roster record; otherwise age remains unavailable.",
            "A roster listing is not a declaration or draft-eligibility determination; returning and ineligible players remain valid negatives for this unconditional checkpoint population.",
            f"Drafted players absent from a reviewed {source_name} roster snapshot cannot be outcome rows in that source-defined risk set; exact reviewed omissions are audited separately and new omissions fail the strict build.",
            f"{source_name} box scores do not provide complete offensive-line blocking, coverage, route, or pressure charting features.",
        ],
        "built_at": timestamp_utc(),
    }
    if strict and source_failures:
        preview = "; ".join(sorted(set(source_failures))[:8])
        raise DataError(f"Historical college training-data quality gate failed: {preview}")
    return all_rows, metadata


def audit_historical_college_training_data(
    rows: Iterable[Mapping[str, Any]],
    *,
    expected_seasons: Iterable[int] | None = None,
) -> dict[str, Any]:
    """Validate row grain, stage, temporal alignment, outcomes, and leakage guardrails."""

    materialized = [dict(row) for row in rows]
    seasons = tuple(sorted(set(int(year) for year in (expected_seasons or ()))))
    failures: list[str] = []
    keys: Counter[tuple[str, int]] = Counter()
    invalid_alignment = 0
    invalid_stage = 0
    invalid_checkpoint = 0
    invalid_population = 0
    invalid_probability_contract = 0
    invalid_outcomes = 0
    invalid_feature_sources = 0
    missing_required = 0
    forbidden_non_null: Counter[str] = Counter()

    for row in materialized:
        season = _as_int(row.get("season"))
        draft_year = _as_int(row.get("draft_year"))
        player_id = str(row.get("player_id") or "")
        if not player_id or season is None or draft_year is None or not row.get("name"):
            missing_required += 1
        if season is not None:
            keys[(player_id, season)] += 1
        if season is None or draft_year != season + 1:
            invalid_alignment += 1
        if row.get("model_stage") != MODEL_STAGE:
            invalid_stage += 1
        week = _as_int(row.get("as_of_week"))
        if week is None or week < 0 or row.get("checkpoint") != _checkpoint_label(week):
            invalid_checkpoint += 1
        if week is None or week < 0 or row.get("population") != _row_population_label(week):
            invalid_population += 1
        if (
            row.get("probability_kind") != PROBABILITY_KIND
            or row.get("probability_condition") != PROBABILITY_CONDITION
        ):
            invalid_probability_contract += 1
        drafted = row.get("drafted")
        pick = _as_int(row.get("draft_ovr"))
        round_number = _as_int(row.get("draft_round"))
        if drafted is True and (pick is None or round_number is None):
            invalid_outcomes += 1
        elif drafted is False and (pick is not None or round_number is not None):
            invalid_outcomes += 1
        elif drafted not in (True, False) or row.get("outcome_label_known") is not True:
            invalid_outcomes += 1
        expected_feature_season = season - 1 if season is not None and week == 0 else season
        if (
            _as_int(row.get("roster_source_year")) != season
            or _as_int(row.get("stats_source_year")) != expected_feature_season
            or _as_int(row.get("outcome_source_draft_year")) != draft_year
            or _as_int(row.get("feature_cutoff_season")) != expected_feature_season
        ):
            invalid_feature_sources += 1
        if row.get("age_at_draft") not in (None, ""):
            documented_age_source = str(row.get("age_source") or "")
            if not documented_age_source.endswith("_roster_date_of_birth"):
                invalid_feature_sources += 1
        for field in FORBIDDEN_SOURCE_FIELDS:
            if row.get(field) not in (None, ""):
                forbidden_non_null[field] += 1

    duplicate_keys = sum(count - 1 for count in keys.values() if count > 1)
    observed_seasons = sorted({int(row["season"]) for row in materialized if _as_int(row.get("season")) is not None})
    if seasons and observed_seasons != list(seasons):
        failures.append(
            f"season partitions differ: expected {list(seasons)}, observed {observed_seasons}"
        )
    for label, count in (
        ("duplicate player-season keys", duplicate_keys),
        ("rows missing required identity/year fields", missing_required),
        ("rows not aligned to the immediate following draft", invalid_alignment),
        ("rows with the wrong model stage", invalid_stage),
        ("rows with a malformed or inconsistent checkpoint", invalid_checkpoint),
        ("rows with a checkpoint-inconsistent population", invalid_population),
        ("rows with an inconsistent probability contract", invalid_probability_contract),
        ("rows with inconsistent outcome labels", invalid_outcomes),
        ("rows with inconsistent feature/outcome source years", invalid_feature_sources),
    ):
        if count:
            failures.append(f"{label}: {count}")
    if forbidden_non_null:
        failures.append(
            "post-checkpoint fields populated: "
            + ", ".join(f"{field}={count}" for field, count in sorted(forbidden_non_null.items()))
        )
    overlap = sorted(set(OUTCOME_FIELDS) & set(MODEL_FEATURE_FIELDS))
    if overlap:
        failures.append(f"outcome fields appear in model feature allow-list: {overlap}")

    return {
        "status": "fail" if failures else "pass",
        "rows": len(materialized),
        "player_season_key_duplicates": duplicate_keys,
        "missing_required_rows": missing_required,
        "invalid_immediate_draft_alignment_rows": invalid_alignment,
        "invalid_stage_rows": invalid_stage,
        "invalid_checkpoint_rows": invalid_checkpoint,
        "invalid_population_rows": invalid_population,
        "invalid_probability_contract_rows": invalid_probability_contract,
        "invalid_outcome_rows": invalid_outcomes,
        "invalid_feature_source_rows": invalid_feature_sources,
        "forbidden_non_null_fields": dict(sorted(forbidden_non_null.items())),
        "outcome_feature_overlap": overlap,
        "season_counts": dict(sorted(Counter(_as_int(row.get("season")) for row in materialized).items())),
        "position_counts": dict(sorted(Counter(str(row.get("position") or "") for row in materialized).items())),
        "failures": failures,
    }


def _validate_seasons(values: Iterable[int]) -> tuple[int, ...]:
    try:
        seasons = tuple(sorted(set(int(value) for value in values)))
    except (TypeError, ValueError) as exc:
        raise DataError("college_seasons must contain integer season years") from exc
    if not seasons:
        raise DataError("college_seasons cannot be empty")
    if seasons[0] < 2000:
        raise DataError("college_seasons must use four-digit FBS season years")
    expected = tuple(range(seasons[0], seasons[-1] + 1))
    if seasons != expected:
        raise DataError("college_seasons must be consecutive so every temporal fold is explicit")
    return seasons


def _checkpoint_label(as_of_week: int) -> str:
    return CHECKPOINT if as_of_week == 0 else f"through_week_{as_of_week:02d}"


def _row_population_label(as_of_week: int) -> str:
    if as_of_week == 0:
        return "FBS roster preseason; prior-season stats"
    return f"FBS roster through completed Week {as_of_week}"


def _population_description(as_of_week: int) -> str:
    if as_of_week == 0:
        return (
            "All unique source-listed FBS roster players at the preseason checkpoint, "
            "with prior completed-season production where available"
        )
    return (
        f"All unique source-listed FBS roster players at the completed Week {as_of_week} checkpoint, "
        f"with current-season statistics bounded through Week {as_of_week} where available"
    )


def _load_nflverse_rows(
    provided: Iterable[Mapping[str, Any]] | None,
    *,
    draft_path: Path | None,
    cache_root: Path | None,
    refresh: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if provided is not None:
        rows = [dict(row) for row in provided]
        return rows, {
            "source": "provided nflverse draft rows",
            "url": NFLVERSE_DRAFT_URL,
            "rows": len(rows),
            "cache_hit": None,
            "sha256": _json_sha256(rows),
        }
    existing_default = Path(".draftscope-cache/draft_picks.csv")
    if draft_path is not None:
        path = draft_path
        if not path.exists() or path.stat().st_size == 0:
            raise DataError(f"nflverse draft file does not exist or is empty: {path}")
        cache_hit: bool | None = True
    elif not refresh and existing_default.exists() and existing_default.stat().st_size > 0:
        path = existing_default
        cache_hit = True
    elif cache_root is not None:
        destination = cache_root / "nflverse_draft_picks.csv"
        cache_hit = destination.exists() and destination.stat().st_size > 0 and not refresh
        path = _download(NFLVERSE_DRAFT_URL, destination, refresh=refresh)
    else:
        request = Request(
            NFLVERSE_DRAFT_URL,
            headers={"User-Agent": "DraftScope/0.1 (+local research tool)"},
        )
        try:
            with urlopen(request, timeout=60) as response:
                payload = _read_response_limited(
                    response,
                    max_bytes=_MAX_NFLVERSE_DRAFT_BYTES,
                    source=NFLVERSE_DRAFT_URL,
                )
        except HTTPError as exc:
            try:
                raise DataError(
                    f"Could not download {NFLVERSE_DRAFT_URL}: {exc}"
                ) from exc
            finally:
                exc.close()
        except (DataError, URLError, TimeoutError, OSError) as exc:
            raise DataError(f"Could not download {NFLVERSE_DRAFT_URL}: {exc}") from exc
        if not payload:
            raise DataError(f"Downloaded nflverse draft file was empty: {NFLVERSE_DRAFT_URL}")
        text = payload.decode("utf-8-sig")
        rows = _read_nflverse_draft_csv(
            io.StringIO(text),
            source=NFLVERSE_DRAFT_URL,
        )
        return rows, {
            "source": "nflverse draft_picks.csv",
            "url": NFLVERSE_DRAFT_URL,
            "path": None,
            "rows": len(rows),
            "cache_hit": False,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    size = path.stat().st_size
    if size > _MAX_NFLVERSE_DRAFT_BYTES:
        raise DataError(
            f"nflverse draft file {path} is {size} bytes; "
            f"limit is {_MAX_NFLVERSE_DRAFT_BYTES}"
        )
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = _read_nflverse_draft_csv(handle, source=str(path))
    return rows, {
        "source": "nflverse draft_picks.csv",
        "url": NFLVERSE_DRAFT_URL,
        "path": str(path),
        "rows": len(rows),
        "cache_hit": cache_hit,
        "sha256": _file_sha256(path),
    }


def _read_nflverse_draft_csv(
    lines: Iterable[str],
    *,
    source: str,
) -> list[dict[str, str]]:
    reader = csv.DictReader(lines, strict=True)
    rows: list[dict[str, str]] = []
    for row_number, row in enumerate(reader, start=1):
        if row_number > _MAX_NFLVERSE_DRAFT_ROWS:
            raise DataError(
                f"nflverse draft CSV {source} exceeded "
                f"{_MAX_NFLVERSE_DRAFT_ROWS} data rows"
            )
        if None in row or any(value is None for value in row.values()):
            raise DataError(
                f"nflverse draft CSV {source} has a malformed row at "
                f"data row {row_number}"
            )
        rows.append(dict(row))
    return rows


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


def _load_cfbd_rows(
    client: Any,
    path: str,
    params: Mapping[str, Any],
    *,
    cache_root: Path | None,
    filename: str,
    refresh: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    destination = cache_root / filename if cache_root is not None else None
    cache_hit = bool(destination and destination.exists() and destination.stat().st_size > 0 and not refresh)
    if cache_hit and destination is not None:
        try:
            parsed = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DataError(f"Could not read cached college source {destination}: {exc}") from exc
    else:
        parsed = client.get(path, **dict(params))
        if destination is not None:
            _atomic_json(destination, parsed)
    if not isinstance(parsed, list) or any(not isinstance(row, dict) for row in parsed):
        raise DataError(f"Unexpected college-data response for {path}: expected a JSON object array")
    rows = [dict(row) for row in parsed]
    return rows, {
        "source": str(getattr(client, "source_name", "CollegeFootballData")),
        "endpoint": path,
        "params": dict(params),
        "path": str(destination) if destination is not None else None,
        "rows": len(rows),
        "cache_hit": cache_hit,
        "sha256": _file_sha256(destination) if destination is not None else _json_sha256(rows),
    }


def _index_nflverse_drafts(
    rows: Sequence[Mapping[str, Any]],
    draft_years: set[int],
) -> dict[int, dict[int, dict[str, Any]]]:
    indexed: dict[int, dict[int, dict[str, Any]]] = {year: {} for year in draft_years}
    for raw in rows:
        year = _as_int(raw.get("season"))
        if year not in draft_years:
            continue
        pick = _as_int(raw.get("pick"))
        if pick is None or pick < 1:
            raise DataError(f"nflverse draft row for {year} has an invalid overall pick")
        if pick in indexed[year]:
            raise DataError(f"nflverse draft class {year} contains duplicate overall pick {pick}")
        indexed[year][pick] = dict(raw)
    missing_years = [year for year, picks in indexed.items() if not picks]
    if missing_years:
        raise DataError(f"nflverse draft data is missing requested classes: {missing_years}")
    for year, picks in indexed.items():
        expected = set(range(1, max(picks) + 1))
        if set(picks) != expected:
            missing = sorted(expected - set(picks))
            raise DataError(f"nflverse draft class {year} is not contiguous; missing picks {missing[:10]}")
    return indexed


def _index_stats(
    rows: Sequence[Mapping[str, Any]],
    *,
    expected_season: int,
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, Any]]:
    grouped: dict[str, dict[str, list[Mapping[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    missing_player_id = 0
    wrong_season = 0
    for raw in rows:
        raw_season = _as_int(raw.get("season"))
        if raw_season is not None and raw_season != expected_season:
            wrong_season += 1
            continue
        player_id = str(raw.get("playerId") or raw.get("player_id") or "").strip()
        if not player_id:
            missing_player_id += 1
            continue
        team = str(raw.get("team") or "").strip()
        grouped[player_id][normalize_name(team)].append(raw)

    indexed: dict[str, dict[str, dict[str, Any]]] = {}
    identical_duplicates = 0
    conflicts = 0
    multi_team_players = 0
    for player_id, by_team in grouped.items():
        if len(by_team) > 1:
            multi_team_players += 1
        indexed[player_id] = {}
        for team_key, stat_rows in by_team.items():
            values: dict[tuple[str, str], Any] = {}
            for raw in stat_rows:
                key = (slug(raw.get("category") or "unknown"), slug(raw.get("statType") or raw.get("stat_type") or "unknown"))
                value = raw.get("stat") if raw.get("stat") not in (None, "") else raw.get("value")
                if key in values:
                    if str(values[key]).strip() == str(value).strip():
                        identical_duplicates += 1
                    else:
                        conflicts += 1
                        values[key] = _prefer_stat_value(values[key], value)
                else:
                    values[key] = value
            first = stat_rows[0]
            indexed[player_id][team_key] = {
                "player_id": player_id,
                "name": first.get("player") or first.get("name"),
                "team": first.get("team"),
                "conference": first.get("conference"),
                "position": first.get("position"),
                "values": values,
                "categories": sorted({key[0] for key in values}),
                "row_count": len(stat_rows),
            }
    return indexed, {
        "raw_stat_rows": len(rows),
        "stat_players": len(indexed),
        "multi_team_stat_players": multi_team_players,
        "stat_rows_missing_player_id_dropped": missing_player_id,
        "wrong_season_stat_rows_dropped": wrong_season,
        "identical_duplicate_stat_values_collapsed": identical_duplicates,
        "conflicting_duplicate_stat_values": conflicts,
    }


def _index_player_success(
    rows: Sequence[Mapping[str, Any]],
    *,
    expected_season: int,
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, Any]]:
    """Index checkpoint-bounded CFBD success data by stable player and team."""

    indexed: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    missing_player_id = 0
    wrong_season = 0
    duplicate_rows = 0
    for raw in rows:
        raw_season = _as_int(raw.get("season"))
        if raw_season is not None and raw_season != expected_season:
            wrong_season += 1
            continue
        player_id = str(raw.get("id") or raw.get("playerId") or "").strip()
        if not player_id:
            missing_player_id += 1
            continue
        team_key = normalize_name(raw.get("team"))
        candidate = dict(raw)
        previous = indexed[player_id].get(team_key)
        if previous is not None:
            duplicate_rows += 1

            def opportunity_count(item: Mapping[str, Any]) -> float:
                passing = item.get("passing") or {}
                rushing = item.get("rushing") or {}
                return float(parse_number(passing.get("plays")) or 0.0) + float(
                    parse_number(rushing.get("plays")) or 0.0
                )

            if opportunity_count(previous) >= opportunity_count(candidate):
                continue
        indexed[player_id][team_key] = candidate
    materialized = {
        player_id: dict(by_team) for player_id, by_team in indexed.items()
    }
    return materialized, {
        "raw_player_success_rows": len(rows),
        "player_success_players": len(materialized),
        "player_success_rows_missing_player_id_dropped": missing_player_id,
        "wrong_season_player_success_rows_dropped": wrong_season,
        "duplicate_player_success_rows_collapsed": duplicate_rows,
    }


def _deduplicate_roster(
    rows: Sequence[Mapping[str, Any]],
    *,
    season: int,
    stat_profiles: Mapping[str, Mapping[str, Mapping[str, Any]]],
    stats_are_prior_year: bool,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    synthetic_ids = 0
    for index, raw in enumerate(rows):
        row = dict(raw)
        player_id = str(row.get("id") or "").strip()
        if not player_id:
            synthetic_ids += 1
            name = _roster_name(row)
            team = str(row.get("team") or "")
            player_id = f"missing:{season}:{normalize_name(team)}:{normalize_name(name)}:{index}"
            row["_synthetic_player_id"] = True
        grouped[player_id].append(row)

    canonical: dict[str, dict[str, Any]] = {}
    duplicate_groups = 0
    duplicate_rows = 0
    team_conflict_groups = 0
    resolved_by_stat_team = 0
    resolved_away_from_prior_stat_team = 0
    for player_id, candidates in grouped.items():
        if len(candidates) > 1:
            duplicate_groups += 1
            duplicate_rows += len(candidates) - 1
            if len({normalize_name(row.get("team")) for row in candidates}) > 1:
                team_conflict_groups += 1
        stat_teams = stat_profiles.get(player_id, {})

        candidate_team_keys = {normalize_name(row.get("team")) for row in candidates}
        stat_candidate_teams = candidate_team_keys & set(stat_teams)
        prior_destination_key: str | None = None
        if stats_are_prior_year and len(candidate_team_keys) > 1 and len(stat_candidate_teams) == 1:
            non_prior = sorted(candidate_team_keys - stat_candidate_teams)
            if len(non_prior) == 1:
                prior_destination_key = non_prior[0]

        def score(row: Mapping[str, Any]) -> tuple[int, int, int, int, str]:
            team_key = normalize_name(row.get("team"))
            stat_profile = stat_teams.get(team_key)
            completeness = sum(
                row.get(field) not in (None, "")
                for field in ("firstName", "lastName", "team", "position", "height", "weight", "year")
            )
            return (
                1 if prior_destination_key and team_key == prior_destination_key else 0,
                1 if stat_profile is not None and not stats_are_prior_year else 0,
                int(stat_profile.get("row_count") or 0) if stat_profile and not stats_are_prior_year else 0,
                completeness,
                str(row.get("team") or ""),
            )

        chosen = max(candidates, key=score)
        chosen_team_key = normalize_name(chosen.get("team"))
        if len(candidates) > 1 and not stats_are_prior_year and chosen_team_key in stat_teams:
            resolved_by_stat_team += 1
        if len(candidates) > 1 and prior_destination_key and chosen_team_key == prior_destination_key:
            resolved_away_from_prior_stat_team += 1
        chosen = dict(chosen)
        chosen["_player_id"] = player_id
        chosen["_duplicate_rows_collapsed"] = len(candidates) - 1
        canonical[player_id] = chosen
    return canonical, {
        "raw_roster_rows": len(rows),
        "unique_roster_players": len(canonical),
        "roster_rows_missing_id_retained_with_synthetic_id": synthetic_ids,
        "duplicate_roster_player_id_groups": duplicate_groups,
        "duplicate_roster_rows_dropped": duplicate_rows,
        "duplicate_roster_team_conflict_groups": team_conflict_groups,
        "duplicate_roster_groups_resolved_by_stat_team": resolved_by_stat_team,
        "duplicate_roster_groups_resolved_away_from_prior_stat_team": resolved_away_from_prior_stat_team,
    }


def _index_fbs_teams(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    aliases: dict[str, dict[str, Any]] = {}
    collisions = 0
    for raw in rows:
        school = str(raw.get("school") or "").strip()
        if not school:
            continue
        canonical = dict(raw)
        names = [school, *(raw.get("alternateNames") or [])]
        for name in names:
            key = normalize_name(name)
            if not key:
                continue
            existing = aliases.get(key)
            if existing and normalize_name(existing.get("school")) != normalize_name(school):
                collisions += 1
                continue
            aliases[key] = canonical
    return aliases, {
        "fbs_team_rows": len(rows),
        "fbs_team_aliases": len(aliases),
        "fbs_team_alias_collisions": collisions,
    }


def _reconcile_draft_sources(
    cfbd_rows: Sequence[Mapping[str, Any]],
    nfl_by_pick: Mapping[int, Mapping[str, Any]],
    *,
    draft_year: int,
    source_namespace: str = "cfbd",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cfbd_by_pick: dict[int, dict[str, Any]] = {}
    invalid_cfbd = 0
    for raw in cfbd_rows:
        row_year = _as_int(raw.get("year"))
        pick = _as_int(raw.get("overall"))
        if pick is None or pick < 1 or (row_year is not None and row_year != draft_year):
            invalid_cfbd += 1
            continue
        if pick in cfbd_by_pick:
            raise DataError(
                f"{source_namespace} draft crosswalk {draft_year} contains duplicate overall pick {pick}"
            )
        cfbd_by_pick[pick] = dict(raw)
    pick_numbers = sorted(set(cfbd_by_pick) & set(nfl_by_pick))
    unreconciled = sorted(set(cfbd_by_pick) ^ set(nfl_by_pick))
    links: list[dict[str, Any]] = []
    name_agreement = 0
    college_agreement = 0
    for pick in pick_numbers:
        cfbd = cfbd_by_pick[pick]
        nfl = dict(nfl_by_pick[pick])
        names_agree = normalize_name(cfbd.get("name")) == normalize_name(nfl.get("pfr_player_name"))
        colleges_agree = normalize_name(cfbd.get("collegeTeam")) == normalize_name(nfl.get("college"))
        name_agreement += int(names_agree)
        college_agreement += int(colleges_agree)
        links.append(
            {
                "draft_year": draft_year,
                "draft_ovr": pick,
                "draft_round": _as_int(nfl.get("round")),
                "nfl_team": nfl.get("team"),
                "nflverse_name": nfl.get("pfr_player_name"),
                "nflverse_college": nfl.get("college"),
                "nflverse_pfr_player_id": nfl.get("pfr_player_id"),
                "nflverse_cfb_player_id": nfl.get("cfb_player_id"),
                "cfbd_player_id": str(cfbd.get("collegeAthleteId") or "").strip(),
                "cfbd_name": cfbd.get("name"),
                "cfbd_school": cfbd.get("collegeTeam"),
                "name_agrees": names_agree,
                "college_agrees": colleges_agree,
            }
        )
    return links, {
        "nflverse_pick_count": len(nfl_by_pick),
        "college_provider_pick_count": len(cfbd_by_pick),
        "cross_source_pick_numbers_reconciled": len(links),
        "cross_source_name_agreements": name_agreement,
        "cross_source_name_mismatches": len(links) - name_agreement,
        "cross_source_college_agreements": college_agreement,
        "unreconciled_pick_numbers": unreconciled,
        "invalid_college_provider_draft_rows_dropped": invalid_cfbd,
    }


def _match_draft_outcomes(
    roster: Mapping[str, Mapping[str, Any]],
    draft_links: Sequence[Mapping[str, Any]],
    *,
    team_aliases: Mapping[str, Mapping[str, Any]],
    stat_profiles: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    roster_source_label: str = "CollegeFootballData",
    source_namespace: str = "cfbd",
    allow_reviewed_roster_omissions: bool = False,
    reviewed_roster_source_omissions: set[
        tuple[int, int, str, str]
    ] = _REVIEWED_ROSTER_SOURCE_OMISSIONS,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    name_team_index: dict[tuple[str, str], list[str]] = defaultdict(list)
    for player_id, row in roster.items():
        team_key = _canonical_team_key(row.get("team"), team_aliases)
        name_team_index[(normalize_name(_roster_name(row)), team_key)].append(player_id)

    outcomes: dict[str, dict[str, Any]] = {}
    method_counts: Counter[str] = Counter()
    expected_fbs = 0
    matched_expected = 0
    unmatched_expected: list[dict[str, Any]] = []
    identity_conflicts: list[dict[str, Any]] = []
    resolved_alias_conflicts: list[dict[str, Any]] = []
    resolved_name_aliases: list[dict[str, Any]] = []
    resolved_cross_id_aliases: list[dict[str, Any]] = []
    reviewed_roster_omissions: list[dict[str, Any]] = []
    duplicate_outcomes: list[dict[str, Any]] = []
    source_fbs_picks = 0
    profiles = stat_profiles or {}
    for link in draft_links:
        direct_id = str(link.get("cfbd_player_id") or "")
        direct = direct_id if direct_id in roster else None
        team_key = _canonical_team_key(link.get("cfbd_school"), team_aliases)
        cfbd_fallback_candidates = name_team_index.get(
            (normalize_name(link.get("cfbd_name")), team_key),
            [],
        )
        nflverse_fallback_candidates = name_team_index.get(
            (normalize_name(link.get("nflverse_name")), team_key),
            [],
        )
        candidate_sources: dict[str, set[str]] = defaultdict(set)
        for player_id in cfbd_fallback_candidates:
            candidate_sources[player_id].add("cfbd_name")
        for player_id in nflverse_fallback_candidates:
            candidate_sources[player_id].add("nflverse_name")
        fallback_candidates = sorted(candidate_sources)
        fallback = fallback_candidates[0] if len(fallback_candidates) == 1 else None
        stat_candidates = [
            player_id
            for player_id in fallback_candidates
            if _has_team_stat_profile(
                player_id,
                profiles,
                team_key=team_key,
                team_aliases=team_aliases,
            )
        ]
        stat_fallback = stat_candidates[0] if len(stat_candidates) == 1 else None
        direct_aliases = [
            candidate_id for candidate_id in fallback_candidates if candidate_id != direct
        ]
        if direct and direct_aliases:
            if len(direct_aliases) == 1 and _crosswalk_resolves_incomplete_name_alias(
                roster[direct],
                roster[direct_aliases[0]],
                link,
                team_aliases=team_aliases,
            ):
                fallback = direct_aliases[0]
                player_id = direct
                method = f"{source_namespace}_player_id_resolved_incomplete_name_alias"
                resolved = _safe_pick_summary(link)
                resolved.update(
                    {
                        "selected_player_id": direct,
                        "incomplete_alias_player_id": fallback,
                        "selected_roster_name": _roster_name(roster[direct]),
                        "incomplete_alias_roster_name": _roster_name(roster[fallback]),
                    }
                )
                resolved_alias_conflicts.append(resolved)
                resolved_cross_id_aliases.append(
                    {
                        "selected_player_id": direct,
                        "dropped_player_ids": [fallback],
                        "evidence": "stable crosswalk identity over incomplete nickname stub",
                    }
                )
            elif len(direct_aliases) == 1 and _crosswalk_resolves_incomplete_name_alias(
                roster[direct_aliases[0]],
                roster[direct],
                link,
                team_aliases=team_aliases,
                minimum_complete_details=1,
            ) and _has_team_stat_profile(
                direct_aliases[0],
                profiles,
                team_key=team_key,
                team_aliases=team_aliases,
            ):
                selected_alias = direct_aliases[0]
                player_id = selected_alias
                method = "exact_name_team_resolved_incomplete_crosswalk_stub"
                resolved = _safe_pick_summary(link)
                resolved.update(
                    {
                        "selected_player_id": selected_alias,
                        "incomplete_crosswalk_player_id": direct,
                        "selected_roster_name": _roster_name(roster[selected_alias]),
                        "incomplete_crosswalk_roster_name": _roster_name(roster[direct]),
                    }
                )
                resolved_alias_conflicts.append(resolved)
                resolved_cross_id_aliases.append(
                    {
                        "selected_player_id": selected_alias,
                        "dropped_player_ids": [direct],
                        "evidence": "complete exact-name stat identity over incomplete crosswalk stub",
                    }
                )
            elif bool(profiles.get(direct)) and all(
                _strong_roster_alias_equivalence(
                    roster[direct],
                    roster[alias_id],
                    team_aliases=team_aliases,
                )
                for alias_id in direct_aliases
            ):
                player_id = direct
                method = f"{source_namespace}_player_id_strong_duplicate_alias"
                resolved = _safe_pick_summary(link)
                resolved.update(
                    {
                        "selected_player_id": direct,
                        "rejected_name_alias_player_ids": direct_aliases,
                        "selected_roster_name": _roster_name(roster[direct]),
                        "rejected_roster_names": [
                            _roster_name(roster[alias_id]) for alias_id in direct_aliases
                        ],
                        "evidence": "strong duplicate identity plus checkpoint stat support",
                    }
                )
                resolved_name_aliases.append(resolved)
                resolved_cross_id_aliases.append(
                    {
                        "selected_player_id": direct,
                        "dropped_player_ids": direct_aliases,
                        "evidence": "strong duplicate identity plus checkpoint stat support",
                    }
                )
            else:
                conflict = _safe_pick_summary(link)
                conflict.update(
                    {
                        "direct_player_id": direct,
                        "candidate_player_ids": direct_aliases,
                        "direct_roster_name": _roster_name(roster[direct]),
                        "candidate_roster_names": [
                            _roster_name(roster[alias_id]) for alias_id in direct_aliases
                        ],
                    }
                )
                identity_conflicts.append(conflict)
                player_id = None
                method = "identity_conflict"
        elif direct:
            player_id = direct
            method = f"{source_namespace}_player_id"
        elif fallback:
            player_id = fallback
            method = (
                "exact_name_team"
                if "cfbd_name" in candidate_sources[fallback]
                else "exact_nflverse_name_team"
            )
        elif stat_fallback and all(
            _strong_roster_alias_equivalence(
                roster[stat_fallback],
                roster[candidate_id],
                team_aliases=team_aliases,
            )
            for candidate_id in fallback_candidates
            if candidate_id != stat_fallback
        ):
            player_id = stat_fallback
            method = "exact_name_team_prior_stat"
            resolved = _safe_pick_summary(link)
            resolved.update(
                {
                    "selected_player_id": stat_fallback,
                    "candidate_player_ids": fallback_candidates,
                    "selected_roster_name": _roster_name(roster[stat_fallback]),
                    "evidence": "unique prior/current checkpoint stat identity",
                }
            )
            resolved_name_aliases.append(resolved)
            resolved_cross_id_aliases.append(
                {
                    "selected_player_id": stat_fallback,
                    "dropped_player_ids": [
                        candidate_id
                        for candidate_id in fallback_candidates
                        if candidate_id != stat_fallback
                    ],
                    "evidence": "strong duplicate identity plus unique checkpoint stat ID",
                }
            )
        elif fallback_candidates:
            player_id = None
            method = "ambiguous_name_team"
            conflict = _safe_pick_summary(link)
            conflict["candidate_player_ids"] = fallback_candidates
            identity_conflicts.append(conflict)
        else:
            player_id = None
            method = "unmatched"

        school_is_fbs = team_key in {
            normalize_name(team.get("school")) for team in team_aliases.values() if team.get("school")
        }
        source_fbs_picks += int(school_is_fbs)
        reviewed_omission = bool(
            allow_reviewed_roster_omissions
            and school_is_fbs
            and not direct
            and not fallback_candidates
            and _is_reviewed_roster_omission(
                link,
                reviewed_roster_source_omissions,
            )
        )
        if reviewed_omission:
            omission = _safe_pick_summary(link)
            omission["reason"] = (
                f"not listed in the corresponding {roster_source_label} FBS roster snapshot"
            )
            reviewed_roster_omissions.append(omission)
        is_expected_fbs = bool(
            (school_is_fbs or direct or fallback_candidates) and not reviewed_omission
        )
        if is_expected_fbs:
            expected_fbs += 1
        if player_id and is_expected_fbs:
            matched_expected += 1
        elif is_expected_fbs:
            unmatched_expected.append(_safe_pick_summary(link))

        if not player_id:
            continue
        if player_id in outcomes:
            duplicate_outcomes.append(_safe_pick_summary(link))
            continue
        outcome = dict(link)
        outcome["match_method"] = method
        outcomes[player_id] = outcome
        method_counts[method] += 1
    return outcomes, {
        "expected_fbs_draft_picks": expected_fbs,
        "matched_expected_fbs_draft_picks": matched_expected,
        "positive_match_coverage": matched_expected / expected_fbs if expected_fbs else None,
        "positive_match_methods": dict(sorted(method_counts.items())),
        "source_fbs_school_draft_picks": source_fbs_picks,
        "unmatched_expected_fbs_picks": unmatched_expected,
        "identity_conflicts": identity_conflicts,
        "resolved_incomplete_name_aliases": resolved_alias_conflicts,
        "resolved_name_aliases": resolved_name_aliases,
        "resolved_cross_id_roster_aliases": resolved_cross_id_aliases,
        "reviewed_roster_source_omissions": reviewed_roster_omissions,
        "reviewed_roster_source_omission_count": len(reviewed_roster_omissions),
        "duplicate_roster_outcome_links": duplicate_outcomes,
    }


def _has_team_stat_profile(
    player_id: str,
    stat_profiles: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    team_key: str,
    team_aliases: Mapping[str, Mapping[str, Any]],
) -> bool:
    for raw_team in stat_profiles.get(player_id, {}):
        if _canonical_team_key(raw_team, team_aliases) == team_key:
            return True
    return False


def _is_reviewed_roster_omission(
    link: Mapping[str, Any],
    reviewed: set[tuple[int, int, str, str]] = _REVIEWED_ROSTER_SOURCE_OMISSIONS,
) -> bool:
    key = (
        _as_int(link.get("draft_year")),
        _as_int(link.get("draft_ovr")),
        normalize_name(link.get("nflverse_name") or link.get("cfbd_name")),
        normalize_name(link.get("cfbd_school") or link.get("nflverse_college")),
    )
    return key in reviewed


def _strong_roster_alias_equivalence(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    team_aliases: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Require multi-field evidence before treating two roster IDs as one player."""

    left_team = _canonical_team_key(left.get("team"), team_aliases)
    right_team = _canonical_team_key(right.get("team"), team_aliases)
    if not left_team or left_team != right_team:
        return False
    left_surname = _roster_surname(left)
    right_surname = _roster_surname(right)
    left_name = _roster_name(left).strip()
    right_name = _roster_name(right).strip()
    if (
        not left_surname
        or left_surname != right_surname
        or not left_name
        or not right_name
        or left_name[0].lower() != right_name[0].lower()
    ):
        return False
    left_position = normalize_position(left.get("position"))
    right_position = normalize_position(right.get("position"))
    compatible_groups = (
        {"LB", "EDGE"},
        {"CB", "S"},
        {"IOL", "OT"},
    )
    if left_position not in POSITION_GROUPS or not (
        left_position == right_position
        or any({left_position, right_position} <= group for group in compatible_groups)
    ):
        return False

    agreements = 0
    identity_agreements = 0
    for field in ("height", "weight", "year"):
        left_value = parse_number(left.get(field))
        right_value = parse_number(right.get(field))
        if left_value is not None and right_value is not None and left_value == right_value:
            agreements += 1
    for field in ("jersey", "homeCity", "homeState", "homeCountyFIPS"):
        left_value = normalize_name(left.get(field))
        right_value = normalize_name(right.get(field))
        if left_value and left_value == right_value:
            agreements += 1
            identity_agreements += 1
    left_recruits = {str(value) for value in (left.get("recruitIds") or []) if value not in (None, "")}
    right_recruits = {str(value) for value in (right.get("recruitIds") or []) if value not in (None, "")}
    if left_recruits & right_recruits:
        agreements += 1
        identity_agreements += 1
    return agreements >= 3 and identity_agreements >= 1


def _collapse_resolved_roster_aliases(
    roster: Mapping[str, Mapping[str, Any]],
    resolutions: Iterable[Mapping[str, Any]],
    *,
    outcomes: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    collapsed = {player_id: dict(row) for player_id, row in roster.items()}
    dropped: list[str] = []
    merged_fields = 0
    outcome_conflicts: list[str] = []
    merge_fields = (
        "height",
        "weight",
        "year",
        "position",
        "homeCity",
        "homeState",
        "homeCountry",
        "homeLatitude",
        "homeLongitude",
        "homeCountyFIPS",
        "recruitIds",
    )
    for resolution in resolutions:
        selected_id = str(resolution.get("selected_player_id") or "")
        if selected_id not in collapsed:
            continue
        selected = collapsed[selected_id]
        for raw_id in resolution.get("dropped_player_ids") or []:
            dropped_id = str(raw_id or "")
            if not dropped_id or dropped_id == selected_id or dropped_id not in collapsed:
                continue
            if dropped_id in outcomes:
                outcome_conflicts.append(dropped_id)
                continue
            alias = collapsed[dropped_id]
            for field in merge_fields:
                if selected.get(field) in (None, "", []) and alias.get(field) not in (None, "", []):
                    selected[field] = alias[field]
                    merged_fields += 1
            del collapsed[dropped_id]
            dropped.append(dropped_id)
    return collapsed, {
        "cross_id_duplicate_alias_rows_dropped": len(dropped),
        "cross_id_duplicate_alias_player_ids_dropped": sorted(dropped),
        "cross_id_duplicate_alias_fields_merged": merged_fields,
        "cross_id_duplicate_alias_outcome_conflicts": sorted(set(outcome_conflicts)),
    }


def _collapse_negative_roster_aliases(
    roster: Mapping[str, Mapping[str, Any]],
    *,
    outcomes: Mapping[str, Mapping[str, Any]],
    stat_profiles: Mapping[str, Mapping[str, Mapping[str, Any]]],
    team_aliases: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Collapse only strongly evidenced duplicate non-selection identities.

    Positive aliases have an immutable draft crosswalk identity. Negative rows
    do not, so this path is deliberately narrower: candidates must pass the
    same multi-field identity test used for positive aliases and exactly one ID
    must own a checkpoint stat profile for the same team. Same-name teammates,
    aliases with no stat anchor, and aliases with multiple stat IDs are kept as
    distinct denominator rows.
    """

    collapsed = {player_id: dict(row) for player_id, row in roster.items()}
    grouped: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for player_id, row in collapsed.items():
        if player_id in outcomes:
            continue
        team_key = _canonical_team_key(row.get("team"), team_aliases)
        surname = _roster_surname(row)
        name = _roster_name(row).strip()
        if team_key and surname and name:
            grouped[(team_key, surname, name[0].lower())].append(player_id)

    candidate_groups = 0
    candidate_rows = 0
    unique_stat_groups = 0
    missing_stat_groups = 0
    multiple_stat_groups = 0
    weak_identity_pairs = 0
    collapsed_groups = 0
    dropped_ids: list[str] = []
    selected_ids: list[str] = []
    merged_fields = 0
    merge_fields = (
        "height",
        "weight",
        "year",
        "position",
        "homeCity",
        "homeState",
        "homeCountry",
        "homeLatitude",
        "homeLongitude",
        "homeCountyFIPS",
        "recruitIds",
    )

    for (team_key, _surname, _initial), raw_ids in sorted(grouped.items()):
        player_ids = sorted(player_id for player_id in raw_ids if player_id in collapsed)
        if len(player_ids) < 2:
            continue
        candidate_groups += 1
        candidate_rows += len(player_ids)
        stat_ids = [
            player_id
            for player_id in player_ids
            if _has_team_stat_profile(
                player_id,
                stat_profiles,
                team_key=team_key,
                team_aliases=team_aliases,
            )
        ]
        if not stat_ids:
            missing_stat_groups += 1
            continue
        if len(stat_ids) > 1:
            multiple_stat_groups += 1
            continue
        unique_stat_groups += 1
        selected_id = stat_ids[0]
        selected = collapsed[selected_id]
        aliases: list[str] = []
        for candidate_id in player_ids:
            if candidate_id == selected_id:
                continue
            if _strong_roster_alias_equivalence(
                selected,
                collapsed[candidate_id],
                team_aliases=team_aliases,
            ):
                aliases.append(candidate_id)
            else:
                weak_identity_pairs += 1
        if not aliases:
            continue

        collapsed_groups += 1
        selected_ids.append(selected_id)
        for dropped_id in aliases:
            alias = collapsed[dropped_id]
            for field in merge_fields:
                if selected.get(field) in (None, "", []) and alias.get(field) not in (
                    None,
                    "",
                    [],
                ):
                    selected[field] = alias[field]
                    merged_fields += 1
            del collapsed[dropped_id]
            dropped_ids.append(dropped_id)
        selected["_duplicate_rows_collapsed"] = int(
            selected.get("_duplicate_rows_collapsed") or 0
        ) + len(aliases)

    return collapsed, {
        "negative_alias_candidate_groups": candidate_groups,
        "negative_alias_candidate_rows": candidate_rows,
        "negative_alias_groups_with_unique_checkpoint_stat_id": unique_stat_groups,
        "negative_alias_groups_without_checkpoint_stat_id": missing_stat_groups,
        "negative_alias_groups_with_multiple_checkpoint_stat_ids": multiple_stat_groups,
        "negative_alias_pairs_rejected_weak_identity": weak_identity_pairs,
        "negative_alias_groups_collapsed": collapsed_groups,
        "negative_cross_id_alias_rows_dropped": len(dropped_ids),
        "negative_cross_id_alias_player_ids_dropped": sorted(dropped_ids),
        "negative_cross_id_alias_selected_player_ids": sorted(selected_ids),
        "negative_cross_id_alias_fields_merged": merged_fields,
    }


def _crosswalk_resolves_incomplete_name_alias(
    direct_row: Mapping[str, Any],
    exact_name_row: Mapping[str, Any],
    link: Mapping[str, Any],
    *,
    team_aliases: Mapping[str, Mapping[str, Any]],
    minimum_complete_details: int = 2,
) -> bool:
    """Prefer a stable crosswalk ID over a clearly incomplete nickname stub.

    CFBD occasionally exposes two same-school roster identities for one player:
    a complete legal-name row under the stable athlete ID and a second exact
    draft-name/nickname row with no usable position, measurements, or class.
    This narrow rule resolves only that shape.  A disagreement between two
    otherwise plausible players remains a hard identity conflict.
    """

    link_team = _canonical_team_key(link.get("cfbd_school"), team_aliases)
    direct_team = _canonical_team_key(direct_row.get("team"), team_aliases)
    alias_team = _canonical_team_key(exact_name_row.get("team"), team_aliases)
    if not link_team or direct_team != link_team or alias_team != link_team:
        return False

    direct_surname = _roster_surname(direct_row)
    alias_surname = _roster_surname(exact_name_row)
    draft_surname = _name_surname(link.get("cfbd_name"))
    if not direct_surname or direct_surname != alias_surname or direct_surname != draft_surname:
        return False

    direct_position = normalize_position(direct_row.get("position"))
    alias_position = normalize_position(exact_name_row.get("position"))
    direct_details = _valid_roster_detail_count(direct_row)
    alias_details = _valid_roster_detail_count(exact_name_row)
    return (
        direct_position in POSITION_GROUPS
        and direct_details >= minimum_complete_details
        and alias_position not in POSITION_GROUPS
        and alias_details == 0
    )


def _valid_roster_detail_count(row: Mapping[str, Any]) -> int:
    height, height_valid = _plausible_measurement(row.get("height"), "height_in")
    weight, weight_valid = _plausible_measurement(row.get("weight"), "weight_lb")
    class_year = _as_int(row.get("year"))
    return sum(
        (
            height_valid and height is not None,
            weight_valid and weight is not None,
            class_year is not None and 1 <= class_year <= 6,
        )
    )


def _roster_surname(row: Mapping[str, Any]) -> str:
    explicit = row.get("lastName") or row.get("last_name")
    return normalize_name(explicit) if explicit else _name_surname(_roster_name(row))


def _name_surname(value: Any) -> str:
    tokens = re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*", str(value or ""))
    for token in reversed(tokens):
        normalized = normalize_name(token)
        if normalized:
            return normalized
    return ""


def _build_season_rows(
    roster: Mapping[str, Mapping[str, Any]],
    *,
    stat_profiles: Mapping[str, Mapping[str, Mapping[str, Any]]],
    success_profiles: Mapping[str, Mapping[str, Mapping[str, Any]]],
    recruiting_index: RecruitingIndex,
    outcomes: Mapping[str, Mapping[str, Any]],
    team_aliases: Mapping[str, Mapping[str, Any]],
    season: int,
    as_of_week: int,
    feature_season: int,
    row_population: str,
    measurement_source: str = "cfbd_school_roster",
    age_source: str = "cfbd_roster_date_of_birth",
    player_id_namespace: str = "cfbd",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    stats_matches = 0
    stats_team_mismatches = 0
    success_matches = 0
    recruiting_matches = 0
    invalid_class_year = 0
    invalid_height = 0
    invalid_weight = 0
    roster_team_not_in_fbs_context = 0
    age_available = 0
    supported_position = 0
    orphan_stats_ids = set(stat_profiles) - set(roster)

    for player_id, roster_row in sorted(roster.items(), key=lambda item: (str(item[1].get("team") or ""), _roster_name(item[1]), item[0])):
        row = {field: None for field in HISTORICAL_COLLEGE_SCHEMA}
        name = _roster_name(roster_row)
        raw_position = roster_row.get("position")
        position = normalize_position(raw_position)
        supported = position in POSITION_GROUPS
        supported_position += int(supported)
        team = str(roster_row.get("team") or "").strip()
        team_context = team_aliases.get(normalize_name(team))
        if team_context is None:
            roster_team_not_in_fbs_context += 1

        raw_class = roster_row.get("year")
        parsed_class = _as_int(raw_class)
        class_year = parsed_class if parsed_class is not None and 1 <= parsed_class <= 6 else None
        if raw_class not in (None, "") and class_year is None:
            invalid_class_year += 1

        height, height_valid = _plausible_measurement(roster_row.get("height"), "height_in")
        weight, weight_valid = _plausible_measurement(roster_row.get("weight"), "weight_lb")
        invalid_height += int(not height_valid and roster_row.get("height") not in (None, ""))
        invalid_weight += int(not weight_valid and roster_row.get("weight") not in (None, ""))

        birth = _first(roster_row, "dateOfBirth", "date_of_birth", "birthDate", "birth_date")
        age_at_draft = None
        row_age_source = None
        if birth:
            age_at_draft = _age_on_draft_day(str(birth), season + 1)
            if age_at_draft is not None:
                row_age_source = age_source
                age_available += 1

        profile, stat_match_method = _select_stat_profile(
            player_id,
            team,
            stat_profiles,
            source_namespace=player_id_namespace,
        )
        if profile:
            stats_matches += 1
            stat_team_matches = normalize_name(profile.get("team")) == normalize_name(team)
            stats_team_mismatches += int(not stat_team_matches)
            production = _derive_production(profile.get("values") or {})
        else:
            stat_team_matches = None
            production = {}
        success_profile, _success_match_method = _select_stat_profile(
            player_id,
            team,
            success_profiles,
            source_namespace=player_id_namespace,
        )
        if success_profile:
            success_matches += 1
            production.update(derive_player_success(success_profile))
        recruiting = match_recruiting_profile(
            roster_row,
            recruiting_index,
            roster_season=season,
        )
        recruiting_matches += int(bool(recruiting))

        outcome = outcomes.get(player_id)
        drafted = outcome is not None
        row.update(
            {
                "row_id": f"{season}:{player_id}",
                "player_id": player_id,
                "cfbd_player_id": (
                    player_id
                    if player_id_namespace == "cfbd"
                    and not str(player_id).startswith("missing:")
                    else None
                ),
                "name": name,
                "position_raw": raw_position,
                "position": position,
                "position_supported": supported,
                "school": team,
                "conference": (team_context.get("conference") if team_context else None)
                or (profile.get("conference") if profile else None),
                "season": season,
                "draft_year": season + 1,
                "checkpoint": _checkpoint_label(as_of_week),
                "as_of_week": as_of_week,
                "feature_cutoff_season": feature_season,
                "model_stage": MODEL_STAGE,
                "class_year": class_year,
                "class_year_raw": raw_class,
                **recruiting,
                "date_of_birth": birth,
                "age_at_draft": age_at_draft,
                "age_source": row_age_source,
                "height_in": height,
                "weight_lb": weight,
                "measurement_source": measurement_source,
                "measurement_date": None,
                "measurements_verified": False,
                "has_recorded_stats": profile is not None,
                "stat_row_count": int(profile.get("row_count") or 0) if profile else 0,
                "stats_team": profile.get("team") if profile else None,
                "stats_categories": "|".join(profile.get("categories") or []) if profile else None,
                "stats_school_matches_roster": stat_team_matches,
                "stats_match_method": stat_match_method,
                "drafted": drafted,
                "draft_round": _as_int(outcome.get("draft_round")) if outcome else None,
                "draft_ovr": _as_int(outcome.get("draft_ovr")) if outcome else None,
                "elite": bool(outcome and _as_int(outcome.get("draft_ovr")) <= 64),
                "nfl_team": outcome.get("nfl_team") if outcome else None,
                "outcome_label_known": True,
                "outcome_source": "nflverse_draft_picks",
                "outcome_match_method": (
                    str(outcome.get("match_method")) if outcome else "complete_next_draft_nonselection"
                ),
                "outcome_verified_by": (
                    "nflverse year+overall pick reconciled to CFBD draft crosswalk"
                    if outcome
                    else "absence from complete contiguous nflverse next-draft class"
                ),
                "outcome_nflverse_pfr_player_id": outcome.get("nflverse_pfr_player_id") if outcome else None,
                "outcome_nflverse_cfb_player_id": outcome.get("nflverse_cfb_player_id") if outcome else None,
                "outcome_cfbd_crosswalk_id": outcome.get("cfbd_player_id") if outcome else None,
                "population": row_population,
                "probability_kind": PROBABILITY_KIND,
                "probability_condition": PROBABILITY_CONDITION,
                "roster_source_year": season,
                "stats_source_year": feature_season,
                "stats_season_type": "both",
                "outcome_source_draft_year": season + 1,
                "roster_duplicate_rows_collapsed": int(roster_row.get("_duplicate_rows_collapsed") or 0),
                **production,
            }
        )
        rows.append({field: row.get(field) for field in HISTORICAL_COLLEGE_SCHEMA})

    position_counts = Counter(str(row["position"] or "") for row in rows)
    return rows, {
        "output_rows": len(rows),
        "position_counts": dict(sorted(position_counts.items())),
        "supported_position_rows": supported_position,
        "unsupported_position_rows_retained": len(rows) - supported_position,
        "roster_players_with_stats": stats_matches,
        "roster_player_stats_coverage": stats_matches / len(rows) if rows else None,
        "roster_stats_team_mismatches": stats_team_mismatches,
        "roster_players_with_success_metrics": success_matches,
        "roster_player_success_coverage": (
            success_matches / len(rows) if rows else None
        ),
        "roster_players_with_recruiting_profile": recruiting_matches,
        "roster_recruiting_profile_coverage": (
            recruiting_matches / len(rows) if rows else None
        ),
        "orphan_stat_players_not_in_roster": len(orphan_stats_ids),
        "invalid_class_year_values_set_missing": invalid_class_year,
        "height_available": sum(row["height_in"] is not None for row in rows),
        "height_coverage": sum(row["height_in"] is not None for row in rows) / len(rows) if rows else None,
        "invalid_height_values_set_missing": invalid_height,
        "weight_available": sum(row["weight_lb"] is not None for row in rows),
        "weight_coverage": sum(row["weight_lb"] is not None for row in rows) / len(rows) if rows else None,
        "invalid_weight_values_set_missing": invalid_weight,
        "class_year_available": sum(row["class_year"] is not None for row in rows),
        "class_year_coverage": sum(row["class_year"] is not None for row in rows) / len(rows) if rows else None,
        "age_at_draft_available_from_roster_dob": age_available,
        "age_at_draft_coverage": age_available / len(rows) if rows else None,
        "roster_teams_missing_fbs_context": roster_team_not_in_fbs_context,
    }


def _select_stat_profile(
    player_id: str,
    roster_team: str,
    profiles: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    source_namespace: str = "cfbd",
) -> tuple[dict[str, Any] | None, str]:
    team_profiles = profiles.get(player_id)
    if not team_profiles:
        return None, "none"
    roster_key = normalize_name(roster_team)
    if roster_key in team_profiles:
        return dict(team_profiles[roster_key]), f"{source_namespace}_player_id_and_team"
    chosen = max(
        team_profiles.values(),
        key=lambda item: (int(item.get("row_count") or 0), str(item.get("team") or "")),
    )
    return dict(chosen), f"{source_namespace}_player_id_team_mismatch"


def _derive_production(values: Mapping[tuple[str, str], Any]) -> dict[str, Any]:
    return derive_college_production(values)


def _stat_number(value: Any) -> float | None:
    return parse_college_stat_number(value)


def _prefer_stat_value(left: Any, right: Any) -> Any:
    left_number = _stat_number(left)
    right_number = _stat_number(right)
    if left_number is not None and right_number is not None:
        return right if right_number > left_number else left
    return min(str(left), str(right))


def _plausible_measurement(value: Any, field: str) -> tuple[float | None, bool]:
    try:
        parsed = parse_height(value) if field == "height_in" else parse_number(value)
    except DataError:
        return None, False
    if parsed is None:
        return None, True
    low, high = PLAUSIBLE_RANGES[field]
    return (parsed, True) if low <= parsed <= high else (None, False)


def _age_on_draft_day(value: str, draft_year: int) -> float | None:
    try:
        born = date.fromisoformat(value[:10])
        target = date(draft_year, 4, 30)
    except (TypeError, ValueError):
        return None
    age = (target - born).days / 365.2425
    return round(age, 3) if 18.0 <= age <= 30.0 else None


def _canonical_team_key(value: Any, aliases: Mapping[str, Mapping[str, Any]]) -> str:
    key = normalize_name(value)
    team = aliases.get(key)
    return normalize_name(team.get("school")) if team else key


def _roster_name(row: Mapping[str, Any]) -> str:
    explicit = row.get("name")
    if explicit:
        return str(explicit).strip()
    return f"{row.get('firstName') or ''} {row.get('lastName') or ''}".strip()


def _safe_pick_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "draft_year": _as_int(row.get("draft_year")),
        "draft_ovr": _as_int(row.get("draft_ovr")),
        "name": row.get("nflverse_name") or row.get("cfbd_name"),
        "college": row.get("cfbd_school") or row.get("nflverse_college"),
        "cfbd_player_id": row.get("cfbd_player_id"),
    }


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if row.get(key) not in (None, ""):
            return row.get(key)
    return None


def _as_int(value: Any) -> int | None:
    try:
        number = parse_number(value)
    except DataError:
        return None
    if number is None or not float(number).is_integer():
        return None
    return int(number)


def _sum_drop_reasons(per_season: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    fields = (
        "duplicate_roster_rows_dropped",
        "cross_id_duplicate_alias_rows_dropped",
        "negative_cross_id_alias_rows_dropped",
        "stat_rows_missing_player_id_dropped",
        "wrong_season_stat_rows_dropped",
        "identical_duplicate_stat_values_collapsed",
        "invalid_college_provider_draft_rows_dropped",
        "invalid_class_year_values_set_missing",
        "invalid_height_values_set_missing",
        "invalid_weight_values_set_missing",
        "orphan_stat_players_not_in_roster",
    )
    return {
        field: sum(int(item.get(field) or 0) for item in per_season)
        for field in fields
    }


def _atomic_json(destination: Path, value: Any) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temp_name, destination)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _file_sha256(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "CHECKPOINT",
    "DEFAULT_COLLEGE_SEASONS",
    "HISTORICAL_COLLEGE_SCHEMA",
    "MODEL_FEATURE_FIELDS",
    "MODEL_STAGE",
    "OUTCOME_FIELDS",
    "POPULATION",
    "PROBABILITY_CONDITION",
    "PROBABILITY_KIND",
    "PRODUCTION_FIELDS",
    "audit_historical_college_training_data",
    "build_historical_college_training_data",
]

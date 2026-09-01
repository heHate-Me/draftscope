#!/usr/bin/env python3
"""Rebuild and score one outcome-blind historical DraftScope holdout.

The default invocation reconstructs the 2025 Ashton Jeanty case from public
SportsDataverse and nflverse releases. Downloaded files remain in ignored local
cache directories; only a compact JSON result is printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE = REPOSITORY / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from draftscope.analysis import ProspectEvaluator  # noqa: E402
from draftscope.historical_college import (  # noqa: E402
    audit_historical_college_training_data,
    build_historical_college_training_data,
)
from draftscope.mathstats import (  # noqa: E402
    average_precision,
    brier_score,
    log_loss,
    roc_auc,
)
from draftscope.model import (  # noqa: E402
    COLLEGE_POSITION_POOLS,
    CollegeDraftProbabilityModel,
)
from draftscope.records import (  # noqa: E402
    DataError,
    load_records,
    normalize_name,
    parse_bool,
    parse_number,
)
from draftscope.sportsdataverse import SportsDataverseClient  # noqa: E402
from draftscope.sportsdataverse_adapter import (  # noqa: E402
    SportsDataverseHistoricalClient,
)
from draftscope.model import is_manual_scouting_field  # noqa: E402
from draftscope.schema import all_specs  # noqa: E402


DEFAULT_CHECKPOINT_EVIDENCE_FIELDS = (
    "height_in",
    "weight_lb",
    "age_at_draft",
    "class_year",
    "prod_carries",
    "prod_rush_yards",
    "prod_rush_tds",
    "prod_receptions",
    "prod_receiving_yards",
    "prod_receiving_tds",
    "prod_scrimmage_yards",
    "prod_touchdowns",
    "observed_yards_per_carry",
    "prod_yards_per_carry",
    "observed_yards_per_touch",
    "prod_yards_per_touch",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reproduce a strict past-only DraftScope holdout score."
    )
    parser.add_argument("--player", default="Ashton Jeanty")
    parser.add_argument("--school", default="Boise State")
    parser.add_argument("--position", default="RB")
    parser.add_argument("--holdout-year", type=int, default=2025)
    parser.add_argument(
        "--week",
        type=int,
        default=16,
        help="Completed-week bound used for every reconstructed college season.",
    )
    parser.add_argument(
        "--training-draft-years",
        type=int,
        default=8,
        help="Number of strictly earlier draft classes included in training.",
    )
    parser.add_argument(
        "--history",
        help="Use an existing audited historical CSV/JSON instead of rebuilding public sources.",
    )
    parser.add_argument("--cache-dir", default=".draftscope-cache")
    parser.add_argument("--refresh", action="store_true")
    return parser


def _integer(value: Any) -> int | None:
    number = parse_number(value)
    return int(number) if number is not None else None


def _canonical_rows_sha256(rows: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        model_audit_row = {
            key: value
            for key, value in row.items()
            if not is_manual_scouting_field(key)
        }
        digest.update(
            json.dumps(
                model_audit_row,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                default=str,
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _source_digest(metadata: Mapping[str, Any]) -> tuple[str, int]:
    artifacts = [
        {
            "endpoint": item.get("endpoint"),
            "params": item.get("params"),
            "sha256": item.get("sha256"),
            "source": item.get("source"),
            "url": item.get("url"),
        }
        for item in metadata.get("source_artifacts", ())
        if isinstance(item, Mapping)
    ]
    payload = json.dumps(
        artifacts,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest(), len(artifacts)


def _load_or_build_history(
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if args.history:
        path = Path(args.history).expanduser().resolve()
        rows = load_records(path)
        metadata_path = path.with_suffix(".metadata.json")
        metadata = (
            json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata_path.is_file()
            else {}
        )
        audit = audit_historical_college_training_data(rows)
        if audit.get("status") != "pass":
            raise DataError("The supplied historical file failed its contract audit")
        metadata.setdefault("contract_audit", audit)
        metadata.setdefault("rows", len(rows))
        metadata.setdefault(
            "drafted_rows",
            sum(parse_bool(row.get("drafted")) is True for row in rows),
        )
        return rows, metadata

    cache_dir = Path(args.cache_dir).expanduser().resolve()
    college = SportsDataverseClient(
        cache_dir,
        nflverse_cache_dir=cache_dir,
        refresh=args.refresh,
    )
    historical = SportsDataverseHistoricalClient(
        college,
        nflverse_cache_dir=cache_dir,
        refresh=args.refresh,
    )
    first_college_season = args.holdout_year - args.training_draft_years - 1
    college_seasons = range(first_college_season, args.holdout_year)
    return build_historical_college_training_data(
        historical,
        college_seasons=college_seasons,
        cache_dir=cache_dir,
        as_of_week=args.week,
        outcome_blind_alias_draft_years=(args.holdout_year,),
        refresh=args.refresh,
        strict=True,
    )


def _find_candidate(
    rows: Sequence[Mapping[str, Any]],
    *,
    name: str,
    school: str,
) -> int:
    wanted_name = normalize_name(name)
    wanted_school = normalize_name(school)
    matches = [
        index
        for index, row in enumerate(rows)
        if normalize_name(row.get("name")) == wanted_name
        and normalize_name(row.get("school")) == wanted_school
    ]
    if len(matches) != 1:
        raise DataError(
            f"Expected one exact {name!r} / {school!r} holdout row; found {len(matches)}"
        )
    return matches[0]


def build_case_result(args: argparse.Namespace) -> dict[str, Any]:
    if args.week < 1:
        raise DataError("A retrospective season checkpoint requires --week >= 1")
    if args.training_draft_years < 3:
        raise DataError("At least three earlier draft classes are required")

    rows, metadata = _load_or_build_history(args)
    position = str(args.position).strip().upper()
    holdout_year = int(args.holdout_year)
    metadata_week = _integer(metadata.get("as_of_week"))
    if metadata_week is not None and metadata_week != args.week:
        raise DataError(
            f"History is Week {metadata_week}, but the requested replay is Week {args.week}"
        )
    blind_years = {
        int(year) for year in metadata.get("outcome_blind_alias_draft_years", ())
    }
    if holdout_year not in blind_years:
        raise DataError(
            "The holdout roster was not deduplicated before its outcomes were joined; "
            "rebuild it with outcome-blind holdout alias resolution"
        )
    first_training_year = holdout_year - args.training_draft_years
    expected_training_years = set(range(first_training_year, holdout_year))
    prior_rows = [
        row
        for row in rows
        if first_training_year
        <= (_integer(row.get("draft_year")) or 0)
        < holdout_year
    ]
    source_position_pool = set(COLLEGE_POSITION_POOLS.get(position, (position,)))
    holdout_rows = [
        row
        for row in rows
        if _integer(row.get("draft_year")) == holdout_year
        and str(row.get("position") or "").strip().upper() in source_position_pool
    ]
    if not holdout_rows:
        raise DataError(f"No {position} rows found for the {holdout_year} holdout")

    population = str(
        metadata.get("row_population")
        or holdout_rows[0].get("population")
        or "FBS roster at requested checkpoint"
    )
    model = CollegeDraftProbabilityModel(
        prior_rows,
        position=position,
        population=population,
    )
    if not model.validation.passes_quality_gate:
        raise DataError(
            "The prior-only position model failed its publication gate; no replay is published"
        )
    replay = model.score_retrospective_holdout(
        holdout_rows,
        holdout_year=holdout_year,
    )
    if set(replay.training_years) != expected_training_years:
        raise DataError(
            "The requested position is missing one or more training draft years: "
            f"expected {sorted(expected_training_years)}, got {list(replay.training_years)}"
        )
    candidate_index = _find_candidate(
        holdout_rows,
        name=args.player,
        school=args.school,
    )
    score_by_index = {score.input_index: score for score in replay.scores}
    candidate_score = score_by_index[candidate_index]
    candidate = holdout_rows[candidate_index]

    comparison_keys = dict.fromkeys(
        (
            "player_id",
            "name",
            "school",
            "position",
            "draft_year",
            *(
                spec.key
                for spec in all_specs(position)
                if spec.category != "skills"
            ),
        )
    )
    comparison_candidate = {
        key: candidate.get(key)
        for key in comparison_keys
        if candidate.get(key) not in (None, "")
    }
    comparison_candidate["position"] = position
    comparison_evaluator = ProspectEvaluator(
        prior_rows,
        [comparison_candidate],
        model_history=prior_rows,
        career_elite_history=[],
    )
    drafted_comparisons = comparison_evaluator._comparables(
        comparison_candidate,
        prior_rows,
        categories={"physical", "production", "skills", "age"},
        limit=5,
        use_benchmark_cohort=False,
        reference="strictly earlier same-position drafted profiles",
    )
    pick_band = comparison_evaluator._pick_range(drafted_comparisons)

    forecast_payload = {
        "holdout_year": holdout_year,
        "position": position,
        "training_years": replay.training_years,
        "features": replay.features,
        "scores": [
            {
                "player_id": holdout_rows[index].get("player_id"),
                "name": holdout_rows[index].get("name"),
                "school": holdout_rows[index].get("school"),
                "raw_probability": score_by_index[index].raw_probability,
                "probability": score_by_index[index].probability,
                "rank": score_by_index[index].within_position_rank,
                "feature_coverage": score_by_index[index].feature_coverage,
                "out_of_distribution_score": score_by_index[
                    index
                ].out_of_distribution_score,
            }
            for index in range(len(holdout_rows))
        ],
    }
    forecast_sha256 = _canonical_json_sha256(forecast_payload)

    # The history builder attaches labels for later audit, but the scorer above
    # received only allow-listed checkpoint fields. Evaluation accesses those
    # joined labels only after the complete score ledger is generated and hashed.
    labels = [parse_bool(row.get("drafted")) for row in holdout_rows]
    if any(label is None for label in labels):
        raise DataError("Holdout outcomes are incomplete; class metrics cannot be revealed")
    binary_labels = [1 if label else 0 for label in labels]
    probabilities = [score_by_index[index].probability for index in range(len(holdout_rows))]
    baseline_probabilities = [replay.training_prevalence] * len(binary_labels)
    ranked_indices = sorted(
        range(len(holdout_rows)),
        key=lambda index: (-probabilities[index], index),
    )
    top_ten = ranked_indices[:10]
    source_sha256, source_artifacts = _source_digest(metadata)

    return {
        "artifact_kind": "retrospective_replay",
        "authenticity": (
            "Reconstructed after the outcome with the current pipeline; not a forecast "
            "saved before the draft. Holdout labels are joined during reconstruction, "
            "but the scorer receives only allow-listed checkpoint fields; evaluation "
            "reads those joined labels only after the complete score ledger is generated "
            "and hashed."
        ),
        "checkpoint": {
            "college_season": holdout_year - 1,
            "stored_as_of_week": args.week,
            "draft_year": holdout_year,
            "population": population,
        },
        "build_audit": {
            "rows": len(rows),
            "drafted_rows": sum(parse_bool(row.get("drafted")) is True for row in rows),
            "quality_gate_status": metadata.get("quality_gate_status"),
            "positive_match_coverage": metadata.get("positive_match_coverage"),
            "canonical_rows_sha256": _canonical_rows_sha256(rows),
            "row_hash_representation": (
                "sorted-key JSON of each model-relevant loaded row, in source order; "
                "manual trait grades and film provenance are excluded"
            ),
            "source_manifest_sha256": source_sha256,
            "source_artifacts": source_artifacts,
            "outcome_blind_alias_draft_years": sorted(blind_years),
        },
        "forecast_artifact": {
            "rows": len(holdout_rows),
            "sha256": forecast_sha256,
            "outcomes_in_payload": False,
        },
        "model_before_holdout": {
            "training_draft_years": list(replay.training_years),
            "training_rows": replay.training_rows,
            "training_positives": replay.training_positives,
            "training_prevalence": replay.training_prevalence,
            "calibration_draft_years": list(replay.calibration_years),
            "calibration_rows": replay.calibration_rows,
            "calibration_positives": replay.calibration_positives,
            "retained_features": list(replay.features),
            "prior_only_quality_gate_passed": model.validation.passes_quality_gate,
            "prior_only_validation": {
                "evaluated_years": list(model.validation.evaluated_years),
                "rows": model.validation.rows,
                "positives": model.validation.positives,
                "brier": model.validation.brier,
                "baseline_brier": model.validation.baseline_brier,
                "roc_auc": model.validation.roc_auc,
                "average_precision": model.validation.average_precision,
                "prevalence": model.validation.prevalence,
            },
        },
        "prospect": {
            "name": candidate.get("name"),
            "school": candidate.get("school"),
            "position": candidate.get("position"),
            "checkpoint_evidence": {
                key: candidate.get(key)
                for key in DEFAULT_CHECKPOINT_EVIDENCE_FIELDS
                if candidate.get(key) not in (None, "")
            },
            "raw_probability": candidate_score.raw_probability,
            "calibrated_probability": candidate_score.probability,
            "within_position_rank": candidate_score.within_position_rank,
            "within_position_rows": len(holdout_rows),
            "ranking_method": "competition rank; 1 + count of strictly higher scores",
            "feature_coverage": candidate_score.feature_coverage,
            "out_of_distribution_score": candidate_score.out_of_distribution_score,
            "out_of_distribution_warning": bool(
                candidate_score.out_of_distribution_score is not None
                and candidate_score.out_of_distribution_score > 3.0
            ),
            "drafted_profile_comparisons": [
                {
                    "name": comparison.name,
                    "draft_year": comparison.draft_year,
                    "draft_pick": comparison.draft_pick,
                    "similarity": comparison.similarity,
                    "features_compared": comparison.features_compared,
                    "feature_overlap": comparison.feature_overlap,
                }
                for comparison in drafted_comparisons
            ],
            "conditional_pick_band": (
                {
                    "comparison_count": len(drafted_comparisons),
                    "percentiles": [20, 50, 80],
                    "values": list(pick_band),
                }
                if pick_band is not None
                else None
            ),
        },
        "outcome_reveal": {
            "drafted": parse_bool(candidate.get("drafted")),
            "draft_round": _integer(candidate.get("draft_round")),
            "draft_pick": _integer(candidate.get("draft_ovr")),
        },
        "holdout_class_evaluation": {
            "rows": len(binary_labels),
            "positives": sum(binary_labels),
            "observed_prevalence": sum(binary_labels) / len(binary_labels),
            "brier": brier_score(binary_labels, probabilities),
            "baseline_brier": brier_score(binary_labels, baseline_probabilities),
            "log_loss": log_loss(binary_labels, probabilities),
            "roc_auc": roc_auc(binary_labels, probabilities),
            "average_precision": average_precision(binary_labels, probabilities),
            "top_10_drafted": sum(binary_labels[index] for index in top_ten),
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_case_result(args)
    except (DataError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

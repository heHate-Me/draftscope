from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from difflib import SequenceMatcher
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .mathstats import mean, median, metric_score, percentile_rank, quantile, robust_scale, weighted_mean
from .model import (
    CollegeDraftProbabilityModel,
    DraftPrediction,
    DraftProbabilityModel,
    ModelError,
)
from .records import (
    DataError,
    complete_film_grades_available,
    film_grades_displayable,
    load_records,
    normalize_name,
    parse_bool,
    parse_number,
)
from .schema import CATEGORY_WEIGHTS, MetricSpec, all_specs, normalize_position
from .teamfit import TeamFit, rank_team_fits


@dataclass(slots=True)
class BenchmarkMetric:
    metric: str
    label: str
    category: str
    value: float
    unit: str
    direction: str
    drafted_n: int
    drafted_mean: float | None
    drafted_median: float | None
    drafted_percentile: float | None
    favorable_score: float | None
    elite_n: int
    elite_mean: float | None
    elite_median: float | None
    reference: str
    elite_reference: str = "top-64 drafted players"
    elite_kind: str = "top64_fallback"


@dataclass(slots=True)
class CategoryScore:
    category: str
    score: float | None
    coverage: float
    observed_metrics: int
    expected_metrics: int
    reference: str


@dataclass(slots=True)
class Comparable:
    name: str
    position: str
    school: str
    draft_year: int | None
    draft_round: int | None
    draft_pick: int | None
    nfl_team: str
    similarity: float
    feature_overlap: float
    features_compared: int
    biggest_differences: list[str]
    reference: str
    nfl_hof: bool | None = None
    nfl_all_pro_selections: int | None = None
    nfl_pro_bowls: int | None = None
    measurement_source: str = ""


@dataclass(slots=True)
class Evaluation:
    player: dict[str, Any]
    as_of_date: str
    benchmark_window: tuple[int, int] | None
    profile_score: float | None
    profile_tier: str
    evidence_coverage: float
    categories: list[CategoryScore]
    benchmarks: list[BenchmarkMetric]
    draft_prediction: DraftPrediction
    projected_pick_range: tuple[float, float, float] | None
    physical_comps: list[Comparable]
    overall_comps: list[Comparable]
    pick_comps: list[Comparable]
    historical_elite_comps: list[Comparable]
    team_fits: list[TeamFit]
    benchmark_context: dict[str, Any]
    college_prediction: DraftPrediction | None = None
    combine_prediction: DraftPrediction | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class _ComparablePool:
    """Immutable-history index used by repeated weekly comparable searches."""

    source: Sequence[Mapping[str, Any]]
    rows_by_position: dict[str, tuple[Mapping[str, Any], ...]]
    cohort_reference: str
    explicit_cohort: bool
    metric_values: dict[tuple[str, str], tuple[float | None, ...]] = field(
        default_factory=dict
    )
    metric_scales: dict[tuple[str, str], float] = field(default_factory=dict)


@dataclass(slots=True)
class _BenchmarkCohort:
    source: Sequence[Mapping[str, Any]]
    rows: list[dict[str, Any]]
    reference: str
    explicit: bool


@dataclass(slots=True)
class _ActiveModelPool:
    source: Sequence[Mapping[str, Any]]
    rows_by_position: dict[str, tuple[dict[str, Any], ...]]
    metric_rows: dict[tuple[str, str], list[dict[str, Any]]] = field(
        default_factory=dict
    )


class ProspectEvaluator:
    def __init__(
        self,
        history: Sequence[Mapping[str, Any]],
        candidates: Sequence[Mapping[str, Any]],
        *,
        history_metadata: Mapping[str, Any] | None = None,
        model_history: Sequence[Mapping[str, Any]] | None = None,
        model_metadata: Mapping[str, Any] | None = None,
        career_elite_history: Sequence[Mapping[str, Any]] | None = None,
        career_elite_metadata: Mapping[str, Any] | None = None,
        team_profiles: Sequence[Mapping[str, Any]] | None = None,
        as_of_date: str | None = None,
    ):
        self.history = [dict(row) for row in history]
        raw_model_history = model_history if model_history is not None else history
        if model_history is None and not any(
            _truthy(row.get("reference_only")) for row in self.history
        ):
            self.model_history = self.history
        else:
            self.model_history = [
                dict(row)
                for row in raw_model_history
                if not _truthy(row.get("reference_only"))
            ]
        self.candidates = [dict(row) for row in candidates]
        self.history_metadata = dict(history_metadata or {})
        self.model_metadata = dict(model_metadata or history_metadata or {})
        catalog_metadata = self.history_metadata.get(
            "historical_career_elite_catalog"
        )
        catalog_metadata = (
            dict(catalog_metadata) if isinstance(catalog_metadata, Mapping) else {}
        )
        catalog_load_error = ""
        if career_elite_history is None:
            catalog_path = str(
                self.history_metadata.get("historical_career_elite_catalog_path") or ""
            ).strip()
            if catalog_path:
                try:
                    if not Path(catalog_path).is_file():
                        raise OSError(f"catalog file does not exist: {catalog_path}")
                    loaded_catalog = load_records(catalog_path)
                    if not loaded_catalog:
                        raise DataError("catalog contains no player rows")
                    expected_rows = _int_or_none(catalog_metadata.get("rows"))
                    if expected_rows is not None and len(loaded_catalog) != expected_rows:
                        raise DataError(
                            "catalog row count does not match metadata "
                            f"({len(loaded_catalog)} != {expected_rows})"
                        )
                    if not any(
                        _truthy(row.get("nfl_career_elite"))
                        for row in loaded_catalog
                    ):
                        raise DataError("catalog contains no career-elite rows")
                    career_elite_history = loaded_catalog
                except (DataError, OSError) as exc:
                    catalog_load_error = str(exc)
                    career_elite_history = []
            elif catalog_metadata:
                catalog_load_error = "catalog metadata exists but its data path is missing"
                career_elite_history = []
        raw_career_elite = (
            career_elite_history if career_elite_history is not None else self.history
        )
        self.career_elite_history = [
            dict(row)
            for row in raw_career_elite
            if _truthy(row.get("nfl_career_elite"))
        ]
        self.career_elite_metadata = dict(
            career_elite_metadata or catalog_metadata
        )
        if catalog_load_error:
            self.career_elite_metadata.update(
                {
                    "status": "unavailable",
                    "rows": 0,
                    "source": "career-elite catalog unavailable",
                    "scope_label": "career-elite catalog unavailable",
                    "coverage_note": catalog_load_error,
                    "all_era_hof_available": False,
                }
            )
        self.team_profiles = [dict(row) for row in (team_profiles or [])]
        self.as_of_date = as_of_date or date.today().isoformat()
        self._model_cache: dict[
            tuple[str, str, str, int | None], DraftProbabilityModel | ModelError
        ] = {}
        self._aligned_history_cache: dict[tuple[str, int | None], list[dict[str, Any]]] = {}
        self._comparable_pool_cache: dict[tuple[int, bool, bool], _ComparablePool] = {}
        self._benchmark_cohort_cache: dict[int, _BenchmarkCohort] = {}
        self._active_model_pool_cache: dict[int, _ActiveModelPool] = {}
        self._active_registry_cache: tuple[
            set[str], set[str], set[tuple[int | None, str]]
        ] | None = None
        self._contributor_registry_cache: tuple[
            set[str], set[str], set[tuple[int | None, str]]
        ] | None = None

    def find_candidate(self, name: str, *, school: str | None = None) -> dict[str, Any]:
        wanted = normalize_name(name)
        matches: list[tuple[float, dict[str, Any]]] = []
        for row in self.candidates:
            score = SequenceMatcher(None, wanted, normalize_name(row.get("name"))).ratio()
            if school and normalize_name(row.get("school")) == normalize_name(school):
                score += 0.20
            matches.append((score, row))
        if not matches:
            raise DataError("Candidate file contains no players")
        matches.sort(key=lambda item: item[0], reverse=True)
        if matches[0][0] < 0.55:
            raise DataError(f"No player matched {name!r}")
        if len(matches) > 1 and matches[0][0] - matches[1][0] < 0.03 and not school:
            choices = ", ".join(f"{row.get('name')} ({row.get('school')})" for _, row in matches[:5])
            raise DataError(f"Player name is ambiguous; specify school. Matches: {choices}")
        return dict(matches[0][1])

    def _align_rows(self, rows: Sequence[dict[str, Any]], candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
        current_week = parse_number(candidate.get("as_of_week"))
        checkpointed = any(parse_number(row.get("as_of_week")) is not None for row in rows)
        if not checkpointed:
            return list(rows)
        chosen: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        for row in rows:
            week = parse_number(row.get("as_of_week"))
            if week is None:
                continue
            if current_week is not None and week > current_week:
                continue
            identity = (str(row.get("player_id") or normalize_name(row.get("name"))), str(row.get("draft_year") or row.get("season") or ""))
            previous = chosen.get(identity)
            if previous is None or week > previous[0]:
                chosen[identity] = (week, row)
        return [row for _, row in chosen.values()]

    def _aligned_history(self, candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
        week_value = parse_number(candidate.get("as_of_week"))
        key = ("benchmark", int(week_value) if week_value is not None else None)
        if key not in self._aligned_history_cache:
            self._aligned_history_cache[key] = self._align_rows(self.history, candidate)
        return self._aligned_history_cache[key]

    def _aligned_model_history(self, candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
        week_value = parse_number(candidate.get("as_of_week"))
        key = ("model", int(week_value) if week_value is not None else None)
        if key not in self._aligned_history_cache:
            self._aligned_history_cache[key] = self._align_rows(self.model_history, candidate)
        return self._aligned_history_cache[key]

    def _benchmark_cohort_rows(
        self, history: Sequence[Mapping[str, Any]]
    ) -> tuple[list[dict[str, Any]], str, bool]:
        cached = self._benchmark_cohort_cache.get(id(history))
        if cached is not None and cached.source is history:
            return cached.rows, cached.reference, cached.explicit
        explicit = any("benchmark_cohort_eligible" in row for row in history)
        if not explicit:
            rows = [dict(row) for row in history]
            reference = "drafted players"
        else:
            rows = [
                dict(row)
                for row in history
                if _truthy(row.get("benchmark_cohort_eligible"))
            ]
            reference = str(
                self.history_metadata.get("benchmark_population")
                or "current-active players drafted in the benchmark window with combine measurements"
            )
        self._benchmark_cohort_cache[id(history)] = _BenchmarkCohort(
            source=history,
            rows=rows,
            reference=reference,
            explicit=explicit,
        )
        return rows, reference, explicit

    def _active_model_reference_rows(
        self,
        candidate: Mapping[str, Any],
        spec: MetricSpec,
    ) -> tuple[list[dict[str, Any]], str]:
        roster_season = self.history_metadata.get("active_roster_season") or "current"
        start = self.history_metadata.get("window_start") or "window start"
        end = self.history_metadata.get("window_end") or "window end"
        reference = (
            f"active on nflverse {roster_season} roster; drafted {start}-{end}; "
            "checkpoint-aligned college production available"
        )
        if spec.category != "production":
            return [], reference

        history = self._aligned_model_history(candidate)
        pool = self._active_model_pool(history)
        position = normalize_position(candidate.get("position"))
        metric_key = (position, spec.key)
        active_rows = pool.metric_rows.get(metric_key)
        if active_rows is None:
            active_rows = [
                row
                for row in pool.rows_by_position.get(position, ())
                if parse_number(row.get(spec.key)) is not None
            ]
            pool.metric_rows[metric_key] = active_rows
        return active_rows, reference

    def _active_registry_keys(
        self,
    ) -> tuple[set[str], set[str], set[tuple[int | None, str]]]:
        if self._active_registry_cache is not None:
            return self._active_registry_cache
        self._active_registry_cache = self._registry_keys("active_drafted_players")
        return self._active_registry_cache

    def _contributor_registry_keys(
        self,
    ) -> tuple[set[str], set[str], set[tuple[int | None, str]]]:
        if self._contributor_registry_cache is not None:
            return self._contributor_registry_cache
        self._contributor_registry_cache = self._registry_keys(
            "active_contributor_players"
        )
        return self._contributor_registry_cache

    def _registry_keys(
        self, metadata_field: str
    ) -> tuple[set[str], set[str], set[tuple[int | None, str]]]:
        registry = [
            row
            for row in self.history_metadata.get(metadata_field, [])
            if isinstance(row, Mapping)
        ]
        pfr_ids = {
            str(row.get("pfr_player_id") or "").strip()
            for row in registry
            if str(row.get("pfr_player_id") or "").strip()
        }
        cfb_ids = {
            str(row.get("cfb_player_id") or "").strip()
            for row in registry
            if str(row.get("cfb_player_id") or "").strip()
        }
        name_year_counts: dict[tuple[int | None, str], int] = {}
        for row in registry:
            key = (
                _int_or_none(row.get("draft_year")),
                normalize_name(row.get("name")),
            )
            if key[0] is not None and key[1]:
                name_year_counts[key] = name_year_counts.get(key, 0) + 1
        unique_name_year = {key for key, count in name_year_counts.items() if count == 1}
        return pfr_ids, cfb_ids, unique_name_year

    @staticmethod
    def _row_matches_registry(
        raw: Mapping[str, Any],
        keys: tuple[set[str], set[str], set[tuple[int | None, str]]],
    ) -> bool:
        pfr_ids, cfb_ids, unique_name_year = keys
        pfr_id = str(raw.get("outcome_nflverse_pfr_player_id") or "").strip()
        cfb_id = str(raw.get("outcome_nflverse_cfb_player_id") or "").strip()
        name_year = (
            _int_or_none(raw.get("draft_year") or raw.get("season")),
            normalize_name(raw.get("name")),
        )
        return bool(
            (pfr_id and pfr_id in pfr_ids)
            or (cfb_id and cfb_id in cfb_ids)
            or (not pfr_id and not cfb_id and name_year in unique_name_year)
        )

    def _active_model_pool(
        self, history: Sequence[Mapping[str, Any]]
    ) -> _ActiveModelPool:
        cached = self._active_model_pool_cache.get(id(history))
        if cached is not None and cached.source is history:
            return cached
        active_keys = self._active_registry_keys()
        contributor_keys = self._contributor_registry_keys()
        rows_by_position: dict[str, tuple[dict[str, Any], ...]] = {}
        if any(active_keys):
            drafted_pool = self._comparable_pool(
                history, use_benchmark_cohort=False
            )
            for position, drafted_rows in drafted_pool.rows_by_position.items():
                active_rows: list[dict[str, Any]] = []
                for raw in drafted_rows:
                    if not self._row_matches_registry(raw, active_keys):
                        continue
                    row = dict(raw)
                    if self._row_matches_registry(raw, contributor_keys):
                        row.update(
                            {
                                "nfl_three_year_outcome_known": True,
                                "nfl_three_year_contributor": True,
                                "benchmark_active_contributor_cohort_eligible": True,
                            }
                        )
                    active_rows.append(row)
                rows_by_position[position] = tuple(active_rows)
        pool = _ActiveModelPool(source=history, rows_by_position=rows_by_position)
        self._active_model_pool_cache[id(history)] = pool
        return pool

    def _reference_rows(self, candidate: Mapping[str, Any], spec: MetricSpec, history: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], str]:
        position = normalize_position(candidate.get("position"))
        benchmark_history, benchmark_reference, explicit_cohort = self._benchmark_cohort_rows(
            history
        )
        drafted = [
            row
            for row in benchmark_history
            if normalize_position(row.get("position")) == position
            and parse_bool(row.get("drafted")) is True
            and parse_number(row.get(spec.key)) is not None
            and (
                spec.category != "skills"
                or complete_film_grades_available(row)
            )
        ]
        if len(drafted) >= 5:
            return drafted, benchmark_reference
        if explicit_cohort:
            active_model_rows, active_model_reference = self._active_model_reference_rows(
                candidate, spec
            )
            if len(active_model_rows) >= 5:
                return active_model_rows, active_model_reference
            return [], (
                active_model_reference
                if spec.category == "production"
                else benchmark_reference
            )
        model_drafted = [
            row
            for row in self._aligned_model_history(candidate)
            if normalize_position(row.get("position")) == position
            and parse_bool(row.get("drafted")) is True
            and parse_number(row.get(spec.key)) is not None
            and (
                spec.category != "skills"
                or complete_film_grades_available(row)
            )
        ]
        if len(model_drafted) >= 5:
            return model_drafted, "drafted players (week-matched production cohort)"
        if spec.category == "production":
            current = [row for row in self.candidates if normalize_position(row.get("position")) == position and parse_number(row.get(spec.key)) is not None]
            if len(current) >= 5:
                return current, "current candidate pool"
        return [], "unavailable"

    def _contributor_outcome_supported(
        self, history: Sequence[Mapping[str, Any]]
    ) -> bool:
        outcome = self.history_metadata.get("nfl_three_year_contributor_outcome")
        if isinstance(outcome, Mapping):
            status = str(outcome.get("status") or "").lower()
            known = parse_number(outcome.get("known_outcomes"))
            if status not in {"", "unavailable"} and known is not None and known > 0:
                return True
        if any(
            _truthy(row.get("benchmark_active_contributor_cohort_eligible"))
            or (
                _truthy(row.get("active_three_year_contributor_eligible"))
                and parse_bool(row.get("nfl_three_year_outcome_known")) is True
                and parse_bool(row.get("nfl_three_year_contributor")) is True
            )
            for row in history
        ):
            return True
        return bool(self.history_metadata.get("active_contributor_players"))

    def _performance_reference_rows(
        self,
        reference_rows: Sequence[Mapping[str, Any]],
        history: Sequence[Mapping[str, Any]],
    ) -> tuple[list[Mapping[str, Any]], str, str]:
        contributor_rows = [
            row
            for row in reference_rows
            if _truthy(row.get("benchmark_active_contributor_cohort_eligible"))
            or (
                _truthy(row.get("active_three_year_contributor_eligible"))
                and parse_bool(row.get("nfl_three_year_outcome_known")) is True
                and parse_bool(row.get("nfl_three_year_contributor")) is True
            )
        ]
        if self._contributor_outcome_supported(history) and contributor_rows:
            return (
                contributor_rows,
                "established active NFL contributors (fixed first-three-season snap outcome)",
                "active_contributor",
            )

        top64_rows = [
            row
            for row in reference_rows
            if parse_bool(row.get("drafted")) is True
            and (
                parse_bool(row.get("elite")) is True
                or (
                    parse_number(row.get("draft_ovr")) is not None
                    and parse_number(row.get("draft_ovr")) <= 64
                )
            )
        ]
        active_label = (
            "active top-64 picks"
            if any("benchmark_cohort_eligible" in row for row in history)
            else "top-64 drafted players"
        )
        reason = (
            "no established contributor at this position has this metric"
            if self._contributor_outcome_supported(history)
            else "fixed three-year NFL outcomes unavailable"
        )
        return top64_rows, f"{active_label} (fallback: {reason})", "top64_fallback"

    def _benchmark_metrics(self, candidate: Mapping[str, Any], history: Sequence[Mapping[str, Any]]) -> list[BenchmarkMetric]:
        position = normalize_position(candidate.get("position"))
        results: list[BenchmarkMetric] = []
        for spec in all_specs(position):
            value = parse_number(candidate.get(spec.key))
            if value is None:
                continue
            reference_rows, reference = self._reference_rows(candidate, spec, history)
            if len(reference_rows) < 5:
                continue
            values = [parse_number(row.get(spec.key)) for row in reference_rows]
            elite_rows, elite_reference, elite_kind = self._performance_reference_rows(
                reference_rows, history
            )
            elite_values = [parse_number(row.get(spec.key)) for row in elite_rows]
            target = median(elite_values) if len([x for x in elite_values if x is not None]) >= 5 else median(values)
            results.append(
                BenchmarkMetric(
                    metric=spec.key,
                    label=spec.label,
                    category=spec.category,
                    value=value,
                    unit=spec.unit,
                    direction=spec.direction,
                    drafted_n=len([x for x in values if x is not None]),
                    drafted_mean=mean(values),
                    drafted_median=median(values),
                    drafted_percentile=percentile_rank(value, values),
                    favorable_score=metric_score(value, values, spec.direction, target=target),
                    elite_n=len([x for x in elite_values if x is not None]),
                    elite_mean=mean(elite_values),
                    elite_median=median(elite_values),
                    reference=reference,
                    elite_reference=elite_reference,
                    elite_kind=elite_kind,
                )
            )
        return results

    def _category_scores(self, candidate: Mapping[str, Any], benchmarks: Sequence[BenchmarkMetric]) -> list[CategoryScore]:
        position = normalize_position(candidate.get("position"))
        specs = all_specs(position)
        results: list[CategoryScore] = []
        by_metric = {item.metric: item for item in benchmarks}
        for category in ("physical", "production", "skills", "age"):
            category_specs = [spec for spec in specs if spec.category == category]
            total_weight = sum(spec.weight for spec in category_specs)
            observed_weight = 0.0
            score_pairs: list[tuple[float | None, float]] = []
            references: set[str] = set()
            for spec in category_specs:
                value = parse_number(candidate.get(spec.key))
                if value is None:
                    continue
                observed_weight += spec.weight
                benchmark = by_metric.get(spec.key)
                if benchmark:
                    score_pairs.append((benchmark.favorable_score, spec.weight))
                    references.add(benchmark.reference)
                elif category == "skills":
                    score_pairs.append((max(0.0, min(100.0, value)), spec.weight))
                    references.add("manual 0–100 film rubric")
            score = weighted_mean(score_pairs)
            coverage = observed_weight / total_weight if total_weight else 0.0
            results.append(
                CategoryScore(
                    category=category,
                    score=score,
                    coverage=coverage,
                    observed_metrics=sum(parse_number(candidate.get(spec.key)) is not None for spec in category_specs),
                    expected_metrics=len(category_specs),
                    reference=", ".join(sorted(references)) if references else "unavailable",
                )
            )
        return results

    def _profile_score(
        self,
        position: str,
        categories: Sequence[CategoryScore],
        *,
        include_film: bool,
    ) -> tuple[float | None, float]:
        weights = CATEGORY_WEIGHTS.get(position, CATEGORY_WEIGHTS["WR"])
        by_category = {item.category: item for item in categories}
        pairs: list[tuple[float | None, float]] = []
        evidence = 0.0
        for category, weight in weights.items():
            if category == "skills" and not include_film:
                continue
            item = by_category.get(category)
            if item and item.score is not None:
                pairs.append((item.score, weight))
                evidence += weight * item.coverage
        raw = weighted_mean(pairs)
        if raw is None:
            return None, evidence
        shrinkage = min(1.0, evidence / 0.75)
        return 50.0 + (raw - 50.0) * shrinkage, evidence

    def _comparables(
        self,
        candidate: Mapping[str, Any],
        history: Sequence[Mapping[str, Any]],
        *,
        categories: set[str],
        limit: int = 5,
        minimum_features: int = 3,
        minimum_overlap: float = 0.35,
        use_benchmark_cohort: bool = True,
        require_drafted: bool = True,
        require_career_elite: bool = False,
        reference: str | None = None,
    ) -> list[Comparable]:
        position = normalize_position(candidate.get("position"))
        specs = [spec for spec in all_specs(position) if spec.category in categories]
        pool = self._comparable_pool(
            history,
            use_benchmark_cohort=use_benchmark_cohort,
            require_drafted=require_drafted,
        )
        if use_benchmark_cohort:
            reference = reference or pool.cohort_reference
        reference = reference or (
            "full drafted history"
            if not pool.explicit_cohort
            else "current-active drafted benchmark cohort"
        )
        drafted = pool.rows_by_position.get(position, ())
        minimum_history = max(5, math.ceil(len(drafted) * 0.20))
        candidate_values = {
            spec.key: parse_number(candidate.get(spec.key)) for spec in specs
        }
        available_specs = [
            spec
            for spec in specs
            if candidate_values[spec.key] is not None
            and sum(
                value is not None
                for value in self._comparable_metric_values(pool, position, spec.key)
            )
            >= minimum_history
        ]
        if len(available_specs) < minimum_features:
            return []
        distributions = {
            spec.key: self._comparable_metric_values(pool, position, spec.key)
            for spec in available_specs
        }
        scales = {
            spec.key: self._comparable_metric_scale(pool, position, spec.key)
            for spec in available_specs
        }
        ranked: list[
            tuple[float, float, Mapping[str, Any], list[str], int]
        ] = []
        for row_index, row in enumerate(drafted):
            if require_career_elite and not _truthy(row.get("nfl_career_elite")):
                continue
            if _same_player_identity(candidate, row):
                continue
            squared = 0.0
            weight_sum = 0.0
            differences: list[tuple[float, str]] = []
            compared = 0
            for spec in available_specs:
                left = candidate_values[spec.key]
                right = distributions[spec.key][row_index]
                if left is None or right is None:
                    continue
                delta = (left - right) / scales[spec.key]
                squared += spec.weight * delta * delta
                weight_sum += spec.weight
                compared += 1
                differences.append((abs(delta), f"{spec.label}: {left:g} vs {right:g}"))
            if compared < minimum_features or weight_sum <= 0:
                continue
            overlap = compared / len(available_specs)
            if overlap < minimum_overlap:
                continue
            distance = math.sqrt(squared / weight_sum) + (1.0 - overlap) * 0.80
            similarity = 100.0 * math.exp(-0.45 * distance)
            differences.sort(reverse=True)
            ranked.append((similarity, overlap, row, [text for _, text in differences[:3]], compared))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        output: list[Comparable] = []
        for similarity, overlap, row, differences, compared in ranked[:limit]:
            output.append(
                Comparable(
                    name=str(row.get("name") or ""),
                    position=position,
                    school=str(row.get("school") or ""),
                    draft_year=_int_or_none(row.get("draft_year") or row.get("season")),
                    draft_round=_int_or_none(row.get("draft_round")),
                    draft_pick=_int_or_none(row.get("draft_ovr")),
                    nfl_team=str(row.get("nfl_team") or ""),
                    similarity=similarity,
                    feature_overlap=overlap,
                    features_compared=compared,
                    biggest_differences=differences,
                    reference=reference,
                    nfl_hof=parse_bool(row.get("nfl_hof")),
                    nfl_all_pro_selections=_int_or_none(
                        row.get("nfl_all_pro_selections")
                    ),
                    nfl_pro_bowls=_int_or_none(row.get("nfl_pro_bowls")),
                    measurement_source=str(row.get("measurement_source") or ""),
                )
            )
        return output

    def _comparable_pool(
        self,
        history: Sequence[Mapping[str, Any]],
        *,
        use_benchmark_cohort: bool,
        require_drafted: bool = True,
    ) -> _ComparablePool:
        """Index one aligned history snapshot once instead of once per prospect."""

        cache_key = (id(history), use_benchmark_cohort, require_drafted)
        cached = self._comparable_pool_cache.get(cache_key)
        if cached is not None and cached.source is history:
            return cached

        if use_benchmark_cohort:
            comparison_history, cohort_reference, explicit_cohort = (
                self._benchmark_cohort_rows(history)
            )
        else:
            comparison_history = history
            cohort_reference = "full drafted history"
            explicit_cohort = False

        rows_by_position: dict[str, list[Mapping[str, Any]]] = {}
        for row in comparison_history:
            if require_drafted and parse_bool(row.get("drafted")) is not True:
                continue
            raw_positions = row.get("comparison_positions")
            if isinstance(raw_positions, (list, tuple, set)):
                values = [str(value) for value in raw_positions]
            else:
                values = str(raw_positions or "").replace(",", ";").split(";")
            positions = [normalize_position(row.get("position"))]
            positions.extend(normalize_position(value) for value in values if value)
            for row_position in dict.fromkeys(positions):
                if row_position:
                    rows_by_position.setdefault(row_position, []).append(row)

        pool = _ComparablePool(
            source=history,
            rows_by_position={
                row_position: tuple(rows)
                for row_position, rows in rows_by_position.items()
            },
            cohort_reference=cohort_reference,
            explicit_cohort=explicit_cohort,
        )
        self._comparable_pool_cache[cache_key] = pool
        return pool

    @staticmethod
    def _comparable_metric_values(
        pool: _ComparablePool,
        position: str,
        metric: str,
    ) -> tuple[float | None, ...]:
        key = (position, metric)
        values = pool.metric_values.get(key)
        if values is None:
            values = tuple(
                (
                    parse_number(row.get(metric))
                    if not metric.startswith("trait_")
                    or complete_film_grades_available(row)
                    else None
                )
                for row in pool.rows_by_position.get(position, ())
            )
            pool.metric_values[key] = values
        return values

    def _comparable_metric_scale(
        self,
        pool: _ComparablePool,
        position: str,
        metric: str,
    ) -> float:
        key = (position, metric)
        scale = pool.metric_scales.get(key)
        if scale is None:
            scale = robust_scale(
                self._comparable_metric_values(pool, position, metric)
            )
            pool.metric_scales[key] = scale
        return scale

    def _pick_range(self, comps: Sequence[Comparable]) -> tuple[float, float, float] | None:
        picks = [float(comp.draft_pick) for comp in comps if comp.draft_pick is not None]
        if len(picks) < 3:
            return None
        low, middle, high = quantile(picks, 0.20), quantile(picks, 0.50), quantile(picks, 0.80)
        if low is None or middle is None or high is None:
            return None
        return low, middle, high

    def _benchmark_context(self, history: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        cohort_rows, fallback_population, explicit = self._benchmark_cohort_rows(history)
        elite_rows, elite_reference, elite_kind = self._performance_reference_rows(
            cohort_rows, history
        )
        metadata = self.history_metadata
        outcome = metadata.get("nfl_three_year_contributor_outcome")
        outcome = dict(outcome) if isinstance(outcome, Mapping) else {}
        top64_rows = [
            row
            for row in cohort_rows
            if parse_bool(row.get("drafted")) is True
            and (
                parse_bool(row.get("elite")) is True
                or (
                    parse_number(row.get("draft_ovr")) is not None
                    and parse_number(row.get("draft_ovr")) <= 64
                )
            )
        ]
        historical_career_elite_rows = self.career_elite_history
        career_elite_metadata = self.career_elite_metadata
        catalog_row_count = career_elite_metadata.get("rows")
        if catalog_row_count is None:
            catalog_row_count = metadata.get("historical_career_elite_rows")
        if catalog_row_count is None:
            catalog_row_count = len(historical_career_elite_rows)
        return {
            "population": str(metadata.get("benchmark_population") or fallback_population),
            "source": str(metadata.get("benchmark_source") or metadata.get("source") or "provided history"),
            "explicit_active_cohort": explicit,
            "rows": int(metadata.get("benchmark_rows") or len(cohort_rows)),
            "elite_rows": len(elite_rows),
            "elite_kind": elite_kind,
            "elite_reference": elite_reference,
            "top64_rows": int(metadata.get("benchmark_elite_rows") or len(top64_rows)),
            "historical_career_elite_rows": int(
                catalog_row_count
            ),
            "historical_career_elite_definition": str(
                career_elite_metadata.get("historical_career_elite_definition")
                or metadata.get("historical_career_elite_definition")
                or "Hall of Fame OR >=1 first-team All-Pro OR >=3 Pro Bowls"
            ),
            "historical_career_elite_scope": str(
                career_elite_metadata.get("scope_label")
                or "provided career-elite history"
            ),
            "historical_career_elite_coverage_note": str(
                career_elite_metadata.get("coverage_note") or ""
            ),
            "historical_career_elite_source": str(
                career_elite_metadata.get("source") or "provided history"
            ),
            "historical_career_elite_comparison_only": True,
            "historical_career_elite_model_feature_eligible": False,
            "historical_career_elite_hof_all_era_available": bool(
                career_elite_metadata.get("all_era_hof_available")
            ),
            "historical_career_elite_all_pro_history_available": bool(
                career_elite_metadata.get("historical_all_pro_available")
            ),
            "historical_career_elite_pro_bowl_history_available": bool(
                career_elite_metadata.get("historical_pro_bowl_available")
            ),
            "historical_career_elite_full_history_criteria_available": bool(
                career_elite_metadata.get("full_history_criteria_available")
            ),
            "historical_career_elite_full_history_sources_available": bool(
                career_elite_metadata.get("full_history_sources_available")
            ),
            "historical_career_elite_source_population_complete": bool(
                career_elite_metadata.get("source_population_complete")
            ),
            "historical_career_elite_full_position_coverage_complete": bool(
                career_elite_metadata.get(
                    "full_history_position_coverage_complete"
                )
            ),
            "historical_career_elite_unpositioned_qualifier_rows": int(
                career_elite_metadata.get("unpositioned_qualifier_rows") or 0
            ),
            "contributor_rows": int(
                metadata.get("benchmark_active_contributor_rows")
                or sum(
                    _truthy(row.get("benchmark_active_contributor_cohort_eligible"))
                    for row in cohort_rows
                )
            ),
            "contributor_outcome_supported": self._contributor_outcome_supported(history),
            "contributor_outcome": outcome,
            "active_drafted_window_rows": _int_or_none(
                metadata.get("active_drafted_window_rows")
            ),
            "coverage": parse_number(metadata.get("benchmark_cohort_coverage")),
            "active_roster_season": _int_or_none(metadata.get("active_roster_season")),
            "active_roster_snapshot_week": _int_or_none(
                metadata.get("active_roster_snapshot_week")
            ),
            "source_as_of": metadata.get("active_roster_source_as_of"),
            "source_as_of_basis": metadata.get("active_roster_source_as_of_basis"),
            "included_statuses": list(metadata.get("active_roster_included_statuses") or []),
            "status_counts": dict(metadata.get("active_roster_status_counts") or {}),
            "draft_join_counts": dict(
                metadata.get("draft_window_roster_match_counts") or {}
            ),
            "draft_join_conflicts": int(
                metadata.get("draft_window_roster_join_conflicts") or 0
            ),
        }

    def _team_access_pick_range(
        self,
        prediction: DraftPrediction,
        comps: Sequence[Comparable],
        pick_range: tuple[float, float, float] | None,
    ) -> tuple[tuple[float, float, float] | None, list[str]]:
        """Only expose a comp-derived pick band to team access scoring when supported."""
        reasons: list[str] = []
        if pick_range is None:
            reasons.append("no comparable-player pick band")
        validation = prediction.validation
        if not prediction.available or validation is None or not validation.passes_quality_gate:
            reasons.append("the position probability model did not pass its quality gate")
        is_college_stage = prediction.model_stage == "college_precombine"
        if (
            not is_college_stage
            and (prediction.conditional_probability is None or prediction.conditional_probability < 0.35)
        ):
            reasons.append("conditional draft probability is below the draft-access evidence threshold")
        minimum_coverage = 0.30 if is_college_stage else 0.60
        if prediction.feature_coverage < minimum_coverage:
            reasons.append(f"model feature coverage is below {minimum_coverage:.0%}")
        if prediction.out_of_distribution_score is not None and prediction.out_of_distribution_score > 3.0:
            reasons.append("the player is outside the typical model feature range")
        picked_comps = [comp for comp in comps if comp.draft_pick is not None]
        if len(picked_comps) < 3:
            reasons.append("fewer than three drafted comparables have pick data")
        elif sum(comp.feature_overlap for comp in picked_comps) / len(picked_comps) < 0.60:
            reasons.append("comparable feature overlap is below 60%")
        if prediction.entry_probability == 0.0:
            reasons.append("the player is known to be ineligible for the next draft")
        return (pick_range if not reasons else None), reasons

    def _probability_model(
        self,
        candidate: Mapping[str, Any],
        history: Sequence[Mapping[str, Any]],
        metadata: Mapping[str, Any],
    ) -> DraftProbabilityModel:
        position = normalize_position(candidate.get("position"))
        week_value = parse_number(candidate.get("as_of_week"))
        week = (
            int(week_value)
            if week_value is not None
            and any(parse_number(row.get("as_of_week")) is not None for row in history)
            else None
        )
        stage = _model_stage(metadata)
        probability_kind = str(metadata.get("probability_kind") or "unknown_risk_set")
        populations = {str(row.get("population")) for row in history if row.get("population")}
        population = metadata.get("population") or (
            next(iter(populations)) if len(populations) == 1 else "provided historical risk set"
        )
        condition = str(
            metadata.get("probability_condition")
            or f"P(drafted | {population})"
        )
        cache_key = (
            position,
            stage,
            f"{probability_kind}|{population}|{condition}",
            week,
        )
        cached = self._model_cache.get(cache_key)
        if isinstance(cached, DraftProbabilityModel):
            return cached
        if isinstance(cached, ModelError):
            raise cached
        try:
            if stage == "college_precombine":
                if probability_kind != "unconditional_next_draft":
                    raise ModelError(
                        "college_precombine metadata must use probability_kind=unconditional_next_draft"
                    )
                model = CollegeDraftProbabilityModel(
                    history,
                    position=position,
                    population=str(population),
                )
            else:
                model = DraftProbabilityModel(
                    history,
                    position=position,
                    candidate_features=(
                        spec.key
                        for spec in all_specs(position)
                        if spec.category != "skills"
                    ),
                    population=str(population),
                    model_stage=stage,
                    probability_kind=probability_kind,
                    probability_condition=condition,
                )
        except ModelError as exc:
            self._model_cache[cache_key] = exc
            raise
        self._model_cache[cache_key] = model
        return model

    def _stage_prediction(
        self,
        candidate: Mapping[str, Any],
        history: Sequence[Mapping[str, Any]],
        metadata: Mapping[str, Any],
        *,
        bootstrap: int,
    ) -> DraftPrediction:
        stage = _model_stage(metadata)
        kind = str(metadata.get("probability_kind") or "unknown_risk_set")
        try:
            model = self._probability_model(candidate, history, metadata)
            prediction = model.predict(candidate, bootstrap=bootstrap)
        except ModelError as exc:
            prediction = DraftPrediction(
                available=False,
                model_stage=stage,
                probability_kind=kind,
                label="Draft probability unavailable",
                population=str(metadata.get("population") or "provided history"),
                warnings=[str(exc)],
            )
        _scope_draft_prediction(candidate, prediction, metadata)
        return prediction

    def evaluate(
        self,
        name: str,
        *,
        school: str | None = None,
        bootstrap: int = 30,
        team_fit_limit: int = 8,
        reference_only: bool = False,
    ) -> Evaluation:
        candidate = self.find_candidate(name, school=school)
        reference_only = reference_only or _truthy(candidate.get("reference_only"))
        position = normalize_position(candidate.get("position"))
        candidate["position"] = position
        history = self._aligned_history(candidate)
        model_history = history if reference_only else self._aligned_model_history(candidate)
        film_displayable = film_grades_displayable(candidate)
        published_candidate = dict(candidate)
        if not film_displayable:
            for key in tuple(published_candidate):
                if key.startswith("trait_"):
                    published_candidate[key] = None
        benchmarks = self._benchmark_metrics(published_candidate, history)
        categories = self._category_scores(published_candidate, benchmarks)
        complete_film = complete_film_grades_available(candidate)
        profile_score, evidence = self._profile_score(
            position,
            categories,
            include_film=complete_film,
        )
        profile_categories = {"physical", "production", "age"}
        if complete_film:
            profile_categories.add("skills")
        physical_comps = self._comparables(
            published_candidate,
            history,
            categories={"physical"},
            limit=8,
            minimum_features=2,
            use_benchmark_cohort=True,
        )
        overall_comps = self._comparables(
            published_candidate,
            history,
            categories=profile_categories,
            limit=8,
            use_benchmark_cohort=True,
        )
        historical_elite_comps = self._comparables(
            published_candidate,
            self.career_elite_history,
            categories={"physical"},
            limit=8,
            minimum_features=2,
            minimum_overlap=0.20,
            use_benchmark_cohort=False,
            require_drafted=False,
            require_career_elite=True,
            reference=(
                "historical career-elite NFL players across the separately scoped "
                f"catalog ({self.career_elite_metadata.get('scope_label') or 'provided scope'}; "
                "Hall of Fame OR >=1 first-team All-Pro OR >=3 Pro Bowls); "
                "comparison-only outcomes are excluded from models"
            ),
        )
        pick_source = model_history if self.model_history is not self.history else history
        pick_comps = self._comparables(
            published_candidate,
            pick_source,
            categories={"physical", "production", "age"},
            use_benchmark_cohort=False,
            reference="full drafted history for pick-band support",
        )
        projection_physical_comps = [
            comp for comp in physical_comps if comp.features_compared >= 3
        ]
        projection_comps = pick_comps or projection_physical_comps
        pick_range = self._pick_range(projection_comps)
        warnings: list[str] = []
        fallback_reason = str(
            self.model_metadata.get("model_history_fallback_reason") or ""
        ).strip()
        if fallback_reason:
            warnings.append(fallback_reason)
        if parse_bool(candidate.get("data_stale")) is True:
            warnings.append(
                "This player's latest refresh failed; the report uses the older checkpoint shown above."
            )
        model_stage = _model_stage(self.model_metadata)
        college_prediction: DraftPrediction | None = None
        combine_prediction: DraftPrediction | None = None
        if reference_only:
            prediction = DraftPrediction(
                available=False,
                model_stage="reference_only",
                probability_kind="not_applicable",
                probability_scope="not applicable; draft outcome already known",
                conditional_scope="not applicable; draft outcome already known",
                label="Draft outcome already known — reference comparison only",
                population="completed NFL draft reference players",
                warnings=[
                    "Probability fitting and team-fit ranking are skipped for already-drafted reference players."
                ],
            )
            warnings.append(
                "This is a reference-only scouting comparison; the player's actual draft result is already known."
            )
        else:
            if model_stage == "college_precombine":
                college_prediction = self._stage_prediction(
                    candidate,
                    model_history,
                    self.model_metadata,
                    bootstrap=bootstrap,
                )
                prediction = college_prediction
                benchmark_stage = _model_stage(self.history_metadata)
                if benchmark_stage != "college_precombine":
                    combine_prediction = self._stage_prediction(
                        candidate,
                        history,
                        self.history_metadata,
                        bootstrap=bootstrap,
                    )
            else:
                prediction = self._stage_prediction(
                    candidate,
                    model_history,
                    self.model_metadata,
                    bootstrap=bootstrap,
                )
                if "combine" in model_stage:
                    combine_prediction = prediction
            population = str(self.model_metadata.get("population") or "").lower()
            source = str(candidate.get("measurement_source") or "unknown").lower()
            if model_stage != "college_precombine" and "combine" in population and source not in {"nfl_combine", "combine"}:
                warnings.append(
                    "The probability model is conditional on combine participants and uses combine-protocol history; "
                    f"this player's {source} measurements are a preseason proxy, not directly equivalent."
                )
        if any(item.reference == "current candidate pool" for item in benchmarks):
            warnings.append("Some production percentiles use the current candidate file because drafted-player production history was not supplied.")
        if not reference_only and parse_number(candidate.get("as_of_week")) is not None:
            model_has_production = any(feature.startswith("prod_") for feature in prediction.features_used)
            if not model_has_production:
                if model_stage == "college_precombine":
                    warnings.append(
                        "This position's college model did not retain a public production feature after held-out feature checks; "
                        "weekly box-score changes do not alter its published probability."
                    )
                else:
                    warnings.append(
                        "Weekly production moves the board profile, but the combine-stage probability cannot learn from it."
                    )
        if evidence < 0.50:
            warnings.append(
                "The comprehensive scouting profile is incomplete because public feeds do not supply standardized film grades "
                "or advanced charting; the profile score is shrunk, while the draft model uses a separate stage-specific evidence gate."
            )
        if not reference_only:
            for limitation in self.model_metadata.get("limitations", []):
                warnings.append(str(limitation))
        has_film_grades = any(
            key.startswith("trait_") and parse_number(value) is not None
            for key, value in candidate.items()
        )
        film_status = str(candidate.get("film_grade_status") or "").strip().lower()
        if has_film_grades and complete_film:
            warnings.append(
                "Complete manual film grades use the documented rubric and may affect only scouting-profile, comparison, and team-fit context."
            )
        elif has_film_grades and film_status == "provisional" and film_displayable:
            warnings.append(
                "PROVISIONAL FILM GRADES — displayed for scouting context only; excluded from the overall profile score, comparisons, and team fit."
            )
        elif has_film_grades:
            warnings.append(
                "Manual film grades are not backed by a complete audited sample and are excluded from the overall profile score, comparisons, and team fit."
            )
        prediction.warnings.extend(warning for warning in warnings if warning not in prediction.warnings)
        comp_source = projection_comps
        if reference_only:
            access_gate_reasons: list[str] = []
            team_fits: list[TeamFit] = []
            pick_range = None
        else:
            access_pick_range, access_gate_reasons = self._team_access_pick_range(
                prediction, comp_source, pick_range
            )
            team_fits = (
                rank_team_fits(
                    candidate,
                    self.team_profiles,
                    pick_range=access_pick_range,
                    limit=team_fit_limit,
                )
                if self.team_profiles
                else []
            )
        has_access_profiles = any(
            parse_number(row.get("draft_access_min")) is not None
            or parse_number(row.get("draft_access_max")) is not None
            for row in self.team_profiles
        )
        if team_fits and has_access_profiles and access_gate_reasons:
            warnings.append(
                "Team draft-access scoring was withheld because "
                + "; ".join(access_gate_reasons)
                + ". Need, scheme, and timeline fit remain available."
            )
        valid_years = sorted(
            year for year in {_int_or_none(row.get("draft_year") or row.get("season")) for row in history} if year is not None
        )
        window = (valid_years[0], valid_years[-1]) if valid_years else None
        return Evaluation(
            player=published_candidate,
            as_of_date=self.as_of_date,
            benchmark_window=window,
            profile_score=profile_score,
            profile_tier=_profile_tier(profile_score),
            evidence_coverage=evidence,
            categories=categories,
            benchmarks=benchmarks,
            draft_prediction=prediction,
            projected_pick_range=pick_range,
            physical_comps=physical_comps,
            overall_comps=overall_comps,
            pick_comps=pick_comps,
            historical_elite_comps=historical_elite_comps,
            team_fits=team_fits,
            benchmark_context=self._benchmark_context(history),
            college_prediction=college_prediction,
            combine_prediction=combine_prediction,
            warnings=warnings,
        )

    def rank(self, *, bootstrap: int = 0) -> list[Evaluation]:
        evaluations: list[tuple[Evaluation, float | None]] = []
        for candidate in self.candidates:
            try:
                if parse_bool(candidate.get("data_stale")) is True:
                    continue
            except DataError:
                continue
            try:
                evaluation = self.evaluate(
                    str(candidate.get("name") or ""),
                    school=str(candidate.get("school") or ""),
                    bootstrap=bootstrap,
                )
                evaluation_player = getattr(evaluation, "player", candidate)
                evaluation_categories = getattr(evaluation, "categories", ())
                non_film_profile, _coverage = self._profile_score(
                    normalize_position(evaluation_player.get("position")),
                    evaluation_categories,
                    include_film=False,
                )
                evaluations.append((evaluation, non_film_profile))
            except (DataError, ModelError):
                continue
        ranked = sorted(
            evaluations,
            key=lambda pair: (
                _ranking_likelihood(pair[0].draft_prediction) is not None,
                _ranking_likelihood(pair[0].draft_prediction)
                if _ranking_likelihood(pair[0].draft_prediction) is not None
                else -1.0,
                pair[1] is not None,
                pair[1] if pair[1] is not None else -1.0,
            ),
            reverse=True,
        )
        return [evaluation for evaluation, _non_film_profile in ranked]


def _candidate_model_feature_observed(candidate: Mapping[str, Any], feature: str) -> bool:
    """Mirror stage feature derivation when reporting candidate evidence."""

    if parse_number(candidate.get(feature)) is not None:
        return True
    if feature == "class_year_numeric":
        return candidate.get("class_year") not in (None, "")
    if feature == "context_school_draft_rate":
        return candidate.get("school") not in (None, "")
    if feature == "context_conference_draft_rate":
        return candidate.get("conference") not in (None, "")
    if feature == "bmi":
        height = parse_number(candidate.get("height_in"))
        return bool(height and parse_number(candidate.get("weight_lb")) is not None)
    if feature == "has_recorded_stats":
        return any(
            key.startswith("prod_") and parse_number(value) is not None
            for key, value in candidate.items()
        )
    return False


def _scope_draft_prediction(
    candidate: Mapping[str, Any],
    prediction: DraftPrediction,
    model_metadata: Mapping[str, Any],
) -> None:
    """Respect the historical risk-set contract before publishing next-draft odds."""
    if prediction.features_used:
        prediction.feature_coverage = sum(
            _candidate_model_feature_observed(candidate, feature)
            for feature in prediction.features_used
        ) / len(prediction.features_used)
    entry_probability, entry_source, entry_warnings = _resolve_entry_probability(candidate)
    prediction.entry_probability = entry_probability
    prediction.warnings.extend(warning for warning in entry_warnings if warning not in prediction.warnings)
    conditional = prediction.conditional_probability
    if conditional is None and prediction.available:
        conditional = prediction.probability
    prediction.conditional_probability = conditional
    probability_kind = str(model_metadata.get("probability_kind") or "unknown_risk_set")
    probability_condition = str(
        model_metadata.get("probability_condition")
        or f"P(drafted | {prediction.population or 'provided historical risk set'})"
    )
    prediction.conditional_scope = probability_condition
    if not prediction.available or conditional is None:
        prediction.probability = None
        prediction.probability_scope = "unavailable"
        return

    # Known non-entry makes next-draft selection impossible regardless of the
    # model population. Other risk-set conversions must remain explicit.
    if entry_probability == 0.0:
        prediction.probability = 0.0
        prediction.probability_scope = "P(drafted next draft) = 0 because next-draft entry is 0%"
        prediction.interval_80 = (0.0, 0.0) if prediction.interval_80 is not None else None
        prediction.projected_pick = None
        return

    if probability_kind == "unconditional_next_draft":
        prediction.probability = conditional
        prediction.probability_scope = "P(drafted next draft) from an unconditional historical risk set"
        return

    prediction.probability = None
    prediction.projected_pick = None
    if probability_kind != "conditional_on_entry":
        prediction.probability_scope = probability_condition
        prediction.warnings.append(
            "Unconditional next-draft likelihood is withheld because this model is conditioned on "
            "a narrower or unspecified historical risk set. Entry probability alone cannot remove that selection condition."
        )
        return

    if entry_probability is None:
        prediction.probability = None
        prediction.probability_scope = (
            "P(drafted | enters next NFL draft); next-draft entry probability unknown"
        )
        prediction.warnings.append(
            "Unconditional next-draft likelihood is withheld because draft entry probability is unknown or inconsistent; "
            "draft_declared=false, when supplied, records current status rather than a future opt-out."
        )
        return

    prediction.probability = conditional * entry_probability
    prediction.projected_pick = (
        bool(prediction.threshold is not None and prediction.probability >= prediction.threshold)
        if entry_probability == 1.0
        else None
    )
    prediction.probability_scope = (
        f"P(drafted next draft), combining P(drafted | enters) with {entry_source}"
    )
    if prediction.interval_80 is not None:
        prediction.interval_80 = (
            prediction.interval_80[0] * entry_probability,
            prediction.interval_80[1] * entry_probability,
        )
    if entry_probability < 1.0:
        prediction.warnings.append(
            "The next-draft likelihood holds the supplied entry probability fixed; its interval does not include entry-estimate uncertainty."
        )


def _resolve_entry_probability(candidate: Mapping[str, Any]) -> tuple[float | None, str, list[str]]:
    warnings: list[str] = []
    try:
        explicit = parse_number(candidate.get("draft_entry_probability"))
        declared = parse_bool(candidate.get("draft_declared"))
        eligible = parse_bool(candidate.get("draft_eligible"))
    except DataError as exc:
        return None, "invalid entry inputs", [f"Draft entry inputs are invalid: {exc}"]
    if explicit is not None and not 0.0 <= explicit <= 1.0:
        return None, "invalid draft_entry_probability", [
            "Unconditional next-draft likelihood is withheld because draft_entry_probability must be between 0 and 1."
        ]
    if declared is True and eligible is False:
        return None, "contradictory entry inputs", [
            "Unconditional next-draft likelihood is withheld because draft_declared=true conflicts with draft_eligible=false."
        ]
    if declared is True:
        if explicit is not None and not math.isclose(explicit, 1.0):
            return None, "contradictory entry inputs", [
                "Unconditional next-draft likelihood is withheld because a declared player has draft_entry_probability below 1."
            ]
        return 1.0, "known declaration (entry probability 100%)", warnings
    if eligible is False:
        if explicit is not None and not math.isclose(explicit, 0.0):
            return None, "contradictory entry inputs", [
                "Unconditional next-draft likelihood is withheld because an ineligible player has a nonzero draft_entry_probability."
            ]
        return 0.0, "known ineligibility (entry probability 0%)", warnings
    if explicit is not None:
        return explicit, f"supplied entry probability ({100.0 * explicit:.1f}%)", warnings
    return None, "unknown entry probability", warnings


def _ranking_likelihood(prediction: DraftPrediction) -> float | None:
    """Use next-draft probability when known, otherwise the explicitly conditional grade."""
    return prediction.probability if prediction.probability is not None else prediction.conditional_probability


def _model_stage(metadata: Mapping[str, Any]) -> str:
    explicit = str(metadata.get("model_stage") or "").strip().lower()
    if explicit:
        return explicit
    probability_kind = str(metadata.get("probability_kind") or "").lower()
    population = str(metadata.get("population") or "").lower()
    if probability_kind == "unconditional_next_draft" and "fbs roster" in population:
        return "college_precombine"
    if probability_kind == "conditional_on_combine_invitation" or "combine" in population:
        return "combine"
    return "custom"


def _truthy(value: object) -> bool:
    try:
        return parse_bool(value) is True
    except DataError:
        return False


def _same_player_identity(
    candidate: Mapping[str, Any], row: Mapping[str, Any]
) -> bool:
    """Prevent a drafted reference player from being returned as their own comp."""

    identity_namespaces = (
        ("player_id",),
        ("pfr_id", "pfr_player_id", "outcome_nflverse_pfr_player_id"),
        ("cfb_id", "cfb_player_id", "outcome_nflverse_cfb_player_id"),
        ("gsis_id",),
    )
    for keys in identity_namespaces:
        left = {
            str(candidate.get(key) or "").strip()
            for key in keys
            if str(candidate.get(key) or "").strip()
        }
        right = {
            str(row.get(key) or "").strip()
            for key in keys
            if str(row.get(key) or "").strip()
        }
        if left & right:
            return True

    if not (_truthy(candidate.get("drafted")) or _truthy(candidate.get("reference_only"))):
        return False
    candidate_year = _int_or_none(candidate.get("draft_year"))
    row_year = _int_or_none(row.get("draft_year") or row.get("season"))
    return bool(
        candidate_year is not None
        and candidate_year == row_year
        and normalize_name(candidate.get("name"))
        and normalize_name(candidate.get("name")) == normalize_name(row.get("name"))
    )


def _int_or_none(value: object) -> int | None:
    parsed = parse_number(value)
    return int(parsed) if parsed is not None else None


def _profile_tier(score: float | None) -> str:
    if score is None:
        return "Insufficient evidence"
    if score >= 85:
        return "Blue-chip profile"
    if score >= 75:
        return "Strong NFL prospect profile"
    if score >= 65:
        return "Draftable traits with development upside"
    if score >= 55:
        return "Watch-list / developmental profile"
    return "Below recent drafted benchmarks"

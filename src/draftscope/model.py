from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
import random
from typing import Any, Iterable, Mapping, Sequence

from .mathstats import (
    average_precision,
    brier_score,
    log_loss,
    logit,
    mean,
    median,
    quantile,
    robust_scale,
    roc_auc,
    sigmoid,
    stdev,
)
from .records import DataError, normalize_name, parse_bool, parse_number
from .schema import production_specs


DECISION_THRESHOLD = 0.50
BOARD_CUTOFFS = (10, 25, 50)
CALIBRATION_BINS = 10
MIN_CALIBRATION_ROWS = 40
MIN_CALIBRATION_CLASS_ROWS = 8
FORWARD_VALIDATION_WARMUP_YEARS = 2
COLLEGE_CONTEXT_FEATURES = (
    "context_conference_draft_rate",
    "context_school_draft_rate",
)
COLLEGE_ROSTER_FEATURES = ("height_in", "weight_lb", "bmi")
COLLEGE_AGE_FEATURES = ("age_at_draft", "class_year_numeric")
COLLEGE_PEDIGREE_FEATURES = (
    "recruit_rating",
    "recruit_stars",
    "recruit_national_rank",
)
COLLEGE_AVAILABILITY_FEATURES = ("has_recorded_stats",)
COLLEGE_MISSINGNESS_AUDIT_MIN_POSITIVES = 8
COLLEGE_MISSINGNESS_AUDIT_MIN_NEGATIVES = 30
COLLEGE_MISSINGNESS_AUDIT_GAP = 0.30
COLLEGE_POSITION_POOLS: dict[str, tuple[str, ...]] = {
    # CFBD commonly reports both tackles and interior linemen as generic OL,
    # which normalize to IOL.  Pooling prevents the sparse explicit-OT subset
    # from becoming a misleadingly tiny training population.
    "OT": ("OT", "IOL"),
    "IOL": ("OT", "IOL"),
}
COLLEGE_PUBLIC_SPECIALIST_FEATURES: dict[str, tuple[str, ...]] = {
    "K": (
        "prod_field_goal_attempts",
        "prod_field_goals_made",
        "prod_field_goal_pct",
        "prod_kicking_long",
        "prod_kicking_points",
    ),
    "P": (
        "prod_punts",
        "prod_punt_yards",
        "prod_yards_per_punt",
        "prod_punts_inside_20",
        "prod_punt_touchbacks",
    ),
    "LS": (),
}


@dataclass(frozen=True, slots=True)
class CollegeEvidenceRule:
    minimum_observed: int
    minimum_production: int
    minimum_roster: int = 0
    minimum_age_or_class: int = 0


COLLEGE_EVIDENCE_RULES: dict[str, CollegeEvidenceRule] = {
    "QB": CollegeEvidenceRule(4, 2, minimum_roster=1),
    "RB": CollegeEvidenceRule(3, 1, minimum_roster=1),
    "WR": CollegeEvidenceRule(3, 1, minimum_roster=1),
    "TE": CollegeEvidenceRule(3, 1, minimum_roster=1),
    "OT": CollegeEvidenceRule(3, 0, minimum_roster=2, minimum_age_or_class=1),
    "IOL": CollegeEvidenceRule(3, 0, minimum_roster=2, minimum_age_or_class=1),
    "EDGE": CollegeEvidenceRule(3, 1, minimum_roster=1),
    "IDL": CollegeEvidenceRule(3, 1, minimum_roster=1),
    "LB": CollegeEvidenceRule(3, 1, minimum_roster=1),
    "CB": CollegeEvidenceRule(3, 1, minimum_roster=1),
    "S": CollegeEvidenceRule(3, 1, minimum_roster=1),
    "K": CollegeEvidenceRule(2, 1),
    "P": CollegeEvidenceRule(2, 1),
    "LS": CollegeEvidenceRule(3, 0, minimum_roster=2, minimum_age_or_class=1),
}


@dataclass(frozen=True, slots=True)
class _CollegeContextEncoder:
    positives: int
    rows: int
    conference_counts: Mapping[str, tuple[int, int]]
    school_counts: Mapping[str, tuple[int, int]]

    @property
    def prevalence(self) -> float:
        return self.positives / self.rows if self.rows else 0.0

    def rate(
        self,
        value: object,
        counts: Mapping[str, tuple[int, int]],
        *,
        smoothing: float,
        label: int | None = None,
    ) -> float | None:
        key = normalize_name(value)
        if not key:
            return None
        remove = 1 if label is not None else 0
        total_rows = max(0, self.rows - remove)
        total_positives = max(0, self.positives - (label or 0))
        prior = total_positives / total_rows if total_rows else self.prevalence
        positives, rows = counts.get(key, (0, 0))
        if label is not None and rows:
            positives = max(0, positives - label)
            rows = max(0, rows - 1)
        return (positives + smoothing * prior) / (rows + smoothing)


def college_candidate_features(position: str) -> tuple[str, ...]:
    """Pre-combine inputs available at a college-season checkpoint."""

    production = COLLEGE_PUBLIC_SPECIALIST_FEATURES.get(
        position,
        tuple(spec.key for spec in production_specs(position)),
    )
    return (
        COLLEGE_ROSTER_FEATURES
        + COLLEGE_AGE_FEATURES
        + COLLEGE_PEDIGREE_FEATURES
        + COLLEGE_AVAILABILITY_FEATURES
        + COLLEGE_CONTEXT_FEATURES
        + production
    )


class ModelError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProbabilityModelContract:
    model_stage: str
    probability_kind: str
    population: str
    probability_condition: str
    allowed_features: tuple[str, ...]

    @property
    def contract_id(self) -> str:
        payload = "|".join(
            (
                self.model_stage,
                self.probability_kind,
                self.population,
                self.probability_condition,
                *self.allowed_features,
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class LogisticModel:
    feature_names: tuple[str, ...]
    l2: float = 0.08
    learning_rate: float = 0.035
    max_iter: int = 2200
    always_missing_indicators: bool = False
    medians: dict[str, float] = field(default_factory=dict)
    means: dict[str, float] = field(default_factory=dict)
    scales: dict[str, float] = field(default_factory=dict)
    missing_features: tuple[str, ...] = ()
    weights: list[float] = field(default_factory=list)
    fitted: bool = False
    use_missing_indicators: bool = True

    def fit(self, rows: Sequence[Mapping[str, Any]], labels: Sequence[int]) -> "LogisticModel":
        if len(rows) != len(labels) or not rows:
            raise ModelError("Rows and labels must be non-empty and have equal length")
        if len(set(labels)) < 2:
            raise ModelError("Logistic model requires both drafted and undrafted examples")
        self._fit_preprocessor(rows)
        matrix = [self._transform(row) for row in rows]
        width = len(matrix[0])
        self.weights = [0.0] * width
        prevalence = min(1.0 - 1e-6, max(1e-6, sum(labels) / len(labels)))
        self.weights[0] = logit(prevalence)
        first_moment = [0.0] * width
        second_moment = [0.0] * width
        beta1, beta2, epsilon = 0.9, 0.999, 1e-8
        n = len(rows)
        for iteration in range(1, self.max_iter + 1):
            gradient = [0.0] * width
            for vector, label in zip(matrix, labels):
                prediction = sigmoid(sum(weight * value for weight, value in zip(self.weights, vector)))
                error = prediction - label
                for index, value in enumerate(vector):
                    gradient[index] += error * value
            max_gradient = 0.0
            for index in range(width):
                gradient[index] /= n
                if index:
                    gradient[index] += self.l2 * self.weights[index]
                max_gradient = max(max_gradient, abs(gradient[index]))
                first_moment[index] = beta1 * first_moment[index] + (1.0 - beta1) * gradient[index]
                second_moment[index] = beta2 * second_moment[index] + (1.0 - beta2) * gradient[index] ** 2
                corrected_first = first_moment[index] / (1.0 - beta1**iteration)
                corrected_second = second_moment[index] / (1.0 - beta2**iteration)
                self.weights[index] -= self.learning_rate * corrected_first / (math.sqrt(corrected_second) + epsilon)
            if max_gradient < 1e-6 and iteration > 100:
                break
        self.fitted = True
        return self

    def _fit_preprocessor(self, rows: Sequence[Mapping[str, Any]]) -> None:
        missing: list[str] = []
        for feature in self.feature_names:
            values = [parse_number(row.get(feature)) for row in rows]
            observed = [value for value in values if value is not None]
            if not observed:
                raise ModelError(f"Feature {feature} has no observed values")
            fill = median(observed)
            assert fill is not None
            self.medians[feature] = fill
            imputed = [fill if value is None else value for value in values]
            center = mean(imputed)
            spread = stdev(imputed)
            self.means[feature] = center if center is not None else fill
            self.scales[feature] = spread if spread is not None and spread > 1e-8 else 1.0
            if self.use_missing_indicators and (
                self.always_missing_indicators or any(value is None for value in values)
            ):
                missing.append(feature)
        self.missing_features = tuple(missing)

    def _transform(self, row: Mapping[str, Any]) -> list[float]:
        vector = [1.0]
        missing_set = set(self.missing_features)
        indicators: list[float] = []
        for feature in self.feature_names:
            value = parse_number(row.get(feature))
            was_missing = value is None
            value = self.medians[feature] if value is None else value
            vector.append((value - self.means[feature]) / self.scales[feature])
            if feature in missing_set:
                indicators.append(1.0 if was_missing else 0.0)
        vector.extend(indicators)
        return vector

    def predict_probability(self, row: Mapping[str, Any]) -> float:
        if not self.fitted:
            raise ModelError("Model is not fitted")
        vector = self._transform(row)
        return sigmoid(sum(weight * value for weight, value in zip(self.weights, vector)))


@dataclass(slots=True)
class ModelValidation:
    rows: int
    positives: int
    years: tuple[int, ...]
    brier: float | None
    log_loss: float | None
    roc_auc: float | None
    average_precision: float | None
    prevalence: float
    baseline_brier: float
    passes_quality_gate: bool
    quality_message: str
    threshold: float
    threshold_f1: float
    validation_scheme: str = "unspecified"
    evaluated_years: tuple[int, ...] = ()
    per_year: tuple[dict[str, Any], ...] = ()
    expected_calibration_error: float | None = None
    board_scope: str = (
        "Within-position/model-population ranking, independently within each evaluated draft year."
    )
    board_metrics: tuple[dict[str, Any], ...] = ()
    simple_baselines: tuple[dict[str, Any], ...] = ()
    missingness_policy: str = (
        "Implicit missing-value indicators enabled when retained features are incomplete."
    )
    missingness_audit_warnings: tuple[str, ...] = ()


@dataclass(slots=True)
class DraftPrediction:
    available: bool
    model_stage: str = "unspecified"
    probability_kind: str = "unknown_risk_set"
    contract_id: str = ""
    model_release_id: str = ""
    probability: float | None = None
    conditional_probability: float | None = None
    entry_probability: float | None = None
    probability_scope: str = "conditional"
    conditional_scope: str = "model population"
    interval_80: tuple[float, float] | None = None
    threshold: float | None = None
    projected_pick: bool | None = None
    label: str = ""
    population: str = ""
    feature_coverage: float = 0.0
    features_used: tuple[str, ...] = ()
    out_of_distribution_score: float | None = None
    validation: ModelValidation | None = None
    warnings: list[str] = field(default_factory=list)


class DraftProbabilityModel:
    """Interpretable, position-specific probability model with year-held-out QA."""

    def __init__(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        position: str,
        candidate_features: Iterable[str],
        population: str,
        min_rows: int = 55,
        min_class_rows: int = 12,
        model_stage: str = "combine_or_custom",
        allow_random_fallback: bool = True,
        probability_kind: str = "unknown_risk_set",
        probability_condition: str | None = None,
    ):
        self.position = position
        self.population = population
        self.model_stage = model_stage
        self.probability_kind = probability_kind
        self.allow_random_fallback = allow_random_fallback
        self.candidate_features = tuple(candidate_features)
        self.contract = ProbabilityModelContract(
            model_stage=model_stage,
            probability_kind=probability_kind,
            population=population,
            probability_condition=(
                probability_condition or f"P(drafted | {population})"
            ),
            allowed_features=self.candidate_features,
        )
        self.rows = [row for row in rows if row.get("position") == position and parse_bool(row.get("drafted")) is not None]
        row_stages = {str(row.get("model_stage")) for row in self.rows if row.get("model_stage")}
        row_kinds = {
            str(row.get("probability_kind"))
            for row in self.rows
            if row.get("probability_kind")
        }
        if row_stages and row_stages != {model_stage}:
            raise ModelError(f"Historical rows mix model stages: {sorted(row_stages)}")
        if row_kinds and row_kinds != {probability_kind}:
            raise ModelError(f"Historical rows mix probability contracts: {sorted(row_kinds)}")
        self.labels = [1 if parse_bool(row.get("drafted")) else 0 for row in self.rows]
        if len(self.rows) < min_rows or sum(self.labels) < min_class_rows or len(self.labels) - sum(self.labels) < min_class_rows:
            raise ModelError(
                f"Insufficient labeled {position} history: {len(self.rows)} rows, "
                f"{sum(self.labels)} drafted, {len(self.labels) - sum(self.labels)} undrafted"
            )
        self.training_rows, self._row_transformer = self._prepare_training_rows(self.rows, self.labels)
        features = self._select_features(self.training_rows, self.labels)
        if len(features) < 2:
            raise ModelError(f"Fewer than two usable features for {position}")
        self.features = tuple(features)
        self.raw_model = self._fit_probability_model(
            self.training_rows,
            self.labels,
            self.features,
            max_iter=2200,
        )
        self.calibrator: LogisticModel | None = None
        self.validation = self._cross_validate()

    def _prepare_training_rows(
        self,
        rows: Sequence[Mapping[str, Any]],
        labels: Sequence[int],
    ) -> tuple[list[dict[str, Any]], Any]:
        return [dict(row) for row in rows], None

    def _prepare_external_row(self, row: Mapping[str, Any], transformer: Any) -> dict[str, Any]:
        return dict(row)

    def _fit_probability_model(
        self,
        rows: Sequence[Mapping[str, Any]],
        labels: Sequence[int],
        features: Sequence[str],
        *,
        max_iter: int,
    ) -> LogisticModel:
        return LogisticModel(tuple(features), max_iter=max_iter).fit(rows, labels)

    def _candidate_evidence(
        self,
        candidate: Mapping[str, Any],
        prepared_candidate: Mapping[str, Any],
    ) -> tuple[list[str], float, str | None]:
        observed = [
            feature
            for feature in self.features
            if parse_number(prepared_candidate.get(feature)) is not None
        ]
        coverage = len(observed) / len(self.features)
        message = None
        if len(observed) < 3 or coverage < 0.30:
            prefix = (
                "Combine-stage cross-check withheld"
                if "combine" in self.model_stage
                else "Stage estimate withheld"
            )
            message = (
                f"{prefix}: {len(observed)}/{len(self.features)} retained features are observed "
                f"({coverage:.0%}); this stage requires at least three observed features and 30% coverage."
            )
        return observed, coverage, message

    def _select_features(
        self, rows: Sequence[Mapping[str, Any]], labels: Sequence[int]
    ) -> list[str]:
        return self._select_features_from_candidates(
            rows,
            labels,
            self.candidate_features,
        )

    def _select_features_from_candidates(
        self,
        rows: Sequence[Mapping[str, Any]],
        labels: Sequence[int],
        candidates: Sequence[str],
    ) -> list[str]:
        features: list[str] = []
        for feature in candidates:
            values = [parse_number(row.get(feature)) for row in rows]
            observed = [value for value in values if value is not None]
            if len(observed) / len(values) < 0.25:
                continue
            positive_values = [value for value, label in zip(values, labels) if label == 1]
            negative_values = [value for value, label in zip(values, labels) if label == 0]
            positive_observed = sum(value is not None for value in positive_values) / len(positive_values)
            negative_observed = sum(value is not None for value in negative_values) / len(negative_values)
            # Prevent outcome-joined fields (for example age available only on draft rows)
            # from turning missingness into a disguised target label.
            if min(positive_observed, negative_observed) < 0.10 or abs(positive_observed - negative_observed) > 0.65:
                continue
            spread = stdev(observed)
            if spread is None or spread < 1e-8:
                continue
            features.append(feature)
        return features

    def _cross_validate(self) -> ModelValidation:
        years = sorted({int(float(row.get("draft_year") or row.get("season") or 0)) for row in self.rows})
        oof_labels: list[int] = []
        oof_raw: list[float] = []
        calibrated: list[float] = []
        baseline_probabilities: list[float] = []
        evaluation_indices: list[int] = []
        evaluated_years: tuple[int, ...] = ()
        per_year: tuple[dict[str, Any], ...] = ()
        simple_baselines: tuple[dict[str, Any], ...] = ()
        validation_scheme = "unavailable"
        if len(years) >= FORWARD_VALIDATION_WARMUP_YEARS + 1:
            year_groups = self._validation_groups()
            (
                oof_labels,
                oof_raw,
                calibrated,
                baseline_probabilities,
                evaluation_indices,
                evaluated_years,
                per_year,
                simple_baselines,
            ) = self._evaluate_forward_years(year_groups)
            validation_scheme = (
                "expanding-window past-only by draft year; "
                f"{FORWARD_VALIDATION_WARMUP_YEARS}-year warmup; prequential calibration"
            )
        if (
            self.allow_random_fallback
            and len(years) < FORWARD_VALIDATION_WARMUP_YEARS + 1
            and len(oof_raw) < max(30, len(self.rows) // 2)
        ):
            # Deterministic fallback for custom datasets without usable year groups.
            rng = random.Random(73191)
            indices = list(range(len(self.rows)))
            rng.shuffle(indices)
            folds = [indices[offset::5] for offset in range(5)]
            random_groups = {
                index: fold_number
                for fold_number, fold in enumerate(folds)
                for index in fold
            }
            (
                oof_labels,
                oof_raw,
                calibrated,
                baseline_probabilities,
                evaluation_indices,
            ) = self._evaluate_groups(random_groups)
            validation_scheme = "deterministic random five-fold fallback; no usable year history"

        # The deployment calibrator sees only raw predictions generated out of
        # fold. Reported validation uses separate nested calibrators below.
        self.calibrator = self._fit_calibrator(oof_raw, oof_labels, max_iter=1600)
        prevalence = sum(oof_labels) / len(oof_labels) if oof_labels else 0.0
        measured_brier = brier_score(oof_labels, calibrated)
        baseline_brier = brier_score(oof_labels, baseline_probabilities)
        if baseline_brier is None:
            baseline_brier = prevalence * (1.0 - prevalence)
        measured_auc = roc_auc(oof_labels, calibrated)
        measured_ap = average_precision(oof_labels, calibrated)
        measured_ece = _expected_calibration_error(oof_labels, calibrated)
        board_metrics = (
            _draft_board_metrics(
                self.rows,
                evaluation_indices,
                calibrated,
                cutoffs=BOARD_CUTOFFS,
            )
            if validation_scheme.startswith("expanding-window past-only")
            else ()
        )
        passes_quality = bool(
            len(oof_labels) >= 50
            and measured_brier is not None
            and measured_brier < baseline_brier * 0.995
            and measured_auc is not None
            and measured_auc >= 0.55
            and measured_ap is not None
            and measured_ap >= prevalence + 0.02
        )
        if validation_scheme.startswith("expanding-window past-only"):
            quality_message = (
                "Past-only expanding-window predictions beat the position prevalence baseline."
                if passes_quality
                else "Past-only expanding-window predictions did not beat the position prevalence baseline reliably."
            )
        else:
            quality_message = (
                "Deterministic random-fold predictions beat the position prevalence baseline; "
                "time-safe validation was unavailable."
                if passes_quality
                else "Deterministic random-fold predictions did not beat the position prevalence baseline reliably; "
                "time-safe validation was unavailable."
            )
        threshold_f1 = _f1_at_threshold(oof_labels, calibrated, DECISION_THRESHOLD)
        return ModelValidation(
            rows=len(oof_labels),
            positives=sum(oof_labels),
            years=tuple(years),
            brier=measured_brier,
            log_loss=log_loss(oof_labels, calibrated),
            roc_auc=measured_auc,
            average_precision=measured_ap,
            prevalence=prevalence,
            baseline_brier=baseline_brier,
            passes_quality_gate=passes_quality,
            quality_message=quality_message,
            threshold=DECISION_THRESHOLD,
            threshold_f1=threshold_f1,
            validation_scheme=validation_scheme,
            evaluated_years=evaluated_years,
            per_year=per_year,
            expected_calibration_error=measured_ece,
            board_metrics=board_metrics,
            simple_baselines=simple_baselines,
        )

    def _validation_groups(self) -> dict[int, int]:
        return {
            index: int(float(row.get("draft_year") or row.get("season") or 0))
            for index, row in enumerate(self.rows)
        }

    def _evaluate_forward_years(
        self, year_by_index: Mapping[int, int]
    ) -> tuple[
        list[int],
        list[float],
        list[float],
        list[float],
        list[int],
        tuple[int, ...],
        tuple[dict[str, Any], ...],
        tuple[dict[str, Any], ...],
    ]:
        """Evaluate each class using only strictly earlier draft classes.

        Earlier prequential predictions may calibrate a later class, but neither
        feature selection, preprocessing, fitting, nor calibration can inspect
        that class or any future class. The warmup classes remain training-only
        for the published aggregate metrics.
        """

        labels: list[int] = []
        raw_probabilities: list[float] = []
        calibrated_probabilities: list[float] = []
        baselines: list[float] = []
        evaluation_indices: list[int] = []
        evaluated: list[int] = []
        per_year: list[dict[str, Any]] = []
        prior_oos_labels: list[int] = []
        prior_oos_raw: list[float] = []
        all_indices = sorted(year_by_index)
        years = sorted(set(year_by_index.values()))
        baseline_states = self._new_simple_baseline_states()

        for year_offset, holdout_year in enumerate(years[1:], start=1):
            train_indices = [
                index for index in all_indices if year_by_index[index] < holdout_year
            ]
            test_indices = [
                index for index in all_indices if year_by_index[index] == holdout_year
            ]
            train_labels = [self.labels[index] for index in train_indices]
            if not test_indices or len(set(train_labels)) < 2:
                continue
            raw_train_rows = [self.rows[index] for index in train_indices]
            train_rows, transformer = self._prepare_training_rows(
                raw_train_rows, train_labels
            )
            fold_features = self._select_features(train_rows, train_labels)
            if len(fold_features) < 2:
                continue
            try:
                model = self._fit_probability_model(
                    train_rows,
                    train_labels,
                    fold_features,
                    max_iter=1600,
                )
            except ModelError:
                continue
            calibrator = self._fit_calibrator(
                prior_oos_raw, prior_oos_labels, max_iter=1400
            )
            fold_labels: list[int] = []
            fold_raw: list[float] = []
            fold_calibrated: list[float] = []
            fold_test_rows: list[dict[str, Any]] = []
            train_prevalence = sum(train_labels) / len(train_labels)
            for index in test_indices:
                test_row = self._prepare_external_row(self.rows[index], transformer)
                fold_test_rows.append(test_row)
                raw_probability = model.predict_probability(test_row)
                calibrated_probability = (
                    calibrator.predict_probability(
                        {"raw_logit": logit(raw_probability)}
                    )
                    if calibrator
                    else raw_probability
                )
                fold_labels.append(self.labels[index])
                fold_raw.append(raw_probability)
                fold_calibrated.append(calibrated_probability)

            self._evaluate_simple_baseline_fold(
                baseline_states,
                train_rows=train_rows,
                train_labels=train_labels,
                test_rows=fold_test_rows,
                test_labels=fold_labels,
                test_indices=test_indices,
                holdout_year=holdout_year,
                train_years=tuple(year for year in years if year < holdout_year),
                publish=year_offset >= FORWARD_VALIDATION_WARMUP_YEARS,
            )

            # The current class becomes calibration evidence only after every
            # current-class prediction has been generated.
            calibration_rows = len(prior_oos_labels)
            prior_oos_labels.extend(fold_labels)
            prior_oos_raw.extend(fold_raw)
            if year_offset < FORWARD_VALIDATION_WARMUP_YEARS:
                continue

            fold_baselines = [train_prevalence] * len(fold_labels)
            labels.extend(fold_labels)
            raw_probabilities.extend(fold_raw)
            calibrated_probabilities.extend(fold_calibrated)
            baselines.extend(fold_baselines)
            evaluation_indices.extend(test_indices)
            evaluated.append(holdout_year)
            per_year.append(
                {
                    "year": holdout_year,
                    "train_years": tuple(year for year in years if year < holdout_year),
                    "rows": len(fold_labels),
                    "positives": sum(fold_labels),
                    "prevalence": sum(fold_labels) / len(fold_labels),
                    "brier": brier_score(fold_labels, fold_calibrated),
                    "baseline_brier": brier_score(fold_labels, fold_baselines),
                    "log_loss": log_loss(fold_labels, fold_calibrated),
                    "roc_auc": roc_auc(fold_labels, fold_calibrated),
                    "average_precision": average_precision(
                        fold_labels, fold_calibrated
                    ),
                    "expected_calibration_error": _expected_calibration_error(
                        fold_labels, fold_calibrated
                    ),
                    "board_metrics": _draft_board_metrics(
                        self.rows,
                        test_indices,
                        fold_calibrated,
                        cutoffs=BOARD_CUTOFFS,
                    ),
                    "calibrator_rows": calibration_rows,
                }
            )

        return (
            labels,
            raw_probabilities,
            calibrated_probabilities,
            baselines,
            evaluation_indices,
            tuple(evaluated),
            tuple(per_year),
            self._summarize_simple_baselines(
                baseline_states,
                primary_indices=evaluation_indices,
            ),
        )

    def _evaluate_groups(
        self, group_by_index: Mapping[int, int]
    ) -> tuple[list[int], list[float], list[float], list[float], list[int]]:
        """Return honest outer-fold labels, raw/calibrated scores, and baselines."""

        oof_labels: list[int] = []
        oof_raw: list[float] = []
        calibrated: list[float] = []
        baseline_probabilities: list[float] = []
        evaluation_indices: list[int] = []
        all_indices = sorted(group_by_index)
        for holdout in sorted(set(group_by_index.values())):
            train_indices = [index for index in all_indices if group_by_index[index] != holdout]
            test_indices = [index for index in all_indices if group_by_index[index] == holdout]
            train_labels = [self.labels[index] for index in train_indices]
            if not test_indices or len(set(train_labels)) < 2:
                continue
            raw_train_rows = [self.rows[index] for index in train_indices]
            train_rows, transformer = self._prepare_training_rows(raw_train_rows, train_labels)
            # Availability and outcome checks must not inspect the held-out year.
            fold_features = self._select_features(train_rows, train_labels)
            if len(fold_features) < 2:
                continue
            model = self._fit_probability_model(
                train_rows,
                train_labels,
                fold_features,
                max_iter=1600,
            )

            # Reusing top-level OOF scores to train this calibrator would leak the
            # outer holdout through base models fitted for the other years.
            inner_labels, inner_raw = self._inner_oof_predictions(train_indices, group_by_index)
            fold_calibrator = self._fit_calibrator(inner_raw, inner_labels, max_iter=1400)
            train_prevalence = sum(train_labels) / len(train_labels)
            for index in test_indices:
                test_row = self._prepare_external_row(self.rows[index], transformer)
                raw_probability = model.predict_probability(test_row)
                calibrated_probability = (
                    fold_calibrator.predict_probability({"raw_logit": logit(raw_probability)})
                    if fold_calibrator
                    else raw_probability
                )
                oof_labels.append(self.labels[index])
                oof_raw.append(raw_probability)
                calibrated.append(calibrated_probability)
                baseline_probabilities.append(train_prevalence)
                evaluation_indices.append(index)
        return oof_labels, oof_raw, calibrated, baseline_probabilities, evaluation_indices

    def _new_simple_baseline_states(self) -> dict[str, dict[str, Any]]:
        pedigree = tuple(
            feature
            for feature in COLLEGE_PEDIGREE_FEATURES
            if feature in self.candidate_features
        )
        production = tuple(
            feature
            for feature in self.candidate_features
            if feature.startswith("prod_") or feature == "has_recorded_stats"
        )
        definitions = (
            (
                "recruiting_only",
                "Recruiting-only",
                "Logistic baseline limited to recruiting rating, stars, and national rank.",
                pedigree,
            ),
            (
                "production_only",
                "Production-only",
                "Logistic baseline limited to position production and its recorded-stats flag.",
                production,
            ),
        )
        return {
            name: {
                "name": name,
                "label": label,
                "definition": definition,
                "candidate_features": candidates,
                "prior_labels": [],
                "prior_raw": [],
                "labels": [],
                "probabilities": [],
                "indices": [],
                "evaluated_years": [],
                "features_used": set(),
                "per_year": [],
            }
            for name, label, definition, candidates in definitions
        }

    def _evaluate_simple_baseline_fold(
        self,
        states: Mapping[str, dict[str, Any]],
        *,
        train_rows: Sequence[Mapping[str, Any]],
        train_labels: Sequence[int],
        test_rows: Sequence[Mapping[str, Any]],
        test_labels: Sequence[int],
        test_indices: Sequence[int],
        holdout_year: int,
        train_years: tuple[int, ...],
        publish: bool,
    ) -> None:
        """Score simple feature-family baselines without inspecting the holdout labels.

        Feature availability, preprocessing, fitting, and calibration all use the
        expanding prefix. The current class is appended to calibration history
        only after every prediction for that class has been generated.
        """

        for state in states.values():
            candidates = tuple(state["candidate_features"])
            if len(candidates) < 2:
                continue
            features = self._select_features_from_candidates(
                train_rows,
                train_labels,
                candidates,
            )
            if len(features) < 2:
                continue
            try:
                model = self._fit_probability_model(
                    train_rows,
                    train_labels,
                    features,
                    max_iter=1000,
                )
            except ModelError:
                continue
            calibrator = self._fit_calibrator(
                state["prior_raw"],
                state["prior_labels"],
                max_iter=900,
            )
            raw_probabilities = [model.predict_probability(row) for row in test_rows]
            probabilities = [
                (
                    calibrator.predict_probability({"raw_logit": logit(probability)})
                    if calibrator
                    else probability
                )
                for probability in raw_probabilities
            ]
            calibration_rows = len(state["prior_labels"])
            state["prior_labels"].extend(test_labels)
            state["prior_raw"].extend(raw_probabilities)
            if not publish:
                continue
            state["labels"].extend(test_labels)
            state["probabilities"].extend(probabilities)
            state["indices"].extend(test_indices)
            state["evaluated_years"].append(holdout_year)
            state["features_used"].update(features)
            state["per_year"].append(
                {
                    "year": holdout_year,
                    "train_years": train_years,
                    "rows": len(test_labels),
                    "positives": sum(test_labels),
                    "features_used": tuple(features),
                    "calibrator_rows": calibration_rows,
                    "brier": brier_score(test_labels, probabilities),
                    "log_loss": log_loss(test_labels, probabilities),
                    "roc_auc": roc_auc(test_labels, probabilities),
                    "average_precision": average_precision(test_labels, probabilities),
                    "expected_calibration_error": _expected_calibration_error(
                        test_labels, probabilities
                    ),
                }
            )

    def _summarize_simple_baselines(
        self,
        states: Mapping[str, Mapping[str, Any]],
        *,
        primary_indices: Sequence[int],
    ) -> tuple[dict[str, Any], ...]:
        summaries: list[dict[str, Any]] = []
        for state in states.values():
            labels = list(state["labels"])
            probabilities = list(state["probabilities"])
            indices = list(state["indices"])
            candidates = tuple(state["candidate_features"])
            available = bool(labels)
            if len(candidates) < 2:
                reason = "Fewer than two features from this family exist in the model contract."
            elif not available:
                reason = "Fewer than two trainable features were available in every published past-only fold."
            else:
                reason = ""
            summaries.append(
                {
                    "name": state["name"],
                    "label": state["label"],
                    "definition": state["definition"],
                    "available": available,
                    "unavailable_reason": reason or None,
                    "validation_scheme": (
                        "expanding-window past-only by draft year; prequential calibration"
                    ),
                    "rows": len(labels),
                    "positives": sum(labels),
                    "evaluated_years": tuple(state["evaluated_years"]),
                    "features_used": tuple(sorted(state["features_used"])),
                    "evaluation_set_matches_primary": tuple(indices)
                    == tuple(primary_indices),
                    "brier": brier_score(labels, probabilities),
                    "log_loss": log_loss(labels, probabilities),
                    "roc_auc": roc_auc(labels, probabilities),
                    "average_precision": average_precision(labels, probabilities),
                    "expected_calibration_error": _expected_calibration_error(
                        labels, probabilities
                    ),
                    "board_metrics": _draft_board_metrics(
                        self.rows,
                        indices,
                        probabilities,
                        cutoffs=BOARD_CUTOFFS,
                    ),
                    "per_year": tuple(state["per_year"]),
                }
            )
        return tuple(summaries)

    def _inner_oof_predictions(
        self, outer_train_indices: Sequence[int], group_by_index: Mapping[int, int]
    ) -> tuple[list[int], list[float]]:
        labels: list[int] = []
        probabilities: list[float] = []
        inner_groups = sorted({group_by_index[index] for index in outer_train_indices})
        for holdout in inner_groups:
            train_indices = [
                index for index in outer_train_indices if group_by_index[index] != holdout
            ]
            test_indices = [
                index for index in outer_train_indices if group_by_index[index] == holdout
            ]
            train_labels = [self.labels[index] for index in train_indices]
            if not test_indices or len(set(train_labels)) < 2:
                continue
            raw_train_rows = [self.rows[index] for index in train_indices]
            train_rows, transformer = self._prepare_training_rows(raw_train_rows, train_labels)
            fold_features = self._select_features(train_rows, train_labels)
            if len(fold_features) < 2:
                continue
            model = self._fit_probability_model(
                train_rows,
                train_labels,
                fold_features,
                max_iter=1400,
            )
            for index in test_indices:
                labels.append(self.labels[index])
                test_row = self._prepare_external_row(self.rows[index], transformer)
                probabilities.append(model.predict_probability(test_row))
        return labels, probabilities

    def _fit_calibrator(
        self,
        probabilities: Sequence[float], labels: Sequence[int], *, max_iter: int
    ) -> LogisticModel | None:
        positives = sum(labels)
        negatives = len(labels) - positives
        if (
            len(labels) < MIN_CALIBRATION_ROWS
            or positives < MIN_CALIBRATION_CLASS_ROWS
            or negatives < MIN_CALIBRATION_CLASS_ROWS
        ):
            return None
        calibration_rows = [{"raw_logit": logit(probability)} for probability in probabilities]
        return LogisticModel(("raw_logit",), l2=0.12, max_iter=max_iter).fit(calibration_rows, labels)

    def _calibrate(self, probability: float) -> float:
        if not self.calibrator:
            return probability
        return self.calibrator.predict_probability({"raw_logit": logit(probability)})

    def _ood_score(self, prepared_candidate: Mapping[str, Any]) -> float | None:
        distances: list[float] = []
        # Smoothed target encodings and binary availability flags are bounded
        # model inputs, but robust z-scores are not meaningful diagnostics for
        # them (a strong program can otherwise look dozens of standard
        # deviations "out of distribution").
        diagnostic_features = tuple(
            feature
            for feature in self.features
            if feature not in COLLEGE_CONTEXT_FEATURES + COLLEGE_AVAILABILITY_FEATURES
        )
        for feature in diagnostic_features:
            value = parse_number(prepared_candidate.get(feature))
            if value is None:
                continue
            historical = [parse_number(row.get(feature)) for row in self.training_rows]
            center = median(historical)
            if center is None:
                continue
            distances.append(((value - center) / robust_scale(historical)) ** 2)
        return math.sqrt(sum(distances) / len(distances)) if distances else None

    def predict(self, candidate: Mapping[str, Any], *, bootstrap: int = 30, seed: int = 8128) -> DraftPrediction:
        if not self.validation.passes_quality_gate:
            return DraftPrediction(
                available=False,
                model_stage=self.model_stage,
                probability_kind=self.probability_kind,
                contract_id=self.contract.contract_id,
                label="Position model withheld by validation gate",
                population=self.population,
                conditional_scope=self.population,
                features_used=self.features,
                validation=self.validation,
                warnings=[self.validation.quality_message],
            )
        prepared_candidate = self._prepare_external_row(candidate, self._row_transformer)
        observed, coverage, evidence_message = self._candidate_evidence(candidate, prepared_candidate)
        warnings: list[str] = []
        if evidence_message:
            return DraftPrediction(
                available=False,
                model_stage=self.model_stage,
                probability_kind=self.probability_kind,
                contract_id=self.contract.contract_id,
                label="Insufficient comparable inputs",
                population=self.population,
                conditional_scope=self.population,
                feature_coverage=coverage,
                features_used=self.features,
                validation=self.validation,
                warnings=[evidence_message],
            )
        probability = self._calibrate(self.raw_model.predict_probability(prepared_candidate))
        interval: tuple[float, float] | None = None
        years = sorted({int(float(row.get("draft_year") or row.get("season") or 0)) for row in self.rows})
        if bootstrap > 0 and len(years) >= 3:
            rng = random.Random(seed)
            samples: list[float] = []
            rows_by_year = {year: [row for row in self.rows if int(float(row.get("draft_year") or row.get("season") or 0)) == year] for year in years}
            for _ in range(bootstrap):
                sampled_years = [rng.choice(years) for _ in years]
                sampled_rows = [row for year in sampled_years for row in rows_by_year[year]]
                sampled_labels = [1 if parse_bool(row.get("drafted")) else 0 for row in sampled_rows]
                if len(set(sampled_labels)) < 2:
                    continue
                try:
                    prepared_rows, transformer = self._prepare_training_rows(
                        sampled_rows,
                        sampled_labels,
                    )
                    model = self._fit_probability_model(
                        prepared_rows,
                        sampled_labels,
                        self.features,
                        max_iter=1400,
                    )
                except ModelError:
                    continue
                sample_candidate = self._prepare_external_row(candidate, transformer)
                samples.append(self._calibrate(model.predict_probability(sample_candidate)))
            low, high = quantile(samples, 0.10), quantile(samples, 0.90)
            if low is not None and high is not None:
                interval = (low, high)
        ood = self._ood_score(prepared_candidate)
        if ood is not None and ood > 3.0:
            warnings.append("The player is outside the typical historical feature range; treat the estimate cautiously.")
        if coverage < 0.60:
            warnings.append("Feature coverage is below 60%; missing inputs increase dependence on imputation.")
        threshold = self.validation.threshold
        projected = probability >= threshold
        if probability >= max(threshold, 0.70):
            label = "Likely draft pick within this model population"
        elif projected:
            label = "Draftable range within this model population"
        elif probability >= max(0.30, threshold * 0.65):
            label = "Borderline draft range within this model population"
        else:
            label = "Below the historical draft line within this model population"
        return DraftPrediction(
            available=True,
            model_stage=self.model_stage,
            probability_kind=self.probability_kind,
            contract_id=self.contract.contract_id,
            probability=probability,
            conditional_probability=probability,
            probability_scope="conditional on model population",
            interval_80=interval,
            threshold=threshold,
            projected_pick=projected,
            label=label,
            population=self.population,
            conditional_scope=self.population,
            feature_coverage=coverage,
            features_used=self.features,
            out_of_distribution_score=ood,
            validation=self.validation,
            warnings=warnings,
        )


class CollegeDraftProbabilityModel(DraftProbabilityModel):
    """Pre-combine model for P(drafted next draft | FBS roster checkpoint)."""

    def __init__(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        position: str,
        population: str,
    ):
        self.source_position_pool = COLLEGE_POSITION_POOLS.get(position, (position,))
        normalized_rows: list[dict[str, Any]] = []
        for source_row in rows:
            row = dict(source_row)
            if row.get("position") in self.source_position_pool:
                row["college_source_position"] = row.get("position")
                row["position"] = position
            if "has_recorded_stats" in row:
                row["has_recorded_stats"] = _college_availability_value(
                    row.get("has_recorded_stats")
                )
            normalized_rows.append(row)
        labeled = [
            row
            for row in normalized_rows
            if row.get("position") == position and parse_bool(row.get("drafted")) is not None
        ]
        if any(parse_number(row.get("draft_year")) is None for row in labeled):
            raise ModelError(
                "College pre-combine history requires draft_year for every labeled FBS roster row"
            )
        years = {int(parse_number(row.get("draft_year")) or 0) for row in labeled}
        if len(years) < 3:
            raise ModelError(
                "College pre-combine history requires at least three draft years for grouped validation"
            )
        kinds = {
            str(row.get("probability_kind"))
            for row in labeled
            if row.get("probability_kind")
        }
        if kinds and kinds != {"unconditional_next_draft"}:
            raise ModelError(
                "College pre-combine rows must use probability_kind=unconditional_next_draft"
            )
        stages = {
            str(row.get("model_stage"))
            for row in labeled
            if row.get("model_stage")
        }
        if stages and stages != {"college_precombine"}:
            raise ModelError("College pre-combine history contains a conflicting model_stage")
        min_rows = 80 if position in {"K", "P", "LS"} else 120
        min_class_rows = 6 if position == "LS" else (8 if position in {"K", "P"} else 12)
        super().__init__(
            normalized_rows,
            position=position,
            candidate_features=college_candidate_features(position),
            population=population,
            min_rows=min_rows,
            min_class_rows=min_class_rows,
            model_stage="college_precombine",
            allow_random_fallback=False,
            probability_kind="unconditional_next_draft",
            probability_condition="P(drafted next draft | FBS roster player at checkpoint)",
        )
        self.validation.missingness_policy = (
            "College-stage implicit missing-value indicators are disabled; incomplete numeric "
            "features use training-fold median imputation, and has_recorded_stats is the sole "
            "explicit production-availability feature."
        )
        self.validation.missingness_audit_warnings = _college_missingness_audit(
            self.training_rows,
            self.labels,
            features=self.features,
        )
        negatives = self.validation.rows - self.validation.positives
        stage_gate = bool(
            len(self.validation.years) >= 4
            and self.validation.positives >= 30
            and negatives >= 100
            and self.calibrator is not None
        )
        if not stage_gate:
            self.validation.passes_quality_gate = False
            self.validation.quality_message = (
                "College-stage probability withheld: past-only validation requires at least four "
                "available draft classes, 30 evaluated drafted outcomes, 100 evaluated undrafted "
                "outcomes, and an out-of-time calibrator."
            )

    def _select_features(
        self,
        rows: Sequence[Mapping[str, Any]],
        labels: Sequence[int],
    ) -> list[str]:
        """Select only checkpoint-safe fields while retaining sparse starter production.

        In the full-roster population, legitimate quarterback, kicker, punter,
        and other starter statistics can be present for far fewer than 25% of a
        position room. Their availability is itself known at the checkpoint, so
        the combine model's outcome-joined missingness guard is not appropriate
        after the college source allow-list and temporal audit have passed.
        """

        return self._select_features_from_candidates(
            rows,
            labels,
            self.candidate_features,
        )

    def _select_features_from_candidates(
        self,
        rows: Sequence[Mapping[str, Any]],
        labels: Sequence[int],
        candidates: Sequence[str],
    ) -> list[str]:
        features: list[str] = []
        minimum_observed = max(20, math.ceil(len(rows) * 0.015))
        for feature in candidates:
            values = [parse_number(row.get(feature)) for row in rows]
            observed = [value for value in values if value is not None]
            if len(observed) < minimum_observed:
                continue
            spread = stdev(observed)
            if spread is None or spread < 1e-8:
                continue
            features.append(feature)
        return features

    def _validation_groups(self) -> dict[int, int]:
        # Keep exact draft years so expanding-window validation can enforce that
        # every holdout is scored using strictly earlier classes.
        return {
            index: int(parse_number(row.get("draft_year")) or 0)
            for index, row in enumerate(self.rows)
        }

    def _prepare_training_rows(
        self,
        rows: Sequence[Mapping[str, Any]],
        labels: Sequence[int],
    ) -> tuple[list[dict[str, Any]], _CollegeContextEncoder]:
        encoder = _fit_college_context(rows, labels)
        prepared = [
            _prepare_college_row(row, encoder, label=label)
            for row, label in zip(rows, labels)
        ]
        return prepared, encoder

    def _prepare_external_row(
        self,
        row: Mapping[str, Any],
        transformer: _CollegeContextEncoder,
    ) -> dict[str, Any]:
        return _prepare_college_row(row, transformer)

    def _fit_probability_model(
        self,
        rows: Sequence[Mapping[str, Any]],
        labels: Sequence[int],
        features: Sequence[str],
        *,
        max_iter: int,
    ) -> LogisticModel:
        sampled_rows, sampled_labels = _case_control_sample(
            rows,
            labels,
            negative_ratio=6,
            minimum_negatives=180,
            maximum_negatives=500,
        )
        model = LogisticModel(
            tuple(features),
            l2=0.12,
            max_iter=min(max_iter, 550),
            use_missing_indicators=False,
        ).fit(sampled_rows, sampled_labels)
        _correct_case_control_intercept(model, labels, sampled_labels)
        return model

    def _fit_calibrator(
        self,
        probabilities: Sequence[float],
        labels: Sequence[int],
        *,
        max_iter: int,
    ) -> LogisticModel | None:
        positives = sum(labels)
        negatives = len(labels) - positives
        if (
            len(labels) < MIN_CALIBRATION_ROWS
            or positives < MIN_CALIBRATION_CLASS_ROWS
            or negatives < MIN_CALIBRATION_CLASS_ROWS
        ):
            return None
        rows = [{"raw_logit": logit(probability)} for probability in probabilities]
        sampled_rows, sampled_labels = _case_control_sample(
            rows,
            labels,
            negative_ratio=8,
            minimum_negatives=240,
            maximum_negatives=800,
        )
        calibrator = LogisticModel(
            ("raw_logit",),
            l2=0.12,
            max_iter=min(max_iter, 650),
        ).fit(sampled_rows, sampled_labels)
        _correct_case_control_intercept(calibrator, labels, sampled_labels)
        return calibrator

    def _candidate_evidence(
        self,
        candidate: Mapping[str, Any],
        prepared_candidate: Mapping[str, Any],
    ) -> tuple[list[str], float, str | None]:
        observed = [
            feature
            for feature in self.features
            if parse_number(prepared_candidate.get(feature)) is not None
        ]
        coverage = len(observed) / len(self.features)
        observed_set = set(observed)
        production = sum(feature.startswith("prod_") for feature in observed_set)
        roster = sum(feature in {"height_in", "weight_lb"} for feature in observed_set)
        age_or_class = sum(feature in COLLEGE_AGE_FEATURES for feature in observed_set)
        rule = COLLEGE_EVIDENCE_RULES.get(self.position, CollegeEvidenceRule(3, 1))
        failures: list[str] = []
        if len(observed) < rule.minimum_observed:
            failures.append(f"{rule.minimum_observed} total college-model inputs")
        if production < rule.minimum_production:
            failures.append(f"{rule.minimum_production} position-production input(s)")
        if roster < rule.minimum_roster:
            failures.append(f"{rule.minimum_roster} roster measurement(s) from height/weight")
        if age_or_class < rule.minimum_age_or_class:
            failures.append(f"{rule.minimum_age_or_class} age/class input(s)")
        message = (
            "College-stage evidence requires " + ", ".join(failures) + "."
            if failures
            else None
        )
        return observed, coverage, message

    def predict(
        self,
        candidate: Mapping[str, Any],
        *,
        bootstrap: int = 30,
        seed: int = 8128,
    ) -> DraftPrediction:
        prediction = super().predict(candidate, bootstrap=bootstrap, seed=seed)
        prediction.warnings.extend(self.validation.missingness_audit_warnings)
        if prediction.available:
            prediction.threshold = None
            prediction.projected_pick = None
            prediction.label = (
                "College-stage next-draft estimate; no binary draft line is published for the "
                "low-base-rate FBS roster population"
            )
        if len(self.source_position_pool) > 1:
            prediction.warnings.append(
                "College-stage OT/IOL estimate uses a shared offensive-line cohort because "
                "the public roster source commonly labels both groups as generic OL; "
                "combine-stage position benchmarks remain separate."
            )
        return prediction


def _college_missingness_audit(
    rows: Sequence[Mapping[str, Any]],
    labels: Sequence[int],
    *,
    features: Sequence[str],
) -> tuple[str, ...]:
    """Flag source-era roster availability gaps without turning them into inputs.

    Production availability is represented by ``has_recorded_stats``. This audit
    is deliberately limited to roster, age, and class fields whose missingness can
    reflect source vintages or identity-join quality rather than player ability.
    """

    audited_features = tuple(
        feature
        for feature in COLLEGE_ROSTER_FEATURES + COLLEGE_AGE_FEATURES
        if feature in features
    )
    years = sorted(
        {
            int(parse_number(row.get("draft_year") or row.get("season")) or 0)
            for row in rows
        }
    )
    flagged_features: list[str] = []
    for feature in audited_features:
        flagged: list[tuple[int, float]] = []
        for year in years:
            year_indices = [
                index
                for index, row in enumerate(rows)
                if int(parse_number(row.get("draft_year") or row.get("season")) or 0)
                == year
            ]
            positives = [index for index in year_indices if labels[index] == 1]
            negatives = [index for index in year_indices if labels[index] == 0]
            if (
                len(positives) < COLLEGE_MISSINGNESS_AUDIT_MIN_POSITIVES
                or len(negatives) < COLLEGE_MISSINGNESS_AUDIT_MIN_NEGATIVES
            ):
                continue
            positive_coverage = sum(
                parse_number(rows[index].get(feature)) is not None for index in positives
            ) / len(positives)
            negative_coverage = sum(
                parse_number(rows[index].get(feature)) is not None for index in negatives
            ) / len(negatives)
            gap = abs(positive_coverage - negative_coverage)
            if gap >= COLLEGE_MISSINGNESS_AUDIT_GAP:
                flagged.append((year, gap))
        if flagged:
            year_text = ", ".join(str(year) for year, _gap in flagged)
            maximum_gap = max(gap for _year, gap in flagged)
            flagged_features.append(
                f"{feature} in {year_text} (maximum gap {maximum_gap:.0%})"
            )
    if not flagged_features:
        return ()
    return (
        "Source-availability audit: drafted/undrafted coverage differs for "
        + "; ".join(flagged_features)
        + ". Implicit missingness indicators are disabled for the college stage.",
    )


def _fit_college_context(
    rows: Sequence[Mapping[str, Any]], labels: Sequence[int]
) -> _CollegeContextEncoder:
    conference_counts: dict[str, tuple[int, int]] = {}
    school_counts: dict[str, tuple[int, int]] = {}
    for row, label in zip(rows, labels):
        for field, counts in (
            ("conference", conference_counts),
            ("school", school_counts),
        ):
            key = normalize_name(row.get(field))
            if not key:
                continue
            positives, total = counts.get(key, (0, 0))
            counts[key] = (positives + label, total + 1)
    return _CollegeContextEncoder(
        positives=sum(labels),
        rows=len(labels),
        conference_counts=conference_counts,
        school_counts=school_counts,
    )


def _prepare_college_row(
    row: Mapping[str, Any],
    encoder: _CollegeContextEncoder,
    *,
    label: int | None = None,
) -> dict[str, Any]:
    prepared = dict(row)
    prepared["class_year_numeric"] = _class_year_number(row.get("class_year"))
    height = parse_number(row.get("height_in"))
    weight = parse_number(row.get("weight_lb"))
    if height is not None and height > 0 and weight is not None:
        # Match records.derive_features so in-memory builds and CSV reloads
        # use exactly the same derived measurement, not rounded-vs-raw BMI.
        prepared["bmi"] = round(703.0 * weight / (height * height), 4)
    recorded_stats = _college_availability_value(prepared.get("has_recorded_stats"))
    if recorded_stats is None:
        prepared["has_recorded_stats"] = float(
            any(
                key.startswith("prod_") and parse_number(value) is not None
                for key, value in row.items()
            )
        )
    else:
        prepared["has_recorded_stats"] = recorded_stats
    prepared["context_conference_draft_rate"] = encoder.rate(
        row.get("conference"),
        encoder.conference_counts,
        smoothing=30.0,
        label=label,
    )
    prepared["context_school_draft_rate"] = encoder.rate(
        row.get("school"),
        encoder.school_counts,
        smoothing=12.0,
        label=label,
    )
    return prepared


def _college_availability_value(value: object) -> float | None:
    """Normalize CSV booleans and numeric 0/1 availability flags."""

    try:
        parsed_bool = parse_bool(value)
    except DataError:
        try:
            parsed_number = parse_number(value)
        except DataError as exc:
            raise ModelError(f"Invalid has_recorded_stats value {value!r}") from exc
        if parsed_number not in (0.0, 1.0, None):
            raise ModelError(f"Invalid has_recorded_stats value {value!r}; expected true/false or 0/1")
        return parsed_number
    return None if parsed_bool is None else float(parsed_bool)


def _class_year_number(value: object) -> float | None:
    try:
        numeric = parse_number(value)
    except DataError:
        numeric = None
    if numeric is not None:
        return numeric
    key = normalize_name(value)
    mapping = {
        "fr": 1.0,
        "freshman": 1.0,
        "rfr": 1.5,
        "redshirtfreshman": 1.5,
        "so": 2.0,
        "sophomore": 2.0,
        "rso": 2.5,
        "redshirtsophomore": 2.5,
        "jr": 3.0,
        "junior": 3.0,
        "rjr": 3.5,
        "redshirtjunior": 3.5,
        "sr": 4.0,
        "senior": 4.0,
        "rsr": 4.5,
        "redshirtsenior": 4.5,
        "gr": 5.0,
        "grad": 5.0,
        "graduate": 5.0,
    }
    return mapping.get(key)


def _case_control_sample(
    rows: Sequence[Mapping[str, Any]],
    labels: Sequence[int],
    *,
    negative_ratio: int,
    minimum_negatives: int,
    maximum_negatives: int,
) -> tuple[list[Mapping[str, Any]], list[int]]:
    positive_indices = [index for index, label in enumerate(labels) if label == 1]
    negative_indices = [index for index, label in enumerate(labels) if label == 0]
    negative_limit = min(
        len(negative_indices),
        maximum_negatives,
        max(minimum_negatives, len(positive_indices) * negative_ratio),
    )

    def stable_key(index: int) -> bytes:
        row = rows[index]
        raw_year = row.get("draft_year") or row.get("season")
        try:
            numeric_year = parse_number(raw_year)
        except DataError:
            numeric_year = None
        year_key = (
            format(numeric_year, ".15g")
            if numeric_year is not None
            else str(raw_year or "").strip()
        )
        identity = (
            f"{str(row.get('player_id') or row.get('name') or index).strip()}|"
            f"{year_key}|{index}"
        )
        return hashlib.sha256(identity.encode("utf-8")).digest()

    kept_negatives = sorted(negative_indices, key=stable_key)[:negative_limit]
    selected = sorted([*positive_indices, *kept_negatives])
    return [rows[index] for index in selected], [labels[index] for index in selected]


def _correct_case_control_intercept(
    model: LogisticModel,
    full_labels: Sequence[int],
    sampled_labels: Sequence[int],
) -> None:
    full_prevalence = sum(full_labels) / len(full_labels)
    sampled_prevalence = sum(sampled_labels) / len(sampled_labels)
    if 0.0 < full_prevalence < 1.0 and 0.0 < sampled_prevalence < 1.0:
        model.weights[0] += logit(full_prevalence) - logit(sampled_prevalence)


def _expected_calibration_error(
    labels: Sequence[int],
    probabilities: Sequence[float],
    *,
    bins: int = CALIBRATION_BINS,
) -> float | None:
    """Return equal-width expected calibration error as a descriptive metric."""

    if not labels or len(labels) != len(probabilities) or bins < 1:
        return None
    bucket_labels: list[list[int]] = [[] for _ in range(bins)]
    bucket_probabilities: list[list[float]] = [[] for _ in range(bins)]
    for label, probability in zip(labels, probabilities):
        if not math.isfinite(float(probability)):
            return None
        bounded = min(1.0, max(0.0, float(probability)))
        index = min(bins - 1, int(bounded * bins))
        bucket_labels[index].append(int(label))
        bucket_probabilities[index].append(bounded)
    error = 0.0
    for bin_labels, bin_probabilities in zip(bucket_labels, bucket_probabilities):
        if not bin_labels:
            continue
        observed_rate = sum(bin_labels) / len(bin_labels)
        mean_probability = sum(bin_probabilities) / len(bin_probabilities)
        error += len(bin_labels) / len(labels) * abs(observed_rate - mean_probability)
    return error


def _draft_board_metrics(
    rows: Sequence[Mapping[str, Any]],
    indices: Sequence[int],
    probabilities: Sequence[float],
    *,
    cutoffs: Sequence[int],
) -> tuple[dict[str, Any], ...]:
    """Rank held-out scores within position and draft year.

    The enclosing model is position-specific, so these are not overall-board
    metrics. Draft round is evaluation-only and never becomes a model feature.
    """

    if not indices or len(indices) != len(probabilities):
        return ()
    groups: dict[int, list[tuple[int, float, int, int | None]]] = {}
    for index, probability in zip(indices, probabilities):
        if index < 0 or index >= len(rows):
            continue
        row = rows[index]
        year_value = parse_number(row.get("draft_year") or row.get("season"))
        label_value = parse_bool(row.get("drafted"))
        if year_value is None or label_value is None:
            continue
        round_value = parse_number(row.get("draft_round"))
        groups.setdefault(int(year_value), []).append(
            (
                index,
                float(probability),
                1 if label_value else 0,
                int(round_value) if round_value is not None else None,
            )
        )
    if not groups:
        return ()

    results: list[dict[str, Any]] = []
    for cutoff in sorted({int(value) for value in cutoffs if int(value) > 0}):
        selected_rows = 0
        drafted_rows = 0
        drafted_hits = 0
        drafted_with_round = 0
        early_round_rows = 0
        early_round_hits = 0
        ndcg_values: list[float] = []
        years_scored: list[int] = []
        for year, group in sorted(groups.items()):
            ranked = sorted(group, key=lambda item: (-item[1], item[0]))
            selected = ranked[:cutoff]
            selected_ids = {item[0] for item in selected}
            selected_rows += len(selected)
            year_positives = sum(item[2] for item in ranked)
            drafted_rows += year_positives
            drafted_hits += sum(item[2] for item in selected)
            drafted_with_round += sum(
                item[2] == 1 and item[3] is not None for item in ranked
            )
            early = [
                item
                for item in ranked
                if item[2] == 1 and item[3] is not None and item[3] <= 3
            ]
            early_round_rows += len(early)
            early_round_hits += sum(item[0] in selected_ids for item in early)
            ndcg = _binary_ndcg_at_k([item[2] for item in ranked], cutoff)
            if ndcg is not None:
                ndcg_values.append(ndcg)
                years_scored.append(year)

        round_coverage = drafted_with_round / drafted_rows if drafted_rows else None
        complete_round_outcomes = drafted_rows > 0 and drafted_with_round == drafted_rows
        results.append(
            {
                "cutoff": cutoff,
                "cutoff_scope": "top-k per position per evaluated draft year",
                "evaluated_years": tuple(sorted(groups)),
                "selected_rows": selected_rows,
                "drafted_rows": drafted_rows,
                "drafted_hits": drafted_hits,
                "precision": drafted_hits / selected_rows if selected_rows else None,
                "recall": drafted_hits / drafted_rows if drafted_rows else None,
                "mean_binary_ndcg": (
                    sum(ndcg_values) / len(ndcg_values) if ndcg_values else None
                ),
                "ndcg_years": tuple(years_scored),
                "draft_round_outcome_coverage": round_coverage,
                "early_round_definition": "draft rounds 1-3",
                "early_round_rows": early_round_rows,
                "early_round_hits": early_round_hits,
                "early_round_recall": (
                    early_round_hits / early_round_rows
                    if complete_round_outcomes and early_round_rows
                    else None
                ),
            }
        )
    return tuple(results)


def _binary_ndcg_at_k(labels_in_rank_order: Sequence[int], cutoff: int) -> float | None:
    positives = sum(1 for label in labels_in_rank_order if label)
    if positives == 0 or cutoff < 1:
        return None
    dcg = sum(
        int(bool(label)) / math.log2(rank + 1)
        for rank, label in enumerate(labels_in_rank_order[:cutoff], start=1)
    )
    ideal_hits = min(positives, cutoff)
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return dcg / ideal if ideal else None


def _f1_at_threshold(labels: Sequence[int], probabilities: Sequence[float], threshold: float) -> float:
    tp = sum(label == 1 and probability >= threshold for label, probability in zip(labels, probabilities))
    fp = sum(label == 0 and probability >= threshold for label, probability in zip(labels, probabilities))
    fn = sum(label == 1 and probability < threshold for label, probability in zip(labels, probabilities))
    denominator = 2 * tp + fp + fn
    return 2 * tp / denominator if denominator else 0.0

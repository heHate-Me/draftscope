from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import tempfile
from typing import Any, Mapping, Sequence

from .records import DataError, normalize_name


TRACKING_SCHEMA_VERSION = 1
MODEL_RELEASE_PREFIX = "model_"
PROMOTION_DECISION_PREFIX = "decision_"
FORECAST_RUN_PREFIX = "forecast_"


class TrackingError(DataError):
    """Raised when a tracking artifact is invalid, inconsistent, or altered."""


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    """Conservative automatic-promotion guardrails.

    A replacement release must improve Brier score, preserve the other
    validation metrics, and (by default) provide a paired draft-year bootstrap
    interval showing positive Brier improvement over the current champion.
    """

    required_validation_scheme: str = "expanding_window_past_only"
    minimum_relative_brier_improvement: float = 0.005
    minimum_absolute_brier_improvement: float = 0.0005
    maximum_relative_log_loss_regression: float = 0.005
    maximum_absolute_log_loss_regression: float = 0.002
    maximum_average_precision_regression: float = 0.01
    maximum_roc_auc_regression: float = 0.01
    require_paired_brier_evidence: bool = True


@dataclass(frozen=True, slots=True)
class ModelRelease:
    release_id: str
    registry_key: str
    path: Path
    content: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    decision_id: str
    registry_key: str
    candidate_release_id: str
    champion_release_id: str | None
    status: str
    reasons: tuple[str, ...]
    path: Path


@dataclass(frozen=True, slots=True)
class ForecastRun:
    run_id: str
    season: int
    week: int
    path: Path
    manifest_path: Path
    forecasts_path: Path
    forecast_count: int


class TrackingStore:
    """Content-addressed model registry and immutable forecast ledger.

    Model releases and promotion decisions are write-once JSON envelopes.
    Forecasts are stored in content-addressed run directories. The champion
    index is the sole mutable artifact and is always replaced atomically.
    """

    def __init__(self, output_dir: str | os.PathLike[str]):
        self.output_dir = Path(output_dir).resolve()
        self.registry_dir = self.output_dir / "model_registry"
        self.releases_dir = self.registry_dir / "releases"
        self.decisions_dir = self.registry_dir / "decisions"
        self.champions_path = self.registry_dir / "champions.json"
        self.ledger_dir = self.output_dir / "forecast_ledger"

    def register_model_release(self, manifest: Mapping[str, Any]) -> ModelRelease:
        content = _normalize_release_manifest(manifest)
        content_hash = canonical_sha256(content)
        release_id = f"{MODEL_RELEASE_PREFIX}{content_hash}"
        content["release_id"] = release_id
        path = self.releases_dir / f"{release_id}.json"
        envelope = _new_envelope("model_release", release_id, content)
        _write_immutable_envelope(path, envelope)
        verified = _read_verified_envelope(
            path,
            expected_type="model_release",
            expected_id=release_id,
        )
        return ModelRelease(
            release_id=release_id,
            registry_key=str(content["registry_key"]),
            path=path,
            content=dict(verified["content"]),
        )

    def get_model_release(self, release_id: str) -> ModelRelease:
        _validate_artifact_id(release_id, MODEL_RELEASE_PREFIX)
        path = self.releases_dir / f"{release_id}.json"
        envelope = _read_verified_envelope(
            path,
            expected_type="model_release",
            expected_id=release_id,
        )
        content = dict(envelope["content"])
        expected = _release_id_for_content(content)
        if expected != release_id:
            raise TrackingError(
                f"Model release content does not match its id: expected {expected}, got {release_id}"
            )
        return ModelRelease(
            release_id=release_id,
            registry_key=str(content.get("registry_key") or ""),
            path=path,
            content=content,
        )

    def champion_release_id(self, registry_key: str) -> str | None:
        index = self._load_champion_index()
        item = index.get("champions", {}).get(registry_key)
        if not isinstance(item, Mapping):
            return None
        release_id = str(item.get("release_id") or "")
        if not release_id:
            return None
        release = self.get_model_release(release_id)
        if release.registry_key != registry_key:
            raise TrackingError(
                f"Champion {release_id} belongs to {release.registry_key!r}, not {registry_key!r}"
            )
        return release_id

    def decide_promotion(
        self,
        candidate_release_id: str,
        *,
        policy: PromotionPolicy | None = None,
    ) -> PromotionDecision:
        policy = policy or PromotionPolicy()
        candidate = self.get_model_release(candidate_release_id)
        champion_id = self.champion_release_id(candidate.registry_key)
        champion = self.get_model_release(champion_id) if champion_id else None
        status, reasons = _promotion_result(candidate, champion, policy)
        decision_content: dict[str, Any] = {
            "schema_version": TRACKING_SCHEMA_VERSION,
            "registry_key": candidate.registry_key,
            "candidate_release_id": candidate.release_id,
            "champion_release_id": champion.release_id if champion else None,
            "status": status,
            "reasons": list(reasons),
            "policy": asdict(policy),
        }
        decision_hash = canonical_sha256(decision_content)
        decision_id = f"{PROMOTION_DECISION_PREFIX}{decision_hash}"
        decision_content["decision_id"] = decision_id
        path = self.decisions_dir / f"{decision_id}.json"
        _write_immutable_envelope(
            path,
            _new_envelope("promotion_decision", decision_id, decision_content),
        )
        if status == "promoted":
            self._set_champion(candidate, decision_id)
        return PromotionDecision(
            decision_id=decision_id,
            registry_key=candidate.registry_key,
            candidate_release_id=candidate.release_id,
            champion_release_id=champion.release_id if champion else None,
            status=status,
            reasons=tuple(reasons),
            path=path,
        )

    def record_forecast_run(
        self,
        *,
        season: int,
        week: int,
        forecasts: Sequence[Mapping[str, Any]],
        input_artifacts: Mapping[str, Any],
        metadata: Mapping[str, Any] | None = None,
    ) -> ForecastRun:
        if season < 1900 or season > 2200:
            raise TrackingError(f"Invalid forecast season: {season}")
        if week < 0 or week > 30:
            raise TrackingError(f"Invalid completed week: {week}")
        normalized_forecasts = [
            _normalize_forecast(row, store=self) for row in forecasts
        ]
        normalized_forecasts.sort(key=lambda row: str(row["forecast_key"]))
        keys = [str(row["forecast_key"]) for row in normalized_forecasts]
        if len(keys) != len(set(keys)):
            duplicates = sorted({key for key in keys if keys.count(key) > 1})
            raise TrackingError(
                f"Forecast run contains duplicate player/model records: {', '.join(duplicates[:5])}"
            )
        release_ids = sorted(
            {str(row["model_release_id"]) for row in normalized_forecasts}
        )
        normalized_inputs = _jsonable(input_artifacts)
        normalized_metadata = _jsonable(metadata or {})
        _validate_portable_values(normalized_inputs, label="forecast input artifacts")
        _validate_portable_values(normalized_metadata, label="forecast metadata")
        _validate_fingerprint_tree(normalized_inputs, label="forecast input artifacts")
        semantic_content = {
            "schema_version": TRACKING_SCHEMA_VERSION,
            "checkpoint": {"season": int(season), "week": int(week)},
            "model_release_ids": release_ids,
            "input_artifacts": normalized_inputs,
            "metadata": normalized_metadata,
            "forecasts": normalized_forecasts,
        }
        semantic_hash = canonical_sha256(semantic_content)
        run_id = f"{FORECAST_RUN_PREFIX}{semantic_hash}"
        week_dir = self.ledger_dir / str(season) / f"week_{week:02d}"
        destination = week_dir / run_id
        if destination.exists():
            return self.verify_forecast_run(destination)

        week_dir.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=week_dir))
        try:
            forecasts_path = temporary / "forecasts.json"
            forecast_text = _pretty_json(normalized_forecasts)
            forecasts_path.write_text(forecast_text, encoding="utf-8")
            forecast_artifact = {
                "path": "forecasts.json",
                "sha256": _bytes_sha256(forecast_text.encode("utf-8")),
                "bytes": len(forecast_text.encode("utf-8")),
                "rows": len(normalized_forecasts),
            }
            manifest_content = {
                "schema_version": TRACKING_SCHEMA_VERSION,
                "run_id": run_id,
                "semantic_sha256": semantic_hash,
                "checkpoint": semantic_content["checkpoint"],
                "model_release_ids": release_ids,
                "input_artifacts": semantic_content["input_artifacts"],
                "metadata": semantic_content["metadata"],
                "forecast_artifact": forecast_artifact,
            }
            manifest_path = temporary / "manifest.json"
            manifest_path.write_text(
                _pretty_json(_new_envelope("forecast_run", run_id, manifest_content)),
                encoding="utf-8",
            )
            try:
                os.rename(temporary, destination)
            except FileExistsError:
                shutil.rmtree(temporary)
            return self.verify_forecast_run(destination)
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise

    def verify_forecast_run(self, path: str | os.PathLike[str]) -> ForecastRun:
        directory = Path(path).resolve()
        run_id = directory.name
        _validate_artifact_id(run_id, FORECAST_RUN_PREFIX)
        manifest_path = directory / "manifest.json"
        forecasts_path = directory / "forecasts.json"
        envelope = _read_verified_envelope(
            manifest_path,
            expected_type="forecast_run",
            expected_id=run_id,
        )
        content = envelope["content"]
        artifact = content.get("forecast_artifact")
        if not isinstance(artifact, Mapping):
            raise TrackingError(f"Forecast run {run_id} has no forecast artifact declaration")
        if str(artifact.get("path") or "") != "forecasts.json":
            raise TrackingError(f"Forecast run {run_id} declares an invalid forecast path")
        if not forecasts_path.is_file():
            raise TrackingError(f"Forecast run {run_id} is missing forecasts.json")
        raw = forecasts_path.read_bytes()
        if _bytes_sha256(raw) != str(artifact.get("sha256") or ""):
            raise TrackingError(f"Forecast run {run_id} forecasts.json failed SHA-256 verification")
        try:
            expected_bytes = int(artifact.get("bytes"))
            expected_rows = int(artifact.get("rows"))
        except (TypeError, ValueError) as exc:
            raise TrackingError(f"Forecast run {run_id} has invalid artifact counts") from exc
        if len(raw) != expected_bytes:
            raise TrackingError(f"Forecast run {run_id} forecasts.json has an unexpected byte count")
        try:
            forecasts = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TrackingError(f"Forecast run {run_id} contains invalid forecast JSON") from exc
        if not isinstance(forecasts, list) or len(forecasts) != expected_rows:
            raise TrackingError(f"Forecast run {run_id} forecast row count does not match its manifest")
        checkpoint = content.get("checkpoint")
        if not isinstance(checkpoint, Mapping):
            raise TrackingError(f"Forecast run {run_id} has no checkpoint")
        semantic_content = {
            "schema_version": TRACKING_SCHEMA_VERSION,
            "checkpoint": dict(checkpoint),
            "model_release_ids": list(content.get("model_release_ids") or []),
            "input_artifacts": content.get("input_artifacts") or {},
            "metadata": content.get("metadata") or {},
            "forecasts": forecasts,
        }
        semantic_hash = canonical_sha256(semantic_content)
        if semantic_hash != str(content.get("semantic_sha256") or ""):
            raise TrackingError(f"Forecast run {run_id} semantic hash does not match its contents")
        if run_id != f"{FORECAST_RUN_PREFIX}{semantic_hash}":
            raise TrackingError(f"Forecast run directory {run_id} does not match its contents")
        declared_release_ids = set(content.get("model_release_ids") or [])
        row_release_ids = {
            str(row.get("model_release_id") or "")
            for row in forecasts
            if isinstance(row, Mapping)
        }
        if declared_release_ids != row_release_ids:
            raise TrackingError(f"Forecast run {run_id} model release list does not match its rows")
        for release_id in declared_release_ids:
            self.get_model_release(str(release_id))
        try:
            season = int(checkpoint.get("season"))
            week = int(checkpoint.get("week"))
        except (TypeError, ValueError) as exc:
            raise TrackingError(f"Forecast run {run_id} has an invalid checkpoint") from exc
        return ForecastRun(
            run_id=run_id,
            season=season,
            week=week,
            path=directory,
            manifest_path=manifest_path,
            forecasts_path=forecasts_path,
            forecast_count=len(forecasts),
        )

    def audit(self) -> dict[str, Any]:
        findings: list[str] = []
        counts = {"model_releases": 0, "promotion_decisions": 0, "forecast_runs": 0}
        for path in sorted(self.releases_dir.glob(f"{MODEL_RELEASE_PREFIX}*.json")):
            counts["model_releases"] += 1
            try:
                self.get_model_release(path.stem)
            except (OSError, TrackingError, json.JSONDecodeError) as exc:
                findings.append(f"{path}: {exc}")
        for path in sorted(self.decisions_dir.glob(f"{PROMOTION_DECISION_PREFIX}*.json")):
            counts["promotion_decisions"] += 1
            try:
                envelope = _read_verified_envelope(
                    path,
                    expected_type="promotion_decision",
                    expected_id=path.stem,
                )
                content = envelope["content"]
                self.get_model_release(str(content.get("candidate_release_id") or ""))
                champion_id = str(content.get("champion_release_id") or "")
                if champion_id:
                    self.get_model_release(champion_id)
            except (OSError, TrackingError, json.JSONDecodeError) as exc:
                findings.append(f"{path}: {exc}")
        try:
            index = self._load_champion_index()
            for key, item in (index.get("champions") or {}).items():
                if not isinstance(item, Mapping):
                    raise TrackingError(f"Champion index entry {key!r} is not an object")
                release = self.get_model_release(str(item.get("release_id") or ""))
                if release.registry_key != key:
                    raise TrackingError(f"Champion index key {key!r} does not match {release.release_id}")
        except (OSError, TrackingError, json.JSONDecodeError) as exc:
            findings.append(f"{self.champions_path}: {exc}")
        for path in sorted(self.ledger_dir.glob(f"*/week_*/{FORECAST_RUN_PREFIX}*")):
            if not path.is_dir():
                continue
            counts["forecast_runs"] += 1
            try:
                self.verify_forecast_run(path)
            except (OSError, TrackingError, json.JSONDecodeError) as exc:
                findings.append(f"{path}: {exc}")
        return {"ok": not findings, "counts": counts, "findings": findings}

    def status(self) -> dict[str, Any]:
        """Return a read-only registry/ledger and validation-health summary."""

        audit = self.audit()
        health = {"passed": 0, "withheld": 0, "unknown": 0}
        schemes: dict[str, int] = {}
        stages: dict[str, int] = {}
        positions: dict[str, int] = {}
        latest_by_registry: dict[str, ModelRelease] = {}
        valid_releases: dict[str, ModelRelease] = {}
        for path in sorted(self.releases_dir.glob(f"{MODEL_RELEASE_PREFIX}*.json")):
            try:
                release = self.get_model_release(path.stem)
            except (OSError, TrackingError, json.JSONDecodeError):
                continue
            valid_releases[release.release_id] = release
            validation = release.content.get("validation")
            if not isinstance(validation, Mapping):
                health["unknown"] += 1
            elif validation.get("passes_quality_gate") is True:
                health["passed"] += 1
            elif validation.get("passes_quality_gate") is False:
                health["withheld"] += 1
            else:
                health["unknown"] += 1
            scheme = str((validation or {}).get("scheme") or "unknown")
            schemes[scheme] = schemes.get(scheme, 0) + 1
            stage = str(release.content.get("model_stage") or "unknown")
            stages[stage] = stages.get(stage, 0) + 1
            position = str(release.content.get("position") or "unknown")
            positions[position] = positions.get(position, 0) + 1
            previous = latest_by_registry.get(release.registry_key)
            if previous is None or _release_recency(release) > _release_recency(previous):
                latest_by_registry[release.registry_key] = release

        report_releases = tuple(latest_by_registry.values())
        state_path = self.output_dir / "state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state_release_ids = tuple(
                dict.fromkeys(str(value) for value in state.get("model_release_ids") or ())
            )
        except (AttributeError, OSError, TypeError, json.JSONDecodeError):
            state_release_ids = ()
        if state_release_ids and all(
            release_id in valid_releases for release_id in state_release_ids
        ):
            report_releases = tuple(
                valid_releases[release_id] for release_id in state_release_ids
            )

        champion_ids: dict[str, str] = {}
        try:
            index = self._load_champion_index()
            for key, item in (index.get("champions") or {}).items():
                if isinstance(item, Mapping) and item.get("release_id"):
                    champion_ids[str(key)] = str(item["release_id"])
        except (OSError, TrackingError, json.JSONDecodeError):
            # The audit already exposes the exact integrity failure.
            champion_ids = {}

        checkpoints: list[tuple[int, int]] = []
        for path in self.ledger_dir.glob(f"*/week_*/{FORECAST_RUN_PREFIX}*"):
            if not path.is_dir():
                continue
            try:
                run = self.verify_forecast_run(path)
            except (OSError, TrackingError, json.JSONDecodeError):
                continue
            checkpoints.append((run.season, run.week))
        latest = max(checkpoints) if checkpoints else None
        return {
            **audit,
            "champion_count": len(champion_ids),
            "champions": champion_ids,
            "validation_health": health,
            "validation_schemes": dict(sorted(schemes.items())),
            "model_stages": dict(sorted(stages.items())),
            "positions": dict(sorted(positions.items())),
            "latest_validation_report": _latest_validation_report(report_releases),
            "latest_forecast_checkpoint": (
                {"season": latest[0], "week": latest[1]} if latest else None
            ),
        }

    def _set_champion(self, release: ModelRelease, decision_id: str) -> None:
        index = self._load_champion_index()
        champions = dict(index.get("champions") or {})
        champions[release.registry_key] = {
            "release_id": release.release_id,
            "decision_id": decision_id,
        }
        content = {
            "schema_version": TRACKING_SCHEMA_VERSION,
            "champions": champions,
        }
        envelope = _new_envelope("champion_index", "champions", content)
        _atomic_write_text(self.champions_path, _pretty_json(envelope))

    def _load_champion_index(self) -> dict[str, Any]:
        if not self.champions_path.exists():
            return {"schema_version": TRACKING_SCHEMA_VERSION, "champions": {}}
        envelope = _read_verified_envelope(
            self.champions_path,
            expected_type="champion_index",
            expected_id="champions",
        )
        content = envelope.get("content")
        if not isinstance(content, Mapping):
            raise TrackingError("Champion index content must be an object")
        champions = content.get("champions")
        if not isinstance(champions, Mapping):
            raise TrackingError("Champion index champions must be an object")
        return dict(content)


def _release_recency(release: ModelRelease) -> tuple[int, int, int, str]:
    checkpoint = release.content.get("checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, Mapping) else {}
    validation = release.content.get("validation")
    validation = validation if isinstance(validation, Mapping) else {}

    def integer(value: Any) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return -1

    season_value = checkpoint.get("target_season")
    if season_value is None:
        season_value = checkpoint.get("season")
    week_value = checkpoint.get("as_of_week")
    if week_value is None:
        week_value = checkpoint.get("week")
    return (
        integer(season_value),
        integer(week_value),
        integer(validation.get("validation_schema_version")),
        release.release_id,
    )


def _latest_validation_report(releases: Sequence[ModelRelease]) -> dict[str, Any]:
    """Build a stable machine-readable report from latest immutable releases."""

    checkpoint_keys = {_release_recency(release)[:2] for release in releases}
    latest_checkpoint = max(checkpoint_keys, default=(-1, -1))
    releases = tuple(
        release
        for release in releases
        if _release_recency(release)[:2] == latest_checkpoint
    )
    latest_by_model: dict[tuple[str, str, str], ModelRelease] = {}
    for release in releases:
        key = (
            str(release.content.get("model_stage") or "unknown"),
            str(release.content.get("position") or "unknown"),
            str(release.content.get("probability_kind") or "unknown"),
        )
        previous = latest_by_model.get(key)
        if previous is None or _release_recency(release) > _release_recency(previous):
            latest_by_model[key] = release
    releases = tuple(latest_by_model.values())

    metric_names = (
        "brier",
        "baseline_brier",
        "log_loss",
        "roc_auc",
        "average_precision",
        "expected_calibration_error",
    )
    per_model: list[dict[str, Any]] = []
    for release in sorted(
        releases,
        key=lambda item: (
            str(item.content.get("model_stage") or ""),
            str(item.content.get("position") or ""),
            item.registry_key,
        ),
    ):
        content = release.content
        validation = content.get("validation")
        validation = dict(validation) if isinstance(validation, Mapping) else {}
        per_model.append(
            {
                "release_id": release.release_id,
                "registry_key": release.registry_key,
                "position": str(content.get("position") or "unknown"),
                "model_stage": str(content.get("model_stage") or "unknown"),
                "probability_kind": str(content.get("probability_kind") or "unknown"),
                "population": str(content.get("population") or "unknown"),
                "algorithm_id": str(content.get("algorithm_id") or "unknown"),
                "checkpoint": dict(content.get("checkpoint") or {}),
                "training_rows": content.get("training_rows"),
                "selected_features": list(content.get("selected_features") or ()),
                "validation": validation,
            }
        )

    stages: dict[str, dict[str, Any]] = {}
    baseline_definitions: dict[str, dict[str, Any]] = {}
    withheld: list[dict[str, Any]] = []
    for item in per_model:
        stage_name = item["model_stage"]
        stage = stages.setdefault(
            stage_name,
            {
                "models": 0,
                "passed": 0,
                "withheld": 0,
                "unknown": 0,
                "rows": 0,
                "positives": 0,
                "positions": [],
                "evaluated_years": set(),
                "metric_values": {name: [] for name in metric_names},
            },
        )
        validation = item["validation"]
        stage["models"] += 1
        stage["rows"] += int(validation.get("rows") or 0)
        stage["positives"] += int(validation.get("positives") or 0)
        stage["positions"].append(item["position"])
        stage["evaluated_years"].update(validation.get("evaluated_years") or ())
        passed = validation.get("passes_quality_gate")
        if passed is True:
            stage["passed"] += 1
        elif passed is False:
            stage["withheld"] += 1
            withheld.append(
                {
                    "position": item["position"],
                    "model_stage": stage_name,
                    "quality_message": validation.get("quality_message"),
                    "rows": validation.get("rows"),
                    "positives": validation.get("positives"),
                }
            )
        else:
            stage["unknown"] += 1
        for metric in metric_names:
            try:
                value = float(validation.get(metric))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                stage["metric_values"][metric].append(value)
        for baseline in validation.get("simple_baselines") or ():
            if not isinstance(baseline, Mapping):
                continue
            name = str(baseline.get("name") or "").strip()
            if name:
                baseline_definitions[name] = {
                    "name": name,
                    "label": baseline.get("label"),
                    "definition": baseline.get("definition"),
                    "validation_scheme": baseline.get("validation_scheme"),
                }

    stage_summaries: dict[str, dict[str, Any]] = {}
    for stage_name, stage in sorted(stages.items()):
        metric_values = stage.pop("metric_values")
        stage_summaries[stage_name] = {
            **stage,
            "positions": sorted(set(stage["positions"])),
            "evaluated_years": sorted(stage["evaluated_years"]),
            "macro_average_metrics": {
                metric: (sum(values) / len(values) if values else None)
                for metric, values in metric_values.items()
            },
        }
    return {
        "source": (
            "current state release set when available; otherwise the newest "
            "content-addressed release per position, stage, and probability contract"
        ),
        "aggregation_note": (
            "Macro averages are unweighted across model records. Stage row and positive "
            "counts are sums and may overlap where displayed roles share a source cohort "
            "(for example OT and IOL)."
        ),
        "checkpoint": (
            {"season": latest_checkpoint[0], "week": latest_checkpoint[1]}
            if latest_checkpoint != (-1, -1)
            else None
        ),
        "model_count": len(per_model),
        "stage_summaries": stage_summaries,
        "baseline_definitions": [
            baseline_definitions[name] for name in sorted(baseline_definitions)
        ],
        "withheld_models": sorted(
            withheld,
            key=lambda item: (item["model_stage"], item["position"]),
        ),
        "models": per_model,
    }


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _bytes_sha256(payload)


def artifact_fingerprint(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Fingerprint an artifact without persisting its machine-local path.

    Content hashes identify immutable inputs; an absolute path is neither part of
    that identity nor portable across checkouts.  Keeping only the filename also
    avoids leaking a user's home directory through model and forecast manifests.
    """

    source = Path(path).resolve()
    if not source.is_file():
        raise TrackingError(f"Artifact does not exist: {source}")
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return {"path": source.name, "sha256": digest.hexdigest(), "bytes": size}


def _normalize_release_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, Mapping):
        raise TrackingError("Model release manifest must be an object")
    content = dict(_jsonable(manifest))
    _validate_portable_values(content, label="model release")
    content.pop("release_id", None)
    content["schema_version"] = TRACKING_SCHEMA_VERSION
    for field in ("position", "contract_id", "model_stage", "algorithm_id"):
        value = str(content.get(field) or "").strip()
        if not value:
            raise TrackingError(f"Model release requires {field}")
        content[field] = value
    checkpoint = content.get("checkpoint")
    if not isinstance(checkpoint, Mapping) or not checkpoint:
        raise TrackingError("Model release requires a non-empty checkpoint object")
    content["checkpoint"] = dict(checkpoint)
    training_artifact = content.get("training_artifact")
    if not isinstance(training_artifact, Mapping):
        raise TrackingError("Model release requires a training_artifact object")
    _validate_fingerprint(training_artifact, label="training_artifact")
    content["training_artifact"] = dict(training_artifact)
    validation = content.get("validation")
    if not isinstance(validation, Mapping):
        raise TrackingError("Model release requires a validation object")
    content["validation"] = dict(validation)
    registry_key = str(content.get("registry_key") or "").strip()
    if not registry_key:
        key_content = {
            "position": content["position"],
            "contract_id": content["contract_id"],
            "model_stage": content["model_stage"],
            "checkpoint": content["checkpoint"],
        }
        registry_key = f"registry_{canonical_sha256(key_content)}"
    content["registry_key"] = registry_key
    return content


def _normalize_forecast(row: Mapping[str, Any], *, store: TrackingStore) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise TrackingError("Each forecast must be an object")
    normalized = dict(_jsonable(row))
    release_id = str(normalized.get("model_release_id") or "").strip()
    if not release_id:
        raise TrackingError("Each forecast requires model_release_id")
    release = store.get_model_release(release_id)
    for field in ("position", "contract_id", "probability_kind", "probability_scope"):
        value = str(normalized.get(field) or "").strip()
        if not value:
            raise TrackingError(f"Each forecast requires {field}")
        normalized[field] = value
    if normalized["position"] != str(release.content.get("position") or ""):
        raise TrackingError(
            f"Forecast position {normalized['position']!r} does not match release {release_id}"
        )
    if normalized["contract_id"] != str(release.content.get("contract_id") or ""):
        raise TrackingError(
            f"Forecast contract {normalized['contract_id']!r} does not match release {release_id}"
        )
    identity = _forecast_identity(normalized)
    for probability_field in (
        "probability",
        "draft_probability",
        "conditional_probability",
        "entry_probability",
    ):
        value = normalized.get(probability_field)
        if value in (None, ""):
            normalized[probability_field] = None
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise TrackingError(f"Forecast {probability_field} must be numeric") from exc
        if not math.isfinite(numeric) or numeric < 0.0 or numeric > 1.0:
            raise TrackingError(f"Forecast {probability_field} must be between 0 and 1")
        normalized[probability_field] = numeric
    role = str(normalized.get("prediction_role") or "primary")
    normalized["prediction_role"] = role
    normalized["forecast_key"] = "|".join(
        (
            identity,
            normalized["position"],
            normalized["contract_id"],
            release_id,
            role,
        )
    )
    return normalized


def _forecast_identity(row: Mapping[str, Any]) -> str:
    cfbd_id = str(row.get("cfbd_player_id") or "").strip()
    if cfbd_id:
        return f"cfbd:{cfbd_id}"
    player_id = str(row.get("player_id") or "").strip()
    if player_id:
        return f"player:{player_id}"
    name = normalize_name(row.get("name"))
    school = normalize_name(row.get("school"))
    if not name or not school:
        raise TrackingError(
            "Forecast requires cfbd_player_id, player_id, or both name and school"
        )
    return f"fallback:{name}:{school}"


def _promotion_result(
    candidate: ModelRelease,
    champion: ModelRelease | None,
    policy: PromotionPolicy,
) -> tuple[str, tuple[str, ...]]:
    validation = candidate.content.get("validation")
    assert isinstance(validation, Mapping)
    eligibility = _validation_eligibility(validation, policy)
    if eligibility:
        return "not_promoted", tuple(eligibility)
    if champion is None:
        return "promoted", ("No champion existed and the release passed all eligibility gates.",)
    if champion.release_id == candidate.release_id:
        return "unchanged", ("The candidate is already the champion.",)
    champion_validation = champion.content.get("validation")
    if not isinstance(champion_validation, Mapping):
        return "not_promoted", ("The champion has no comparable validation record.",)
    candidate_set = str(validation.get("evaluation_set_id") or "")
    champion_set = str(champion_validation.get("evaluation_set_id") or "")
    if candidate_set != champion_set:
        return "not_promoted", (
            "Candidate and champion use different evaluation sets; automatic promotion is not comparable.",
        )
    reasons: list[str] = []
    candidate_metrics = _validation_metrics(validation)
    champion_metrics = _validation_metrics(champion_validation)
    required_brier_gain = max(
        policy.minimum_absolute_brier_improvement,
        abs(champion_metrics["brier"]) * policy.minimum_relative_brier_improvement,
    )
    actual_brier_gain = champion_metrics["brier"] - candidate_metrics["brier"]
    if actual_brier_gain < required_brier_gain:
        reasons.append(
            f"Brier improvement {actual_brier_gain:.6f} is below the required {required_brier_gain:.6f}."
        )
    allowed_log_loss_regression = max(
        policy.maximum_absolute_log_loss_regression,
        abs(champion_metrics["log_loss"]) * policy.maximum_relative_log_loss_regression,
    )
    if candidate_metrics["log_loss"] - champion_metrics["log_loss"] > allowed_log_loss_regression:
        reasons.append("Log loss regressed beyond the promotion guardrail.")
    if (
        champion_metrics["average_precision"] - candidate_metrics["average_precision"]
        > policy.maximum_average_precision_regression
    ):
        reasons.append("Average precision regressed beyond the promotion guardrail.")
    if (
        champion_metrics["roc_auc"] - candidate_metrics["roc_auc"]
        > policy.maximum_roc_auc_regression
    ):
        reasons.append("ROC-AUC regressed beyond the promotion guardrail.")
    if policy.require_paired_brier_evidence:
        comparison = validation.get("comparison")
        if not isinstance(comparison, Mapping):
            reasons.append("Paired Brier improvement evidence is required for automatic replacement.")
        elif str(comparison.get("against_release_id") or "") != champion.release_id:
            reasons.append("Paired comparison does not target the current champion.")
        else:
            interval = comparison.get("paired_brier_improvement_ci80")
            if not isinstance(interval, Sequence) or isinstance(interval, (str, bytes)) or len(interval) != 2:
                reasons.append("Paired Brier improvement interval is missing or invalid.")
            else:
                try:
                    low, high = float(interval[0]), float(interval[1])
                except (TypeError, ValueError):
                    reasons.append("Paired Brier improvement interval is not numeric.")
                else:
                    if not math.isfinite(low) or not math.isfinite(high) or low > high:
                        reasons.append("Paired Brier improvement interval is not finite and ordered.")
                    elif low <= 0.0:
                        reasons.append(
                            "Paired draft-year bootstrap does not show positive Brier improvement."
                        )
    if reasons:
        return "not_promoted", tuple(reasons)
    return "promoted", (
        "The challenger improved Brier score and passed every regression and paired-evidence guardrail.",
    )


def _validation_eligibility(
    validation: Mapping[str, Any], policy: PromotionPolicy
) -> list[str]:
    reasons: list[str] = []
    if validation.get("passes_quality_gate") is not True:
        reasons.append("The release did not pass its model quality gate.")
    scheme = str(validation.get("scheme") or validation.get("validation_scheme") or "")
    if scheme != policy.required_validation_scheme:
        reasons.append(
            f"Validation scheme must be {policy.required_validation_scheme!r}; got {scheme!r}."
        )
    if not str(validation.get("evaluation_set_id") or "").strip():
        reasons.append("Validation must declare an immutable evaluation_set_id.")
    try:
        _validation_metrics(validation)
    except TrackingError as exc:
        reasons.append(str(exc))
    return reasons


def _validation_metrics(validation: Mapping[str, Any]) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for field in ("brier", "log_loss", "average_precision", "roc_auc"):
        try:
            value = float(validation.get(field))
        except (TypeError, ValueError) as exc:
            raise TrackingError(f"Validation metric {field} is missing or non-numeric.") from exc
        if not math.isfinite(value):
            raise TrackingError(f"Validation metric {field} is not finite.")
        metrics[field] = value
    return metrics


def _release_id_for_content(content: Mapping[str, Any]) -> str:
    without_id = dict(content)
    without_id.pop("release_id", None)
    return f"{MODEL_RELEASE_PREFIX}{canonical_sha256(without_id)}"


def _new_envelope(artifact_type: str, artifact_id: str, content: Any) -> dict[str, Any]:
    content = _jsonable(content)
    envelope_core = {
        "schema_version": TRACKING_SCHEMA_VERSION,
        "artifact_type": artifact_type,
        "artifact_id": artifact_id,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "content_sha256": canonical_sha256(content),
        "content": content,
    }
    return {**envelope_core, "envelope_sha256": canonical_sha256(envelope_core)}


def _read_verified_envelope(
    path: Path,
    *,
    expected_type: str,
    expected_id: str,
) -> dict[str, Any]:
    if not path.is_file():
        raise TrackingError(f"Tracking artifact does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TrackingError(f"Tracking artifact is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise TrackingError(f"Tracking artifact must be an object: {path}")
    envelope_hash = str(payload.get("envelope_sha256") or "")
    envelope_core = dict(payload)
    envelope_core.pop("envelope_sha256", None)
    if canonical_sha256(envelope_core) != envelope_hash:
        raise TrackingError(f"Tracking artifact envelope failed SHA-256 verification: {path}")
    if payload.get("schema_version") != TRACKING_SCHEMA_VERSION:
        raise TrackingError(f"Unsupported tracking schema in {path}")
    if payload.get("artifact_type") != expected_type:
        raise TrackingError(f"Unexpected tracking artifact type in {path}")
    if payload.get("artifact_id") != expected_id:
        raise TrackingError(f"Tracking artifact id does not match its filename: {path}")
    content = payload.get("content")
    if canonical_sha256(content) != str(payload.get("content_sha256") or ""):
        raise TrackingError(f"Tracking artifact content failed SHA-256 verification: {path}")
    return payload


def _write_immutable_envelope(path: Path, envelope: Mapping[str, Any]) -> None:
    if path.exists():
        existing = _read_verified_envelope(
            path,
            expected_type=str(envelope.get("artifact_type") or ""),
            expected_id=str(envelope.get("artifact_id") or ""),
        )
        if existing.get("content_sha256") != envelope.get("content_sha256"):
            raise TrackingError(f"Refusing to overwrite immutable tracking artifact: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_pretty_json(envelope))
        try:
            os.link(temporary_name, path)
        except FileExistsError:
            existing = _read_verified_envelope(
                path,
                expected_type=str(envelope.get("artifact_type") or ""),
                expected_id=str(envelope.get("artifact_id") or ""),
            )
            if existing.get("content_sha256") != envelope.get("content_sha256"):
                raise TrackingError(f"Immutable artifact collision at {path}")
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _validate_fingerprint(value: Mapping[str, Any], *, label: str) -> None:
    digest = str(value.get("sha256") or "")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise TrackingError(f"{label} requires a lowercase SHA-256 digest")
    try:
        size = int(value.get("bytes"))
    except (TypeError, ValueError) as exc:
        raise TrackingError(f"{label} requires a byte count") from exc
    if size < 0:
        raise TrackingError(f"{label} byte count cannot be negative")
    if "path" in value:
        path = str(value.get("path") or "").strip()
        if not path:
            raise TrackingError(f"{label} path cannot be empty")
        _validate_portable_path(path, label=f"{label} path")


def _validate_fingerprint_tree(value: Any, *, label: str) -> None:
    if isinstance(value, Mapping):
        if "sha256" in value or "bytes" in value:
            if "sha256" not in value or "bytes" not in value:
                raise TrackingError(f"{label} contains an incomplete artifact fingerprint")
            _validate_fingerprint(value, label=label)
        for key, item in value.items():
            _validate_fingerprint_tree(item, label=f"{label}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_fingerprint_tree(item, label=f"{label}[{index}]")


def _validate_portable_values(value: Any, *, label: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _validate_portable_values(item, label=f"{label}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_portable_values(item, label=f"{label}[{index}]")
    elif isinstance(value, str) and _looks_like_absolute_path(value):
        raise TrackingError(f"{label} must not contain an absolute machine-local path")


def _validate_portable_path(value: str, *, label: str) -> None:
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute():
        raise TrackingError(f"{label} must not contain an absolute machine-local path")
    if ".." in (*posix.parts, *windows.parts) or value.startswith("~"):
        raise TrackingError(f"{label} must be a portable path without parent or home traversal")


def _looks_like_absolute_path(value: str) -> bool:
    text = value.strip()
    if not text or text.startswith(("http://", "https://")):
        return False
    return PurePosixPath(text).is_absolute() or PureWindowsPath(text).is_absolute()


def _validate_artifact_id(value: str, prefix: str) -> None:
    expected_length = len(prefix) + 64
    if (
        len(value) != expected_length
        or not value.startswith(prefix)
        or any(character not in "0123456789abcdef" for character in value[len(prefix) :])
    ):
        raise TrackingError(f"Invalid tracking artifact id: {value!r}")


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TrackingError("Tracking artifacts cannot contain NaN or infinity")
        return value
    raise TrackingError(f"Tracking artifact contains unsupported value {value!r}")


def _pretty_json(value: Any) -> str:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def _bytes_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()

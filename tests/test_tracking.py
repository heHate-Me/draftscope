from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from draftscope.tracking import (
    PromotionPolicy,
    TrackingError,
    TrackingStore,
    artifact_fingerprint,
)


def release_manifest(
    *,
    algorithm_id: str = "college_logistic_v1",
    evaluation_set_id: str = "evaluation-set-2020-2026",
    brier: float = 0.12,
    log_loss: float = 0.40,
    average_precision: float = 0.30,
    roc_auc: float = 0.70,
    passes: bool = True,
    comparison: dict[str, object] | None = None,
) -> dict[str, object]:
    validation: dict[str, object] = {
        "scheme": "expanding_window_past_only",
        "passes_quality_gate": passes,
        "evaluation_set_id": evaluation_set_id,
        "evaluated_years": [2020, 2021, 2022, 2023, 2024, 2025, 2026],
        "brier": brier,
        "log_loss": log_loss,
        "average_precision": average_precision,
        "roc_auc": roc_auc,
    }
    if comparison is not None:
        validation["comparison"] = comparison
    return {
        "position": "WR",
        "contract_id": "contract-123",
        "model_stage": "college_precombine",
        "algorithm_id": algorithm_id,
        "checkpoint": {"target_season": 2026, "as_of_week": 4},
        "training_artifact": {"sha256": "a" * 64, "bytes": 12345},
        "selected_features": ["height_in", "weight_lb", "prod_receiving_yards"],
        "validation": validation,
    }


def forecast_row(release_id: str, *, probability: float = 0.42) -> dict[str, object]:
    return {
        "cfbd_player_id": "12345",
        "player_id": "cfbd:12345",
        "name": "Example Receiver",
        "school": "Example University",
        "position": "WR",
        "model_release_id": release_id,
        "contract_id": "contract-123",
        "probability_kind": "unconditional_next_draft",
        "probability_scope": "next_draft",
        "probability": probability,
        "conditional_probability": probability,
        "evidence_coverage": 0.75,
        "rank": 8,
    }


class TrackingTests(unittest.TestCase):
    def test_model_release_is_content_addressed_idempotent_and_tamper_evident(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TrackingStore(temporary)
            first = store.register_model_release(release_manifest())
            second = store.register_model_release(release_manifest())

            self.assertEqual(first.release_id, second.release_id)
            self.assertEqual(first.path, second.path)
            self.assertEqual(len(list(store.releases_dir.glob("model_*.json"))), 1)
            payload = json.loads(first.path.read_text(encoding="utf-8"))
            payload["content"]["algorithm_id"] = "altered"
            first.path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(TrackingError, "SHA-256"):
                store.get_model_release(first.release_id)
            audit = store.audit()
            self.assertFalse(audit["ok"])
            self.assertTrue(audit["findings"])

    def test_status_exposes_latest_machine_readable_validation_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TrackingStore(temporary)
            older = release_manifest(algorithm_id="college_logistic_v1")
            older["validation"]["validation_schema_version"] = 1
            store.register_model_release(older)
            stale_other = release_manifest()
            stale_other["position"] = "QB"
            stale_other["contract_id"] = "old-qb-contract"
            stale_other["checkpoint"] = {
                "target_season": 2025,
                "as_of_week": 14,
            }
            store.register_model_release(stale_other)
            latest = release_manifest(algorithm_id="college_logistic_v3", passes=False)
            latest["population"] = "FBS roster checkpoint"
            latest["training_rows"] = 800
            latest["validation"].update(
                {
                    "validation_schema_version": 2,
                    "rows": 480,
                    "positives": 36,
                    "expected_calibration_error": 0.03,
                    "quality_message": "withheld for test",
                    "simple_baselines": [
                        {
                            "name": "production_only",
                            "label": "Production-only",
                            "definition": "Production fields only.",
                            "validation_scheme": "expanding-window past-only",
                        }
                    ],
                }
            )
            newest_release = store.register_model_release(latest)

            status = store.status()
            report = status["latest_validation_report"]

            self.assertEqual(report["checkpoint"], {"season": 2026, "week": 4})
            self.assertEqual(report["model_count"], 1)
            self.assertEqual(report["models"][0]["release_id"], newest_release.release_id)
            stage = report["stage_summaries"]["college_precombine"]
            self.assertEqual(stage["rows"], 480)
            self.assertEqual(stage["positives"], 36)
            self.assertEqual(stage["withheld"], 1)
            self.assertAlmostEqual(
                stage["macro_average_metrics"]["expected_calibration_error"],
                0.03,
            )
            self.assertEqual(report["withheld_models"][0]["position"], "WR")
            self.assertEqual(
                report["baseline_definitions"][0]["name"],
                "production_only",
            )
            json.dumps(status, sort_keys=True)

    def test_validation_report_preserves_preseason_week_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TrackingStore(temporary)
            manifest = release_manifest()
            manifest["checkpoint"] = {"target_season": 2026, "as_of_week": 0}
            store.register_model_release(manifest)

            report = store.status()["latest_validation_report"]

            self.assertEqual(report["checkpoint"], {"season": 2026, "week": 0})

    def test_initial_eligible_release_becomes_champion_and_repeat_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TrackingStore(temporary)
            release = store.register_model_release(release_manifest())

            initial = store.decide_promotion(release.release_id)
            repeated = store.decide_promotion(release.release_id)

            self.assertEqual(initial.status, "promoted")
            self.assertEqual(repeated.status, "unchanged")
            self.assertEqual(store.champion_release_id(release.registry_key), release.release_id)
            self.assertTrue(store.champions_path.is_file())
            self.assertTrue(initial.path.is_file())
            self.assertTrue(repeated.path.is_file())

    def test_failed_or_incomparable_challenger_cannot_replace_champion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TrackingStore(temporary)
            champion = store.register_model_release(release_manifest())
            self.assertEqual(store.decide_promotion(champion.release_id).status, "promoted")

            failed = store.register_model_release(
                release_manifest(algorithm_id="failed-v2", passes=False, brier=0.08)
            )
            failed_decision = store.decide_promotion(failed.release_id)
            self.assertEqual(failed_decision.status, "not_promoted")
            self.assertTrue(any("quality gate" in reason for reason in failed_decision.reasons))

            incomparable = store.register_model_release(
                release_manifest(
                    algorithm_id="new-data-v2",
                    evaluation_set_id="different-evaluation-set",
                    brier=0.08,
                )
            )
            incomparable_decision = store.decide_promotion(incomparable.release_id)
            self.assertEqual(incomparable_decision.status, "not_promoted")
            self.assertTrue(any("different evaluation sets" in reason for reason in incomparable_decision.reasons))
            self.assertEqual(store.champion_release_id(champion.registry_key), champion.release_id)

    def test_replacement_requires_paired_evidence_and_metric_guardrails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TrackingStore(temporary)
            champion = store.register_model_release(release_manifest())
            store.decide_promotion(champion.release_id)

            unpaired = store.register_model_release(
                release_manifest(
                    algorithm_id="better-unpaired-v2",
                    brier=0.115,
                    log_loss=0.395,
                    average_precision=0.31,
                    roc_auc=0.71,
                )
            )
            unpaired_decision = store.decide_promotion(unpaired.release_id)
            self.assertEqual(unpaired_decision.status, "not_promoted")
            self.assertTrue(any("Paired" in reason for reason in unpaired_decision.reasons))

            paired = store.register_model_release(
                release_manifest(
                    algorithm_id="better-paired-v3",
                    brier=0.114,
                    log_loss=0.392,
                    average_precision=0.32,
                    roc_auc=0.72,
                    comparison={
                        "against_release_id": champion.release_id,
                        "paired_brier_improvement_ci80": [0.001, 0.010],
                    },
                )
            )
            paired_decision = store.decide_promotion(paired.release_id)

            self.assertEqual(paired_decision.status, "promoted")
            self.assertEqual(store.champion_release_id(champion.registry_key), paired.release_id)

    def test_metric_only_policy_can_be_enabled_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TrackingStore(temporary)
            champion = store.register_model_release(release_manifest())
            store.decide_promotion(champion.release_id)
            challenger = store.register_model_release(
                release_manifest(
                    algorithm_id="metric-only-v2",
                    brier=0.114,
                    log_loss=0.395,
                    average_precision=0.31,
                    roc_auc=0.71,
                )
            )

            decision = store.decide_promotion(
                challenger.release_id,
                policy=PromotionPolicy(require_paired_brier_evidence=False),
            )

            self.assertEqual(decision.status, "promoted")

    def test_forecast_runs_are_idempotent_and_changed_predictions_create_new_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TrackingStore(temporary)
            release = store.register_model_release(release_manifest())
            input_artifacts = {
                "players": {"sha256": "b" * 64, "bytes": 1000},
                "history": {"sha256": "a" * 64, "bytes": 12345},
            }
            first = store.record_forecast_run(
                season=2026,
                week=4,
                forecasts=[forecast_row(release.release_id)],
                input_artifacts=input_artifacts,
                metadata={"completed_games_only": True},
            )
            unchanged = store.record_forecast_run(
                season=2026,
                week=4,
                forecasts=[forecast_row(release.release_id)],
                input_artifacts=input_artifacts,
                metadata={"completed_games_only": True},
            )
            first_bytes = first.forecasts_path.read_bytes()
            revised = store.record_forecast_run(
                season=2026,
                week=4,
                forecasts=[forecast_row(release.release_id, probability=0.47)],
                input_artifacts=input_artifacts,
                metadata={"completed_games_only": True},
            )

            self.assertEqual(first.run_id, unchanged.run_id)
            self.assertNotEqual(first.run_id, revised.run_id)
            self.assertEqual(first.forecasts_path.read_bytes(), first_bytes)
            self.assertEqual(first.forecast_count, 1)
            self.assertEqual(revised.forecast_count, 1)
            runs = list((store.ledger_dir / "2026" / "week_04").glob("forecast_*"))
            self.assertEqual(len(runs), 2)
            self.assertTrue(store.audit()["ok"])
            status = store.status()
            self.assertEqual(status["validation_health"]["passed"], 1)
            self.assertEqual(status["champion_count"], 0)
            self.assertEqual(status["latest_forecast_checkpoint"], {"season": 2026, "week": 4})

    def test_forecast_validation_rejects_bad_probability_contract_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TrackingStore(temporary)
            release = store.register_model_release(release_manifest())
            bad_probability = forecast_row(release.release_id, probability=1.2)
            with self.assertRaisesRegex(TrackingError, "between 0 and 1"):
                store.record_forecast_run(
                    season=2026,
                    week=4,
                    forecasts=[bad_probability],
                    input_artifacts={},
                )
            wrong_contract = forecast_row(release.release_id)
            wrong_contract["contract_id"] = "wrong"
            with self.assertRaisesRegex(TrackingError, "does not match release"):
                store.record_forecast_run(
                    season=2026,
                    week=4,
                    forecasts=[wrong_contract],
                    input_artifacts={},
                )
            valid = forecast_row(release.release_id)
            with self.assertRaisesRegex(TrackingError, "duplicate"):
                store.record_forecast_run(
                    season=2026,
                    week=4,
                    forecasts=[valid, valid],
                    input_artifacts={},
                )

    def test_tampered_forecast_run_is_detected_without_changing_model_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TrackingStore(temporary)
            release = store.register_model_release(release_manifest())
            run = store.record_forecast_run(
                season=2026,
                week=4,
                forecasts=[forecast_row(release.release_id)],
                input_artifacts={},
            )
            payload = json.loads(run.forecasts_path.read_text(encoding="utf-8"))
            payload[0]["probability"] = 0.99
            run.forecasts_path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(TrackingError, "SHA-256"):
                store.verify_forecast_run(run.path)
            self.assertFalse(store.audit()["ok"])
            self.assertEqual(store.get_model_release(release.release_id).release_id, release.release_id)

    def test_artifact_fingerprint_records_exact_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "artifact.csv"
            payload = b"name,probability\nPlayer,0.5\n"
            path.write_bytes(payload)

            fingerprint = artifact_fingerprint(path)

            self.assertEqual(fingerprint["bytes"], len(payload))
            self.assertEqual(fingerprint["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual(fingerprint["path"], path.name)
            self.assertNotIn(str(Path(temporary).resolve()), fingerprint["path"])

    def test_release_rejects_absolute_or_parent_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TrackingStore(temporary)
            absolute = release_manifest()
            absolute["training_artifact"]["path"] = "/Users/example/private.csv"
            with self.assertRaisesRegex(TrackingError, "absolute machine-local path"):
                store.register_model_release(absolute)

            traversal = release_manifest()
            traversal["training_artifact"]["path"] = "../private.csv"
            with self.assertRaisesRegex(TrackingError, "parent or home traversal"):
                store.register_model_release(traversal)

    def test_forecast_rejects_nonportable_input_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TrackingStore(temporary)
            release = store.register_model_release(release_manifest())
            with self.assertRaisesRegex(TrackingError, "absolute machine-local path"):
                store.record_forecast_run(
                    season=2026,
                    week=4,
                    forecasts=[forecast_row(release.release_id)],
                    input_artifacts={
                        "players": {
                            "path": "C:\\Users\\example\\players.csv",
                            "sha256": "b" * 64,
                            "bytes": 100,
                        }
                    },
                )


if __name__ == "__main__":
    unittest.main()

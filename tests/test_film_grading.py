from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import unittest

from draftscope.analysis import ProspectEvaluator
from draftscope.model import (
    CollegeDraftProbabilityModel,
    DraftProbabilityModel,
    college_candidate_features,
    is_manual_scouting_field,
)
from draftscope.records import (
    DataError,
    audit_records,
    candidate_template_fields,
    normalize_record,
)
from draftscope.reporting import render_evaluation
from draftscope.schema import POSITION_GROUPS, TRAITS


FILM_FIELDS = (
    "film_grade_status",
    "film_grader",
    "film_graded_at",
    "film_game_ids",
    "film_games_reviewed",
    "film_snaps_reviewed",
    "film_opponent_mix",
    "film_grade_source",
    "film_notes_path",
    "film_second_grader",
    "film_second_grader_agreement",
    "film_consensus_method",
)
ROOT = Path(__file__).resolve().parents[1]


def _film_row(
    *,
    status: str = "complete",
    grade: float | None = 78.0,
    second_grader: bool = False,
) -> dict[str, object]:
    row: dict[str, object] = {
        "player_id": "film-test-wr",
        "name": "Film Test Receiver",
        "position": "WR",
        "school": "Synthetic University",
        "season": 2026,
        "projected_draft_year": 2027,
        "film_grade_status": status,
        "film_grader": "Synthetic Grader One",
        "film_graded_at": "2026-09-01",
        "film_game_ids": (
            "2026:week01:example_state;"
            "2026:week02:strong_state;"
            "2026:week03:adversity_state"
        ),
        "film_games_reviewed": 3,
        "film_snaps_reviewed": 78,
        "film_opponent_mix": "recent;strongest_available;adversity",
        "film_grade_source": "synthetic_coach_film_review",
        "film_notes_path": "scouting/film/film-test-wr-2026-09-01.md",
    }
    if grade is not None:
        row["trait_route_running"] = grade
    if second_grader:
        row.update(
            {
                "film_second_grader": "Synthetic Grader Two",
                "film_second_grader_agreement": 0.86,
                "film_consensus_method": "discussion_consensus",
            }
        )
    return row


def _high_findings(row: dict[str, object]) -> list[dict[str, str]]:
    return [
        finding
        for finding in audit_records([row])
        if finding["severity"] == "high"
    ]


def _college_history(
    draft_years: range,
    *,
    rows_per_year: int = 40,
) -> list[dict[str, object]]:
    """Deterministic low-base-rate history with no manual-film model inputs."""

    rows: list[dict[str, object]] = []
    for draft_year in draft_years:
        for index in range(rows_per_year):
            drafted = index < 8
            strength = float(rows_per_year - index)
            receptions = 18.0 + strength * 1.3
            rows.append(
                {
                    "player_id": f"history-{draft_year}-{index}",
                    "name": f"History Receiver {draft_year} {index}",
                    "position": "WR",
                    "school": f"Synthetic School {index % 8}",
                    "conference": "Synthetic Conference",
                    "season": draft_year - 1,
                    "draft_year": draft_year,
                    "checkpoint": "through_week_16",
                    "as_of_week": 16,
                    "feature_cutoff_season": draft_year - 1,
                    "model_stage": "college_precombine",
                    "probability_kind": "unconditional_next_draft",
                    "population": "synthetic FBS roster through Week 16",
                    "drafted": drafted,
                    "draft_round": 1 if drafted else None,
                    "draft_ovr": index + 1 if drafted else None,
                    "height_in": 70.0 + strength / 20.0,
                    "weight_lb": 180.0 + strength / 2.0,
                    "class_year": 4 if drafted else 2 + index % 3,
                    "has_recorded_stats": True,
                    "prod_receptions": receptions,
                    "prod_receiving_yards": receptions * (10.0 + strength / 20.0),
                    "prod_receiving_tds": max(1.0, strength / 4.0),
                    # Deliberately outcome-correlated fields that must remain outside
                    # the model's explicit college-stage allow-list.
                    "trait_route_running": 95.0 if drafted else 5.0,
                    "film_grade_status": "complete" if drafted else "provisional",
                    "film_grader": "Synthetic History Grader",
                    "film_graded_at": "2026-01-01",
                }
            )
    return rows


def _candidate(
    name: str,
    *,
    status: str,
    trait_grade: float,
) -> dict[str, object]:
    row = _film_row(status=status, grade=None)
    row.update(
        {
            "player_id": name.lower().replace(" ", "-"),
            "name": name,
            "conference": "Synthetic Conference",
            "as_of_week": 16,
            "height_in": 73.0,
            "weight_lb": 202.0,
            "class_year": 4,
            "has_recorded_stats": True,
            "prod_receptions": 68.0,
            "prod_receiving_yards": 1_050.0,
            "prod_receiving_tds": 9.0,
        }
    )
    for trait in TRAITS["WR"]:
        row[f"trait_{trait}"] = trait_grade
    if status == "provisional":
        row.update(
            {
                "film_game_ids": (
                    "2026:week01:example_state;2026:week02:adversity_state"
                ),
                "film_games_reviewed": 2,
                "film_snaps_reviewed": 52,
                "film_opponent_mix": "recent;adversity",
            }
        )
    return row


def _history_with_audited_film(
    history: list[dict[str, object]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, source in enumerate(history):
        row = dict(source)
        grade = float(10 + (index * 11) % 86)
        row.update(
            {
                "film_grade_status": "complete",
                "film_grader": "Synthetic History Grader",
                "film_graded_at": "2026-01-01",
                "film_game_ids": "history:game01;history:game02;history:game03",
                "film_games_reviewed": 3,
                "film_snaps_reviewed": 80,
                "film_opponent_mix": "recent;strongest_available;adversity",
                "film_grade_source": "synthetic_history_review",
                "film_notes_path": "scouting/synthetic-history.md",
            }
        )
        for trait in TRAITS["WR"]:
            row[f"trait_{trait}"] = grade
        rows.append(row)
    return rows


def _semantic_replay_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class FilmGradingAuditTests(unittest.TestCase):
    def test_candidate_template_contains_all_film_provenance_fields(self) -> None:
        fields = candidate_template_fields()
        self.assertTrue(set(FILM_FIELDS).issubset(fields))

    def test_numeric_trait_without_provenance_reports_every_required_field(self) -> None:
        findings = _high_findings(
            {
                "name": "Missing Evidence Receiver",
                "position": "WR",
                "school": "Synthetic University",
                "season": 2026,
                "trait_route_running": 78,
            }
        )
        fields = {finding["field"] for finding in findings}
        self.assertTrue(
            {
                "film_grade_status",
                "film_grader",
                "film_graded_at",
                "film_game_ids",
                "film_games_reviewed",
                "film_snaps_reviewed",
                "film_opponent_mix",
                "film_grade_source",
                "film_notes_path",
            }.issubset(fields)
        )

    def test_malformed_grading_date_is_rejected(self) -> None:
        row = _film_row()
        row["film_graded_at"] = "September 1, 2026"
        findings = _high_findings(row)
        self.assertTrue(
            any(
                finding["field"] == "film_graded_at"
                and "YYYY-MM-DD" in finding["message"]
                for finding in findings
            )
        )

    def test_duplicate_and_malformed_game_identifiers_are_rejected(self) -> None:
        cases = {
            "duplicate": (
                "2026:week01:example_state;"
                "2026:week01:example_state;"
                "2026:week03:adversity_state"
            ),
            "local_path": (
                "2026:week01:example_state;"
                "/Users/example/film/game.mov;"
                "2026:week03:adversity_state"
            ),
            "url": (
                "2026:week01:example_state;"
                "https://video.example/game;"
                "2026:week03:adversity_state"
            ),
            "placeholder": "none;2026:week02:strong_state;2026:week03:adversity_state",
        }
        for label, game_ids in cases.items():
            with self.subTest(label=label):
                row = _film_row()
                row["film_game_ids"] = game_ids
                findings = _high_findings(row)
                self.assertTrue(
                    any(finding["field"] == "film_game_ids" for finding in findings)
                )

    def test_non_text_and_local_provenance_values_are_rejected(self) -> None:
        cases = {
            "boolean_grade": ("trait_route_running", True),
            "boolean_grader": ("film_grader", True),
            "numeric_notes": ("film_notes_path", 123),
            "drive_relative_notes": ("film_notes_path", "C:notes/film.md"),
            "file_source": ("film_grade_source", "file:/Users/example/game.mov"),
            "relative_media_source": ("film_grade_source", "film/game.mov"),
            "boolean_agreement": ("film_second_grader_agreement", True),
        }
        for label, (field, value) in cases.items():
            with self.subTest(label=label):
                row = _film_row(second_grader=field == "film_second_grader_agreement")
                row[field] = value
                self.assertIn(
                    field,
                    {finding["field"] for finding in _high_findings(row)},
                )

    def test_percent_syntax_is_rejected_before_film_audit(self) -> None:
        for field, value in (
            ("trait_route_running", "80%"),
            ("film_games_reviewed", "300%"),
            ("film_snaps_reviewed", "7500%"),
            ("film_second_grader_agreement", "90%"),
        ):
            with self.subTest(field=field):
                row = _film_row(second_grader=field == "film_second_grader_agreement")
                row[field] = value
                with self.assertRaises(DataError):
                    normalize_record(row)

    def test_boolean_numeric_values_are_rejected_during_normalization(self) -> None:
        for field in (
            "trait_route_running",
            "film_games_reviewed",
            "film_snaps_reviewed",
            "film_second_grader_agreement",
        ):
            with self.subTest(field=field):
                row = _film_row(second_grader=field == "film_second_grader_agreement")
                row[field] = True
                with self.assertRaises(DataError):
                    normalize_record(row)

    def test_provisional_grade_requires_some_reviewed_evidence(self) -> None:
        row = _film_row(status="provisional")
        row.update(
            {
                "film_game_ids": "2026:week01:example_state",
                "film_games_reviewed": 0,
                "film_snaps_reviewed": 0,
                "film_opponent_mix": "recent",
            }
        )
        fields = {finding["field"] for finding in _high_findings(row)}
        self.assertIn("film_games_reviewed", fields)
        self.assertIn("film_snaps_reviewed", fields)

    def test_complete_status_enforces_games_snaps_and_opponent_mix(self) -> None:
        cases = {
            "too_few_games": {
                "film_game_ids": (
                    "2026:week01:example_state;2026:week02:strong_state"
                ),
                "film_games_reviewed": 2,
            },
            "too_few_snaps": {"film_snaps_reviewed": 74},
            "missing_strongest_opponent": {
                "film_opponent_mix": "recent;adversity;different_game_script"
            },
            "missing_adverse_script": {
                "film_opponent_mix": "recent;strongest_available"
            },
        }
        expected_fields = {
            "too_few_games": "film_games_reviewed",
            "too_few_snaps": "film_snaps_reviewed",
            "missing_strongest_opponent": "film_opponent_mix",
            "missing_adverse_script": "film_opponent_mix",
        }
        for label, updates in cases.items():
            with self.subTest(label=label):
                row = _film_row()
                row.update(updates)
                fields = {finding["field"] for finding in _high_findings(row)}
                self.assertIn(expected_fields[label], fields)

    def test_insufficient_status_rejects_numeric_trait_grades(self) -> None:
        row = _film_row(status="insufficient")
        row.update(
            {
                "film_game_ids": "2026:week01:example_state",
                "film_games_reviewed": 1,
                "film_snaps_reviewed": 22,
                "film_opponent_mix": "recent",
            }
        )
        findings = _high_findings(row)
        self.assertTrue(
            any(
                finding["field"] in {"film_grade_status", "trait_route_running"}
                and "insufficient" in finding["message"].lower()
                for finding in findings
            )
        )

    def test_provisional_sample_below_complete_threshold_is_valid(self) -> None:
        row = _film_row(status="provisional")
        row.update(
            {
                "film_game_ids": [
                    "2026:week01:example_state",
                    "2026:week02:adversity_state",
                ],
                "film_games_reviewed": 2,
                "film_snaps_reviewed": 52,
                "film_opponent_mix": "recent;adversity",
            }
        )
        self.assertEqual(_high_findings(row), [])

    def test_second_grader_metadata_is_consistent_and_bounded(self) -> None:
        missing_companion = _film_row()
        missing_companion["film_second_grader"] = "Synthetic Grader Two"
        fields = {
            finding["field"] for finding in _high_findings(missing_companion)
        }
        self.assertIn("film_second_grader_agreement", fields)
        self.assertIn("film_consensus_method", fields)

        orphaned_metadata = _film_row()
        orphaned_metadata.update(
            {
                "film_second_grader_agreement": 0.90,
                "film_consensus_method": "independent_average",
            }
        )
        self.assertIn(
            "film_second_grader",
            {finding["field"] for finding in _high_findings(orphaned_metadata)},
        )

        invalid_agreement = _film_row(second_grader=True)
        invalid_agreement["film_second_grader_agreement"] = 1.01
        self.assertIn(
            "film_second_grader_agreement",
            {finding["field"] for finding in _high_findings(invalid_agreement)},
        )

        invalid_method = _film_row(second_grader=True)
        invalid_method["film_consensus_method"] = "invented_method"
        self.assertIn(
            "film_consensus_method",
            {finding["field"] for finding in _high_findings(invalid_method)},
        )

        same_grader = _film_row(second_grader=True)
        same_grader["film_second_grader"] = same_grader["film_grader"]
        self.assertIn(
            "film_second_grader",
            {finding["field"] for finding in _high_findings(same_grader)},
        )

        self.assertEqual(_high_findings(_film_row(second_grader=True)), [])

    def test_valid_complete_evaluation_passes_audit(self) -> None:
        row = _film_row(second_grader=True)
        row["film_game_ids"] = [
            "2026:week01:example_state",
            "2026:week02:strong_state",
            "2026:week03:adversity_state",
        ]
        self.assertEqual(_high_findings(row), [])

    def test_out_of_range_grade_is_rejected_without_deleting_the_value(self) -> None:
        row = _film_row(grade=101)
        findings = _high_findings(row)
        self.assertTrue(
            any(finding["field"] == "trait_route_running" for finding in findings)
        )
        self.assertEqual(row["trait_route_running"], 101)

    def test_rubric_documents_every_defined_position_trait(self) -> None:
        rubric = (ROOT / "SCOUTING_RUBRIC.md").read_text(encoding="utf-8")
        actual: list[tuple[str, str]] = []
        current_position: str | None = None
        for line in rubric.splitlines():
            heading = re.fullmatch(r"### .+ \(`([A-Z]+)`\)", line)
            if heading:
                current_position = heading.group(1)
                continue
            if not line.startswith("| `trait_"):
                continue
            self.assertIsNotNone(current_position)
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            self.assertEqual(len(cells), 5, line)
            self.assertTrue(all(cells), line)
            actual.append((str(current_position), cells[0].strip("`")))

        expected = {
            (position, f"trait_{trait}")
            for position, traits in TRAITS.items()
            for trait in traits
        }
        self.assertEqual(len(actual), len(set(actual)), "duplicate rubric trait row")
        self.assertEqual(set(actual), expected)


class FilmGradingIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.population = "synthetic FBS roster through Week 16"
        cls.prior_history = _college_history(range(2020, 2026))
        cls.holdout_history = _college_history(range(2026, 2027))
        cls.model = CollegeDraftProbabilityModel(
            cls.prior_history,
            position="WR",
            population=cls.population,
        )

    def test_college_model_contract_excludes_traits_and_film_metadata(self) -> None:
        for position in POSITION_GROUPS:
            features = college_candidate_features(position)
            self.assertFalse(
                any(
                    feature.startswith("trait_") or feature.startswith("film_")
                    for feature in features
                ),
                position,
            )
        self.assertFalse(
            any(
                feature.startswith("trait_") or feature.startswith("film_")
                for feature in self.model.features
            )
        )
        self.assertTrue(set(self.model.features).issubset(self.model.contract.allowed_features))

    def test_generic_probability_contract_filters_manual_scouting_fields(self) -> None:
        model = DraftProbabilityModel(
            self.prior_history,
            position="WR",
            candidate_features=(
                "height_in",
                "weight_lb",
                "prod_receptions",
                "trait_route_running",
                "film_snaps_reviewed",
                "film_future_confidence",
            ),
            population=self.population,
            model_stage="college_precombine",
            probability_kind="unconditional_next_draft",
        )
        self.assertEqual(
            model.candidate_features,
            ("height_in", "weight_lb", "prod_receptions"),
        )
        self.assertFalse(
            any(
                feature.startswith("trait_") or feature.startswith("film_")
                for feature in model.features
            )
        )
        self.assertFalse(
            any(
                is_manual_scouting_field(key)
                for row in (*model.rows, *model.training_rows)
                for key in row
            )
        )

    def test_manual_grade_and_provenance_cannot_change_draft_probability(self) -> None:
        common = {
            "position": "WR",
            "school": "Synthetic School 1",
            "conference": "Synthetic Conference",
            "height_in": 73.0,
            "weight_lb": 202.0,
            "class_year": 4,
            "has_recorded_stats": True,
            "prod_receptions": 68.0,
            "prod_receiving_yards": 1_050.0,
            "prod_receiving_tds": 9.0,
        }
        low = self.model.predict(
            {
                **common,
                "trait_route_running": 20,
                "film_grade_status": "provisional",
                "film_grader": "Grader Low",
            },
            bootstrap=0,
        )
        high = self.model.predict(
            {
                **common,
                "trait_route_running": 99,
                "film_grade_status": "complete",
                "film_grader": "Grader High",
            },
            bootstrap=0,
        )
        self.assertTrue(low.available)
        self.assertTrue(high.available)
        self.assertEqual(low.probability, high.probability)
        self.assertEqual(low.conditional_probability, high.conditional_probability)
        self.assertEqual(low.feature_coverage, high.feature_coverage)
        self.assertEqual(low.out_of_distribution_score, high.out_of_distribution_score)
        self.assertEqual(low.features_used, high.features_used)

    def test_retrospective_probabilities_and_semantic_hash_ignore_film_grades(self) -> None:
        source = dict(self.holdout_history[0])
        low = {
            **source,
            "trait_route_running": 20,
            "film_grade_status": "provisional",
            "film_grader": "Grader Low",
        }
        high = {
            **source,
            "trait_route_running": 99,
            "film_grade_status": "complete",
            "film_grader": "Grader High",
        }
        low_replay = self.model.score_retrospective_holdout(
            [low],
            holdout_year=2026,
        )
        high_replay = self.model.score_retrospective_holdout(
            [high],
            holdout_year=2026,
        )
        self.assertEqual(low_replay, high_replay)
        self.assertEqual(
            _semantic_replay_sha256(asdict(low_replay)),
            _semantic_replay_sha256(asdict(high_replay)),
        )

    def _evaluator(
        self,
        candidates: list[dict[str, object]],
        *,
        history: list[dict[str, object]] | None = None,
    ) -> ProspectEvaluator:
        selected_history = history if history is not None else self.prior_history
        return ProspectEvaluator(
            selected_history,
            candidates,
            model_history=selected_history,
            model_metadata={
                "model_stage": "college_precombine",
                "population": self.population,
                "probability_kind": "unconditional_next_draft",
                "probability_condition": (
                    "P(drafted next draft | FBS roster player at checkpoint)"
                ),
            },
            team_profiles=[
                {
                    "team": "Synthetic Scheme Team",
                    "position": "WR",
                    "need_score": 65,
                    "need_confidence": 1.0,
                    "need_evidence_coverage": 1.0,
                    "importance_trait_route_running": 1.0,
                    "scheme_confidence": 1.0,
                    "profile_source": "synthetic_manual_profile",
                }
            ],
        )

    def test_complete_grades_can_change_profile_and_supported_team_fit_only(self) -> None:
        low_candidate = _candidate(
            "Complete Low Receiver",
            status="complete",
            trait_grade=45,
        )
        high_candidate = _candidate(
            "Complete High Receiver",
            status="complete",
            trait_grade=90,
        )
        self.assertEqual(_high_findings(low_candidate), [])
        self.assertEqual(_high_findings(high_candidate), [])
        evaluator = self._evaluator([low_candidate, high_candidate])
        low = evaluator.evaluate("Complete Low Receiver", bootstrap=0)
        high = evaluator.evaluate("Complete High Receiver", bootstrap=0)

        self.assertEqual(
            low.draft_prediction.probability,
            high.draft_prediction.probability,
        )
        self.assertEqual(
            low.draft_prediction.features_used,
            high.draft_prediction.features_used,
        )
        self.assertEqual(low.projected_pick_range, high.projected_pick_range)
        self.assertEqual(low.pick_comps, high.pick_comps)
        self.assertGreater(high.profile_score or 0.0, low.profile_score or 100.0)
        low_skills = next(item for item in low.categories if item.category == "skills")
        high_skills = next(item for item in high.categories if item.category == "skills")
        self.assertGreater(high_skills.score or 0.0, low_skills.score or 100.0)
        self.assertTrue(low.team_fits)
        self.assertTrue(high.team_fits)
        self.assertGreater(
            high.team_fits[0].scheme_score or 0.0,
            low.team_fits[0].scheme_score or 100.0,
        )

    def test_manual_grades_cannot_change_board_order_tie_break(self) -> None:
        low = _candidate("Alpha Receiver", status="complete", trait_grade=20)
        high = _candidate("Zulu Receiver", status="complete", trait_grade=99)
        first = self._evaluator([low, high]).rank(bootstrap=0)

        low_swapped = _candidate(
            "Alpha Receiver", status="complete", trait_grade=99
        )
        high_swapped = _candidate(
            "Zulu Receiver", status="complete", trait_grade=20
        )
        second = self._evaluator([low_swapped, high_swapped]).rank(bootstrap=0)

        self.assertEqual(
            [evaluation.player["name"] for evaluation in first],
            [evaluation.player["name"] for evaluation in second],
        )

    def test_manual_grades_cannot_change_pick_band_with_audited_trait_history(
        self,
    ) -> None:
        history = _history_with_audited_film(self.prior_history)
        low_candidate = _candidate(
            "Audited Low Receiver", status="complete", trait_grade=20
        )
        high_candidate = _candidate(
            "Audited High Receiver", status="complete", trait_grade=95
        )
        evaluator = self._evaluator(
            [low_candidate, high_candidate], history=history
        )
        low = evaluator.evaluate("Audited Low Receiver", bootstrap=0)
        high = evaluator.evaluate("Audited High Receiver", bootstrap=0)

        self.assertEqual(low.draft_prediction.probability, high.draft_prediction.probability)
        self.assertEqual(low.projected_pick_range, high.projected_pick_range)
        self.assertEqual(low.pick_comps, high.pick_comps)
        self.assertNotEqual(low.overall_comps, high.overall_comps)

    def test_provisional_grades_are_labeled_and_excluded_from_profile_and_fit(self) -> None:
        low_candidate = _candidate(
            "Provisional Low Receiver",
            status="provisional",
            trait_grade=20,
        )
        high_candidate = _candidate(
            "Provisional High Receiver",
            status="provisional",
            trait_grade=99,
        )
        self.assertEqual(_high_findings(low_candidate), [])
        self.assertEqual(_high_findings(high_candidate), [])
        evaluator = self._evaluator([low_candidate, high_candidate])
        low = evaluator.evaluate("Provisional Low Receiver", bootstrap=0)
        high = evaluator.evaluate("Provisional High Receiver", bootstrap=0)

        self.assertEqual(low.draft_prediction.probability, high.draft_prediction.probability)
        self.assertEqual(low.profile_score, high.profile_score)
        self.assertEqual(low.team_fits, high.team_fits)
        for evaluation in (low, high):
            skills = next(
                item for item in evaluation.categories if item.category == "skills"
            )
            # The provisional values remain visible to a human reviewer; their
            # exclusion is proven by the invariant profile and team-fit outputs.
            self.assertIsNotNone(skills.score)
            self.assertTrue(
                any("provisional" in warning.lower() for warning in evaluation.warnings)
            )
            self.assertIn("provisional", render_evaluation(evaluation).lower())

    def test_insufficient_grades_are_withheld_from_published_evaluation(self) -> None:
        candidate = _candidate(
            "Insufficient Receiver",
            status="insufficient",
            trait_grade=99,
        )
        candidate.update(
            {
                "film_game_ids": "2026:week01:example_state",
                "film_games_reviewed": 1,
                "film_snaps_reviewed": 24,
                "film_opponent_mix": "recent",
            }
        )
        evaluator = self._evaluator([candidate])
        evaluation = evaluator.evaluate("Insufficient Receiver", bootstrap=0)

        self.assertTrue(
            all(
                value is None
                for key, value in evaluation.player.items()
                if key.startswith("trait_")
            )
        )
        skills = next(
            item for item in evaluation.categories if item.category == "skills"
        )
        self.assertIsNone(skills.score)
        report = render_evaluation(evaluation)
        self.assertIn("INSUFFICIENT", report)
        self.assertNotIn("99.0", report)

    def test_invalid_provisional_claim_is_withheld_and_not_called_displayed(self) -> None:
        candidate = _candidate(
            "Malformed Provisional Receiver",
            status="provisional",
            trait_grade=88,
        )
        candidate["film_notes_path"] = "/Users/example/private-notes.md"
        evaluator = self._evaluator([candidate])
        evaluation = evaluator.evaluate("Malformed Provisional Receiver", bootstrap=0)

        self.assertIsNone(evaluation.player["trait_route_running"])
        report = render_evaluation(evaluation)
        self.assertIn("PROVISIONAL CLAIM NOT AUDIT-ELIGIBLE", report)
        self.assertNotIn("PROVISIONAL — displayed only", report)


if __name__ == "__main__":
    unittest.main()

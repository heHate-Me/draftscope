from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from draftscope.records import (
    audit_records,
    candidate_template_fields,
    historical_template_fields,
    normalize_record,
    parse_height,
    load_records,
    write_records,
)


class RecordTests(unittest.TestCase):
    def test_height_and_derived_features(self) -> None:
        self.assertEqual(parse_height("6-4"), 76.0)
        row = normalize_record(
            {
                "name": "Test Player",
                "position": "DE",
                "height_in": "6-4",
                "weight_lb": "260",
                "forty_s": "4.65",
                "vertical_in": "35",
                "broad_jump_in": "120",
            }
        )
        self.assertEqual(row["position"], "EDGE")
        self.assertAlmostEqual(row["height_in"], 76.0)
        self.assertGreater(row["speed_score"], 100)
        self.assertAlmostEqual(row["explosion_index"], 65.0)

    def test_derived_age_respects_the_plausible_draft_age_contract(self) -> None:
        plausible = normalize_record(
            {"date_of_birth": "2004-06-01", "draft_year": 2027}
        )
        impossible = normalize_record(
            {"date_of_birth": "2011-06-01", "draft_year": 2027}
        )

        self.assertGreater(plausible["age_at_draft"], 22.0)
        self.assertIsNone(impossible["age_at_draft"])

    def test_numeric_boolean_fields_are_accepted(self) -> None:
        row = normalize_record(
            {
                "has_recorded_stats": 1.0,
                "outcome_label_known": "0.0",
            }
        )
        self.assertIs(row["has_recorded_stats"], True)
        self.assertIs(row["outcome_label_known"], False)

    def test_audit_detects_duplicate_and_range_error(self) -> None:
        rows = [
            normalize_record({"name": "Same Name", "position": "WR", "school": "A", "season": 2026, "forty_s": 3.1}),
            normalize_record({"name": "Same Name", "position": "WR", "school": "A", "season": 2026}),
        ]
        findings = audit_records(rows)
        messages = " ".join(item["message"] for item in findings)
        self.assertIn("outside plausible range", messages)
        self.assertIn("Duplicate player-season", messages)

    def test_entry_scope_fields_are_typed_and_in_candidate_template(self) -> None:
        row = normalize_record(
            {
                "name": "Entry Test",
                "position": "WR",
                "as_of_week": "6",
                "draft_entry_probability": "65%",
                "draft_declared": "no",
                "draft_eligible": "yes",
            }
        )
        self.assertEqual(row["as_of_week"], 6.0)
        self.assertEqual(row["draft_entry_probability"], 0.65)
        self.assertFalse(row["draft_declared"])
        self.assertTrue(row["draft_eligible"])
        fields = candidate_template_fields()
        for field in ("as_of_week", "draft_entry_probability", "draft_declared", "draft_eligible"):
            self.assertIn(field, fields)
        self.assertIn("probability_kind", historical_template_fields())

    def test_audit_rejects_impossible_entry_inputs(self) -> None:
        row = normalize_record(
            {
                "name": "Contradictory Entry",
                "position": "WR",
                "draft_entry_probability": 0.7,
                "draft_declared": True,
                "draft_eligible": False,
            }
        )
        findings = audit_records([row])
        messages = " ".join(item["message"] for item in findings)
        self.assertIn("both declared and ineligible", messages)
        self.assertIn("declared player's draft entry probability must be 1", messages)
        self.assertIn("ineligible player's next-draft entry probability must be 0", messages)

    def test_historical_boolean_fields_survive_csv_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "history.csv"
            write_records(
                path,
                [
                    {
                        "name": "Round Trip",
                        "position": "WR",
                        "drafted": True,
                        "elite": False,
                        "outcome_label_known": True,
                        "has_recorded_stats": False,
                        "nfl_three_year_outcome_known": True,
                        "nfl_three_year_right_censored": False,
                        "nfl_three_year_contributor": True,
                        "active_three_year_contributor_eligible": True,
                        "benchmark_active_contributor_cohort_eligible": True,
                        "nfl_three_year_primary_snaps": 725,
                    }
                ],
            )

            row = load_records(path)[0]
            self.assertIs(row["drafted"], True)
            self.assertIs(row["elite"], False)
            self.assertIs(row["outcome_label_known"], True)
            self.assertIs(row["has_recorded_stats"], False)
            self.assertIs(row["nfl_three_year_outcome_known"], True)
            self.assertIs(row["nfl_three_year_right_censored"], False)
            self.assertIs(row["nfl_three_year_contributor"], True)
            self.assertIs(row["active_three_year_contributor_eligible"], True)
            self.assertIs(
                row["benchmark_active_contributor_cohort_eligible"], True
            )
            self.assertEqual(row["nfl_three_year_primary_snaps"], 725.0)


if __name__ == "__main__":
    unittest.main()

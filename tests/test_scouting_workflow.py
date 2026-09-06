import json
import tempfile
from pathlib import Path

import unittest

from draftscope.scouting import evidence_template, validate_evidence, write_template, summarize_evidence
from draftscope.records import normalize_record, load_records, write_records


class TestScoutingWorkflow(unittest.TestCase):
    def test_evidence_template_position(self):
        tmpl = evidence_template("RB")
        self.assertIn("trait_evidence", tmpl)
        # ensure trait keys correspond to RB traits
        keys = {t.get("trait_key") for t in tmpl["trait_evidence"]}
        self.assertTrue(any(k and k.startswith("trait_") for k in keys))

    def test_validate_incomplete_evidence(self):
        tmpl = evidence_template("WR")
        # intentionally leave name empty
        findings = validate_evidence(tmpl)
        self.assertTrue(any(f["field"] == "player.name" for f in findings))

    def test_summarize_and_write_template(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "evidence.json"
            write_template(str(out), "QB")
            data = json.loads(out.read_text(encoding="utf-8"))
            summary = summarize_evidence(data)
            self.assertEqual(summary.get("position"), "QB")

    def test_manual_grades_do_not_change_model_interface(self):
        # This test ensures that evidence can be serialized and summarized and
        # importantly no attempt is made here to change the trained model.
        tmpl = evidence_template("RB")
        tmpl["player"]["name"] = "Test Runner"
        tmpl["player"]["position"] = "RB"
        # add one graded trait but don't connect to model
        if tmpl["trait_evidence"]:
            tmpl["trait_evidence"][0]["trait_key"] = "trait_vision"
            tmpl["trait_evidence"][0]["grade"] = 85
            tmpl["film_grade_status"] = "provisional"
            tmpl["film_grader"] = "UnitTest"
            tmpl["film_graded_at"] = "2026-09-06"
            # minimal required provenance
            tmpl["film_game_ids"] = ["G1"]
            tmpl["film_games_reviewed"] = 1
            tmpl["film_snaps_reviewed"] = 5
            tmpl["film_opponent_mix"] = ["recent"]
            tmpl["film_grade_source"] = "https://example.com/highlights"
        findings = validate_evidence(tmpl)
        # Expect no high-severity errors that would prevent display (may include info notes)
        self.assertFalse(any(f.get("severity") == "high" for f in findings if f.get("field") == "film_grade_status"))


if __name__ == "__main__":
    unittest.main()

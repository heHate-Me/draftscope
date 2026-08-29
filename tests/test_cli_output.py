from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from draftscope.cli import main
from draftscope.records import write_records


class SimpleOutputCLITests(unittest.TestCase):
    def test_unavailable_current_board_errors_clearly_but_recent_draft_search_works(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "current_board_available": False,
                        "current_board_reason": "No current-season player rows were available for ranking.",
                    }
                ),
                encoding="utf-8",
            )
            drafted = {
                "name": "Recent Draftee",
                "position": "WR",
                "school": "Example U",
                "draft_year": 2026,
                "drafted": True,
                "draft_round": 1,
                "draft_ovr": 18,
                "nfl_team": "SEA",
            }
            report = {
                "player": drafted,
                "draft_prediction": {"available": False},
                "categories": [],
                "benchmarks": [],
                "overall_comps": [],
                "physical_comps": [],
                "historical_elite_comps": [],
                "pick_comps": [],
                "team_fits": [],
            }
            evaluator = SimpleNamespace(evaluate=lambda *_args, **_kwargs: report)

            stderr = StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(main(["board", "--state", str(state)]), 2)
            self.assertIn("No current draft board was published", stderr.getvalue())

            output = StringIO()
            with (
                patch(
                    "draftscope.cli.load_latest_completed_draft_class",
                    return_value=([drafted], {"draft_year": 2026}),
                ),
                patch("draftscope.cli.load_nflverse_history", return_value=([], {})),
                patch("draftscope.cli.ProspectEvaluator", return_value=evaluator),
                redirect_stdout(output),
            ):
                self.assertEqual(
                    main(["search", "Recent Draftee", "--state", str(state)]),
                    0,
                )
            self.assertIn("2026 result: selected #18", output.getvalue())

    def test_show_defaults_to_summary_and_full_preserves_technical_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "snapshot"
            reports = snapshot / "reports"
            reports.mkdir(parents=True)
            board = snapshot / "board.csv"
            write_records(
                board,
                [
                    {
                        "rank": 1,
                        "rank_delta": 2,
                        "draft_probability_delta": 0.05,
                        "name": "Current Prospect",
                        "position": "WR",
                        "school": "Example U",
                    }
                ],
            )
            payload = {
                "player": {
                    "name": "Current Prospect",
                    "position": "WR",
                    "school": "Example U",
                    "season": 2026,
                },
                "as_of_date": "2026-09-01",
                "evidence_coverage": 0.2,
                "draft_prediction": {
                    "available": True,
                    "probability": 0.25,
                    "features_used": ["height_in"],
                    "feature_coverage": 1.0,
                },
                "categories": [],
                "benchmarks": [],
                "overall_comps": [],
                "physical_comps": [],
                "historical_elite_comps": [],
                "pick_comps": [],
                "team_fits": [],
            }
            (reports / "current_prospect_example_u.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            (reports / "current_prospect_example_u.txt").write_text(
                "TECHNICAL REPORT\n", encoding="utf-8"
            )
            state = root / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "latest_board": str(board),
                        "latest_snapshot": str(snapshot),
                    }
                ),
                encoding="utf-8",
            )

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(["show", "Current Prospect", "--state", str(state)]), 0
                )
            self.assertIn("Draft chance: 25%", output.getvalue())
            self.assertNotIn("TECHNICAL REPORT", output.getvalue())

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "show",
                            "Current Prospect",
                            "--state",
                            str(state),
                            "--full",
                        ]
                    ),
                    0,
                )
            self.assertEqual(output.getvalue(), "TECHNICAL REPORT\n")

    def test_show_falls_back_to_latest_completed_draft_class(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "snapshot"
            snapshot.mkdir()
            board = snapshot / "board.csv"
            write_records(
                board,
                [
                    {
                        "rank": 1,
                        "name": "Different Prospect",
                        "position": "QB",
                        "school": "Other U",
                    }
                ],
            )
            state = root / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "latest_board": str(board),
                        "latest_snapshot": str(snapshot),
                    }
                ),
                encoding="utf-8",
            )
            drafted = {
                "name": "Recent Draftee",
                "position": "WR",
                "school": "Example U",
                "season": 2026,
                "draft_year": 2026,
                "drafted": True,
                "draft_round": 1,
                "draft_ovr": 18,
                "nfl_team": "SEA",
            }
            report = {
                "player": drafted,
                "as_of_date": "2026-09-01",
                "evidence_coverage": 0.2,
                "draft_prediction": {
                    "available": False,
                    "label": "Draft outcome already known",
                },
                "categories": [],
                "benchmarks": [],
                "overall_comps": [],
                "physical_comps": [],
                "historical_elite_comps": [],
                "pick_comps": [],
                "team_fits": [],
            }
            evaluator = SimpleNamespace(evaluate=lambda *_args, **_kwargs: report)

            output = StringIO()
            with (
                patch(
                    "draftscope.cli.load_latest_completed_draft_class",
                    return_value=([drafted], {"draft_year": 2026}),
                ),
                patch(
                    "draftscope.cli.load_nflverse_history",
                    return_value=([], {}),
                ),
                patch("draftscope.cli.ProspectEvaluator", return_value=evaluator),
                redirect_stdout(output),
            ):
                self.assertEqual(
                    main(["show", "Recent Draftee", "--state", str(state)]), 0
                )

            self.assertIn("2026 result: selected #18 | Round 1 | by SEA", output.getvalue())
            self.assertIn("Draft chance: not shown", output.getvalue())

    def test_search_alias_prompts_for_player_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            board = root / "board.csv"
            snapshot = root / "snapshot"
            reports = snapshot / "reports"
            reports.mkdir(parents=True)
            write_records(
                board,
                [{"name": "Prompt Player", "school": "Example U", "position": "WR"}],
            )
            state = root / "state.json"
            state.write_text(
                json.dumps({"latest_board": str(board), "latest_snapshot": str(snapshot)}),
                encoding="utf-8",
            )
            report = {
                "player": {"name": "Prompt Player", "school": "Example U", "position": "WR"},
                "draft_prediction": {"available": False},
                "categories": [],
                "benchmarks": [],
                "overall_comps": [],
                "physical_comps": [],
                "historical_elite_comps": [],
                "pick_comps": [],
                "team_fits": [],
            }
            (reports / "prompt_player_example_u.json").write_text(
                json.dumps(report), encoding="utf-8"
            )

            output = StringIO()
            with patch("builtins.input", return_value="Prompt Player"), redirect_stdout(output):
                self.assertEqual(main(["search", "--state", str(state)]), 0)

            self.assertIn("Prompt Player", output.getvalue())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import argparse
from datetime import date
from difflib import SequenceMatcher
import getpass
import json
from pathlib import Path
import sys
from typing import Any, Mapping

from .analysis import ProspectEvaluator
from .config_template import render_config_template
from .data_sources import (
    CFBDClient,
    build_cfbd_weekly_history,
    discover_cfbd_candidates,
    load_latest_completed_draft_class,
    load_nflverse_history,
    load_nflverse_team_profiles,
    merge_team_profiles,
)
from .credentials import CFBD_KEYCHAIN_SERVICE, store_cfbd_api_key_in_keychain
from .doctor import diagnose, render_diagnosis
from .records import (
    DataError,
    audit_records,
    candidate_template_fields,
    file_sha256,
    historical_template_fields,
    load_records,
    normalize_name,
    normalize_record,
    slug,
    team_template_fields,
    write_records,
)
from .reporting import (
    render_board,
    render_board_summary,
    render_evaluation,
    render_player_summary,
)
from .schema import POSITION_GROUPS, normalize_position
from .scheduler import (
    install_weekly_schedule,
    remove_weekly_schedule,
    weekly_schedule_status,
)
from .tracking import TrackingStore
from .weekly import load_config, run_weekly_update


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="draftscope",
        description="NCAA prospect benchmarking, draft likelihood, comps, and NFL team fit.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    template = sub.add_parser("template", help="Write an empty input template")
    template.add_argument("kind", choices=("config", "players", "history", "teams", "evidence"))
    template.add_argument("--out", required=True)

    doctor = sub.add_parser("doctor", help="Check local setup without making network calls")
    doctor.add_argument("--config", default="draftscope.toml")
    doctor.add_argument("--json", action="store_true")

    sub.add_parser("configure-key", help="Securely store a CFBD key in macOS Keychain")

    add = sub.add_parser("add", help="Add or update a player locally without API credentials")
    add.add_argument("name")
    add.add_argument("--position", required=True)
    add.add_argument("--school", required=True)
    add.add_argument("--season", type=int, default=date.today().year)
    age = add.add_mutually_exclusive_group()
    age.add_argument("--date-of-birth", help="ISO date, YYYY-MM-DD")
    age.add_argument("--age-at-draft")
    add.add_argument("--height", "--height-in", dest="height_in")
    add.add_argument("--weight", "--weight-lb", dest="weight_lb")
    add.add_argument("--entry-probability", dest="draft_entry_probability")
    add.add_argument("--out", required=True, help="CSV or JSON candidate file to create/update")

    refresh = sub.add_parser("refresh-history", help="Download and validate the latest nflverse ten-draft cohort")
    _history_arguments(refresh)

    build_history = sub.add_parser("build-weekly-history", help="Build a week-matched drafted/undrafted CFBD production cohort")
    build_history.add_argument("--season", type=int, default=date.today().year)
    build_history.add_argument("--week", type=int, required=True)
    build_history.add_argument("--lookback", type=int, default=10)
    build_history.add_argument("--out", required=True)
    build_history.add_argument("--raw-dir")

    discover = sub.add_parser("discover", help="Build a national FBS prospect pool from bounded bulk production")
    discover.add_argument("--season", type=int, default=date.today().year)
    discover.add_argument(
        "--provider",
        choices=("sportsdataverse", "cfbd"),
        default="sportsdataverse",
        help="College source; SportsDataverse needs no API key",
    )
    discover.add_argument("--cache-dir", default=".draftscope-cache")
    discover.add_argument("--week", type=int, help="Completed week; defaults to the latest fully completed week")
    discover.add_argument("--per-position", type=int, default=50, help="Maximum players retained per position; 0 keeps all")
    discover.add_argument("--no-preseason-prior", action="store_true")
    discover.add_argument("--raw-dir")
    discover.add_argument("--out", required=True)

    lookup = sub.add_parser("lookup", help="Look up a player in CFBD and add/update the local players file")
    lookup.add_argument("name")
    lookup.add_argument("--season", type=int, default=date.today().year)
    lookup.add_argument("--team")
    lookup.add_argument("--out", required=True, help="CSV or JSON candidate file to create/update")
    lookup.add_argument("--week", type=int)

    audit = sub.add_parser(
        "audit",
        help=(
            "Check identities, positions, ranges, and manual film-grade provenance/protocol"
        ),
    )
    audit.add_argument("--players", required=True)

    evidence_validate = sub.add_parser(
        "validate-evidence",
        help="Validate a scouting evidence JSON file for completeness and protocol",
    )
    evidence_validate.add_argument("--evidence", required=True)
    evidence_validate.add_argument("--out", help="Write findings to a file (JSON)")

    evidence_template = sub.add_parser(
        "evidence-template",
        help="Write a position-specific scouting evidence template (JSON)",
    )
    evidence_template.add_argument("--position", help="Normalized position (e.g., RB, WR)")
    evidence_template.add_argument("--out", required=True)

    tracking = sub.add_parser(
        "model-status",
        aliases=["tracking-audit"],
        help="Verify immutable model releases and weekly forecast runs",
    )
    tracking.add_argument("--output-dir", default="data")
    tracking.add_argument("--json", action="store_true")

    board = sub.add_parser("board", help="Show the latest completed prospect board instantly")
    board.add_argument("--top", type=int, default=50)
    board.add_argument("--state", default="data/state.json")
    board.add_argument("--json", action="store_true")
    board.add_argument("--full", action="store_true", help="Show profile and coverage diagnostics")

    show = sub.add_parser(
        "show",
        aliases=["player", "search"],
        help="Search for one player and show the latest scouting report",
    )
    show.add_argument("name", nargs="?", help="Player name; omit it to be prompted")
    show.add_argument("--school")
    show.add_argument("--state", default="data/state.json")
    show.add_argument("--cache-dir", default=".draftscope-cache")
    show.add_argument("--json", action="store_true")
    show.add_argument("--full", action="store_true", help="Show technical validation and every table")

    schedule = sub.add_parser(
        "schedule",
        help="Install, inspect, or remove the macOS weekly updater",
    )
    schedule.add_argument("action", choices=("install", "status", "remove"))
    schedule.add_argument("--config", default="draftscope.toml")
    schedule.add_argument("--weekday", type=int, default=2, help="launchd weekday; 2 is Tuesday")
    schedule.add_argument("--hour", type=int, default=6)
    schedule.add_argument("--minute", type=int, default=0)
    schedule.add_argument("--json", action="store_true")

    evaluate = sub.add_parser("evaluate", aliases=["scout"], help="Evaluate one player by name")
    evaluate.add_argument("name")
    evaluate.add_argument("--players", required=True)
    evaluate.add_argument("--school")
    evaluate.add_argument("--team-profiles")
    evaluate.add_argument("--no-auto-team-needs", action="store_true")
    evaluate.add_argument("--bootstrap", type=int, default=30)
    evaluate.add_argument("--json", action="store_true")
    evaluate.add_argument("--full", action="store_true", help="Show technical validation and every table")
    evaluate.add_argument("--out")
    evaluate.add_argument("--as-of-date")
    _history_arguments(evaluate)

    rank = sub.add_parser("rank", help="Rank every prospect in a candidate file")
    rank.add_argument("--players", required=True)
    rank.add_argument("--team-profiles")
    rank.add_argument("--no-auto-team-needs", action="store_true")
    rank.add_argument("--top", type=int, default=50)
    rank.add_argument("--json", action="store_true")
    rank.add_argument("--out")
    rank.add_argument("--as-of-date")
    _history_arguments(rank)

    update = sub.add_parser("update", help="Refresh current college production and rebuild the weekly board")
    update.add_argument("--config", default="draftscope.toml")
    update.add_argument("--no-refresh-sources", action="store_true")
    update.add_argument(
        "--reports-only",
        action="store_true",
        help="Rebuild reports from the saved checkpoint without live college-player requests",
    )
    return parser


def _history_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--history", help="Custom all-prospect labeled history CSV/JSON; defaults to nflverse combine history")
    parser.add_argument("--cache-dir", default=".draftscope-cache")
    parser.add_argument("--lookback", type=int, default=10)
    parser.add_argument("--end-year", type=int)
    parser.add_argument("--refresh", action="store_true")


def _load_history(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if getattr(args, "history", None):
        rows = load_records(args.history)
        return rows, _custom_history_metadata(rows, str(args.history))
    return load_nflverse_history(
        args.cache_dir,
        lookback_years=args.lookback,
        end_year=args.end_year,
        refresh=args.refresh,
    )


def _load_evaluation_histories(
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    benchmark_rows, benchmark_metadata = load_nflverse_history(
        args.cache_dir,
        lookback_years=args.lookback,
        end_year=args.end_year,
        refresh=args.refresh,
    )
    history_path = str(getattr(args, "history", None) or "").strip()
    automatic_history = not history_path
    if not history_path:
        start = int(benchmark_metadata.get("window_start") or 0)
        end = int(benchmark_metadata.get("window_end") or 0)
        expected_draft_years = tuple(range(start, end + 1)) if start and end >= start else None
        automatic = _latest_model_history_path(
            getattr(args, "players", None),
            expected_draft_years=expected_draft_years,
        )
        if automatic is None:
            fallback_metadata = dict(benchmark_metadata)
            fallback_metadata["model_history_fallback_reason"] = (
                "No audited college history matches the requested draft window; "
                "run `./run_draftscope.py update --config draftscope.toml` to rebuild it. "
                "Only the separately scoped Combine-stage probability is available."
            )
            return benchmark_rows, fallback_metadata, benchmark_rows, fallback_metadata
        history_path = str(automatic)
    model_rows = load_records(history_path)
    model_metadata = _history_metadata(model_rows, history_path)
    if automatic_history:
        actual_years: set[int] = set()
        for row in model_rows:
            try:
                actual_years.add(int(float(row.get("draft_year") or row.get("season"))))
            except (TypeError, ValueError):
                continue
        expected_years = set(expected_draft_years or ())
        expected_rows = model_metadata.get("rows")
        try:
            row_count_matches = int(expected_rows) == len(model_rows)
        except (TypeError, ValueError):
            row_count_matches = False
        if actual_years != expected_years or not row_count_matches:
            fallback_metadata = dict(benchmark_metadata)
            fallback_metadata["model_history_fallback_reason"] = (
                "The automatic college-history CSV does not match its audited ten-draft window/row count; "
                "run `./run_draftscope.py update --config draftscope.toml` to rebuild it. "
                "Only the separately scoped Combine-stage probability is available."
            )
            return benchmark_rows, fallback_metadata, benchmark_rows, fallback_metadata
    return benchmark_rows, benchmark_metadata, model_rows, model_metadata


def _custom_history_metadata(rows: list[dict[str, Any]], source: str) -> dict[str, Any]:
    populations = {str(row.get("population")) for row in rows if row.get("population")}
    kinds = {str(row.get("probability_kind")) for row in rows if row.get("probability_kind")}
    conditions = {
        str(row.get("probability_condition"))
        for row in rows
        if row.get("probability_condition")
    }
    stages = {str(row.get("model_stage")) for row in rows if row.get("model_stage")}
    metadata: dict[str, Any] = {
        "source": source,
        "population": next(iter(populations)) if len(populations) == 1 else "provided historical risk set",
        "probability_kind": next(iter(kinds)) if len(kinds) == 1 else "unknown_risk_set",
    }
    if len(conditions) == 1:
        metadata["probability_condition"] = next(iter(conditions))
    if len(stages) == 1:
        metadata["model_stage"] = next(iter(stages))
    return metadata


def _history_metadata(rows: list[dict[str, Any]], source: str) -> dict[str, Any]:
    path = Path(source)
    candidates = (
        path.with_suffix(".metadata.json"),
        path.with_suffix(path.suffix + ".metadata.json"),
    )
    for metadata_path in candidates:
        if not metadata_path.is_file():
            continue
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload["source"] = str(path)
            return payload
    return _custom_history_metadata(rows, str(path))


def _latest_model_history_path(
    players_path: str | None,
    *,
    expected_draft_years: tuple[int, ...] | None = None,
) -> Path | None:
    if not players_path:
        return None
    players = Path(players_path).resolve()
    working_root = Path.cwd().resolve()
    working_data_dir = working_root / "data"
    search_roots = [players.parent]
    # The checkout-level fallback is useful for tracked examples, but it must
    # not leak into arbitrary player files elsewhere on the machine (or into
    # callers validating their own adjacent history artifact).
    try:
        players.relative_to(working_root)
        players_in_working_tree = True
    except ValueError:
        players_in_working_tree = False
    if players_in_working_tree and working_data_dir != players.parent:
        search_roots.append(working_data_dir)

    for root in search_roots:
        state_path = root / "state.json"
        if not state_path.is_file():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        raw = str(state.get("latest_model_history") or "").strip()
        if raw:
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = state_path.parent / candidate
            if candidate.is_file() and _model_history_candidate_is_valid(
                candidate, expected_draft_years=expected_draft_years
            ):
                return candidate.resolve()

    # A strict college history may have been built independently after an
    # earlier weekly run fell back to combine data.  Adopt only a sidecar that
    # records a clean quality gate; never silently select an unaudited CSV.
    for root in search_roots:
        fallback = root / "model_history" / "college_history.csv"
        if fallback.is_file() and _model_history_candidate_is_valid(
            fallback, expected_draft_years=expected_draft_years
        ):
            return fallback.resolve()
    return None


def _model_history_candidate_is_valid(
    history_path: Path,
    *,
    expected_draft_years: tuple[int, ...] | None,
) -> bool:
    metadata_path = history_path.with_suffix(".metadata.json")
    if not metadata_path.is_file():
        return False
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if (
        metadata.get("model_stage") != "college_precombine"
        or metadata.get("quality_gate_status") != "pass"
        or metadata.get("source_failures")
        or metadata.get("contract_audit", {}).get("status") != "pass"
    ):
        return False
    artifact = metadata.get("training_artifact") or {}
    try:
        artifact_matches = bool(
            artifact.get("sha256") == file_sha256(history_path)
            and int(artifact.get("bytes")) == history_path.stat().st_size
        )
    except (OSError, TypeError, ValueError):
        artifact_matches = False
    if not artifact_matches:
        return False
    if expected_draft_years is not None:
        try:
            observed = tuple(int(value) for value in metadata.get("draft_years", ()))
        except (TypeError, ValueError):
            return False
        if observed != expected_draft_years:
            return False
    return True


def _profiles(args: argparse.Namespace, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    manual = load_records(args.team_profiles) if args.team_profiles else []
    if getattr(args, "no_auto_team_needs", False):
        return manual
    seasons = [int(float(row.get("season"))) for row in candidates if row.get("season") not in (None, "")]
    season = max(seasons) if seasons else date.today().year
    automatic = load_nflverse_team_profiles(args.cache_dir, season=season, refresh=args.refresh)
    try:
        from .nflverse_free import (
            load_nflverse_team_enrichment,
            merge_nflverse_team_enrichment,
        )

        enrichment, _metadata = load_nflverse_team_enrichment(
            args.cache_dir,
            season=season,
            refresh=args.refresh,
        )
        automatic = merge_nflverse_team_enrichment(automatic, enrichment)
    except (OSError, ValueError, DataError):
        # The roster/draft prior remains available when an optional free bulk
        # enrichment release has not been published yet.
        pass
    return merge_team_profiles(automatic, manual)


def _write_or_print(text: str, path: str | None) -> None:
    if path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def _require_candidates(candidates: list[dict[str, Any]], path: str) -> None:
    if candidates:
        return
    raise DataError(
        f"Player file contains headers but no player rows: {Path(path)}. Add one without credentials with "
        f"`draftscope add \"Player Name\" --position WR --school \"School Name\" --season {date.today().year} "
        f"--out {path}`, or use credential-free `draftscope discover`; only the optional `lookup` command requires CFBD_API_KEY."
    )


def _evaluation_command(args: argparse.Namespace) -> int:
    candidates = load_records(args.players)
    _require_candidates(candidates, args.players)
    print(
        f"Evaluating {args.name} with the saved historical model; this can take several minutes...",
        file=sys.stderr,
        flush=True,
    )
    history, metadata, model_history, model_metadata = _load_evaluation_histories(args)
    evaluator = ProspectEvaluator(
        history,
        candidates,
        history_metadata=metadata,
        model_history=model_history,
        model_metadata=model_metadata,
        team_profiles=_profiles(args, candidates),
        as_of_date=args.as_of_date,
    )
    result = evaluator.evaluate(args.name, school=args.school, bootstrap=max(0, args.bootstrap))
    if args.json:
        text = json.dumps(result.to_dict(), indent=2, sort_keys=True, default=str) + "\n"
    elif args.full:
        text = render_evaluation(result)
    else:
        text = render_player_summary(result)
    _write_or_print(text, args.out)
    return 0


def _rank_command(args: argparse.Namespace) -> int:
    candidates = load_records(args.players)
    _require_candidates(candidates, args.players)
    print(
        f"Ranking {len(candidates)} player(s) with forward-validated position models; "
        "this can take several minutes...",
        file=sys.stderr,
        flush=True,
    )
    history, metadata, model_history, model_metadata = _load_evaluation_histories(args)
    evaluator = ProspectEvaluator(
        history,
        candidates,
        history_metadata=metadata,
        model_history=model_history,
        model_metadata=model_metadata,
        team_profiles=_profiles(args, candidates),
        as_of_date=args.as_of_date,
    )
    evaluations = evaluator.rank(bootstrap=0)[: max(1, args.top)]
    rows: list[dict[str, Any]] = []
    for rank, evaluation in enumerate(evaluations, start=1):
        prediction = evaluation.draft_prediction
        rows.append(
            {
                "rank": rank,
                "name": evaluation.player.get("name"),
                "position": evaluation.player.get("position"),
                "school": evaluation.player.get("school"),
                "profile_score": evaluation.profile_score,
                "draft_probability": prediction.probability,
                "conditional_probability": prediction.conditional_probability,
                "conditional_scope": prediction.conditional_scope,
                "entry_probability": prediction.entry_probability,
                "probability_scope": prediction.probability_scope,
                "projected_pick": prediction.projected_pick,
                "evidence_coverage": evaluation.evidence_coverage,
                "profile_tier": evaluation.profile_tier,
                "top_team_fits": "; ".join(fit.team for fit in evaluation.team_fits[:5]),
            }
        )
    text = json.dumps(rows, indent=2, default=str) + "\n" if args.json else render_board(rows)
    if args.out and Path(args.out).suffix.lower() == ".csv":
        write_records(args.out, rows, preferred_fields=("rank", "name", "position", "school"))
    else:
        _write_or_print(text, args.out)
    return 0


def _lookup_command(args: argparse.Namespace) -> int:
    client = CFBDClient()
    base = client.player_record(args.name, year=args.season, team=args.team)
    try:
        record, _ = client.refresh_candidate(base, season=args.season, week=args.week)
        record["data_stale"] = False
        record["refresh_error"] = None
    except DataError as exc:
        record = base
        # Keep any existing production at its old checkpoint when this bounded
        # refresh fails instead of relabeling stale totals as the requested week.
        record["data_stale"] = True
        record["refresh_error"] = str(exc)
        print(f"WARNING: {record.get('name')}: {exc}", file=sys.stderr)
    destination = Path(args.out)
    rows = load_records(destination) if destination.exists() else []
    identity = (str(record.get("cfbd_player_id") or ""), normalize_name(record.get("name")))
    replaced = False
    for index, row in enumerate(rows):
        other = (str(row.get("cfbd_player_id") or ""), normalize_name(row.get("name")))
        if identity[0] and identity[0] == other[0] or identity[1] == other[1]:
            rows[index] = {**row, **record}
            replaced = True
            break
    if not replaced:
        rows.append(record)
    write_records(destination, rows)
    print(f"Saved {record.get('name')} ({record.get('position')}, {record.get('school')}) to {destination}")
    return 0


def _discover_command(args: argparse.Namespace) -> int:
    if args.per_position < 0:
        raise DataError("--per-position must be zero or greater")
    if args.provider == "sportsdataverse":
        from .sportsdataverse import SportsDataverseClient

        client = SportsDataverseClient(args.cache_dir, refresh=True)
        week = (
            args.week
            if args.week is not None
            else client.current_week(args.season)
        )
        rows = client.discover_candidates(
            season=args.season,
            as_of_week=week,
            preseason_prior_year=not args.no_preseason_prior,
            raw_dir=args.raw_dir,
        )
    else:
        client = CFBDClient()
        week = args.week if args.week is not None else client.current_week(year=args.season)
        rows = discover_cfbd_candidates(
            client,
            season=args.season,
            as_of_week=week,
            preseason_prior_year=not args.no_preseason_prior,
            raw_dir=args.raw_dir,
        )
    if args.per_position > 0:
        counts: dict[str, int] = {}
        selected: list[dict[str, Any]] = []
        for row in rows:
            position = str(row.get("position") or "")
            if counts.get(position, 0) >= args.per_position:
                continue
            counts[position] = counts.get(position, 0) + 1
            selected.append(row)
        rows = selected
    if not rows:
        raise DataError(
            f"National FBS discovery returned no candidates from {args.provider}; "
            "the current-season roster release may not be published yet"
        )
    write_records(args.out, rows)
    print(f"Discovered {len(rows)} FBS candidates through completed Week {week}; saved {args.out}")
    return 0


def _template_command(args: argparse.Namespace) -> int:
    if args.kind == "config":
        destination = Path(args.out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(render_config_template(), encoding="utf-8")
        print(f"Wrote config template to {destination}")
        return 0

    if args.kind == "evidence":
        from .scouting import write_template as _write_evidence_template

        _write_evidence_template(args.out, position=None)
        print(f"Wrote evidence template to {Path(args.out)}")
        return 0

    fields = {
        "players": candidate_template_fields,
        "history": historical_template_fields,
        "teams": team_template_fields,
    }[args.kind]()
    write_records(args.out, [], preferred_fields=fields)
    print(f"Wrote {args.kind} template to {Path(args.out)}")
    return 0


def _doctor_command(args: argparse.Namespace) -> int:
    result = diagnose(args.config)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(render_diagnosis(result), end="")
    return 0 if result["ok"] else 1


def _validate_evidence_command(args: argparse.Namespace) -> int:
    from .scouting import load_evidence, validate_evidence, write_findings

    try:
        payload = load_evidence(args.evidence)
    except FileNotFoundError:
        raise DataError(f"Evidence file not found: {args.evidence}")
    findings = validate_evidence(payload)
    write_findings(findings, path=args.out)
    if any(f.get("severity") == "high" for f in findings):
        return 1
    return 0

*** End Patch
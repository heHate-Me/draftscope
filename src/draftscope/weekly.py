from __future__ import annotations

from contextvars import ContextVar
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import shutil
import tempfile
import tomllib
from typing import Any, Mapping, Sequence

from .analysis import ProspectEvaluator
from .data_sources import (
    CFBDClient,
    discover_cfbd_candidates,
    load_nflverse_history,
    load_nflverse_team_profiles,
    merge_team_profiles,
)
from .historical_college import (
    HISTORICAL_PIPELINE_VERSION,
    HISTORICAL_COLLEGE_SCHEMA,
    build_historical_college_training_data,
)
from .model import DraftPrediction
from .records import (
    DataError,
    file_sha256,
    load_records,
    normalize_name,
    normalize_record,
    parse_bool,
    parse_number,
    slug,
    timestamp_utc,
    write_records,
)
from .reporting import render_board, render_evaluation
from .schema import COMMON_PHYSICAL, FILM_PROVENANCE_FIELDS
from .tracking import (
    PromotionPolicy,
    TrackingError,
    TrackingStore,
    artifact_fingerprint,
    canonical_sha256,
)


@dataclass(slots=True)
class WeeklyUpdateResult:
    season: int
    week: int
    players_refreshed: int
    players_failed: int
    snapshot_dir: Path
    board_path: Path
    forecast_run_id: str | None
    model_release_ids: tuple[str, ...]
    warnings: list[str]


@dataclass(slots=True)
class _WeeklyTrackingResult:
    forecast_run_id: str | None
    model_release_ids: tuple[str, ...]
    warnings: list[str]


_PUBLICATION_PATHS: ContextVar[tuple[tuple[Path, Path], ...]] = ContextVar(
    "draftscope_weekly_publication_paths",
    default=(),
)
_STAGING_MARKER_NAME = ".draftscope-staging.json"
_MAX_STAGING_MARKER_BYTES = 16 * 1024
_MAX_PUBLICATION_JOURNAL_BYTES = 64 * 1024


class _WeeklyUpdateLock:
    """Non-blocking process lock whose kernel ownership survives stale files.

    The lock file is intentionally persistent.  On POSIX and Windows the lock
    belongs to the open file descriptor, so the operating system releases it
    if an updater exits or crashes; stale PID text can never keep an update
    blocked.
    """

    def __init__(self, output_dir: Path):
        output_key = hashlib.sha256(str(output_dir.resolve()).encode("utf-8")).hexdigest()[:20]
        self.path = _weekly_lock_directory() / f"draftscope-update-{output_key}.lock"
        self.output_dir = output_dir.resolve()
        self._handle: Any | None = None

    def __enter__(self) -> _WeeklyUpdateLock:
        handle = _open_weekly_lock_file(self.path)
        try:
            self._acquire(handle)
        except OSError as exc:
            handle.seek(0)
            owner = handle.read(4096).strip()
            handle.close()
            detail = f" Lock owner: {owner}" if owner else ""
            raise DataError(
                f"Another DraftScope weekly update is already running for "
                f"{self.output_dir}.{detail} Wait for it to finish, then retry."
            ) from exc
        self._handle = handle
        handle.seek(0)
        handle.truncate()
        json.dump(
            {
                "pid": os.getpid(),
                "started_at": timestamp_utc(),
                "output_dir": str(self.output_dir),
            },
            handle,
            sort_keys=True,
        )
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        return self

    @staticmethod
    def _acquire(handle: Any) -> None:
        if os.name == "nt":  # pragma: no cover - exercised on Windows
            import msvcrt

            handle.seek(0)
            if not handle.read(1):
                handle.write("\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _weekly_lock_directory() -> Path:
    """Return a private, owner-validated directory for persistent lock files."""

    identity = str(os.getuid()) if hasattr(os, "getuid") else "current-user"
    directory = Path(tempfile.gettempdir()).resolve() / f"draftscope-locks-{identity}"
    try:
        directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        metadata = directory.lstat()
    except OSError as exc:
        raise DataError(f"Could not create the weekly-update lock directory: {directory}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or directory.is_symlink():
        raise DataError(f"Weekly-update lock directory is not a real directory: {directory}")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise DataError(f"Weekly-update lock directory is owned by another user: {directory}")
    if os.name != "nt" and metadata.st_mode & 0o077:
        try:
            directory.chmod(0o700)
            metadata = directory.lstat()
        except OSError as exc:
            raise DataError(
                f"Could not make the weekly-update lock directory private: {directory}"
            ) from exc
        if metadata.st_mode & 0o077:
            raise DataError(f"Weekly-update lock directory is not private: {directory}")
    return directory


def _open_weekly_lock_file(path: Path) -> Any:
    """Open a lock without following links, then verify the opened inode."""

    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise DataError(f"Could not securely open weekly-update lock: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        named = path.lstat()
        if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(named.st_mode):
            raise DataError(f"Weekly-update lock is not a regular file: {path}")
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise DataError(f"Weekly-update lock changed while it was opened: {path}")
        if opened.st_nlink != 1:
            raise DataError(f"Weekly-update lock has an unsafe link count: {path}")
        if hasattr(os, "getuid") and opened.st_uid != os.getuid():
            raise DataError(f"Weekly-update lock is owned by another user: {path}")
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, "r+", encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        raise


def load_config(path: str | os.PathLike[str]) -> dict[str, Any]:
    source = Path(path).resolve()
    if not source.exists():
        example = source.parent / "draftscope.toml.example"
        if not example.exists():
            repository_example = Path.cwd() / "draftscope.toml.example"
            example = repository_example if repository_example.exists() else example
        copy_command = (
            f"cp '{example}' '{source}'"
            if example.exists()
            else f"draftscope template config --out '{source}'"
        )
        raise DataError(
            f"DraftScope config not found: {source}. Create it with `{copy_command}`, then run "
            f"`draftscope doctor --config '{source}'`."
        )
    with source.open("rb") as handle:
        config = tomllib.load(handle)
    settings = dict(config.get("draftscope") or config)
    for key in ("players_file", "historical_file", "team_profiles_file", "output_dir", "cache_dir"):
        value = settings.get(key)
        if value and not Path(str(value)).is_absolute():
            settings[key] = str((source.parent / str(value)).resolve())
    return settings


def run_weekly_update(
    config: Mapping[str, Any],
    *,
    refresh_sources: bool = True,
    refresh_players: bool = True,
) -> WeeklyUpdateResult:
    """Build and publish one complete weekly checkpoint.

    All mutable outputs are prepared away from their live paths.  A process
    lock serializes publishers, and a small durable journal makes publication
    recoverable if the process stops between filesystem renames.
    """

    season = int(config.get("season") or date.today().year)
    output_dir = Path(str(config.get("output_dir") or "data")).resolve()
    players_path = Path(
        str(config.get("players_file") or f"data/players_{season}.csv")
    ).resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with _WeeklyUpdateLock(output_dir):
        _recover_interrupted_publication(
            output_dir=output_dir,
            players_path=players_path,
        )
        if _relative_path(players_path, output_dir) is None:
            raise DataError(
                "players_file must be inside output_dir so the refreshed player file and "
                "weekly reports publish as one atomic checkpoint. Move the player CSV beneath "
                f"{output_dir} and update players_file before retrying."
            )
        _cleanup_abandoned_staging(
            output_dir=output_dir,
            players_path=players_path,
        )
        return _run_weekly_update_transaction(
            config,
            output_dir=output_dir,
            players_path=players_path,
            refresh_sources=refresh_sources,
            refresh_players=refresh_players,
        )


def _run_weekly_update_uncommitted(
    config: Mapping[str, Any],
    *,
    refresh_sources: bool = True,
    refresh_players: bool = True,
) -> WeeklyUpdateResult:
    season = int(config.get("season") or date.today().year)
    players_path = Path(str(config.get("players_file") or f"data/players_{season}.csv"))
    if not players_path.exists():
        raise DataError(f"Players file does not exist: {players_path}")
    output_dir = Path(str(config.get("output_dir") or "data")).resolve()
    cache_dir = Path(str(config.get("cache_dir") or ".draftscope-cache")).resolve()
    candidates = load_records(players_path)
    provider = str(
        config.get("college_data_provider") or "sportsdataverse"
    ).strip().lower()
    if provider not in {"sportsdataverse", "cfbd"}:
        raise DataError(
            "college_data_provider must be 'sportsdataverse' or 'cfbd'"
        )
    try:
        cfbd_fallback = parse_bool(config.get("cfbd_fallback")) is True
    except DataError as exc:
        raise DataError("cfbd_fallback must be true or false") from exc
    college_client: Any | None = None
    if provider == "sportsdataverse":
        from .sportsdataverse import SportsDataverseClient

        college_client = SportsDataverseClient(cache_dir)
    elif refresh_players:
        college_client = CFBDClient()
    history_client = college_client
    if provider == "sportsdataverse":
        from .sportsdataverse_adapter import SportsDataverseHistoricalClient

        assert college_client is not None
        history_client = SportsDataverseHistoricalClient(
            college_client,
            nflverse_cache_dir=cache_dir,
            refresh=refresh_sources,
        )
    configured_week = config.get("week")
    if configured_week is not None:
        week = int(configured_week)
    elif refresh_players:
        assert college_client is not None
        try:
            if provider == "sportsdataverse":
                week = college_client.current_week(
                    year=season, refresh=refresh_sources
                )
            else:
                week = college_client.current_week(year=season)
        except DataError:
            week = _saved_checkpoint_week(output_dir, candidates)
    else:
        week = _saved_checkpoint_week(output_dir, candidates)
    if week < 0:
        week = 0
    raw_dir = output_dir / "raw" / provider / str(season) / f"week_{week:02d}"
    raw_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    discovered: list[dict[str, Any]] | None = None
    all_discovered: list[dict[str, Any]] | None = None
    active_refresh_provider = provider
    if refresh_players and bool(config.get("discover_candidates", True)):
        assert college_client is not None
        configured_limit = config.get("discovery_per_position")
        per_position = 50 if configured_limit is None else int(configured_limit)
        if per_position < 0:
            raise DataError("discovery_per_position must be zero or greater")
        try:
            if provider == "sportsdataverse":
                all_discovered = college_client.discover_candidates(
                    season=season,
                    as_of_week=week,
                    preseason_prior_year=bool(
                        config.get("preseason_prior_year", True)
                    ),
                    refresh=refresh_sources,
                    raw_dir=raw_dir,
                )
                discovered = list(all_discovered)
            else:
                discovered = discover_cfbd_candidates(
                    college_client,
                    season=season,
                    as_of_week=week,
                    preseason_prior_year=bool(
                        config.get("preseason_prior_year", True)
                    ),
                    raw_dir=raw_dir,
                )
                all_discovered = discovered
            if per_position > 0:
                discovered = _top_discovered_per_position(discovered, per_position)
                if provider == "sportsdataverse" and all_discovered is not None:
                    discovered = _include_explicitly_tracked_players(
                        discovered,
                        all_discovered,
                        candidates,
                    )
            if not discovered:
                raise DataError("National FBS discovery returned no candidates")
        except DataError as exc:
            discovered = None
            all_discovered = None
            warnings.append(f"{provider} national discovery was unavailable: {exc}")
            if provider == "sportsdataverse" and cfbd_fallback:
                try:
                    college_client = CFBDClient()
                    active_refresh_provider = "cfbd"
                    week = (
                        int(configured_week)
                        if configured_week is not None
                        else college_client.current_week(year=season)
                    )
                    fallback_raw = (
                        output_dir
                        / "raw"
                        / "cfbd"
                        / str(season)
                        / f"week_{week:02d}"
                    )
                    fallback_raw.mkdir(parents=True, exist_ok=True)
                    discovered = discover_cfbd_candidates(
                        college_client,
                        season=season,
                        as_of_week=week,
                        preseason_prior_year=bool(
                            config.get("preseason_prior_year", True)
                        ),
                        raw_dir=fallback_raw,
                    )
                    all_discovered = discovered
                    if per_position > 0:
                        discovered = _top_discovered_per_position(
                            discovered, per_position
                        )
                    raw_dir = fallback_raw
                    warnings.append("Used the explicitly enabled CFBD fallback.")
                except DataError as fallback_exc:
                    discovered = None
                    all_discovered = None
                    warnings.append(f"CFBD fallback was unavailable: {fallback_exc}")

    if refresh_players:
        if active_refresh_provider == "sportsdataverse":
            if discovered is None:
                refreshed, failed, reused = _reuse_saved_checkpoint(
                    candidates, week=week
                )
                warnings.append(
                    f"Reused {reused} saved Week {week} player record(s) and withheld "
                    f"{failed} older/mismatched record(s); the current SportsDataverse "
                    "roster/player-box release is not available yet."
                )
            else:
                refreshed, failed, refresh_warnings = _refresh_bulk_candidate_pool(
                    candidates,
                    discovered=discovered,
                    week=week,
                )
                warnings.extend(refresh_warnings)
        else:
            assert college_client is not None
            refreshed, failed, refresh_warnings = _refresh_candidate_pool(
                college_client,
                candidates,
                discovered=discovered,
                season=season,
                week=week,
                raw_dir=raw_dir,
            )
            warnings.extend(refresh_warnings)
    else:
        refreshed, failed, reused = _reuse_saved_checkpoint(candidates, week=week)
        if reused:
            warnings.append(
                f"Reused {reused} saved player record(s) already labeled for Week {week}; no live player refresh was requested."
            )

    write_records(players_path, refreshed)
    benchmark_history, benchmark_metadata = load_nflverse_history(
        cache_dir,
        lookback_years=int(config.get("lookback_years") or 10),
        end_year=int(config["benchmark_end_year"]) if config.get("benchmark_end_year") else None,
        refresh=refresh_sources,
    )
    history_file = str(config.get("historical_file") or "").strip()
    latest_model_history: Path | None = None
    if history_file:
        model_history = load_records(history_file)
        populations = {str(row.get("population")) for row in model_history if row.get("population")}
        kinds = {str(row.get("probability_kind")) for row in model_history if row.get("probability_kind")}
        conditions = {
            str(row.get("probability_condition"))
            for row in model_history
            if row.get("probability_condition")
        }
        model_metadata = {
            "source": "custom historical file",
            "population": next(iter(populations)) if len(populations) == 1 else "provided historical risk set",
            "probability_kind": next(iter(kinds)) if len(kinds) == 1 else "unknown_risk_set",
        }
        if len(conditions) == 1:
            model_metadata["probability_condition"] = next(iter(conditions))
        stages = {str(row.get("model_stage")) for row in model_history if row.get("model_stage")}
        if len(stages) == 1:
            model_metadata["model_stage"] = next(iter(stages))
        latest_model_history = Path(history_file).resolve()
    elif bool(config.get("build_weekly_history", True)):
        history_output = output_dir / "model_history" / "college_history.csv"
        metadata_output = history_output.with_suffix(".metadata.json")
        lookback = int(config.get("lookback_years") or 10)
        college_seasons = tuple(range(season - lookback, season))
        try:
            reuse = False
            if history_output.is_file() and metadata_output.is_file():
                try:
                    cached_metadata = json.loads(metadata_output.read_text(encoding="utf-8"))
                    artifact = cached_metadata.get("training_artifact") or {}
                    reuse = bool(
                        cached_metadata.get("model_stage") == "college_precombine"
                        and str(
                            cached_metadata.get("college_data_provider") or "cfbd"
                        ).lower()
                        == provider
                        and str(cached_metadata.get("schema_version") or "") == "1.2"
                        and str(
                            cached_metadata.get("historical_pipeline_version") or ""
                        )
                        == HISTORICAL_PIPELINE_VERSION
                        and tuple(cached_metadata.get("schema_fields") or ())
                        == tuple(HISTORICAL_COLLEGE_SCHEMA)
                        and int(cached_metadata.get("as_of_week")) == week
                        and tuple(int(value) for value in cached_metadata.get("college_seasons", ()))
                        == college_seasons
                        and cached_metadata.get("contract_audit", {}).get("status") == "pass"
                        and not cached_metadata.get("source_failures")
                        and cached_metadata.get("quality_gate_status", "pass") == "pass"
                        and float(cached_metadata.get("positive_match_coverage") or 0.0) >= 1.0
                        and artifact.get("sha256") == file_sha256(history_output)
                        and int(artifact.get("bytes") or -1) == history_output.stat().st_size
                    )
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    reuse = False
            if reuse:
                model_history = load_records(history_output)
                model_metadata = cached_metadata
            else:
                raw_cache = str(config.get("historical_raw_cache_dir") or "").strip()
                model_history, model_metadata = build_historical_college_training_data(
                    history_client,
                    college_seasons=college_seasons,
                    cache_dir=raw_cache or None,
                    nflverse_draft_path=cache_dir / "draft_picks.csv",
                    as_of_week=week,
                    refresh=refresh_sources,
                    strict=bool(config.get("strict_model_data", True)),
                )
                write_records(
                    history_output,
                    model_history,
                    preferred_fields=HISTORICAL_COLLEGE_SCHEMA,
                )
                model_metadata["training_artifact"] = {
                    "sha256": file_sha256(history_output),
                    "bytes": history_output.stat().st_size,
                }
                metadata_output.write_text(
                    json.dumps(model_metadata, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            latest_model_history = history_output.resolve()
        except (DataError, OSError) as exc:
            model_history, model_metadata = benchmark_history, benchmark_metadata
            warnings.append(
                f"Checkpoint-aligned college model history could not be built; using combine-stage fallback: {exc}"
            )
    else:
        model_history, model_metadata = benchmark_history, benchmark_metadata
    profiles_path = str(config.get("team_profiles_file") or "").strip()
    manual_profiles = load_records(profiles_path) if profiles_path and Path(profiles_path).exists() else []
    automatic_profiles = []
    if bool(config.get("auto_team_needs", True)):
        automatic_profiles = load_nflverse_team_profiles(cache_dir, season=season, refresh=refresh_sources)
        try:
            from .nflverse_free import (
                load_nflverse_team_enrichment,
                merge_nflverse_team_enrichment,
            )

            nfl_enrichment, nfl_enrichment_metadata = load_nflverse_team_enrichment(
                cache_dir,
                season=season,
                refresh=refresh_sources,
            )
            automatic_profiles = merge_nflverse_team_enrichment(
                automatic_profiles,
                nfl_enrichment,
            )
            if nfl_enrichment_metadata.get("status") != "pass":
                warnings.append(
                    "Optional nflverse depth/starter enrichment is partial; "
                    "unsupported team-fit components remain visibly withheld."
                )
        except (OSError, ValueError, DataError) as exc:
            warnings.append(
                f"Optional nflverse depth/starter enrichment was unavailable: {exc}"
            )
    team_profiles = merge_team_profiles(automatic_profiles, manual_profiles)
    evaluator = ProspectEvaluator(
        benchmark_history,
        refreshed,
        history_metadata=benchmark_metadata,
        model_history=model_history,
        model_metadata=model_metadata,
        team_profiles=team_profiles,
        as_of_date=date.today().isoformat(),
    )
    evaluations = evaluator.rank(bootstrap=int(config.get("bootstrap") or 0))
    try:
        tracking_result = _track_weekly_forecasts(
            output_dir=output_dir,
            evaluator=evaluator,
            evaluations=evaluations,
            season=season,
            week=week,
            players_path=players_path,
            model_history=model_history,
            model_metadata=model_metadata,
            model_history_path=latest_model_history,
            benchmark_history=benchmark_history,
            benchmark_metadata=benchmark_metadata,
            team_profiles=team_profiles,
        )
    except Exception as exc:  # Tracking must never suppress the weekly scouting output.
        tracking_result = _WeeklyTrackingResult(
            forecast_run_id=None,
            model_release_ids=(),
            warnings=[f"Model tracking failed; board generation continued without a ledger entry: {exc}"],
        )
    warnings.extend(tracking_result.warnings)

    state_path = output_dir / "state.json"
    previous_rows: list[dict[str, Any]] = []
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            previous_value = str(state.get("latest_board") or "").strip()
            previous_board = Path(previous_value) if previous_value else None
            if previous_board is not None and previous_board.is_file():
                previous_rows = load_records(previous_board)
        except (json.JSONDecodeError, OSError, DataError):
            warnings.append("Previous board state could not be read; movement columns start fresh.")
    previous_map = {
        (normalize_name(row.get("name")), normalize_name(row.get("school"))): row for row in previous_rows
    }
    board_rows: list[dict[str, Any]] = []
    for rank, evaluation in enumerate(evaluations, start=1):
        player = evaluation.player
        key = (normalize_name(player.get("name")), normalize_name(player.get("school")))
        previous = previous_map.get(key, {})
        previous_rank = _float(previous.get("rank"))
        previous_profile = _float(previous.get("profile_score"))
        previous_probability = _first_float(
            previous,
            "ranking_probability",
            "draft_probability",
            "conditional_probability",
        )
        previous_scope = str(previous.get("ranking_probability_scope") or previous.get("probability_scope") or "")
        profile = evaluation.profile_score
        prediction = evaluation.draft_prediction
        ranking_probability = (
            prediction.probability
            if prediction.probability is not None
            else prediction.conditional_probability
        )
        probability_scope = prediction.probability_scope
        ranking_scope = (
            "next_draft"
            if prediction.probability is not None
            else prediction.conditional_scope
        )
        board_rows.append(
            {
                "rank": rank,
                "previous_rank": previous_rank,
                "rank_delta": previous_rank - rank if previous_rank is not None else None,
                "name": player.get("name"),
                "position": player.get("position"),
                "school": player.get("school"),
                "as_of_week": player.get("as_of_week"),
                "profile_score": profile,
                "profile_delta": profile - previous_profile if profile is not None and previous_profile is not None else None,
                "draft_probability": prediction.probability,
                "conditional_probability": prediction.conditional_probability,
                "conditional_scope": prediction.conditional_scope,
                "entry_probability": prediction.entry_probability,
                "probability_scope": probability_scope,
                "ranking_probability": ranking_probability,
                "ranking_probability_scope": ranking_scope,
                "draft_probability_delta": (
                    ranking_probability - previous_probability
                    if ranking_probability is not None
                    and previous_probability is not None
                    and ranking_scope == previous_scope
                    else None
                ),
                "projected_pick": prediction.projected_pick,
                "model_release_id": prediction.model_release_id or None,
                "forecast_run_id": tracking_result.forecast_run_id,
                "evidence_coverage": evaluation.evidence_coverage,
                "profile_tier": evaluation.profile_tier,
                "top_team_fits": "; ".join(fit.team for fit in evaluation.team_fits[:5]),
            }
        )

    snapshot_dir = output_dir / "snapshots" / str(season) / f"week_{week:02d}"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    write_records(snapshot_dir / "players.csv", refreshed)
    board_path = snapshot_dir / "board.csv"
    write_records(board_path, board_rows, preferred_fields=("rank", "previous_rank", "rank_delta", "name", "position", "school"))
    (snapshot_dir / "board.txt").write_text(render_board(board_rows), encoding="utf-8")
    reports_dir = snapshot_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    for evaluation in evaluations:
        filename = slug(f"{evaluation.player.get('name')}_{evaluation.player.get('school')}") or "player"
        (reports_dir / f"{filename}.txt").write_text(render_evaluation(evaluation), encoding="utf-8")
        (reports_dir / f"{filename}.json").write_text(
            json.dumps(evaluation.to_dict(), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )
    latest_dir = output_dir / "reports" / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)
    current_board_available = bool(board_rows)
    if current_board_available:
        shutil.copy2(board_path, latest_dir / "board.csv")
        shutil.copy2(snapshot_dir / "board.txt", latest_dir / "board.txt")
    else:
        # The transaction may have started from a previously successful
        # checkpoint. Do not leave a stale "latest" board beside a state file
        # that correctly says the current board is unavailable.
        (latest_dir / "board.csv").unlink(missing_ok=True)
        (latest_dir / "board.txt").unlink(missing_ok=True)
    warnings = [_published_text(warning) for warning in warnings]
    state = {
        "season": season,
        "week": week,
        "updated_at": timestamp_utc(),
        "latest_snapshot": str(_published_path(snapshot_dir)),
        "latest_board": (
            str(_published_path(board_path)) if current_board_available else None
        ),
        "current_board_available": current_board_available,
        "current_board_reason": (
            None
            if current_board_available
            else "No current-season player rows were available for ranking."
        ),
        "latest_model_history": (
            str(_published_path(latest_model_history)) if latest_model_history else None
        ),
        "latest_forecast_run_id": tracking_result.forecast_run_id,
        "model_release_ids": list(tracking_result.model_release_ids),
        "players_refreshed": len(refreshed) - failed,
        "players_failed": failed,
        "warnings": warnings,
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return WeeklyUpdateResult(
        season=season,
        week=week,
        players_refreshed=len(refreshed) - failed,
        players_failed=failed,
        snapshot_dir=snapshot_dir,
        board_path=board_path,
        forecast_run_id=tracking_result.forecast_run_id,
        model_release_ids=tracking_result.model_release_ids,
        warnings=warnings,
    )


def _run_weekly_update_transaction(
    config: Mapping[str, Any],
    *,
    output_dir: Path,
    players_path: Path,
    refresh_sources: bool,
    refresh_players: bool,
) -> WeeklyUpdateResult:
    if not players_path.is_file():
        raise DataError(f"Players file does not exist: {players_path}")
    if output_dir.exists() and not output_dir.is_dir():
        raise DataError(f"Weekly output path is not a directory: {output_dir}")
    players_relative = _relative_path(players_path, output_dir)
    if players_relative is None:
        raise DataError("players_file must be inside output_dir for atomic publication")

    transaction_root = _create_output_transaction_root(
        output_dir=output_dir,
        players_path=players_path,
    )
    staged_output = transaction_root / "next-output"
    journal_path = _publication_journal_path(output_dir)
    try:
        if output_dir.exists():
            shutil.copytree(output_dir, staged_output)
        else:
            staged_output.mkdir(parents=True)

        staged_players = staged_output / players_relative
        if not staged_players.is_file():
            staged_players.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(players_path, staged_players)

        staged_config = _staged_config(
            config,
            output_dir=output_dir,
            staged_output=staged_output,
        )
        staged_config["output_dir"] = str(staged_output)
        staged_config["players_file"] = str(staged_players)
        token = _PUBLICATION_PATHS.set(((staged_output, output_dir),))
        try:
            prepared = _run_weekly_update_uncommitted(
                staged_config,
                refresh_sources=refresh_sources,
                refresh_players=refresh_players,
            )
            _verify_prepared_update(prepared, staged_players=staged_players)
            published = WeeklyUpdateResult(
                season=prepared.season,
                week=prepared.week,
                players_refreshed=prepared.players_refreshed,
                players_failed=prepared.players_failed,
                snapshot_dir=_published_path(prepared.snapshot_dir),
                board_path=_published_path(prepared.board_path),
                forecast_run_id=prepared.forecast_run_id,
                model_release_ids=prepared.model_release_ids,
                warnings=prepared.warnings,
            )
            targets = [
                {
                    "name": "output",
                    "final": str(output_dir),
                    "staged": str(staged_output),
                    "backup": str(transaction_root / "previous-output"),
                    "had_original": output_dir.exists(),
                }
            ]
            _publish_prepared_update(
                journal_path=journal_path,
                transaction_root=transaction_root,
                output_dir=output_dir,
                players_path=players_path,
                targets=targets,
            )
            return published
        finally:
            _PUBLICATION_PATHS.reset(token)
    finally:
        # A retained journal means recovery still needs its staged/backup tree.
        if not journal_path.exists():
            _remove_path(transaction_root)


def _staged_config(
    config: Mapping[str, Any],
    *,
    output_dir: Path,
    staged_output: Path,
) -> dict[str, Any]:
    staged = dict(config)
    for key in (
        "cache_dir",
        "historical_file",
        "historical_raw_cache_dir",
        "team_profiles_file",
    ):
        raw_value = str(staged.get(key) or "").strip()
        if not raw_value:
            continue
        candidate = Path(raw_value).resolve()
        relative = _relative_path(candidate, output_dir)
        if relative is not None:
            staged[key] = str(staged_output / relative)
    return staged


def _verify_prepared_update(
    result: WeeklyUpdateResult,
    *,
    staged_players: Path,
) -> None:
    required = (
        staged_players,
        result.snapshot_dir / "players.csv",
        result.board_path,
        result.snapshot_dir / "board.txt",
        result.snapshot_dir / "reports",
        result.snapshot_dir.parents[2] / "state.json",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise DataError(
            "Weekly update did not produce a complete checkpoint: " + ", ".join(missing)
        )
    # Parsing both records before publication catches truncated/invalid files.
    load_records(staged_players)
    board_rows = load_records(result.board_path)
    state_path = result.snapshot_dir.parents[2] / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataError("Prepared weekly state is not valid JSON") from exc
    if int(state.get("season", -1)) != result.season or int(state.get("week", -1)) != result.week:
        raise DataError("Prepared weekly state does not match its snapshot checkpoint")
    if board_rows:
        if state.get("current_board_available") is not True:
            raise DataError("Prepared weekly state withholds a populated board")
        if Path(str(state.get("latest_board") or "")).resolve() != _published_path(
            result.board_path
        ):
            raise DataError("Prepared weekly state points to the wrong board")
    elif state.get("current_board_available") is not False or state.get(
        "latest_board"
    ) not in (None, ""):
        raise DataError("Prepared weekly state advertises an empty board")


def _publish_prepared_update(
    *,
    journal_path: Path,
    transaction_root: Path,
    output_dir: Path,
    players_path: Path,
    targets: list[dict[str, Any]],
) -> None:
    journal: dict[str, Any] = {
        "schema_version": 1,
        "transaction_root": str(transaction_root),
        "output_dir": str(output_dir),
        "players_path": str(players_path),
        "committed": False,
        "targets": targets,
    }
    _write_publication_journal(journal_path, journal)
    try:
        for target in targets:
            final = Path(str(target["final"]))
            staged = Path(str(target["staged"]))
            backup = Path(str(target["backup"]))
            if target["had_original"]:
                if not final.exists():
                    raise OSError(f"Publication target disappeared before commit: {final}")
                if final.is_file():
                    _backup_publication_file(final, backup)
                else:
                    _publication_replace(final, backup)
                target["backed_up"] = True
                _write_publication_journal(journal_path, journal)
            _publication_replace(staged, final)
            target["published"] = True
            _fsync_directory(final.parent)
            _write_publication_journal(journal_path, journal)
        journal["committed"] = True
        _write_publication_journal(journal_path, journal)
    except BaseException as exc:
        try:
            _restore_publication_targets(targets)
            journal_path.unlink(missing_ok=True)
            _fsync_directory(journal_path.parent)
        except BaseException as rollback_exc:
            raise DataError(
                f"Weekly update publication failed and automatic rollback could not finish. "
                f"The recovery journal is {journal_path}: {rollback_exc}"
            ) from exc
        raise

    # The commit marker is durable before old checkpoints are removed.  A
    # crash here is safe: recovery keeps the complete new targets and resumes
    # this cleanup.
    _cleanup_committed_publication(targets, journal_path=journal_path)


def _recover_interrupted_publication(*, output_dir: Path, players_path: Path) -> None:
    journal_path = _publication_journal_path(output_dir)
    try:
        journal_path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise DataError(
            f"The interrupted weekly-update journal could not be inspected: {journal_path}."
        ) from exc
    try:
        payload = json.loads(
            _read_small_regular_file(journal_path, _MAX_PUBLICATION_JOURNAL_BYTES)
        )
        if not isinstance(payload, Mapping):
            raise ValueError("publication journal must be a JSON object")
        targets = _validated_recovery_targets(
            payload,
            output_dir=output_dir,
            players_path=players_path,
        )
        transaction_root = Path(str(payload["transaction_root"])).resolve()
        if not _verified_output_staging_directory(
            transaction_root,
            output_dir=output_dir,
            players_path=players_path,
        ):
            raise ValueError("transaction staging marker could not be verified")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise DataError(
            f"An interrupted weekly-update journal could not be validated: {journal_path}. "
            "Leave the file in place and inspect it before retrying."
        ) from exc
    if payload.get("committed") is True and all(
        Path(str(target["final"])).exists() for target in targets
    ):
        _cleanup_committed_publication(targets, journal_path=journal_path)
        if journal_path.exists():
            raise DataError(
                f"A committed weekly update is intact, but its recovery files could "
                f"not be cleaned: {journal_path}."
            )
        return
    try:
        _restore_publication_targets(targets)
        for target in targets:
            _remove_path(Path(str(target["staged"])))
            _remove_path(Path(str(target["backup"])))
        journal_path.unlink(missing_ok=True)
        _fsync_directory(journal_path.parent)
        _remove_path(transaction_root)
    except OSError as exc:
        raise DataError(
            f"Could not recover the interrupted weekly update recorded at {journal_path}: {exc}"
        ) from exc


def _validated_recovery_targets(
    payload: Mapping[str, Any],
    *,
    output_dir: Path,
    players_path: Path,
) -> list[dict[str, Any]]:
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported publication journal")
    if Path(str(payload.get("output_dir") or "")).resolve() != output_dir:
        raise ValueError("journal output mismatch")
    if Path(str(payload.get("players_path") or "")).resolve() != players_path:
        raise ValueError("journal players mismatch")
    transaction_root = Path(str(payload.get("transaction_root") or "")).resolve()
    expected_prefix = f".{output_dir.name}.draftscope-update-"
    if transaction_root.parent != output_dir.parent or not transaction_root.name.startswith(
        expected_prefix
    ):
        raise ValueError("unsafe transaction root")
    if not _safe_transaction_parent(output_dir.parent):
        raise ValueError("unsafe shared output parent")
    raw_targets = payload.get("targets")
    if not isinstance(raw_targets, list):
        raise ValueError("journal targets are missing")
    expected_finals = {output_dir}
    if _relative_path(players_path, output_dir) is None:
        expected_finals.add(players_path)
    targets = [dict(target) for target in raw_targets if isinstance(target, Mapping)]
    finals = {Path(str(target.get("final") or "")).resolve() for target in targets}
    if finals != expected_finals or len(targets) != len(expected_finals):
        raise ValueError("journal targets do not match this update")
    for target in targets:
        final = Path(str(target.get("final") or "")).resolve()
        staged = Path(str(target.get("staged") or "")).resolve()
        backup = Path(str(target.get("backup") or "")).resolve()
        if not isinstance(target.get("had_original"), bool):
            raise ValueError("journal target has an invalid original-state flag")
        if final == output_dir:
            if (
                target.get("name") != "output"
                or staged != transaction_root / "next-output"
                or backup != transaction_root / "previous-output"
            ):
                raise ValueError("unsafe output recovery path")
        else:
            staged_prefix = f".{players_path.name}.draftscope-update-next-"
            backup_prefix = f".{players_path.name}.draftscope-update-backup-"
            if (
                target.get("name") != "players"
                or target.get("had_original") is not True
                or staged.parent != players_path.parent
                or backup.parent != players_path.parent
                or not staged.name.startswith(staged_prefix)
                or not backup.name.startswith(backup_prefix)
                or staged == backup
            ):
                raise ValueError("unsafe player recovery path")
            if not _safe_transaction_parent(players_path.parent):
                raise ValueError("unsafe shared player parent")
        target["final"] = str(final)
        target["staged"] = str(staged)
        target["backup"] = str(backup)
    return targets


def _restore_publication_targets(targets: Sequence[Mapping[str, Any]]) -> None:
    for target in reversed(targets):
        final = Path(str(target["final"]))
        staged = Path(str(target["staged"]))
        backup = Path(str(target["backup"]))
        had_original = bool(target.get("had_original"))
        if backup.exists():
            _remove_path(final)
            _publication_replace(backup, final)
            _fsync_directory(final.parent)
        elif not had_original and not staged.exists() and final.exists():
            _remove_path(final)
            _fsync_directory(final.parent)


def _cleanup_committed_publication(
    targets: Sequence[Mapping[str, Any]],
    *,
    journal_path: Path,
) -> None:
    try:
        for target in targets:
            _remove_path(Path(str(target["backup"])))
            _remove_path(Path(str(target["staged"])))
        journal_path.unlink(missing_ok=True)
        _fsync_directory(journal_path.parent)
    except OSError:
        # The committed journal deliberately remains recoverable.  The next
        # update will retry cleanup while retaining the complete new outputs.
        return


def _cleanup_abandoned_staging(*, output_dir: Path, players_path: Path) -> None:
    """Remove only marker-verified debris from a killed pre-publication build."""

    if not _safe_transaction_parent(output_dir.parent):
        return
    try:
        _publication_journal_path(output_dir).lstat()
    except FileNotFoundError:
        pass
    else:
        return
    output_prefix = f".{output_dir.name}.draftscope-update-"
    try:
        candidates = list(output_dir.parent.iterdir())
    except FileNotFoundError:
        return
    for candidate in candidates:
        if not candidate.name.startswith(output_prefix):
            continue
        if _verified_output_staging_directory(
            candidate,
            output_dir=output_dir,
            players_path=players_path,
        ):
            _remove_path(candidate)


def _create_output_transaction_root(*, output_dir: Path, players_path: Path) -> Path:
    if not _safe_transaction_parent(output_dir.parent):
        raise DataError(
            f"Weekly output parent is writable by other users without sticky-directory "
            f"protection: {output_dir.parent}"
        )
    transaction_root = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}.draftscope-update-",
            dir=output_dir.parent,
        )
    ).resolve()
    try:
        transaction_root.chmod(0o700)
        _write_publication_journal(
            transaction_root / _STAGING_MARKER_NAME,
            {
                "schema_version": 1,
                "kind": "weekly-output-staging",
                "transaction_root": str(transaction_root),
                "output_dir": str(output_dir),
                "players_path": str(players_path),
            },
        )
    except BaseException:
        _remove_path(transaction_root)
        raise
    return transaction_root


def _verified_output_staging_directory(
    candidate: Path,
    *,
    output_dir: Path,
    players_path: Path,
) -> bool:
    """Verify an abandoned directory before recursive deletion."""

    try:
        directory_metadata = candidate.lstat()
        if not stat.S_ISDIR(directory_metadata.st_mode):
            return False
        if hasattr(os, "getuid") and directory_metadata.st_uid != os.getuid():
            return False
        if os.name != "nt" and directory_metadata.st_mode & 0o077:
            return False
        entries = list(candidate.iterdir())
        if any(
            entry.name not in {_STAGING_MARKER_NAME, "next-output", "previous-output"}
            for entry in entries
        ):
            return False
        for entry in entries:
            if entry.name == _STAGING_MARKER_NAME:
                continue
            entry_metadata = entry.lstat()
            if not stat.S_ISDIR(entry_metadata.st_mode):
                return False
            if hasattr(os, "getuid") and entry_metadata.st_uid != os.getuid():
                return False
        marker = candidate / _STAGING_MARKER_NAME
        payload = json.loads(_read_small_regular_file(marker, _MAX_STAGING_MARKER_BYTES))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    return (
        payload.get("schema_version") == 1
        and payload.get("kind") == "weekly-output-staging"
        and Path(str(payload.get("transaction_root") or "")).resolve()
        == candidate.resolve()
        and Path(str(payload.get("output_dir") or "")).resolve()
        == output_dir.resolve()
        and Path(str(payload.get("players_path") or "")).resolve()
        == players_path.resolve()
    )


def _safe_transaction_parent(path: Path) -> bool:
    """Reject rename races in shared parents lacking sticky-directory protection."""

    try:
        metadata = path.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        return False
    if os.name == "nt":  # Windows temp/workspace ACLs are not represented by mode bits.
        return True
    shared_write = metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    return not shared_write or bool(metadata.st_mode & stat.S_ISVTX)


def _read_small_regular_file(path: Path, maximum_bytes: int) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        named = path.lstat()
        if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(named.st_mode):
            raise OSError("not a regular file")
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise OSError("file changed while opening")
        if opened.st_nlink != 1:
            raise OSError("file has an unsafe link count")
        if hasattr(os, "getuid") and opened.st_uid != os.getuid():
            raise OSError("file is owned by another user")
        if os.name != "nt" and opened.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise OSError("file is writable by other users")
        if opened.st_size > maximum_bytes:
            raise OSError("file exceeds size limit")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(maximum_bytes + 1)
        if len(payload) > maximum_bytes:
            raise OSError("file exceeds size limit")
        return payload.decode("utf-8")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_publication_journal(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _publication_journal_path(output_dir: Path) -> Path:
    digest = hashlib.sha256(str(output_dir).encode("utf-8")).hexdigest()[:12]
    return output_dir.parent / f".{output_dir.name}.draftscope-transaction-{digest}.json"


def _publication_replace(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def _backup_publication_file(source: Path, destination: Path) -> None:
    """Keep the live file readable until its atomic replacement is ready."""

    try:
        os.link(source, destination)
    except OSError:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            dir=destination.parent,
        )
        os.close(descriptor)
        try:
            shutil.copy2(source, temporary_name)
            with Path(temporary_name).open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary_name, destination)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
    _fsync_directory(destination.parent)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":  # pragma: no cover - directory fsync is POSIX-specific
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _relative_path(path: Path, parent: Path) -> Path | None:
    try:
        return path.resolve().relative_to(parent.resolve())
    except ValueError:
        return None


def _published_path(path: Path) -> Path:
    resolved = path.resolve()
    for staged, published in _PUBLICATION_PATHS.get():
        relative = _relative_path(resolved, staged)
        if relative is not None:
            return published / relative
    return resolved


def _published_text(value: str) -> str:
    published = value
    for staged, destination in _PUBLICATION_PATHS.get():
        published = published.replace(str(staged), str(destination))
    return published


def _portable_artifact_fingerprint(path: Path, *, root: Path) -> dict[str, Any]:
    """Record a checkout-independent artifact path relative to its output tree."""

    result = artifact_fingerprint(path)
    try:
        result["path"] = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise TrackingError(f"Tracking artifact is outside its output tree: {path}") from exc
    return result


def _reuse_saved_checkpoint(
    candidates: Sequence[Mapping[str, Any]],
    *,
    week: int,
) -> tuple[list[dict[str, Any]], int, int]:
    """Reuse same-week evidence and withhold rows from older checkpoints."""

    refreshed: list[dict[str, Any]] = []
    reused = 0
    for candidate in candidates:
        retained = dict(candidate)
        candidate_week = parse_number(retained.get("as_of_week"))
        if candidate_week == week:
            retained["data_stale"] = False
            retained["refresh_error"] = None
            reused += 1
        else:
            saved_label = (
                f"Week {int(candidate_week)}"
                if candidate_week is not None
                else "an unknown week"
            )
            retained["data_stale"] = True
            retained["refresh_error"] = (
                f"Saved evidence is from {saved_label}, not requested Week {week}."
            )
        refreshed.append(retained)
    failed = sum(parse_bool(row.get("data_stale")) is True for row in refreshed)
    return refreshed, failed, reused


def _saved_checkpoint_week(
    output_dir: Path,
    candidates: Sequence[Mapping[str, Any]],
) -> int:
    saved_state = output_dir / "state.json"
    try:
        state = json.loads(saved_state.read_text(encoding="utf-8"))
        return max(0, int(state["week"]))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        candidate_weeks = [
            int(value)
            for row in candidates
            if (value := parse_number(row.get("as_of_week"))) is not None
        ]
        return max(candidate_weeks, default=0)


def _player_identity_values(row: Mapping[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in ("espn_player_id", "cfbd_player_id", "player_id"):
        value = str(row.get(key) or "").strip()
        if not value:
            continue
        values.add(value)
        if ":" in value:
            values.add(value.rsplit(":", 1)[-1])
    return values


def _include_explicitly_tracked_players(
    selected: Sequence[Mapping[str, Any]],
    national_pool: Sequence[Mapping[str, Any]],
    tracked: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Keep tracked players even when the per-position discovery cap omits them."""

    output = [dict(row) for row in selected]
    selected_keys = {
        (normalize_name(row.get("name")), normalize_name(row.get("school")))
        for row in output
    }
    by_identity: dict[str, Mapping[str, Any]] = {}
    by_name_school: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in national_pool:
        for identity in _player_identity_values(row):
            by_identity.setdefault(identity, row)
        by_name_school.setdefault(
            (normalize_name(row.get("name")), normalize_name(row.get("school"))),
            row,
        )
    for tracked_row in tracked:
        match = next(
            (
                by_identity[identity]
                for identity in _player_identity_values(tracked_row)
                if identity in by_identity
            ),
            None,
        )
        if match is None:
            match = by_name_school.get(
                (
                    normalize_name(tracked_row.get("name")),
                    normalize_name(tracked_row.get("school")),
                )
            )
        if match is None:
            continue
        key = (normalize_name(match.get("name")), normalize_name(match.get("school")))
        if key in selected_keys:
            continue
        output.append(dict(match))
        selected_keys.add(key)
    return output


def _refresh_bulk_candidate_pool(
    tracked: Sequence[Mapping[str, Any]],
    *,
    discovered: Sequence[Mapping[str, Any]],
    week: int,
) -> tuple[list[dict[str, Any]], int, list[str]]:
    """Merge one complete, checkpoint-bounded bulk release without API calls."""

    by_identity: dict[str, int] = {}
    by_name_school: dict[tuple[str, str], int] = {}
    for index, candidate in enumerate(tracked):
        for identity in _player_identity_values(candidate):
            by_identity.setdefault(identity, index)
        by_name_school.setdefault(
            (normalize_name(candidate.get("name")), normalize_name(candidate.get("school"))),
            index,
        )

    refreshed: list[dict[str, Any]] = []
    matched: set[int] = set()
    for discovered_player in discovered:
        index = next(
            (
                by_identity[identity]
                for identity in _player_identity_values(discovered_player)
                if identity in by_identity
            ),
            None,
        )
        if index is None:
            index = by_name_school.get(
                (
                    normalize_name(discovered_player.get("name")),
                    normalize_name(discovered_player.get("school")),
                )
            )
        existing = tracked[index] if index is not None else None
        if index is not None:
            matched.add(index)
        merged = _merge_discovered_player(discovered_player, existing, week=week)
        merged["data_stale"] = False
        merged["refresh_error"] = None
        refreshed.append(merged)

    unmatched = 0
    for index, candidate in enumerate(tracked):
        if index in matched:
            continue
        retained = dict(candidate)
        retained["data_stale"] = True
        retained["refresh_error"] = (
            f"Player was not present in the SportsDataverse FBS roster for Week {week}"
        )
        refreshed.append(normalize_record(retained))
        unmatched += 1
    warnings = []
    if unmatched:
        warnings.append(
            f"Withheld {unmatched} tracked player record(s) not present in the current "
            "SportsDataverse FBS roster; their older evidence was not relabeled."
        )
    return refreshed, unmatched, warnings


def _track_weekly_forecasts(
    *,
    output_dir: Path,
    evaluator: Any,
    evaluations: Sequence[Any],
    season: int,
    week: int,
    players_path: Path,
    model_history: Sequence[Mapping[str, Any]],
    model_metadata: Mapping[str, Any],
    model_history_path: Path | None,
    benchmark_history: Sequence[Mapping[str, Any]],
    benchmark_metadata: Mapping[str, Any],
    team_profiles: Sequence[Mapping[str, Any]],
) -> _WeeklyTrackingResult:
    """Register fitted models and append one all-or-nothing immutable forecast run."""

    warnings: list[str] = []
    store = TrackingStore(output_dir)
    artifacts = _snapshot_tracking_inputs(
        output_dir=output_dir,
        season=season,
        week=week,
        players_path=players_path,
        model_history=model_history,
        model_metadata=model_metadata,
        model_history_path=model_history_path,
        benchmark_history=benchmark_history,
        benchmark_metadata=benchmark_metadata,
        team_profiles=team_profiles,
    )
    release_by_contract: dict[tuple[str, str, str], str] = {}
    registered_release_ids: list[str] = []
    cache = getattr(evaluator, "_model_cache", {})
    cached_models = list(cache.values()) if isinstance(cache, Mapping) else []
    seen_models: set[int] = set()
    for model in cached_models:
        if id(model) in seen_models:
            continue
        seen_models.add(id(model))
        validation = getattr(model, "validation", None)
        contract = getattr(model, "contract", None)
        if validation is None or contract is None:
            continue
        eligible_for_forecast = (
            getattr(validation, "passes_quality_gate", None) is True
        )
        position = str(getattr(model, "position", "") or "")
        stage = str(getattr(model, "model_stage", "") or "")
        contract_id = str(getattr(contract, "contract_id", "") or "")
        source_key = "model" if stage == "college_precombine" else "benchmark"
        source_artifacts = artifacts[source_key]
        manifest = _model_release_manifest(
            model,
            season=season,
            week=week,
            training_artifact=source_artifacts["data"],
            metadata_artifact=source_artifacts["metadata"],
        )
        try:
            release = store.register_model_release(manifest)
        except (OSError, TrackingError, TypeError, ValueError) as exc:
            warnings.append(f"Could not register the {position} {stage} model release: {exc}")
            continue
        registered_release_ids.append(release.release_id)
        if eligible_for_forecast:
            release_by_contract[(position, stage, contract_id)] = release.release_id
        try:
            store.decide_promotion(
                release.release_id,
                policy=PromotionPolicy(
                    required_validation_scheme="expanding_window_past_only"
                ),
            )
            # Promotion decisions are immutable registry artifacts. They are
            # deliberately not embedded in forecast semantics, so an identical
            # rerun remains content-addressed to the same forecast run.
        except (OSError, TrackingError, TypeError, ValueError) as exc:
            warnings.append(
                f"Registered {release.release_id}, but its champion decision could not be recorded: {exc}"
            )

    forecasts: list[dict[str, Any]] = []
    missing_releases: list[str] = []
    for rank, evaluation in enumerate(evaluations, start=1):
        player = evaluation.player
        position = str(player.get("position") or "")
        for role, prediction in _available_prediction_variants(evaluation):
            key = (position, prediction.model_stage, prediction.contract_id)
            release_id = release_by_contract.get(key, "")
            if not release_id:
                missing_releases.append(
                    f"{player.get('name') or 'unknown player'} ({position}, {prediction.model_stage})"
                )
                continue
            prediction.model_release_id = release_id
            forecasts.append(
                _forecast_record(
                    player,
                    prediction,
                    release_id=release_id,
                    prediction_role=role,
                    rank=rank,
                    evidence_coverage=getattr(evaluation, "evidence_coverage", None),
                )
            )

    release_ids = tuple(sorted(set(registered_release_ids)))
    if missing_releases:
        examples = ", ".join(missing_releases[:3])
        warnings.append(
            f"Immutable forecast run withheld because {len(missing_releases)} available forecast(s) "
            f"had no validated model release: {examples}."
        )
        return _WeeklyTrackingResult(None, release_ids, warnings)
    if not forecasts:
        return _WeeklyTrackingResult(None, release_ids, warnings)

    run = store.record_forecast_run(
        season=season,
        week=week,
        forecasts=forecasts,
        input_artifacts={
            "players": artifacts["players"],
            "model_history": artifacts["model"],
            "benchmark_history": artifacts["benchmark"],
            "team_profiles": artifacts["team_profiles"],
        },
        metadata={
            "completed_games_only": True,
            "forecast_roles": sorted({str(row["prediction_role"]) for row in forecasts}),
        },
    )
    return _WeeklyTrackingResult(run.run_id, release_ids, warnings)


def _model_release_manifest(
    model: Any,
    *,
    season: int,
    week: int,
    training_artifact: Mapping[str, Any],
    metadata_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    validation = model.validation
    actual_scheme = str(getattr(validation, "validation_scheme", "") or "")
    if actual_scheme.startswith("expanding-window past-only"):
        normalized_scheme = "expanding_window_past_only"
    elif "random five-fold" in actual_scheme:
        normalized_scheme = "deterministic_random_five_fold"
    else:
        normalized_scheme = "unrecognized"
    evaluated_years = list(getattr(validation, "evaluated_years", ()) or ())
    evaluation_set_payload = {
        "position": model.position,
        "contract_id": model.contract.contract_id,
        "checkpoint": {"season": season, "week": week},
        "training_sha256": training_artifact.get("sha256"),
        "evaluated_years": evaluated_years,
    }
    evaluation_set_id = "evaluation_" + canonical_sha256(evaluation_set_payload)
    validation_record = {
        "validation_schema_version": 2,
        "scheme": normalized_scheme,
        "validation_scheme": actual_scheme,
        "passes_quality_gate": validation.passes_quality_gate is True,
        "evaluation_set_id": evaluation_set_id,
        "rows": validation.rows,
        "positives": validation.positives,
        "years": list(validation.years),
        "evaluated_years": evaluated_years,
        "per_year": list(getattr(validation, "per_year", ()) or ()),
        "brier": validation.brier,
        "baseline_brier": validation.baseline_brier,
        "log_loss": validation.log_loss,
        "average_precision": validation.average_precision,
        "roc_auc": validation.roc_auc,
        "expected_calibration_error": getattr(
            validation, "expected_calibration_error", None
        ),
        "board_scope": getattr(validation, "board_scope", None),
        "board_metrics": list(getattr(validation, "board_metrics", ()) or ()),
        "simple_baselines": list(
            getattr(validation, "simple_baselines", ()) or ()
        ),
        "quality_message": validation.quality_message,
    }
    registry_payload = {
        "position": model.position,
        "model_stage": model.model_stage,
        "contract_id": model.contract.contract_id,
    }
    registry_key = "registry_" + canonical_sha256(registry_payload)
    return {
        "registry_key": registry_key,
        "position": model.position,
        "contract_id": model.contract.contract_id,
        "model_stage": model.model_stage,
        "probability_kind": model.probability_kind,
        "population": model.population,
        "algorithm_id": f"draftscope_{type(model).__name__.lower()}_stdlib_logistic_v3",
        "checkpoint": {"target_season": season, "as_of_week": week},
        "training_artifact": dict(training_artifact),
        "training_metadata_artifact": dict(metadata_artifact),
        "training_rows": len(model.rows),
        "selected_features": list(model.features),
        "fitted_model_sha256": canonical_sha256(_fitted_model_state(model)),
        "validation": validation_record,
    }


def _fitted_model_state(model: Any) -> dict[str, Any]:
    def logistic_state(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        return {
            "feature_names": list(getattr(value, "feature_names", ()) or ()),
            "l2": getattr(value, "l2", None),
            "learning_rate": getattr(value, "learning_rate", None),
            "medians": dict(getattr(value, "medians", {}) or {}),
            "means": dict(getattr(value, "means", {}) or {}),
            "scales": dict(getattr(value, "scales", {}) or {}),
            "missing_features": list(getattr(value, "missing_features", ()) or ()),
            "weights": list(getattr(value, "weights", ()) or ()),
        }

    transformer = getattr(model, "_row_transformer", None)
    if is_dataclass(transformer) and not isinstance(transformer, type):
        transformer_state: Any = asdict(transformer)
    elif isinstance(transformer, Mapping):
        transformer_state = dict(transformer)
    elif transformer is None:
        transformer_state = None
    else:
        transformer_state = {"type": type(transformer).__name__}
    return {
        "raw_model": logistic_state(getattr(model, "raw_model", None)),
        "calibrator": logistic_state(getattr(model, "calibrator", None)),
        "row_transformer": transformer_state,
    }


def _available_prediction_variants(evaluation: Any) -> list[tuple[str, DraftPrediction]]:
    variants: list[tuple[str, DraftPrediction]] = []
    seen: set[int] = set()
    for role, attribute in (
        ("college_precombine", "college_prediction"),
        ("combine_crosscheck", "combine_prediction"),
        ("primary", "draft_prediction"),
    ):
        prediction = getattr(evaluation, attribute, None)
        if not isinstance(prediction, DraftPrediction) or id(prediction) in seen:
            continue
        seen.add(id(prediction))
        if prediction.available:
            variants.append((role, prediction))
    return variants


def _forecast_record(
    player: Mapping[str, Any],
    prediction: DraftPrediction,
    *,
    release_id: str,
    prediction_role: str,
    rank: int,
    evidence_coverage: Any,
) -> dict[str, Any]:
    return {
        "cfbd_player_id": player.get("cfbd_player_id"),
        "player_id": player.get("player_id"),
        "name": player.get("name"),
        "school": player.get("school"),
        "position": player.get("position"),
        "model_release_id": release_id,
        "contract_id": prediction.contract_id,
        "model_stage": prediction.model_stage,
        "probability_kind": prediction.probability_kind,
        "probability_scope": prediction.probability_scope,
        "probability": prediction.probability,
        "draft_probability": prediction.probability,
        "conditional_probability": prediction.conditional_probability,
        "entry_probability": prediction.entry_probability,
        "interval_80": prediction.interval_80,
        "feature_coverage": prediction.feature_coverage,
        "evidence_coverage": evidence_coverage,
        "out_of_distribution_score": prediction.out_of_distribution_score,
        "prediction_role": prediction_role,
        "rank": rank,
        "label": prediction.label,
    }


def _snapshot_tracking_inputs(
    *,
    output_dir: Path,
    season: int,
    week: int,
    players_path: Path,
    model_history: Sequence[Mapping[str, Any]],
    model_metadata: Mapping[str, Any],
    model_history_path: Path | None,
    benchmark_history: Sequence[Mapping[str, Any]],
    benchmark_metadata: Mapping[str, Any],
    team_profiles: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    history_dir = output_dir / "model_history" / "checkpoints"
    input_dir = output_dir / "forecast_inputs" / str(season) / f"week_{week:02d}"
    if model_history_path is not None and model_history_path.is_file():
        model_data = _snapshot_file(
            model_history_path,
            history_dir,
            prefix=f"season_{season}_week_{week:02d}",
            artifact_root=output_dir,
        )
    else:
        model_data = _snapshot_json(
            model_history,
            history_dir,
            prefix=f"season_{season}_week_{week:02d}_model_history",
            artifact_root=output_dir,
        )
    model_meta = _snapshot_json(
        model_metadata,
        history_dir,
        prefix=f"season_{season}_week_{week:02d}_{model_data['sha256']}_metadata",
        artifact_root=output_dir,
    )
    benchmark_data = _snapshot_json(
        benchmark_history,
        history_dir,
        prefix=f"season_{season}_week_{week:02d}_benchmark_history",
        artifact_root=output_dir,
    )
    benchmark_meta = _snapshot_json(
        benchmark_metadata,
        history_dir,
        prefix=f"season_{season}_week_{week:02d}_{benchmark_data['sha256']}_benchmark_metadata",
        artifact_root=output_dir,
    )
    players = _snapshot_file(
        players_path,
        input_dir,
        prefix="players",
        artifact_root=output_dir,
    )
    profiles = _snapshot_json(
        team_profiles,
        input_dir,
        prefix="team_profiles",
        artifact_root=output_dir,
    )
    return {
        "model": {"data": model_data, "metadata": model_meta},
        "benchmark": {"data": benchmark_data, "metadata": benchmark_meta},
        "players": players,
        "team_profiles": profiles,
    }


def _snapshot_file(
    source: Path,
    directory: Path,
    *,
    prefix: str,
    artifact_root: Path,
) -> dict[str, Any]:
    source_artifact = artifact_fingerprint(source)
    suffix = source.suffix.lower() or ".bin"
    destination = directory / f"{prefix}_{source_artifact['sha256']}{suffix}"
    if destination.exists():
        existing = artifact_fingerprint(destination)
        if (
            existing["sha256"] != source_artifact["sha256"]
            or existing["bytes"] != source_artifact["bytes"]
        ):
            raise TrackingError(f"Immutable input collision at {destination}")
        return _portable_artifact_fingerprint(destination, root=artifact_root)
    directory.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=directory
    )
    os.close(file_descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        copied = artifact_fingerprint(temporary)
        if (
            copied["sha256"] != source_artifact["sha256"]
            or copied["bytes"] != source_artifact["bytes"]
        ):
            raise TrackingError(f"Input changed while it was being snapshotted: {source}")
        try:
            os.link(temporary, destination)
        except FileExistsError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    result = _portable_artifact_fingerprint(destination, root=artifact_root)
    if result["sha256"] != source_artifact["sha256"]:
        raise TrackingError(f"Immutable input snapshot failed verification: {destination}")
    return result


def _snapshot_json(
    value: Any,
    directory: Path,
    *,
    prefix: str,
    artifact_root: Path,
) -> dict[str, Any]:
    portable_value = _portable_snapshot_value(value)
    payload = (
        json.dumps(
            portable_value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    destination = directory / f"{prefix}_{digest}.json"
    _write_immutable_bytes(destination, payload)
    return _portable_artifact_fingerprint(destination, root=artifact_root)


def _portable_snapshot_value(value: Any) -> Any:
    """Remove machine-local absolute paths from shareable tracking snapshots."""

    if is_dataclass(value) and not isinstance(value, type):
        return _portable_snapshot_value(asdict(value))
    if isinstance(value, Path):
        return value.name if value.is_absolute() else value.as_posix()
    if isinstance(value, Mapping):
        return {str(key): _portable_snapshot_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_portable_snapshot_value(item) for item in value]
    if isinstance(value, str):
        if value.strip().startswith(("http://", "https://")):
            return value
        posix = PurePosixPath(value)
        windows = PureWindowsPath(value)
        if posix.is_absolute():
            return posix.name
        if windows.is_absolute():
            return windows.name
    return value


def _write_immutable_bytes(destination: Path, payload: bytes) -> None:
    expected = hashlib.sha256(payload).hexdigest()
    if destination.exists():
        if file_sha256(destination) != expected or destination.stat().st_size != len(payload):
            raise TrackingError(f"Refusing to overwrite immutable input: {destination}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    if file_sha256(destination) != expected or destination.stat().st_size != len(payload):
        raise TrackingError(f"Immutable input snapshot failed verification: {destination}")


def _top_discovered_per_position(
    candidates: list[dict[str, Any]], per_position: int
) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    selected: list[dict[str, Any]] = []
    for candidate in candidates:
        position = str(candidate.get("position") or "")
        if counts.get(position, 0) >= per_position:
            continue
        counts[position] = counts.get(position, 0) + 1
        selected.append(candidate)
    return selected


def _refresh_candidate_pool(
    client: CFBDClient,
    tracked: list[dict[str, Any]],
    *,
    discovered: list[dict[str, Any]] | None,
    season: int,
    week: int,
    raw_dir: Path,
) -> tuple[list[dict[str, Any]], int, list[str]]:
    """Merge the national pool with tracked scouting inputs and refresh extras."""
    refreshed: list[dict[str, Any]] = []
    warnings: list[str] = []
    failed = 0
    matched_tracked: set[int] = set()
    by_id: dict[str, int] = {}
    by_name_school: dict[tuple[str, str], int] = {}
    for index, candidate in enumerate(tracked):
        player_id = str(candidate.get("cfbd_player_id") or "").strip()
        if player_id:
            by_id[player_id] = index
        by_name_school[
            (normalize_name(candidate.get("name")), normalize_name(candidate.get("school")))
        ] = index

    for discovered_player in discovered or []:
        player_id = str(discovered_player.get("cfbd_player_id") or "").strip()
        index = by_id.get(player_id) if player_id else None
        if index is None:
            index = by_name_school.get(
                (
                    normalize_name(discovered_player.get("name")),
                    normalize_name(discovered_player.get("school")),
                )
            )
        existing = tracked[index] if index is not None else None
        if index is not None:
            matched_tracked.add(index)
        merged = _merge_discovered_player(discovered_player, existing, week=week)
        merged["data_stale"] = False
        merged["refresh_error"] = None
        refreshed.append(merged)

    # Explicitly tracked players remain in scope even if the national production
    # triage limit omits them or CFBD has no box-score row for their position.
    for index, candidate in enumerate(tracked):
        if index in matched_tracked:
            continue
        try:
            updated, raw = client.refresh_candidate(candidate, season=season, week=week)
            updated["data_stale"] = False
            updated["refresh_error"] = None
            refreshed.append(updated)
            identifier = slug(updated.get("cfbd_player_id") or updated.get("name") or "player")
            (raw_dir / f"tracked_{identifier}.json").write_text(
                json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        except DataError as exc:
            failed += 1
            retained = dict(candidate)
            # Preserve the prior checkpoint. It stays recoverable in the player
            # file but ProspectEvaluator.rank() omits stale rows from this board.
            retained["data_stale"] = True
            retained["refresh_error"] = str(exc)
            refreshed.append(retained)
            warnings.append(f"{candidate.get('name')}: {exc}")
    return refreshed, failed, warnings


def _merge_discovered_player(
    discovered: Mapping[str, Any], existing: Mapping[str, Any] | None, *, week: int
) -> dict[str, Any]:
    merged = dict(discovered)
    if not existing:
        return normalize_record(merged)

    always_preserve = {
        "player_id",
        "projected_draft_year",
        "draft_entry_probability",
        "draft_declared",
        "draft_eligible",
        "date_of_birth",
        "age_at_draft",
        "measurement_date",
        "notes",
        *FILM_PROVENANCE_FIELDS,
    }
    for key, value in existing.items():
        if value in (None, ""):
            continue
        if key in always_preserve or key.startswith("trait_"):
            merged[key] = value

    physical_keys = {spec.key for spec in COMMON_PHYSICAL}
    try:
        verified = parse_bool(existing.get("measurements_verified")) is True
    except DataError:
        verified = False
    source = str(existing.get("measurement_source") or "").lower()
    preserve_workout = verified or source not in {"", "school_roster"}
    for key in physical_keys:
        value = existing.get(key)
        if value in (None, ""):
            continue
        if preserve_workout or merged.get(key) in (None, ""):
            merged[key] = value
    if preserve_workout:
        for key in ("measurement_source", "measurement_date", "measurements_verified"):
            if existing.get(key) not in (None, ""):
                merged[key] = existing.get(key)

    # Preserve a same-checkpoint manually supplied advanced stat when CFBD does
    # not expose an equivalent. Never carry an older weekly total forward.
    existing_week = parse_number(existing.get("as_of_week"))
    if existing_week is not None and int(existing_week) == week:
        for key, value in existing.items():
            if key.startswith("prod_") and value not in (None, "") and merged.get(key) in (None, ""):
                merged[key] = value
    return normalize_record(merged)


def _first_float(row: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float(row.get(key))
        if value is not None:
            return value
    return None


def _float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None

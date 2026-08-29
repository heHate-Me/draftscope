from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import os
from pathlib import Path
import shlex
import shutil
import sys
import tomllib
from typing import Any

from .data_sources import cfbd_api_key_status
from .records import DataError, load_records


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    status: str
    name: str
    message: str
    action: str = ""


def diagnose(config_path: str | os.PathLike[str] = "draftscope.toml") -> dict[str, Any]:
    """Inspect local readiness without network calls or external credentials."""

    checks: list[DoctorCheck] = []
    executable = Path(sys.executable).resolve()
    module_command = f"{shlex.quote(str(executable))} -m draftscope"
    project_root = Path(__file__).resolve().parents[2]
    project_launcher = project_root / "run_draftscope.py"
    command = shlex.quote(str(project_launcher)) if project_launcher.is_file() else module_command
    config_probe = Path(config_path).resolve()
    college_provider = "sportsdataverse"
    cfbd_fallback = False
    if config_probe.is_file():
        try:
            with config_probe.open("rb") as handle:
                probe = tomllib.load(handle)
            probe_settings = dict(probe.get("draftscope") or probe)
            college_provider = str(
                probe_settings.get("college_data_provider") or "sportsdataverse"
            ).strip().lower()
            cfbd_fallback = bool(probe_settings.get("cfbd_fallback", False))
        except (OSError, TypeError, ValueError, tomllib.TOMLDecodeError):
            pass
    version = sys.version_info
    if version >= (3, 11):
        checks.append(
            DoctorCheck("PASS", "Python", f"Python {version.major}.{version.minor}.{version.micro} at {executable}")
        )
    else:
        checks.append(
            DoctorCheck(
                "FAIL",
                "Python",
                f"Python {version.major}.{version.minor}.{version.micro} is too old; DraftScope requires 3.11+.",
                "Install Python 3.11 or newer, recreate .venv, and reinstall with `python -m pip install -e .`.",
            )
        )

    if sys.prefix != sys.base_prefix:
        checks.append(DoctorCheck("PASS", "Environment", f"Virtual environment active: {sys.prefix}"))
    else:
        checks.append(
            DoctorCheck(
                "WARN",
                "Environment",
                "No virtual environment is active. The current interpreter can import DraftScope, but shell launchers may differ.",
                "From the repository: `python3 -m venv .venv`, then `source .venv/bin/activate` and `python -m pip install -e .`.",
            )
        )

    launcher = shutil.which("draftscope")
    if project_launcher.is_file():
        checks.append(
            DoctorCheck(
                "PASS",
                "Launcher",
                f"Project launcher is available at {project_launcher}; it selects this checkout's .venv and works from any directory.",
            )
        )
    elif launcher:
        checks.append(DoctorCheck("PASS", "Launcher", f"`draftscope` resolves to {launcher}"))
    else:
        checks.append(
            DoctorCheck(
                "WARN",
                "Launcher",
                "The `draftscope` console command is not on PATH for this shell.",
                f"Use `{module_command}` now, or activate the environment that contains DraftScope.",
            )
        )

    cfbd_is_required = college_provider == "cfbd" or cfbd_fallback
    # The default providers need no credential. Avoid touching macOS Keychain in
    # that path: a network-free diagnostic must also remain prompt-free.
    key_status = cfbd_api_key_status() if cfbd_is_required else "not_required"
    if key_status == "configured":
        source = "CFBD_API_KEY" if str(os.environ.get("CFBD_API_KEY") or "").strip() else "macOS Keychain"
        checks.append(
            DoctorCheck(
                "PASS",
                "Optional CFBD fallback" if not cfbd_is_required else "CFBD credential",
                f"A credential resolved from {source} and is not an obvious placeholder (network access was not tested).",
            )
        )
    elif key_status == "not_required":
        checks.append(
            DoctorCheck(
                "PASS",
                "College data",
                "SportsDataverse bulk files are selected; normal discovery and weekly updates require no API credential.",
            )
        )
    elif key_status == "placeholder":
        checks.append(
            DoctorCheck(
                "WARN",
                "CFBD credential",
                "CFBD_API_KEY contains an obvious placeholder. Live lookup, discovery, and update commands will reject it.",
                "Replace it with a real CollegeFootballData key. Credential-free `add`, `audit`, and `doctor` commands remain available.",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                "WARN",
                "CFBD credential",
                "CFBD_API_KEY is not set. Live lookup, discovery, and update commands are unavailable.",
                f"On macOS run `{command} configure-key`; otherwise set CFBD_API_KEY. The `add` command needs no credential.",
            )
        )

    config = Path(config_path).resolve()
    settings: dict[str, Any] = {}
    if config.exists():
        try:
            with config.open("rb") as handle:
                parsed = tomllib.load(handle)
            settings = dict(parsed.get("draftscope") or parsed)
            checks.append(DoctorCheck("PASS", "Config", f"Loaded {config}"))
            configured_provider = str(
                settings.get("college_data_provider") or "sportsdataverse"
            ).strip().lower()
            if configured_provider not in {"sportsdataverse", "cfbd"}:
                checks.append(
                    DoctorCheck(
                        "FAIL",
                        "College provider",
                        f"Unsupported college_data_provider: {configured_provider!r}.",
                        'Use `college_data_provider = "sportsdataverse"` or `"cfbd"`.',
                    )
                )
        except (OSError, TypeError, ValueError, tomllib.TOMLDecodeError) as exc:
            checks.append(
                DoctorCheck(
                    "FAIL",
                    "Config",
                    f"Could not read {config}: {exc}",
                    "Fix the TOML syntax, or replace it from draftscope.toml.example.",
                )
            )
    else:
        example = config.parent / "draftscope.toml.example"
        if not example.exists():
            candidate = Path.cwd() / "draftscope.toml.example"
            if candidate.exists():
                example = candidate.resolve()
        action = (
            f"Create it with `cp {shlex.quote(str(example))} {shlex.quote(str(config))}`."
            if example.exists()
            else f"Create it with `{command} template config --out {shlex.quote(str(config))}`, then edit it."
        )
        checks.append(DoctorCheck("WARN", "Config", f"Config file is missing: {config}", action))

    try:
        season = int(settings.get("season") or date.today().year)
    except (TypeError, ValueError):
        season = date.today().year
        checks.append(
            DoctorCheck(
                "FAIL",
                "Season",
                f"Config season must be an integer; got {settings.get('season')!r}.",
                "Set `season` to the active NCAA season year.",
            )
        )
    configured_players = settings.get("players_file")
    players_path = Path(str(configured_players or f"data/players_{season}.csv"))
    if not players_path.is_absolute():
        players_path = (config.parent / players_path).resolve()
    if players_path.exists():
        try:
            players = load_records(players_path)
            if players:
                checks.append(
                    DoctorCheck("PASS", "Players", f"Loaded {len(players)} player row(s) from {players_path}")
                )
            else:
                checks.append(
                    DoctorCheck(
                        "WARN",
                        "Players",
                        f"{players_path} contains headers but no player rows.",
                        f"Create a row without credentials using `{command} add \"Example Prospect\" --position WR "
                        f"--school \"Example University\" --season {season} --out {shlex.quote(str(players_path))}`.",
                    )
                )
        except (DataError, OSError) as exc:
            checks.append(
                DoctorCheck(
                    "FAIL",
                    "Players",
                    f"Could not read {players_path}: {exc}",
                    "Repair the CSV/JSON file or create a new one with the `add` command.",
                )
            )
    else:
        checks.append(
            DoctorCheck(
                "WARN",
                "Players",
                f"Player file does not exist: {players_path}",
                f"Create it without credentials using `{command} add \"Example Prospect\" --position WR "
                f"--school \"Example University\" --season {season} --out {shlex.quote(str(players_path))}`.",
            )
        )

    commands = [
        f"{command} --help",
        f"{command} doctor --config {shlex.quote(str(config))}",
        (
            f"{command} add \"Example Prospect\" --position WR --school \"Example University\" "
            f"--season {season} --height 6-2 --weight 205 --out {shlex.quote(str(players_path))}"
        ),
        f"{command} audit --players {shlex.quote(str(players_path))}",
    ]
    failures = sum(check.status == "FAIL" for check in checks)
    warnings = sum(check.status == "WARN" for check in checks)
    return {
        "ok": failures == 0,
        "failures": failures,
        "warnings": warnings,
        "config": str(config),
        "players_file": str(players_path),
        "checks": [asdict(check) for check in checks],
        "credential_free_commands": commands,
    }


def render_diagnosis(result: dict[str, Any]) -> str:
    lines = ["DRAFTSCOPE DOCTOR"]
    for check in result["checks"]:
        lines.append(f"[{check['status']}] {check['name']}: {check['message']}")
        if check.get("action"):
            lines.append(f"       Action: {check['action']}")
    lines.extend(["", "Credential-free commands that work in this environment:"])
    lines.extend(f"  {command}" for command in result["credential_free_commands"])
    summary = "READY FOR LOCAL USE" if result["ok"] else "NOT READY"
    lines.extend(
        [
            "",
            f"Result: {summary} ({result['failures']} failure(s), {result['warnings']} warning(s)).",
            "CFBD credentials are required only for the explicit CFBD provider, fallback, or CFBD lookup command.",
        ]
    )
    return "\n".join(lines) + "\n"

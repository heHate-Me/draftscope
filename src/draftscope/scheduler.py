from __future__ import annotations

import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
from typing import Any, Mapping

from .records import DataError


LAUNCH_AGENT_LABEL = "com.draftscope.weekly"


def build_launch_agent_payload(
    *,
    launcher: str | os.PathLike[str],
    config: str | os.PathLike[str],
    working_directory: str | os.PathLike[str],
    log_directory: str | os.PathLike[str],
    weekday: int = 2,
    hour: int = 6,
    minute: int = 0,
) -> dict[str, Any]:
    """Build a secret-free launchd job for one bounded weekly update."""

    if not 0 <= weekday <= 7:
        raise DataError("weekday must be 0-7 (1=Monday, 2=Tuesday, 0/7=Sunday)")
    if not 0 <= hour <= 23:
        raise DataError("hour must be 0-23")
    if not 0 <= minute <= 59:
        raise DataError("minute must be 0-59")
    launcher_path = Path(launcher).resolve()
    config_path = Path(config).resolve()
    work_path = Path(working_directory).resolve()
    log_path = Path(log_directory).resolve()
    if not launcher_path.is_file():
        raise DataError(f"DraftScope launcher does not exist: {launcher_path}")
    if not config_path.is_file():
        raise DataError(f"DraftScope config does not exist: {config_path}")
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            str(launcher_path),
            "update",
            "--config",
            str(config_path),
        ],
        "WorkingDirectory": str(work_path),
        "StartCalendarInterval": {
            "Weekday": weekday,
            "Hour": hour,
            "Minute": minute,
        },
        "RunAtLoad": False,
        "ProcessType": "Background",
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(log_path / "weekly-update.log"),
        "StandardErrorPath": str(log_path / "weekly-update.error.log"),
    }


def install_weekly_schedule(
    config: str | os.PathLike[str],
    settings: Mapping[str, Any],
    *,
    weekday: int = 2,
    hour: int = 6,
    minute: int = 0,
) -> Path:
    _require_macos()
    config_path = Path(config).resolve()
    project_dir = config_path.parent
    launcher = project_dir / "run_draftscope.py"
    output_dir = Path(str(settings.get("output_dir") or project_dir / "data")).resolve()
    log_dir = output_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    payload = build_launch_agent_payload(
        launcher=launcher,
        config=config_path,
        working_directory=project_dir,
        log_directory=log_dir,
        weekday=weekday,
        hour=hour,
        minute=minute,
    )
    destination = weekly_schedule_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    serialized = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(serialized)
        os.replace(temporary_name, destination)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise

    domain = f"gui/{os.getuid()}"
    # An already-loaded prior definition is safely replaced by the exact plist
    # the user just requested. bootout returns nonzero when nothing was loaded.
    subprocess.run(
        ["launchctl", "bootout", domain, str(destination)],
        check=False,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(destination)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise DataError(
            "launchctl could not load the weekly schedule: "
            + (result.stderr.strip() or result.stdout.strip() or "unknown error")
        )
    return destination


def weekly_schedule_status() -> dict[str, Any]:
    _require_macos()
    path = weekly_schedule_path()
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"],
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        "installed": path.is_file(),
        "loaded": result.returncode == 0,
        "path": str(path),
        "detail": (result.stdout if result.returncode == 0 else result.stderr).strip(),
    }


def remove_weekly_schedule() -> Path:
    _require_macos()
    path = weekly_schedule_path()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if path.exists():
        path.unlink()
    return path


def weekly_schedule_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise DataError("The built-in weekly scheduler currently supports macOS launchd only")


__all__ = [
    "LAUNCH_AGENT_LABEL",
    "build_launch_agent_payload",
    "install_weekly_schedule",
    "remove_weekly_schedule",
    "weekly_schedule_path",
    "weekly_schedule_status",
]

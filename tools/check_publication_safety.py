#!/usr/bin/env python3
"""Fail when Git-tracked content contains unsafe publication artifacts."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Iterable


MAX_TRACKED_FILE_BYTES = 10 * 1024 * 1024

_BLOCKED_COMPONENTS = {
    ".draftscope-cache",
    ".idea",
    ".ipynb_checkpoints",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".vscode",
    "__pycache__",
}
_BLOCKED_ROOT_DIRECTORIES = {"build", "data", "dist", "htmlcov"}
_BLOCKED_FILENAMES = {".coverage", ".ds_store", "draftscope.toml"}
_BLOCKED_SUFFIXES = {".jks", ".key", ".keystore", ".log", ".p12", ".pem", ".pfx", ".pyc", ".pyo"}
_PRIVATE_KEY_FILENAMES = {
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
}
_SYNTHETIC_USERNAMES = {
    "example",
    "runner",
    "sample",
    "test",
    "user",
    "username",
}

_PRIVATE_KEY_BLOCK = re.compile(
    rb"-----BEGIN (?:DSA |EC |ENCRYPTED |OPENSSH |PGP |RSA )?PRIVATE KEY(?: BLOCK)?-----"
)
_CREDENTIAL_PATTERNS = (
    (
        "GitHub credential-shaped token",
        re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})\b"),
    ),
    ("AWS access-key-shaped token", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    (
        "Google API-key-shaped token",
        re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b"),
    ),
    (
        "Slack credential-shaped token",
        re.compile(rb"\bxox[baprs]-[0-9A-Za-z-]{20,}\b"),
    ),
)
_POSIX_HOME = re.compile(
    rb"(?<![A-Za-z0-9:/])/(?:Users|home)/([^/\s\"'<>]+)(?:/[^\s\"'<>]*)?"
)
_ROOT_HOME = re.compile(
    rb"(?<![A-Za-z0-9:/])/" + rb"root(?:/[^\s\"'<>]*)?"
)
_WINDOWS_HOME = re.compile(
    rb"(?<![A-Za-z0-9])(?:[A-Za-z]:)?\\+Users\\+([^\\\s\"'<>]+)(?:\\+[^\s\"'<>]*)?",
    re.IGNORECASE,
)
_MACOS_TEMP = re.compile(
    rb"(?<![A-Za-z0-9:/])/(?:private/)?var/folders/[^\s\"'<>]+"
)


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    reason: str
    line: int | None = None

    def render(self) -> str:
        location = f"{self.path}:{self.line}" if self.line is not None else self.path
        return f"{location}: {self.reason}"


def check_path_name(relative_path: str) -> list[Finding]:
    """Return publication findings based only on a repository-relative path."""

    normalized = relative_path.replace("\\", "/")
    path = PurePosixPath(normalized)
    parts = tuple(part.casefold() for part in path.parts)
    basename = parts[-1] if parts else ""
    findings: list[Finding] = []

    if path.is_absolute() or ".." in path.parts:
        findings.append(Finding(relative_path, "tracked path is not safely repository-relative"))
        return findings
    if parts and parts[0] in _BLOCKED_ROOT_DIRECTORIES:
        findings.append(Finding(relative_path, "tracked runtime or build-output directory"))
    if any(part in _BLOCKED_COMPONENTS for part in parts):
        findings.append(Finding(relative_path, "tracked cache, environment, or editor directory"))
    if basename in _BLOCKED_FILENAMES:
        findings.append(Finding(relative_path, "tracked local runtime configuration or output"))
    if basename.startswith(".env") and basename != ".env.example":
        findings.append(Finding(relative_path, "tracked local environment configuration"))
    if any(part.startswith(".data.draftscope-update-") for part in parts):
        findings.append(Finding(relative_path, "tracked interrupted-update staging artifact"))
    if any(part.startswith(".data.draftscope-transaction-") for part in parts):
        findings.append(Finding(relative_path, "tracked update transaction journal"))
    if basename in _PRIVATE_KEY_FILENAMES or any(
        basename.endswith(suffix) for suffix in _BLOCKED_SUFFIXES
    ):
        findings.append(Finding(relative_path, "tracked credential, log, cache, or compiled artifact"))
    if basename == "core" or re.fullmatch(r"core\.\d+", basename) or basename.endswith(".stackdump"):
        findings.append(Finding(relative_path, "tracked crash artifact"))
    if basename.startswith("hs_err_pid") and basename.endswith(".log"):
        findings.append(Finding(relative_path, "tracked crash artifact"))
    return findings


def _line_number(data: bytes, offset: int) -> int:
    return data.count(b"\n", 0, offset) + 1


def _is_synthetic_username(raw: bytes) -> bool:
    try:
        username = raw.decode("utf-8").casefold()
    except UnicodeDecodeError:
        return False
    return username in _SYNTHETIC_USERNAMES or (
        username.startswith("<") and username.endswith(">")
    )


def check_file_content(relative_path: str, data: bytes) -> list[Finding]:
    """Return findings for secrets and machine-local paths in file bytes."""

    findings: list[Finding] = []
    private_key = _PRIVATE_KEY_BLOCK.search(data)
    if private_key:
        findings.append(
            Finding(
                relative_path,
                "private-key material",
                _line_number(data, private_key.start()),
            )
        )

    for description, pattern in _CREDENTIAL_PATTERNS:
        match = pattern.search(data)
        if match:
            findings.append(
                Finding(relative_path, description, _line_number(data, match.start()))
            )

    for pattern in (_POSIX_HOME, _WINDOWS_HOME):
        for match in pattern.finditer(data):
            if _is_synthetic_username(match.group(1)):
                continue
            findings.append(
                Finding(
                    relative_path,
                    "machine-local home-directory path",
                    _line_number(data, match.start()),
                )
            )
            break

    for pattern, description in (
        (_ROOT_HOME, "machine-local root home-directory path"),
        (_MACOS_TEMP, "machine-local macOS temporary path"),
    ):
        match = pattern.search(data)
        if match:
            findings.append(
                Finding(relative_path, description, _line_number(data, match.start()))
            )
    return findings


def _tracked_paths(repository: Path) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return tuple(
        raw.decode("utf-8", errors="surrogateescape")
        for raw in result.stdout.split(b"\0")
        if raw
    )


def _symlink_finding(repository: Path, relative_path: str, path: Path) -> Finding | None:
    if not path.is_symlink():
        return None
    target = Path(os.readlink(path))
    if target.is_absolute():
        return Finding(relative_path, "tracked symlink has an absolute target")
    try:
        resolved = path.resolve(strict=False)
        resolved.relative_to(repository)
    except ValueError:
        return Finding(relative_path, "tracked symlink escapes the repository")
    return None


def scan_repository(repository: Path) -> tuple[tuple[str, ...], list[Finding]]:
    """Scan the current worktree contents of all paths tracked by Git."""

    repository = repository.resolve()
    tracked = _tracked_paths(repository)
    findings: list[Finding] = []
    for relative_path in tracked:
        findings.extend(check_path_name(relative_path))
        path = repository / relative_path
        symlink_finding = _symlink_finding(repository, relative_path, path)
        if symlink_finding:
            findings.append(symlink_finding)
            continue
        if not path.is_file():
            findings.append(Finding(relative_path, "tracked path is not a readable regular file"))
            continue
        size = path.stat().st_size
        if size > MAX_TRACKED_FILE_BYTES:
            findings.append(
                Finding(
                    relative_path,
                    f"tracked file exceeds {MAX_TRACKED_FILE_BYTES // (1024 * 1024)} MiB safety limit",
                )
            )
            continue
        findings.extend(check_file_content(relative_path, path.read_bytes()))
    return tracked, sorted(set(findings))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check Git-tracked files for unsafe publication artifacts."
    )
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path.cwd(),
        help="repository worktree to scan (default: current directory)",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        tracked, findings = scan_repository(args.repository)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Publication safety check could not run: {exc}", file=sys.stderr)
        return 2
    if findings:
        print("Publication safety check failed:", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding.render()}", file=sys.stderr)
        return 1
    print(f"Publication safety check passed for {len(tracked)} tracked files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

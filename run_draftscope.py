#!/usr/bin/env python3
"""Run DraftScope directly from a source checkout without installing it."""

from __future__ import annotations

from pathlib import Path
import os
import sys


PROJECT_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = PROJECT_ROOT / "src"
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"

if VENV_PYTHON.is_file() and Path(sys.prefix).resolve() != (PROJECT_ROOT / ".venv").resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])
if sys.version_info < (3, 11):
    raise SystemExit(
        "DraftScope requires Python 3.11 or newer. Create .venv with a current Python, "
        "or run this checkout's existing .venv/bin/python."
    )

sys.path.insert(0, str(SOURCE_ROOT))
os.chdir(PROJECT_ROOT)

from draftscope.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())

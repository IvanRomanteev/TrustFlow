"""Canonical one-command M5.2 demo. Prerequisites: pip install -e '.[dev,demo]'."""

import sys
from pathlib import Path

# Invocation by path works from any directory after installation of the project.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from m52.runner import main  # noqa: E402

if __name__ == "__main__":
    # Windows terminals may use cp1251. Diagnostics must never turn a recorded
    # scenario FAIL into an encoding traceback (Playwright includes UI arrows).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    raise SystemExit(main())

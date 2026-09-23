"""Environment configuration loader for VentureBot (Step 30B).

Loads simple KEY=VALUE pairs from a local .env file into os.environ
at the application boundary using only the Python standard library.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env_file(env_path: Path | str | None = None) -> bool:
    """Load simple KEY=VALUE pairs from a .env file into os.environ.

    - Uses os.environ.setdefault() so existing environment variables take precedence.
    - Strips matching surrounding single/double quotes.
    - Ignores comments (lines starting with #) and blank lines.
    - Does not support interpolation or complex shell syntax.
    - Never prints, logs, or exposes parsed values.

    Args:
        env_path: Optional explicit Path or string path to .env file.
                  If omitted, resolves .env relative to the repository root.

    Returns:
        bool: True if the file exists and was parsed, False if not found or unreadable.
    """
    if env_path is None:
        # Standard repository root relative to backend/venturebot/env.py:
        # file is at backend/venturebot/env.py -> parents[2] is the repo root.
        repo_root = Path(__file__).resolve().parents[2]
        target_path = repo_root / ".env"
    else:
        target_path = Path(env_path)

    if not target_path.is_file():
        return False

    try:
        content = target_path.read_text(encoding="utf-8")
    except OSError:
        return False

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue

        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()

        if not key:
            continue

        # Strip matching surrounding single or double quotes
        if len(val) >= 2 and (
            (val.startswith('"') and val.endswith('"'))
            or (val.startswith("'") and val.endswith("'"))
        ):
            val = val[1:-1]

        os.environ.setdefault(key, val)

    return True


def is_safe_mode() -> bool:
    """Return whether VentureBot safe mode is active.

    Checked via VENTUREBOT_SAFE_MODE environment variable.
    Defaults to True (blocking all real write actions) unless explicitly set to
    'false', '0', or 'no'.
    """
    raw = os.environ.get("VENTUREBOT_SAFE_MODE", "true").strip().lower()
    return raw not in ("false", "0", "no")


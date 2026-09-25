"""VentureBot local entry point root runner.

Delegates directly to venturebot.__main__.main().
"""

import sys
from pathlib import Path

# Ensure backend/ directory is on sys.path for local discovery
_repo_root = Path(__file__).resolve().parent
_backend_dir = _repo_root / "backend"
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))

from venturebot.env import load_env_file  # noqa: E402

# Load local gitignored .env into os.environ before application execution
load_env_file(_repo_root / ".env")

from venturebot.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()


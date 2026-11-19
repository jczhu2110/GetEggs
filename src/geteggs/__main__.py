"""Allow `python -m geteggs` execution."""

from __future__ import annotations

import os
import sys
from datetime import datetime


def _ensure_hidden_stdio() -> None:
    """Redirect stdio to a log file when launched by pythonw (no console)."""
    if os.name != "nt" or (sys.stdout is not None and sys.stderr is not None):
        return
    try:
        from geteggs.core.config import get_data_dir

        log_path = get_data_dir() / "geteggs-ui.log"
        stream = log_path.open("a", encoding="utf-8", buffering=1)
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    os.environ["GETEGGS_HIDDEN"] = "1"
    print(
        f"\n===== {datetime.now().isoformat(timespec='seconds')} "
        f"pid={os.getpid()} argv={sys.argv[1:]} ====="
    )


if __name__ == "__main__":
    _ensure_hidden_stdio()
    if len(sys.argv) == 2 and sys.argv[1] == "open":
        from geteggs.launcher import main as launcher_main

        launcher_main()
    else:
        from geteggs.cli import main

        main()

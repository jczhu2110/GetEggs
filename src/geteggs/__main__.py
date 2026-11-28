"""Allow `python -m geteggs` execution."""

from __future__ import annotations

import atexit
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, TextIO


class _LazyLogStream:
    """Defer opening the hidden log file until non-empty output is written."""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, path: Path, argv: list[str]) -> None:
        self._path = path
        self._argv = list(argv)
        self._stream: Optional[TextIO] = None
        self._lock = threading.Lock()

    def _open_if_needed(self) -> Optional[TextIO]:
        if self._stream is not None:
            return self._stream
        from geteggs.core.config import trim_log_file

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            trim_log_file(self._path)
            stream = self._path.open("a", encoding="utf-8", buffering=1)
        except OSError:
            return None
        header = (
            f"\n===== {datetime.now().isoformat(timespec='seconds')} "
            f"pid={os.getpid()} argv={self._argv} =====\n"
        )
        stream.write(header)
        self._stream = stream
        return stream

    def write(self, text: str) -> int:
        if not text:
            return 0
        with self._lock:
            if self._stream is None and not text.strip():
                return len(text)
            stream = self._open_if_needed()
            if stream is None:
                return len(text)
            return stream.write(text)

    def flush(self) -> None:
        with self._lock:
            if self._stream is not None:
                self._stream.flush()

    def close(self) -> None:
        with self._lock:
            if self._stream is not None:
                self._stream.close()
                self._stream = None

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True


def _ensure_hidden_stdio() -> None:
    """Redirect stdio to a lazy bounded log file when launched by pythonw (no console)."""
    if os.name != "nt" or (sys.stdout is not None and sys.stderr is not None):
        return
    try:
        from geteggs.core.config import get_ui_log_path

        log_path = get_ui_log_path()
    except OSError:
        return
    stream = _LazyLogStream(log_path, sys.argv[1:])
    atexit.register(stream.close)
    if sys.stdout is None:
        sys.stdout = stream  # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = stream  # type: ignore[assignment]
    os.environ["GETEGGS_HIDDEN"] = "1"


if __name__ == "__main__":
    _ensure_hidden_stdio()
    if len(sys.argv) == 2 and sys.argv[1] == "open":
        from geteggs.launcher import main as launcher_main

        launcher_main()
    else:
        from geteggs.cli import main

        main()

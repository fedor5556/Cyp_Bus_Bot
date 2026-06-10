"""log_tee.py - tee this process's stdout/stderr to a rotating UTF-8 log file.

THE RULE (full write-up: TELEGRAM_BOT_NOTE.md in the GEMINI_PROJECTS root):
a process is the ONLY writer of its own log file, and it writes it from inside
Python. Never wrap a launcher in `... | Tee-Object -FilePath <log>` or
`>> <log>` - on Windows the outer shell holds the file with write-sharing
denied, which locks the log for as long as the process lives, makes the file
UTF-16, lets it grow forever, and (if the program also opens the same file)
kills one of the two writers with PermissionError at startup.

Fail-safe by design: any OSError on open/write/rotate degrades to console-only
output. Logging must never crash or block the host program.
"""
import os
import sys


class _RotatingSink:
    """Single shared file writer with size-based rotation (UTF-8)."""

    def __init__(self, path, max_bytes=2_000_000, backups=3):
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self._file = None

    def _open(self):
        if self._file is None:
            try:
                self._file = open(self.path, "a", encoding="utf-8",
                                  errors="replace", buffering=1)
            except OSError:
                self._file = None
        return self._file

    def write(self, text):
        f = self._open()
        if f is None:
            return
        try:
            f.write(text)
        except OSError:
            self._file = None
            return
        if f.tell() >= self.max_bytes:
            try:
                self._rotate()
            except OSError:
                # Rotation blocked (e.g. a backup file is held open elsewhere).
                # Keep appending to the current file rather than losing output.
                pass

    def _rotate(self):
        # monitor.log -> monitor.log.1 -> .2 -> ... (oldest dropped)
        try:
            if self._file is not None:
                self._file.close()
        finally:
            self._file = None
        for i in range(self.backups - 1, 0, -1):
            src = "{}.{}".format(self.path, i)
            dst = "{}.{}".format(self.path, i + 1)
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(self.path, self.path + ".1")

    def flush(self):
        if self._file is not None:
            try:
                self._file.flush()
            except OSError:
                self._file = None


class _TeeStream:
    """Wraps a console stream; mirrors every write into the shared sink."""

    def __init__(self, console, sink):
        self._console = console
        self._sink = sink

    def write(self, text):
        try:
            self._console.write(text)
        except Exception:
            pass  # a dead or non-UTF console must not stop file logging
        self._sink.write(text)
        return len(text)

    def flush(self):
        try:
            self._console.flush()
        except Exception:
            pass
        self._sink.flush()

    def __getattr__(self, name):
        return getattr(self._console, name)


def setup(log_name, base_dir=None):
    """Tee stdout+stderr into <base_dir>/logs/<log_name>.log.

    Call this FIRST in __main__, before any output and before
    logging.basicConfig (so logging's StreamHandler binds to the tee).
    base_dir defaults to the project root (parent of this file's directory).
    """
    if base_dir is None:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(base_dir, "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        return
    path = os.path.join(log_dir, log_name + ".log")
    # One-time migration: logs left behind by the old Tee-Object launchers are
    # UTF-16. Appending UTF-8 to one corrupts the file for every reader, so
    # push a UTF-16 leftover aside before the first write.
    try:
        with open(path, "rb") as f:
            is_utf16 = f.read(2) == b"\xff\xfe"
        # replace must happen after the handle is closed - Windows refuses to
        # rename a file this process still holds open
        if is_utf16:
            os.replace(path, path + ".utf16.bak")
    except OSError:
        pass
    sink = _RotatingSink(path)
    if sys.stdout is not None:
        sys.stdout = _TeeStream(sys.stdout, sink)
    if sys.stderr is not None:
        sys.stderr = _TeeStream(sys.stderr, sink)

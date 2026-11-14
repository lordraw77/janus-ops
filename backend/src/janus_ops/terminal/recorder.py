"""Terminal recordings in asciicast v2 (https://docs.asciinema.org/manual/asciicast/v2/).

Only the output shown to the user is recorded, never the keystrokes: passwords typed at a
`sudo` prompt are not echoed by the remote side, so they do not end up in the file.
"""

import codecs
import json
import os
import time
from pathlib import Path
from typing import IO


class Recorder:
    def __init__(self, directory: Path, session_id: str, cols: int, rows: int, title: str) -> None:
        self.path = directory / f"{session_id}.cast"
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._file: IO[str] = os.fdopen(fd, "w", encoding="utf-8")
        self._start = time.monotonic()
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        header = {
            "version": 2,
            "width": cols,
            "height": rows,
            "timestamp": int(time.time()),
            "title": title,
            "env": {"TERM": "xterm-256color"},
        }
        self._file.write(json.dumps(header) + "\n")

    def _event(self, kind: str, data: str) -> None:
        elapsed = round(time.monotonic() - self._start, 6)
        self._file.write(json.dumps([elapsed, kind, data]) + "\n")

    def output(self, data: bytes) -> None:
        text = self._decoder.decode(data)  # keeps partial UTF-8 sequences for the next chunk
        if text:
            self._event("o", text)

    def resize(self, cols: int, rows: int) -> None:
        self._event("r", f"{cols}x{rows}")

    def close(self) -> None:
        tail = self._decoder.decode(b"", final=True)
        if tail:
            self._event("o", tail)
        self._file.close()


def recording_path(directory: Path, session_id: str) -> Path | None:
    """Path of a recording, built from the id only (never from user input or DB paths)."""
    path = directory / f"{session_id}.cast"
    return path if path.is_file() else None

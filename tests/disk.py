"""A disk that can be told to fill up.

Opens real files, so everything a test reads back is what is really on disk,
and fails writes on demand the way a full disk does: outright, or after part of
the line has already landed.
"""

from __future__ import annotations

import errno
from pathlib import Path

from tacet import annotations as ann


def no_space() -> OSError:
    """A fresh one each time: a raised exception carries its traceback along."""
    return OSError(errno.ENOSPC, "No space left on device")


class Disk:
    def __init__(self) -> None:
        #: While set, every write raises.
        self.full = False
        #: Whether a failing write lands the first half of its line first.
        self.tears = False
        self.opened = 0

    def open(self, path: Path) -> ann.LineHandle:
        self.opened += 1
        return _Handle(ann.open_for_append(path), self)


class _Handle:
    def __init__(self, inner: ann.LineHandle, disk: Disk) -> None:
        self._inner = inner
        self._disk = disk

    def write(self, text: str, /) -> int:
        if self._disk.full:
            if self._disk.tears:
                self._inner.write(text[: len(text) // 2])
                self._inner.flush()
            raise no_space()
        return self._inner.write(text)

    def flush(self) -> None:
        self._inner.flush()

    def fileno(self) -> int:
        return self._inner.fileno()

    def close(self) -> None:
        self._inner.close()

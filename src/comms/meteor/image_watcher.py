"""Output-directory watcher for SatDump METEOR images.

Polls the SatDump output directory for newly created PNG files and emits
a signal for each one so the UI can display it without blocking the main thread.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

# How many times one file may be handed back by retry() before it is given up on.
# At the default 2 s poll interval this is about a minute, longer than SatDump needs
# to finish writing even the largest composite.
_MAX_RETRIES = 30


class ImageWatcher(QObject):
    """Polls *output_dir* every *interval_ms* milliseconds for new PNG files.

    Signals
    -------
    new_image(Path)
        Emitted once per newly discovered PNG file.
    """

    new_image = Signal(object)  # Path

    def __init__(
        self,
        output_dir: Path,
        interval_ms: int = 2000,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._output_dir = output_dir
        self._seen: set[Path] = set()
        self._retries: dict[Path, int] = {}

        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._poll)

    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start watching.  Pre-seeds the seen set so old files are not re-emitted."""
        self._seen = set(self._output_dir.rglob("*.png")) if self._output_dir.exists() else set()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def poll_now(self) -> None:
        """Run one check immediately, bypassing the timer interval.

        Call this right before stop() when a caller knows new files may
        have just been written (e.g. SatDump's post-processing pass
        finishing) and can't afford to wait for the next scheduled tick.
        """
        self._poll()

    def retry(self, path: Path) -> None:
        """Offer *path* again on a later poll, because the receiver could not read it yet.

        SatDump creates a PNG before it has finished writing it, so the first read can
        fail (libpng "Read Error"). Without this the file stays in the seen set and the
        image is lost for good. A file that never becomes readable is dropped after
        _MAX_RETRIES attempts so it cannot be retried forever.
        """
        count = self._retries.get(path, 0) + 1
        self._retries[path] = count
        if count <= _MAX_RETRIES:
            self._seen.discard(path)

    def set_output_dir(self, path: Path) -> None:
        self._output_dir = path
        self._seen.clear()
        self._retries.clear()

    # ------------------------------------------------------------------

    def _poll(self) -> None:
        if not self._output_dir.exists():
            return
        for png in self._output_dir.rglob("*.png"):
            if png not in self._seen:
                self._seen.add(png)
                self.new_image.emit(png)

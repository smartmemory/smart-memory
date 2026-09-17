"""Newline-only Hugging Face download progress for daemon startup logs."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator


def _format_bytes(value: float) -> str:
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.0f} KB"
    return f"{value:.0f} bytes"


class _DownloadBar:
    """Small tqdm-compatible byte counter backed by one shared reporter."""

    def __init__(
        self,
        reporter: "DownloadProgressReporter",
        *,
        total: int | None,
        initial: int,
    ) -> None:
        self._reporter = reporter
        self.total = total
        self.n = initial
        self._reporter._register(self)

    def update(self, amount: float = 1) -> None:
        self.n += amount
        self._reporter._changed(self)

    def close(self) -> None:
        self._reporter._changed(self)

    def __enter__(self) -> "_DownloadBar":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class DownloadProgressReporter:
    """Aggregate concurrent model downloads into throttled, complete log lines."""

    def __init__(
        self,
        emit: Callable[[str], None],
        *,
        clock: Callable[[], float] = time.monotonic,
        interval: float = 2.0,
    ) -> None:
        self._emit = emit
        self._clock = clock
        self._interval = interval
        self._last_emit = float("-inf")
        self._bars: list[_DownloadBar] = []
        self._lock = threading.Lock()

    def new_bar(self, *, total: int | None, initial: int = 0) -> _DownloadBar:
        return _DownloadBar(self, total=total, initial=initial)

    def _register(self, bar: _DownloadBar) -> None:
        with self._lock:
            first_bar = not self._bars
            self._bars.append(bar)
        self._changed(bar, force=first_bar)

    def _changed(self, _bar: _DownloadBar, *, force: bool = False) -> None:
        with self._lock:
            now = self._clock()
            if not force and now - self._last_emit < self._interval:
                return
            downloaded = sum(float(bar.n) for bar in self._bars)
            totals = [bar.total for bar in self._bars]
            total = sum(float(value) for value in totals if value is not None)
            all_known = bool(totals) and all(value is not None for value in totals)
            self._last_emit = now
            if all_known and total > 0:
                percent = min(100, round(downloaded * 100 / total))
                line = (
                    "Downloading the local AI model: "
                    f"{_format_bytes(downloaded)} of {_format_bytes(total)} ({percent}%)"
                )
            else:
                line = f"Downloading the local AI model: {_format_bytes(downloaded)} received"
            # Every update is one complete line. Never emit tqdm's carriage-return redraws.
            self._emit(line.replace("\r", " ").replace("\n", " "))


@contextmanager
def discrete_huggingface_progress(
    emit: Callable[[str], None],
) -> Iterator[None]:
    """Replace Hugging Face terminal bars with throttled newline log updates.

    This is process-local and temporary. It patches only while the daemon resolves
    its startup model files, then restores every Hugging Face function.
    """
    try:
        import huggingface_hub
        from huggingface_hub import file_download
        from huggingface_hub.utils import tqdm
    except ImportError:
        yield
        return

    reporter = DownloadProgressReporter(emit)
    original_snapshot_download = huggingface_hub.snapshot_download
    original_http_get = file_download.http_get
    original_xet_get = file_download.xet_get

    class _QuietTqdm(tqdm):
        def __init__(self, *args: object, **kwargs: object) -> None:
            kwargs["disable"] = True
            super().__init__(*args, **kwargs)

    def snapshot_download(*args: object, **kwargs: object):
        kwargs.setdefault("tqdm_class", _QuietTqdm)
        return original_snapshot_download(*args, **kwargs)

    def http_get(*args: object, **kwargs: object):
        if kwargs.get("_tqdm_bar") is None:
            kwargs["_tqdm_bar"] = reporter.new_bar(
                total=kwargs.get("expected_size"),
                initial=int(kwargs.get("resume_size", 0)),
            )
        return original_http_get(*args, **kwargs)

    def xet_get(*args: object, **kwargs: object):
        if kwargs.get("_tqdm_bar") is None:
            kwargs["_tqdm_bar"] = reporter.new_bar(
                total=kwargs.get("expected_size"),
            )
        return original_xet_get(*args, **kwargs)

    huggingface_hub.snapshot_download = snapshot_download
    file_download.http_get = http_get
    file_download.xet_get = xet_get
    try:
        yield
    finally:
        huggingface_hub.snapshot_download = original_snapshot_download
        file_download.http_get = original_http_get
        file_download.xet_get = original_xet_get

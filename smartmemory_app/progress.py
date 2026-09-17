"""Terminal-safe progress display for long SmartMemory startup waits."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Callable, Iterator, TextIO


@contextmanager
def startup_progress(
    message: str,
    *,
    stream: TextIO | None = None,
    emit: Callable[[str], None] | None = None,
) -> Iterator[Callable[[str], None]]:
    """Show a spinner with elapsed time on terminals and plain lines elsewhere.

    The returned callback is safe to pass to ``start_daemon(on_log=...)``. Rich's
    own console prints streamed daemon lines above the live spinner, so neither
    overwrites the other. Pipes, CI, and launchd logs never receive animation
    control characters.
    """
    output = stream or sys.stdout
    plain_emit = emit or (lambda line: print(line, file=output, flush=True))

    if not getattr(output, "isatty", lambda: False)():
        yield lambda line: plain_emit(f"  {line}")
        return

    try:
        from rich.console import Console
        from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
    except ImportError:
        yield lambda line: plain_emit(f"  {line}")
        return

    console = Console(file=output)
    progress = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )
    with progress:
        progress.add_task(message, total=None)

        def on_log(line: str) -> None:
            progress.console.print(f"  {line}", markup=False)

        yield on_log

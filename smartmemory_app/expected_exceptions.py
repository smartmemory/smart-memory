"""The one list of exceptions that are expected conditions, never crashes.

``crash_reporter.report_exception`` consults ``is_expected`` before anything is
captured, and every ``report_*`` entry point (``report_in_background``,
``crash_notice``, the install troubleshooter, the daemon and viewer ASGI
handlers, the tour) funnels through it. Add a type here, not at a call site.

Types are matched by class name along the exception's MRO, so the list needs no
import of the module that defines them (starlette, storage) and subclasses
count.
"""

EXPECTED_TYPE_NAMES = frozenset(
    {
        # The HTTP client aborted the request. Nothing on our side failed.
        "ClientDisconnect",
        # Expected write-lock concurrency: no write started, the caller retries.
        "StoreBusyError",
    }
)


def is_expected(exc: BaseException) -> bool:
    """True for an expected type, directly or via an explicit ``raise ... from``.

    The implicit ``__context__`` is not followed: a new bug raised while handling
    an expected error is still a crash.
    """
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if any(cls.__name__ in EXPECTED_TYPE_NAMES for cls in type(exc).__mro__):
            return True
        exc = exc.__cause__
    return False

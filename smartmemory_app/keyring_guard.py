"""One gate in front of every OS keychain write (CLI-KEYRING-NULL-GUARD-1).

A setup run under a redirected HOME (tests, scratch jobs, CI) has no login keychain, so
``keyring.set_password`` on macOS makes SecurityAgent pop a "Keychain Not Found" dialog in
the owner's GUI session. Setting ``PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring``
(or ``SMARTMEMORY_NO_KEYRING=1``) says "do not touch the OS store", and every write site
asks ``set_secret`` instead of calling ``keyring`` itself.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

NO_KEYRING_ENV = "SMARTMEMORY_NO_KEYRING"
BACKEND_ENV = "PYTHON_KEYRING_BACKEND"
NULL_BACKEND = "keyring.backends.null.Keyring"

_TRUE = {"1", "true", "yes", "on"}


def keyring_disabled() -> bool:
    """True when the OS credential store must not be written or read.

    Decided from the environment alone, never from ``keyring.get_keyring()``: that call
    caches the first backend it resolves, so a later env change would be ignored.
    """
    if os.environ.get(NO_KEYRING_ENV, "").strip().lower() in _TRUE:
        return True
    return os.environ.get(BACKEND_ENV, "").strip() == NULL_BACKEND


def set_secret(name: str, value: str, *, where: str = "the shell profile") -> bool:
    """Write ``name`` to the OS keychain unless it is disabled.

    Returns False (after one WARNING naming what was kept and where) when the write was
    skipped. Raises whatever ``keyring.set_password`` raises, so callers keep their own
    failure handling. The value is never logged.
    """
    if keyring_disabled():
        logger.warning(
            "OS keychain disabled (%s or the null backend): %s was not written to it and "
            "is kept in %s only.",
            NO_KEYRING_ENV,
            name,
            where,
        )
        return False
    import keyring

    keyring.set_password("smartmemory", name, value)
    return True

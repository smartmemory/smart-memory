"""Every Lite process entry binds the billing mode ``local`` (WRAPPER-2.0-LITE-BIND-BILLING-MODE-1).

Core resolves an unset ``SMARTMEMORY_BILLING_MODE`` as ``enforce``, so a Lite process that never
set it would refuse every platform-keyed call. Core's ``bind_local_mode_for_lite`` is the one
mechanism (``setdefault``: an explicit env value wins, a hosted ``enforce`` stays ``enforce``).
This module only names the wrapper's single call so each entry reads the same.

Call it from a process entry (a ``main`` that a console script, ``python -m`` or a spawned
child starts), never from a library factory such as ``storage.get_memory``: it writes the
process environment and a hosted process may import the same factory.
"""

from __future__ import annotations

from smartmemory.billing.mode import BillingMode, bind_local_mode_for_lite


def bind_lite_billing_mode() -> BillingMode:
    return bind_local_mode_for_lite()

"""Run a scenario once per pytest session, also under xdist (OPS-REAL-USE-HOSTED-LITE-1).

The land gate runs tests/real_use/ with ``-n 4``, so each worker would build the module fixture and run the whole scenario
again. ``observations(tmp_path_factory, name, run)`` runs ``run(workdir)`` in the first worker that gets the lock and writes
the observations next to the session's shared base temp folder; the other workers wait on the lock and read them.
"""

from __future__ import annotations

import fcntl
import json
from pathlib import Path


def observations(tmp_path_factory, name: str, run) -> dict:
    base = Path(tmp_path_factory.getbasetemp())
    shared = base.parent if base.name.startswith("popen-gw") else base
    cache, lock = shared / f"real-use-{name}.json", shared / f"real-use-{name}.lock"
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            if cache.exists():
                return json.loads(cache.read_text(encoding="utf-8"))
            work = shared / f"real-use-{name}"
            obs = run(str(work))
            cache.write_text(json.dumps(obs, default=str), encoding="utf-8")
            return obs
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)

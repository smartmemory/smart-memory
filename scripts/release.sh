#!/usr/bin/env bash
#
# Manual out-of-CI release for the `smartmemory` wrapper.
#
# The wrapper is a thin CLI shipped as a single py3-none-any wheel (it legitimately
# contains .py — the protected source lives in smartmemory-core, not here). So unlike
# the core script this does NOT assert pyc-only. It DOES stay FAIL-CLOSED on sdists:
# per policy we publish wheels only, never a .tar.gz.
#
# Release order: publish smartmemory-core FIRST (this wheel pins it exactly), then run this.
# Prereqs: `uv`, `twine`, a valid ~/.pypirc. Set version + core pin + commit first.
#
# Usage:
#   scripts/release.sh              # build wheel, verify, upload
#   scripts/release.sh --dry-run    # build + verify, do NOT upload
#   scripts/release.sh --check-only # verify existing dist/ only (no build)
#
set -euo pipefail

DRY_RUN=0; CHECK_ONLY=0
for a in "$@"; do
  case "$a" in
    --dry-run)    DRY_RUN=1 ;;
    --check-only) CHECK_ONLY=1 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."   # repo root
VER="$(python3 -c "import tomllib,sys; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")"
echo ">> smartmemory (wrapper) release ${VER}"

# Resolve the uploader BEFORE building: a stale twine should cost zero build time.
if [ "${DRY_RUN}" -eq 0 ] && [ "${CHECK_ONLY}" -eq 0 ]; then
  # ---- twine resolution + version gate (added 2026-08-15) ------------------------------
  # Do NOT use `python3 -m twine`: that binds the upload to whichever interpreter is first
  # on PATH (here, the conda base env), which is exactly how a stale twine got used. twine
  # is a standalone CLI and belongs in its own environment (pipx), independent of any
  # project interpreter.
  #
  # The version floor is load-bearing. The wrapper builds with hatchling, which emits
  # `Metadata-Version: 2.5`; twine validates that field against its bundled `packaging`,
  # and twine < 7 ships a packaging too old to know 2.5, so it rejects a VALID wheel with
  # "'2.5' is not a valid metadata version". Fail here with an actionable message rather
  # than after a full multi-wheel build.
  TWINE_BIN="${TWINE:-$(command -v twine || true)}"
  if [ -z "${TWINE_BIN}" ]; then
    echo "FATAL: twine not found. Install it isolated:  pipx install twine" >&2
    exit 1
  fi
  TWINE_MAJOR="$("${TWINE_BIN}" --version 2>/dev/null | sed -n 's/.*twine version \([0-9][0-9]*\).*/\1/p')"
  if [ -z "${TWINE_MAJOR}" ] || [ "${TWINE_MAJOR}" -lt 7 ]; then
    echo "FATAL: twine >= 7 required (found: $("${TWINE_BIN}" --version 2>&1 | head -1))." >&2
    echo "       Older twine rejects hatchling's Metadata-Version 2.5 on a valid wheel." >&2
    echo "       Fix:  pipx install twine   (or: pipx upgrade twine)" >&2
    exit 1
  fi
  echo ">> twine: ${TWINE_BIN} ($("${TWINE_BIN}" --version 2>&1 | head -1 | cut -d, -f1))"
fi

if [ "${CHECK_ONLY}" -eq 0 ]; then
  rm -rf dist build
  echo ">> building py3-none-any wheel (--wheel only, never sdist)"
  uv build --wheel
fi

# ---- FAIL-CLOSED guards ---------------------------------------------------------------
python3 - "${VER}" <<'PY'
import glob, os, sys, zipfile

ver = sys.argv[1]
tars = glob.glob("dist/*.tar.gz")
if tars:
    sys.exit(f"FATAL: sdist present in dist/ ({[os.path.basename(t) for t in tars]}) — "
             "we publish wheels only. Aborting.")

wheels = glob.glob("dist/smartmemory-*.whl")
if not wheels:
    sys.exit("FATAL: no wrapper wheel found in dist/.")
if len(wheels) != 1:
    sys.exit(f"FATAL: expected exactly one py3-none-any wheel, found {len(wheels)}: {wheels}")

w = wheels[0]; base = os.path.basename(w)
if f"-{ver}-" not in base:
    sys.exit(f"FATAL: {base} does not match pyproject version ({ver}).")

# The wrapper MUST exact-pin core in lockstep — catch a forgotten pin bump.
meta = next(n for n in zipfile.ZipFile(w).namelist() if n.endswith("METADATA"))
pins = [l for l in zipfile.ZipFile(w).read(meta).decode().splitlines()
        if l.lower().startswith("requires-dist: smartmemory-core")]
if not any(f"=={ver}" in p for p in pins):
    sys.exit(f"FATAL: {base} does not pin smartmemory-core[lite]=={ver} (found: {pins}). "
             "Wrapper and core versions move in lockstep.")

print(f">> guards passed: {base}, no sdist, pins core =={ver}")
PY

# ---- smartmemory-mcp pin freshness (added 2026-08-26) ---------------------------------
# The core pin above is guarded fail-closed and has never drifted. The mcp pin had NO
# guard and rotted 16 releases (1.4.51 while 1.4.67 was published) because smartmemory-mcp
# is NOT in the core release sync chain — nothing bumps it for you and nothing complained.
# This cannot be a lockstep check (mcp does not track the wrapper version), so it is a
# WARNING, not a gate: a deliberate hold-back is legitimate, a silent 16-version drift is
# not. Fails closed only if the pinned version does not exist on PyPI at all.
python3 - <<'PY' || exit 1
import json, re, sys, tomllib, urllib.request

deps = tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]
pin = next((d for d in deps if d.replace(" ", "").startswith("smartmemory-mcp==")), None)
if pin is None:
    print(">> smartmemory-mcp: no exact pin found — skipping freshness check.")
    sys.exit(0)
pinned = pin.split("==", 1)[1].strip()

try:
    data = json.load(urllib.request.urlopen("https://pypi.org/pypi/smartmemory-mcp/json", timeout=15))
except Exception as exc:
    print(f">> smartmemory-mcp: PyPI unreachable ({exc}) — freshness UNCHECKED.")
    sys.exit(0)

released = set(data["releases"])
if pinned not in released:
    sys.exit(f"FATAL: pinned smartmemory-mcp=={pinned} is not published on PyPI. "
             "The wheel would be uninstallable.")

key = lambda v: [int(x) for x in re.findall(r"\d+", v)]
latest = max(released, key=key)
if key(latest) > key(pinned):
    print(f">> WARNING: smartmemory-mcp pinned at {pinned}, but {latest} is published. "
          "Bump the pin in pyproject.toml, or hold it back deliberately.")
else:
    print(f">> smartmemory-mcp pin {pinned} is current.")
PY

if [ "${DRY_RUN}" -eq 1 ] || [ "${CHECK_ONLY}" -eq 1 ]; then
  echo ">> dry-run/check-only — not uploading."
  exit 0
fi

echo ">> uploading wheel to PyPI (~/.pypirc token)"
env -u TWINE_USERNAME -u TWINE_PASSWORD "${TWINE_BIN}" upload --non-interactive dist/*.whl
echo ">> done: smartmemory ${VER} (wheel only)"

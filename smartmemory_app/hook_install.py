"""Guarded install of the bundled Claude Code hooks (SM-HOOK-SYNC-CLOBBER-1).

``sm setup`` and every daemon start copy the package's hooks to
``~/.claude/hooks/smartmemory-*.sh``. A copy replaces a file only when the package can
show it wrote the bytes that are there now: the file is missing, already the bundled
bytes, the bytes this install last recorded, or a version a past release shipped. Any
other file was changed by someone else and is left in place with a WARNING naming it.

The record (``installed-hooks.json`` in the data dir) maps each destination path to the
sha256 of the bytes last installed there.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

RECORD_NAME = "installed-hooks.json"

# What a destination holds, from the installer's point of view.
MISSING = "missing"  # nothing there: install
CURRENT = "current"  # already the bundled bytes: nothing to write
OURS = "ours"  # bytes this package installed: safe to upgrade
MODIFIED = "modified"  # anything else: leave it and warn

# sha256 of every committed version of each bundled hook (``git log --follow`` of
# smartmemory_app/hooks/<name> at 276d0c6). An install made before the record existed
# has none, and these let it upgrade a pristine old copy. Later versions are recorded at
# install time, so this table stays frozen.
_RELEASED_SHAS: dict[str, frozenset[str]] = {
    "orient.sh": frozenset(
        {
            "02a5049a50bd41e21fb49281e37c968eb49b525a58ad4d12d2448e0d8ffa0e80",
            "1dd5ab9724b2c948cea6dd2f708341656d6d70b06921394d83dcd60f558db985",
            "43c1f04288b24a8e732452ef4609b9b3dfa06f6f678f6c9b1e3306416fa7c74f",
            "699a648cc9a48c664c55c3a10091d07e37c58ca5032358f6a7f507efbef6f052",
        }
    ),
    "recall.sh": frozenset(
        {
            "342b1f1af4bad6e6571717dac0fe64ec172420e1e79d18e20d69736459cf87ca",
            "599d6b51f95159771859e54641bc91afc9e0036c961bb006e9afe0f3876e8d9f",
            "929d5140cca1d797348c5fde1c04787958e29d506c0b1491e6e568bebe9b0a7f",
            "d39448d60a22482d2e242896e72baab788109015012267994a6556a8396737dd",
            "f1f29ec6bd7d7dc8c179afa0320307093f2c9223ec92c86599b9c52715cde54c",
        }
    ),
    "observe.sh": frozenset(
        {
            "21cf6be1977c72a5b0e516a4d103681431d4936c9eb8689c939a12c753e103ec",
            "2ced89e8e36c310b847a9fc89b1b3e2795970ba303e5886f74f1dc2239dedf76",
            "42b944622a2db30acc59a48bf3a4a42450164f4ddc837fccf34683cec46de9ac",
            "50cb58313812ad3f9368005bfb72d84762b8fc012dac913e63b4a9d99fc8906f",
            "621af9410a58614d9379ad27fadde9b1b47fc13d48f8843aec74e52cb7ec9aa0",
            "75f63794c5a217dd626c104d633cd2ce44b0958e1b9e44ba091e8cd630eac676",
            "819f68e6cdcf6f68529ca004990963079556189b56326711be2beddf0e438918",
            "b32b627bdc2a6b5c0bb897e7de0d91643f969a9b90b8038b28d64eecf7863735",
            "badbad75091c5ef9e32224b86703fa1807eb494fe996b9cf3414d39059efd3d6",
            "bc0cb404f45bc15ce94d708505740c8fc4e9623bafa28708b4090e19291c732d",
            "c1238e7ae125cee17a807bddc952c91d45dbdbd3d4e05a4852344ec35b58f5d2",
            "c4d6f8d8b79fd346cdaa7fa73fc7a58c3a9e4333f9c27af239cadfd131a473dd",
            "ce5025573c2de4983bda225cbced9ab2d16978a104589681b8562d7b7b868749",
        }
    ),
    "distill.sh": frozenset(
        {
            "1b8b9ea737202e1dd783d51e79ca5288161571182d8d44d6aedf336cb28d31e6",
            "39bce2d9c7be4e57d540d8e9741e3fece440491422d6c8e9d70326cc38b39206",
            "4541878f8f638ad1af6d215fef900e39bf2432be8046d57fb482151f4db204a7",
            "4c28c787d1d2b70658421359b3c129487f80b950240fb8e350f1da0d1aba07a4",
            "55296ec5ad8f61fd473208f445ab71f7c2ed1b80bab7399e5becb5101aa92ab1",
            "5f18353f0027993f5e0da4dcd4a42f338ad562a71273dd8246865c38a4af3af8",
            "64703e689d9b0b5cab4848455e9291abe22ea60babae1d58bb79177d0ee94c5e",
            "71e21af451482f04bffb24a8f5ce5eccb9f620f01ae016ead353c900d9b7cfe2",
            "88536127bf2dac12f246ef22ac12774206252b5fc55272a1a59bca858e82ce92",
            "d515ebd218d68511f66722d58d1ed016df78b4ce6c0f291d13c1aa496146878e",
            "f43c807a6f8650d61772db96dae51c728cd72627a80bb35780a9a17517c0ed34",
            "fdaec56136082206a021cd7bb7cd2a2055b27e935ab3a6b1a37efac1da9c1b83",
            "fdbf4d751ea94e452c2dbf8b0081478994245454b956027428913ed3dd81946a",
        }
    ),
    "learn.sh": frozenset(
        {
            "377bdffc4a8f248863265cc7e8e015005888cf0063f208dd8c95a5ca6cbc5094",
            "3a43be880560ce52fd5a82f2bf43c8547229bd0399a9dbdec3ddd6789412380c",
            "5b1b122d36076ec0f6d8773e8af8d2e2281d284ee580052cf1f633704b6b7c0e",
            "646aee10b6f304d6476907d42af5c4f6e68dfcebf6e494ab7d0372bc207473e1",
            "6a194a5cfbed9632683e7c6387b31902a6e69234cc5957a8991f4168fe52b8f7",
            "775c55961e14bbf75c09edf8624ee3976dd974fe8c6c29cdf44d5d370f5a00fc",
            "7f79dec309d8bcdc9dd89cdd5cd9b4e26393d40439457b63b6b13bfe6ee1c074",
            "a990ed38a298ceea03750325a9a7b8766ef50a88a073baceda167b23a9fd5516",
            "d4ea1fecd74454991f772432cf12ad7e891a47472020c350fcb63eb2abb1738e",
            "df3b985ff9cc4582f229f44b69c19891f4f834dcacfc8b100007cfc59469b682",
            "ea48006e5b862a064c143b8a22cbc5a02d79cd9f4645e7c4f46410cd8d5aee63",
            "f56484db9e797663b3fbceb310485337af3bf6c01b511db2ffcc75a8a0c2b3f1",
            "fe97806c1574d35828be08ae2ab34bdfacf89c9c3dc4e70f168e73fc99d75016",
        }
    ),
    "persist.sh": frozenset(
        {
            "b51e75ca770216f042cd833a34291a4d2551531614619c6c6e373eb0deecd124",
            "dc2c105f7b5af33f2186901a2ead8fa90c8fb25a7b218c6b7d6d9b909daf8324",
            "ee9330702df8c420d7286698ab02580953c1c693ee255f9bec7f0771116dfbd9",
            "f4d9a30c11db50ee9d7d3a28c65e68198e8e72e5c75614ade8c0bcfca70c0db3",
            "fda2055ad0c816a58e61bce6bdde3a0003bb0099a947dd5c3309e953d7d2f9ce",
        }
    ),
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def record_path() -> Path:
    from smartmemory_app.storage import _resolve_data_dir

    return _resolve_data_dir() / RECORD_NAME


def load_record() -> dict[str, str]:
    """Destination path -> sha256 last installed there. Unreadable counts as empty."""
    path = record_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning(
            "Hook install record %s could not be read (%s): a hook that differs from "
            "this package and every past release counts as locally modified",
            path,
            type(exc).__name__,
        )
        return {}
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return {}
    return {k: v for k, v in hooks.items() if isinstance(k, str) and isinstance(v, str)}


def _save_record(hooks: dict[str, str]) -> None:
    path = record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{RECORD_NAME}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps({"version": 1, "hooks": hooks}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def hook_state(
    target: Path, src_name: str, bundled: bytes, record: dict[str, str]
) -> str:
    """Classify ``target`` against the bundled bytes. A symlink is judged by the file it names."""
    if not os.path.lexists(target):
        return MISSING
    try:
        current = target.read_bytes()
    except OSError:
        # A dangling link, a directory, an unreadable file: nothing proves it is ours.
        return MODIFIED
    if current == bundled:
        return CURRENT
    digest = _sha256(current)
    if record.get(str(target)) == digest or digest in _RELEASED_SHAS.get(src_name, ()):
        return OURS
    return MODIFIED


def _publish(src: Path, target: Path) -> None:
    # bash reads a script while it runs: publish by rename, so a hook running at the
    # swap keeps reading the old file whole instead of a mix of old and new bytes.
    # Through a symlink, never over it: the link is the user's, the file it names is ours.
    real = target.resolve() if target.is_symlink() else target
    temporary = real.with_name(f".{real.name}.{os.getpid()}.tmp")
    try:
        shutil.copy2(src, temporary)
        os.replace(temporary, real)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def install_hooks(src_dir: Path, dest_dir: Path, file_map: dict[str, str]) -> list[str]:
    """Install or upgrade the hooks the package owns. Returns the names left in place."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    record = load_record()
    updated = dict(record)
    kept: list[str] = []
    for src_name, dest_name in file_map.items():
        src = src_dir / src_name
        if not src.exists():
            continue
        bundled = src.read_bytes()
        target = dest_dir / dest_name
        state = hook_state(target, src_name, bundled, record)
        if state == MODIFIED:
            logger.warning(
                "hook %s locally modified, not replaced (%s). Delete it and run sm setup "
                "to take this version's copy",
                dest_name,
                target,
            )
            kept.append(dest_name)
            continue
        if state != CURRENT:
            _publish(src, target)
        updated[str(target)] = _sha256(bundled)
    if updated != record:
        _save_record(updated)
    return kept

"""SmartMemory Claude Code plugin — zero-infra persistent memory."""

import os
from importlib.metadata import version as _pkg_version, PackageNotFoundError

# huggingface_hub reads these once at import; its ~10 s defaults time out model
# downloads on slow links during setup. User-set values win.
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "300")
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "60")

try:
    __version__ = _pkg_version("smartmemory")
except PackageNotFoundError:
    __version__ = "dev"

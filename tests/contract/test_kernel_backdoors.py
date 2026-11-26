from pathlib import Path

from smartmemory.testing import kernel_fence as fence


def test_kernel_backdoors():
    snapshot = fence.load_snapshot(Path(__file__).with_name("kernel_backdoors.json"))
    handles = [fence.HANDLES_BY_KEY[key] for key in snapshot["handle_keys"]]
    sites = fence.scan_handles(
        Path(__file__).resolve().parents[2], ["smartmemory_app"], handles
    )
    result = fence.compare_handles(sites, snapshot["handles"])
    fence.warn_stale(result)
    assert not result.failures, result.report()

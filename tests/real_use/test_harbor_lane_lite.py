"""Real-use scenario for the Lite app: one person's week offline, in through the Lite app's own surface (OPS-REAL-USE-HOSTED-LITE-1).

One row per frozen note row (smart-memory-ops units/OPS-REAL-USE-HOSTED-LITE-1/notes-lite.md, machine form
harbor_lane_lite/scenario.json). The land gate (smart-memory-ops land_repo.real_use_gate) runs this folder on the merged tip and
requires it green. No model is configured and no network is allowed, so nothing is recorded or replayed. A known gap
(harbor_lane_lite/expected.json) is an xfail naming its bug; a known gap that passes fails, so the list is cut by the fix.
"""

import pytest

from tests.real_use import _once, lite_week

GAPS = lite_week.known_gaps()


@pytest.fixture(scope="module")
def outcome(tmp_path_factory):
    obs = _once.observations(tmp_path_factory, lite_week.NAME, lite_week.run)
    return lite_week.checks(obs)


def test_known_gaps_name_real_rows():
    assert sorted(set(GAPS) - set(lite_week.ROW_IDS)) == []


@pytest.mark.parametrize("row", lite_week.ROW_IDS)
def test_real_use(outcome, row):
    ok, detail = outcome[row]
    if row in GAPS:
        assert not ok, (
            f"GAP CLOSED: {row} passes now ({detail}); remove it from {lite_week.NAME}/expected.json"
        )
        pytest.xfail(f"known gap: {GAPS[row]} | {detail}")
    assert ok, detail

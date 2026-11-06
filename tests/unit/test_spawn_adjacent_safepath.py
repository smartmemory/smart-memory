"""Real children must not import modules planted in the caller's directory."""

import json
from pathlib import Path

from click.testing import CliRunner

from smartmemory_app import cli as cli_module
from smartmemory_app import daemon, tour, update_check
from smartmemory_app.diagnostic_process import offline_checks as real_offline_checks


def _child_paths(tmp_path, monkeypatch, package, payload):
    """Supply an offline installed payload and a hostile caller package."""
    trusted = tmp_path / "trusted"
    installed = trusted / package
    installed.mkdir(parents=True)
    (installed / "__init__.py").write_text("")
    for name, code in payload.items():
        (installed / name).write_text(code)
    caller = tmp_path / "caller"
    hostile = caller / package
    hostile.mkdir(parents=True)
    marker = tmp_path / "shadow-imported"
    (hostile / "__init__.py").write_text(
        f"from pathlib import Path; Path({str(marker)!r}).touch()\n"
        "raise SystemExit(0)\n"
    )
    monkeypatch.chdir(caller)
    # A trusted absolute PYTHONPATH is the existing worker fix's trust boundary.
    monkeypatch.setenv("PYTHONPATH", str(trusted.resolve()))
    return marker


def test_tour_child_ignores_caller_package(tmp_path, monkeypatch):
    observed = tmp_path / "tour-child.json"
    marker = _child_paths(
        tmp_path,
        monkeypatch,
        "smartmemory_app",
        {
            "viewer_server.py": (
                "import json, os, sys\nfrom pathlib import Path\n"
                "def main(**kwargs):\n"
                f"    Path({str(observed)!r}).write_text(json.dumps("
                "dict(cwd=os.getcwd(), safe=sys.flags.safe_path)))\n"
            )
        },
    )
    # Exercise relative/symlink data paths as well as caller-cwd shadowing.
    data = tmp_path / "data"
    data.mkdir()
    Path("tour-link").symlink_to(data, target_is_directory=True)
    child = tour.TourDaemon(port=19998, data_dir="tour-link", keep=True)

    def wait_for_child():
        assert child.process.wait(timeout=10) == 0

    monkeypatch.setattr(child, "_wait_until_healthy", wait_for_child)
    monkeypatch.setattr(child, "_assert_reached_empty_store", lambda: None)
    try:
        child._start_selected_port()
        assert not marker.exists(), "tour imported the caller's package"
        assert json.loads(observed.read_text()) == {
            "cwd": str(data.resolve()),
            "safe": True,
        }
    finally:
        child.stop()


def test_diagnostic_child_ignores_caller_package(tmp_path, monkeypatch):
    marker = _child_paths(
        tmp_path,
        monkeypatch,
        "smartmemory_app",
        {
            "diagnostic_worker.py": (
                "import json, os, sys\n"
                "print(json.dumps('trusted diagnostic'))\n"
                "print(json.dumps('safe=' + str(sys.flags.safe_path)))\n"
                "print(json.dumps('cwd=' + os.getcwd()))\n"
            )
        },
    )
    rows = real_offline_checks("dependencies", budget=10)
    assert not marker.exists(), "diagnostic imported the caller's package"
    assert rows == [
        "trusted diagnostic",
        "safe=True",
        f"cwd={Path(cli_module.sys.executable).resolve().parent}",
    ]


def test_version_child_ignores_caller_stdlib_shadow(tmp_path, monkeypatch):
    caller = tmp_path / "caller"
    caller.mkdir()
    marker = tmp_path / "shadow-imported"
    # importlib.metadata imports email in a fresh interpreter.
    (caller / "email.py").write_text(
        f"from pathlib import Path; Path({str(marker)!r}).touch()\nraise SystemExit(0)\n"
    )
    monkeypatch.chdir(caller)
    version = cli_module._installed_version_from_subprocess()
    assert not marker.exists(), "version probe imported the caller's email.py"
    assert version


def test_update_pip_child_ignores_caller_package(tmp_path, monkeypatch):
    observed = tmp_path / "pip-child.json"
    marker = _child_paths(
        tmp_path,
        monkeypatch,
        "pip",
        {
            "__main__.py": (
                "import json, os, sys\nfrom pathlib import Path\n"
                f"Path({str(observed)!r}).write_text(json.dumps("
                "dict(cwd=os.getcwd(), safe=sys.flags.safe_path, args=sys.argv[1:])))\n"
            )
        },
    )
    monkeypatch.setattr(update_check, "latest_version", lambda **kwargs: "999.0.0")
    monkeypatch.setattr(daemon, "get_status", lambda: None)
    monkeypatch.setattr(
        cli_module, "_installed_version_from_subprocess", lambda: "999.0.0"
    )
    # Invoke the product command callback without unrelated CLI startup diagnostics.
    result = CliRunner().invoke(cli_module.update_cmd, obj={})
    assert result.exit_code == 0, result.output
    assert not marker.exists(), "update imported the caller's pip package"
    assert json.loads(observed.read_text()) == {
        "cwd": str(Path(cli_module.sys.executable).resolve().parent),
        "safe": True,
        "args": ["install", "-U", "smartmemory"],
    }

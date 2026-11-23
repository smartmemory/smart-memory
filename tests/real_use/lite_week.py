"""Real-use scenario for the Lite app: one person's week, offline (OPS-REAL-USE-HOSTED-LITE-1).

Ground truth: harbor_lane_lite/scenario.json, the machine form of smart-memory-ops
units/OPS-REAL-USE-HOSTED-LITE-1/notes-lite.md (frozen before this code). The week is sent through the Lite app's public
surface: its local HTTP routes (``smartmemory_app.local_api`` mounted at ``/memory``, what the CLI, viewer and hooks call),
the assistant connector Lite installs for decisions (``smartmemory_mcp.backends.local.LocalBackend``), and the ``export`` /
``import`` commands run as a user runs them (``python -m smartmemory_app``). Answers are checked in plain code; no model
judges, and no model is configured at all: every provider key is empty and every non-loopback socket is refused and logged
(``_net_guard``), so the run needs no key and no network, and nothing is recorded or replayed.

Two child processes, each with its own fresh data folder and HOME: ``week`` (rows L-01..L-20) and ``fresh`` (a new install
importing the export, rows L-21..L-23). Row L-24 reads the guard logs of both and of every CLI run.

    python -m tests.real_use.lite_week run [--keep]   # run the scenario, print one line per row and the verdict
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
NAME = "harbor_lane_lite"
PROVIDER_KEYS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GROQ_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "DEEPSEEK_API_KEY",
    "MISTRAL_API_KEY",
    "TOGETHER_API_KEY",
    "LLM_API_KEY",
)
PASS_ENV = ("PATH", "LANG", "LC_ALL", "TMPDIR", "PYTHONPATH", "USER", "LOGNAME")
CHILD_TIMEOUT_S = 900


def load() -> dict:
    return json.loads((HERE / NAME / "scenario.json").read_text(encoding="utf-8"))


def known_gaps() -> dict[str, str]:
    p = HERE / NAME / "expected.json"
    return (
        json.loads(p.read_text(encoding="utf-8")).get("known_gaps", {})
        if p.exists()
        else {}
    )


ROW_IDS = [f"L-{n:02d}" for n in range(1, 25)]


# ------------------------------------------------------------------ parent: environment and child runs
def child_env(work: Path, data_dir: Path, net_log: Path) -> dict:
    """A clean environment: allowlisted names only, a throwaway HOME and data folder, every provider key empty, models offline."""
    env = {k: os.environ[k] for k in PASS_ENV if os.environ.get(k)}
    real_hf = os.environ.get("HF_HOME") or str(Path.home() / ".cache" / "huggingface")
    home = work / "home"
    home.mkdir(parents=True, exist_ok=True)
    env.update(
        HOME=str(home),
        HF_HOME=real_hf,
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        SMARTMEMORY_DATA_DIR=str(data_dir),
        SMARTMEMORY_MCP_MODE="local",
        SMARTMEMORY_CRASH_REPORTS="0",
        SMARTMEMORY_ENV_CHECK="0",
        SMARTMEMORY_TELEMETRY="0",
        DO_NOT_TRACK="1",
        PYTHONDONTWRITEBYTECODE="1",
        SM_REAL_USE_NET_LOG=str(net_log),
    )
    for k in PROVIDER_KEYS:
        env[k] = ""
    paths = [str(REPO)] + [
        p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p
    ]
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(paths))
    return env


def _child(phase: str, work: Path, data_dir: Path, out: Path, extra: list[str]) -> dict:
    net_log = work / f"net-{phase}.log"
    net_log.write_text("", encoding="utf-8")
    env = child_env(work, data_dir, net_log)
    argv = [
        sys.executable,
        "-m",
        "tests.real_use.lite_week",
        phase,
        "--out",
        str(out),
        "--work",
        str(work),
        *extra,
    ]
    with open(work / f"{phase}.log", "w", encoding="utf-8") as log:
        p = subprocess.run(
            argv,
            env=env,
            cwd=str(REPO),
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=CHILD_TIMEOUT_S,
        )
    obs = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    obs["rc"] = p.returncode
    if p.returncode != 0 or "error" in obs:
        tail = (work / f"{phase}.log").read_text(encoding="utf-8", errors="replace")[
            -1500:
        ]
        obs.setdefault("error", f"child {phase} rc {p.returncode}")
        obs["log_tail"] = tail
    return obs


def run(workdir: str | None = None) -> dict:
    """-> observations {'week': ..., 'fresh': ..., 'net': [...], 'provider_keys_set': [...]}. Removes its folders unless workdir is given."""
    own = workdir is None
    work = Path(workdir or tempfile.mkdtemp(prefix="real-use-lite-"))
    work.mkdir(parents=True, exist_ok=True)
    try:
        export_dir = work / "export"
        week = _child(
            "week",
            work,
            work / "data-week",
            work / "week.json",
            ["--export", str(export_dir)],
        )
        fresh = (
            _child(
                "fresh",
                work,
                work / "data-fresh",
                work / "fresh.json",
                ["--import", str(export_dir)],
            )
            if export_dir.is_dir()
            else {"error": "no export folder to import (see L-20)"}
        )
        net = []
        for f in sorted(work.glob("net-*.log")):
            net += [
                ln for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()
            ]
        keys = sorted(k for k in PROVIDER_KEYS if child_env(work, work, work / "x")[k])
        return {"week": week, "fresh": fresh, "net": net, "provider_keys_set": keys}
    finally:
        if own:
            shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------ children
def _cli(args: list[str], work: Path) -> tuple[int, str]:
    """One Lite CLI command, as a user runs it, under the offline guard (same env as this child)."""
    argv = [
        sys.executable,
        "-m",
        "tests.real_use._net_guard",
        os.environ["SM_REAL_USE_NET_LOG"],
        "smartmemory_app",
        *args,
    ]
    p = subprocess.run(
        argv,
        cwd=str(work),
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=600,
    )
    return p.returncode, p.stdout + p.stderr


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from smartmemory_app import local_api

    app = FastAPI()
    app.mount("/memory", local_api.api)
    return TestClient(app, raise_server_exceptions=False)


def _ids(items) -> list[str]:
    out = []
    for r in items or []:
        if isinstance(r, dict):
            out.append(
                str(r.get("item_id") or r.get("id") or r.get("decision_id") or "")
            )
    return out


def _search(http, q: str, k: int) -> dict:
    r = http.post("/memory/search", json={"query": q, "top_k": k})
    items = r.json().get("items", []) if r.status_code == 200 else []
    return {
        "status": r.status_code,
        "ids": _ids(items),
        "contents": [str(i.get("content") or "") for i in items if isinstance(i, dict)],
        "superseded": [bool(i.get("superseded")) for i in items if isinstance(i, dict)],
    }


def _save(http, text: str) -> dict:
    r = http.post(
        "/memory/ingest",
        json={
            "content": text,
            "memory_type": "semantic",
            "context": {"origin": "cli:add"},
        },
    )
    body = (
        r.json()
        if r.headers.get("content-type", "").startswith("application/json")
        else {}
    )
    item_id = (
        body.get("item_id") or body.get("id") or (body.get("item") or {}).get("item_id")
        if isinstance(body, dict)
        else None
    )
    return {"status": r.status_code, "id": str(item_id or ""), "body": str(body)[:300]}


def _decision(d) -> dict:
    d = d or {}
    return {
        "id": str(d.get("decision_id") or d.get("id") or ""),
        "status": d.get("status"),
        "content": d.get("content") or "",
    }


def child_week(a) -> dict:
    from tests.real_use import _net_guard

    _net_guard.install(os.environ["SM_REAL_USE_NET_LOG"])
    from smartmemory_mcp.backends.local import LocalBackend

    data, k = load(), load()["search_top_k"]
    http, b = _client(), LocalBackend()
    obs: dict = {"saved": {}, "search": {}}
    for key in ("standup", "client", "launch", "invoice", "printer"):
        obs["saved"][key] = _save(http, data["notes"][key]["text"])
    ids = {key: v["id"] for key, v in obs["saved"].items()}
    obs["search"]["standup"] = _search(http, data["queries"]["standup"], k)
    obs["search"]["launch"] = _search(http, data["queries"]["launch"], k)
    r = http.get(f"/memory/{ids['client']}")
    obs["get_client"] = {
        "status": r.status_code,
        "content": str(r.json().get("content") or "") if r.status_code == 200 else "",
    }
    dn = data["decisions"]
    obs["netlify"] = _decision(
        b.decision_create(dn["netlify"]["text"], evidence_ids=[ids["client"]])
    )
    obs["saved"]["cost"] = _save(http, data["notes"]["cost"]["text"])
    ids["cost"] = obs["saved"]["cost"]["id"]
    sup = b.decision_supersede(
        obs["netlify"]["id"], dn["cloudflare"]["text"], dn["cloudflare"]["reason"]
    )
    # The connector answers {old_decision_id, new_decision_id, status}; the new decision is read back by its id.
    new_id = sup.get("new_decision_id") if isinstance(sup, dict) else None
    obs["cloudflare"] = _decision(b.decision_get(new_id)) if new_id else _decision(sup)
    obs["supersede_answer"] = str(sup)[:400]
    obs["netlify_after"] = _decision(b.decision_get(obs["netlify"]["id"]))
    obs["active"] = [_decision(d) for d in b.decision_list(limit=200)]
    obs["decision_search"] = [
        _decision(d) for d in b.decision_search(data["queries"]["hosting"], limit=20)
    ]
    r = http.get(
        "/memory/recall", params={"query": data["queries"]["recall"], "top_k": 10}
    )
    obs["recall"] = {
        "status": r.status_code,
        "text": json.dumps(r.json())[:20000] if r.status_code == 200 else r.text[:300],
    }
    r = http.delete(f"/memory/{ids['printer']}")
    obs["delete_printer"] = {"status": r.status_code, "body": r.text[:200]}
    obs["search"]["printer"] = _search(http, data["queries"]["printer"], k)
    r = http.get(f"/memory/{ids['printer']}")
    obs["get_printer"] = {"status": r.status_code}
    r = http.get("/memory/graph/full")
    nodes = r.json().get("nodes", []) if r.status_code == 200 else []
    obs["graph"] = {
        "status": r.status_code,
        "ids": sorted(
            {
                str(n.get("item_id") or n.get("id") or "")
                for n in nodes
                if isinstance(n, dict)
            }
        ),
    }
    obs["ids"] = ids
    # Export, as the user types it, from this install's data folder.
    rc, out = _cli(["export", a.export], Path(a.work))
    texts = ""
    if Path(a.export).is_dir():
        for f in sorted(Path(a.export).rglob("*")):
            if f.is_file() and f.suffix in (
                ".md",
                ".json",
                ".jsonl",
                ".yaml",
                ".yml",
                ".txt",
            ):
                texts += f.read_text(encoding="utf-8", errors="replace") + "\n"
    obs["export"] = {"rc": rc, "out": out[-400:], "text": texts}
    return obs


def child_fresh(a) -> dict:
    from tests.real_use import _net_guard

    _net_guard.install(os.environ["SM_REAL_USE_NET_LOG"])
    data, k = load(), load()["search_top_k"]
    rc, out = _cli(["import", a.import_dir], Path(a.work))
    summary = [
        ln for ln in out.splitlines() if ln.startswith(("Imported ", "OKF bundle:"))
    ]
    obs: dict = {
        "import": {"rc": rc, "out": " | ".join(summary) or out[-600:]},
        "search": {},
    }
    http = _client()
    obs["search"]["launch"] = _search(http, data["queries"]["launch"], k)
    obs["search"]["printer"] = _search(http, data["queries"]["printer"], k)
    return obs


# ------------------------------------------------------------------ checks (plain code, no model)
def _top(search: dict, item_id: str, n: int = 3) -> bool:
    return bool(item_id) and item_id in search.get("ids", [])[:n]


def checks(obs: dict) -> dict[str, tuple[bool, str]]:
    data = load()
    w, f = obs.get("week") or {}, obs.get("fresh") or {}
    res: dict[str, tuple[bool, str]] = {}
    err = w.get("error")
    if err:  # the week child died: every row of it fails with the reason, never a skip
        for rid in ROW_IDS[:20]:
            res[rid] = (
                False,
                f"week run died: {err} | {str(w.get('log_tail', ''))[-600:]}",
            )
    else:
        saved, ids, s = w["saved"], w["ids"], w["search"]
        for key in ("standup", "client", "launch", "invoice", "printer", "cost"):
            row = data["notes"][key]["row"]
            res[row] = (
                saved[key]["status"] == 200 and bool(saved[key]["id"]),
                f"status {saved[key]['status']}, id {saved[key]['id']!r}",
            )
        res["L-06"] = (
            _top(s["standup"], ids["standup"]),
            f"standup {ids['standup']} in top 3 of {s['standup']['ids']}",
        )
        res["L-07"] = (
            _top(s["launch"], ids["launch"]),
            f"launch {ids['launch']} in top 3 of {s['launch']['ids']}",
        )
        gc = w["get_client"]
        res["L-08"] = (
            gc["status"] == 200 and data["notes"]["client"]["text"] in gc["content"],
            f"status {gc['status']}, content {gc['content'][:80]!r}",
        )
        net, cf, after = w["netlify"], w["cloudflare"], w["netlify_after"]
        res["L-09"] = (bool(net["id"]) and net["status"] == "active", f"netlify {net}")
        res["L-11"] = (
            bool(cf["id"]) and cf["id"] != net["id"] and "Cloudflare" in cf["content"],
            f"answer {w['supersede_answer'][:200]}",
        )
        res["L-12"] = (
            after["status"] == "superseded",
            f"netlify status {after['status']!r}",
        )
        act = {d["id"] for d in w["active"]}
        res["L-13"] = (
            cf["id"] in act and net["id"] not in act,
            f"cloudflare listed {cf['id'] in act}, netlify listed {net['id'] in act}",
        )
        ds = {d["id"] for d in w["decision_search"]}
        res["L-14"] = (
            cf["id"] in ds and net["id"] not in ds,
            f"cloudflare found {cf['id'] in ds}, netlify found {net['id'] in ds}",
        )
        rc = w["recall"]
        res["L-15"] = (
            rc["status"] == 200 and data["launch_phrase"] in rc["text"],
            f"status {rc['status']}, launch phrase in recall: {data['launch_phrase'] in rc['text']}",
        )
        dp = w["delete_printer"]
        res["L-16"] = (
            200 <= dp["status"] < 300,
            f"DELETE {dp['status']} {dp['body'][:80]}",
        )
        res["L-17"] = (
            s["printer"]["status"] == 200 and ids["printer"] not in s["printer"]["ids"],
            f"printer {ids['printer']} in {s['printer']['ids']}",
        )
        res["L-18"] = (
            w["get_printer"]["status"] == 404,
            f"GET deleted printer: {w['get_printer']['status']}",
        )
        g = set(w["graph"]["ids"])
        missing = [k for k in data["kept_after_delete"] if ids[k] not in g]
        res["L-19"] = (
            w["graph"]["status"] == 200 and not missing and ids["printer"] not in g,
            f"status {w['graph']['status']}, kept missing {missing}, printer node {ids['printer'] in g}",
        )
        ex = w["export"]
        absent = [
            k
            for k in data["kept_after_delete"]
            if data["notes"][k]["text"] not in ex["text"]
        ]
        res["L-20"] = (
            ex["rc"] == 0 and not absent and data["printer_phrase"] not in ex["text"],
            f"rc {ex['rc']}, texts missing {absent}, printer in export {data['printer_phrase'] in ex['text']} | {ex['out'][-200:]}",
        )
    ferr = f.get("error")
    if ferr:
        for rid in ("L-21", "L-22", "L-23"):
            res[rid] = (
                False,
                f"fresh-install run died: {ferr} | {str(f.get('log_tail', ''))[-600:]}",
            )
    else:
        imp = f["import"]
        import re

        m = re.search(r"Imported (\d+) records \((\d+) errors", imp["out"])
        n, errs = (int(m.group(1)), int(m.group(2))) if m else (0, -1)
        want = len(data["kept_after_delete"])
        res["L-21"] = (
            imp["rc"] == 0 and n >= want and errs == 0,
            f"rc {imp['rc']}, imported {n} (want >= {want}), errors {errs} | {imp['out'][-200:]}",
        )
        sl = f["search"]["launch"]
        res["L-22"] = (
            any(data["launch_phrase"] in c for c in sl["contents"][:3]),
            f"top 3 contents {[c[:50] for c in sl['contents'][:3]]}",
        )
        sp = f["search"]["printer"]
        res["L-23"] = (
            sp["status"] == 200
            and not any(data["printer_phrase"] in c for c in sp["contents"]),
            f"status {sp['status']}, contents {[c[:50] for c in sp['contents']]}",
        )
    res["L-24"] = (
        not obs.get("net") and not obs.get("provider_keys_set"),
        f"outbound attempts {obs.get('net', [])[:3]}, keys set {obs.get('provider_keys_set')}",
    )
    return res


def verdict(res: dict, gaps: dict) -> tuple[bool, list[str]]:
    ok, lines = True, []
    for cid in sorted(set(gaps) - set(res)):
        ok = False
        lines.append(f"FAIL {cid}: known gap names no row")
    for cid in ROW_IDS:
        good, detail = res[cid]
        if cid in gaps:
            if good:
                ok = False
                lines.append(
                    f"FAIL {cid}: GAP CLOSED, remove it from expected.json ({gaps[cid]})"
                )
            else:
                lines.append(f"GAP  {cid}: {detail} [{gaps[cid]}]")
        elif good:
            lines.append(f"PASS {cid}: {detail}")
        else:
            ok = False
            lines.append(f"FAIL {cid}: {detail}")
    return ok, lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=("run", "week", "fresh"))
    ap.add_argument("--out")
    ap.add_argument("--work")
    ap.add_argument("--export")
    ap.add_argument("--import", dest="import_dir")
    ap.add_argument("--keep", help="keep the run's folders here (diagnosis)")
    ap.add_argument("--json", help="write the observations here")
    a = ap.parse_args(argv)
    if a.phase == "run":
        obs = run(a.keep)
        if a.json:
            Path(a.json).write_text(
                json.dumps(obs, indent=1, default=str), encoding="utf-8"
            )
        ok, lines = verdict(checks(obs), known_gaps())
        print("\n".join(lines))
        print(f"REAL-USE {NAME} {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1
    try:
        obs = child_week(a) if a.phase == "week" else child_fresh(a)
    except Exception as exc:  # the parent turns this into failing rows with the reason
        import traceback

        traceback.print_exc()
        obs = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    Path(a.out).write_text(json.dumps(obs, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

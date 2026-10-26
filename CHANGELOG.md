# Changelog — smartmemory

## Unreleased

- HOOK-CONSUMER-SHUTDOWN-RACE-1 (2026-10-26): fix(daemon): a mid-drain stop no longer closes the store under a running hook job (measured: 2 of 6 SIGTERMs on a real store copy died with SIGSEGV in `sqlite.py` `execute`, one at 6.3 s, past launchd's 5 s). Work that can outlive uvicorn's drain now holds the store through `storage.store_in_use()`: hook consumer jobs and the `/lifecycle` phase pool (uvicorn 0.53 returns from `server.run()` while sync-route and `run_in_executor` threads still run). Cleanup closes the store only once the consumer thread has stopped and no gated work runs (`close_store_when_idle`, 0.25 s wait); otherwise the daemon exits with the store left open, the uncommitted write rolls back and the job stays in `running/`. The atexit close is gated the same way. A daemon stop during a job, and a lost store write lock (`StoreBusyError`), no longer use the job's failure budget: the consumer records the jobs a stop leaves running in `hook-queue/interrupted.json` and recovery requeues those without an attempt; both kinds get up to 40 free retries (`SMARTMEMORY_HOOK_MAX_FREE_RETRIES`, about 55 minutes of continuous lock loss), after which they count as failures again. Test: `tests/unit/test_consumer_shutdown_race.py` (3 shutdown rows, 7 attempt-budget rows).
- HOOK-CONSUMER-SHUTDOWN-RACE-1 round 2 (2026-10-26): fix(daemon): sync HTTP routes hold the store too. Every sync route of the `/memory` app (list, get, ingest, search, update, delete, ...) runs inside `store_in_use()` on the worker thread that executes it (`local_api._StoreGatedRoute`), and `/health` holds it around its node count and re-extract notice, so a slow request that outlives uvicorn's 1.5 s drain can no longer have the store closed under it; a request that arrives once the close has begun gets 503 and touches nothing. The gate is taken in the thread, not around the awaited handler, because uvicorn cancels the request task while the thread keeps running. fix(hooks): a daemon stop no longer uses an attempt when the consumer thread is still inside a backlog scan at the bound: measured on a real store copy (9.2K queued records), 2 of 3 mid-drain stops left no `interrupted.json` and the next start counted the in-flight jobs as failures. `DaemonConsumer.stop()` now names the `running/` jobs in the marker itself when the join times out (only while `worker.pid` names this process), the consumer skips the backlog scan while every slot is busy, and a stop seen mid-scan starts no new job. Tests: `tests/unit/test_consumer_shutdown_race.py` gains a 5-row route table and a 2-row stop-marker table. `tests/conftest.py` reopens the store gate after every test: a test that runs `viewer_server.main()` in-process left it closed, and since round 1 a later `test_health_during_slow_write` in the same xdist worker waited forever for a lifecycle phase that was refused.
- DAEMON-WARMUP-SIGNAL-RACE-FU-1 (2026-10-26): fix(daemon): warmup no longer publishes hooks or starts the hook queue consumer in the up-to-50 ms window after uvicorn has handled SIGTERM. The hook-sync, consumer-start and post-warm guards read the serving server's `should_exit` (set by the signal handler itself) as well as `_shutdown_requested` (relayed by the stop watcher). The signal handlers are unchanged and take no lock. `tests/unit/test_daemon_warmup_shutdown.py` gains three rows that release a parked warmup the instant `should_exit` is set (warming, inside hook sync, between hook sync and consumer start). DAEMON-WARMUP-WARNING-PROSE-FU-1: the four daemon hook warnings in `viewer_server.py` lose their semicolons.
- fix(daemon): SIGTERM during warmup no longer leaves hooks queue-only with no consumer, stale pid files and an unclosed store (DAEMON-WARMUP-SHUTDOWN-1). uvicorn re-raises SIGTERM after it drains, and with the default handler that killed the process before `_cleanup` ran, so on every SIGTERM the hook consumer was never stopped, the store never flushed or closed, and `daemon.pid` and `hook-queue/worker.pid` stayed behind; the old comment said atexit covered it. `_run_until_stopped` now installs a flag-only SIGTERM handler that uvicorn restores before its re-raise, runs cleanup, then re-raises SIGTERM with the previous handler so the exit status is unchanged. A shutdown flag, set by the stop watcher as soon as uvicorn sees SIGTERM or a stop request and by cleanup under `_hook_lifecycle_lock`, stops warmup from syncing hooks or starting the consumer once shutdown began (each skip logs a WARNING; hook writes wait on disk for the next start). Shutdown is bounded for launchd's 5 s SIGKILL window: in-flight requests get 1.5 s (`timeout_graceful_shutdown`; an SSE stream or a held hook call used to keep uvicorn waiting until SIGKILL) and the consumer 1.5 s (was 30 s; jobs still running stay in `running/` and are retried on the next start). Measured in the test: SIGTERM to exit 1.7 s with the store close stubbed. Test: `tests/unit/test_daemon_warmup_shutdown.py` runs the real `main()` and uvicorn in a child process with a request held open and sends SIGTERM at three warmup stages (warming, hooks synced, consumer running); on the old code all three rows were SIGKILLed after 5 s with the store unclosed and pid files left.

### HOOK-WRITE-QUEUE-SPILL-REQUEUE-FU-1 (2026-10-26)

- fix(hooks): `sm lifecycle requeue-hooks` now requeues an adopted spill job that reached `dead/` (HOOK-WRITE-QUEUE-SPILL-REQUEUE-FU-1). It skipped every file named `spill-*`, but `_adopt_one` names each valid adopted job `spill-<stamp>-<pid>.<phase>.json`, so those jobs stayed dead after the store came back, through both `--name` and bulk requeue (the command reported "skipped" and exited 0). Requeue now reads the record: a `spill-*` file with a `spill` field and a `payload_path` is a job and requeues like any other (attempt count and error reset, `queued_ns` kept so its order holds); a raw envelope dead-lettered as malformed is still skipped. Table test `tests/unit/test_capture_queue.py::test_requeue_adopted_spill_jobs_table`.

### HOOK-WRITE-QUEUE-REMOTE-EXIT-FU-1 (2026-10-26)

- fix(hooks): in remote mode the last observe, learn or distill job no longer waits forever when the remote drainer exits while the hook is still reading its payload (HOOK-WRITE-QUEUE-REMOTE-EXIT-FU-1). The hooks read `worker.pid` once, before stdin, and reused that value after publishing the job to decide whether to start a drainer. A drainer that cleared its pid, rescanned an empty queue and exited during that window left the new job queued with no drainer and no WARNING until another hook fired. The hooks now read and check `worker.pid` again after the publish (`live_consumer` in `smartmemory_app/hooks/{observe,learn,distill}.sh`), which closes the handshake: the drainer clears its pid before its last scan and the hook publishes before its last pid read, so one of the two always sees the other. The early read still gates the depth cap. Local mode (no `remote-drain` marker) does no second read and starts nothing. Test: `test_drainer_exit_during_stdin_starts_a_replacement` in `tests/unit/test_hook_write_queue.py` (a real `consume` loop as the drainer, one row per phase plus a still-alive drainer row and a local-mode row).

### DAEMON-9014-CRASH-DIAG-1 + CLI-TEST-HOST-ISOLATION-1 port to 2.0 (2026-10-26, HOOK-WRITE-QUEUE-1 daemon-exit diagnosis)

- `sm stop` (and anything else that stops the daemon or worker) no longer switches off a background job it did not install (DAEMON-9014-CRASH-DIAG-1). macOS keys background jobs by user, not by home folder, so a process running with a different HOME (a test suite, a second install) saw your real `ai.smartmemory.daemon` job and booted it out; that is how the local daemon died twice on 2026-10-22 during test runs. A job now counts as ours only when `launchctl print` shows it loaded from this HOME's `~/Library/LaunchAgents/<label>.plist` (symlinks resolved); any other job is left running with one WARNING naming both paths. Covers `stop_daemon`, the worker stop and the legacy-worker retirement. Table test `tests/unit/test_launchd_ownership.py`.
- CLI tests can no longer touch the machine owner's real daemon, HOME or launchd job (CLI-TEST-HOST-ISOLATION-1). Every test gets a port reserved in `~/.ports` as `cliTest` and exported as `SMARTMEMORY_DAEMON_PORT` (released on teardown, failure path too), scratch launchd job labels, a `launchctl` shim that fails any call naming a real label, a bind guard on 9014/9015, the setup module's import-time paths pointed at the isolated HOME, and every process a test starts stopped at teardown. `tests/unit/test_host_isolation.py` proves each site. `viewer_server.main()` with no port now serves the configured port (`SMARTMEMORY_DAEMON_PORT`, config, then 9014) instead of a hard-coded 9014. Cause found: `daemon.stop_daemon()` and `_stop_workers()` ask launchd about `ai.smartmemory.daemon`/`worker` whatever HOME is, so a test that ran them unmocked booted out the real job.

### HOOK-CAPTURE-NO-LLM-LANDED-TEST-FU-1 (2026-10-26)

- test(capture): `tests/integration/test_hook_capture_no_llm.py` passes 5/5 under the isolated no-credential, outbound-TCP-denied, ports-pinned-to-1 run (HOOK-CAPTURE-NO-LLM-LANDED-TEST-FU-1). The 2.0 fixture writes under the store's own minted Lite workspace instead of an unrelated synthetic one (writes no longer raise `EffectScopeMismatch`). Both lines pin every backend to loopback port 1 in the fixture, stop counting those refused connects as LLM calls (any other TCP connect still counts and fails the row), and drain through `worker_entry.run_configured_worker` so `stage:enrich` boxes finish. No production change.

### HOOK-CAPTURE-NO-LLM-1 (2026-10-25)

- fix(capture): transcript capture makes no LLM call by default. The capture worker builds its memory with core's costless profile (`PipelineConfig.costless()`: spaCy rules extraction and local enrichers, no deferred LLM extraction, supersession judge, grounding or evolution), whatever provider keys the environment holds, and session lessons (three Groq calls per session) are skipped with a WARNING and the existing `degradation` receipt. `SMARTMEMORY_CAPTURE_LLM=1` restores the previous Groq-pinned pipeline and lessons (rollback). `SMARTMEMORY_CAPTURE_OFFLINE=1` still selects Tier 1 and wins over the opt-in.
- Hook notes (observe, learn, distill) need no wrapper change: core selects the costless profile by origin (`hook:*`, `lifecycle:distill`; core `SMARTMEMORY_COSTLESS_ORIGINS`), so the daemon's hook consumer and any direct `storage.ingest` caller are covered.
- Requires core with `PipelineConfig.costless()` (HOOK-CAPTURE-NO-LLM-1 core). Caveat: transcript captures keep the user-import origin (`import:claude_code`), so an explicit user-run `smartmemory reextract` in LLM mode can still send them to the LLM (core drops only hook and distill origins there); that is a user command, not background ingestion. Fix round 1: the e2e `lite_env` fixture sets `SMARTMEMORY_NO_UPDATE_CHECK=1`, so core's update check cannot dial out through a system proxy and trip the connect trap.
- Tests: `tests/integration/test_hook_capture_no_llm.py` (one table: hook observe, learn, distill, transcript capture, user-source control) asserts no LLM entry point or outbound connect is reached with a Groq key present and that rules extraction still wrote entities. Tests that drive the LLM lesson path now set `SMARTMEMORY_CAPTURE_LLM=1`.

### U20.S1-HOOK-STORE-WORKSPACE-REFUSAL-1 (2026-10-25)

- test(hooks): `tests/integration/test_hook_write_route_lite.py` golden rows (observe, learn and distill land a project-tagged memory through the route on a real Lite store) lose their strict xfail: core 2.0 ruling (a), `workspace_bind.lite_owner_scope`, lands them.
- fix(hooks): `POST /lifecycle/observe`, `/learn` and `/distill` no longer answer `{"status": "ok"}` when the write was lost. The route runs the phase strict; a failed store answers 500 with `{"status": "error", "phase", "lost", "error"}` and logs one WARNING `Lifecycle <phase> write failed; <what> not stored: <error>` (the CLI caller then runs the phase in-process, as for any daemon failure). Test: `tests/integration/test_hook_write_route_lite.py` (real Lite store; the three "a hook write lands a memory" golden rows are strict xfail on core 2.0 until the U20.S1 scope-bind producer fix).

### DAEMON-HEALTH-STARVE-1 (2026-10-25)

- fix(daemon): the `/lifecycle/*` routes (orient, recall, observe, distill, learn, persist) were `async def` handlers that ran synchronous store, embedding and write-lock work on the event loop, so one hook call stalled every request and `/health` never answered while hooks were flowing. They now run on a bounded `smartmemory-lifecycle` thread pool (`SMARTMEMORY_LIFECYCLE_THREADS`, default 4), separate from the threadpool that serves `/health` and the viewer. `/lifecycle/status` is a sync route.
- fix(daemon): `/health` counted memories by serializing the whole graph under the viewer lock on every probe. The Lite backend now answers with one `SELECT COUNT(*)`; other backends keep the old path.

### HOOK-WRITE-QUEUE-1 (2026-10-25)

- fix(hooks): the consumer reads and removes only its own queue's payload files. A record names its payload and state snapshot by absolute path, so a queue copied to another data dir (a test harness backlog snapshot) used to read the original's files and then delete them. Every producer writes those files next to the record in `<queue>/payloads/`, so `hook_jobs` now resolves both by name there and logs one WARNING per queue when a record named a path outside it. Found when the r2 load harness deleted 424 live payload files (restored). Test: `copied_queue_never_touches_the_original` row in `tests/unit/test_hook_consumer.py`.
- fix(hooks): no queue file can stop the one consumer (review r1 M2). A spill file with a pattern-valid but impossible timestamp, or any other unexpected per-file error, moves to `hook-queue/dead/spill-<name>` with a WARNING. A claim, requeue, dead-letter or completion write that fails (full disk, permissions) is retried after a backoff (0.5 s doubling to 30 s) and, after `SMARTMEMORY_HOOK_MAX_ATTEMPTS`, the record moves to `dead/` by rename (no new bytes needed); a completed job whose record cannot be removed is left for the next start (a duplicate, never a loss). A failing consumer pass logs and backs off instead of ending the thread, and the daemon's consumer thread restarts `consume` if it ever returns on an error. Persistent spill failures warn once per file and back the adoption pass off (1 s doubling to 60 s) instead of a WARNING every 0.1 s (L1). The hook's shed WARNING names the session and tool that were lost (L4). The hooks count `worker.pid` as a live consumer only when `ps` shows it is the daemon (`smartmemory_app.viewer_server`) or the remote drainer (`lifecycle drain-hooks`), so a pid reused after a crash does not switch the depth cap off (L6). `setup` publishes hook files by temp file + rename, so a hook running at the swap never reads a mix of old and new bytes (L5). Tests: `tests/unit/test_hook_consumer.py` rows for a poison spill name, ENOSPC on claim, EACCES on requeue, a once-only spill WARNING, the shed names and a foreign `worker.pid`.
- fix(hooks): the consumer adopts HOOK-SPILL-INTERIM-1's spill folder (`<data dir>/hook-spill/<ts>-<pid>-<phase>.json`, written by the interim pre-queue hooks when they shed) into the hook queue before each scan, in arrival order, so spilled observe, learn and distill payloads get the same per-session order, retry and dead-letter policy. A spill file is deleted only after its payload and queue record are fsynced; the job is named after the file, so a crash in between never queues it twice. `.tmp-*` files are ignored; a malformed file moves to `hook-queue/dead/spill-<name>` with a WARNING. Spilled distills carry no prompt snapshot and are stored unpaired, with a WARNING. The daemon's start line now reports the spill count.
- fix(hooks): daemon-sole-consumer. In local mode the daemon is the one reader of the hook queue: a consumer thread (`smartmemory_app/hook_consumer.py`, started after a warm backend, stopped before the store closes) runs each queued observe, learn or distill job in-process against the daemon's store, in order per session, with the existing retry and dead-letter policy. The hooks start no process: the drainer spawn, its HTTP hop to `/lifecycle/*` and its in-process fallback writer (`capture_worker.run_hooks`/`run_hook_job`) are gone. While the daemon is down jobs wait on disk and drain on the next start (orphans in `running/` are retried); past `SMARTMEMORY_HOOK_QUEUE_MAX` queued jobs (default 2000; counted, in C collation, only while no live consumer holds `worker.pid`, so the steady state pays nothing) a hook sheds its payload with a `WARNING: lifecycle <phase> payload shed` line in `hooks.log`. Remote mode has no daemon, so `setup` remote mode writes a `remote-drain` marker and only then do the hooks start `smartmemory lifecycle drain-hooks`, which now refuses (WARNING) in local mode
- fix(hooks): the observe, learn and distill hooks no longer write to the store or drop payloads under load. Each hook appends its payload to a durable queue under `<data dir>/hook-queue/` in pure bash (no Python start) and returns; HOOK-LOAD-CAP-1's "skipped under load" branches are deleted. A drainer, `smartmemory lifecycle drain-hooks`, started by the hook only when no drainer is alive, writes the jobs through the same `MemoryLifecycle` phases, oldest first and one at a time per session, at most `SMARTMEMORY_HOOK_DRAIN_MAX` (default 3) at once. A failed write is retried with backoff and moved to `hook-queue/dead/` after `SMARTMEMORY_HOOK_MAX_ATTEMPTS` (default 5), with a WARNING. Remote mode goes through the same queue. The one loss path left is a queue that cannot be written, logged as `payload lost` in `hooks.log`. Distill pairs the response with the prompt snapshotted when the Stop hook fired. Persist (SessionEnd) is unchanged.

### HOOK-WRITE-QUEUE-FU-1 (2026-10-25)

- fix(hooks): a full disk no longer dead-letters healthy hook jobs. The consumer claims a job with a bare rename into `running/` (it used to rewrite the record first, so on ENOSPC each session's head job went to `dead/` after `SMARTMEMORY_HOOK_MAX_ATTEMPTS` failed claims). The rename writes no bytes, so the job stays queued, and the file's mtime survives so it keeps its place in its session's order. The `claimed_at` field is gone (nothing read it). Tests: `enospc_on_every_queue_write_leaves_claims_working` and `claim_keeps_the_record_bytes_and_mtime` rows in `tests/unit/test_hook_consumer.py` replace the old dead-letter-on-claim row.
- feat(cli): `sm lifecycle requeue-hooks [--name <file>]... [--dry-run] [--dir <queue>]` moves dead-lettered observe, learn and distill hook jobs from `hook-queue/dead/` back to `queue/` with a fresh attempt count, keeping `queued_ns`. Malformed `spill-*` files, records with no known phase or a missing payload, and names already queued are skipped with a reason; an unknown `--name` exits 1. Test: `test_requeue_hooks_table` in `tests/unit/test_capture_queue.py`.

### WRAPPER-WORKER-V2-FALLBACK-1 (2026-10-25)

- fix(worker): `worker_entry.run_configured_worker` passes `v2=` only when core's `run_worker` accepts it. An older core (no `lite_handlers`, no `v2` parameter) now logs the WARNING and runs the legacy drain instead of raising `TypeError`.

### U20.S1-HANDLERS (2026-10-24)

- feat(worker): `worker_entry.run_configured_worker` passes core's switch-gated `v2=lite_v2_worker_config(data_dir)`; None (the legacy drain, unchanged) while `sm2_save_local_admission` is OFF, the default. An older core without `lite_handlers` logs one WARNING and runs the legacy drain.

### REL-CALLERS-PROMPTS-1 (2026-10-24)

- The `/ingest` and `/remember` skills (`smartmemory_app/skills/ingest.md`, `remember.md`) call `memory_save` instead of the deleted `memory_ingest` and `memory_add` MCP tools (G140, no aliases), and report the save status (`saved`, `queued`, `held`, `failed`) with the item id. Prose only, no code changed.

### SETUP-FRESH-CI-1 (2026-10-17)

- New fresh-install check for the `sm` CLI: `scripts/fresh_install_check.py` and `.github/workflows/fresh-install.yml`. It installs the built wheel into a clean venv with a throwaway `HOME`, then checks a keyless local `sm setup`, `sm doctor`, an add, search and decision round trip, the inherited `SMARTMEMORY_*` detection with poisoned variables, and a remote-mode setup against a local fake server. It prints `STEP <name> PASS|FAIL|SKIP` lines and needs no secrets. No product code changed.

## 1.5.27 — 2026-10-17

- Setup and first-run hardening from the 2026-10-17 demo feedback: pins `smartmemory-core==1.5.27` and `smartmemory-mcp==1.5.23`. Ships the `Unreleased` sections below: CLI-TENANT-1, LOCAL-DAEMON-1, TOUR-ASK-502-1, CLI-WORKSPACE-UX-1, CLI-ENV-DETECT-1, SETUP-DOCTOR-1, CLI-SETUP-UX-1, BENEFITS-WORDING-1, plus the SM-SETUP-BATCH-1 integration glue. The per-unit headings keep their `Unreleased` labels.

## Unreleased - CLI-TENANT-1 (2026-10-17)

- `sm ask` in local mode with no LLM provider key now exits non-zero before any request. The error names the missing key (the configured provider's, or all supported ones), and gives `export <KEY>=...`, `sm restart` and `sm setup`. The keyless excerpt listing moved behind `sm ask --extractive`. A daemon that answers extractively although this shell has a key now says it needs `sm restart`.
- Remote mode: when the workspace the CLI sends (`SMARTMEMORY_TEAM_ID` or the config file) differs from the default workspace of the API key's login (`/auth/me`), every remote command prints, once per run on stderr, both workspaces, the tenant, the API URL and how to switch. An empty search, list or recall result from such a workspace says so instead of reading as "no memories". New module `smartmemory_app/preflight.py`.

## Unreleased - LOCAL-DAEMON-1 (2026-10-17)

- `sm setup` (local) now names what is wrong when the daemon does not come up, with a one-line fix per case: port taken by another program (names the process), daemon crashed and left `daemon.pid`, auto-start pointing at a Python that no longer exists, required model files missing, daemon up but degraded. The raw reason stays in a `Detail:` line. On success it prints `daemon running at http://127.0.0.1:<port>`.
- `daemon.daemon_health()` returns `(ok, detail, fix)` for the doctor's daemon row. It is read-only and never raises. The logic lives in the new `daemon_diagnosis.py`.
- `start_daemon` removes a leftover `daemon.pid` (or `.daemon.stop-<pid>` request) only when its recorded process is gone by exact pid check, and never while a daemon is answering.

## Unreleased - TOUR-ASK-502-1 (2026-10-17)

- `sm ask` (and the tour's ask step) no longer fails with "502 Bad Gateway" when the Claude Agent SDK happens to be installed in the same Python. A `groq` or `openai` setup now pins its own default model (`openai/gpt-oss-120b`, `OPENAI_MODEL` or `gpt-4o-mini`), as `gemini` already did, so core no longer prefers the `claude` CLI, which exits 1 when it is not logged in. `claude-agent` and `none` keep core's default. An explicit `SMARTMEMORY_LLM_MODEL` still wins.
- `sm worker` and `smartmemory worker --loop` now apply the configured provider before running, like the daemon and the background worker already did, so Tier-2 extraction no longer goes to the Claude Agent SDK on a `groq` or `openai` setup. Both worker entry points share one helper (`worker_entry.run_configured_worker`), and a table test covers every entry point that can reach an LLM. `anthropic` and `deepseek` setups stay on core's default: core has no non-SDK route to pin them to.
- Ask failures show their reason. The daemon's 502 detail names the error type. The tour shows `HTTP <code>: <server detail>` instead of the bare status line. The hosted `sm ask` shows the service's 502 detail (503 stays hidden, it names server settings).
- Handled failures are reported (when crash reports are on): every daemon route that answers 5xx through `HTTPException` (except the by-design 501 of remote-proxy mode), the tour's ask failure and empty answer, the tour's "seeded content not returned" degradation, and a tour run that fails.

## Unreleased - CLI-WORKSPACE-UX-1 (2026-10-17)

- New `sm workspace list | create <name> | switch <name|id|auto> | current`, in both modes. Remote: the list is what the web console can select (`/memory/workspaces` minus system teams, names from `/memory/teams`), `switch` writes `team_id`, `current` compares the CLI's workspace with the console's default for the same account (`/auth/me` `default_team_id`, the field the console opens) and says MISMATCH loudly, `--check` exits 1. Local: named workspaces and a pin in `workspaces.toml` next to the config, `sm add` and `sm recall` send the pin, `list` counts the tags in the store.
- Every memory command prints one header line on stderr naming mode, tenant and workspace (`[smartmemory] mode=... tenant=... workspace=...`). Hidden with `sm --quiet`, any `--json` flag, or `SMARTMEMORY_QUIET=1`. Never on `lifecycle` hooks. The remote tenant comes from the identity cached by the last `sm workspace` command, so the header makes no network call.
- Fix (CLI-WORKSPACE-UX-1 round 1): the header and the request now name the same workspace. With `SMARTMEMORY_WORKSPACE_ID` set, `sm add` and `sm recall` used to print that workspace but send none (the daemon is another process and cannot see the CLI's env), so the item was stored untagged. The env var, then the pin, is now sent explicitly on both the daemon path and the daemon-down fallback, from the same resolver the header reads. A workspace you pass as a property can no longer override it: `sm add --prop workspace_id=...` is refused ("Reserved properties"), and the daemon and fallback stamp the request's workspace over any caller property.

- `sm doctor` now starts with a pass/warn/fail table of what the CLI will actually use: Python and venv, config path, active mode and which source won, inherited `SMARTMEMORY_*` variables (names and the file that exports them, never values), API key presence, tenant and workspace against the web identity (`/auth/me`), LLM provider and key presence, daemon or service health and version, and graph backend. Each failing row has a one-line fix. `--json` prints the same rows. Exit code 1 on any FAIL. A check that cannot run is a WARN naming what was not checked.
- `sm setup` runs the table at the end, and `sm tour` runs it before starting. A FAIL stops the tour and prints the fix.
- The table reads this project's `sm env` decisions (CLI-ENV-DETECT-1) and never prints a `SMARTMEMORY_*` value: an env-set URL, workspace, team id or data dir shows as `$NAME`. A stopped daemon is a warning, since commands fall back to running in-process.
- Review fixes (fix round 1): every row asks the resolver the CLI uses instead of re-deriving it. The hosted workspace is the one `RemoteMemory` sends (new `remote_backend.resolve_team_id`), with a WARN when the web console opens a different one. The local workspace names the source `recall_format.derive_workspace_id` used. The LLM row reports provider, model and endpoint from core `provider_pin`, `get_default_model`, the extractor's base URL and `call_llm`'s route order after the CLI's own config translation (`config.set_derived_env`, `storage.pin_llm_model`), probes that endpoint, and covers `LLM_PROVIDER`, `LLM_API_KEY`, `OPENAI_MODEL` and Bedrock. An unreadable keychain or shell profile (new `setup._profile_lookup`) turns the LLM row into a WARN that names it. Every line `sm doctor` prints, text and `--json`, including the older Network and Storage lines, shows env-sourced values as `$NAME (env)`.
- Review fixes (fix round 2): the LLM row now reports what core would actually send. Doctor runs core's real extractor up to the network call, with sockets blocked and the send functions stubbed, and reads the model, endpoint and refusal from that run. A model rewritten by core shows as `sent as <model>`, an `EXTRACTOR_*` env override is named as `$NAME`, and a core config file core could not read turns the row into a WARN. Log records printed while doctor runs (core warnings included) mask env values the same way the table does.
- Tests: an autouse fixture restores `SMARTMEMORY_*` variables and the `env_pin` state after every test, because applied project overrides leaked into later tests.

## Unreleased - CLI-ENV-DETECT-1 (2026-10-17)

- New `smartmemory_app.env_sources.inherited_vars()`: lists every `SMARTMEMORY_*` variable the process inherited, with the shell startup file and line that exported it (`~/.zshrc:42`) and this project's saved decision (`use`, `ignore`, `override` or undecided). Names only, it never reads or returns a value. `SMARTMEMORY_ENV_CHECK=0` is a meta switch and is never listed.
- Every `smartmemory` command (setup and tour included) now checks for undecided inherited `SMARTMEMORY_*` variables. In a terminal it lists them (names and where they were exported, never values) and asks once: use them, ignore them for this project, or override them with a project value. Without a terminal it prints one WARNING line naming them and `smartmemory env`, and never prompts. Hook commands (`lifecycle`) and `doctor` stay quiet. `SMARTMEMORY_ENV_CHECK=0` turns the check off.
- Decisions are stored per project (git top level, else the directory) in the existing user config file under `[projects."<path>"]`. `ignore` removes the variable for that project, `override` replaces it, `use` keeps it. New precedence: flag > project decision > environment > config file > default. Secrets (`*_KEY`, `*_TOKEN`) can be used or ignored but not stored as an override.
- New `smartmemory env` (also `smartmemory config env`): list, `set NAME use|ignore|override [VALUE]`, `reset [NAME]`.
- Fix: `smartmemory config KEY VALUE` and remote login no longer write inherited `SMARTMEMORY_*` values (team id, data dir, API URL) into the config file. `load_config(apply_env=False)` reads the file layer for read-change-save.
- Fix: a config file that exists but cannot be read or parsed is never rewritten. `smartmemory env set/reset`, the first-command prompt, `save_config()` (setup, `config`, login) and first-run auto-config now stop with one line naming the file (`Cannot update <path>: it is not valid TOML (line N). Fix or remove it, then retry.`) instead of replacing the user's settings with defaults. The first-command prompt prints that line and your command still runs. Read-only paths are unchanged.
- Fix: a config file with invalid UTF-8 no longer crashes read-only commands. They read it as empty and log one WARNING naming the file, the same arm as malformed TOML.

## Unreleased - SETUP-DOCTOR-1 (2026-10-17)

- `sm doctor` now starts with a pass/warn/fail table of what the CLI will actually use: Python and venv, config path, active mode and which source won, inherited `SMARTMEMORY_*` variables (names and the file that exports them, never values), API key presence, tenant and workspace against the web identity (`/auth/me`), LLM provider and key presence, daemon or service health and version, and graph backend. Each failing row has a one-line fix. `--json` prints the same rows. Exit code 1 on any FAIL. A check that cannot run is a WARN naming what was not checked.
- `sm setup` runs the table at the end, and `sm tour` runs it before starting. A FAIL stops the tour and prints the fix.

## Unreleased - CLI-SETUP-UX-1 (2026-10-17)

- `smartmemory setup` numbers every step: `[n/N] <step> ... (about <estimate>)` before it runs, then a done or failed line with the elapsed time. Estimates are constants in the new `smartmemory_app/setup_steps.py`, measured in a clean venv with an empty cache (spaCy model about 70 s, embedding model about 2 min, helper models about 5 min). Output is plain lines, so pipes and CI logs get no control characters. On a terminal long steps also show the existing spinner.
- Model downloads in setup now print throttled newline progress lines (`Downloading the local AI model: 12.0 MB of 86.2 MB (14%)`) by reusing `hf_progress.py`. The helper models (search and reranker) are now their own counted step instead of being folded into the embedding step.
- The setup TUI progress screen shows the same counter, estimate and elapsed time per row. The tour title shows `[n/7]`.
- The local model choice (ollama, lmstudio) now defaults to none, in the text prompt and as the first, highlighted row of the TUI model screen, with one line saying what you lose and how to add one later (`sm config llm_model <model name>`). The TUI Skip button is gone because none replaces it.
- Fix: `hf_progress._DownloadBar` lacked the tqdm display methods the Hugging Face Xet reporter calls, which printed an `AttributeError` traceback during downloads.
- Setup choice lists and TUI choice text no longer use em dashes or semicolons.
- Fix round 1: setup prints an estimate only for steps that were measured. The daemon start and daemon stop steps have none (they touch launchd and cannot be timed safely), so they show only the real elapsed time. Choosing no model for ollama or lmstudio now really turns LLM extraction off (`llm_key_present()` is false and no placeholder key is set), and setup ends with the line that says how to turn it on. Setup asks again whether the daemon is running after the downloads, as it did before this unit. `sm tour`, `sm explore`, the setup TUI and the `sm import` progress bar start only when stdin, stdout and stderr are all terminals (`console.interactive_terminal`), so piped output holds no escape codes. Repaired the TUI warm-up message so it keeps the tested wording without a semicolon.
- Fix round 2: a local provider (ollama, lmstudio) with no model now turns LLM extraction off everywhere, not only in the status flag. Setup sets no `OPENAI_BASE_URL` for it (that endpoint alone let core queue extraction on every save), and `apply_runtime_config` holds core's LLM route check off while no model is chosen, so a key already in your environment or the Claude Agent SDK cannot switch it back on. The daemon start line and the save warning say `LLM extraction is off until you pick a model`. `sm worker` now applies the saved provider settings first, as the background worker does, and an ollama `llm_base_url` override applies once a model is set.

## Unreleased - BENEFITS-WORDING-1 (2026-10-15)

- README: removed benefit claims our measurements do not support. The "under 200ms" CLI line now gives the measured warm `sm add` time (about 210 ms, version 1.5.16) and first-add time (about 0.9 s). The "2 minutes" setup time, the "4ms", "740ms" and "96.9% entity F1" figures, "no more re-explaining" and "the same mistake isn't repeated" are gone or reworded as aims. This text reaches PyPI only at the next release.

## Unreleased - HUB-DOCS-LINK-1 (2026-10-15)

- README: Links section now lists the SmartMemory Hub (plugin and integration marketplace, hub.smartmemory.ai).

## 1.5.26 — 2026-10-09

- Hotfix: pins `smartmemory-core==1.5.26`, which bounds `tree-sitter<0.26`. With tree-sitter 0.26.0, `smartmemory code bundle` on a real TypeScript tree crashed with a segfault (exit 139).

## Unreleased - CODE-BUNDLE-CLI-1 (2026-10-08)

- Repo references renamed `smartmemory-*` -> `smart-memory-*` in docs, scripts and config (INFRA-REPO-RENAME-1).

- Final bundle fixes: partial snapshots preserve collection failure paths with colon characters, Unicode names and filename-less errors. CLI regression coverage uses real audit-hook traversal failures. Core validates every declared nested record and reports unavailable membership inputs explicitly.
- `smartmemory code bundle <path> --repo <id> --out <file> [--language ...] [--exclude ...] [--allow-partial] [--fields full|minimal]`: writes a store-free local snapshot of a checkout (entities with `item_id`, relations with `edge_state`, source identity, `resolution_dependencies`, completeness). It never calls `get_memory`, opens no graph, vector store or embedder, and uploads nothing. The file is written atomically (temp file in the `--out` directory, then `os.replace`); a refusal creates no file and leaves an existing `--out` untouched. `--allow-partial` keeps good files when others fail to parse (exit 0, `complete=false`, a `[code:bundle] warning: incomplete` line naming the failed paths). `--out` inside the checkout and not git-ignored prints a warning. There is no `--commit-hash` flag.
- Fix round 1: the snapshot write refuses NaN/Infinity (`allow_nan=False`, prior file kept, no temp left), and `--fields` help names the forge/stratum code-graph consumer (STRAT-CODEGRAPH-1: minimal by default there, full opt-in). The CLI default stays `full`.
- The wrapper now depends on `smartmemory-core[typescript]` (tree-sitter TS/JS grammars moved to that extra).

## Unreleased - CODE-INDEXER-HARDEN-1 U5 (2026-10-08)

- CODE-INDEXER-HARDEN-1 (F25, F28, F29): hosted `prepare_code_index` stamps dirty-aware provenance (`<HEAD>-dirty-<fingerprint>` for uncommitted or untracked changes, with a WARNING) and sends `repo_identity` so the service refuses a repo name owned by a different checkout. The hosted CLI reports the server's `embeddings_generated` instead of always warning that hosted indexes have no embeddings (the warning stays for older services).

## Unreleased - CODE-CALLSITE-COVERAGE-1 build

- Point user-facing links at https://www.smartmemory.ai (the web app had no app.smartmemory.ai DNS until 2026-10-08, and it now only redirects to www). Filed as PLAT-APP-SUBDOMAIN-1.
- CORE-HOLD-RECEIPT-1 round 2: Preserve held receipts through local and hosted storage, daemon, CLI, lifecycle and tour consumers. Describe searches against existing memory when no tour seed was stored.

- Show shared source call-site extraction status and resolved CALLS edges separately in local and hosted code-index summaries.

- Integrated FRAMEWORK with Wave B, preserving scoped liveness and diagnostic evidence.

Notable, **user-facing** changes to the `smartmemory` distribution package. The wrapper is thin — it pins an exact `smartmemory-core` version and the two move in lockstep — so entries here highlight what a release *delivers* (features, fixes, security), not routine version-pin bumps. For full internal detail, see the `CHANGELOG.md` shipped in the `smartmemory-core` distribution. Loosely follows [Keep a Changelog](https://keepachangelog.com); not every patch release gets an entry.

## [Unreleased]

- U5.21: local `search`, filtered widening, the gate-ON wildcard window and `recall` (query, workspace and orient arms) no longer read a short page as exhaustion while core reports its answer partial (`read_gate_scan_budget_spent`, `complete` False and the other coverage entries). Gate ON they keep widening, stop at the gate's scan budget (a fetch of 1000 or more) with one named WARNING, and the wildcard cap refusal says whether core was partial or full. A core refusal that the two optional-context degrade sites (snapshot frame, decision lookup) swallow is recorded in the trace without a second WARNING (`record_hook_degradation(..., logged=)`). Gate OFF output, calls and logs are the legacy ones, and the signatures are unchanged. Not wrapped here: the raw `_graph.search_nodes` decision lookup (C-38) and the intake port binding (the flip unit).
- U5.21 fix round 1: gate ON, every fetch of `search` (filtered loop) and `recall` (first fetch and every doubling) is clamped BEFORE the call to `min(lane_gate.DEFAULT_MAX_SCAN, read_port.MAX_PAGE_LIMIT)` (200), the widest a gated search can ask core, so recall no longer reaches 1536 and the gate-ON stop is said as a partial answer, not "candidates exhausted"; rows a callee returns past the fetch are not used. `search` and `recall` return the same plain list/str with core's partial reasons on it (`PartialRows`/`PartialText`, plus `fetch_bound` when the wrapper's own bound stopped the loop). `record_hook_degradation` loses its `logged` parameter: "already logged" is read from the exception through `smartmemory.refusal_logging`, gate ON only, so the gate OFF log is the base's. A caller-supplied count in a WARNING is rendered bounded.
- U5.21 fix round 2: gate ON, every fetch site (`search` unfiltered and per group, filtered widening, `recall` query/workspace/orient, the orient snapshot lookup) uses core's `gated_fetch_bound`, so no row a callee returns past the fetch is matched or shown. A recall stop at the 200 bound carries `fetch_bound` whether core said partial or the page came back full (it was a plain `''`). A failed snapshot or decision lookup is carried on the answer as `snapshot_lookup_failed` / `decision_lookup_failed` instead of reading as none. OFF unchanged.

- U5.21 landing (tests only): the gate-ON refusal row of `tests/unit/test_gated_output_wrapper.py` opens the Lite intake door (`sm2_intake`) before searching, because core U20.BIND1 binds an empty intake port on every host and a bare host no longer refuses. The row still asserts the typed refusal and one origin WARNING.

- FORWARD-MERGE-2: main forward-merged into 2.0 again (wrapper release 1.5.27: sm doctor, numbered setup steps, inherited `SMARTMEMORY_*` detection, `sm workspace`, the configured-provider fixes for `ask` and the worker). No conflicts. `pyproject.toml` takes main's 1.5.27 and its core pin, because 2.0's version and pins have only ever been main's release commits. Test-only follow-up: main's new `[smartmemory] mode=... workspace=...` stderr header broke CLI output asserts, so six tests (`test_cli`, `test_why`, `test_tui`, `test_save_return_compat`, `test_work_graph_wrapper`) set `SMARTMEMORY_QUIET=1` as main's own tests do, expect `properties=None` for an empty scope, and fake a terminal for the `explore` refusal check. No product code changed.
- FORWARD-MERGE-1: main forward-merged into 2.0 (wrapper releases 1.5.17 to 1.5.26 and the Windows, remote-parity, diagnostics, code-bundle and hold-receipt fixes). Held ingest results and the hidden hook origins on wildcard search keep working beside the U4.32 and U5.22 receipt arms.

- U4.32: prepares the local save return and work status for core's save receipt, with no switch and no change while core still returns an id string. `smartmemory_app.save_return` reads either arm: a legacy id string or dict (queued, held) is returned as it is and nothing is imported, and a receipt (object or wire dict) reads as its subject id, never `str(receipt)`, with a G98 "Saved, extraction skipped" summary. An admission rejection is a typed `SaveRejected` and an unreadable or non-save receipt a typed `SaveReturnRefused`, each with one WARNING, never an id. `storage.ingest` uses it on both return paths, so an async receipt becomes the usual `{item_id, queued, run_id}` dict plus a `receipt` key. `work_graph.get_local_status`, `open_local_status`, `project_local_status` and `local_status_capability` project core's live, retained and expired run status through the actor and workspace gate: an unseen, foreign, hidden or revoked run reads as one uniform unknown, and a core without the status adapter is `LocalStatusUnavailable` (explicit, with one WARNING), never an empty status. Nothing under the legacy save path calls them yet (the adapters lane wires the callers). First status read installs core's additive `wg_*` tables on the local `memory.db`.

- U4.32 review fix: `open_local_status` now checks every core module it opens with (`OPEN_DEPENDENCIES`) at one boundary and refuses a missing one with `LocalStatusUnavailable` and one WARNING, instead of letting a raw `ModuleNotFoundError` out. `resolve_save_return` refuses (typed, one WARNING) a receipt when the core cannot read status badges. No change on the legacy path or while core returns an id string.

- U4.32 review fix 2: every name the local status path takes from core (16 module and symbol pairs in `work_graph.STATUS_IMPORTS`, from which `OPEN_DEPENDENCIES` is now derived) is resolved at one boundary before use. A core module that imports but lacks a needed class or function now refuses with `LocalStatusUnavailable` and one WARNING naming the module, the symbol and what is lost, instead of a raw `ImportError`, and an ImportError raised inside a module's own import is named with its cause rather than reported as an absent module. `get_work_graph` resolves its two names the same way and raises `WorkGraphUnavailable`, an `ImportError`, so its existing callers see the same exception family. The `storage.ingest` consumer census test now resolves every import form and fails on a reference it cannot follow.

- U4.32 review fix 3 (tests only, rule 4): the `storage.ingest` consumer census fails closed on the dynamic-import and module-registry surface. `importlib.import_module` and `__import__` are followed under any alias (`from x import y as z`, `import x as y`, `builtins`, as a call receiver) when the module name is a constant, and anything else raises AssertionError naming file:line: a name read at run time, a relative name, the loader or `importlib`/`sys`/`builtins` held as a value, `sys.modules` used other than by a constant key, a star import of those modules, or the module name as a bare string. The two packages keep copies of the helper, pinned to one hash.

- U5.22: local `sm search "*"` (wildcard) now lists intake-only items and every wildcard row carries the extraction `read_status` when `sm2_search_visibility_gate` is ON (the snapshot scan could not see receipts). Recall lines add an `[extraction_pending]`-style tag only when a status is present. Gate OFF is unchanged. On the gate-ON arm the wildcard now returns core's own merged enumeration (membership, edit and tombstone precedence, origin filters, status on every row) instead of merging onto the graph snapshot; a full window at the cap, or a core page larger than the window it was asked for, raises `WildcardEnumerationOverflowError` with one WARNING. Every storage site that serializes a core result (search, grouped search, filtered search, get, wildcard) checks the carrier first (`_result_dict`), so a malformed `read_status` is a typed `MalformedReadStatusError` with one WARNING, never `to_dict()`'s raw error or a copied stateless dict.

- U4.42: adds `smartmemory_app.save_caller`, the caller seam for a receipt save, with no switch and no change to `add` or `ingest`. `build_save_call` makes one frozen `SaveCall` carrying a stable retry key (a supplied `idempotency_key` verbatim, or a minted `<seam>:<uuid4 hex>` when none is given), an explicit `wait` (`stored`, `extracted` or omitted) and the caller's own producer namespace; submitting the same call again sends the same key and request. `storage.save(call)` submits it to `SmartMemory.save` under the same write lock as `ingest` and reads the receipt with `save_return`. It never falls back to `add` or `ingest`: core's `SaveNotEnabled`, `SaveRequestError` and `IncompatibleWait` pass through unchanged, a memory with no `save`, a legacy return from `save`, and hosted mode are each one typed `SaveCallRefused` with one WARNING. Nothing on the legacy save path imports it, so there is no change until core's common-receipt facade is enabled.

- U4.42 review fix (round 2): one rule tells a receipt from a legacy save return (`is_receipt_arm`): a subclass of core's `Receipt`/`ReceiptRefusal`, a lookalike named like one, a receipt from another copy of core and any mapping with `schema_version` are all the receipt arm, and every receipt is read through core's own `Receipt.to_dict` and `decode_receipt`, so a subclass can no longer hide a rejected admission. The retry snapshot is now rebuilt from plain JSON types without calling any copy hook and stored as bytes, so a caller's object can never change what a retry sends; a value that is not plain JSON, an unreadable snapshot and a `submit` of something that is not a `SaveCall` are each one typed, logged refusal. In a core without `smartmemory.refusal_logging` the cause of a refusal is chained, never rendered, so it cannot raise.
- U4.42 review fix: a `SaveCall` now holds a deep, read-only snapshot of its request (`FrozenRequest`), so changing the dict you passed in, or anything nested in it, can no longer change what a retry sends under the same key. Every new failure on the save path is a typed `SaveCallRefused` with one WARNING and a refusal mark, never a raw exception or a second WARNING: the memory cannot be opened, the data directory cannot be resolved, the write lock cannot be taken or is held, and a request that cannot be copied. `save_return` marks its own refusals and admission rejections (`SaveReturnRefused`, `SaveRejected`) at the producer, so the boundary above does not log them again. Still dormant: no switch, no change while core returns an id string.
- U4.42 review fix (round 3): a receipt is now recognised before its shape, so a receipt that also is a string or a mapping is still refused when it carries a rejected admission, and `resolve_save_return` always reads a receipt through core's own export. A failure that core already logged is refused without a second WARNING, and a return that cannot be inspected is refused on the first failure. No switch; nothing reaches this path until core returns a receipt.

## [1.5.25] - 2026-10-09

- Pins `smartmemory-core==1.5.25` (now with the `[typescript]` extra, which carries the tree-sitter TS/JS grammars).
- New `smartmemory code bundle <path> --repo <id> --out <file>` writes a store-free local snapshot of a checkout (entities, relations, source identity, `resolution_dependencies`, completeness) without opening a memory store or uploading anything. `--fields minimal` gives the lean form, `--allow-partial` keeps good files when others fail to parse (CODE-BUNDLE-CLI-1).
- Pins `smartmemory-mcp==1.5.21` (latest published MCP).
- Core mirror rework (DIST-LITE-SYNC-1) ships closed by default: nothing changes for ordinary add, ingest or export.

## [1.5.24] - 2026-10-08

- Pins `smartmemory-core==1.5.24`: Lite (SQLite) memory never opens a Redis socket unless one is configured (CORE-LITE-REDIS-PROBE-1), plus the 2026-10-08 core security hotfixes.

- `sm code index` now indexes every supported language present (Python and TypeScript/JavaScript, including `.mjs/.cjs/.mts/.cts`) by default instead of Python only; `--language` (alias `--languages`) narrows it (CODE-INDEXER-HARDEN-1 F32). Under the default, a TS/JS file that fails to extract is skipped with a warning instead of refusing the whole index; passing `--language typescript` makes such a failure refuse.
- Prompt and session-start hooks no longer time out in Claude Code. `smartmemory lifecycle recall` and `lifecycle orient` now stop after 8 seconds (set `SMARTMEMORY_HOOK_DEADLINE` to change it). On overrun they add no context for that prompt, write a warning with per-phase timings to `hooks.log`, send at most one anonymous `hook_deadline_exceeded` event a day (no prompt text, no paths, honours `SMARTMEMORY_CRASH_REPORTS=0`) and exit cleanly. The prompt is still saved first, so the turn is remembered. The hooks also skip the daemon connection when no daemon is running, and wait at most 1.5 s to connect when one is. `smartmemory setup` now gives the SessionStart and UserPromptSubmit hooks a 15 s timeout when none is set, and keeps any timeout you chose.
- Add a real SQLite lifecycle golden through the local API, verifying persisted rows, search, recall, clear and empty storage, with no network allowed. The four lite-mode Redis probes it catches are declared as a strict known gap (CORE-LITE-REDIS-PROBE-1).
- Code index accepts recoverable grammar partials with visible spans and coverage, reports clean/partial/failed counts and publication outcomes, and retains successful file checkpoints for retry (CODE-PARSE-DIAGNOSTICS-1).
- Add sm code effects for deterministic Python boundary evidence without starting a memory store (CODE-EFFECTS-ENGINE-1).
- Verify hosted CLI bundles preserve framework kinds, export evidence and TS TESTS relations through the shared core parser (CODE-FRAMEWORK-SEMANTICS-1).

- Hosted code upload bundles preserve lexical identity, source spans and call confidence, including unresolved source references (CODE-EDGE-CONFIDENCE-1).

- CODE-INGEST-SURFACES-1: hosted code indexing uses core bundle preparation and complete dataclass serialization while preserving language selection and client-side parsing.

- Mirror groundwork (DIST-LITE-SYNC-1): the packaged mirror contract schema now matches the current contract, including the per-item `schema_changed_during_write` report flag.

## [1.5.23] - 2026-10-05

- On Windows, `*` and `?` in arguments such as `sm search "*"` are passed through literally instead of being expanded to file names.

## [1.5.22] - 2026-10-05

- Default local search hides raw hook transcripts. Use --origin PREFIX to inspect a producer in direct or daemon mode. Hosted mode clearly refuses this unsupported filter before connecting.
- Custom local embedding models warm successfully when their configured dimension is unavailable, with an explicit unverified-dimension warning. Runtime names now resolve the correct model files, while known dimension mismatches still fail startup.
- Semantic-hop planner notices use the executing daemon's key status, or local key status for direct search, and appear only after a successful search.
- Doctor treats verified healthy daemon and worker store owners as OK, retains contention and stale-owner warnings, and omits repair hints on healthy keyring and disk checks.
- Doctor records separate probe timings and budgets, reuses a healthy daemon's verified embedding warmup for the configured store, and reports slow cold-start timeouts as incomplete checks.
- Inline add accepts arbitrary property options before or after quoted text, including --key=value, while retaining reserved, duplicate and missing-value validation. Split text prompts users to quote it or use --all - stdin.
- Search prints full IDs, offers --json with complete fields, marks truncated previews, and supports --full bodies. Local read-only get resolves unique ID prefixes of at least six hex characters and lists candidates on ambiguity. Remote get asks for the full ID.
- sm help and sm help COMMAND forward to Click help. Keyless local semantic-hop searches show and log their heuristic-planner fallback.
- Restart prints worker-stop, daemon-stop, start/spawn and ready timings, including partial failures. Detached daemon stdio defaults to UTF-8 while preserving a user-selected encoding.
- Extractive `sm ask --reasoning` displays the returned graph relations using the same format as synthesized answers.
- Local `sm ask` without an LLM key returns clearly labelled extractive memory excerpts with full IDs, types and dates. Empty results exit successfully. Configured LLM failures keep their existing errors.
- Groundwork for Lite → hosted mirroring (DIST-LITE-SYNC-1 U2, no user command yet): a local mirror state store (`mirror.sqlite3`) holds the pairing, frozen pending snapshots, receipts and per-key baselines, refuses deletes after a database replacement or restore until an explicit re-pair, and lets only one mirror apply run at a time. `sm reset` now keeps `okf_namespace.json` and any mirror state.

## [1.5.21] - 2026-10-04

- Remote lifecycle hooks run the in-process engine directly, skipping the local daemon attempt while preserving local daemon acceleration and fail-open behaviour.
- Remote `sm add --prop key=value` delivers user properties through hosted ingest context and refuses reserved producer/scope keys. Remote `sm search '*' --prop key=value` uses one exact list filter, with filtered pagination. Semantic property search and unsupported combinations remain explicit refusals.
- Local `sm status` explains that memories live only on this machine, outside the cloud account, and reports the saved memory count even when the daemon is stopped or warming. `sm status --json` adds explicit local-only disclosure fields while preserving backend status fields.
- Remote `sm start`, `stop` and `restart` disclose their local proxy/viewer scope, configured API and workspace. Proxy health and extraction messages distinguish local readiness from unchecked hosted connectivity and capability, with `sm status` as the hosted access check.
- Remote lifecycle observe, distill and learn now deliver producer origin, requested memory type and properties through hosted ingest. Reserved property keys cannot override producer fields, and the service retains authority over workspace scope.
- Remote `sm search '*'` lists scoped memories with `--top-k` page limits, `--offset` continuation and surfaced service errors.
- Remote `sm code index` parses the checkout locally and uploads code entities and relations to the configured hosted workspace. Repo and request-size caps are explicit, failed replacements report uncertainty, and uploads never delete the previous index first. Incomplete directory traversal refuses replacement and reports failed paths, preserving the prior index. Hosted indexing reports that vector embeddings are unavailable.

### Install troubleshooting

- Handled setup, model warmup, store busy and reset failures use the anonymous crash reporter, shared opt-out and privacy filtering. User cancellations and ordinary usage errors are excluded. Failed setup prints bounded doctor guidance and a report ID or reporting status.
- Hook shells record failed lifecycle commands without launching a reporter. The next CLI consumes each hook and exit code once per install per day. Doctor shows recent hook failures and repair guidance.
- First-run checks validate the package's Python requirement and native vector/embedding imports, then cache successful checks per installed version. Failed native checks retry and report once.
- Doctor inspects SQLite and vector snapshots without repairing them, checks dimensions/counts, detects active or stale locks, tests cached embeddings offline, and checks keyring, Git Bash, disk space and daemon port owners. Expensive probes run in a bounded child process.
- Explicit `smartmemory warm` now returns an actionable failure when a loader fails instead of announcing successful warmup.
- Reports go through a durable outbox: written (already privacy-filtered) before sending, removed only after confirmed delivery, retried at the next interactive command or daemon start, capped in size, with corrupt files quarantined. Opting out deletes queued reports unsent.
- Redaction now also covers private API/proxy hosts (config, environment, OS proxy settings, URLs passed to setup), any user's profile path, and the current username, without altering report field names.
- Lifecycle hooks never run diagnostics, flush the outbox or send reports. All six hooks write failure markers to `~/.smartmemory`, including installs with a custom data directory.
- Doctor never sends, never consumes hook markers, never takes the store write lock, and shows pending hook failures and queued reports read-only.
- Model warmup and setup report a real failure when the embedding or reranker loader fails.

## [1.5.20] - 2026-10-04

- Windows wrapper and standalone MCP environments now use the same canonical credential file and lock. A newer legacy credential migrates before its source is removed, so account rotations survive reads across environments.
- Remote setup honors `--api-url` and the effective configured endpoint for validation and persistence. Unattended setup accepts `SMARTMEMORY_API_KEY`.
- Windows setup and MCP login share one credential fallback. Credential updates lock writers and verify a protected current-SID-only temporary file before writing any key, preserving the previous key on ACL failure. Legacy MCP credentials migrate once and are removed.
- Direct daemon startup retains its absolute lifecycle deadline. Git Bash executable and hook script paths both escape shell expansions.
- Windows Claude hooks use a validated absolute Git Bash path and setup refuses missing or broken Git Bash.
- Direct `get` emits ISO timestamps using the same JSON encoder as the API.
- Direct writes wait up to 30 seconds (configurable), report busy stores clearly, and never replay a write after a dropped daemon response.
- Store reset checks live owners and every file before mutation. Windows deletion holds exclusive handles and reports retained files as failure.
- Windows detached launches request terminal-job breakaway and no console window, with an explicit warning if breakaway is denied.
- Daemon startup rotates an oversized `daemon-output.log` before opening the child output handle (5 MiB default, one previous log).
- Local diagnostics skip the SmartMemory API probe. Remote diagnostics use the effective configured API URL, including support zip.
- Support exports redact raw, slash-separated and JSON-escaped Windows profile paths and omit absolute config paths.
- Include lightweight Requests SOCKS support and document UTF-8 piping for PowerShell 5.1.

## [1.5.19] - 2026-10-03

- Pins smartmemory-core 1.5.19 and smartmemory-mcp 1.5.19 (MCP lists its tools without opening the local store).

- Stop confirms that daemon health has disappeared before clearing its PID marker, and launchd shutdown polling leaves time for worker termination within the lifecycle deadline.
- Windows daemon and legacy-worker liveness checks inspect processes without signalling them.
- Stop verifies daemon and worker identity through portable process inspection and preserves markers when ownership cannot be established.
- Daemon shutdown requests let the HTTP server drain and close storage before bounded termination, with a warning if forced shutdown cannot confirm the final flush.
- Restart and stop retire orphan core workers even when the daemon is absent.
- Session state updates replace existing files safely and serialize concurrent saves on Windows.
- Codex MCP configuration escapes Windows executable paths and validates TOML before writing.
- Clear reports retained storage files as a failure and skips reseeding and success events after a partial reset.
- Piped memories and lifecycle JSON decode as UTF-8 independently of the Windows locale, with explicit invalid-input errors.
- Claude settings and shell profiles use UTF-8 during setup and removal.
- Workspace identity decodes Git repository roots as UTF-8, preserving Unicode paths.
- Background daemon, core worker and capture launches use native Windows detachment flags and redirect stdin.
- Diagnostic logs coordinate rotation across processes and release file handles between records. Daemon console output uses a separate file.
- Windows setup explains that login startup and automatic crash recovery require manual `sm start`.
- Windows API-key setup uses the credential store and provides PowerShell environment instructions, with a warning when persistence fails.
- Doctor provides native Windows virtual-environment repair commands without shell activation.

- Windows lifecycle hooks use quoted forward-slash paths and repair older registrations when setup is rerun. CLI and hook streams use UTF-8 so Unicode memory text and doctor status marks print safely.
- Releases automatically update the exact MCP dependency pin to the latest published version before building, keeping installs reproducible and the MCP server current.

- Remote `sm ask`, add, search, recall, get and status use the hosted service directly. Ask uses hosted LLM credentials, reports API and quota failures clearly, and also works through the remote viewer proxy. Local store maintenance commands explain their local-only scope.
- Windows setup uses text prompts and the guided tour and graph explorer TUIs are skipped until fully tested. Set `SMARTMEMORY_FORCE_TUI=1` to test the TUIs.
- Automatic anonymous crash reports cover CLI failures, daemon process and thread
  failures, HTTP 500 errors and degraded warmup. Reports remove credentials, memory
  bodies and home-directory names, with persistent deduplication and daily limits.
  Disable with `SMARTMEMORY_CRASH_REPORTS=0` or config `crash_reports = false`.
- `smartmemory report [MESSAGE] --send` previews the support upload and asks for
  confirmation (`--yes` skips the prompt). Failed sends save a local support zip.
  CLI crashes show the report ID, and first-run setup discloses automatic reporting.

## [1.5.18] - 2026-10-03

- CLI failures now save redacted tracebacks and command context to `cli-debug.log`
  and show a short support instruction. `SMARTMEMORY_DEBUG=1` restores terminal tracebacks.
- Setup logs model preparation context and gives timeout and mirror recovery guidance.
  Daemon warm-up and uncaught process/thread failures retain redacted tracebacks in `daemon.log`.
- `smartmemory doctor` checks network reachability, proxies, model cache, daemon health
  and storage. `smartmemory report --zip [PATH]` creates one redacted local support file.

## [1.5.17] - 2026-10-03

- Pins `smartmemory-core==1.5.17`: **Windows saves work again.** Every `sm add` on Windows failed with `OSError: [Errno 9] Bad file descriptor` while flushing the vector index (a regression from 2026-09-26).
- `sm tour` no longer crashes with `httpx.ReadTimeout` on a slow first start. It waits for the local server to finish warming up instead of treating the first health reply as ready, and reports a failed warmup in plain words.
- Hook capture and lesson saves no longer try to fsync a directory on Windows, which Windows does not allow.
- Model downloads during setup wait up to 300 s per read (`HF_HUB_DOWNLOAD_TIMEOUT`, was ~10 s) unless you set your own value.

## [1.5.16] - 2026-10-01

- Pins `smartmemory-core==1.5.16` (first- and second-person pronouns resolve to real participants before extraction, so "I now prefer light mode" supersedes "I prefer dark mode" while stored text stays verbatim; the supersession judge no longer retires current-state notes or silently rewrites its verdicts; Lite wildcard and typed listing return stored memories again; extracted FalkorDB entities get canonical ownership; an opt-in FalkorDB duplicate-vector guard, off by default).
- Daemon-backed `sm add` now warns on stderr when no usable LLM provider is configured, while keeping the memory UUID on stdout. The response and startup banner disclose that LLM entity/relation extraction is unavailable while ruler extraction and local enrichers still run, using core's effective route resolution.
- On macOS, stopping SmartMemory now waits for launchd to finish removing its job before a restart can begin. Status correctly reports a deliberate stop, and stop/restart also handle an unresponsive daemon. Startup, shutdown and restart share an absolute deadline, allow individual health probes to time out, and reserve time to force-stop resistant workers and verify their exit. They also tolerate workers already removed by launchd and support unmanaged shutdown without a GUI login. Startup failures show the underlying error and daemon log details with credential values, including provider API keys, redacted while preserving diagnostic file paths.

## [1.5.15] - 2026-10-01

- Pins `smartmemory-core==1.5.15` (Lite recency recall returns the newest memories; Lite recall no longer returns raw entity nodes; a failed spaCy model download raises `MissingModelError` instead of exiting the process; OKF export/import round-trips edges; newly ingested structured documents are searchable).
- CLI launch-funnel events now reach the daemon at `/memory/launch/event` and persist locally (or forward in remote mode). The emitter previously missed the daemon's `/memory` mount and silently returned false.
- Local `sm setup` now exits non-zero for failed or degraded daemon startup, with the failure reason, daemon log path, and `sm doctor` / `sm start --wait` recovery steps. Failed TUI setup also propagates the error to the command exit status. A daemon verified to be responding and still warming at the wait deadline stays running; setup exits zero without claiming readiness, defers additional worker startup, and warns to check `sm status` and run `sm start --wait` once warmup finishes. Its `setup.complete` event includes `warming: true`.
- Generated macOS daemon and worker launchd plists preserve the setup-time `HOME`, so custom homes keep their config and model caches after login startup.

## [1.5.13] - 2026-09-30

- A fresh install's first `sm add` no longer crashes with a `MissingModelError` traceback (LITE-FIRSTRUN-SPACY-1). When the daemon is not running, `sm add`, `sm search` and `sm get` download a missing spaCy model (the configured one plus `en_core_web_sm`) and the default local embedding model once, in a terminal or not, using the same downloaders as `sm setup`. Before each download they print one line naming the model, its size and "one time only", and after it one line saying it is done. These messages go to stderr, so scripts reading stdout are unaffected. `sm setup` now uses the same messages. If a download fails, the command prints one line naming what failed and asking you to run `smartmemory setup`, and logs a WARNING with the cause. A non-default embedding model that runs code from its model repository is only installed by `smartmemory setup`, and the message says so. `sm recall` and the hook `lifecycle` commands never download: they print the one-line setup instruction instead of a traceback. Set `SMARTMEMORY_AUTO_DOWNLOAD_MODELS=0` to turn off the download. The daemon, MCP server, viewer and background worker still never download. The "First run: loading local models" notice now prints only after the models are present.

## [1.5.12] - 2026-09-30

- `sm setup` now also prefetches the models that load lazily on first use (the MiniLM embedder shared by the domain/type classifiers and hybrid retrieval, and the MiniLM cross-encoder reranker), including when embeddings run on ONNX, so first queries no longer stall on a download. Setup no longer asks for remote code when preloading the embedder; approval now follows core's model registry.
- The Claude Code plugin manifest starts the MCP server from `smartmemory_mcp.server` (the old `smartmemory_app.server` entry point no longer exists).
- Dev tooling pins ruff 0.11.4 (`dev` extra, `required-version`) to match CI formatting.
- Claude Code auto-recall now dates every recalled memory (CORE-RECALL-FRESHNESS-1). Each non-decision line carries `[YYYY-MM-DD]`, the date the fact became true (reference time, then valid-from, then session date, then write time), so a months-old memory no longer looks as current as yesterday's conflicting one. Decisions keep their `[session:…]` tag. A memory with no usable date shows `[date?]` and logs a warning. Costs about 8 tokens per recalled memory.

## [1.4.118] - 2026-09-24

- Pins `smartmemory-core==1.4.118` (proof trees show evidence and supersede history; relation aliases that changed a fact's meaning removed).

## [1.4.117] - 2026-09-24

- Pins `smartmemory-core==1.4.117` (extraction no longer turns CFO/CTO/COO/president into CEO; titles link to people; name fragments fold into full names).

## [1.4.116] - 2026-09-24

- Pins `smartmemory-core==1.4.116` (decision listings keep `context_snapshot`: supersede/retract reason).

## Unreleased

- Show unknown for hosted code counts and outcomes absent from the server response, including transport failures (CODE-PARSE-DIAGNOSTICS-1).

- Retain hosted code-index failure bodies and print clean, partial and failed counts with the server publication outcome on CLI failures (CODE-PARSE-DIAGNOSTICS-1).

### Fixed

- `sm setup` also installs `en_core_web_sm` alongside the selected spaCy model so background workers can drain deferred work.
- Local startup checks model files on disk without downloading. `sm setup` prepares the configured local embedding backend's model files and transitive remote code for pinned torch models. The selected spaCy sm/md/lg model persists in config and is required at startup. Missing models return their setup instructions through the viewer API (HTTP 503) and daemon-backed CLI.
- Inference-time loaders still honor explicit `SMARTMEMORY_HF_ALLOW_DOWNLOAD=true` as an operator opt-in, including image builds.

- Lite daemon: `GET /memory/{id}/neighbors` now returns each neighbor's `content` (its
  human-readable label, falling back to the item ID). Without it the Obsidian plugin, which
  reads that field for entity names, wrote an empty entity list to every note and left
  entity chips and auto-link with nothing to work with in local mode. Neighbor labels are
  resolved once per request under a single lock.
- Lesson lifecycle first extracts explicit session rule changes, then matches stored
  rules to verified changes, including implementation findings of the same old rule.
  Matching now selects a verified change ID without asking the model to label the
  relation; the product derives supersession or retraction from that change's kind.
  Failed proof claims warn and appear in capture receipts; unverified claims never
  retire rules. Matching is skipped without verified changes. Duplicate/refines
  annotations are deferred. Malformed rows remain isolated and conflicts guarded.

- Lesson supersession can retire differently worded copies of one old rule, including
  its implementation findings, when explicit per-pair evidence identifies the same
  replaced rule; unrelated targets and conflicting actions remain guarded.
- Session lesson extraction keeps standing user policies as separate constraints, including
  forward-looking asides when the assistant also implements part of the rule, and
  preserves their attribution without treating current-task requirements as policies.
  Tagged, attributed standing rules remain constraints when the classifier calls them
  findings; stored attribution punctuation is normalized for consistent recall.

- Local ingest warns when caller properties contain reserved keys that are dropped,
  naming the keys while preserving producer-controlled provenance.

- Fresh Lite installs verify the spaCy model in the running Python environment. `sm status` and degraded startup explain when missing model files prevent queued work from draining.

- Async lifecycle hooks remove captured payload files after the CLI exits, including
  failures, and purge stale hook payload files from existing installations.

- Recall and confidence integration checks now use producer-supplied origins and
  verify stored provenance, tier filtering, and confidence markers. Lite async
  coverage expects a durable work-graph run ID when deferred work is queued and
  isolates special-character entity extraction from Tier 1 deduplication.

- Isolate wrapper tests from inherited data directories and sandbox-restricted
  port binding; update Click and doctor assertions to the current interfaces.

- Lite saves work without an LLM key, with deferred work owned by one core worker.
  `sm status` shows pending/running/dead work counts and a re-extraction offer.
  `sm admin reextract` queues work with `--yes`, `--all`, `--ruler`, or `--decline`;
  `sm worker requeue-dead` retries dead and skipped work. Daemon and launchd workers
  now use the durable core work graph; the legacy queue modules remain for migration.
  Upgrades retire legacy workers and reload old launchd agents, recover in-flight
  jobs only after confirmed consumer exit, and block replacement when process
  inspection or launchd retirement is uncertain. All persistent
  workers load the same provider configuration as saves; stop/restart also stops
  on-demand workers through the core worker lock.

- SessionStart injects a separately budgeted rules card of active workspace constraints,
  newest first. A dedicated classifier identifies external constraints, with separately
  reported usage and prefix-only fallback on failure. `smartmemory lifecycle reclassify`
  migrates active workspace lessons in batches (supports `--dry-run`); superseded
  lessons disappear and card IDs are excluded from subsequent prompt recall.
  Configure `rules_card_enabled` and `rules_card_budget` (default 2000 tokens).
  Legacy classification, budget loss, and unavailable remote cards emit warnings.

- Session lessons keep every stated rule. Extraction now files each rule,
  constraint, limit or required format as its own lesson, and a rule the model
  still labels as background ("must", "exactly", "rejected", an error code)
  is kept rather than discarded. Previously a bank rule could be extracted and
  then silently dropped.

- Prompt recall no longer lets loosely related lessons crowd out relevant
  memories. A lesson must share at least two meaningful words with the prompt
  (one for a one-word prompt), lessons are ranked by overlap, and words under
  three letters no longer count ("Stripe's" used to match every lesson
  containing an apostrophe).

- Session lessons now retire outdated ones. When a new session explicitly
  replaces or withdraws an earlier lesson (verbatim evidence required), the old
  lesson is marked superseded or retracted and drops out of Orient/Recall.
  Several new lessons may replace one old rule; one is kept as its successor of
  record. Declined transitions carry a downgrade reason in the capture receipt.

- Session capture receipts now record the lesson-extraction LLM usage (provider,
  model, prompt/completion tokens) and its cost from the core price table, with
  provenance. Retries and cached tokens are reported as unmeasured, never estimated.

- Session capture now stores up to eight durable lessons per Claude Code session
  (core reasoning → decision path, origin `import:claude_code:lesson`, linked to
  the transcript chunks). Hooks inject active lessons first, drop graph-only
  entity/relation/pattern nodes, and excerpt oversized memories by query
  relevance instead of head-truncating them.

- Fix async Observe, Learn, and Distill hooks, which never received their stdin
  payloads. Capture payloads before backgrounding and remove temporary files
  after processing, including large tool responses.

- Capture SessionEnd transcripts through the existing Claude Code importer with a
  durable capture ledger and detached worker; `smartmemory lifecycle drain`
  acknowledges completion, errors, or timeout.
- Fix topic-change recall embeddings and distinguish non-fatal trace degradations
  from failed injections.

- Hooks retain complete recalled memories within phase budgets, label injected items with stable IDs, and record final payloads and failures in the recall trace. Hook stderr is retained in `hooks.log`.
- Learn, Distill, Persist, and Observe captures retain workspace metadata; Learn, Distill, and Persist now preserve their explicit origins.

## [1.4.115] - 2026-09-24

- **Fixed:** pins `smartmemory-core==1.4.115`. A slow Wikidata no longer fails or stalls a save: grounding makes one
  attempt under a 10 s budget (`SMARTMEMORY_SYNC_GROUND_BUDGET_SECONDS`), unfinished entities are flagged on the item
  for later re-grounding, and money/number entities are no longer looked up. Also carries CORE-BG-2e (flags default OFF).

## [1.4.114] - 2026-09-23

- **Changed:** pins `smartmemory-core==1.4.114`. Core adds the CORE-BG-2b evolve-ownership flag (default OFF) and
  structured per-evolver outcomes; lite behaviour is unchanged (see core's CHANGELOG).

## [1.4.113] - 2026-09-23

- **Changed:** pins `smartmemory-core==1.4.113`. Core ships the CORE-BG-2a collaborator seam and
  the CORE-BG-2c enrich/ground worker offload, both behind a default-OFF flag; lite mode
  behaviour is unchanged (see core's CHANGELOG).

## [1.4.112] - 2026-09-21

- **Fixed:** pins `smartmemory-core==1.4.112` — `sm admin import` still failed after
  1.4.111's crash fix because exported bundles included internal entity graph nodes as
  unimportable `type: entity` pages (see core's CHANGELOG, TC-LITE-384).

## [1.4.111] - 2026-09-21

- **Added:** `sm update` — checks PyPI for a newer `smartmemory` release, installs it via
  `pip`, verifies the install, and restarts the daemon immediately if it was already running
  (a stopped daemon stays stopped).
- **Fixed:** pins `smartmemory-core==1.4.111` — `sm admin import` could crash with
  `TypeError: object of type 'NoneType' has no len()` while reporting item preflight errors
  in lite/local mode (see core's CHANGELOG, TC-LITE-384).

## [1.4.109] - 2026-09-21

- **Fixed:** `POST /memory/reextract` always 500'd — `local_api.py` imported a
  `_get_memory` helper that no longer exists in `storage.py`. Now uses the public
  `storage.get_memory()` accessor.
- **Fixed:** `sm add` could still fire live Wikipedia grounding HTTP calls even with
  grounding disabled, because a core-side policy override reset the wrapper's disable
  flags back to their defaults on every Tier-1 foreground ingest. Requires
  `smartmemory-core`'s matching fix (see its CHANGELOG). The daemon's Tier-2 dispatch
  no longer wastes a Redis connection attempt on every `add` either — lite mode now
  tells core to skip straight to the wrapper's own SQLite `enrichment_queue.py`.

## [1.4.108] - 2026-09-21

### Fixed — grounding could still hang on proxied networks after 1.4.106/1.4.107

- On macOS, `smartmemory setup` installs the daemon/worker as launchd jobs, which
  do not inherit the shell's environment — only the small env-var set baked into
  the plist. That set never included `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`/
  `NO_PROXY`, so a daemon on a network that requires a proxy for internet egress
  (e.g. via sing-box) would attempt direct connections to Wikipedia and hang
  until the per-request timeout. Proxy variables present at `setup` time are now
  captured into the launchd plist, matching the existing non-launchd daemon path.
- `sm doctor`'s SOCKS proxy check only verified `socksio` (used by the daemon's
  own local API calls over httpx). It never checked PySocks, which is what the
  Wikipedia grounder's HTTP client (`requests`/`wikipediaapi`) actually needs for
  a SOCKS proxy — so `doctor` could report a clean bill of health while grounding
  still couldn't reach a SOCKS-only proxy. Now reports both packages separately.
- No `smartmemory-core` functional change; pins `smartmemory-core==1.4.108`, a
  version-only bump required to keep the wrapper/core release lockstep.

### Changed — `sm uninstall` also removes the config file

- Previously only removed hooks, skills, and (unless `--keep-data`) the data
  directory. Now also removes the config file; `--keep-data` still preserves the
  data directory but no longer preserves the config file.

## [1.4.107] - 2026-09-21

- Pins `smartmemory-core==1.4.107`, which lowers the grounding lookup time budget
  introduced in 1.4.106 from 15s to 3s.

## [1.4.106] - 2026-09-21

### Fixed — `sm add` daemon timeout under Wikipedia grounding

- Local ingest no longer makes live Wikidata/Wikipedia network calls for entity
  grounding by default. Previously every `add`/`ingest` could make one live HTTP
  call per extracted entity with no time budget, which under rate-limiting could
  accumulate past the daemon's own 120s request timeout and cause a live daemon
  to be falsely reported as unresponsive. Pins `smartmemory-core==1.4.106`, which
  also bounds this network call to a 15s wall-clock budget regardless of caller.

## [1.4.105] - 2026-09-20

### Added — local CLI bug report formatting

- The CLI now keeps a silent, rotating DEBUG log in the configured local data
  directory (`~/.smartmemory/cli-debug.log` by default) without changing console
  verbosity or requiring `SMARTMEMORY_LOG_LEVEL=DEBUG`.
- `sm report [TEST_ID] MESSAGE` prints a structured, ready-to-paste tracker report
  with runtime/version context, the local debug-log path, and a bounded log preview.
  The tester pastes it into the tracker's Bug Report dialog and attaches the log
  manually. Direct submission was removed because the live API requires real user
  authentication that a standalone CLI cannot obtain.

### Fixed — daemon reloads immediately after pip upgrades

- The local daemon now checks installed wrapper and core versions on every
  request, so launchd restarts stale pre-upgrade code on the first request after
  a pip upgrade instead of waiting for a request-count milestone.

## [1.4.104] - 2026-09-20

### Fixed — search returned unrelated memories instead of empty results

- `sm search "<query>"` (and `SmartMemory.search()`) could return unrelated
  memories for a query with no real matches, because MiniLM's local embedder
  scores some unrelated short queries as high as 0.301 cosine similarity,
  clearing the existing recall floor. Core now additionally requires shared
  query/content words for weak hits. See `smartmemory-core` CHANGELOG for detail.

## [1.4.103] - 2026-09-20

### Fixed — search kwargs silently dropped since core added them

- `metadata_filter`, `reranker`, `include_reference`, and `memory_types` now
  actually reach the core search engine. They were valid parameters on
  `SmartMemory.search()` but missing from this wrapper's kwarg allowlist, so
  every caller setting one got a silent no-op instead of the intended search
  behavior (same failure class as CORE-RETRACTED-RECALL-1). `include_reference`
  had a second, narrower instance of the same bug: it only ever reached core on
  the unfiltered-search path, so a call combining it with a property filter
  dropped it too. `limit` is deliberately not exposed — this wrapper's `top_k`
  is already the one result-count knob.

### Added (2026-09-20) — one-switch CLI diagnostics

- `SMARTMEMORY_LOG_LEVEL=DEBUG` now records one startup environment summary,
  daemon request/response wire previews with status and latency, retry decisions,
  and every daemon-to-in-process fallback. Diagnostics stay on stderr, payloads
  are bounded, and `Authorization`, API keys, tokens, and other
  credential-shaped fields are redacted.

### Fixed (2026-09-20) — Wikipedia grounding works out of the box

- Default installs now include `wikipedia-api`, which the pipeline's default
  Wikipedia grounder requires. Existing incomplete installs no longer log an
  ERROR-level traceback on every ingest; the missing dependency is reported as
  one actionable WARNING and grounding skips the affected item cleanly.

### Changed
- **`smartmemory-mcp` pin bumped 1.4.96 → 1.4.100.** The wrapper installs the current MCP server again (release.sh had been warning about the drift).

### Fixed (2026-09-18) — install the compatible-provider SDK

- Default installs now include the `openai` Python SDK used by `sm ask` for Groq
  and other OpenAI-compatible providers, without adding `litellm` or requiring an
  OpenAI API key. Missing-SDK errors distinguish the dependency from credentials
  and explain how to reinstall/upgrade the wrapper or install the core LLM extra.

### Fixed (2026-09-17) — adaptive recall allocation

- Local recall preserves its recency-first blend while allowing either channel to
  fill unused slots, widening candidate windows in the app when necessary. Dedup
  precedes the final cap; exhausted eligible results produce a shortfall warning.
- Recall filtering and formatting now warn with item identifiers and discard
  reasons. Content dedup compares full lowercased content within a memory type,
  replacing the unsafe 120-character prefix comparison shared by local and remote
  formatting. Distinct memories sharing a prefix are retained.

### Fixed (2026-09-17) — readable recovery from stale installs

- Daemon startup now checks the spaCy and embedding-model prerequisites in
  parallel. Both steps are announced as in flight, each completion keeps its own
  elapsed time, and failures are collected so one failed check cannot hide the
  other.
- `/health` now reports an observed `warming` state while the daemon loads its
  backend. `sm start` returns as soon as that response is reachable, `sm status`
  distinguishes warming from a broken background process, and `sm start --wait`
  retains the ready-blocking behavior used by setup and programmatic callers.
- Startup now reports each timed model/backend step, shows throttled newline-only
  model-download progress, and uses an elapsed spinner only on interactive terminals.
  The setup TUI shows the same live startup state. `sm start` and `sm restart` now
  report the verified `/health` result: healthy, degraded with its reason and `sm
  doctor`, or failed when nothing responds. `sm status` distinguishes a stopped
  install from a configured background process that has stopped responding.
- `sm doctor` now detects a configured SOCKS proxy without the optional httpx
  transport, exits unsuccessfully, and gives the exact install command. `smartmemory
  setup` reports the same problem but continues so cached-model and offline recovery
  remain available. Hook-fed and ordinary memory commands stay silent.
- `smartmemory setup` now runs the same Python and `smartmemory-core` compatibility check as `sm doctor` before doing any setup work. An old or incomplete install stops with copy-paste recovery commands instead of reaching daemon startup and exposing a traceback. Hook-fed commands remain unchanged and silent.
- Subprocess daemon startup failures now include the recent daemon log output when available. A missing or unreadable log leaves the original startup error intact.
- macOS background jobs now switch themselves off if a bare `pip uninstall smartmemory` removes the package first. This stops macOS from restarting a permanently broken job and filling the daemon log forever; rerunning `smartmemory setup` after upgrading replaces older background jobs with the protected versions.
- `sm status` now explains why a daemon is degraded and gives the copy-paste next step `sm doctor`. The `/health` response and daemon warning log carry the same short, redacted reason without exposing a traceback, credentials, or local file paths.

### Fixed (2026-09-15) — 1.4.101: hardened Lite update check (DIST-LITE-UPDATE-CHECK-1)

- `smartmemory-core` 1.4.101 pin. The anonymous update check now takes an OS advisory lock so concurrent processes sharing one `SMARTMEMORY_HOME` send exactly one request, validates the on-disk install id (foreign-owned or unreadable ids disable the check, owned ids are secured to mode `0600`), and treats unknown cache states and malformed responses as a backoff rather than a retry. The server side has been live since core 1.4.100; this release brings the client up to the reviewed behaviour.

### Added (2026-09-15) — 1.4.100: Lite vector backend hygiene (DIST-LITE-VECTOR-HYGIENE-1)

- `smartmemory-core` 1.4.100 pin (first publish since 1.4.96, so this release also carries core 1.4.97 through 1.4.99). Lite's usearch vector backend gains a public `close()` (also reachable through `SmartMemory.close()` / `with SmartMemory(...)`), one `<collection>.fts.db` per collection with an automatic migration off the shared `fts.db`, and `SMARTMEMORY_LICENSE_KEY` is pinned by test as read only by the update check.
- `smartmemory-mcp` pin moved to the latest published 1.4.96.

### Added (2026-09-11) — DIST-BYOC-1 diagnostics download

- `sm doctor --bundle --url --out` downloads the authenticated, allowlisted metadata-only diagnostics bundle for support.

### Changed: CORE-LEXICAL-INDEX-1 consumer cutover

- Use one `lexical` channel with default weight 0.8. Removed channel names fail validation, explicit zero is preserved, and unavailable required lexical search fails without partial success.
- Coordinated service, common, Python, JS, MCP and lite contracts cover migration and recovery. See [migration guidance](https://github.com/smartmemory/smart-memory-docs/blob/main/docs/features/CORE-LEXICAL-INDEX-1/migration.md) and the [canonical contract](https://github.com/smartmemory/smart-memory-docs/blob/main/docs/features/CORE-LEXICAL-INDEX-1/lexical-contract.json).
- Release remains pending maintainer review of measured write cost and final verification. No version bump.

## [1.4.94] - 2026-09-10

- Core 1.4.94: Lite update check with anonymous install ping and redistribution license keys (DIST-LITE-UPDATE-CHECK-1). Opt out with `SMARTMEMORY_NO_UPDATE_CHECK=1` or `DO_NOT_TRACK=1`.

### Added (2026-09-06) — search windows and hop flags on the CLI (SEARCH-TIME-RANGE-1, SEARCH-HOP-STRATEGY-SURFACE-1)

- `sm search` gains `--since`/`--until` creation windows and `--multi-hop`, `--max-hops`,
  `--hop-strategy`, forwarded through the daemon, local and remote paths. A strategy without
  `--multi-hop` is rejected rather than silently ignored.

### Fixed (2026-09-06) — the kwargs allowlist dropped real search options

- `smartmemory_app/storage.py` filtered forwarded kwargs against an allowlist that omitted
  `hop_strategy`, `cleanup`, `origin`, `exclude_origins`, `include_archived` and
  `consolidation_first`. It is the only path from the MCP local backend and the CLI fallback
  into core, so those keys were dropped with no error — its own comment recorded having done
  this to four params for months. The allowlist now enumerates every explicit core search
  parameter and a guard test fails when a new one is added without being forwarded.


### Changed
- **`smartmemory-mcp` pin bumped 1.4.67 → 1.4.91.** The wrapper installs the current MCP server again (release.sh had been warning about the drift).
- **License.** `smartmemory` now ships under the SmartMemory Lite Runtime License instead of AGPL: free for personal, internal, evaluation, and non-commercial use; shipping it inside a commercial product needs a Lite Redistribution Agreement (free during early access). See LICENSE.
- **Lite is light (DIST-LITE-HARDEN-1, core 1.4.91).** `pip install smartmemory` no longer pulls torch. The same all-MiniLM-L6-v2 model now runs on ONNX Runtime by default (about 150 MB, sub-second cold start, identical vectors); `pip install "smartmemory-core[gpu]"` restores the torch stack and uses CUDA automatically. Local memory storage moved to format v2 (metadata in SQLite, vectors only in the index, atomic saves); existing stores migrate on first open. `sm` keeps downloading models on first run; library callers get a hermetic `MissingModelError` instead.

### Added
- **`smartmemory doctor` knows the real floor (DIST-INSTALL-RESOLVE-1).** Every `smartmemory` and `smartmemory-core` release below 1.4.39 is yanked on PyPI, so a fresh `pip install smartmemory` can no longer backtrack onto a wrapper that pins a core from the retired `/auth/me` API. `doctor` now checks the installed core against that same 1.4.39 floor (was 1.0.0) and a new integration test asks pip, in an empty venv, what it would install and fails if the resolved core is not the lockstep partner of the resolved wrapper. Install docs already recommend a clean venv and warn against the Anaconda `base` environment.
- **`sm` tells you when a newer release is out (DIST-UPDATE-HINT-1).** Any command can end with one dim line, `smartmemory 1.4.87 available — pip install -U smartmemory`. The version is read from PyPI at most once every 24 hours with a 2 second timeout, cached in the data directory, and a failed lookup is silent and backs off for a day, so the check never delays or breaks a command. Nothing is printed when output is not a terminal, when `--json` was asked for, on the hook commands that feed a model prompt (`sm recall`, `sm lifecycle`), or when `SMARTMEMORY_NO_UPDATE_CHECK=1` is set.
- **First run and upgrades now point at the tour.** A first `smartmemory setup` ends with `All done. Run 'sm tour' if this is your first time.` After an upgrade, the next command prints `Updated to <version>` once, and adds `Run 'sm tour' to see what's new` only when the tour gained a step, so a routine patch release does not re-advertise a tour you have already seen.
- **The tour has an `sm ask` step.** Between cross-session recall and the closing cheat sheet, the tour asks a question in plain language and shows the grounded answer, so the capability shipped alongside search is visible on the first run.
- **`sm explore` browses the knowledge graph from the terminal (DIST-LITE-10).** A memory in
  focus, its relations grouped by type, and a walk: up and down to move, Enter to follow a
  relation, Backspace to go back, `/` to search, `?` to ask, `q` to quit, with a breadcrumb
  showing the path taken. A live pane tails the daemon's event stream, one line per event, and
  flashes when an event touches the memory in focus. Deliberately not a drawn graph, because a
  terminal cannot lay one out legibly past a few dozen nodes, and the browser viewer already
  does that well. It adds no new endpoint, reads the daemon that is already running, and
  refuses with a clear message naming `sm start` when that daemon is not healthy. Structural
  edges are filtered out the way `sm ask` filters them, and the semantic edges of a memory's
  entities are walked in their place so a memory is never a dead end.

## [1.4.86] - 2026-09-04

### Removed
- **The lite WebSocket events server on `:9015` is gone (PLAT-PUSH-SSE-1).** No browser client had read it since the graph package moved to Server-Sent Events, and it was the second transport for a stream already published over SSE. The `websockets` dependency and the hidden `events-server` CLI command went with it. Real-time updates now reach the local viewer only over `GET /memory/progress/stream` on the daemon port.

### Added
- **`POST /memory/ask` relations now carry `source_id` and `target_id`.** Labels are what a reader sees; ids are what a UI focuses. `AskPanel` in `@smartmemory/graph` addresses the edge as `${source_id}->${target_id}:${type}`, and a pair of display labels cannot address a graph element. Additive — the existing `source` / `type` / `target` fields are unchanged. Contract: `docs/features/DIST-LITE-9/ask-contract.json`.
- **`sm ask "QUESTION"` answers from lite-mode memory evidence and graph relations.** It semantically retrieves the matching memories, follows their entity neighbors to collect the relevant relation edges, and makes one configured LLM call for a direct, grounded answer. The CLI prints only the direct answer by default (with a hint); `--reasoning` adds the model's reasoning, the exact evidence memory IDs, and the relations used. The endpoint returns `answer` and `reasoning` as separate fields. It refuses with a clear 503 when no supported LLM key is configured; it never fabricates a fallback answer.

### Fixed
- **The local graph viewer went silent whenever port 9015 was busy (PLAT-PUSH-SSE-1).** The loop that drained pipeline events lived *inside* the WebSocket server block, so a bind failure logged one warning about animations and then delivered nothing at all over SSE either. The drain loop is now the top-level task and binds no port, so there is no longer any way for a port conflict to stop event delivery.
- **A delete in lite mode drew as an add.** Lite frames carried no action field, so the viewer's classifier defaulted every graph-node event to "added" and deleted nodes stayed on screen. Every graph mutation the pipeline emits (adds, updates, node and edge deletes, clears) now maps to the right event kind and carries an explicit action, matching the hosted stream.
- **The lite stream now survives a reconnect without losing events.** Frames carry an event id, the daemon keeps a bounded replay window, and a client reconnecting with `Last-Event-ID` gets the events it missed. The sequence number is persisted, so a daemon restart no longer reissues numbers a client has already seen and had it discard fresh events as duplicates.
- **The lite stream no longer ignores query parameters it does not support.** Resume and replay requests (`since`, `run_id` with `from_seq`) are honoured; an invalid combination is refused with the same 400 and 404 responses the hosted route gives, instead of being silently dropped into a plain live stream.
- **Worker-side deletes reach the viewer.** The loopback bridge the background enrichment worker posts to accepted node and edge additions only, so a delete or a clear done by the worker never reached the graph and the picture drifted out of step with the store.
- **A slow viewer no longer loses events without saying so.** When a client's queue is full the daemon logs a warning naming the dropped frame and how to recover it, instead of discarding it silently.
- **Lifecycle hooks now use the warm daemon instead of cold-starting a model every time.** `DIST-DAEMON-1` promised every memory command tries the daemon HTTP API first and falls back to direct storage, but the `lifecycle` group was never wired up. Each of the six hooks (orient, recall, observe, distill, learn, persist) spawned a fresh interpreter and paid the embedder cold start on every single fire — and `observe` fires on every tool call. Measured over 120 real prompts, recall ran a 4.9s median with a 15.2s p90 and a 28.4s max, against Claude Code's 30s hook kill line, so under load `UserPromptSubmit` failed visibly and discarded its output. All six now POST to the daemon and fall back in-process unchanged when it is not running. Warm recall measures 0.34s against 5.3s cold on an idle machine, and the loaded-machine tail disappears because no model is loaded on the hook path at all.
- **The daemon's `/lifecycle/recall` dropped `cwd`, undoing the workspace scoping fix below.** The CLI passed it and the HTTP endpoint did not, so recall through the daemon searched the config team instead of the session's workspace — the same leak that put a demo fixture corpus into unrelated coding sessions. The endpoint now forwards `cwd` like the CLI always did.
- **The daemon's `/lifecycle/observe` and `/lifecycle/learn` took `tool_response` raw.** Claude Code sends that field as a JSON object, and the phases slice it, so a dict raised `KeyError: slice(...)` before the phases' own error handling could log anything — the crash that left `hook:observe` and `hook:learn` items absent from the graph entirely. The CLI already coerced via a private `_as_text`; that helper is now `lifecycle.as_text` and both entry points share it, so the two sides cannot drift apart again.
- **One-shot `search` now returns ranked results.** When the daemon is not running, the CLI falls back to an in-process search whose reranker loaded "in the background" — but a one-shot process exits before that finishes, so every result came back in fusion order with an UNRANKED warning. The fallback now opts into the synchronous load (`SMARTMEMORY_RERANK_BLOCK=1`), costing about a quarter second on top of the embed load the path already blocks on. The daemon path is unchanged and still warms at boot.
- **`retag` dry run pointed at an option that does not exist.** It printed "pass `--no-dry-run`", but the command declares a plain `--dry-run` flag, so the suggested follow-up failed with `No such option`. Anyone following the hint saw a dry run that listed items and a real run that silently did nothing. The hint now says to re-run without `--dry-run`, and a test asserts the command's messages only name declared options.
- **Session-start orient now scopes by workspace too.** `orient()` looked up "patterns conventions decisions" via `storage.search`, which takes no cwd and so had the same remote-mode leak as the recall hook: it returned the config team's memories for every project. It now uses the workspace-scoped `storage.recall(cwd, query=...)` path.
- **Recall hook now scopes by workspace in remote mode.** `smartmemory lifecycle recall` called `storage.search` with no cwd, which in remote mode sent only the config `team_id`, so every Claude Code session (any project) recalled from the shared team's memories. With a demo tenant configured, its fixture corpus was injected into unrelated coding sessions. The hook now routes through the workspace-scoped `storage.recall(cwd, query=...)` path, and the recall trace (`hook-recall.jsonl`) finally records what was actually emitted.
- **`smartmemory-mcp` pin advanced 1.4.51 -> 1.4.67.** The pin had drifted 16 releases behind because `smartmemory-mcp` is not part of the core release sync chain, and nothing checked it. The comment claiming it "moves in lockstep with the wrapper" was wrong and has been corrected: it tracks the latest published `smartmemory-mcp`, and `smartmemory-mcp` does not depend on `smartmemory-core`, so the two pins cannot conflict.
- **`scripts/release.sh` now checks the mcp pin.** It fails closed if the pinned version is not published on PyPI (an uninstallable wheel), and warns when a newer one exists, so a deliberate hold-back stays possible but a silent drift does not.

## [1.4.71] - 2026-08-25

Version-only release to carry core 1.4.71. No wrapper code changed.

Core 1.4.71 closes CORE-ASYNC-STAGE-DIVERGENCE-1. **Async ingest used to accept content
and silently discard it**: `mode=async` wrote one queue message nobody consumed and
answered `queued`. It now returns 503 unless a live consumer is draining every stage,
and the new `async_preferred` mode falls back to synchronous processing and says so in
the response. Accepted runs get a deadline and a terminal `abandoned` status instead of
vanishing after an hour. A poison queue message that used to retry forever is now
dead-lettered. The pipeline's stage list lives in one import-free catalog
(`smartmemory.pipeline.stage_catalog`) that both the sync and async paths derive from,
and `stage_retry_policies` overlays naming a stage that does not exist are rejected
instead of silently ignored. Full detail in the core changelog.

## [1.4.70] - 2026-08-23

Version-only release to carry core 1.4.70. No wrapper code changed.

Core 1.4.70 fixes a search-ranking bug with a user-visible symptom: **the same query
against the same data could return a different top-5.** Item ids are random UUIDs, and
ranking broke ties on them, so re-ingesting identical content reshuffled which of two
equally-scored memories took the last slot. Ties now break on a digest of the item's
content, which is stable across ingests. Retrieval quality is unchanged — the fix only
reorders items already tied on score — but results are now reproducible. See the core
changelog for the measurement and the full list of affected ranking paths.

## [1.4.69] - 2026-08-22

Version-only release to carry core 1.4.69. No wrapper code changed.

Core 1.4.69 fixes a crash in Studio workflow mode (a real evolution workflow raised
`AttributeError` on every run) and completes the retirement of the never-functional
CORE-EVO-LIVE-1 incremental evolution layer. See the core changelog for detail,
including the corrected scope note: the service sleep daemon schedules only a named
subset of evolvers, not the full auto-run set.

## [1.4.68] - 2026-08-22

### Fixed — automatic memory lifecycle silently recorded nothing

The `observe` and `learn` phases of the automatic lifecycle never wrote a memory.
Claude Code sends a tool's result as a JSON object, and both phases built their text by
slicing that value as if it were a string, which raised an error. The error escaped
before the surrounding failure handler could catch it, and the hook script discards its
own errors and always reports success, so nothing surfaced anywhere: no warning, no
failed hook, no items. Tool observations and error captures were absent from every
install for months while the feature reported itself as enabled.

Result payloads are now converted to text where the hook input is first read, so both
phases keep working whatever shape the payload arrives in. Regression tests cover the
object payload end to end for both phases.

## [1.4.66] - 2026-08-15

### Fixed — extraction stopped working when Groq retired its Llama models

Groq shut down `llama-3.3-70b-versatile` and `llama-3.1-8b-instant` on 2026-08-16.
Those were the defaults SmartMemory picked whenever a `GROQ_API_KEY` was present, so
on that date extraction would have started failing for anyone relying on them. The
defaults are now `openai/gpt-oss-120b` (Groq's recommended replacement, and cheaper at
$0.15/$0.60 per million tokens against the retired model's $0.59/$0.79) and
`openai/gpt-oss-20b` for the fast tier.

Provider detection was rebuilt around an explicit model table rather than guessing from
the model name. The replacement models are vendor-namespaced (`openai/gpt-oss-120b`), so
name-based guessing would have read them as OpenAI models and sent them to OpenAI with
the wrong key.

### Fixed — a configured OpenAI key could break calls to other providers

When you pointed SmartMemory at an OpenAI-compatible endpoint (via `OPENAI_BASE_URL` or
an explicit `api_base`), it used `OPENAI_API_KEY` before checking which provider the
endpoint actually belonged to. If that key was set but not valid for the destination,
every such call failed with `Invalid API Key`, even though the correct provider key was
configured. Keys are now matched to the endpoint first, falling back to `OPENAI_API_KEY`.
Local servers such as Ollama and LM Studio are unaffected.


## [1.4.62] - 2026-08-08

### Fixed — local extraction could silently learn nothing

If you had `OPENAI_BASE_URL` pointed at an OpenAI-compatible provider (Groq and similar)
**and** the Claude Agent SDK installed, SmartMemory picked a model name that endpoint
could not serve and sent it anyway. Extraction then produced **no entities and no
relations, with no error** — memory appeared to work while learning nothing from your
text. Hosted endpoints also failed to pick up their own API key (`GROQ_API_KEY`), falling
back to a local placeholder and returning 401.

Both are fixed: model selection now honours your configured endpoint, and a hosted
endpoint resolves the matching provider key. Local servers (Ollama, LM Studio, vLLM) are
unaffected. If you worked around this by setting `SMARTMEMORY_LLM_MODEL`, that still takes
precedence and needs no change.

### Fixed — relationship extraction missed common phrases

Nearly half of the built-in relationship patterns could never match, including everyday
structural phrasing such as "depends on", "part of", "is a type of", and "causes". Those
now work. This affects the opt-in rule-based relation extractor, so no default behaviour
changes.

### Changed (2026-08-07) — README: remove open-source framing and dead repo links

The README renders publicly on the PyPI project page. GitHub is blocking the org from
publishing any repository, so every repo URL in it 404s for outside readers, and the
open-source claim was unverifiable.

- License section no longer says "dual-licensed ... for both open-source and commercial
  use"; it points at the bundled LICENSE file without characterizing the terms. **The
  LICENSE file itself is unchanged.**
- Links: GitHub, Issue Tracker, Obsidian plugin, and Web capture repo URLs replaced with
  a support address (all four 404 publicly, verified).
- Obsidian section: the plugin repo link is unlinked, and the BRAT install instructions
  are replaced with a support contact — BRAT installs by repo slug, so those steps could
  not work while the repo is private.
- This file's header no longer links to core's CHANGELOG on GitHub.

## [1.4.60] - 2026-08-06

### Fixed — local-mode search silently ignored four documented params (CORE-RETRACTED-RECALL-1)

`smartmemory_app.storage.search()` forwards kwargs to core through an allowlist, and an
unknown key is **dropped rather than rejected**. Four params callers can legitimately set
were missing from that list, so in local mode they silently meant nothing:

- `as_of_date` / `as_of_strict` and `include_superseded` — missing since
  PLAT-AUDITABLE-MEMORY-1, whose MCP changelog stated they were "forwarded through both
  backends". They were not: this function is the MCP **local** backend's only path to core.
  A local-mode agent asking for an as-of audit answer received plain present-day search
  results, with nothing indicating the request had been ignored.
- `include_retracted` — new in core 1.4.60, would have been the fourth.

All four are now forwarded, with a regression test. Remote mode was unaffected (it builds
the POST body separately). **If you add a parameter to `SmartMemory.search()` that callers
can set, add it to this allowlist in the same change** — the drop-don't-raise design means
nothing will tell you it is missing.


## [1.4.59] - 2026-08-04

### Added (via core) — auditable memory (PLAT-AUDITABLE-MEMORY-1)
Core 1.4.59 delivers the auditable-memory surface: `explain()` single-call provenance, hash-chained version audit with supersession lineage, and as-of (transaction-time) recall. No wrapper-specific changes.

## [1.4.58] - 2026-08-04
### Fixed (via core) — background sweeps, write-path serialization, hidden profiler memories
- Decay and compaction sweeps now enumerate items through a scoped graph reader instead of a
  wildcard search, so they work on lightweight (CRUD-only) memory handles and no longer skip
  the corpus silently.
- Compaction treats an item with no activation telemetry as "no signal yet" and skips it,
  instead of reading it as maximally cold and tombstoning it.
- Metadata containing sub-dicts keyed by non-identifier strings (UUIDs, entity names) no
  longer makes an item permanently unwritable: both the sync and async graph write paths keep
  such subtrees whole instead of flattening them into invalid property names.
- Trait memories written by the Maya psychology profiler (`profiler:*` origins) are now
  registered as recall-visible; previously they fell through to the hidden infrastructure
  tier and the feature was a silent no-op.
- Also via core 1.4.58: vector rows now carry workspace scoping on write, the vector
  relevance floor is calibrated per embedding model, stale vector indexes are detected
  instead of silently disabling semantic recall, and the DSPy dependency is gone.

## [1.4.57] - 2026-07-30
### Fixed (via core) — scoped vector search silently returned too few results
- On a shared vector index, a search scoped to one workspace fetched a fixed number of
  candidates, threw away everything belonging to other workspaces, and returned whatever
  survived. A workspace owning a small slice of the index got a short answer, or no answer at
  all, with nothing in the logs to say so. Measured against a real backend, only 4 of 10
  available matches came back (recall@10 of 0.400).
- Fixed in smartmemory-core 1.4.57: the search now widens its candidate window when a page
  comes back under-filled, and stops only when the backend itself confirms the index has been
  read in full or a configurable ceiling is reached (`VECTOR_MAX_SEARCH_CANDIDATES`, default
  512). The same query now returns all 10 of 10 matches (recall@10 of 1.000). Unscoped and
  global reads are unaffected, as are collections smaller than a single fetch.
- Under-filled pages now log a warning once per collection and workspace instead of degrading
  in silence, and a `top_k` larger than the ceiling warns rather than truncating quietly.
- Behaviour change: the FalkorDB backend's text search now honors its `top_k` argument
  exactly. It previously requested twice that internally, an undocumented doubling the other
  backend never had.

## [1.4.56] - 2026-07-21
### Fixed (via core) — local llama/mixtral-named models on ollama/lmstudio no longer misroute
- Follow-up to 1.4.55: a local model whose name starts with `llama`/`mixtral`/`gemini`/`claude`
  was still routed to the matching cloud provider and 500'd (e.g. `ollama run llama3.2` — the
  most common Ollama model). Fixed in smartmemory-core 1.4.56: `call_llm` now honors an explicit
  OpenAI-compatible endpoint (`OPENAI_BASE_URL`, set by `llm_provider=ollama`) over guessing the
  provider from the model name. Every local model now works, not just non-cloud-named ones.

## [1.4.55] - 2026-07-21
### Added — Local LLM extraction via OpenAI-compatible servers (Ollama, LM Studio, LocalAI)
- Setting `llm_provider = ollama` (or `lmstudio` / `localai`) now actually runs local LLM
  extraction. An OpenAI-compatible URL *is* the OpenAI path, so no provider-specific client
  code was needed: the wrapper translates the provider into the env vars the extraction path
  already consumes — `OPENAI_BASE_URL` (localhost default, override via `llm_base_url`),
  a placeholder `OPENAI_API_KEY` (local servers ignore auth; this also flips extraction on so
  the daemon runs Tier-2 instead of the keyless Tier-1 downgrade), and `SMARTMEMORY_LLM_MODEL`
  from `llm_model`. Never clobbers keys/URLs the user set explicitly.
- New config field `llm_base_url` (+ `SMARTMEMORY_LLM_BASE_URL` / `SMARTMEMORY_LLM_MODEL` env)
  for non-default hosts/ports (e.g. a remote GPU box running Ollama).
- Note: extraction *quality* on a small local model may be weaker than a cloud model (messier
  JSON) — a quality tradeoff, not a failure. Also, `OPENAI_BASE_URL` is process-wide, so a
  config mixing `llm_provider=ollama` with `embedding_provider=openai` is not supported.

## [1.4.54] - 2026-07-21
### Fixed — Keyless lite ingest 500 on a fresh install
- Local `/ingest` with no LLM API key ran the full core pipeline (including `llm_extract`),
  which hard-requires a cloud LLM key. On a fresh lite install with no key, every
  `smartmemory add` returned HTTP 500 (`ValueError: No API key found` →
  `RuntimeError: Stage 'llm_extract' failed after 3 attempts`). Keyless ingest now runs
  Tier-1 (spaCy + EntityRuler) only, as it always claimed to. Invisible on dev machines
  because they carry cloud keys and take the working two-tier path.
- Note: this does **not** wire a local Ollama model into extraction — `llm_provider=ollama`
  still does not enable LLM extraction; that remains a separate, unimplemented gap.
- Released manually (out of CI) because the smart-memory-org publish pipeline is billing-blocked.

### Changed (auto, lockstep) — track smartmemory-core==1.4.51 (1.4.51)
- Version copied from smartmemory-core 1.4.51 release (single-source lockstep).

### Changed (auto, lockstep) — track smartmemory-core==1.4.50 (1.4.50)
- Version copied from smartmemory-core 1.4.50 release (single-source lockstep).

### Changed (auto, lockstep) — track smartmemory-core==1.4.49 (1.4.49)
- Version copied from smartmemory-core 1.4.49 release (single-source lockstep).

### Changed (auto, lockstep) — track smartmemory-core==1.4.48 (1.4.48)
- Version copied from smartmemory-core 1.4.48 release (single-source lockstep).

### Changed (auto, lockstep) — track smartmemory-core==1.4.47 (1.4.47)
- Version copied from smartmemory-core 1.4.47 release (single-source lockstep).

### Changed (auto, lockstep) — track smartmemory-core==1.4.46 (1.4.46)
- Version copied from smartmemory-core 1.4.46 release (single-source lockstep).

### Fixed
- Remote mode `sm search` always printed "No results": `RemoteMemory.search()` predated the CORE-RECALL-LINEAGE-1 `SearchResponse` envelope (`{"results": [...]}`) and silently degraded every dict response to an empty list. It now unwraps the envelope (bare-array responses from pre-LINEAGE-1 services still work). Found live in DEMO-WALKTHROUGH-4 spike 0.1.
- Local-mode search 500'd whenever `entity_patterns.jsonl` contained a Wikidata-harvest `_comment` header row: `JSONLPatternStore._read_all()` KeyError'd on rows without `name`. Header/metadata rows are now skipped generically (mirroring core `seed_rom.py`); malformed JSON rows are skipped with a WARNING.
- `sm why` rendered evidence memories as "(unknown date)": resolved provenance evidence carries its timestamp in `metadata.created_at` (no top-level `created_at`), which `_why_date` now falls back to (then bi-temporal `transaction_time`).

### Added
- Docs: README now surfaces OKF portability (export and import your whole memory as portable Open Knowledge Format bundles, Google's OKF v0.1, with an Obsidian-native round trip), plus Links pointers to the Obsidian plugin and the web capture browser extension.
- DIST-TOUR-1: `sm tour` now launches a guided onboarding TUI that runs against an isolated local tour store, opens the graph viewer, seeds demo project facts, shows semantic search and cross-session recall, and computes a real `tiktoken` token receipt. `--no-viewer`, `--keep`, `--port`, and the stubbed `--code` branch are wired through the CLI.

### Changed
- DIST-WHY-1: `sm why "QUESTION"` is a read-only decision/provenance query: service mode finds the closest hosted decision and renders its rationale, supersession chain, and evidence (or raw JSON); local mode visibly explains its capability limit and shows closest-match derivation lineage instead.
- DIST-TOUR-1: the tour TUI now presents each step as the CLI session it teaches — `$ sm add/search/recall` commands (was raw `POST /memory/...` REST strings), command above output (was output-command-output, with the body duplicated into the scroll pane), and a two-phase search step (the command appears, then its results land). The tour box also clamps to the terminal width (`max-width: 100%`) — terminals narrower than 88 columns previously clipped its right edge.

### Changed (auto, lockstep) — track smartmemory-core==1.4.45 (1.4.45)
- Version copied from smartmemory-core 1.4.45 release (single-source lockstep).

### Changed
- README restructured around an accessible quickstart: install-to-working in the first screen, a guided CLI tour (capture, piping notes in, per-project tagging, daemon, viewer), and new "Use it with Claude Code" / "Use it with Obsidian" sections. Full command reference, evolver/plugin catalogs, and the Python API moved into collapsible sections; no information removed. CLI examples validated against a live install.

### Fixed
- `sm tour` now dwells on each guided step so semantic search results and the token receipt
  are readable in the TUI. The human tour path defaults to 4 seconds per step and can be
  overridden with `SMARTMEMORY_TOUR_STEP_DWELL`; direct `TourArcDriver` callers still default
  to no dwell.
- README command reference showed `smartmemory mcp install claude`; the actual client argument is `claude-code` (choices: `claude-code`, `cursor`, `codex`).
- DIST-CLI-QUIET-1: the CLI now installs a root logging policy (default WARNING, `SMARTMEMORY_LOG_LEVEL` to override), so cold-start adds stop printing walls of pipeline INFO chatter. Installing the root handler up front also neutralizes import-time `logging.basicConfig()` in dependencies (fastcoref). `sm worker` keeps its INFO progress lines. Pairs with smartmemory-core's DIST-LITE-QUIET-3 fixes.

### Changed (auto, lockstep) — track smartmemory-core==1.4.44 (1.4.44)
- Version copied from smartmemory-core 1.4.44 release (single-source lockstep).

### Changed (auto, lockstep) — track smartmemory-core==1.4.44 (1.4.44)
- Version copied from smartmemory-core 1.4.44 release (single-source lockstep).

### Changed (auto, lockstep) — track smartmemory-core==1.4.43 (1.4.43)
- Version copied from smartmemory-core 1.4.43 release (single-source lockstep).

### Changed (auto, lockstep) — track smartmemory-core==1.4.42 (1.4.42)
- Version copied from smartmemory-core 1.4.42 release (single-source lockstep).

### Changed (auto, lockstep) — track smartmemory-core==1.4.40 (1.4.40)
- Version copied from smartmemory-core 1.4.40 release (single-source lockstep).

### Changed (auto, lockstep) — track smartmemory-core==1.4.39 (1.4.39)
- Version copied from smartmemory-core 1.4.39 release (single-source lockstep).


### Fixed — Deleted memories left orphaned vector embeddings behind (CORE-VEC-DELETE-1)
- `sm`'s local/lite mode never actually removed a deleted memory's embedding from disk —
  `smartmemory-core`'s usearch backend had no single-item delete, so the vector index kept
  serving stale hits for item IDs that no longer existed in the graph. Fixed upstream in
  `smartmemory-core` (see its CHANGELOG); also fixes a related shutdown-time warning
  (`'CollectionAwareVectorBackend' object has no attribute '_save'`) that fired on every
  daemon/CLI exit in local mode — harmless (embeddings already persist on every write) but noisy.

### Changed (auto, lockstep) — track smartmemory-core==1.4.38 (1.4.38)
- Version copied from smartmemory-core 1.4.38 release (single-source lockstep).

### Changed (auto, lockstep) — track smartmemory-core==1.4.37 (1.4.37)
- Version copied from smartmemory-core 1.4.37 release (single-source lockstep).

### Fixed — Proxy immunity + friendly CLI errors for daemon calls (FIX-C / L1 + L4)
- **`trust_env=False` on all local daemon httpx calls.** `_daemon_request`, `is_running`,
  `stop_daemon`, and `get_status` now construct `httpx.Client(trust_env=False)` so proxy
  env vars (`ALL_PROXY`, `HTTP_PROXY`, `HTTPS_PROXY`, SOCKS) never route health-checks
  through a proxy — fixing `sm start`/`sm status` falsely reporting the daemon as down under
  a SOCKS proxy (L4).
- **Friendly CLI errors for transport failures.** Connection refused/timeout now raises a
  `ClickException("SmartMemory daemon is not running. Run \`sm start\` …")` instead of silently
  returning `None`. HTTP 5xx responses now append a `(check ~/.smartmemory/daemon.log)` hint.
  `ReadTimeout` likewise surfaces a message with the log pointer. No raw tracebacks for either
  class (L1).
- **Port-conflict message.** When `sm start` opens a port but the health check fails (another
  process is already using the port), the error message now reads "Port N is in use (possibly
  another SmartMemory daemon or process); check \`sm status\` after fixing."

### Added — `--version` flag (FIX-C / L2)
- Both `sm` and `smartmemory` entry points now accept `--version` to print the package version
  (`smartmemory x.y.z`) and exit.

### Fixed — Launch telemetry now reaches the hosted funnel in remote mode (LAUNCH-METRICS-1)
- **Daemon `/launch/event` forwards to the hosted service in remote mode** (authenticated with
  your API key), so CLI funnel events actually land in the hosted `launch_events` store. The
  CLI docstring claimed this proxying existed; it did not — events silently stayed on-disk. A
  failed forward falls back to the local `launch_events.jsonl` (never discarded, logged at
  WARNING). Local mode is unchanged: events stay local. Opt-out remains
  `SMARTMEMORY_DISABLE_LAUNCH_METRICS=1`. New `tests/unit/test_launch_emission.py` covers the
  emitter contract, JSONL append, remote forward, and the fallback (9 tests).

### Added — Code-authorship capture (CORE-CODE-PROVENANCE-1 Phase 2a)
- **Trace code back to the conversation that wrote it.** The PostToolUse hook now persists full-payload code-authorship evidence for Edit/Write/MultiEdit (alongside the existing reflection, never replacing it), anchored to a `:Session` graph node. A new `smartmemory provenance import-codex` command imports Codex `apply_patch` authorship from `~/.codex/sessions`. Capture is failure-isolated (a provenance error never blocks the normal hook) and serializes across concurrent detached hooks via the cross-process write lock.
- **Fixed: hook captures stored `origin="unknown"`.** `observe`/`distill`/`learn`/`persist` passed `origin` via `properties`, a reserved key that gets stripped — so those items were mis-attributed as `unknown` (tier 4) instead of their intended tags. The re-routed `observe` capture is corrected to `origin="hook:observe"` (tier 3); the `distill`/`learn`/`persist` siblings are flagged for a follow-up.

### Changed (auto, lockstep) — track smartmemory-core==1.4.36 (1.4.36)
- Version copied from smartmemory-core 1.4.36 release (single-source lockstep).

### Added (lockstep) — track smartmemory-core==1.4.33 (1.4.33)
- **Cold-start entity-typing parity for server mode.** Bundles the seed-pattern ROM so a fresh workspace gets deterministic dictionary entity-typing from request #1, matching Lite (CORE-RELATION-RULER-1).

### Fixed
- **No-LLM ingestion is no longer silent.** When no LLM provider key is configured, the daemon degraded to Tier-1 (spaCy) extraction without telling anyone — `add` returned a normal item id, the boot banner said nothing, and `status` showed the configured provider as if it were active. Now: the daemon boot prints an explicit `LLM extraction: DISABLED …` line, the worker-start gate logs a WARNING instead of returning silently, `/memory/ingest` returns a `warning` field that `smartmemory add` surfaces (`⚠ …`), and `smartmemory status` shows `LLM: <provider> (no API key — extraction disabled)`.
- **Anthropic/DeepSeek keys are now recognised for Tier-2.** The worker-start and ingest paths previously hardcoded a `GROQ_API_KEY || OPENAI_API_KEY` check, so a user configured only with Anthropic or DeepSeek silently got no entity extraction despite a valid key. All four providers now share one source of truth (`config.llm_key_present()`).

### Changed (auto, lockstep) — track smartmemory-core==1.4.32 (1.4.32)
- Version copied from smartmemory-core 1.4.32 release (single-source lockstep).

### Changed (auto, lockstep) — track smartmemory-core==1.4.28 (1.4.28)
- Version copied from smartmemory-core 1.4.28 release (single-source lockstep).

### Changed (auto, lockstep) — track smartmemory-core==1.4.27 (1.4.27)
- Version copied from smartmemory-core 1.4.27 release (single-source lockstep).

### Changed — quiet, correctly-attributed FREE/local first run (DIST-LITE-QUIET-1)
- The `"No API key found"` warning now appears **only in remote mode** — a local-mode
  default needs no key, so the warning was misleading first-run noise on the zero-account path.
- The `smartmemory add` CLI now declares its producer (`cli:add`) to the local daemon, so
  CLI-ingested memories are attributed as tier-1 user content instead of `origin='unknown'`.
- The local daemon `/memory/ingest` endpoint is **producer-neutral** (it no longer blanket-labels
  every caller as `cli:add`); each producer declares its own origin via request context.
- `JSONLPatternStore` (the local pattern store) declares `quiet_missing_capabilities`, so its
  expected missing-capability notes log at DEBUG instead of WARNING on the FREE tier.
- **Security:** `origin` is now a reserved ingest key — user-supplied properties can no longer
  override the producer-declared origin (which drives tier visibility and precedence guards).

### Added — `smartmemory warm` + background model warming (DIST-LITE-WARMSTART-1)
- New `smartmemory warm` CLI command pre-loads the local embedder (and reranker) so the
  first `add`/`search` is instant instead of paying a cold model load (~12s, or ~38s the
  first time the model downloads). Run once after install or before a demo. `--no-reranker`
  warms the embedder only.
- The local backend now **warms the embedder in the background at construction** (daemon
  thread) so the user's first `add()` overlaps the model load instead of paying it inline.
  Opt out with `SMARTMEMORY_NO_WARM=1`.
- Direct (no-daemon) CLI ops (`add`, `recall`) now print a one-time "First run: loading
  local models…" notice before a cold load, so first-run isn't a silent hang. (The daemon
  path already prints "loading models" at `start`.)
- Paired with the core reranker fix (non-blocking model load), this removes the
  multi-second first-run hang from the local FREE path.

### Changed (auto, lockstep) — track smartmemory-core==1.4.26 (1.4.26)
- Version copied from smartmemory-core 1.4.26 release (single-source lockstep).

### Added — lite daemon graph parity (DIST-OBSIDIAN-LITE-PARITY-1)
- Local daemon (`viewer_server` + `local_api`) now serves `GET /memory/{id}/lineage`
  (walks the `derived_from` chain, mirroring the hosted service) and
  `GET /memory/{id}/links` (incident edges in `{source_id, target_id, link_type}`
  shape). Previously missing, so the Obsidian lineage panel and graph link
  fallback 404'd against the daemon. `/health` capabilities now advertise
  `lineage`/`links` (true) and `decisions` (false, hosted-only).

### Fixed
- `reextract` logged via an undefined `logger` (NameError on failure) → use `log`;
  dropped an unused import.

### Changed (auto, lockstep) — track smartmemory-core==1.4.25 (1.4.25)
- Version copied from smartmemory-core 1.4.25 release (single-source lockstep).

### Changed (auto, lockstep) — pin smartmemory-core==0.9.48 (1.4.24)
- Automated lockstep sync triggered by smartmemory-core 0.9.48 release.


## [1.4.23] - 2026-06-04

### Security
- **Tenant-scope ingest-time alias resolution.** The opt-in `alias_resolve` stage scanned entities across the shared graph with no workspace filter, so when enabled a surface could resolve to another tenant's entity. Now workspace-scoped (core 0.9.46).

## [1.4.13 – 1.4.22] - 2026-06-02 → 2026-06-03

A run of entity-resolution and graph-quality releases (core 0.9.32–0.9.45). Highlights — not a per-bump log:

### Security
- **Search-result cache cross-tenant leak fixed.** The process-global Redis search cache now keys on tenant/workspace, closing a path where workspace A's cached results were served to workspace B for the same query (1.4.13 / core 0.9.32, SEC-CACHE-1).
- **Graph-maintenance ops no longer run cross-tenant.** `rename_entity_type` / `delete_by_run_id` silently ran UNSCOPED across every workspace (auto-scoping swallowed by a bare `except`); now scoped via `get_isolation_filters()` and fail-closed (1.4.17 / core 0.9.40, CORE-GRAPH-SCOPE-LEAK-1).

### Added
- **Cross-encoder rerank in semantic code search** (1.4.15 / core 0.9.36, CODE-RERANK-1).
- **`SmartMemory.resolve_aliases()`** — graph-maintenance op that merges unambiguous single-token aliases ("Hudson") into their multi-token canonical ("Rock Hudson"), abstaining on collisions; with opt-in collision disambiguation (1.4.14–1.4.18 / core 0.9.33–0.9.41).

### Changed
- Internal extraction-accuracy work: cross-extractor entity-node dedup, write-time entity-type coarsening, async-path canonical-key dedup, and a hierarchical, domain-tagged entity-type ontology (1.4.19–1.4.22 / core 0.9.42–0.9.45).

### Fixed
- `ontology_constrain` null-confidence crash that aborted ingests, and an empty-basis evaluation cycle overwriting a legitimate prior score with 0.0 (1.4.15 / core 0.9.34–0.9.36).

## [1.4.6] - 2026-05-28

### Fixed
- **MCP `memory_search` completely broken in local mode.** `storage.search()` rejected the `memory_type`, `enable_hybrid`, `decompose_query`, `multi_hop`, `max_hops`, and `budget_ms` kwargs the MCP server passes — every local-mode `memory_search` raised `TypeError`. Now accepts `memory_type` (post-filter) and an allowlisted `**search_kwargs`; unknown keys are dropped instead of raising. (2026-05-18.)
- **`smartmemory search` CLI crash** (`AttributeError: 'str' object has no attribute 'get'`). The daemon returns the `{"items": [...]}` contract shape but the CLI iterated the dict directly, so `r.get("content")` threw on the key string. Now unwraps `results["items"]` vs a bare list. (DEMO-WALKTHROUGH-1, 2026-05-17.)

## [1.4.3] - 2026-05-17

### Added
- **Bitemporal accuracy, attention fusion, vault sync, and recall citations** reach `pip install smartmemory` (core 0.9.8): CORE-BITEMPORAL-1 transaction-time activation, CORE-ATTENTION-FUSION-1 Phase 1, APP-VAULT-SYNC-1 Phase 0, RECALL-CITATIONS-1.

### Fixed (DEMO-WALKTHROUGH-1, lite-mode graph viewer SSE, 2026-05-17)

- **Lite daemon now serves `GET /memory/progress/stream` (SSE).** `smart-memory-graph`'s `useGraphStream` migrated WS→SSE; the lite daemon only exposed the legacy `sm.v1` WebSocket on `:9015`, so the local graph viewer never connected ("Not connected to event stream", 0 nodes).
  - `smartmemory_app/events_server.py`: SSE subscriber registry + `_to_progress_event()` reshaping raw sink items into ProgressEvent contract frames (`kind: graph.node|graph.edge`) + `_fanout_sse()` at the **single** existing `sink._q` drain point. The `ws://:9015` broadcast is byte-for-byte unchanged (SSE is an additional broadcast target, not a second queue consumer — preserves existing WS clients incl. the recorder). Cross-loop hand-off via `call_soon_threadsafe` (same bridge as `InProcessQueueSink.emit`).
  - `smartmemory_app/local_api.py`: `GET /memory/progress/stream` `StreamingResponse` (per-connection queue, 15s keepalive, disconnect cleanup), declared before `/{memory_id}` routes.
  - Verified: one ingest yields 6 `graph.node` + 10 `graph.edge` contract frames with `payload.data.memory_id`+`label`; legacy WS path unaffected.
- **Events server port now tracks the API port.** `smartmemory_app/viewer_server.py` starts the events WS on `port + 1` instead of a hardcoded `9015`. Default `9014 → 9015` is unchanged; a non-default daemon (e.g. an isolated demo on `9114`) now gets its own events server (`9115`) instead of colliding with the dev daemon's `:9015`. Enables running an isolated demo daemon alongside the launchd dev daemon.

### Added (LAUNCH-METRICS-1, Wave 2 Stream I, 2026-05-10)

- **`smartmemory_app.launch_metrics`** module — best-effort `emit(event_type, props)` POSTing to the daemon HTTP API at `/launch/event`. Honors `SMARTMEMORY_DISABLE_LAUNCH_METRICS=1` opt-out. Failures log WARNING; never raises into a CLI command path.
- Funnel emission wired into the three Stream A entry points:
  - `sm setup` / `sm init` -> `setup.complete` with `{mode: local|remote}`
  - `sm code index` -> `index.start` and `index.complete` with `{repo, files, entities, edges, elapsed_s}`
  - `sm mcp install <client>` -> `mcp.install` with `{client, dry_run}`
- **Daemon-side ingest** at `POST /launch/event` in `local_api.py`. Local mode appends to `launch_events.jsonl` in the data dir.

Contract: `smart-memory-docs/docs/features/LAUNCH-METRICS-1/launch-event-contract.json`.

## [1.4.2] — 2026-05-10

### Added (Launch Sprint Wave 1, Stream A)

- **`sm init`** — alias for `sm setup`. Same Click command registered under a second name; option/flag parity is automatic.
- **`sm code index <path>`** — wraps `SmartMemory.ingest_code()` from the core library to index Python (and optionally TypeScript) repos into the local knowledge graph. Streams structured progress to stdout (`[code:index] phase=start|done …`). Default exclusions cover `node_modules`, virtualenvs, build artifacts, and VCS dirs. Every entity is tagged with `origin = code:index` (Tier 1 user content per `smartmemory/origin_policy.py`) — set by the indexer itself, not the wrapper. Flags: `--repo`, `--language` (repeatable, `python|typescript`), `--exclude` (repeatable), `--commit-hash`.
- **`sm mcp install <client>`** — writes MCP config for `claude-code` (→ `~/.claude.json`), `cursor` (→ `~/.cursor/mcp.json`), or `codex` (→ `~/.codex/config.toml`). Pointer-only wrapper around the already-shipped `smartmemory-mcp` PyPI package — no new server logic. Flags: `--dry-run` prints the would-be config without touching disk; `--path` overrides the destination. Existing config files are merged (JSON) or section-replaced (TOML); malformed configs raise loudly rather than being clobbered.

### Changed (CORE-EXPERTISE-1 Phase 4b, 2026-05-08)

- **README "Memory Types" listing extended** with `Constraint Memory` and `Learned Memory` entries plus a new "Expertise vs knowledge" callout that explains the knowledge / expertise cohort split, points to the canonical 1-pager at `docs.smartmemory.ai/smartmemory/concepts/expertise-vs-knowledge`, and surfaces `mem.search(query, expertise=True)` for partitioned recall. No code change.

## [1.4.0] — 2026-05-04

### Fixed

- **HOOK-RECALL-RELEVANCE-1 (COMPLETE): Claude Code session-start and per-prompt hook recall payloads.** Empty `[type]` lines, duplicate concept lines, tier-4 hook noise, and unscoped recall all eliminated. **G1 (formatter primitives):** new `smartmemory_app/recall_format.py` provides `format_recall_lines()` (dedup by item_id then content, empty-content suppression, top_k cap), `derive_workspace_id()` (env-var → realpath(git-toplevel) → sha1[:12]), and `_trace()` (JSONL append at `~/.smartmemory/hook-recall.jsonl`). **G2 (recall paths):** both `storage.recall()` and `RemoteMemory.recall()` rewritten to share the formatter, apply `origin_policy.filter_by_tiers` (excludes tier 4 by default), and post-filter by `metadata.workspace_id`. **G3 (workspace propagation + retag):** daemon `/ingest` auto-derives `workspace_id` from request `cwd` and stamps it on `metadata.workspace_id`; new `--strict` recall flag (and `SMARTMEMORY_RECALL_STRICT` env var) drops legacy items with no `workspace_id` to kill cross-workspace leak; new `smartmemory retag --content X --origin seed:demo` CLI moves seed/fixture data to tier 4 in one shot (verified: 38 Alice/Atlas/Acme items retagged in dev DB). **G4 (hook scripts):** `~/.claude/hooks/smartmemory-session-start.sh` passes `workspace_id` + `include_snapshot=true`; `~/.claude/hooks/smartmemory-prompt-recall.sh` rewritten to route through the local daemon (single fix surface); `UserPromptSubmit` wired in `~/.claude/settings.json` for the first time. **G5 (integration tests):** 10 new tests in `tests/integration/test_hook_recall.py` covering no-empty-buckets, dedup, tier filter, workspace isolation, strict mode (param + env), failure-mode-empty, query mode, JSONL trace, seed origin filter — all green. 20 new unit tests + pre-existing recall_confidence + recall_flow suites green. Report: [`smart-memory-docs/docs/features/HOOK-RECALL-RELEVANCE-1/report.md`](../smart-memory-docs/docs/features/HOOK-RECALL-RELEVANCE-1/report.md).
- **`test_shutdown_calls_save_and_close` updated** to assert `SmartMemory.close()` (current code path) instead of stale `_graph.backend.close()`.

### Changed — BREAKING

- **CORE-MEMORY-DYNAMICS-1 M1b: `working` → `pending` rename (wrapper).** `smartmemory_app/lifecycle.py` distill path now writes `memory_type="pending"` (was `"working"`; the core validator rejects the old value post-rename). `smartmemory_app/cli.py` `_VALID_MEMORY_TYPES` set rebuilt with `"pending"`. `smartmemory_app/local_api.py` user_types tuple in SELECT-by-type query updated. Test fixture in `tests/integration/test_local_api_integration.py::test_node_fields_correct` updated. README evolver listing removed `WorkingToEpisodicEvolver` / `WorkingToProceduralEvolver` (retired in core M1b); "Working Memory" type description replaced with "Pending Memory".


### Added

- **CORE-PROPS-1 Phase 1b: Confidence surface wiring.** Recall excludes items below configurable confidence floor (default 0.3, `SMARTMEMORY_RECALL_FLOOR` env var). Low-confidence items (< 0.5) show `~` prefix in both CLI search and recall output. Remote recall path has parity with local path.
- **CORE-PROPS-1 Phase 2: Staleness wiring.** Stale items show `⚠` prefix in CLI search, recall, and remote recall. Wildcard search (`*`) now includes `confidence` and `stale` fields in results.
- **CORE-PROPS-1 Phase 6: Reference data filtering.** Recall always excludes `reference=True` items. Search excludes by default with `--include-reference` CLI flag. Wildcard search promotes `reference` to top-level key.

### Changed

- **DIST-QA-2: CLI cleanup.** `persist` renamed to `add`. Admin commands (`export`, `import`, `mine`, `convert-rebel`, `list-packs`, `install-pack`, `reindex`) moved under `smartmemory admin` subgroup.
- **Input validation.** `smartmemory add` rejects invalid `--type` values and empty content with clear error messages.
- **Wildcard search.** `smartmemory search "*"` returns all memory nodes (excludes entity/relation/pattern nodes from enrichment).
- **Admin reindex guard.** `smartmemory admin reindex` rejects with clear error in remote mode (local-only operation).
- **Auto-sync hooks on daemon start.** Hook scripts are recopied from the package to `~/.claude/hooks/` on every daemon start, so pip upgrades that change hook content take effect without re-running `smartmemory setup`.
- **`ingest` command removed.** `add` is now the single entry point — `ingest` was identical (both called `/memory/ingest`).

### Fixed

- **SQLite enrichment queue replaces in-memory threading.** New `enrichment_queue.py` persists Tier 2 jobs to SQLite. Separate `smartmemory worker` process drains queue — no threading, no `_drain_running` import bug, survives daemon restarts. Queue stats visible via `smartmemory status` and `/health`.
- **Daemon no longer crashes after ingest.** Disabled evolution worker in daemon pipeline profile — evolvers crash with missing typed configs (EpisodicDecayEvolver, ExponentialDecayEvolver datetime serialization). Evolution re-enabled when configs are wired properly.
- **Launchd plist has GROQ_API_KEY.** Daemon managed by launchd didn't inherit shell env vars — LLM enrichment was silently disabled.
- **Async enrichment no longer drops nodes on SQLite.** Tier 2 LLM entities used deterministic SHA256[:16] item_ids that could collide with existing nodes via SQLite's `ON CONFLICT DO UPDATE`. Entity IDs are now stripped before persist on backends without dual-node support.
- **Recall works with few memories.** Recency sort key returned `""` for `None` created_at, pushing items with missing timestamps to the end. Fixed to use `"0000-00-00"` fallback.

## [1.1.9] — 2026-03-27

### Changed

- **TUI is now a default dependency.** `textual` moved from the `[tui]` optional extra to the base install so the first-run setup TUI works out of the box. `smartmemory[tui]` still works for backward compat (no-op).

## [1.1.5] — 2026-03-24

### Fixed

- **Real-time graph viewer updates work.** The lite WebSocket events server now negotiates the `sm.v1` subprotocol; per RFC 6455 the browser closes the connection when the server doesn't echo back a requested subprotocol, so the viewer showed "disconnected" and node animations never fired.

## [1.0.10] — 2026-03-21

### Fixed

- **Search returns only matching results.** Lite mode search no longer returns unrelated memories. Text-first fallback chain (substring + keyword) replaces unreliable vector search on small corpora.
- **Add no longer drops nodes with LLM key.** Batch evolution disabled in Tier 1 config — destructive evolvers were deleting nodes during rapid sequential ingests.
- **Recall endpoint works.** `/recall` route moved before `/{memory_id}` wildcard to prevent 404 capture.
- **Recall returns results.** Recency sort and empty-query handling fixed in search fallback.

### Added

- **`smartmemory get <item_id>`** — retrieve a memory by ID via CLI.
- **Auto-restart on pip upgrade.** Daemon middleware checks installed package version every 10th request. Version mismatch triggers clean exit; launchd restarts with new code.
- **CLI retry on daemon restart.** `_daemon_request` retries once with 2s wait on connection drop for seamless upgrades.
- **Arbitrary properties.** `smartmemory add "text" --project atlas --domain legal` passes extra properties.

## [1.0.5] — 2026-03-19

### Added

#### DIST-SETUP-TUI-1 — Interactive Setup TUI (COMPLETE)

- **Textual TUI for `smartmemory setup`**: Arrow-key selection for mode, LLM provider, embedding provider. 6 screens: Welcome, LLM, Model Discovery, Embedding, Summary, Progress.
- **Live model discovery**: `@work` async worker fetches available models from ollama/lmstudio with loading indicator and graceful failure.
- **Summary screen**: Edit data directory, toggle coreference, review all choices before confirming.
- **Progress screen**: Per-step checklist with indeterminate spinner for daemon startup.
- **Graceful fallback**: Non-interactive environments (pipes, CI, `TERM=dumb`, missing textual) fall back to existing click prompts automatically.
- **Optional dependency**: `pip install smartmemory[tui]` adds Textual. Base install falls back to click.
- **`SetupResult` dataclass**: Clean contract between TUI and business logic.
- **`_can_run_tui()`**: Detects interactive terminal availability.
- **`_apply_setup_result()`**: Shared post-config logic with `on_step` callback for per-step progress updates.

### Fixed

- **`_seed_data_dir()`**: Now accepts explicit `data_dir` parameter with `expanduser()`. Previously ignored user-selected directory from setup.

## [1.0.4] — 2026-03-19

### Added

#### DIST-DAEMON-1 — Async Background Enrichment (COMPLETE)

- **Two-tier ingest pipeline**: When LLM API key is available, `POST /memory/ingest` runs Tier 1 (spaCy + EntityRuler, ~4ms) synchronously and returns immediately. Tier 2 (LLM extraction via `process_extract_job()`) runs in a background drain thread, progressively improving extraction quality from 96.9% to 100% E-F1.
- **`AsyncEnrichmentQueue`**: Thread-safe bounded deque (`maxlen=10_000`) with enqueue/dequeue_all/wait/clear/stats. Overflow drops oldest items and tracks `total_dropped`.
- **Background drain thread**: Daemon thread in `viewer_server.py` processes queued items one at a time, acquiring `_rw_lock` to serialize with API endpoints. Graceful shutdown via `_stop_event`. Re-fetches SmartMemory singleton per item to handle `/clear` invalidation.
- **Health endpoint enrichment stats**: `/health` now includes `async_enrichment` object with `enabled`, `pending`, `total_enqueued`, `total_processed`, `total_failed`, `total_dropped`.
- **`smartmemory status` enrichment display**: Shows enrichment queue stats when drain thread is active.
- **Thread safety hardened**: All read endpoints (`/graph/full`, `/graph/edges`, `/list`, `/{id}/neighbors`, `/{id}`) and `/health` now acquire `_rw_lock` for backend access.

### Changed

- **`storage.ingest()`**: Now accepts `sync` parameter. Passes `memory_type` via `context={"memory_type": ...}` instead of loose kwarg (fixes memory_type not being preserved for raw string inputs).
- **`SmartMemory.ingest(sync=False)`**: Now returns `entity_ids` in result dict alongside `item_id` and `queued`.

## [1.0.3] — 2026-03-16

### Fixed

- **Hook safety**: Namespaced hook files (`smartmemory-session-start.sh` etc.) so `smartmemory setup` never clobbers other apps' hooks. Migrates legacy registrations in `settings.json`.
- **Hook registration format**: Updated to current Claude Code hooks API format (`{matcher, hooks: [{type, command}]}`).
- **Wheel packaging**: Added `hooks/*.sh` and `skills/*.md` to `pyproject.toml` includes — were missing from built wheels.

### Added

- **`smartmemory server`** command — starts MCP server (with events-server in background).
- **`smartmemory clear`** command — deletes all local memories and resets vector index.
- **Pinned embedding provider**: `embedding_provider` config field (`local`/`openai`/`ollama`) prevents env var changes from silently switching providers and causing dimension mismatches.

### Changed

- **Default to local**: Setup question 1 defaults to local mode.
- **`events-server`** command hidden from help (still accessible for debugging).

## [1.0.2] — 2026-03-05

### Changed

- Bumped `smartmemory-core[lite]` minimum to `>=0.5.4` (DIST-QA-1: fixes Version node duplication in search results, adds `get()` to usearch backend for embedding retrieval).

#### CORE-EXT-2 — Unified PatternManager Backend (COMPLETE)

- **`LitePatternManager` deleted** from `smartmemory_app/patterns.py`. Replaced by `JSONLPatternStore`, a `PatternStore`-protocol-conformant class with atomic `FileLock`-protected writes and source provenance preservation on update.
- **`storage.py`**: `get_memory()` now constructs `PatternManager(store=JSONLPatternStore(data_path))` instead of `LitePatternManager(data_path)`.
- **`setup.py`**: `_seed_data_dir()` instantiates `JSONLPatternStore(data_dir)` (seeds `entity_patterns.jsonl` on first run via `__init__` side-effect).
- **`JSONLPatternStore.save()`**: source field is written on CREATE only — not overwritten on frequency increment updates. Matches FalkorDB `ON MATCH SET` provenance semantics.
- 108 unit tests + 5 integration tests green; zero `LitePatternManager` references remain in source or test files.

---

## [1.0.1] - 2026-03-01

### Fixed

- Bumped `smartmemory-core[lite]` minimum to `>=0.5.3` (fixes pymongo lazy import, VectorStore lazy init, and no-op VersionTracker for SQLiteBackend).

## [1.0.0] - 2026-03-01

### Breaking Changes

- **Python 3.11+ required.** Dropped Python 3.10 support (intentional product decision — `tomllib` stdlib, `asyncio.TaskGroup` alignment).
- **Package renamed** from `smartmemory-cc` to `smartmemory`. CLI entry point renamed from `smartmemory-cc` to `smartmemory`.
- **Core library renamed** from `smartmemory` (PyPI) to `smartmemory-core`. Python import name (`import smartmemory`) is unchanged.

### Added

#### DIST-LITE-5 — Setup, Config & Dual-Mode Backend

- `config.py` — XDG-aware config at `~/.config/smartmemory/config.toml`. `SmartMemoryConfig` dataclass, `load_config()` / `save_config()` (UTF-8 explicit), env var overlay, `UnconfiguredError`, `get_api_key()` / `set_api_key()` with OS keychain via `keyring`, `_detect_and_migrate()` for upgrade path. Mode validation: invalid `mode=` in config file warns and returns unconfigured; invalid `SMARTMEMORY_MODE` env var raises `ValueError` immediately.
- `remote_backend.py` — `RemoteMemory` class: httpx client wrapping the hosted API. Same MCP tool interface as local storage (`ingest`, `search`, `get`, `recall`), plus graph methods for the viewer (`get_graph_full`, `get_edges_bulk`, `get_neighbors`). `_request()` never raises. `get_neighbors()` normalises response to always include `edges` key (service omits it). `recall()` deduplicates on both `item_id` and `id` field names. `login()` persists `team_id` to config after successful auth.
- `storage.py` — dual-mode dispatch: `get_memory()` returns local `SmartMemory` or `RemoteMemory` based on config. Unconfigured state raises `UnconfiguredError` (upgrade auto-migration attempted first). All four operations (`ingest`, `search`, `get`, `recall`) have explicit remote branches — no duck-typing across asymmetric return types (`MemoryItem` vs `dict`, `sort_by` support). `filelock` imported lazily inside `ingest()` only (not available in base install).
- `local_api.py` — `_get_mem()` wrapper: `UnconfiguredError` → HTTP 503 with setup instructions; `ValueError` (invalid mode) → HTTP 400. `_get_backend()` routes through `_get_mem()` so all local paths share the 503 guard. All viewer endpoints dispatch to `RemoteMemory` graph methods in remote mode.
- `setup.py` — `smartmemory setup` questionnaire: choose local or remote mode, install `[local]` deps on demand (uv or pip), ask pipeline questions (coreference, LLM provider, data dir), write config, wire Claude Code hooks (local mode) or validate API key + store in keychain (remote mode).
- `server.py` — `login`, `whoami`, `switch_team` MCP tools (delegate to `RemoteMemory` in remote mode; no-op in local).
- `pyproject.toml` — `[local]` extra: `smartmemory-core[lite]>=0.3.1` + `filelock>=3.12`. Base install adds `httpx`, `keyring`, `tomli-w`. `requires-python` raised to `>=3.11` (intentional — `tomllib` stdlib).
- Tests — 102 unit tests (was 78): `test_config.py` (21), `test_remote_backend.py` (13 new), plus coverage for 503/400 error paths, remote dispatch branches, singleton isolation, and auth tool delegation.

### Added

#### DIST-LITE-4 — Loginless Pip-Bundled Graph Viewer

- `local_api.py` — FastAPI sub-app mounted at `/memory`; `GET /graph/full`, `POST /graph/edges`, `GET /list`, `GET /{id}/neighbors`, `GET /{id}`. `_flatten_node()` promotes `node_category`, `entity_type`, and other viewer fields from SQLite's nested `properties` blob to top-level. Route ordering ensures `/{id}/neighbors` resolves before `/{id}`.
- `viewer_server.py` — module-level `app = _build_app()` (side-effect-free); mounts `local_api` at `/memory` and serves built static assets via `StaticFiles(html=True)`. `main()` calls `start_background()` (idempotent) then `uvicorn.run()`.
- `static/index.html` — built viewer bundle (replaced placeholder by `make build-viewer`); hatchling picks it up via `smartmemory_cc/static/**/*` in `[tool.hatch.build.targets.wheel].include`.
- `cli.py` — `viewer [--port N] [--no-browser]` command with lazy import matching the `events-server` pattern.
- `pyproject.toml` — added `fastapi>=0.110`, `uvicorn>=0.29` dependencies; added static asset glob to wheel include list.
- Option B delete endpoints (`DELETE /{id}` and `DELETE /graph/nodes/{id}` → 405) added because `GraphExplorer` has no `readOnly` prop.

#### DIST-LITE-3 — Lite Mode Graph Viewer Animations (Phase 2 — plugin)

- `event_sink.py` — process-singleton `get_event_sink()` with double-checked locking; safe for concurrent calls from main thread (`get_memory()`) and daemon thread (`_serve()`).
- `events_server.py` — asyncio WebSocket broadcaster on port 9004. `start_background()` spawns a daemon thread; `_server_thread_lock` makes it idempotent under concurrent callers. Loop captured inside `_serve()` after `asyncio.run()` starts it; `sink.attach_loop(None)` called in `finally` regardless of exit path. `asyncio.wait_for(timeout=1.0)` allows `stop_event` check on idle queue. `return_exceptions=True` in `asyncio.gather` so a broken client never kills the broadcast loop. `OSError` on bind logged as warning, not raised.
- `_to_viewer_message()` — reshapes raw sink queue items to the `new_event` schema expected by `classifyEvent.js`: `type`, `component`, `operation`, `name` at top level; node/edge payload nested in `data`.
- `storage.py` — `get_memory()` now passes `event_sink=get_event_sink()` to `create_lite_memory()`.
- `server.py` — `main()` calls `start_background()` before `mcp.run()`; wrapped in `try/except` so MCP always starts even if the port is unavailable.
- `cli.py` — `smartmemory-cc events-server [--port N]` command for standalone debug use.
- `pyproject.toml` — `websockets>=12.0` added to dependencies.

## [0.1.0] — 2026-02-23

### Added

- **LitePatternManager** (`smartmemory_cc/patterns.py`) — JSONL-backed entity pattern store
  duck-typing `PatternManager.get_patterns()` for `EntityRulerStage`. Bundled seed patterns,
  frequency gate (≥2), atomic rewrite via `.tmp`. Graceful degradation when seed file absent.

- **Storage singleton** (`smartmemory_cc/storage.py`) — double-checked-locked `get_memory()`,
  filelock-protected `ingest()`, `_data_path` module var so lock and singleton always share the
  same directory. `_shutdown()` logs warnings on save failure (never silent data loss).

- **FastMCP server** (`smartmemory_cc/server.py`) — `memory_ingest`, `memory_search`,
  `memory_recall`, `memory_get` tools; all error-safe (return strings, never raise).

- **CLI** (`smartmemory_cc/cli.py`) — `persist`, `ingest`, `recall`, `setup`, `uninstall`
  commands. Entry point: `smartmemory-cc`.

- **Setup/teardown** (`smartmemory_cc/setup.py`) — idempotent hook registration via
  `_get_hook_registrations()` (lazy, patchable in tests), skill copy with no-overwrite,
  `SMARTMEMORY_DATA_DIR` env var honoured throughout.

- **Session hooks** — `session-start.sh` (recall on session open), `session-end.sh`
  (persist last assistant message), `post-tool-failure.sh` (ingest tool errors, skips interrupts).
  All hooks exit 0 and redirect stderr to `~/.smartmemory/hooks.log`.

- **Skills** — `/remember`, `/search`, `/ingest`, `/orient`.

- **Sync stub** (`smartmemory_cc/sync.py`) — raises `NotImplementedError` if
  `SMARTMEMORY_SYNC_TOKEN` set, silent no-op otherwise.

- **Test suite** — 31 unit tests + 12 integration tests (real SQLite + usearch, no mocks).

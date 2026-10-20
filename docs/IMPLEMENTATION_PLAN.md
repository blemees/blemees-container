# blemees-container — Implementation plan

This document organizes [SPEC §12](SPEC.md#12-implementation-order)'s
12 ordered steps into shippable phases, with cross-repo dependencies,
parallelization opportunities, and risk hotspots called out.

The spec is the source of truth for *what* to build; this plan is the
source of truth for *what order* to build it in and how the work
splits across repos (`blemees-agent`, `blemees-peer`, `blemees-tui`,
`blemees-container`).

## Phase 0 — Foundations (cross-repo prep)

Nothing user-visible; downstream phases unblock.

- **Step 1.** Codify the runtime/state path conventions in
  `blemees-agent`, `blemees-peer`, `blemees-tui` path-resolution
  helpers. Add a shared discovery helper that scans
  `$XDG_RUNTIME_DIR/blemees/agentd.sock`,
  `$XDG_RUNTIME_DIR/blemees/peerd.sock`, and
  `$XDG_RUNTIME_DIR/blemees/containers/<name>/agentd.sock`.
- **Step 2.** In `blemees-agent`:
  - Verify env passthrough of `BLEMEES_AGENT_HOME`, `BLEMEES_AGENT_SID`,
    `BLEMEES_AGENT_ALIAS` to spawned backend processes (`claude`,
    `codex`).
  - Migrate session persistence from `<event_log_dir>/<session>.jsonl`
    to per-session directories at
    `$XDG_STATE_HOME/blemees/sessions/<id>/{meta.json, events.jsonl,
    usage.json, …}` per [SPEC §7.1](SPEC.md#71-blemees-agent-changes).

Step 2 is the largest piece in this phase.

## Phase 1 — "Agent runs in a container"

Spec milestone 1: a containerized agentd reachable from existing
tooling. No auth, no resume.

- **Step 3.** `ContainerEngine` Protocol per
  [SPEC §11](SPEC.md#11-engine-driver-protocol) plus the docker
  reference driver. Auto-detection. Mount / env / cp helpers.
- **Step 4.** `blemees-container` CLI core: `start` (without auth or
  resume yet), `stop`, `list`, `rm`, `exec`, `logs`. Uses the docker
  driver. Implements the create-then-cp-then-start sequence so
  Phase 2 has the seam to inject copy-in. Includes `meta.json`
  schema, name validation, UID alignment, runtime-vs-state dir split.
- **Step 5.** Reference image and entry-point script at
  `blemees-container/examples/`. Dockerfile installs `blemees-agent`,
  `blemees-peer`, `claude`, `codex`. Entry-point waits for
  `/run/blemees/.ready` then `exec`s `blemees-agentd`; on a 30s
  timeout it exits 75 without starting the agentd (fail closed, per
  [SPEC §9](SPEC.md#9-reference-image)). Smoke test:
  `start` → connect via host CLI → close → `stop` round-trip, plus a
  missing-sentinel case that asserts exit 75 and no `agentd.sock`.

## Phase 2 — "Agent CLIs authenticate"

Spec milestone 2: containers run real agents until access-token TTL;
the refresh token never crosses the boundary.

- **Step 6.** `--auth claude|codex` access-only extractors and
  `--auth-file` verbatim mode per
  [SPEC §3.5](SPEC.md#35-agent-authentication-files). Per-provider
  helpers (one Python module each), pre-flight expiry checks. Drops
  the `.ready` sentinel after copy-in is complete.

## Phase 3 — "Sessions are portable"

Spec milestone 3 — the headline cross-environment feature.

- **Step 7.** `--resume`, `--no-capture`. Session lockfiles with
  stale-takeover semantics per
  [SPEC §3.6](SPEC.md#36-session-resume--state-lifecycle): a sibling
  `<id>.lock` (not inside `<id>/`), created with `O_EXCL` under the
  `flock` guard `.lock-guard`, and live while its PID is a live
  `blemees-container` process or its engine container has started and still exists.
  The lock records `engine_host`; a lock from another engine host is
  held, not stale. `meta.json` records the same `engine_host` and
  `engine_context`. The driver's `pin()` pins every engine call of
  `start` after the lock is written, of `stop`, `rm`, `exec` and
  `logs`, and the probe of `list` to that host
  ([SPEC §11 *Pinning*](SPEC.md#11-engine-driver-protocol));
  `stop`, `rm`, `exec` and `logs` refuse if `meta.json` has no
  `engine_host` or disagrees with the lock
  ([SPEC §4.2 step 0](SPEC.md#42-stop)). `list` writes `state` only
  from a pinned probe answer when the current endpoint is the
  recorded host ([SPEC §4.3](SPEC.md#43-list)).
  Sequenced wait+cp. `stop` takes the session lock with its own PID
  before capture, then the rename-aside capture commit with per-PID
  temp names and crash recovery (`<id>` → `<id>.old.<pid>`,
  `<id>.tmp.<pid>` → `<id>`, delete `.old.<pid>`). A failed copy-out
  keeps the lock and the container. Tests: two concurrent acquires
  → exactly one wins; stale takeover; the lock stays live after
  `start` exits while the container exists; a failed copy-out keeps
  the lock and a second `stop` retries; capture over a non-empty
  pool entry; a crash between the two renames (roll forward with
  `.tmp.<pid>`, roll back without); `start --resume` does not touch
  the leftovers of a live PID; two concurrent stops; a second
  `stop --rm` while the first holds the lock does not remove the
  container; `stop` with `<id>/` missing keeps the container;
  `start` writes the same `engine_id` to `meta.json` and the lock,
  and the next `stop` matches it and captures; a container that
  never started does not keep the lock live, and its `stop` skips
  the copy-out and releases the lock; `stop --no-capture` releases
  the lock; a lock with another `engine_host` and a dead PID is held
  (`start --resume` refuses, the engine is not queried); a lock with
  the same `engine_host`, a dead PID and the container gone is stale
  and taken over; a valid lock with no `engine_host` is held; an
  `endpoint()` that fails or times out makes the lock live;
  `endpoint()` precedence (`DOCKER_HOST` over the context;
  `CONTAINER_CONNECTION` over `CONTAINER_HOST` over the default
  connection); podman on Linux with a default connection but remote
  mode off gives `local`; podman in remote mode with no default
  connection gives its default API socket; an `engine.remote` set in
  a later containers.conf layer wins, and an unreadable layer makes
  `endpoint()` fail; the nerdctl endpoint includes the
  namespace and takes `address` / `namespace` from `nerdctl.toml`
  when the env is unset (the env wins over the toml).
  Pinning tests (a fake engine records the argv and env of each
  call): `start` writes the same `engine_host` and `engine_context`
  to `meta.json` and the lock; a context switch between the lock
  write and `create` still sends `create`, `cp` and `start` to the
  lock's host; `stop` after a context switch to a host with a
  container of the same name waits on, copies out of and (with
  `--rm`) removes the recorded host's container, warns, and releases
  the lock; `rm` after a switch does the same; the lock check's
  `inspect` is pinned; `meta.json` and the lock disagree on
  `engine` or `engine_host` → `stop` and `rm` refuse in step 0 with
  no engine call and no file change; `meta.json` without
  `engine_host` → they refuse the same way; a lock that changes to a
  mismatch between `stop`'s step 0 and step 3 is not taken, and
  `stop` skips capture and keeps the lock and the container; the
  same change before `rm`'s second lock check keeps the lock and the
  state dir; the `stop` SIGTERM/SIGKILL calls are pinned; a pinned
  podman `--url` call carries `--identity` when `CONTAINER_SSHKEY` is
  set; a pinned docker
  call has `--host` and no `--context`, and its env has no
  `DOCKER_HOST` or `DOCKER_CONTEXT`; podman uses `--connection` when
  `engine_context` is set, refuses if that connection was removed or
  now has another URI, and uses `--url` when it is null; podman
  `local` passes no flag and refuses if `endpoint()` is no longer
  `local`; nerdctl passes `--address` and `--namespace` (only
  `--namespace` for a `local` address) and has no `CONTAINERD_*`
  vars in the env; a pin that fails after `create` keeps the lock
  and the state dir.
  `list` / `exec` / `logs` pinning tests (same fake engine):
  `exec` and `logs --source engine|agentd` after a context switch
  to a host with a container of the same name run on the recorded
  host A, not on B's container, and warn; `exec` and `logs` refuse
  in step 0 with zero engine calls and no file change when
  `meta.json` has no `engine_host`, when it and the lock disagree,
  and when the pin cannot be built; `list` after a switch makes no
  engine call for A's container and writes no `state` into its
  `meta.json` (no `dead`, not B's state), shows the stored state,
  warns, and exits zero; `list` with a pin that cannot be built, or
  a pinned probe that fails, writes no `state` and still lists the
  other containers; `list` on the recorded host updates `state`
  through the pinned probe (argv carries the pin and the
  `engine_id`, not the name); `list` with a null `engine_id` makes
  no engine call and no warning; a pinned probe that answers
  `created` writes no `state`; a same-name container on the same
  host with another engine ID does not change `state`; `list`
  compares the current endpoint of each `meta.json`'s own `engine`;
  `logs --source container`, and `logs` without `--follow` on a
  stopped container with a captured `engine.log` / `agentd.log`,
  make no engine call and work when the pin cannot be built; `logs
  --follow --source engine` with a captured file still makes the
  live pinned call; with no captured file, `logs --source engine`
  makes the live pinned call; step 0 warnings and refusals go to
  stderr and stdout holds only the command output or log text.

## Phase 4 — "Unified TUI"

Spec milestone 4 — one TUI managing many agentds with first-class
container lifecycle.

- **Step 8.** Replace the single client in `blemees-tui` with a
  `ConnectionRegistry` per
  [SPEC §7.3.1](SPEC.md#731-multi-agentd-connection-registry). Tag
  events with their source. Discovery + watch via
  inotify/kqueue/fsevents, falling back to a 2-second poll. No
  visible UX change yet — the sidebar stays flat.
- **Step 9.** Sidebar grouped by home, source badges, collapse/expand,
  updated keybindings per
  [SPEC §7.3.3](SPEC.md#733-sidebar-grouped-by-home). Container
  lifecycle commands (`:stop`, `:rm`, "new container session" prompt)
  per [SPEC §7.3.5](SPEC.md#735-container-lifecycle-control-from-the-tui).
  Persistence updates per
  [SPEC §7.3.4](SPEC.md#734-persistence).

## Phase 5 — Broader engine support

- **Step 10.** Extend the docker driver to support podman and
  nerdctl. Parameterize the binary name; smoke-test each.

Apple's `container` runtime is deferred per
[SPEC §10](SPEC.md#10-out-of-scope-for-v01) pending empirical
validation that Unix sockets pass through its virtio-fs cleanly.

## Parallelization and dependencies

- **Phase 0 / Phase 1 overlap.** Step 2 (agent persistence migration)
  can run in parallel with Phase 1's steps 3–5. It is a hard
  prerequisite for Phase 3 — resume relies on the per-session
  directory layout being in place.
- **Step 3 unblocks 4–7.** The engine driver protocol is the only
  blocker for the CLI work; once it lands, steps 4 → 5 → 6 → 7
  proceed linearly.
- **Phase 4 is code-independent** of Phases 2–3, but UX-wise wants
  them done first — otherwise the "new container session" affordance
  is half-functional (no auth, no resume).
- **Phase 5 is fully independent** of Phase 4 and can be picked up
  whenever someone needs podman or nerdctl support.

## Risk hotspots

- **Step 2 (session persistence migration).** Touches the agent's
  hot path. Budget time for follow-on test churn in `blemees-tui`
  since its tests exercise the agent contract.
- **Step 6 (auth extractors).** Depends on real upstream CLI auth
  schemas, which drift. Build with a test fixture per provider so
  drift is detectable.
- **Step 9 (TUI tree + lifecycle).** The largest UI change in the
  suite to date — tree render, source badges, collapse/expand state,
  new modal for container creation, lifecycle commands.

## Milestone summary

| Milestone | Phases | Capability |
|---|---|---|
| 1 | 0–1 (steps 1–5) | Containerized agentd reachable from host CLI / TUI |
| 2 | 2 (step 6) | Agent CLIs authenticate via access-token copy-in |
| 3 | 3 (step 7) | Sessions move between host and container |
| 4 | 4 (steps 8–9) | Unified TUI with container lifecycle |
| 5 | 5 (step 10) | podman / nerdctl support |

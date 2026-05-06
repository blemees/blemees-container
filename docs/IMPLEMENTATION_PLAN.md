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
  `/run/blemees/.ready` then `exec`s `blemees-agentd`. Smoke test:
  `start` → connect via host CLI → close → `stop` round-trip.

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

- **Step 7.** `--resume`, `--no-capture`. PID-keyed lockfiles with
  stale-takeover semantics per
  [SPEC §3.6](SPEC.md#36-session-resume--state-lifecycle). Sequenced
  wait+cp, atomic replace.

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

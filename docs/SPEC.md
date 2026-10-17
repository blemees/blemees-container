# blemees-container — Containerized agent environments

**Version:** 0.1
**Language:** Python 3.11+, stdlib only (subprocess wraps `docker` / `podman`)
**Target OS:** Linux, macOS

A new sibling in the suite alongside
[`blemees-agent`](https://github.com/blemees/blemees-agent),
[`blemees-peer`](https://github.com/blemees/blemees-peer), and
[`blemees-tui`](https://github.com/blemees/blemees-tui).
`blemees-container` lets users run `blemees-agentd` inside a sandboxed
container, with full participation in the existing operator and peer
channels. From the operator's perspective (TUI, ctl) and from the
peer mesh's perspective, a session running inside a container is
indistinguishable from one running on the host — the container is
just where the agent process happens to live.

This document also specifies the cross-cutting changes needed in
**`blemees-agent`**, **`blemees-peer`**, and **`blemees-tui`** to
make this work.

> **Status:** v0.1 design. No code yet.

---

## 1. Architecture

```
                                 ┌────────────────┐
                                 │      TUI       │
                                 └────────┬───────┘
                                          │ blemees-agent/1 (operator)
                  ┌───────────────────────┼─────────────────────────────┐
                  │                       │                             │
                  ▼                       ▼                             ▼
          ┌──────────────┐        ┌──────────────┐              ┌──────────────┐
          │ blemees-     │        │ blemees-     │              │ blemees-     │
          │ agentd       │        │ agentd       │              │ agentd       │
          │ (host)       │        │ (container   │              │ (container   │
          │              │        │  "test-env") │              │  "scratch")  │
          └──────┬───────┘        └──────┬───────┘              └──────┬───────┘
                 │ blemees-peer/1        │ blemees-peer/1              │ blemees-peer/1
                 └───────────────┬───────┴─────────────────────────────┘
                                 ▼
                          ┌──────────────┐
                          │blemees-peerd │  routes + persists
                          └──────────────┘
```

`blemees-container` itself is **not** a daemon and not on this
diagram — it's a CLI that uses the host's container runtime
(`docker`/`podman`) to spawn agentd containers. Container lifecycle is
delegated to the runtime; `blemees-container` just composes the right
invocation and conventions.

The TUI connects to *every* agentd it can find on the host's runtime
dir — host's plus every container's — and merges the resulting
session lists into one view.

## 2. Runtime socket layout

Per-user runtime layout under `$XDG_RUNTIME_DIR/blemees/`:

```
$XDG_RUNTIME_DIR/blemees/
├── peerd.sock                   ← peer mesh (one per user)
├── agentd.sock                  ← host agentd (optional; absent if user runs only containers)
└── containers/
    ├── <name-1>/
    │   ├── agentd.sock          ← container <name-1>'s agentd
    │   └── meta.json            ← metadata written by blemees-container
    └── <name-2>/
        ├── agentd.sock
        └── meta.json
```

Conventions:

- **`<name>`** matches `^[a-z][a-z0-9_-]{0,63}$` — same grammar as a
  peer alias. Names are unique within the runtime dir.
- **`agentd.sock`** lives at a fixed offset within the container's
  runtime subdirectory so the bind-mount target is predictable.
- **`meta.json`** is written by `blemees-container start` and read by
  `blemees-container list`, the TUI, and anything else that wants to
  enumerate environments. Schema in §6.
- **`peerd.sock`** stays at the top level — there is exactly one peer
  mesh per user, regardless of how many agentd environments exist.
- **`agentd.sock`** at the top level is optional; users who run only
  containerized agents won't have it. Clients must handle its absence
  gracefully.

Naming convention for the container subdirectory matches the peer
addressing alias grammar so `blemees-container list` output and TUI
labels can use the same identifiers as `peer_set_alias` claims —
useful but not load-bearing.

## 3. Container launch contract

### 3.0 Lifecycle model: one session per container

**A container hosts exactly one session for its lifetime.** Starting
a container creates (or resumes) one session; stopping the container
captures that session's state and tears the container down.

This is the central simplifying assumption of the design. Reasons:

- **Isolation is the point of containers.** If multiple sessions
  shared a container, a misbehaving session could read another's
  files, exhaust the auth token, or destabilize the container's
  process tree. Every session getting its own container preserves
  the isolation guarantee.
- **Auth becomes tractable.** With one session per container, the
  access-only auth model (§3.5) maps cleanly: the container's auth
  TTL bounds the session's lifetime, period.
- **Lifecycle is dead simple.** "Container running" ⇔ "session
  active." No need to track session-to-container bindings or worry
  about the container outliving its sessions.

Consequence: **opening a new session in a containerized environment
means starting a new container.** The TUI's "new session" affordance
that today spawns a session on the host becomes a "new
containerized session" affordance that calls `blemees-container
start` under the hood (see §7.3).

The agentd inside the container is therefore a degenerate
single-session daemon. It implements the same `blemees-agent/1`
operator protocol as the host agentd, but it only ever serves one
session across its lifetime.

### 3.1 Mounts

| # | Host path | Container path | Mode | Purpose |
|---|---|---|---|---|
| 1 | `$XDG_RUNTIME_DIR/blemees/containers/<name>/` | `/run/blemees/` | rw, dir | Container's agentd writes `agentd.sock` here, visible on host |
| 2 | `$XDG_RUNTIME_DIR/blemees/peerd.sock`         | `/run/blemees/peerd.sock` | rw, file-bind | Peer mesh access from inside the container |
| 3 | `<host-project-dir>` (resolved absolute)      | *user-chosen* (default `/workspace`) | rw, dir | Project files the agent operates on |

Mount #2 must be a **file bind-mount** layered on top of mount #1 —
on Docker / `podman` this is `--mount type=bind,source=...,target=...`
where `source` is a regular file. Both daemons (Docker, podman)
support this. The container engine creates a "phantom" file at the
target if it doesn't already exist, then overlays it with the
host's socket.

The two-mount scheme (peerd.sock as a separate file mount, not
exposed via a parent-dir mount) is intentional: each container sees
**only its own** `containers/<name>/` subdirectory, never sibling
containers' agentds or the host's `agentd.sock`. The trust boundary
is "same-user same-machine"; the two-mount form keeps containers
from scanning each other through the filesystem even though peer
addressing already exposes their identities.

### 3.2 Environment

Every blemees-container-launched container receives:

| Variable | Value | Purpose |
|---|---|---|
| `XDG_RUNTIME_DIR` | `/run` | So clients inside the container resolve `/run/blemees/peerd.sock` etc. via the standard XDG fallbacks |
| `BLEMEES_AGENT_HOME` | host-canonical form of the project dir (per `blemees-peer` SPEC §2 normalization) | Anchors the peer-mesh identity to the *host's* path, not the container's bind-mount target. Critical for cross-environment addressing — see §5 |
| `BLEMEES_AGENT_SID` *(optional)* | `sess_` + 16 url-safe base64 chars | If set, every agent in this container shares one session ID; usually left unset |
| `BLEMEES_AGENT_ALIAS` *(optional)* | alias | If set, the container's agents auto-claim this alias on hello |

Internal binaries (`blemees-agentd`, `blemees-peer-mcp`, agent CLIs
like `claude`/`codex`) consume these env vars via the conventions
already documented in their respective specs.

### 3.3 The agentd inside the container

The image is expected to either:

1. **Run `blemees-agentd` as PID 1 / its entry point.** Most natural;
   container's main job is to host an agentd. The user sets up a
   `Dockerfile` that installs `blemees-agent` and sets
   `ENTRYPOINT ["blemees-agentd"]`.
2. **Run a supervisor that includes blemees-agentd plus other
   processes.** Allowed but out of scope here.

Either way, the agentd binds the socket at `/run/blemees/agentd.sock`
(its default given `XDG_RUNTIME_DIR=/run`). The agents it spawns
inherit `BLEMEES_AGENT_HOME` and friends from the container env.

### 3.4 UID alignment

The container process needs write access to:

- `/run/blemees/` (mount #1) — to bind `agentd.sock`
- `/run/blemees/peerd.sock` (mount #2) — to connect

Both belong to the host user. Two strategies:

- **Run the container as the user's UID** via `--user $(id -u):$(id -g)`.
  Requires the image to either not need root for setup or to have
  the relevant directories pre-chowned. Recommended.
- **Use rootless `podman` / `docker`**. Achieves the same by mapping
  the container's root to the user's UID. Convenient if the user is
  already running rootless.

`blemees-container start` defaults to `--user`; an `--as-root` flag
overrides for images that need it. UID mismatch produces a clear
error, not a silent failure to bind.

### 3.5 Agent authentication files

Most agent CLIs are authenticated against an upstream provider —
`claude` against Anthropic, `codex` against OpenAI — and store
credentials on disk under the user's config directory. A
freshly-started container has none of that, and re-authenticating
inside the container would require a browser dance that doesn't
work in a sandboxed agent process.

**Mechanism: access-token-only copy-in at start; never copy back
out; never share the refresh token.** `blemees-container` extracts
the *access* token (and only non-secret identifying claims) from
the host's credential file, writes that into the container's
filesystem before the agent starts, and at no point copies any auth
state back out. The container's lifetime is bounded by the access
token's TTL.

Why access-only, not refresh-only:

- **Refresh tokens rotate.** Anthropic's and OpenAI's auth flows
  both rotate the refresh token on each refresh exchange. If we
  shared the refresh token with a container and the container did
  a refresh, the host's refresh token would silently become invalid
  — re-authenticating the user out of nowhere. Sharing the refresh
  token at all means accepting that risk.
- **Access tokens don't rotate; they expire.** Sharing an access
  token has a clear, time-bounded blast radius: until expiry. There
  is nothing the container can do with it that affects the host's
  credentials.
- **No refresh in the container, by construction.** Without a
  refresh token, the CLI inside the container can't refresh. When
  the access token expires, the agent's API calls start failing —
  the session ends. This is the intended behavior: containers are
  short-lived auth boundaries.

Why copy (rather than bind-mount):

- Auth dirs contain unrelated state (history, caches, other tools'
  tokens) we don't need to expose.
- Bind-mounting would let the containerized agent observe the
  host's refresh token even if it didn't try to use it. Copy-with-
  rewrite gives us a hard guarantee that no refresh token ever
  enters the container.
- Some VM-based engines have unreliable bind-mount semantics for
  small files; explicit copy works on every engine via `<engine> cp`.

Why never copy back out:

- The container's auth state has nothing valuable in it for the
  host (no refresh token; access token will expire shortly anyway).
- A compromised container can't poison the host's credentials by
  writing to its own copy.

The CLI surface (§4.1):

```
blemees-container start <name> ...
    --auth claude                  → known shorthand
    --auth codex                   → known shorthand
    --auth-file <host-path>:<container-path>[:ro]
                                    → arbitrary verbatim file copy
```

`--auth <provider>` invokes a per-provider helper that reads the
host's auth file, extracts the *access* token (and minimal
identifying claims), and writes a synthesized credential blob into
the container at the provider's expected location. The container's
CLI starts up, uses the access token until it expires, then errors
out on its next API call. The container is then effectively done;
the user issues `blemees-container stop` (which captures session
state per §3.6) and starts a fresh container if they want to
continue.

`--auth-file` is verbatim: it's an escape hatch for non-standard
layouts or providers we don't have a built-in helper for. **Verbatim
copies are not access-only** — the user is opting into copying
whatever they pointed at, including any refresh token. The `mode`
field in `meta.json` (§6) records whether each auth entry was
`"access-only"` or `"verbatim"` so this is auditable.

Initial provider helpers:

| Provider | Host paths read (in order, first match wins) | Extracted fields | **Stripped fields** | Container destination |
|---|---|---|---|---|
| `claude` | `~/.config/claude/auth.json`, `~/.claude/auth.json`, `~/.claude.json` | `access_token`, `expires_at`, `account_uuid`, `organization_id` (and similar non-secret identifiers) | **`refresh_token` (always)**, any other long-lived secret material | same path under container's `$HOME` |
| `codex` | `~/.codex/auth.json`, `~/.config/codex/auth.json` | `access_token`, `id_token`, `expires_at` | **`refresh_token` (always)** | same path under container's `$HOME` |

The exact field set tracks each upstream CLI's accepted credential
schema and will need to be revised as those CLIs evolve. The
helper for each provider is responsible for producing a blob the
upstream CLI will *accept* (so the CLI runs without prompting for
re-auth), but that has *no refresh capability*.

**Pre-flight expiry check.** Before copying in, the helper inspects
the host's `expires_at` (or equivalent). Behavior:

- Token expired or expires within 5 minutes → refuse to start with
  a clear error. The user re-authenticates on the host (which does
  the rotation locally, on the host's terms) and tries again.
- Token expires within 30 minutes → proceed but emit a warning so
  the user knows the container has a short usable window.
- Token expires later → proceed silently.

The actual TTLs of provider access tokens vary (Anthropic ~1 hour
in 2025, OpenAI ~1 hour). Users should expect container sessions
that last more than ~30–45 minutes to need a fresh container.

**No fallback to verbatim for `--auth <provider>`.** If the upstream
CLI requires a refresh token and won't run with access-only auth,
the helper fails with a clear error directing the user to either
(a) re-authenticate on the host so the access token is fresh and
the container session is bounded by the TTL anyway, or (b) use
`--auth-file` if they consciously want to share the refresh token.
We **do not** silently fall back to copying the refresh token —
that's exactly the behavior the user is opting out of by saying
`--auth claude`.

**Container `$HOME` is required.** The destination for auth copy-in
needs to be known *before* the agent starts (otherwise the agent
runs without credentials on its first call). Inspecting the
container after start to discover `$HOME` introduces an ordering
problem we don't want, so `--container-home <path>` is a required
argument to `blemees-container start` whenever auth or session
copy-in is requested. The reference image (§9) sets `$HOME=/root`,
so most users pass `--container-home /root`; non-standard images
specify their own value.

**v0.2 ideas (not committed)**:

- `--auth-refresh-from-host`: a host-side sidecar that, while the
  container runs, periodically pushes a fresh access token into the
  container via `<engine> cp` (refreshing on the host, where the
  refresh token lives). Lifts the TTL ceiling without ever putting
  the refresh token in the container. Adds operational complexity
  and only matters if container sessions routinely exceed a single
  TTL; defer until that's a real pain.
- Per-provider configurable claim sets, in case some user wants to
  expose extra non-secret claims beyond the helper's defaults.

### 3.6 Session resume / state lifecycle

Agent CLIs persist session state — turn history, pending tool calls,
caches — under their data directory. To resume an existing session
inside a container, that state must be present at agent start; for
the resumed session to outlive the container, state must be captured
back to the host on shutdown.

**Mechanism: copy-in on start, copy-out on stop.** Symmetric to auth,
but bidirectional.

Canonical session pool, host-side:

```
$XDG_STATE_HOME/blemees/sessions/<session-id>/
    ├── (agent-specific layout: turns/, tools/, cache/, …)
    └── (whatever blemees-agentd persists for this session)
```

This is the pool any environment (host agentd or any container)
reads from on resume and writes to on stop. **`blemees-agentd`
persists directly into this pool by default**, both on the host
and inside containers (where the path resolves to the same relative
location since `XDG_STATE_HOME` is set to the user's homedir
default). For `blemees-container`, copying a session in/out is
therefore a single `<engine> cp` of the same path on either side.

Per the one-session-per-container model (§3.0), each container has
exactly one session, identified by a stable `session_id`. The
agentd inside the container hosts only that session.

Lifecycle (timeline for `blemees-container start <name>` with
`[--resume <session-id>]`):

1. **Pre-start: resolve session ID.**
   - With `--resume <id>`: run capture-commit recovery (below), then
     validate `$XDG_STATE_HOME/blemees/sessions/<id>/` exists.
     Refuse if not.
   - Without `--resume`: mint a fresh `session_id`
     (`sess_` + 16 url-safe base64 chars).
2. **Pre-start: acquire session lock.** Write
   `$XDG_STATE_HOME/blemees/sessions/<id>/.lock` containing JSON:
   ```json
   {"container_name": "<name>", "blemees_container_pid": 4711, "acquired_at": 1714512345.6}
   ```
   If a `.lock` already exists, check its `blemees_container_pid`:
   - If the PID is alive *and* belongs to a `blemees-container`
     process, refuse with `session 'X' is currently checked out by
     container 'Y' (pid 4711)`.
   - If the PID is dead, the lock is stale (its blemees-container
     died without releasing). Take it over, log a warning naming
     the prior holder, and proceed.
3. **Pre-start: copy session in.** If `--resume`, copy the host
   pool directory into the container at the agent's expected path
   (`<container-home>/.local/state/blemees/sessions/<id>/`).
4. **Pre-start: copy auth in** per §3.5.
5. **Start container.** `<engine> run` with the configured mounts
   (§3.1) and env (§3.2). Pass `BLEMEES_AGENT_SID=<session-id>`
   so the agentd inside knows the session it's hosting.
6. **Run.** Agent updates session state in its in-container path
   under `$XDG_STATE_HOME/blemees/sessions/<id>/`. The host pool
   is **not** updated during the run; the host's copy on disk
   reflects the pre-start state until the container stops.
7. **Stop: SIGTERM.** `blemees-container stop` sends SIGTERM to
   the container and *immediately* runs `<engine> wait <name>`
   (with a configurable timeout; default 30s). The agentd inside
   receives the signal, flushes its session state, exits cleanly.
8. **Stop: capture session state.** Once `wait` returns (graceful
   exit) — or after a SIGKILL escalation if `wait` times out —
   run `<engine> cp <name>:/root/.local/state/blemees/sessions/<id>/
   $XDG_STATE_HOME/blemees/sessions/<id>.tmp/`, then commit it with
   the rename-aside procedure below. The capture *must* run after
   `wait` returns; copying a directory the agentd is still writing
   to risks a torn snapshot.
9. **Stop: release lock.** `os.unlink(.lock)`.
10. **Cleanup.** Container engine state is removed by
    `blemees-container rm` (or implicitly by `stop --rm`).

**Capture commit (rename-aside).** POSIX `rename(2)` cannot replace
a non-empty directory, and the pool entry `<id>/` exists at capture
time (step 2 creates it to hold the lock, also for a fresh
session). Paths are relative to `$XDG_STATE_HOME/blemees/sessions/`:

1. Run recovery (below). Remove any leftover `<id>.tmp/`: an
   `<engine> cp` into an existing directory nests the source inside
   it.
2. `<engine> cp` the in-container session directory to `<id>.tmp/`.
   On failure, remove `<id>.tmp/` and leave `<id>/` untouched (see
   *Crash semantics*).
3. Carry the lock over: delete any `.lock` that came out of the
   container in `<id>.tmp/`, then copy the host's `<id>/.lock` to
   `<id>.tmp/.lock`. After the swap the lock is still at
   `<id>/.lock`, so step 9 is unchanged.
4. Swap: `os.rename(<id>, <id>.old)`, then
   `os.rename(<id>.tmp, <id>)`, then `shutil.rmtree(<id>.old)`. If
   `<id>/` does not exist (removed by hand), only do
   `os.rename(<id>.tmp, <id>)`.

Each rename is atomic. Between the two renames `<id>/` does not
exist for a moment; a concurrent `start --resume <id>` that sees
this either rolls forward through recovery (the same result as the
swap) or fails with "session not in pool" (§8). Both are safe. If
the second rename finds `<id>.tmp/` gone and `<id>/` present, a
concurrent recovery already did it, and the swap continues with
the `rmtree`.

**Recovery.** A crash inside step 4 can leave `<id>/` missing.
Before any `blemees-container` read or write of pool entry `<id>`
(`start --resume` step 1, the `stop` capture), if `<id>/` is missing
and `<id>.old/` exists:
- If `<id>.tmp/` exists, roll forward:
  `os.rename(<id>.tmp, <id>)`. `<id>.tmp/` is complete, because
  step 4 starts only after the copy succeeded.
- Otherwise roll back: `os.rename(<id>.old, <id>)`.

Then, if `<id>/` exists, delete any leftover `<id>.old/` and
`<id>.tmp/` — only while holding the session lock (after step 2 on
`start`; before step 9 on `stop`), so a capture in progress is
never cut short.

An atomic directory swap (Linux `renameat2(RENAME_EXCHANGE)`, macOS
`renamex_np(RENAME_SWAP)`) is not in the Python stdlib and differs
per OS; a versioned directory with a symlink flip changes the pool
layout that other repos read (§7.1). Rename-aside needs only
`os.rename`.

**Crash semantics.** If the container crashes (segfault, OOM,
ungraceful kill) before step 7, the agentd has had no chance to
flush. blemees-container detects the crash via `<engine> wait`
returning a non-zero exit code, attempts a best-effort `cp` of
whatever is on disk inside the container, and warns. The pool entry
is replaced with whatever was captured. If even the `cp` fails
(container removed by engine, dead beyond reach), the pool entry
is left untouched (pre-start state) and the lock released so the
session can be re-resumed from the last known good state. The
behavior is opt-out: `blemees-container stop --no-capture` skips
step 8 entirely.

**Exception: the agentd never ran.** If `<engine> wait` reports
the reference entrypoint's ready-timeout exit code (75, §9), the
entrypoint gave up before `exec`ing `blemees-agentd`. Nothing in
the container is newer than the pool entry, and the in-container
session path may hold an incomplete copy-in or nothing at all.
blemees-container skips step 8, warns, leaves the pool entry
untouched, and releases the lock.

**Lock semantics.** The lockfile is the source of truth for "is
this session currently checked out". A live PID in the lockfile
means a `blemees-container` process is actively managing that
session; a dead PID means the manager died and the lock is
recoverable. The lock is only ever held by `blemees-container`
itself, never by the host's `blemees-agentd` directly — when
`blemees-agentd` runs a session on the host, it doesn't write a
lock (since there's nothing to lock against; the session is just
files in the pool that the daemon has open).

**Multi-environment movement.** A session moves between
environments serially: stop the current container (capture); resume
elsewhere (host or another container). The pool entry on disk is
the source of truth between hops.

### 3.7 Persistent state directories on the host

For each container, blemees-container manages two host-side
directories:

**Runtime dir** is for *ephemeral* artifacts that don't need to
survive a logout / reboot. **State dir** is for everything that
must persist.

```
$XDG_RUNTIME_DIR/blemees/containers/<name>/
    └── agentd.sock              ← bind-mount target; the container's agentd writes here

$XDG_STATE_HOME/blemees/containers/<name>/
    ├── meta.json                ← per-container metadata (§6)
    ├── logs/
    │   ├── blemees-container.log    ← copy-in/out events, lifecycle
    │   ├── engine.log               ← engine stdout/stderr (proxied from `<engine> logs`)
    │   └── agentd.log               ← container-side agentd log (captured at stop)
    └── (auth-related state is **not** persisted; see §3.5)
```

Sessions specifically do **not** live under the per-container state
dir — they live in the shared pool at
`$XDG_STATE_HOME/blemees/sessions/`. The per-container state dir
is for things that are scoped to one container's lifetime
(metadata, logs).

Why split runtime vs state:
- The socket is meaningless once the container is gone, and
  `$XDG_RUNTIME_DIR` gets cleared on logout/reboot — exactly the
  semantics we want.
- `meta.json` and the logs need to survive a host reboot so
  `blemees-container list` and forensic / debugging workflows can
  see what happened, even after the engine restarts containers
  automatically.
- The session lock files in `$XDG_STATE_HOME/blemees/sessions/`
  also persist appropriately — they're not orphaned by reboots
  and a stale-PID check handles abandoned locks (§3.6).
- `<id>.tmp/` and `<id>.old/` siblings in the session pool are
  transient capture-commit state (§3.6, *Capture commit*), not
  sessions.

## 4. `blemees-container` CLI

```
blemees-container start <name> --image <image> [options]
blemees-container stop <name> [--force]
blemees-container list
blemees-container exec <name> [--] <cmd> [args...]
blemees-container logs <name> [--follow]
blemees-container rm <name>
```

### 4.1 `start`

```
blemees-container start <name>
    --image <image-ref>
    --container-home <path>         (REQUIRED; the image's $HOME, e.g. /root)
    --home <host-path>              (REQUIRED; project dir, host-canonical form)
    [--workdir <container-path>]    (default: same as --container-home)
    [--alias <alias>]               (sets BLEMEES_AGENT_ALIAS for the session)
    [--auth claude|codex]           (access-only copy-in; repeatable)
    [--auth-file HOST:CONTAINER[:ro]]
                                    (verbatim copy-in; repeatable)
    [--resume <session-id>]         (resume an existing session; default: fresh)
    [--env KEY=VALUE]               (additional env; repeatable)
    [--mount HOST:CONTAINER[:ro]]   (additional bind-mounts; repeatable)
    [--engine docker|podman|nerdctl]
                                    (default: auto-detect)
    [--as-root]                     (skip --user mapping; image needs it)
    [-- <agent-cmd> ...]            (override the image's entrypoint)
```

The CLI always detaches; there is no foreground mode. Lifecycle is
managed via `start` / `stop` / `logs --follow`.

Behavior:

1. **Validate** `<name>` (alias grammar, not already in use as a
   container name in either runtime or state directories).
2. **Resolve `--home`** to canonical form via `normalize_path`
   (paths under `~` rewritten, outside stay absolute).
3. **Resolve session ID:**
   - With `--resume <id>`: run capture-commit recovery (§3.6), then
     validate the pool entry exists.
   - Without `--resume`: mint a fresh `session_id`.
4. **Acquire session lock** per §3.6 step 2 (PID-keyed; takes over
   stale locks).
5. **Create state and runtime dirs** for the container:
   - `$XDG_STATE_HOME/blemees/containers/<name>/` (with `logs/` subdir)
   - `$XDG_RUNTIME_DIR/blemees/containers/<name>/`
6. **Pre-stage the runtime dir.** With the runtime subdir created
   on the host, the container's bind-mount source exists. The
   container has not started yet.
7. **Write `meta.json`** to the state dir with all fields known so
   far (image, home, container_home, session_id, resumed_session,
   auth_files, …).
8. **Pre-flight auth check.** For each `--auth <provider>`, read
   the host's auth file, run the access-token-only extractor, run
   the expiry pre-flight (§3.5). Any failure here aborts before
   the engine is invoked.
9. **Construct `<engine> run --detach`** with mounts, env (including
   `BLEMEES_AGENT_HOME`, `BLEMEES_AGENT_SID=<session_id>`,
   `XDG_RUNTIME_DIR=/run`), and `--user $(id -u):$(id -g)` unless
   `--as-root`.
10. **Engine run, *paused*.** We need the container's filesystem
    available before the agent starts so we can copy-in. There are
    two engine-driver-specific ways to achieve this — see §11 — but
    the conceptual sequence is:
    - Create the container with the engine, but don't let the
      agent's main process run yet (e.g. `docker create` then
      `docker cp` then `docker start`, or run with a sleeping
      entry-point until copy-in completes).
11. **Copy-in auth** (the access-only blob and any `--auth-file`s)
    to `--container-home`-relative destinations.
12. **Copy-in session** (if `--resume`) from
    `$XDG_STATE_HOME/blemees/sessions/<id>/` to the container's
    `<container-home>/.local/state/blemees/sessions/<id>/`.
13. **Start the agent.** Drop the ready sentinel
    `$XDG_RUNTIME_DIR/blemees/containers/<name>/.ready` (seen as
    `/run/blemees/.ready` through mount #1) — only after steps 11
    and 12 have succeeded, never on a failure path. Then either the
    engine's `start` if (10) used `create`, or signal the
    entry-point if (10) used a sleep. The agentd inside binds
    `/run/blemees/agentd.sock` and awaits operator connections.
14. **Confirm the socket appeared.** Poll
    `$XDG_RUNTIME_DIR/blemees/containers/<name>/agentd.sock` until
    it exists, with a short timeout (5s default). Failure to
    appear means the container failed to bring up the agentd —
    this includes the reference entrypoint exiting on a
    ready-sentinel timeout (§9). `blemees-container start` runs
    cleanup and exits non-zero. Cleanup here removes the engine
    container, the runtime subdir and the state subdir, and
    releases the session lock. It never captures session state:
    the pool entry stays at its pre-start state.
15. **Print `<name>` and the session_id** to stdout, exit zero.

If the engine command fails at step 9 or 10, `blemees-container`
removes the runtime subdirectory and the state subdirectory it
created and releases the session lock so partial state doesn't
leak.

### 4.2 `stop`

```
blemees-container stop <name>
    [--no-capture]                  (skip session state copy-out)
    [--timeout <duration>]          (SIGTERM grace; default 30s)
    [--force]                       (skip SIGTERM; SIGKILL immediately)
    [--rm]                          (also `blemees-container rm` after stop)
```

Lifecycle:

1. **Send SIGTERM** to the container (skipped if `--force`).
2. **`<engine> wait <name>` with `--timeout`.** Block until the
   container exits or the timeout elapses. On timeout, send
   SIGKILL and re-`wait`.
3. **Capture session state** (unless `--no-capture`):
   - Read the `session_id` from `meta.json`.
   - `<engine> cp <name>:<container-home>/.local/state/blemees/sessions/<session_id>/
     $XDG_STATE_HOME/blemees/sessions/<session_id>.tmp/`
   - On success, commit with the rename-aside procedure (§3.6,
     *Capture commit*), which also runs recovery first.
   - On failure, log the failure, remove `<session_id>.tmp/`, leave
     the host pool entry as-is (pre-start state), proceed.
   - Skip this step, with a warning, if the container's exit code
     is the reference entrypoint's ready-timeout code (75, §9). The
     agentd never ran, so there is no newer state to capture, and
     capturing would overwrite the pool entry with an incomplete
     copy-in or an empty session.
4. **Release the session lock**: `os.unlink` the `.lock` file in
   the session pool entry.
5. **Capture container logs** (best-effort): `<engine> logs <name>`
   piped to `$XDG_STATE_HOME/blemees/containers/<name>/logs/engine.log`,
   and the in-container agentd log copied via `<engine> cp`.
6. **Mark container stopped** in `meta.json`:
   `state: "stopped"`, `stopped_at: <now>`, `captured_session: <id>`
   (or null if `--no-capture`).
7. If `--rm`, also remove the engine container and the runtime
   subdir (state subdir is preserved unless the user runs `rm` in
   a separate step — keeps logs available for debugging).

The `wait`-then-`cp` sequence in steps 2 and 3 is critical: the
agent must have fully exited before the cp begins, or we risk a
torn snapshot of in-flight session state.

If the container was force-killed (`--force` or step 2 SIGKILLed
after timeout) and `--no-capture` was not passed,
`blemees-container` warns that session state may be incomplete and
attempts capture anyway — incomplete state is better than no state,
and the agent's persistence format is append-only and tolerant of
torn writes.

### 4.3 `list`

Reads every `$XDG_STATE_HOME/blemees/containers/*/meta.json` and
probes each runtime `agentd.sock` for liveness. Output format:

```
NAME       IMAGE              HOME                SESSION       STATE     AGE
test-env   blemees:latest     ~/projects/foo      sess_abc      running   14m
scratch    blemees:latest     ~/projects/scratch  sess_def      stopped   1h
```

`--json` flag dumps the full meta blocks for scripting.

### 4.4 `exec`

Thin convenience wrapper around `<engine> exec --user <uid> <name>
<cmd>...`. Mostly for debugging; the agent-driven workflows go
through the operator/peer channels, not exec.

### 4.5 `logs`

```
blemees-container logs <name>
    [--follow]                  (tail -f the live engine log)
    [--source engine|agentd|container]
                                (pick a log source; default: engine)
```

Available log sources:

- `engine` — `<engine> logs <name>` (live container stdout/stderr).
  After stop, the captured copy lives at
  `$XDG_STATE_HOME/blemees/containers/<name>/logs/engine.log`.
- `agentd` — the in-container agentd's structured log. Live, while
  the container is running, via
  `<engine> exec <name> tail -f /var/log/blemees-agentd.log` (the
  reference image puts it there); captured at
  `$XDG_STATE_HOME/blemees/containers/<name>/logs/agentd.log` after
  stop.
- `container` — `blemees-container`'s own log of what it did
  (copy-in events, signals sent, capture results). Always at
  `$XDG_STATE_HOME/blemees/containers/<name>/logs/blemees-container.log`.
  This is the place to look when something failed *outside* the
  agent itself.

### 4.6 `rm`

Removes a stopped container's engine state, its runtime subdir,
and its state subdir (logs included). Errors if the container is
still running unless `--force` (which implies `stop --force` first).
Does **not** touch the session pool — `rm` of a container does not
delete its captured session.

## 5. Identity propagation across environments

The single most important design constraint: a session running
inside a container with cwd `/workspace/foo` but bind-mounted from
the host's `~/projects/foo` must appear in the peer mesh as
`home:~/projects/foo#…`, not `home:/workspace/foo#…`. Otherwise an
agent on the host and an agent in the container working on the
same project can't address each other.

This is achieved entirely through `BLEMEES_AGENT_HOME`:

1. `blemees-container start` resolves the host path
   (`--home <host-path>`) and normalizes it.
2. It exports `BLEMEES_AGENT_HOME=<host-canonical>` in the container.
3. The container's agentd / its agent / its MCP sidecar all read
   `BLEMEES_AGENT_HOME` per `blemees-peer SPEC §3` and use that as
   their peer identity, ignoring `os.getcwd()`.
4. The agentd-side identity (the daemon's view of "this session is
   working on home X") uses the same env var.

Concretely: an agent in container `test-env` mounted from
`~/projects/foo` is addressable as
`home:~/projects/foo#sess_…` exactly as if it ran on the host, and
peer addressing works transparently across environments.

The session's *environment label* (host vs container) lives only in
the operator-channel session record (§7); peers cannot see it.

## 6. `meta.json` schema

Written by `blemees-container start` to
`$XDG_STATE_HOME/blemees/containers/<name>/meta.json` (note: state
dir, not runtime — see §3.7 for the reasoning):

```json
{
  "name": "test-env",
  "engine": "docker",
  "engine_id": "9f7a3b2c…",
  "image": "blemees:latest",
  "home": "~/projects/foo",
  "workdir": "/root",
  "container_home": "/root",
  "session_id": "sess_abc",
  "resumed": true,
  "started_at": 1714512345.678,
  "stopped_at": null,
  "state": "running",
  "uid": 501,
  "auth_providers": ["claude"],
  "auth_files": [
    {
      "host": "~/.claude/auth.json",
      "container": "/root/.claude/auth.json",
      "mode": "access-only",
      "expires_at": 1714515945,
      "ro": true
    }
  ],
  "captured_session": null,
  "version": "0.1"
}
```

Field notes:

- `state` ∈ `"running" | "stopped" | "dead"`. Updated by `stop`,
  `list` (when probing finds the engine reports otherwise), and `rm`.
- `container_home` is the value passed via the required
  `--container-home` flag (§3.5).
- `session_id` is the single session this container hosts (§3.0).
  Identifies which pool entry the container reads from on resume
  and writes to on stop.
- `resumed` is `true` if the start command used `--resume`,
  `false` if a fresh session was minted.
- `captured_session` is `null` while the container is running. On
  `stop`, it's set to `<session_id>` after a successful capture, or
  `null` if `--no-capture`, capture failed, or capture was skipped
  because the agentd never ran (§4.2 step 3).
- `auth_providers` and `auth_files` together describe what was
  copied in. Useful for `list --json` and forensic reconstruction.
  Each `auth_files[]` entry has a `mode`: `"access-only"` for
  provider-helper extractions (only the short-lived access token
  crossed into the container) or `"verbatim"` for `--auth-file`
  copies (whatever the user pointed at, possibly including a
  refresh token).
- `expires_at` (when `mode == "access-only"`) is the access
  token's expiry as a Unix timestamp, copied from the host's auth
  file. Lets `list` show how much usable time is left and lets the
  TUI surface a warning as expiry approaches.

Stable fields. Anything in `meta.json` is part of the v0.1 contract
between `blemees-container` and the TUI / `blemees-agentctl`.
Additions are non-breaking; renames are.

## 7. Cross-cutting changes in other repos

### 7.1 `blemees-agent` changes

**§3 Bootstrap (or wherever socket resolution lives):** add a note
that when `blemees-agentd` runs inside a container, the socket path
resolves to `/run/blemees/agentd.sock` via the standard XDG fallback
(because `XDG_RUNTIME_DIR=/run`). No code change needed if the existing
resolution is correct, which it is.

**Identity propagation:** the daemon must consume
`BLEMEES_AGENT_HOME`, `BLEMEES_AGENT_SID`, `BLEMEES_AGENT_ALIAS` and
inject them into the env of every backend process it spawns
(`claude`, `codex`). This is required so the agents inside see the
host-canonical home, not whatever `os.getcwd()` reports inside the
container.

If the daemon currently passes through the parent process's env
verbatim, this already works. If it constructs a sanitized env, the
three `BLEMEES_AGENT_*` vars must be added to the allow-list.

**Session persistence layout — per-session directory.** The daemon
persists each session as a directory under
`$XDG_STATE_HOME/blemees/sessions/<session-id>/`. Inside that
directory is whatever the daemon needs (event log JSONL, usage
sidecar JSON, in-flight tool-call artifacts, etc.). Today the
codebase has `event_log_dir` config that puts session frames at
`<event_log_dir>/<session>.jsonl` (one file per session); v0.1 of
this spec expects that to change to one *directory* per session,
since:

- `blemees-container` copies the directory in/out as one unit; a
  flat-file layout means the container would only see the JSONL
  log and not any auxiliary state.
- Future per-session state (tool-call caches, attachments, partial
  responses) wants somewhere to live and a per-session directory is
  the obvious answer.
- Picking a clean layout now costs nothing and avoids a migration
  later.

Concrete v0.1 layout inside each session directory:

```
$XDG_STATE_HOME/blemees/sessions/<session-id>/
    ├── meta.json              ← session-level metadata (backend, home, alias, started_at, …)
    ├── events.jsonl           ← append-only frame log (today's event_log_dir content)
    ├── usage.json             ← token-usage sidecar (today's <session>.usage.json)
    └── (room for future state — tool caches, attachments, etc.)
```

The host agentd persists into this layout directly (no per-deployment
configuration needed). Inside a container, `XDG_STATE_HOME` is the
container user's homedir default (e.g. `/root/.local/state`), so
the agent persists to `/root/.local/state/blemees/sessions/<id>/`
— exactly what `<engine> cp` operates on at the boundaries.

**`.lock` files** in this directory are for `blemees-container`
only (per §3.6); the host's blemees-agentd does not touch them.

**`<session-id>.tmp/` and `<session-id>.old/`** siblings in the pool
are transient state of the `blemees-container` capture commit (§3.6,
*Capture commit*). Readers of the pool (agentd, TUI) must ignore
them and must not treat them as sessions.

**Migration:** none — these path/layout changes happen as part of
the same v0.1 work that introduces blemees-container; existing
deployments are alpha-stage and have no data to migrate.

### 7.2 `blemees-peer` changes

None. The peer protocol and sidecar already do the right thing —
they read `BLEMEES_AGENT_HOME` per SPEC §3, and the host-canonical
path is what they get. The peer mesh sees container-spawned agents
as ordinary peers.

### 7.3 `blemees-tui` changes

Four substantial pieces. The first three are the same as before
(connection registry, source metadata, tree-by-home, persistence);
the fourth adds container lifecycle control to the TUI.

#### 7.3.1 Multi-agentd connection registry

Replace the single `BlemeesClient` with a `ConnectionRegistry` that
manages one client per discovered agentd socket.

- **Discovery** at startup: scan `$XDG_RUNTIME_DIR/blemees/` for
  `agentd.sock` (host) and `containers/*/agentd.sock` (each
  container). Connect to each, perform the standard `agent.hello`
  handshake.
- **Watch** for additions/removals: use `inotify` on Linux,
  `kqueue`/`fsevents` on macOS, falling back to a poll every 2
  seconds. New socket appears → connect; existing socket disappears
  → mark sessions on it as `disconnected` but keep them in the
  sidebar grayed out until the user removes them or the env
  reappears.
- **Event tagging**: every frame received from a given client gets
  tagged with that client's source (see 7.3.2) before flowing into
  the reducer. The reducer state grows a `source` field per session.

#### 7.3.2 Source / environment metadata

Each connection carries a `source` record:

```python
@dataclass(frozen=True, slots=True)
class AgentdSource:
    label: str  # "local" or container name
    kind: Literal["host", "container"]
    socket_path: Path
    meta: dict | None  # contents of meta.json for containers; None for host
```

The TUI computes a `source` for each client at connect time:

- Top-level `agentd.sock` → `AgentdSource(label="local", kind="host", …)`
- `containers/<name>/agentd.sock` → `AgentdSource(label=<name>, kind="container", meta=<read meta.json>)`

The display label for a session is its source's `.label`; the
session record carries a stable reference to the source.

#### 7.3.3 Sidebar grouped by home

Today the sidebar is a flat list. New shape:

```
▼ ~/projects/foo
  [1] architect    · local
  [2] tester       · test-env
▼ ~/projects/bar
  [3] reviewer     · local
▼ /tmp/scratch
  [4] coder        · scratch
```

- One node per distinct home (canonical form, taken from each
  session's `home` field — which is the host-canonical form for
  every session regardless of environment, by §5).
- Sessions listed under their home, in the order they were first
  seen across all sources.
- Per-session badge shows the source label. A subtle color or icon
  by `kind` (e.g., square = host, layered icon = container) is a
  v0.2 polish concern.
- Collapse/expand state per home, persisted in `sessions.json`.

Keybindings:

- `Ctrl+1..0`: select session by global ordinal (counts across
  sources, in the tree's display order).
- `:select N`: same.
- `:select <home>:<alias>`: human-readable form (e.g.
  `:select ~/projects/foo:architect`).
- New `:env <label>` (or `:source`) filters the sidebar to one
  source. `:env *` clears.

#### 7.3.4 Persistence

`$XDG_STATE_HOME/blemees/tui/sessions.json` adds per-session:

```json
{
  "session_id": "…",
  "source_label": "test-env",     // new
  "source_kind": "container",     // new
  "home": "~/projects/foo",
  "alias": "architect",
  ...
}
```

On TUI restart:

1. Read `sessions.json`.
2. Discover currently-live agentd sockets via the registry.
3. For each persisted session, find a matching live source (by
   `source_label` + `source_kind`); if found, re-attach. If not,
   render the session as offline / grayed-out until the source
   comes back or the user closes the row.

#### 7.3.5 Container lifecycle control from the TUI

The TUI manages containers as first-class lifecycle objects, not
just observable sources. Symmetric to how it starts host sessions
today.

**New session affordance — choose where to run.** The "new session"
prompt (currently `Ctrl+N`) gains a `--in <env>` selector:

- `host` — starts a session on the host's `blemees-agentd` (today's
  behavior, default).
- `<container-name>` — selects an *existing running* container by
  name and opens a session there. (In v0.1 with one-session-per-
  container this is rarely useful: a running container already has
  its session.)
- `new container` — opens a sub-prompt to start a fresh container
  for this session. Asks for: image (default = the reference image,
  see §9), home (default = the currently-selected home in the
  sidebar, or `$PWD` of the TUI), alias (optional), auth providers
  (checkboxes for `claude` / `codex`, plus a free-form list of
  files). On submit, the TUI shells out to `blemees-container start`
  with the chosen flags, waits for the new socket to appear, and
  attaches.

**Resume affordance — also choose where.** The "resume session"
flow (selecting an offline session row in the sidebar, e.g. one
loaded from `sessions.json` whose source disappeared) prompts:
"resume on host" or "resume in new container". The latter spawns
a `blemees-container start --resume <session-id> …` invocation
under the hood.

**Stop / remove containers.** From the sidebar, a container's
session row gains:

- `:stop <name>` — sends `blemees-container stop <name>`, waits for
  the socket to disappear from discovery.
- `:rm <name>` — sends `blemees-container rm <name>` after `stop`.
- A keybinding (e.g. `Ctrl+Shift+W` on a container session) that
  prompts and then runs the same.

**Container status surfacing.** The sidebar shows a per-container
indicator next to each container's session(s):

- `· running` (green dot) — agentd reachable
- `· stopping` (yellow) — `stop` issued, waiting for capture
- `· dead` (red) — engine reports exited but no clean stop happened
- `· offline` (gray) — known from `meta.json` but no live socket

This means the TUI shells out to `blemees-container` rather than
duplicating its logic. The CLI is the source of truth for engine
operations; the TUI is a controller in front of it.

**TUI never talks to the engine directly.** Even when the TUI
"starts a container" it does so via `blemees-container start`, not
by invoking `docker run`. This keeps engine-driver complexity in
one place and means the TUI's container-lifecycle code is just
subprocess management.

## 8. Failure modes

| Situation | Behavior |
|---|---|
| Container engine not installed | `blemees-container` exits with a clear error pointing at install instructions; nothing under `containers/` is touched |
| Container start failed (image not found, mount denied, etc.) | `blemees-container` removes any partial `containers/<name>/` it created, releases any session lock it acquired, and exits non-zero |
| Container process crashes after start | `agentd.sock` becomes stale; TUI's discovery probes it, gets connection-refused, marks the source as down. `blemees-container list` shows `state: dead`. Best-effort capture: if the container's agentd had time to flush, `blemees-container stop --force` still copies whatever is there to the host pool. Otherwise the resumed session is left in its pre-start state and the resume lock is released |
| `peerd.sock` mount missing | Container's MCP sidecar emits the existing "peerd unavailable" tool errors per `blemees-peer` SPEC §10. Agentd channel still works |
| `--resume <id>` but session not in pool | `blemees-container start` exits non-zero before touching the engine. No partial state |
| `--resume <id>` but session is locked by a live container | `blemees-container start` checks the lockfile's PID; if alive, exits with `session 'X' is currently checked out by container 'Y' (pid 4711)`. Suggests `blemees-container stop Y` first |
| `--resume <id>` but session is locked by a dead PID | `blemees-container start` takes over the stale lock, logs a warning naming the prior holder, and proceeds |
| Auth copy-in fails (file missing on host) | `blemees-container start` exits non-zero before starting the agent. Container is removed; lock (if any) is released |
| `--auth <provider>` but host's access token is expired or expires within 5 min | `blemees-container start` refuses with a clear error directing user to re-auth on host. No container is created |
| Access token expires mid-session inside the container | Agent's API calls start failing. Container's agentd reports the failure on its operator channel. User runs `blemees-container stop` (captures session state) and starts a fresh container if continuing |
| Ready sentinel not seen within 30s (slow copy-in, or `start` died before dropping it) | Reference entrypoint exits 75 without starting `blemees-agentd` (fail closed, §9). If `start` is still running, its socket poll (§4.1 step 14) fails and it cleans up without capture; the lock is released and the pool entry is untouched. If `start` is gone, a later `stop` sees exit code 75 and skips capture (§4.2 step 3) |
| Session capture (copy-out) fails on stop | `blemees-container stop` warns, leaves the host pool entry as-is (pre-start state), keeps the lock so a manual `cp` recovery is possible. Operator intervention required |
| Concurrent `blemees-container stop` invocations on the same name | Second sees the engine has already exited and skips to capture / cleanup. Capture is idempotent: both stops copy the same stopped container's state, and the commit is the rename-aside with recovery (§3.6, *Capture commit*) |
| UID mismatch — container can't write `/run/blemees/agentd.sock` | Engine emits a permission error; `blemees-container start` surfaces it directly |
| Two `blemees-container start` invocations with the same `<name>` | Second exits with `name 'X' already in use`; no state changes |
| Container restarts and re-creates `agentd.sock` | TUI's watcher reconnects; sessions are seen anew. If the container kept its sessions across restart (via agentd's own persistence), they reappear. If not, prior sessions are reported as closed |
| User deletes `containers/<name>/` while container is running | Container engine still owns the bind-mount; agentd inside continues to run but new connections from outside fail (path gone). Recovery: `blemees-container rm --force` then re-start |

## 9. Reference image

`blemees-container` ships exactly one reference image and assumes
users start there. The image lives in
`blemees-container/examples/Dockerfile` and is published as
`blemees/agent:<version>` (or whatever registry tag the project
adopts).

**What it contains:**

- Python 3.11+ runtime
- `blemees-agent` (installed via `pip install blemees-agent`)
- `blemees-peer` (for the MCP sidecar; agents in the container
  participate in the peer mesh)
- `claude` and `codex` CLI binaries
- An entry point that runs `blemees-agentd` after waiting for a
  ready signal from the host (so copy-in completes before the agent
  fires up)

**What it sets:**

- `HOME=/root`
- `XDG_RUNTIME_DIR=/run`, `XDG_STATE_HOME=/root/.local/state`,
  `XDG_CONFIG_HOME=/root/.config`
- `WORKDIR=/root` (overridable via `--workdir`)
- `ENTRYPOINT ["/usr/local/bin/blemees-agent-entrypoint"]` —
  entrypoint script that:
  1. Waits for `/run/blemees/.ready` (a sentinel file
     `blemees-container start` drops after copy-in is complete) —
     timeout 30s. On timeout it writes an error to stderr and
     exits with code 75 (`EX_TEMPFAIL`) **without** starting
     `blemees-agentd`
  2. `exec`s `blemees-agentd`

The "wait for ready" sentinel is the cleanest fix for the ordering
problem (auth files needed before agent starts). `blemees-container`
creates the file at the right point in the start sequence (§4.1
step 13).

The timeout **fails closed**. Proceeding without the sentinel would
start an agentd with no auth and without the resumed session, and
a later `stop` would capture that fresh or empty state over the
host pool entry (§3.6 step 8) — silent session data loss. Exiting
instead means no `agentd.sock` appears, so `start`'s socket poll
(§4.1 step 14) surfaces the failure and cleans up. The dedicated
exit code lets `stop` tell "agentd never ran" apart from a crash
and skip capture (§4.2 step 3). Custom images that replace the
entrypoint should keep both properties.

**What users typically build on top:**

- Their own preferred shell tooling (vim, less, ripgrep, …) for
  `blemees-container exec` debugging
- Project-specific build deps if they intend to run them in the
  container

The reference image is intentionally minimal — users who need more
fork the Dockerfile.

## 10. Out of scope for v0.1

- **Container orchestration** — auto-restart, healthchecks,
  dependency graphs, liveness probes. Use the engine's own
  facilities (`docker --restart`, podman systemd units) if needed.
- **Container restart and resurrection** — if the engine restarts
  a stopped container on its own, `blemees-container` doesn't try
  to be clever: the runtime socket reappears, the TUI's discovery
  watcher picks it up and reconnects to the agentd inside, but no
  state copy-in re-runs. The session continues from wherever the
  agentd persisted it last. If the user wants a clean restart with
  fresh copy-in, they `stop` and `start` again.
- **Container health monitoring** — beyond "is the socket live?"
  via the TUI's discovery probe. Deep health (memory pressure,
  approaching OOM, etc.) is the engine's job.
- **Image building** — `blemees-container` does not build images.
  The reference Dockerfile in `examples/` is a starting point.
- **Cross-host containers** — out of scope for v0.1, same as
  `blemees-peer` cross-host federation.
- **Privileged / `--privileged` containers** — `blemees-container`
  refuses to set this flag. Users who really need it call docker
  directly.
- **Apple's `container` runtime** — out of scope for v0.1 pending
  empirical validation that Unix sockets pass through its virtio-fs
  cleanly.
- **Container engines other than docker / podman / nerdctl** —
  others may work via the engine driver protocol (§11), but only
  the three named are tested.
- **Nested containers** — a container running `blemees-container start`
  to nest more containers. Out of scope; the runtime layout
  conventions assume one level.
- **Multi-session containers** — explicitly forbidden by §3.0.
  v0.2 may relax if a clear use-case emerges, but the auth and
  isolation models would have to be revisited.
- **TUI live-reload of `meta.json`** — if a container's meta block
  changes mid-life (image upgraded in place, etc.), the TUI keeps
  the cached copy until reconnect.

## 11. Engine driver protocol

`blemees-container` interacts with the container engine through a
narrow Python protocol. One driver per engine; the abstraction lets
us add new engines (or accommodate engine-specific quirks) without
spreading conditional code through the rest of the codebase.

```python
class ContainerEngine(Protocol):
    """Abstraction over `docker` / `podman` / `nerdctl` (and friends)."""

    name: str
    """Short identifier, e.g. 'docker'. Used in meta.json."""

    def is_available(self) -> bool:
        """True if the engine binary is on PATH and the daemon is reachable.
        Checked at start; informs --engine auto-detection."""

    def create(
        self,
        container_name: str,
        image: str,
        mounts: list[Mount],
        env: dict[str, str],
        user: str | None,  # "uid:gid" or None for default
        workdir: str | None,
        entrypoint: list[str] | None,
        labels: dict[str, str],  # for engine-side discovery / cleanup
    ) -> str:
        """Create the container without starting it. Returns engine_id.
        Mirrors `docker create`. blemees-container uses this so it can
        cp files in before the agent runs."""

    def start(self, container_name: str) -> None:
        """Start a previously-created container."""

    def cp_to(self, container_name: str, host_src: Path, container_dst: str) -> None:
        """Copy a host path into the container. Must work for files
        and directories. Wraps `docker cp <src> <name>:<dst>`."""

    def cp_from(self, container_name: str, container_src: str, host_dst: Path) -> None:
        """Copy a container path back to the host. Wraps `docker cp
        <name>:<src> <dst>`."""

    def exec(
        self,
        container_name: str,
        cmd: list[str],
        check: bool = True,
    ) -> tuple[int, bytes, bytes]:
        """Run a command inside the container synchronously. Returns
        (returncode, stdout, stderr)."""

    def signal(self, container_name: str, sig: int) -> None:
        """Send a Unix signal to the container's PID 1."""

    def wait(self, container_name: str, timeout: float | None = None) -> int:
        """Block until the container exits; return its exit code.
        Raises TimeoutError if `timeout` elapses."""

    def stop(
        self,
        container_name: str,
        timeout: float = 30.0,
    ) -> int:
        """Send SIGTERM, wait up to `timeout`, then SIGKILL. Returns
        the exit code. Convenience wrapper around signal + wait."""

    def logs(self, container_name: str, follow: bool = False) -> Iterator[bytes]:
        """Stream the container's stdout/stderr."""

    def inspect(self, container_name: str) -> dict[str, Any]:
        """Engine-specific dict; blemees-container reads only the
        fields it needs (state, exit code, started_at)."""

    def remove(self, container_name: str, force: bool = False) -> None:
        """Delete the container. Errors if running unless force=True."""


@dataclass(frozen=True)
class Mount:
    host_source: Path
    container_target: str
    read_only: bool = False
    is_file: bool = False  # True for file binds (e.g. peerd.sock)
```

Driver implementations live in
`blemees_container/engines/<name>.py`. The reference docker driver
is straight subprocess-of-`docker` calls; `podman` and `nerdctl`
drivers inherit from it and override only the binary name (and any
flag differences). The abstraction is deliberately "boring" — every
method maps to one engine command — to keep substitution cheap.

**Auto-detection** (`--engine` not passed): try drivers in this
order: `docker`, `podman`, `nerdctl`. First one whose
`is_available()` returns true wins. Tie-breaking by `$PATH` order
gets surfaced as a warning so users know they're inheriting an
implicit choice.

**The "create-then-cp-then-start" sequence is a hard requirement
of the protocol.** Drivers that can't separate `create` from
`start` can't be supported without more elaborate pre-staging
(running a sleeping process and signaling it). All three named
v0.1 engines support this naturally.

## 12. Implementation order

1. **Runtime + state layout convention codified** in `blemees-agent`,
   `blemees-peer`, `blemees-tui` path-resolution helpers. Already
   resolve `$XDG_RUNTIME_DIR/blemees/agentd.sock` and
   `$XDG_RUNTIME_DIR/blemees/peerd.sock`; add the
   `$XDG_RUNTIME_DIR/blemees/containers/<name>/agentd.sock` and
   `$XDG_STATE_HOME/blemees/containers/<name>/` conventions in a
   shared spot, and a discovery helper that scans them. Tiny.
2. **`blemees-agent` adjustments** (independent of the rest):
   - Verify env passthrough of `BLEMEES_AGENT_HOME`, `_SID`,
     `_ALIAS` to spawned backend processes.
   - Move session persistence to per-session directories at
     `$XDG_STATE_HOME/blemees/sessions/<id>/` (per §7.1). Includes
     `meta.json`, `events.jsonl`, `usage.json`, etc.
   Small-to-medium depending on how the daemon currently lays out
   session state.
3. **Engine driver protocol** in `blemees-container/blemees_container/engines/`
   per §11 — `ContainerEngine` Protocol, plus the docker
   implementation as the reference. Auto-detection. Mount /
   Env / cp helpers. Small; mostly subprocess wrangling.
4. **`blemees-container` CLI core** — `start` (without auth or
   resume yet) / `stop` / `list` / `rm` / `exec` / `logs`. Uses
   the docker driver. Implements the create-then-start sequence
   so step 6 has the seam to inject copy-in. Includes `meta.json`
   schema, name validation, UID alignment, runtime-vs-state dir
   split. Medium.
5. **Reference image + entry-point script** at
   `blemees-container/examples/` — Dockerfile installing
   blemees-agent + blemees-peer + claude + codex; entry-point
   waits on `/run/blemees/.ready` then `exec`s `blemees-agentd`,
   or exits 75 on timeout (fail closed, §9). Smoke test: a
   `start` + connect + close + `stop` round-trip against the
   image, plus a missing-sentinel run. Medium.
6. **`blemees-container` auth copy-in** — `--auth claude|codex`
   and `--auth-file`. Per-provider access-only extractors
   (one Python module each, schema work is the risk — needs
   real upstream CLI auth files to verify). Pre-flight expiry
   checks. Drops the `.ready` sentinel after copy-in. Medium.
7. **`blemees-container` session resume / capture** — `--resume`,
   `--no-capture`, PID-keyed lockfiles with stale takeover,
   sequenced wait+cp. Rename-aside capture commit with crash
   recovery (§3.6). Medium-large.
8. **TUI multi-agentd registry** — replace single client with
   registry per §7.3.1. Tag events with source. Discovery + watch.
   No visible UX change yet (sidebar stays flat). Large.
9. **TUI sidebar grouped by home + container lifecycle control** —
   tree render, source badges, collapse/expand, updated
   keybindings, "new container session" / `:stop` / `:rm`
   affordances per §7.3.5. Persistence updates. Medium-large.
10. **Engine driver: podman + nerdctl** — extend the docker
    driver, parameterize binary name, smoke-test. Small.
11. **(Out of v0.1)** Engine driver: Apple `container` — gated
    on empirical validation that Unix sockets pass through its
    virtio-fs cleanly. Deferred per §10.

Milestones:

- Steps 1–5 form **"you can run an agent in a container"** — a
  containerized agentd is reachable from the host CLI / TUI. No
  auth, no resume; the agent runs against whatever's baked into
  the image.
- Step 6 unblocks **"agent CLIs authenticate"** — host's access
  token is shared in (refresh token never crosses); container
  runs until token expiry.
- Step 7 unblocks **"sessions are portable"** — the headline
  cross-environment feature.
- Steps 8–9 deliver the **"unified TUI"** UX with first-class
  container lifecycle from inside the TUI.
- Step 10 broadens engine support.

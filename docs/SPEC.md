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
                                          │ blemees/2 (operator)
                  ┌───────────────────────┼─────────────────────────────┐
                  │                       │                             │
                  ▼                       ▼                             ▼
          ┌──────────────┐        ┌──────────────┐              ┌──────────────┐
          │ blemees-     │        │ blemees-     │              │ blemees-     │
          │ agentd       │        │ agentd       │              │ agentd       │
          │ (host)       │        │ (container   │              │ (container   │
          │              │        │  "test-env") │              │  "scratch")  │
          └──────┬───────┘        └──────┬───────┘              └──────┬───────┘
                 │ peer/1                │ peer/1                      │ peer/1
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

Two bind-mounts and one env block per container.

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
    [--home <host-path>]            (default: $PWD)
    [--workdir <container-path>]    (default: /workspace)
    [--alias <alias>]               (sets BLEMEES_AGENT_ALIAS)
    [--sid <sid>]                   (sets BLEMEES_AGENT_SID; rare)
    [--env KEY=VALUE]               (additional env, repeatable)
    [--mount HOST:CONTAINER[:ro]]   (additional bind-mounts, repeatable)
    [--engine docker|podman|nerdctl] (default: auto-detect)
    [--as-root]                     (skip --user mapping; image needs it)
    [--detach]                      (default; foreground with --no-detach)
    [-- <agent-cmd> ...]            (override the image's entrypoint)
```

Behavior:

1. Validate `<name>` (alias grammar, not already in use).
2. Resolve `--home` to canonical form via the same `normalize_path`
   from `blemees-peer/blemees-tui/blemees-agent` (paths under `~`
   get rewritten, paths outside stay absolute).
3. Create `$XDG_RUNTIME_DIR/blemees/containers/<name>/` if absent.
4. Write `meta.json` (see §6).
5. Construct the `<engine> run` invocation with all three mounts,
   the env block, the chosen entry point, and `--name <name>`.
6. Exec the engine; on success, print `<name>` and the container ID.

If the engine command fails, `blemees-container` removes the
runtime subdirectory it created so partial state doesn't leak.

### 4.2 `stop`

Sends `SIGTERM` to the container, waits up to 10 seconds, then
`SIGKILL` if needed. Removes `agentd.sock` (which the container's
agentd should have unlinked on graceful shutdown anyway). Leaves
`meta.json` for `blemees-container list` to surface as
`status: stopped` until the user runs `rm`.

### 4.3 `list`

Reads every `containers/*/meta.json` and probes each `agentd.sock`
for liveness. Output format:

```
NAME       IMAGE              HOME                STATE      AGE
test-env   blemees:latest     ~/projects/foo      running    14m
scratch    blemees:latest     ~/projects/scratch  stopped    1h
```

`--json` flag dumps the full meta blocks for scripting.

### 4.4 `exec`

Thin convenience wrapper around `<engine> exec --user <uid> <name>
<cmd>...`. Mostly for debugging; the agent-driven workflows go
through the operator/peer channels, not exec.

### 4.5 `logs`

Tails the container engine's stdout/stderr. `--follow` proxies the
engine's `-f` flag.

### 4.6 `rm`

Removes a stopped container and its runtime subdirectory. Errors
if the container is still running unless `--force`.

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
`$XDG_RUNTIME_DIR/blemees/containers/<name>/meta.json`:

```json
{
  "name": "test-env",
  "engine": "docker",
  "engine_id": "9f7a3b2c…",
  "image": "blemees:latest",
  "home": "~/projects/foo",
  "workdir": "/workspace",
  "started_at": 1714512345.678,
  "uid": 501,
  "version": "0.1"
}
```

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

### 7.2 `blemees-peer` changes

None. The peer protocol and sidecar already do the right thing —
they read `BLEMEES_AGENT_HOME` per SPEC §3, and the host-canonical
path is what they get. The peer mesh sees container-spawned agents
as ordinary peers.

### 7.3 `blemees-tui` changes

Three substantial pieces:

#### 7.3.1 Multi-agentd connection registry

Replace the single `BlemeesClient` with a `ConnectionRegistry` that
manages one client per discovered agentd socket.

- **Discovery** at startup: scan `$XDG_RUNTIME_DIR/blemees/` for
  `agentd.sock` (host) and `containers/*/agentd.sock` (each
  container). Connect to each, perform the standard `blemeesd.hello`
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
    label: str                # "local" or container name
    kind: Literal["host", "container"]
    socket_path: Path
    meta: dict | None         # contents of meta.json for containers; None for host
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

## 8. Failure modes

| Situation | Behavior |
|---|---|
| Container engine not installed | `blemees-container` exits with a clear error pointing at install instructions; nothing under `containers/` is touched |
| Container start failed (image not found, mount denied, etc.) | `blemees-container` removes any partial `containers/<name>/` it created and exits non-zero |
| Container process crashes after start | `agentd.sock` becomes stale; TUI's discovery probes it, gets connection-refused, marks the source as down. `blemees-container list` shows `state: dead` |
| `peerd.sock` mount missing | Container's MCP sidecar emits the existing "peerd unavailable" tool errors per `blemees-peer` SPEC §10. Agentd channel still works |
| UID mismatch — container can't write `/run/blemees/agentd.sock` | Engine emits a permission error; `blemees-container start` surfaces it directly |
| Two `blemees-container start` invocations with the same `<name>` | Second exits with `name 'X' already in use`; no state changes |
| Container restarts and re-creates `agentd.sock` | TUI's watcher reconnects; sessions are seen anew. If the container kept its sessions across restart (via agentd's own persistence), they reappear. If not, prior sessions are reported as closed |
| User deletes `containers/<name>/` while container is running | Container engine still owns the bind-mount; agentd inside continues to run but new connections from outside fail (path gone). Recovery: `blemees-container rm --force` then re-start |

## 9. Out of scope for v0.1

- **Container orchestration** — auto-restart, healthchecks, dependency
  graphs. Use the engine's own facilities (Docker `--restart`, podman
  systemd units) if needed.
- **Image building** — `blemees-container` does not build images. Users
  bring their own; a reference Dockerfile lives in `examples/`.
- **Cross-host containers** — out of scope for v0.1, same as
  `blemees-peer` cross-host federation.
- **Privileged / `--privileged` containers** — `blemees-container`
  refuses to set this flag. Users who really need it can call docker
  directly.
- **Container engines other than docker / podman / nerdctl** — others
  may work via the `--engine` extension hook, but only the three
  named are tested.
- **Nested containers** — a container running `blemees-container start`
  to nest more containers. Out of scope; the runtime layout
  conventions assume one level.
- **Operator-driven container start** — the TUI has no "start a new
  container" affordance in v0.1. Users invoke `blemees-container start`
  from the shell. A `Ctrl+Shift+N` for "new container session" is
  v0.2+.
- **TUI live-reload of `meta.json`** — if a container's meta block
  changes mid-life (image upgraded in place, etc.), the TUI keeps the
  cached copy until reconnect.

## 10. Implementation order

1. **Runtime layout convention codified** in `blemees-agent`,
   `blemees-peer`, `blemees-tui` socket-resolution helpers — they
   already build paths like `$XDG_RUNTIME_DIR/blemees/agentd.sock` and
   `$XDG_RUNTIME_DIR/blemees/peerd.sock`; add a documented spot for
   `containers/<name>/agentd.sock` and shared discovery code.
   Tiny.
2. **`blemees-container` CLI** — `start` / `stop` / `list` / `rm`
   first; `exec` / `logs` second. Includes `meta.json` schema, the
   two-mount invocation, the env block, name validation, UID
   handling. Medium.
3. **`blemees-agent` env passthrough** — verify the daemon propagates
   `BLEMEES_AGENT_HOME` etc. into spawned backend env. Trivial check;
   small fix if missing.
4. **Reference Dockerfile + integration test** in
   `blemees-container/examples/` — a minimal image that boots
   `blemees-agentd` and proves a host TUI can drive a session inside
   it. Medium.
5. **TUI multi-agentd registry** — replace the single client with the
   registry; tag events with source. No visible UX change yet (still
   flat list). Large.
6. **TUI sidebar grouped by home** — render the tree, with the source
   badge per session, collapse/expand, updated keybindings, persisted
   state. Medium (depends on 5).

Steps 1 + 2 + 3 + 4 together form the "you can run an agent in a
container" milestone — the TUI works against it via
`blemees-tui --socket containers/<name>/agentd.sock` while the
multi-agentd UX is being built. Steps 5 + 6 are the polished
end-state.

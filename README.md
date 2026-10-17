# blemees-container

Run [`blemees-agentd`](https://github.com/blemees/blemees-agent) inside
a sandboxed container with full participation in the existing operator
([`blemees-tui`](https://github.com/blemees/blemees-tui)) and peer-mesh
([`blemees-peer`](https://github.com/blemees/blemees-peer)) channels.

A session running inside a container is indistinguishable from one
running on the host — to the TUI, to the peer mesh, and to other
agents addressing it.

> **Status — v0.1.0 (pre-alpha):** Package scaffold only. The CLI
> shell is in place; the engine drivers and lifecycle logic land
> in subsequent releases. See
> [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) for
> the phase-by-phase roadmap.

## Install

```sh
pip install blemees-container
```

Once shipped to Homebrew:

```sh
brew install blemees/tap/blemees-container
```

## What it does

`blemees-container` composes the right `docker` / `podman` / `nerdctl`
invocation to spawn a containerized agentd, wires up the bind-mounts
and env so the contained agent appears on the host's peer mesh, and
manages session state copy-in/copy-out so sessions are portable
between host and container.

It is **not a daemon.** It's a CLI that delegates container lifecycle
to the host's container runtime.

```
blemees-container start <name> --image <image> [options]
blemees-container stop <name> [--force]
blemees-container list [--json]
blemees-container exec <name> [--] <cmd> [args...]
blemees-container logs <name> [--follow]
blemees-container rm <name>
```

## How it fits

```
                                 ┌────────────────┐
                                 │      TUI       │
                                 └────────┬───────┘
                                          │ blemees-agent/1
                  ┌───────────────────────┼─────────────────────────────┐
                  │                       │                             │
                  ▼                       ▼                             ▼
          ┌──────────────┐        ┌──────────────┐              ┌──────────────┐
          │ agentd       │        │ agentd       │              │ agentd       │
          │ (host)       │        │ (container)  │              │ (container)  │
          └──────┬───────┘        └──────┬───────┘              └──────┬───────┘
                 │                       │                             │
                 └───────────────┬───────┴─────────────────────────────┘
                                 ▼
                          ┌──────────────┐
                          │   peerd      │
                          └──────────────┘
```

The TUI connects to *every* agentd it finds (host plus every
container) and merges their session lists into one view. Identity
propagation via `BLEMEES_AGENT_HOME` makes a session in
`/workspace/foo` (bind-mounted from `~/projects/foo`) addressable on
the peer mesh as `home:~/projects/foo#…`, exactly as if it were
running on the host.

## Documentation

- [`docs/SPEC.md`](docs/SPEC.md) — full v0.1 design (1280 lines).
- [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) — phase
  breakdown of the 12 implementation steps with parallelization and
  risk hotspots.

## License

MIT — see [`LICENSE`](LICENSE).

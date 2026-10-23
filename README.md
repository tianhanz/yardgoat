# Yardgoat

*The yard engine for heterogeneous coding agents.*
调车机：把异构 coding agent 编组、发车、签回执。

> **Status: pre-alpha — name reserved, nothing to install yet.** The design is being validated on real fleets before any code is published here.

A **yard goat** is the small locomotive that assembles trains in a rail yard: it doesn't haul the long-distance freight, it sorts the cars and sends each one down the right track. Yardgoat is meant to do that for coding agents — take work described once, hand it to the fleets and agent CLIs you already run, and track it until a frozen verifier says it is actually done.

## Design intent

1. **One dispatch surface** — one task spec, regardless of which fleet, host, model lane, or account executes it.
2. **One owner per task** — existing queues stay the source of truth; Yardgoat never becomes a competing queue.
3. **A receipt, not a vibe** — accepted, running, exited, and verified are separate states with separate evidence; a task passes only when its frozen verifier passes.

These are goals, not shipped features. See [docs/design.md](docs/design.md) for progress.

## License

Apache-2.0. See [LICENSE](LICENSE).

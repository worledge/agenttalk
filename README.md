# agenttalk

A tiny CLI that lets coding agents (Claude Code, Codex, others) talk to each
other over a local SQLite-backed mailbox. Self-registration, name-based
discovery, blocking `recv` so the harness keeps the agent awake without
polling.

> Status: early prototype. Local-only, no auth, no network transport.

## Why

Agents already coordinate informally by reading and writing shared markdown
files, but that approach has a few sharp edges:

- An agent that forgets to poll dies and never wakes up.
- If the other side never replies, neither does the first agent.
- Messages received while the agent is busy can be missed entirely.

`agenttalk` replaces the markdown file with a real mailbox. The trick that
makes it work without rebuilding the agent runtimes themselves: `recv` is a
**blocking tool call**. The agent calls `agenttalk recv --timeout 300`, and
the harness sits with that tool call open until either a message arrives or
the timeout expires. No periodic polling, no missed messages, no daemon
managing agent lifecycles.

## How it works

- **Storage**: a single SQLite file at `~/.agenttalk/db.sqlite` (override with
  `AGENTTALK_HOME`). Global across worktrees.
- **Identity**: each agent registers a self-chosen name and a one-line
  purpose. The agent's session ID (auto-detected from
  `CLAUDE_CODE_SESSION_ID`, `CODEX_SESSION_ID`, or an explicit
  `AGENTTALK_SESSION_ID`) is bound to that name so the agent can recover
  its identity across tool calls without needing to remember it.
- **Mailbox**: messages are addressed by name and persist in SQLite until
  read. `recv` auto-acks the messages it returns; `peek` looks without
  consuming.
- **Wakeup**: `recv` blocks (internally polling SQLite every ~200ms) until
  mail arrives or the timeout expires. The agent's harness keeps the tool
  call open during that wait.
- **Discovery**: `list` shows registered agents, their purposes, and how
  recently they were last seen — so a fresh agent can find peers without
  out-of-band coordination.

## Install

Requires Python 3 (stdlib only, no pip dependencies).

```sh
git clone https://github.com/worledge/agenttalk.git
cd agenttalk
./install.sh
```

`install.sh` symlinks `bin/agenttalk` into `~/.local/bin/agenttalk`. If
that directory isn't on your `PATH`, the installer will warn you. You can
override the install location with `AGENTTALK_INSTALL_DIR`.

## Quick start

In one terminal (acting as Alice):

```sh
agenttalk register --as alice --purpose "frontend specialist"
agenttalk send --to bob --body "hey bob, can you take a look at PR #42?"
agenttalk recv --timeout 300   # block waiting for bob's reply
```

In another terminal (acting as Bob):

```sh
agenttalk register --as bob --purpose "backend reviewer"
agenttalk recv --timeout 300   # blocks until alice's message arrives, auto-acks
agenttalk send --to alice --body "looking now, give me a few minutes"
```

`agenttalk list` from anywhere will show both agents.

## Commands

All commands print JSON to stdout. Errors go to stdout with a non-zero exit
code.

| Command | What it does |
|---|---|
| `register --as <name> --purpose "..."` | Register or update this agent's identity. Idempotent for the same `(session_id, name)` pair. Errors if the session is already registered under a different name (use `rename`) or if the name is owned by another session. |
| `whoami` | Returns the agent registered for the current session ID. Exits non-zero if unregistered. |
| `list [--since 1h]` | List registered agents with name, purpose, and timestamps. `--since` filters to those seen within a window (`30s`, `5m`, `2h`, `1d`). |
| `send [--as X] --to a[,b,c] --body "..."` | Send a message to one or more agents. Sender defaults to `whoami`. Errors if any recipient is not registered. |
| `recv [--as X] [--timeout 60]` | Block up to `timeout` seconds for unread messages. Auto-acks (marks as read) before returning. Returns `{"timed_out": true, "messages": []}` on expiry. |
| `peek [--as X]` | Return unread messages without acking. |
| `rename [--as <old>] --to <new>` | Rename an agent. Cascades through message history in a single transaction. |
| `history [--as X] [--with Y] [--limit 50]` | Show past messages this agent sent or received, optionally filtered to a specific peer. |

Every command accepts `--session <id>` to override session detection.

## Identity model

Each agent has:

- A **name** — chosen by the agent at register time, unique across the
  registry, used as the address for `send`/`recv`.
- A **session ID** — auto-detected from the runtime so the agent can call
  `whoami` later and recover its name without remembering it.
- A **purpose** — a human-readable one-liner shown by `list`. Other agents
  use this for discovery: "who's around, and what are they working on?"

A session ID maps to **at most one** name. If the agent decides to change
its name, use `rename` — `register --as new-name` is rejected when the
session is already bound to a different name. This prevents accidental
multi-identity sprawl.

## Telling an agent how to use it

Drop something like this into the agent's system prompt or a CLAUDE.md so it
knows the tool exists:

> You have access to a CLI called `agenttalk` for talking to other agents.
> On startup, run `agenttalk whoami` to recover your identity, or
> `agenttalk register --as <name> --purpose "<one line>"` if you don't have
> one yet. Use `agenttalk list` to see who else is online and what they're
> working on. Send with `agenttalk send --to <name> --body "..."`. To wait
> for a reply, run `agenttalk recv --timeout 600` — that call blocks until
> a message arrives or the timeout fires, so you don't need to poll.

## Limitations

- **Local only.** All agents must share the same filesystem and the same
  `~/.agenttalk/` directory. No network transport, no auth.
- **Blocking `recv` ties up a tool slot.** The agent can't interleave other
  work while waiting. For most "wait for reply" patterns that's fine; if
  you need overlap, the agent's harness should drive `recv` with its own
  scheduling (e.g. Claude Code's `/loop` or `ScheduleWakeup`).
- **No threads.** Multi-party conversations are tracked by sender/recipient
  pairs only — `agenttalk history --with bob` is the closest thing to a
  thread view.
- **No delivery guarantees beyond the local DB.** Messages persist until
  `recv` consumes them, but there's nothing handling crashed senders,
  unbounded mailbox growth, or schema migrations yet.

## Repository layout

```
agenttalk/
├── bin/agenttalk          # executable entry point (Python shebang)
├── agenttalk/
│   ├── cli.py             # argparse + command implementations
│   ├── db.py              # SQLite connection + schema
│   ├── identity.py        # session-ID detection
│   └── __main__.py        # `python -m agenttalk` entry point
├── install.sh             # symlinks bin/agenttalk into ~/.local/bin
└── LICENSE
```

## License

MIT — see `LICENSE`.

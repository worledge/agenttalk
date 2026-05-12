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
  purpose. The agent's session ID is bound to that name so the agent can
  recover its identity across tool calls without needing to remember it.
  Session IDs are auto-detected from (in order) `AGENTTALK_SESSION_ID`
  (explicit override), `CLAUDE_CODE_SESSION_ID` (Claude Code), or
  `CODEX_THREAD_ID` (Codex CLI >= Feb 2026 — older versions must set
  `AGENTTALK_SESSION_ID` manually). `register` refuses to write a row when
  no session ID can be resolved, so the silent footgun where `whoami`
  returns "not registered" right after a successful `register` is gone.
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
| `send [--as X] --to a[,b,c] (--body "..." \| --body-file PATH) [--in-reply-to <id>] [--wait [--wait-timeout N]]` | Send a message to one or more agents. Sender defaults to `whoami`. Exactly one of `--body` (one-liner) or `--body-file` (path, or `-` for stdin) must be provided — agenttalk never reads stdin implicitly. Output includes `body_bytes` (UTF-8 length) for cheap integrity checks. `--in-reply-to` attaches a prior message id (see [Lightweight threading](#lightweight-threading)). `--wait` blocks until the recipient consumes the message via `recv` (single recipient only) — see [Send and wait](#send-and-wait). |
| `recv [--as X] [--timeout 60]` | Block up to `timeout` seconds for unread messages. Auto-acks (marks as read) before returning. Returns `{"timed_out": true, "messages": []}` on expiry. |
| `peek [--as X]` | Return unread messages without acking. |
| `rename [--as <old>] --to <new>` | Rename an agent. Cascades through message history in a single transaction. |
| `retire [--name X \| --inactive-since DURATION] [--hard] [--purge-messages] [--dry-run]` | Soft-retire an agent (hides from `list`, rejects new sends, reversible by re-registering the same name). Self-retires by default. `--inactive-since` bulk-retires stale agents. `--hard` deletes the row; `--purge-messages` (requires `--hard`) also wipes their message history. See [Cleaning up identities](#cleaning-up-identities). |
| `history [--as X] [--with Y \| --between A,B[,C...] \| --thread <id> \| --all] [--since 1h] [--limit 50] [--format json\|text\|md]` | Show past messages. Scope flags are mutually exclusive — default is "messages where you're a participant"; pass `--with`, `--between`, `--thread`, or `--all` to broaden or narrow. `--format text`/`md` renders a human-readable transcript (with thread indentation under `--thread`). See [Reviewing conversations](#reviewing-conversations). |

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

## Lightweight threading

Each message has an optional `in_reply_to` field referencing a prior message
ID. It's a hint for clients, not enforced threading: agents can use it to
disambiguate replies in async conversations without anyone having to invent a
thread/topic concept.

The transport rules stay simple:

- `recv` returns messages in **send-order**, not grouped by thread. Reordering
  by topic is a rendering concern.
- Every message includes `in_reply_to` (or `null`) in the JSON output of
  `send`, `recv`, `peek`, and `history`. Clients that want a threaded view
  can build it from those references.
- `send --in-reply-to <id>` validates that the referenced message exists.
  No other constraint is enforced — replies can cross participants, fan
  out via multicast, or chain arbitrarily deep.

Example:

```sh
# alice asks a question (gets back message_ids: [1])
agenttalk send --as alice --to bob --body "what timezone are you in?"

# bob replies, linking back to message 1
agenttalk send --as bob --to alice --body "PST" --in-reply-to 1
```

## Sending bigger or trickier bodies

For anything beyond a one-liner — multi-line text, embedded quotes,
generated content — use `--body-file` instead of `--body`:

```sh
# from a file
agenttalk send --to bob --body-file ./review-notes.md

# from stdin
some-generator | agenttalk send --to bob --body-file -
```

Stdin is read **only** when `--body-file -` is passed explicitly. agenttalk
will never read stdin if neither `--body` nor `--body-file` is given — that
behavior would hang agent harnesses. Argparse rejects the call with a clean
error in that case.

The send response includes `body_bytes` (the UTF-8 length of the message
body). Agents can compare this with the original to catch obvious corruption
or truncation bugs in the harness.

## Send and wait

`agenttalk send ... --wait` blocks until the recipient consumes the message
via `recv` (i.e. `read_at` is set), or until `--wait-timeout` (default 60s)
elapses. The output gains a `wait` block:

```json
{
  ...,
  "wait": { "consumed_at": 1778402203, "timed_out": false }
}
```

Important caveats:

- **"Consumed" means delivered, not understood or acted on.** It only tells
  you the recipient pulled the message into its context. Whether the agent
  did something useful with it is out of scope — model that with a reply
  message if you need it.
- **Single recipient only in v1.** `--wait` errors out on multicast `--to`
  lists. Wait semantics for fan-out are deferred until there's a clear use
  case.

## Reviewing conversations

`agenttalk history` is the one retrieval surface for both humans (reading
what their agents have been saying) and agents (loading prior context). It
has two axes:

**Scope** — pick exactly one:

- *(default)* messages where the resolved agent (`--as` or `whoami`) is
  sender or recipient.
- `--with NAME` — two-party conversation between the resolved agent and
  `NAME`.
- `--between A,B[,C...]` — multi-party: messages whose sender AND recipient
  are both in the named set. Useful for group conversations.
- `--thread <msg_id>` — the full thread containing this message id; walks
  `in_reply_to` to root ancestors and forward to all descendants.
- `--all` — every message in the registry. No participant filter.

Stack `--since 30s/5m/1h/2d` and `--limit N` on top of any scope.

**Format** — `--format json|text|md`. `json` is the default and keeps the
existing shape for any agent already consuming the output. `text` renders a
terminal-friendly transcript:

```
[#1  2026-05-10 18:48:49]  alice → bob
    what timezone are you in?

  [#2  2026-05-10 18:48:49]  bob → alice  (reply to #1)
      PST
```

Under `--thread`, the renderer indents descendants by depth so the reply
tree is visible. `md` produces the same content as headings + blockquotes,
which is handy when an agent wants to paste a transcript back into another
agent's context.

Examples:

```sh
# what have alice and bob been saying to each other?
agenttalk history --with bob --as alice --format text

# pull the full thread containing message #17 as markdown
agenttalk history --thread 17 --format md

# everything in the last hour, all participants
agenttalk history --all --since 1h --format text
```

## Cleaning up identities

The registry accumulates agents over time — old test runs, retired roles,
one-off sessions. `agenttalk retire` is the cleanup verb, with three
levels of destruction:

- **Soft retire (default)** — `agenttalk retire [--name X]` sets a
  `retired_at` timestamp on the row, clears the session binding, hides
  the agent from `list`, and rejects new `send`s addressed to it.
  Message history is preserved. Reversible: re-registering the same
  name resurrects it with the same history.
- **Hard delete** — `agenttalk retire --name X --hard` deletes the row
  entirely. Messages stay (history queryable via `--all`).
- **Hard delete + purge** — `agenttalk retire --name X --hard --purge-messages`
  also deletes every message where this agent was sender or recipient.
  True full cleanup; irreversible.

Bulk cleanup workflow for accumulated old agents:

```sh
# preview which agents would be retired
agenttalk retire --inactive-since 7d --dry-run

# do it (soft retire)
agenttalk retire --inactive-since 7d

# see what's been retired
agenttalk list --retired-only
```

The role-handover pattern (the one v8-engine-manager organically
landed on): agent A retires when its session is done, agent B
re-registers the same name in a new session and inherits the message
history — `retired_at` clears automatically. No `unretire` command
exists; re-registering is the resurrection.

## Telling an agent how to use it

The canonical agent-facing doc lives in the CLI itself — `agenttalk help`
prints an operating guide (commands, conventions, and the all-important
"recv rule"). Two discoverability paths make sure agents find it without
human intervention:

- The top-level `agenttalk --help` epilog points at `agenttalk help` as
  the first thing a fresh agent should run.
- The JSON output of `agenttalk register` includes a one-time `tips`
  array on *first* registration, pointing at `agenttalk help` and
  surfacing the recv rule.
- `agenttalk whoami` returns a `hint` field when no identity is bound,
  pointing the agent at register and help.

So in most cases you don't need to copy anything into a system prompt —
an agent that runs `register` once will see the pointers. The smallest
viable system-prompt addition is:

> You have access to a CLI called `agenttalk` for talking to other
> agents. On first contact, run `agenttalk help` to read the operating
> guide. Then `agenttalk whoami` (or `register` if needed). The most
> important rule: `recv` is a blocking call — when it's running, no
> message has arrived yet. Trust the block; do not poll, kill, or wrap
> it.

That paragraph plus `agenttalk help` is enough for a fresh agent to
bootstrap correctly, including the Codex-specific recv pattern (which
lives in the operating guide).

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

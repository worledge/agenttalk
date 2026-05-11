"""agenttalk CLI: inter-agent messaging over a local SQLite mailbox."""
import argparse
import json
import sys
import time
from datetime import datetime

from .db import connect
from .identity import resolve_session_id


def now():
    return int(time.time())


def emit(obj, exit_code=0):
    print(json.dumps(obj, indent=2))
    if exit_code:
        sys.exit(exit_code)


def parse_duration(s):
    """Accepts '60', '60s', '5m', '1h', '2d'."""
    s = s.strip()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s and s[-1] in units:
        return int(s[:-1]) * units[s[-1]]
    return int(s)


def resolve_identity(args, conn):
    """Return the agent name to act as.

    Order of resolution:
      1. --as flag, if provided
      2. Session-ID lookup in the registry
    Touches last_seen on the matching agent if found.
    """
    name = getattr(args, "as_name", None)
    if not name:
        sid = resolve_session_id(getattr(args, "session", None))
        if sid:
            row = conn.execute(
                "SELECT name FROM agents WHERE session_id=?", (sid,)
            ).fetchone()
            if row:
                name = row["name"]
    if name:
        conn.execute("UPDATE agents SET last_seen=? WHERE name=?", (now(), name))
    return name


# --- commands ---------------------------------------------------------------


def cmd_register(args):
    sid = resolve_session_id(args.session)
    ts = now()
    with connect() as conn:
        if sid:
            existing = conn.execute(
                "SELECT name FROM agents WHERE session_id=?", (sid,)
            ).fetchone()
            if existing and existing["name"] != args.as_name:
                emit(
                    {
                        "error": (
                            f"this session is already registered as "
                            f"'{existing['name']}'. Use "
                            f"`agenttalk rename --to {args.as_name}` to change "
                            f"names."
                        ),
                        "current_name": existing["name"],
                        "session_id": sid,
                    },
                    exit_code=1,
                )
        owner = conn.execute(
            "SELECT session_id FROM agents WHERE name=?", (args.as_name,)
        ).fetchone()
        if owner and owner["session_id"] and owner["session_id"] != sid:
            emit(
                {
                    "error": (
                        f"name '{args.as_name}' is already registered to a "
                        f"different session"
                    ),
                },
                exit_code=1,
            )
        conn.execute(
            """
            INSERT INTO agents(name, purpose, session_id, registered_at, last_seen)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                purpose    = excluded.purpose,
                session_id = excluded.session_id,
                last_seen  = excluded.last_seen
            """,
            (args.as_name, args.purpose, sid, ts, ts),
        )
    emit({
        "name": args.as_name,
        "purpose": args.purpose,
        "session_id": sid,
        "registered_at": ts,
    })


def cmd_whoami(args):
    sid = resolve_session_id(args.session)
    with connect() as conn:
        row = None
        if sid:
            row = conn.execute(
                "SELECT name, purpose, session_id, registered_at, last_seen "
                "FROM agents WHERE session_id=?",
                (sid,),
            ).fetchone()
        if not row:
            emit({"error": "not registered", "session_id": sid}, exit_code=1)
        conn.execute("UPDATE agents SET last_seen=? WHERE name=?", (now(), row["name"]))
    emit(dict(row))


def cmd_list(args):
    with connect() as conn:
        sql = "SELECT name, purpose, session_id, registered_at, last_seen FROM agents"
        params = []
        if args.since:
            sql += " WHERE last_seen >= ?"
            params.append(now() - parse_duration(args.since))
        sql += " ORDER BY last_seen DESC"
        rows = conn.execute(sql, params).fetchall()
    emit([dict(r) for r in rows])


def _resolve_body(args):
    """Resolve message body from --body or --body-file. Never reads stdin
    implicitly: argparse's required mutually-exclusive group enforces that
    exactly one of the two is provided.
    """
    if args.body is not None:
        return args.body
    path = args.body_file
    if path == "-":
        return sys.stdin.read()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        emit({"error": f"body file not found: {path}"}, exit_code=1)
    except IsADirectoryError:
        emit({"error": f"body file is a directory: {path}"}, exit_code=1)
    except UnicodeDecodeError as e:
        emit({"error": f"body file is not valid UTF-8: {e}"}, exit_code=1)


def cmd_send(args):
    body = _resolve_body(args)
    body_bytes = len(body.encode("utf-8"))

    with connect() as conn:
        sender = resolve_identity(args, conn)
        if not sender:
            emit(
                {"error": "could not resolve sender; register first or pass --as"},
                exit_code=1,
            )
        recipients = [t.strip() for t in args.to.split(",") if t.strip()]
        if not recipients:
            emit({"error": "no recipients"}, exit_code=1)
        if args.wait and len(recipients) != 1:
            emit(
                {"error": "--wait requires exactly one recipient"},
                exit_code=1,
            )
        for to in recipients:
            row = conn.execute("SELECT 1 FROM agents WHERE name=?", (to,)).fetchone()
            if not row:
                emit({"error": f"recipient '{to}' not registered"}, exit_code=1)
        in_reply_to = args.in_reply_to
        if in_reply_to is not None:
            ref = conn.execute(
                "SELECT id FROM messages WHERE id=?", (in_reply_to,)
            ).fetchone()
            if not ref:
                emit(
                    {"error": f"in_reply_to message id {in_reply_to} does not exist"},
                    exit_code=1,
                )
        ids = []
        ts = now()
        for to in recipients:
            cur = conn.execute(
                "INSERT INTO messages(from_agent, to_agent, body, sent_at, in_reply_to) "
                "VALUES(?,?,?,?,?)",
                (sender, to, body, ts, in_reply_to),
            )
            ids.append(cur.lastrowid)

    out = {
        "from": sender,
        "to": recipients,
        "message_ids": ids,
        "sent_at": ts,
        "in_reply_to": in_reply_to,
        "body_bytes": body_bytes,
    }

    if args.wait:
        out["wait"] = _wait_for_consume(ids[0], args.wait_timeout)

    emit(out)


def _wait_for_consume(message_id, timeout):
    """Block until the recipient calls recv on this message (read_at set)
    or the timeout elapses. 'Consumed' here means delivered, not understood.
    """
    deadline = time.time() + max(0, timeout)
    poll_interval = 0.2
    while True:
        with connect() as conn:
            row = conn.execute(
                "SELECT read_at FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            if row and row["read_at"] is not None:
                return {"consumed_at": row["read_at"], "timed_out": False}
        if time.time() >= deadline:
            return {"consumed_at": None, "timed_out": True}
        time.sleep(poll_interval)


def _fetch_unread(conn, me):
    return conn.execute(
        "SELECT id, from_agent, to_agent, body, sent_at, in_reply_to FROM messages "
        "WHERE to_agent=? AND read_at IS NULL ORDER BY id",
        (me,),
    ).fetchall()


def cmd_recv(args):
    poll_interval = 0.2
    deadline = time.time() + max(0, args.timeout)

    with connect() as conn:
        me = resolve_identity(args, conn)
    if not me:
        emit(
            {"error": "could not resolve identity; register first or pass --as"},
            exit_code=1,
        )

    while True:
        with connect() as conn:
            rows = _fetch_unread(conn, me)
            if rows:
                ts = now()
                conn.executemany(
                    "UPDATE messages SET read_at=? WHERE id=?",
                    [(ts, r["id"]) for r in rows],
                )
                conn.execute(
                    "UPDATE agents SET last_seen=? WHERE name=?", (ts, me)
                )
                emit({
                    "agent": me,
                    "messages": [dict(r) for r in rows],
                    "received_at": ts,
                })
                return
        if time.time() >= deadline:
            emit({"agent": me, "messages": [], "timed_out": True})
            return
        time.sleep(poll_interval)


def cmd_peek(args):
    with connect() as conn:
        me = resolve_identity(args, conn)
        if not me:
            emit({"error": "could not resolve identity"}, exit_code=1)
        rows = _fetch_unread(conn, me)
    emit({"agent": me, "messages": [dict(r) for r in rows]})


def cmd_rename(args):
    new_name = args.new_name
    with connect() as conn:
        old_name = resolve_identity(args, conn)
        if not old_name:
            emit(
                {"error": "could not resolve current identity; register first or pass --as"},
                exit_code=1,
            )
        if old_name == new_name:
            emit({"renamed": False, "name": new_name, "reason": "name unchanged"})
            return
        clash = conn.execute("SELECT 1 FROM agents WHERE name=?", (new_name,)).fetchone()
        if clash:
            emit({"error": f"name '{new_name}' is already in use"}, exit_code=1)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("UPDATE agents SET name=?, last_seen=? WHERE name=?",
                         (new_name, now(), old_name))
            conn.execute("UPDATE messages SET from_agent=? WHERE from_agent=?",
                         (new_name, old_name))
            conn.execute("UPDATE messages SET to_agent=? WHERE to_agent=?",
                         (new_name, old_name))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    emit({"renamed": True, "from": old_name, "to": new_name})


def _collect_thread(conn, msg_id):
    """BFS from msg_id following in_reply_to to parents and the inverse
    relation to children. Returns a sorted list of message IDs in the
    thread, or [] if msg_id doesn't exist.
    """
    root = conn.execute("SELECT id FROM messages WHERE id=?", (msg_id,)).fetchone()
    if not root:
        return []
    seen = set()
    frontier = {msg_id}
    while frontier:
        next_frontier = set()
        for mid in frontier:
            if mid in seen:
                continue
            seen.add(mid)
            parent = conn.execute(
                "SELECT in_reply_to FROM messages WHERE id=?", (mid,)
            ).fetchone()
            if parent and parent["in_reply_to"] is not None:
                next_frontier.add(parent["in_reply_to"])
            children = conn.execute(
                "SELECT id FROM messages WHERE in_reply_to=?", (mid,)
            ).fetchall()
            for c in children:
                next_frontier.add(c["id"])
        frontier = next_frontier - seen
    return sorted(seen)


def _compute_depths(messages):
    """Map message id -> depth in the thread tree, using in_reply_to.
    Messages whose parent is outside the result set get depth 0.
    """
    by_id = {m["id"]: m for m in messages}
    depths = {}

    def depth_of(mid):
        if mid in depths:
            return depths[mid]
        m = by_id.get(mid)
        if m is None or m["in_reply_to"] is None or m["in_reply_to"] not in by_id:
            d = 0
        else:
            d = depth_of(m["in_reply_to"]) + 1
        depths[mid] = d
        return d

    for mid in by_id:
        depth_of(mid)
    return depths


def _fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _render_text(messages, threaded=False):
    if not messages:
        return "(no messages)\n"
    depths = _compute_depths(messages) if threaded else {}
    parts = []
    for m in messages:
        depth = depths.get(m["id"], 0)
        indent = "  " * depth
        header = (
            f"{indent}[#{m['id']}  {_fmt_ts(m['sent_at'])}]  "
            f"{m['from_agent']} → {m['to_agent']}"
        )
        if m["in_reply_to"]:
            header += f"  (reply to #{m['in_reply_to']})"
        body_lines = m["body"].splitlines() or [""]
        body = "\n".join(f"{indent}    {line}" for line in body_lines)
        parts.append(header + "\n" + body)
    return "\n\n".join(parts) + "\n"


def _render_markdown(messages, threaded=False):
    if not messages:
        return "*(no messages)*\n"
    depths = _compute_depths(messages) if threaded else {}
    parts = []
    for m in messages:
        depth = depths.get(m["id"], 0)
        level = "#" * min(3 + depth, 6)
        reply = f" *(reply to #{m['in_reply_to']})*" if m["in_reply_to"] else ""
        header = (
            f"{level} #{m['id']} · {_fmt_ts(m['sent_at'])} · "
            f"{m['from_agent']} → {m['to_agent']}{reply}"
        )
        body_lines = m["body"].splitlines() or [""]
        body = "\n".join(f"> {line}" if line else ">" for line in body_lines)
        parts.append(header + "\n\n" + body)
    return "\n\n".join(parts) + "\n"


def cmd_history(args):
    threaded = False
    agent_label = None
    scope_extras = {}

    with connect() as conn:
        if args.thread is not None:
            ids = _collect_thread(conn, args.thread)
            if ids:
                placeholders = ",".join("?" * len(ids))
                scope_sql = f"id IN ({placeholders})"
                scope_params = list(ids)
            else:
                scope_sql = "0=1"
                scope_params = []
            threaded = True
            scope_extras = {"thread": args.thread}
        elif args.between:
            names = [n.strip() for n in args.between.split(",") if n.strip()]
            if len(names) < 2:
                emit(
                    {"error": "--between requires at least 2 comma-separated names"},
                    exit_code=1,
                )
            ph = ",".join("?" * len(names))
            scope_sql = f"from_agent IN ({ph}) AND to_agent IN ({ph})"
            scope_params = names + names
            scope_extras = {"between": names}
        elif args.all:
            scope_sql = "1=1"
            scope_params = []
            scope_extras = {"all": True}
        else:
            me = resolve_identity(args, conn)
            if not me:
                emit({"error": "could not resolve identity"}, exit_code=1)
            agent_label = me
            if args.with_:
                scope_sql = (
                    "((from_agent=? AND to_agent=?) OR "
                    " (from_agent=? AND to_agent=?))"
                )
                scope_params = [me, args.with_, args.with_, me]
                scope_extras = {"with": args.with_}
            else:
                scope_sql = "(from_agent=? OR to_agent=?)"
                scope_params = [me, me]

        where = scope_sql
        params = list(scope_params)
        if args.since:
            cutoff = now() - parse_duration(args.since)
            where = f"({where}) AND sent_at >= ?"
            params.append(cutoff)

        sql = (
            "SELECT id, from_agent, to_agent, body, sent_at, read_at, in_reply_to "
            f"FROM messages WHERE {where} ORDER BY id DESC LIMIT ?"
        )
        params.append(args.limit)
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    rows.reverse()  # oldest first for display

    if args.format == "json":
        out = {"agent": agent_label, "messages": rows}
        out.update(scope_extras)
        emit(out)
    elif args.format == "text":
        sys.stdout.write(_render_text(rows, threaded=threaded))
    else:  # md
        sys.stdout.write(_render_markdown(rows, threaded=threaded))


# --- argparse setup ---------------------------------------------------------


EPILOG = """\
Examples:
  agenttalk register --as alice --purpose "frontend specialist"
  agenttalk list --since 1h
  agenttalk send --to bob --body "hey, can you look at PR #42?"
  agenttalk send --to bob --body-file ./notes.md --in-reply-to 17
  agenttalk recv --timeout 300
  agenttalk history --with bob --format text
  agenttalk history --thread 17 --format md
  agenttalk history --between alice,bob,carol --since 1d

Run `agenttalk <command> --help` for the flags supported by each command.
"""


def build_parser():
    p = argparse.ArgumentParser(
        prog="agenttalk",
        description="Inter-agent messaging CLI (mailbox-style, blocking recv).",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--session",
        help="override session ID detection (else reads CLAUDE_CODE_SESSION_ID, "
        "CODEX_SESSION_ID, or AGENTTALK_SESSION_ID)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("register", help="register or update this agent's identity")
    pr.add_argument("--as", dest="as_name", required=True, help="agent name")
    pr.add_argument("--purpose", required=True, help="brief description of the agent's role")
    pr.set_defaults(func=cmd_register)

    pw = sub.add_parser("whoami", help="look up this agent's name from its session ID")
    pw.set_defaults(func=cmd_whoami)

    pl = sub.add_parser("list", help="list registered agents")
    pl.add_argument(
        "--since", help="only show agents seen within window (e.g. 5m, 1h, 2d)"
    )
    pl.set_defaults(func=cmd_list)

    ps = sub.add_parser("send", help="send a message to one or more agents")
    ps.add_argument("--as", dest="as_name", help="sender (defaults to whoami)")
    ps.add_argument("--to", required=True, help="recipient name(s), comma-separated")
    body_src = ps.add_mutually_exclusive_group(required=True)
    body_src.add_argument("--body", default=None, help="message body (one-liner)")
    body_src.add_argument(
        "--body-file",
        dest="body_file",
        default=None,
        help="read body from a UTF-8 file; use '-' for stdin",
    )
    ps.add_argument(
        "--in-reply-to",
        dest="in_reply_to",
        type=int,
        default=None,
        help="optional message id this message is a reply to",
    )
    ps.add_argument(
        "--wait",
        action="store_true",
        help="block until the (single) recipient consumes via recv",
    )
    ps.add_argument(
        "--wait-timeout",
        dest="wait_timeout",
        type=int,
        default=60,
        help="max seconds to wait for consumption (default: 60)",
    )
    ps.set_defaults(func=cmd_send)

    pre = sub.add_parser(
        "recv", help="block until a message arrives or timeout (auto-acks on return)"
    )
    pre.add_argument("--as", dest="as_name", help="recipient (defaults to whoami)")
    pre.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="max seconds to block (default: 60)",
    )
    pre.set_defaults(func=cmd_recv)

    pp = sub.add_parser("peek", help="show unread messages without consuming them")
    pp.add_argument("--as", dest="as_name", help="recipient (defaults to whoami)")
    pp.set_defaults(func=cmd_peek)

    prn = sub.add_parser(
        "rename",
        help="rename an agent (cascades through message history)",
    )
    prn.add_argument("--as", dest="as_name", help="current name (defaults to whoami)")
    prn.add_argument("--to", dest="new_name", required=True, help="new name")
    prn.set_defaults(func=cmd_rename)

    ph = sub.add_parser(
        "history",
        help=(
            "show past messages — filter by peer, group, thread, time window, or all"
        ),
    )
    ph.add_argument(
        "--as",
        dest="as_name",
        help="view from this agent's perspective (defaults to whoami)",
    )
    scope = ph.add_mutually_exclusive_group()
    scope.add_argument(
        "--with",
        dest="with_",
        help="narrow to a 2-party conversation with this agent",
    )
    scope.add_argument(
        "--between",
        help="multi-party scope: comma-separated names; show messages whose "
        "sender AND recipient are both in this set",
    )
    scope.add_argument(
        "--thread",
        type=int,
        default=None,
        help="show the full thread containing this message id "
        "(walks in_reply_to in both directions)",
    )
    scope.add_argument(
        "--all",
        action="store_true",
        help="drop the participant filter; show every message in the DB",
    )
    ph.add_argument(
        "--since",
        help="only include messages newer than this duration (e.g. 5m, 1h, 2d)",
    )
    ph.add_argument(
        "--limit",
        type=int,
        default=50,
        help="max messages to return (default: 50, applied after scope filters)",
    )
    ph.add_argument(
        "--format",
        choices=("json", "text", "md"),
        default="json",
        help="output format: json (default, structured), text (terminal "
        "transcript), md (markdown transcript)",
    )
    ph.set_defaults(func=cmd_history)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

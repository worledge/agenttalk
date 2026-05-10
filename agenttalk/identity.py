"""Session ID detection for the current agent process.

Priority:
  1. AGENTTALK_SESSION_ID  (explicit override)
  2. CLAUDE_CODE_SESSION_ID (Claude Code)
  3. CODEX_SESSION_ID       (Codex CLI)
"""
import os

SESSION_ENV_VARS = (
    "AGENTTALK_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_SESSION_ID",
)


def detect_session_id():
    for var in SESSION_ENV_VARS:
        val = os.environ.get(var)
        if val:
            return val
    return None


def resolve_session_id(override=None):
    if override:
        return override
    return detect_session_id()

#!/usr/bin/env python3
"""Native Windows MCP server letting Hermes request host command execution.

Runs OUTSIDE Docker as a plain Windows process -- it needs to launch real
PowerShell against the host, not a container's shell. Reachable from the
Hermes gateway/dashboard containers at http://host.docker.internal:<port>/mcp
(Docker Desktop for Windows resolves this automatically; no compose changes
needed). Uses Streamable HTTP transport (not classic SSE) because `hermes mcp
add --url` assumes Streamable HTTP by default -- it POSTs JSON-RPC straight
to the URL, which 405s against a two-endpoint SSE server. Register via:

    hermes mcp add host-commands --url http://host.docker.internal:8090/mcp

Trust model: no bearer token (network-only, by design). The SDK's
DNS-rebinding protection defaults to only allowing Host headers of
127.0.0.1/localhost, which rejects requests from containers (they arrive
with Host: host.docker.internal) -- so `host.docker.internal` is added to
the allowed hosts/origins below. Do not port-forward this or bind it to
anything other than 127.0.0.1.

Safety model: `run_command` NEVER executes on first request. It only runs
commands that already match a pattern in allowlist.json (fnmatch globs,
e.g. "docker compose logs *"). Anything else is queued in pending.json and
must be approved interactively via approve-commands.ps1, which appends the
*exact* approved command string back into allowlist.json for next time.

Run: python server.py
Requires: pip install -r requirements.txt
"""
from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

DATA_DIR = Path(os.environ.get("HOST_COMMANDS_DATA_DIR", str(Path.home() / ".hermes" / "host-commands")))
ALLOWLIST_PATH = DATA_DIR / "allowlist.json"
PENDING_PATH = DATA_DIR / "pending.json"
COMMAND_TIMEOUT_SECONDS = int(os.environ.get("HOST_COMMANDS_TIMEOUT", "120"))

# Guards allowlist.json/pending.json read-modify-write against concurrent tool calls.
_lock = threading.Lock()


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        raw = path.read_bytes()
    except OSError:
        return default
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # A `Set-Content` without -Encoding UTF8 (approve-commands.ps1's
        # historical bug) writes non-ASCII reason/command text in the system
        # ANSI codepage, which strict utf-8 rejects. cp1252 fallback recovers
        # the data instead of permanently breaking every run_command/
        # list_pending_commands call on one bad byte.
        text = raw.decode("cp1252", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _structurally_safe(command: str) -> bool:
    """Reject command shapes that fnmatch patterns can't fully constrain.

    fnmatch `*` is greedy: a pattern like `Get-ChildItem -Path X\\*` will match
    `Get-ChildItem -Path X\\* -Recurse -Force | Remove-Item` because `*` swallows
    the pipeline. This guard rejects the structural vectors that turn a read-only
    cmdlet into a write/delete/leak:

    - `|`  pipeline (the primary escape: `Get-X | Remove-Item`)
    - `;`  statement chaining
    - `&`  call operator / background
    - `<` `>`  redirection
    - `` ` `` backtick (line continuation / escape injection)

    Any command containing one of these is NOT auto-runnable, regardless of
    whether it matches an allowlist pattern. It still goes through the
    pending-approval flow where a human can judge it individually.
    """
    return not any(ch in command for ch in "|`;&<>")


def _is_allowed(command: str, patterns: list[str]) -> bool:
    if not _structurally_safe(command):
        return False
    return any(command == p or fnmatch.fnmatch(command, p) for p in patterns)


def _run_powershell(command: str) -> dict:
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        # Truncate to keep tool results well within model context limits.
        return {"exit_code": proc.returncode, "stdout": proc.stdout[-8000:], "stderr": proc.stderr[-4000:]}
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "stdout": "", "stderr": f"Timed out after {COMMAND_TIMEOUT_SECONDS}s"}


server = MCPServer(
    "host-commands",
    description=(
        "Run allowlisted PowerShell commands on Max's Windows host, with "
        "human-in-the-loop approval required for anything not yet allowed."
    ),
)


@server.tool()
def run_command(command: str, reason: str) -> dict:
    """Run a PowerShell command on the host, or queue it for Max's approval.

    Args:
        command: The exact PowerShell command to run.
        reason: Why you need to run it -- shown to Max when he reviews it.
    """
    command = command.strip()
    if not command:
        return {"status": "error", "message": "command must not be empty"}

    with _lock:
        allowlist = _load_json(ALLOWLIST_PATH, [])
        if _is_allowed(command, allowlist):
            result = _run_powershell(command)
            return {"status": "executed", **result}

        pending = _load_json(PENDING_PATH, {})
        for entry in pending.values():
            if entry["command"] == command:
                return {
                    "status": "pending_approval",
                    "message": "Already queued, waiting on Max to approve. Ask again later.",
                }

        request_id = uuid.uuid4().hex[:8]
        pending[request_id] = {
            "command": command,
            "reason": reason,
            "requested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save_json(PENDING_PATH, pending)

    return {
        "status": "pending_approval",
        "message": (
            "This command is not on the allowlist yet. It has been queued for "
            "Max's approval (approve-commands.ps1). Do not assume it ran -- "
            "check back later or ask Max to approve it."
        ),
    }


@server.tool()
def list_pending_commands() -> list[dict]:
    """List commands currently waiting on Max's approval."""
    with _lock:
        pending = _load_json(PENDING_PATH, {})
    return [{"id": rid, **entry} for rid, entry in pending.items()]


@server.tool()
def list_allowed_commands() -> list[str]:
    """List command patterns already approved to run automatically."""
    with _lock:
        return _load_json(ALLOWLIST_PATH, [])


@server.tool()
def approve_command(request_id: str, save: bool = True) -> dict:
    """Approve a pending command: run it, optionally save to the allowlist.

    Programmatic equivalent of answering 'y' in approve-commands.ps1.
    The command is executed immediately and removed from pending.json.

    Args:
        request_id: The 8-char id from list_pending_commands().
        save: When True (default), the exact command string is appended to
            allowlist.json so identical future calls run automatically
            (matches approve-commands.ps1 behavior). When False, the command
            runs once and is NOT persisted -- use this for one-time actions.
    """
    with _lock:
        pending = _load_json(PENDING_PATH, {})
        entry = pending.pop(request_id, None)
        if entry is None:
            return {"status": "error", "message": f"No pending command with id {request_id!r}"}

        command = entry["command"]
        result = _run_powershell(command)

        saved = False
        if save:
            allowlist = _load_json(ALLOWLIST_PATH, [])
            if command not in allowlist:
                allowlist.append(command)
                _save_json(ALLOWLIST_PATH, allowlist)
                saved = True

        _save_json(PENDING_PATH, pending)

    resp = {"status": "executed", "command": command, "saved_to_allowlist": saved}
    if saved:
        resp["allowlist_entry"] = command
    resp.update(result)
    return resp


@server.tool()
def deny_command(request_id: str) -> dict:
    """Deny a pending command: remove it from the queue without running it.

    Programmatic equivalent of answering 'N' in approve-commands.ps1.
    The command is NOT executed and NOT added to the allowlist.

    Args:
        request_id: The 8-char id from list_pending_commands().
    """
    with _lock:
        pending = _load_json(PENDING_PATH, {})
        entry = pending.pop(request_id, None)
        if entry is None:
            return {"status": "error", "message": f"No pending command with id {request_id!r}"}
        _save_json(PENDING_PATH, pending)

    return {
        "status": "denied",
        "command": entry["command"],
        "message": "Command denied and removed from the pending queue.",
    }


@server.tool()
def save_pattern(pattern: str) -> dict:
    """Add a fnmatch glob pattern to the allowlist (no command execution).

    Use this to permanently allow a *class* of commands, e.g.
    "Get-Service -Name *" allows any Get-Service -Name <x> call.
    The pattern is validated for structural safety (no pipes, chaining,
    redirection, call-operator, or backtick) before being saved.

    This does NOT run any command -- it only extends the allowlist.
    Max should confirm the proposed pattern via clarify before Hermes calls
    this tool, so globs are never silently widened.

    Args:
        pattern: A fnmatch glob, e.g. "Get-Service -Name *" or
            "Get-ChildItem -Path C:\\Users\\maxva\\Repositories\\*".
    """
    pattern = pattern.strip()
    if not pattern:
        return {"status": "error", "message": "pattern must not be empty"}

    if not _structurally_safe(pattern):
        return {
            "status": "error",
            "message": (
                "Pattern contains a structurally-unsafe character (| ; & < > or backtick). "
                "Globs that could carry a pipeline or redirection are never saved."
            ),
        }

    with _lock:
        allowlist = _load_json(ALLOWLIST_PATH, [])
        if pattern in allowlist:
            return {"status": "already_present", "pattern": pattern}
        allowlist.append(pattern)
        _save_json(ALLOWLIST_PATH, allowlist)

    return {"status": "saved", "pattern": pattern}


if __name__ == "__main__":
    host = os.environ.get("HOST_COMMANDS_HOST", "127.0.0.1")
    port = int(os.environ.get("HOST_COMMANDS_PORT", "8090"))
    transport_security = TransportSecuritySettings(
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "host.docker.internal:*"],
        allowed_origins=[
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
            "http://host.docker.internal:*",
        ],
    )
    server.run(transport="streamable-http", host=host, port=port, transport_security=transport_security)

# Handover: host-commands MCP server restart

**Context:** Hermes (the WSL agent) needs the host-commands MCP server restarted to pick up the 6-tool code. The running process (PID 2596) is still on the old 3-tool code.

## What's wrong
- `C:\Users\maxva\.hermes\host-commands\server.py` is the OLD version (3 tools: `run_command`, `list_pending_commands`, `list_allowed_commands`).
- `C:\Users\maxva\Repositories\hermes-agent\host-commands-mcp\server.py` is the NEW version (6 tools: adds `approve_command`, `deny_command`, `save_pattern`).
- The running python process (PID 2596, found via `Get-Process -Name python`) was started from the OLD file.

## What to do (3 commands)
```powershell
Stop-Process -Id 2596 -Force
Copy-Item 'C:\Users\maxva\Repositories\hermes-agent\host-commands-mcp\server.py' "$env:USERPROFILE\.hermes\host-commands\server.py" -Force
cd "$env:USERPROFILE\.hermes\host-commands"
Start-Process python -ArgumentList 'server.py' -WorkingDirectory "$env:USERPROFILE\.hermes\host-commands" -WindowStyle Hidden
```

## Verify
After restart, from WSL:
```bash
# Get a fresh MCP session + list tools — should show 6 tools
SID=$(curl -s -D - -X POST http://host.docker.internal:8090/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"probe","version":"1.0"}}}' \
  2>&1 | grep -i mcp-session-id | awk '{print $2}' | tr -d '\r')

curl -s -X POST http://host.docker.internal:8090/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'

curl -s -X POST http://host.docker.internal:8090/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```
Expected: 6 tools in the response.

## After that
Hermes needs a restart (Ctrl+C in TUI, then `hermes`) so the MCP client re-discovers the tool list. New sessions will see all 6 tools.

## Note
The allowlist at `C:\Users\maxva\.hermes\host-commands\allowlist.json` has ~50 patterns and does NOT need to be touched. Only `server.py` changes.

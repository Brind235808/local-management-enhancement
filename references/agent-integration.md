# Agent integration

The runtime generates rules and MCP configs for several agents. Rules live only in
`session/`, so normal projects are unaffected. MCP entries point at the **proxy**; when
the gateway is OFF, the tool fails closed.

## Generated files

| Agent | Session rules | Session MCP config | Launch |
|---|---|---|---|
| Codex | `AGENTS.md`, `.codex/config.toml` (sandbox only) | injected by `isolationctl launch codex` via `-c`; snippet in `private/agent-mcp/codex-mcp.toml` | `isolationctl launch codex` |
| Qoder IDE/CLI | `AGENTS.md`, `QODER.md`, `.qoder/rules/isolation.md` | `private/agent-mcp/qoder-mcp.json` (IDE UI) and `.mcp.json` (CLI) | `isolationctl launch qoder` or open `session/` |
| Trae | `AGENTS.md`, `.trae/rules/project_rules.md` | `.trae/mcp.json` | `isolationctl launch trae` or open `session/` |
| Cursor | `.cursor/rules/isolation.mdc` | `.cursor/mcp.json` | `isolationctl launch cursor` |
| Claude Code | `CLAUDE.md` | `.mcp.json` | `isolationctl launch claude` |
| Gemini CLI | `GEMINI.md` | `.gemini/settings.json` | `isolationctl launch gemini` |
| Windsurf | `.windsurf/rules/isolation.md` | manual: `private/agent-mcp/mcpServers.json` | open `session/` |
| Cline | `.clinerules` | manual: `private/agent-mcp/mcpServers.json` | open `session/` |
| GitHub Copilot | `.github/copilot-instructions.md` | `.vscode/mcp.json` (VS Code `servers` format) | open `session/` |

`isolationctl launch ... --dry-run --json` prints the command without launching. Use it
when the skill is invoked from an automation context that must not open a GUI.

## Codex

- Project rules: `session/AGENTS.md`.
- Sandbox: `session/.codex/config.toml` sets `sandbox_mode = "workspace-write"` and
  `network_access = false` for shell commands. The model API is not affected.
- MCP: `isolationctl launch codex` runs:

  ```text
  codex -C <session> -s workspace-write -a on-request \
    -c mcp_servers.image_gateway.command="<python>" \
    -c mcp_servers.image_gateway.args=["<gateway>","proxy","--url","<gateway-url>"] \
    -c 'mcp_servers.image_gateway.env={ IMAGE_GATEWAY_TOKEN = "<token>" }'
  ```

  This does not modify `~/.codex/config.toml`. If you want the tool in a plain Codex
  session, copy `private/agent-mcp/codex-mcp.toml` into a project config or add it with
  Codex's MCP settings; never add broad read/list tools next to it.
- If `codex mcp list` reports a config parse error, fix the existing
  `~/.codex/config.toml` first. The skill intentionally does not edit it.
- Hard boundary: Codex's own sandbox is not a guarantee that it cannot read outside the
  workspace. Use the Docker agent box or a VM if that matters.

## Qoder

- Rules: `AGENTS.md` is read by current Qoder versions; Qoder also supports
  `.qoder/rules/*.md`. Both are generated.
- MCP in the IDE: open **Settings → MCP → My Services**, paste the JSON from
  `private/agent-mcp/qoder-mcp.json`, save. This is a one-time global entry. When the
  gateway is OFF, the proxy returns an OFF/unreachable error; normal Qoder work is
  unaffected.
- Qoder CLI: the generated `.mcp.json` in the session is a project-level config. Start
  the CLI with the session as its workspace.
- Opening the workspace: `isolationctl launch qoder` locates the running
  `Qoder.exe`/`Qoder CN.exe` (or a shortcut) and passes `session/`. If the already-open
  app ignores the folder argument, open the folder manually.
- Qoder is a GUI app with file/terminal tools; rules are not a hard boundary. Use a VM
  or a separate Windows user (see `isolation-tiers.md`).

## Trae

- Rules: `session/.trae/rules/project_rules.md` (project-scoped). Trae can also import
  `AGENTS.md`/`CLAUDE.md` if those import switches are enabled in Settings → Rules.
- MCP: `session/.trae/mcp.json` is project-scoped. If your Trae version only supports a
  global MCP config, paste `private/agent-mcp/mcpServers.json` there. When OFF, the
  gateway is down.
- Opening: `isolationctl launch trae` detects `Trae CN.exe`/`Trae.exe` or a running
  instance. Open `session/` manually if needed.
- Hard boundary: same as Qoder; use L3 for a real boundary.

## Cursor

- Rules: `.cursor/rules/isolation.mdc` with `alwaysApply: true`.
- MCP: `.cursor/mcp.json` (project). Open the session folder in Cursor; approve the MCP
  server when prompted.
- `isolationctl launch cursor` opens the folder when a Cursor executable is found.

## Claude Code

- Rules: `CLAUDE.md`.
- MCP: `.mcp.json` (project). Start Claude Code with the session as its working
  directory; approve the project MCP server on first use.
- Claude Code has shell access; for a hard boundary, run it inside the Docker agent box
  or a VM.

## Gemini CLI

- Rules: `GEMINI.md`.
- MCP: `.gemini/settings.json` with `mcpServers`. Start Gemini with the session as its
  working directory. If your version merges only the user-level settings, copy the
  `mcpServers` object there.
- Same shell caveat as Claude Code.

## Windsurf, Cline, Copilot

- Rules are generated as listed above.
- Use `private/agent-mcp/mcpServers.json` (or `.vscode/mcp.json` for VS Code/Copilot)
  to add the gateway manually. The `mcpServers` object is the common shape; VS Code's
  `.vscode/mcp.json` uses a top-level `servers` key instead.
- If the host editor has a global MCP entry, leaving it installed is acceptable: the
  proxy only works while the switch is ON. Do not add a second, broad image tool.

## Docker agent box

The `agent` service installs `@openai/codex` by default. Its entrypoint writes
`~/.codex/config.toml` with the container gateway URL and token, then sleeps. Use:

```bash
docker compose --env-file <runtime>/private/docker/.env \
  -f <runtime>/private/docker/docker-compose.yml --profile agent \
  exec agent codex -C /workspace
```

The container mounts only `session/`, its own home, and the read-only gateway source.
It cannot see `private/` or ComfyUI. To install another CLI agent, set
`AGENT_NPM_PACKAGES` in `private/docker/.env` and rebuild.

## Normal usage / OFF

Turning isolation OFF does not change any global agent config. Open your normal
projects as usual. The only lingering artifact for GUI agents is the optional global
MCP entry; it points at a stopped gateway and fails closed. If you prefer, remove that
entry from the app's MCP settings after you finish the isolated work.

Do not copy files from `private/` into `session/` to "help" the agent. That defeats the
separation. For a reference image the user explicitly approves, put one file in
`session/in/` and tell the agent its exact path.

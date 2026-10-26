---
name: local-management-enhancement
description: Scan a local machine for coding agents (Codex, Qoder, Trae, Cursor, Claude Code, Gemini CLI), isolation capabilities (Docker/Podman/WSL/Hyper-V/Windows Sandbox/OS sandbox), and ComfyUI/Ollama/LM Studio; then bootstrap a session-level ON/OFF isolation workspace with a single narrow image-generation MCP gateway, private archives, per-agent rules, and optional Docker/VM configurations. Use for 本地管理能力增强, agent sandbox, 生图隔离, ComfyUI gateway, prompt privacy, reproducible image metadata, or toggling isolated work without changing normal agent setups.
---

# Local Management Enhancement

## Overview

This skill adds a **session-level switch** for agent isolation without rewriting the
agent's normal installation or global configuration. It scans the machine, creates a
dedicated workspace plus a private archive, and exposes exactly one image-generation
MCP tool (`submit_image_generation`) plus `get_generation_status`. When the switch is
off, the gateway is down and the tool fails closed; normal agent work is unchanged.

The security boundary is **not** folder nesting, prompt rewriting, or `AGENTS.md`.
Those are policy layers. The enforced boundary is OS/container/VM permissions around
the agent, plus the gateway having no file-reading tools. State this plainly to the
user; never sell L1 as a hard sandbox.

## Quick start

```bash
# 1. Scan only; no writes to agent configs.
python scripts/scan_environment.py --report scan.md

# 2. Build the runtime once. Replace the workflow with your ComfyUI API export.
python scripts/bootstrap_isolation.py --root /path/to/runtime \
  --workflow /path/to/comfy_api_workflow.json

# 3. Turn isolation ON and open the isolated session.
python scripts/isolationctl.py --root /path/to/runtime on
python scripts/isolationctl.py --root /path/to/runtime launch codex    # or qoder, trae

# 4. Turn it OFF; normal projects and agent configs are untouched.
python scripts/isolationctl.py --root /path/to/runtime off
```

On Windows use `python`; on Linux/macOS use `python3`. Python 3.10+ is required.
The first `on` refuses to start if the workflow is still the example or ComfyUI is
unreachable; override only deliberately with `--allow-placeholder-workflow` and/or
`--allow-missing-comfy`.

## What gets created

```
runtime/
  session/                  # agent opens only this when isolation is ON
    in/ out/                # user-provided references / agent scratch
    AGENTS.md, QODER.md, CLAUDE.md, GEMINI.md
    .qoder/rules/, .trae/rules/, .cursor/rules/, .windsurf/rules/
    .mcp.json, .cursor/mcp.json, .trae/mcp.json, .codex/config.toml
  private/                  # never mounted/opened by the agent
    archive/prompts/        # private prompt/history area
    archive/outputs/        # generated originals (metadata preserved by default)
    config/gateway.json     # host gateway config
    gateway/                # gateway runtime copy
    agent-mcp/              # snippets for one-time GUI MCP setup
    docker/, agentbox/      # optional L2 container assets
    state.json              # switch state
  launchers/                # isolate-codex.cmd/.sh, qoder, trae, ...
  MANIFEST.json
```

The gateway refuses to start if `output_dir` overlaps the agent workspace. There is no
path/filename/URL argument in any MCP tool, so path traversal cannot be expressed
through the tool. Unknown JSON properties are rejected by schema and runtime checks.

## Metadata and reproducibility

**Default: preserve the original bytes.** The gateway downloads the image from
ComfyUI and writes it to `private/archive/outputs/` unchanged. ComfyUI prompt/workflow
metadata is kept, so the image can be reproduced later. ComfyUI's own output directory
also remains untouched. Do **not** add `--disable-metadata` by default.

Only if the user explicitly asks for a shareable/sanitized copy:

- Set `"metadata_mode": "strip"` in `private/config/gateway.json` (or run bootstrap
  with `--metadata-mode strip`), or
- Run `python scripts/strip_metadata.py <copy.png>` on a copy.

Never strip the only original. Redaction and metadata stripping are privacy aids,
not isolation boundaries. Prompt redaction (`scripts/prompt_redact.py`) is for prompts
sent to cloud LLM APIs; keep its map in `private/` and use it only when the prompt
leaves the machine.

## Switch semantics

- `on` / `enable`: validates config/workflow, starts the host gateway or Docker stack,
  waits for ComfyUI, writes `state.json`. Failure rolls back to OFF.
- `launch <agent>`: requires ON and healthy; opens the session workspace with the
  MCP gateway injected. Codex additionally gets `-s workspace-write -a on-request`.
- `off` / `disable`: stops the gateway/stack; archives, token, and config are preserved.
- `status [--json]`: shows ON/OFF, backend, ComfyUI reachability, workflow readiness.
- Choices for `launch`: `codex`, `qoder`, `trae`, `cursor`, `claude`, `gemini`.
- Docker: `on --backend docker [--with-agent]`; GUI apps cannot run in the agent box.

Agent global configs are not modified by this skill. Rules live only in `session/`,
so normal work in other folders is unaffected. For Qoder/Trae GUI apps, a global MCP
entry may be installed once from `private/agent-mcp/`; when OFF, the proxy is
unreachable, so the tool fails closed while the rest of the app works normally.

## Isolation strength

- **L1 host gateway + session rules** (default): stops a tool-minimized agent from
  using a broad file/image tool; does **not** stop Codex/Qoder/Trae shell or native
  file tools from reading outside the session. It is workflow isolation, not a hard
  boundary.
- **L2 Docker agent box**: the agent container mounts only `session/` and its own
  home, never `private/`; ComfyUI is on `ai-net`, the agent is not; the gateway is the
  only bridge. This is a hard filesystem boundary for CLI agents. Requires Docker.
- **L3 VM / Windows Sandbox / separate OS user**: use for GUI agents when a hard
  boundary is required. The switch supports `external` start/stop hooks in
  `private/config/switch.json`. See `references/isolation-tiers.md` for ACL commands
  and honest limitations.

`scan_environment.py` recommends a tier from what is actually installed. On a machine
without Docker/WSL/VM, it recommends L1 and warns accordingly.

## Agent integration

Read `references/agent-integration.md` for exact rule/MCP paths and one-time setup.
Key points:

- **Codex**: `session/AGENTS.md`; sandbox settings in `session/.codex/config.toml`;
  MCP is injected by `isolationctl launch codex` (no global `codex mcp add`).
- **Qoder**: `session/AGENTS.md` + `session/.qoder/rules/`; MCP via Settings → MCP
  using `private/agent-mcp/qoder-mcp.json`, or project `.mcp.json` for Qoder CLI.
- **Trae**: `session/AGENTS.md` + `.trae/rules/project_rules.md`; `.trae/mcp.json`.
- **Cursor/Claude/Gemini**: project MCP files are generated in the session.
- **Windsurf/Cline/Copilot**: rules are generated; use the generic MCP snippet
  manually. All MCP entries point at the proxy, so OFF means fail-closed.

Launch GUI apps by opening `session/` (the launcher does this when it can locate the
executable). If the app is already running and ignores the folder argument, open the
folder manually. Do not copy private archive paths into the workspace.

## Docker path

```bash
python scripts/bootstrap_isolation.py --root /path/to/runtime --tier 2 \
  --workflow /path/to/comfy_api_workflow.json
python scripts/isolationctl.py --root /path/to/runtime on --backend docker --with-agent
# Inside the agent box:
#   docker compose --env-file <runtime>/private/docker/.env \
#     -f <runtime>/private/docker/docker-compose.yml --profile agent \
#     exec agent codex -C /workspace
python scripts/isolationctl.py --root /path/to/runtime off
```

The compose stack uses AI-Dock's ComfyUI image and keeps metadata enabled by default.
Models mount from `private/models/`. GPU users can uncomment the `deploy` block or add
an override; the base stack is CPU/daemon-agnostic. Docker was not installed on the
development host for this skill, so the host gateway path is covered by `selftest.py`,
while the Docker path must be validated after installing Docker.

## Verification

Run the built-in end-to-end test with a stub ComfyUI (no GPU, Docker, or real ComfyUI):

```bash
python scripts/selftest.py
```

It checks scanning, bootstrap layout, ON/OFF, HTTP and MCP submission, strict rejection
of a path argument, metadata preservation by default, output-inside-workspace refusal,
launcher command generation, and fail-closed behavior after OFF. Additionally verify:

- `isolationctl status --json` shows ON and ComfyUI reachable.
- The agent's `tools/list` exposes exactly the two image tools (no read/list/glob).
- A path-like argument is rejected; the archive is outside `session/`.
- With OFF, the MCP proxy returns an OFF/unreachable error instead of silently starting.

## Resources

- `references/isolation-tiers.md` — threat model, L1/L2/L3, Windows ACL/VM notes.
- `references/agent-integration.md` — per-agent rule/MCP paths, launch guidance.
- `references/comfyui.md` — API workflow, placeholders, metadata policy, Docker, failures.
- `scripts/` — scanner, bootstrap, switch, gateway, metadata stripping, redaction, tests.
- `assets/` — rules template, API workflow example, compose, Dockerfiles.

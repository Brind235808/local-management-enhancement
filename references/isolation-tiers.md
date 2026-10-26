# Isolation tiers and honest limits

## Threat model

The agent (LLM plus its tools) may be prompt-injected by workspace content, filenames,
web pages, or user-supplied text. A successful injection can try to:

- enumerate and read prompt archives, prior outputs, model files, or API keys;
- call ComfyUI/Ollama directly and browse `/history`;
- write prompt metadata into files or exfiltrate it to a cloud API;
- start its own image tool or shell loop to bypass the intended workflow.

The skill's answer is: **one narrow capability + an enforced filesystem boundary**.
Folder nesting, renaming, and prompt rewriting are not boundaries. A same-user process
with `os.walk` can traverse any subtree it can read; only permissions, containers, or
VMs change that.

## Tier comparison

| Tier | What it is | Stops | Does not stop |
|---|---|---|---|
| L1 | Host gateway + private archive + session-scoped rules + dedicated workspace | Tool-minimized agent using a broad file/image tool; accidental archive scanning by an agent that only has the gateway; direct path arguments (schema rejects them) | Codex/Qoder/Trae native shell/file tools reading outside `session/`; a same-OS-user process reading `private/`; prompt injection that runs shell commands |
| L2 | L1 plus Docker agent box, no private mounts, split networks, gateway sidecar | Shell/file reads outside the mounted `session/`; direct ComfyUI access from the agent; archive access; host home/credential access | Kernel/container escape; malicious Docker daemon config; an agent explicitly given host mounts or extra networks; GUI apps that cannot run in the box |
| L3 | L1 plus VM / Windows Sandbox / separate OS user with ACLs | Host file access from GUI agents; most same-user bypasses; persistence outside the VM | VM escape; host shared folders you map incorrectly; admin-level malware inside the guest |

Use **L2 for CLI agents** when you need a real filesystem boundary. Use **L3 for
Qoder/Trae/Cursor-style GUI apps** when they must be hard-isolated. Use **L1** only
when you accept a policy boundary (for example, a custom scheduler that truly exposes
only the MCP tool and no shell).

## L1: session switch

What is enforced by code:

- The MCP tool schema has no path, filename, URL, or base64 image field. Unknown
  properties are rejected before anything reaches ComfyUI.
- The gateway validates the workflow, substitutes text/number placeholders, and writes
  output only under `private/archive/outputs/`.
- The gateway refuses to start if `output_dir` overlaps `workspace_root`; workflow and
  output paths must also be outside the workspace.
- The original image bytes are preserved by default, so the archive remains
  reproducible. This is safe **only because the archive is private**. Do not put
  `private/` inside `session/`, a shared folder, or a cloud-synced directory.
- With the gateway OFF, the MCP proxy cannot reach it and returns an error. It does not
  start ComfyUI or fall back to a broad tool.

What is policy only:

- `AGENTS.md`, `.qoder/rules/`, `.trae/rules/`, `.cursor/rules/`, and the other rule
  files. They tell a well-behaved model what to do; they do not stop a shell command.
- The session workspace is the only folder the agent is *asked* to use. On the host,
  a shell-capable agent can still read `C:\Users\...` or `~/...` unless the OS denies it.

## L2: Docker agent box

Design used by `docker-compose.yml`:

- `comfyui` is attached only to `ai-net` (internal). No host port.
- `ollama` is attached only to `llm-net` (internal).
- `gateway` is attached to `ai-net` + `agent-net`; no egress network. It is the only
  component that can reach ComfyUI.
- `agent` is attached to `agent-net` (gateway), `llm-net` (Ollama), and `egress-net`
  (internet for cloud LLM APIs). It cannot reach ComfyUI and has no `private/` mount.
- The agent mounts `session/` at `/workspace`, the gateway source read-only at
  `/opt/image-gateway`, and a named volume at `/home/node`. It never mounts the host
  home, SSH keys, private archive, or the Docker socket.
- The gateway is published only on `127.0.0.1:<port>` for the host MCP proxy and the
  switch health check.

Enable it with:

```bash
python scripts/bootstrap_isolation.py --root <runtime> --tier 2 --workflow <api.json>
python scripts/isolationctl.py --root <runtime> on --backend docker --with-agent
docker compose --env-file <runtime>/private/docker/.env \
  -f <runtime>/private/docker/docker-compose.yml --profile agent \
  exec agent codex -C /workspace
```

The agent box installs `@openai/codex` by default (`AGENT_NPM_PACKAGES` build arg).
Add other CLI agents there, not to the host. Authenticate inside the container so tokens
land in the named volume, not in a host home directory.

Limitations:

- GUI apps (Qoder/Trae/Cursor) cannot run in this box; use L3.
- Docker Desktop on Windows uses WSL2; bind-mount ownership and GPU availability must
  be checked. On Linux, set `GATEWAY_UID`/`GATEWAY_GID` in `private/docker/.env` to the
  host user that owns `private/archive` if the default 1000 does not match.
- The gateway token is available to the agent (it is in the MCP config). It grants only
  `submit` and `status`, not reads. Add rate limiting at a reverse proxy if resource
  abuse matters.
- `ai-net` is internal, so the ComfyUI container cannot download models at runtime.
  Put models in `private/models/` before starting.

## L3: VM, Windows Sandbox, or a separate OS user

### Windows: separate standard user + NTFS deny

This is the most practical hard boundary for a GUI agent on a single machine. Run the
agent as a different, standard (non-admin) Windows account; deny that account access to
`private/` and grant only `session/`.

```powershell
# Elevated PowerShell. Create a standard local user once.
net user isoagent /add
icacls "<runtime>\private" /deny "isoagent:(OI)(CI)F"
icacls "<runtime>\session" /grant "isoagent:(OI)(CI)M"

# Launch the GUI app as isoagent (install it for that user first if necessary).
runas /user:isoagent "C:\Path\To\Qoder CN.exe" "<runtime>\session"
```

Undo the deny when you no longer need it:

```powershell
icacls "<runtime>\private" /remove:d "isoagent"
```

Caveats:

- The agent must actually run under `isoagent`. If you double-click the normal shortcut
  in your own session, it runs as you and can read `private/`.
- Per-user GUI installations may not be visible to `isoagent`; install the app for that
  account or use a machine-wide installation.
- `runas` does not provide a separate desktop by itself; use Fast User Switching or a
  dedicated VM if you need stronger separation.
- Test with `whoami` inside the agent's terminal and `dir "<runtime>\private"`; it must
  be denied.

### Windows Sandbox

Map only `session/` read-write; never map `private/`. A `.wsb` file can disable
networking (but then cloud LLM APIs fail) or enable it. The sandbox is disposable and
starts fresh, so installing Qoder/Trae inside each session is painful. Use it for
one-off checks, not a daily workflow. Hyper-V is better for persistence.

### Hyper-V / VM

Best isolation for GUI agents:

- Keep `private/` on the host; share only `session/` with the guest.
- Install the agent inside the guest.
- Forward only the gateway port (or run a proxy) into the guest; do not expose ComfyUI
  or the host filesystem.
- Use snapshots/checkpoints to roll back.

The switch can drive a VM with external hooks. Put this in
`<runtime>/private/config/switch.json`:

```json
{
  "backend": "external",
  "external": {
    "start": ["powershell", "-NoProfile", "-Command", "Start-VM -Name AgentIso"],
    "stop": ["powershell", "-NoProfile", "-Command", "Stop-VM -Name AgentIso -Force"],
    "health_url": "http://127.0.0.1:18765"
  }
}
```

Then:

```bash
python scripts/isolationctl.py --root <runtime> on --backend external
python scripts/isolationctl.py --root <runtime> off
```

The health URL must reach the gateway inside/forwarded into the VM. If the VM start
succeeds but health fails, the switch runs the stop commands and returns an error.

## Prompt redaction and metadata

- Redaction (`prompt_redact.py`) is for prompts that leave the machine to a cloud LLM.
  It replaces known sensitive strings with tokens and keeps the map locally. It cannot
  stop an agent from reading existing images or prompts.
- Metadata stripping (`strip_metadata.py`) is for **copies that leave the private
  archive**. The gateway preserves original metadata by default for reproducibility.
  Do not strip the only original; do not assume metadata removal is isolation.
- ComfyUI's own output/history and logs may contain prompts. Keep them on the private
  side, exclude them from backups sent to cloud storage, and restrict ACLs.

## Verification checklist

1. `python scripts/selftest.py` passes.
2. `isolationctl status --json` shows ON and ComfyUI reachable.
3. MCP `tools/list` returns exactly two tools; `submit_image_generation` has no path
   fields and `additionalProperties: false`.
4. Submitting `{"prompt":"x","image_path":"..."}` returns an error at both MCP and HTTP.
5. The saved image is under `private/archive/outputs/`, outside `session/`, and its bytes
   match the ComfyUI response (metadata preserved).
6. With OFF, the MCP proxy returns an OFF/unreachable error.
7. For L2, from inside the agent container:
   - `ls /workspace` works;
   - `ls /data/archive` does not exist;
   - `curl http://comfyui:8188` fails;
   - `curl http://gateway:8765/health` works;
   - `curl http://ollama:11434` works only if you intentionally allowed local LLM.
8. For L3, run `whoami` and attempt to list `private/`; access must be denied.

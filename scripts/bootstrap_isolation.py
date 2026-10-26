#!/usr/bin/env python3
"""Bootstrap a session-level isolated workspace and the on/off switch assets."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
ASSETS_DIR = SKILL_DIR / "assets"
sys.path.insert(0, str(SCRIPT_DIR))

import image_gateway as gatewaymod  # noqa: E402
import scan_environment as scanmod  # noqa: E402

NAME = "local-management-enhancement"
SCHEMA_VERSION = 1
AGENT_IDS = [
    "codex",
    "qoder",
    "trae",
    "cursor",
    "claude",
    "gemini",
    "windsurf",
    "cline",
    "copilot",
]
DEFAULT_PORT = 18765
DEFAULT_COMFY_URL = "http://127.0.0.1:8188"
DEFAULT_COMFY_IMAGE = "ghcr.io/ai-dock/comfyui:latest-cuda"
DEFAULT_OLLAMA_IMAGE = "ollama/ollama:latest"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_text(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    if mode is not None and os.name != "nt":
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def _write_json(path: Path, payload: Any, mode: int | None = None) -> None:
    _write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        mode=mode,
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _toml_quote(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_quote(value) for value in values) + "]"


def _find_free_port(start: int = DEFAULT_PORT) -> int:
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free localhost port found for the gateway.")


def _private_mode(root: Path) -> None:
    if os.name == "nt":
        return
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass


def _selected_agents(value: str, detected: list[str]) -> list[str]:
    if value.strip().lower() == "all":
        return list(AGENT_IDS)
    if value.strip().lower() == "auto":
        return detected or list(AGENT_IDS)
    result: list[str] = []
    for item in value.split(","):
        agent = item.strip().lower()
        if not agent:
            continue
        if agent not in AGENT_IDS:
            raise ValueError(f"Unknown agent '{agent}'. Known: {', '.join(AGENT_IDS)}")
        if agent not in result:
            result.append(agent)
    return result or detected or list(AGENT_IDS)


def _render_rules(session_dir: Path, gateway_url: str) -> str:
    template = (ASSETS_DIR / "agent-rules.md.template").read_text(encoding="utf-8")
    return (
        template.replace("{{SESSION_DIR}}", str(session_dir))
        .replace("{{GATEWAY_URL}}", gateway_url)
    )


def _write_rules(session_dir: Path, gateway_url: str) -> None:
    body = _render_rules(session_dir, gateway_url)
    targets = {
        "AGENTS.md": body,
        "QODER.md": body,
        "CLAUDE.md": body,
        "GEMINI.md": body,
        ".qoder/rules/isolation.md": body,
        ".trae/rules/project_rules.md": body,
        ".windsurf/rules/isolation.md": body,
        ".clinerules": body,
        ".github/copilot-instructions.md": body,
        ".cursor/rules/isolation.mdc": (
            "---\n"
            "description: Isolated session file and image-generation boundaries\n"
            "alwaysApply: true\n"
            "---\n\n"
            + body
        ),
    }
    for relative, content in targets.items():
        _write_text(session_dir / relative, content)


def _mcp_server(python_exe: str, gateway_script: Path, url: str, token: str) -> dict[str, Any]:
    return {
        "command": python_exe,
        "args": [str(gateway_script), "proxy", "--url", url],
        "env": {"IMAGE_GATEWAY_TOKEN": token},
    }


def _write_mcp_configs(
    session_dir: Path,
    private_dir: Path,
    python_exe: str,
    gateway_script: Path,
    url: str,
    token: str,
) -> None:
    server = _mcp_server(python_exe, gateway_script, url, token)
    wrapper = {"mcpServers": {"image_gateway": server}}
    for relative in (".mcp.json", ".cursor/mcp.json", ".trae/mcp.json"):
        _write_json(session_dir / relative, wrapper)
    _write_json(session_dir / ".gemini/settings.json", wrapper)
    _write_json(
        session_dir / ".vscode/mcp.json",
        {"servers": {"image_gateway": {"type": "stdio", **server}}},
    )
    agent_mcp = private_dir / "agent-mcp"
    agent_mcp.mkdir(parents=True, exist_ok=True)
    for name in (
        "mcpServers.json",
        "qoder-mcp.json",
        "claude-mcp.json",
        "cursor-mcp.json",
        "trae-mcp.json",
    ):
        _write_json(agent_mcp / name, wrapper)
    codex_toml = (
        "[mcp_servers.image_gateway]\n"
        f"command = {_toml_quote(server['command'])}\n"
        f"args = {_toml_array(server['args'])}\n"
        "env = { IMAGE_GATEWAY_TOKEN = "
        + _toml_quote(token)
        + " }\n"
    )
    _write_text(agent_mcp / "codex-mcp.toml", codex_toml)

    docker_server = {
        "command": "python3",
        "args": [
            "/opt/image-gateway/image_gateway.py",
            "proxy",
            "--url",
            "http://gateway:8765",
        ],
        "env": {"IMAGE_GATEWAY_TOKEN": token},
    }
    docker_wrapper = {"mcpServers": {"image_gateway": docker_server}}
    _write_json(agent_mcp / "docker-mcpServers.json", docker_wrapper)
    _write_text(
        agent_mcp / "docker-codex-mcp.toml",
        "[mcp_servers.image_gateway]\n"
        f"command = {_toml_quote('python3')}\n"
        f"args = {_toml_array(docker_server['args'])}\n"
        "env = { IMAGE_GATEWAY_TOKEN = " + _toml_quote(token) + " }\n",
    )
    _write_text(
        session_dir / ".codex/config.toml",
        "# Generated by local-management-enhancement.\n"
        "# MCP is injected by `isolationctl launch codex`; do not add broad file tools here.\n"
        'sandbox_mode = "workspace-write"\n'
        'approval_policy = "on-request"\n\n'
        "[sandbox_workspace_write]\n"
        "network_access = false\n",
    )


def _write_launchers(root: Path, python_exe: str) -> None:
    isolationctl = SCRIPT_DIR / "isolationctl.py"
    launchers = root / "launchers"
    launchers.mkdir(parents=True, exist_ok=True)
    for agent in ("codex", "qoder", "trae", "cursor", "claude", "gemini"):
        cmd = (
            "@echo off\n"
            "setlocal\n"
            f'"{python_exe}" "{isolationctl}" --root "{root}" launch {agent} %*\n'
        )
        _write_text(launchers / f"isolate-{agent}.cmd", cmd)
        sh = (
            "#!/usr/bin/env sh\n"
            f'exec "{python_exe}" "{isolationctl}" --root "{root}" launch {agent} "$@"\n'
        )
        _write_text(launchers / f"isolate-{agent}.sh", sh, mode=0o755)


def _copy_gateway_runtime(private_dir: Path) -> None:
    gateway_dir = private_dir / "gateway"
    gateway_dir.mkdir(parents=True, exist_ok=True)
    for name in ("image_gateway.py", "strip_metadata.py", "prompt_redact.py"):
        shutil.copy2(SCRIPT_DIR / name, gateway_dir / name)
    shutil.copy2(ASSETS_DIR / "Dockerfile.gateway", gateway_dir / "Dockerfile")


def _write_docker_assets(
    private_dir: Path,
    token: str,
    port: int,
    comfy_image: str,
) -> None:
    docker_dir = private_dir / "docker"
    docker_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ASSETS_DIR / "docker-compose.yml", docker_dir / "docker-compose.yml")
    agentbox = private_dir / "agentbox"
    agentbox.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ASSETS_DIR / "Dockerfile.agent", agentbox / "Dockerfile")
    shutil.copy2(ASSETS_DIR / "entrypoint.agent.sh", agentbox / "entrypoint.sh")
    if os.name != "nt":
        try:
            os.chmod(agentbox / "entrypoint.sh", 0o755)
        except OSError:
            pass
    models = private_dir / "models"
    for sub in ("checkpoints", "loras", "vae", "controlnet"):
        path = models / sub
        path.mkdir(parents=True, exist_ok=True)
        keep = path / ".gitkeep"
        if not keep.exists():
            keep.write_text("", encoding="utf-8")

    env_path = docker_dir / ".env"
    env: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    gateway_uid = str(os.getuid()) if hasattr(os, "getuid") else "1000"
    gateway_gid = str(os.getgid()) if hasattr(os, "getgid") else "1000"
    env.update(
        {
            "COMPOSE_PROJECT_NAME": NAME,
            "IMAGE_GATEWAY_TOKEN": token,
            "GATEWAY_PORT": str(port),
            "GATEWAY_UID": env.get("GATEWAY_UID", gateway_uid),
            "GATEWAY_GID": env.get("GATEWAY_GID", gateway_gid),
            "COMFYUI_IMAGE": env.get("COMFYUI_IMAGE", comfy_image),
            "OLLAMA_IMAGE": env.get("OLLAMA_IMAGE", DEFAULT_OLLAMA_IMAGE),
            "COMFYUI_MODELS_DIR": env.get("COMFYUI_MODELS_DIR", "../models"),
            "AGENT_NPM_PACKAGES": env.get("AGENT_NPM_PACKAGES", "@openai/codex"),
        }
    )
    _write_text(
        env_path,
        "\n".join(f"{key}={value}" for key, value in env.items()) + "\n",
        mode=0o600,
    )


def _write_switch(private_dir: Path, backend: str) -> dict[str, Any]:
    path = private_dir / "config" / "switch.json"
    if path.exists():
        data = _read_json(path)
        if data.get("backend") in ("host", "docker", "external"):
            return data
    data = {
        "backend": backend,
        "external": {
            "start": [],
            "stop": [],
            "health_url": "",
        },
    }
    _write_json(path, data, mode=0o600)
    return data


def _harden_private(private_dir: Path) -> None:
    if os.name != "nt":
        try:
            os.chmod(private_dir, 0o700)
        except OSError:
            pass


def _manifest_summary(manifest: dict[str, Any]) -> str:
    lines = [
        f"{NAME} runtime",
        f"Root:      {manifest['root']}",
        f"Tier:      L{manifest['tier']} ({manifest['backend']} backend)",
        f"Session:   {manifest['paths']['session']}",
        f"Private:   {manifest['paths']['private']}",
        f"Gateway:   {manifest['gateway']['url']}",
        f"Agents:    {', '.join(manifest['agents'])}",
        f"Workflow:  {'ready' if manifest['workflow_ready'] else 'NOT READY (replace the example workflow)'}",
        f"Metadata:  {manifest.get('metadata_mode', 'preserve')}",
    ]
    if manifest.get("warnings"):
        lines.append("Warnings:")
        for warning in manifest["warnings"]:
            lines.append(f"- {warning}")
    lines.append("")
    lines.append("Next:")
    lines.append(f"  python {manifest['paths']['isolationctl']} --root \"{manifest['root']}\" on")
    lines.append(f"  python {manifest['paths']['isolationctl']} --root \"{manifest['root']}\" launch codex")
    return "\n".join(lines)


def bootstrap(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).expanduser().resolve()
    if root.exists():
        entries = list(root.iterdir())
        if entries and not (root / "MANIFEST.json").exists() and not args.force:
            raise SystemExit(
                f"Refusing to write into non-empty directory without MANIFEST.json: {root}\n"
                "Use an empty directory or pass --force."
            )
    root.mkdir(parents=True, exist_ok=True)

    scan_report = scanmod.scan()
    detected = list(scan_report["recommendation"]["detected_agents"])
    selected = _selected_agents(args.agents, detected)
    if args.tier == "auto":
        tier = int(scan_report["recommendation"]["recommended_tier"])
    else:
        tier = int(args.tier)
    backend = "docker" if tier >= 2 else "host"

    session_dir = root / "session"
    private_dir = root / "private"
    config_dir = private_dir / "config"
    gateway_script = private_dir / "gateway" / "image_gateway.py"
    archive_dir = private_dir / "archive"
    for path in (
        session_dir / "in",
        session_dir / "out",
        private_dir / "archive" / "prompts",
        private_dir / "archive" / "outputs",
        config_dir,
        private_dir / "gateway",
        private_dir / "redaction",
        private_dir / "logs",
    ):
        path.mkdir(parents=True, exist_ok=True)
    redaction_map = private_dir / "redaction" / "map.json"
    if not redaction_map.exists():
        _write_json(redaction_map, {"version": 1, "terms": {}}, mode=0o600)
    for keep_dir in (session_dir / "in", session_dir / "out"):
        keep = keep_dir / ".gitkeep"
        if not keep.exists():
            keep.write_text("", encoding="utf-8")

    existing_config = _read_json(config_dir / "gateway.json")
    token_path = config_dir / "token.txt"
    if args.reset_config:
        token = secrets.token_urlsafe(32)
        _write_text(token_path, token + "\n", mode=0o600)
        port = _find_free_port(DEFAULT_PORT)
        existing_config = {}
    else:
        if token_path.exists():
            token = token_path.read_text(encoding="utf-8").strip()
        elif existing_config.get("token"):
            token = str(existing_config["token"])
        else:
            token = secrets.token_urlsafe(32)
            _write_text(token_path, token + "\n", mode=0o600)
        if not token_path.exists():
            _write_text(token_path, token + "\n", mode=0o600)
        try:
            port = int(existing_config.get("http_port") or DEFAULT_PORT)
        except (TypeError, ValueError):
            port = DEFAULT_PORT
        if port == DEFAULT_PORT:
            # Keep an existing port even if another process happens to hold it;
            # isolationctl will report the conflict instead of silently moving.
            port = int(existing_config.get("http_port") or _find_free_port(DEFAULT_PORT))

    if args.comfy_url:
        comfy_url = args.comfy_url.rstrip("/")
    elif existing_config.get("comfy_url"):
        comfy_url = str(existing_config["comfy_url"]).rstrip("/")
    else:
        comfy_url = DEFAULT_COMFY_URL

    workflow_path = config_dir / "comfy_api_workflow.json"
    example_path = ASSETS_DIR / "comfy_api_workflow.example.json"
    workflow_ready = False
    workflow_warning = ""
    if args.workflow:
        source = Path(args.workflow).expanduser().resolve()
        if not source.exists():
            raise SystemExit(f"Workflow file not found: {source}")
        shutil.copy2(source, workflow_path)
    elif not workflow_path.exists():
        shutil.copy2(example_path, workflow_path)
    try:
        example_text = example_path.read_text(encoding="utf-8")
        current_text = workflow_path.read_text(encoding="utf-8")
        gatewaymod._read_workflow(workflow_path)
        workflow_ready = "REPLACE_WITH_YOUR_MODEL" not in current_text and current_text != example_text
        if not workflow_ready:
            workflow_warning = (
                "The workflow is still the example. Replace "
                f"{workflow_path} with your ComfyUI API-format workflow "
                "(replace REPLACE_WITH_YOUR_MODEL.safetensors)."
            )
    except (OSError, gatewaymod.ConfigError) as exc:
        workflow_warning = f"Workflow validation failed: {exc}"

    config = dict(existing_config)
    config.update(
        {
            "comfy_url": comfy_url,
            "workflow_path": str(workflow_path),
            "output_dir": str(archive_dir / "outputs"),
            "workspace_root": str(session_dir),
            "http_host": "127.0.0.1",
            "http_port": port,
            "token": token,
        }
    )
    config.setdefault("request_timeout_sec", 30)
    config.setdefault("download_timeout_sec", 60)
    config.setdefault("max_output_images", 8)
    config.setdefault("max_download_bytes", 64 * 1024 * 1024)
    if args.metadata_mode:
        config["metadata_mode"] = args.metadata_mode
    else:
        config.setdefault("metadata_mode", "preserve")
    _write_json(config_dir / "gateway.json", config, mode=0o600)

    docker_config = _read_json(config_dir / "gateway.docker.json")
    docker_config.update(
        {
            "comfy_url": "http://comfyui:8188",
            "workflow_path": "/data/config/comfy_api_workflow.json",
            "output_dir": "/data/archive/outputs",
            "workspace_root": "",
            "http_host": "0.0.0.0",
            "http_port": 8765,
            "token": token,
        }
    )
    docker_config.setdefault("request_timeout_sec", 30)
    docker_config.setdefault("download_timeout_sec", 60)
    docker_config.setdefault("max_output_images", 8)
    docker_config.setdefault("max_download_bytes", 64 * 1024 * 1024)
    if args.metadata_mode:
        docker_config["metadata_mode"] = args.metadata_mode
    else:
        docker_config.setdefault("metadata_mode", "preserve")
    _write_json(config_dir / "gateway.docker.json", docker_config, mode=0o600)

    _copy_gateway_runtime(private_dir)
    gateway_url = f"http://127.0.0.1:{port}"
    _write_rules(session_dir, gateway_url)
    _write_mcp_configs(
        session_dir,
        private_dir,
        sys.executable,
        gateway_script,
        gateway_url,
        token,
    )
    _write_docker_assets(private_dir, token, port, DEFAULT_COMFY_IMAGE)
    switch = _write_switch(private_dir, backend)
    _write_launchers(root, sys.executable)
    _write_text(
        root / ".gitignore",
        "private/\nsession/in/*\n!session/in/.gitkeep\nsession/out/*\n!session/out/.gitkeep\n",
    )
    _harden_private(private_dir)
    _harden_private(config_dir)
    if os.name != "nt":
        for path in (config_dir / "gateway.json", config_dir / "gateway.docker.json", token_path):
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    warnings: list[str] = []
    if tier < 2:
        warnings.append(
            "L1 is workflow isolation, not a hard boundary. Codex/Qoder/Trae can still "
            "read outside the session with their own tools. Use L2 Docker or a VM when "
            "you need a real boundary."
        )
    if tier >= 2 and not scan_report["isolation_capabilities"]["container_runtime_ready"]:
        warnings.append(
            "Tier 2 assets were generated, but no running Docker/Podman daemon was detected. "
            "Install/start Docker before `isolationctl on`."
        )
    if not scan_report["backends"]["comfyui"]["running_on_localhost"]:
        warnings.append(
            "ComfyUI is not reachable on 127.0.0.1:8188. Start it with --disable-metadata "
            "before `isolationctl on` (or point comfy_url at your running instance)."
        )
    if workflow_warning:
        warnings.append(workflow_warning)
    if not workflow_ready:
        warnings.append(
            "The gateway will reject submissions until the example checkpoint is replaced."
        )
    if config.get("metadata_mode") == "strip":
        warnings.append(
            "metadata_mode=strip: archived images will lose prompt/workflow metadata and "
            "cannot be reproduced from the PNG. Keep the original ComfyUI output if you "
            "need reproducibility."
        )
    else:
        warnings.append(
            "Original image bytes (including ComfyUI prompt/workflow metadata) are preserved "
            "in the private archive by default. Keep that folder private; use "
            "strip_metadata.py on a copy before sharing."
        )
    if switch.get("backend") == "external":
        warnings.append(
            "External backend selected. Fill private/config/switch.json start/stop commands "
            "for your VM before enabling."
        )

    old_manifest = _read_json(root / "MANIFEST.json")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "name": NAME,
        "created_at": old_manifest.get("created_at") or _now(),
        "updated_at": _now(),
        "root": str(root),
        "tier": tier,
        "backend": switch.get("backend", backend),
        "agents": selected,
        "detected_agents": detected,
        "paths": {
            "session": str(session_dir),
            "private": str(private_dir),
            "config": str(config_dir / "gateway.json"),
            "docker_config": str(config_dir / "gateway.docker.json"),
            "workflow": str(workflow_path),
            "archive": str(archive_dir),
            "gateway_script": str(gateway_script),
            "isolationctl": str(SCRIPT_DIR / "isolationctl.py"),
            "compose": str(private_dir / "docker" / "docker-compose.yml"),
            "launchers": str(root / "launchers"),
        },
        "gateway": {
            "url": gateway_url,
            "port": port,
            "token_file": str(token_path),
            "server": gatewaymod.SERVER_NAME,
        },
        "workflow_ready": workflow_ready,
        "metadata_mode": config.get("metadata_mode", "preserve"),
        "warnings": warnings,
        "scan_summary": {
            "recommended_tier": scan_report["recommendation"]["recommended_tier"],
            "hard_boundary_available": scan_report["recommendation"]["hard_boundary_available"],
            "agents": detected,
            "comfyui_detected": scan_report["backends"]["comfyui"]["detected"],
            "comfyui_running": scan_report["backends"]["comfyui"]["running_on_localhost"],
        },
    }
    _write_json(root / "MANIFEST.json", manifest)
    return manifest


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(
        description="Bootstrap a local-management-enhancement isolated session."
    )
    parser.add_argument(
        "--root",
        default=str(Path.cwd() / "local-management-runtime"),
        help="Runtime root (default: ./local-management-runtime).",
    )
    parser.add_argument("--tier", choices=["auto", "1", "2"], default="auto")
    parser.add_argument(
        "--agents",
        default="auto",
        help="auto, all, or comma-separated: " + ", ".join(AGENT_IDS),
    )
    parser.add_argument("--comfy-url", help="ComfyUI base URL (default: http://127.0.0.1:8188).")
    parser.add_argument("--workflow", help="ComfyUI API-format workflow JSON.")
    parser.add_argument(
        "--metadata-mode",
        choices=["preserve", "strip"],
        help="preserve original PNG/JPEG metadata (default) or strip prompt metadata.",
    )
    parser.add_argument("--force", action="store_true", help="Allow a non-empty root without MANIFEST.json.")
    parser.add_argument("--reset-config", action="store_true", help="Rotate token and pick a new gateway port.")
    parser.add_argument("--json", action="store_true", help="Print the manifest as JSON.")
    args = parser.parse_args()
    try:
        manifest = bootstrap(args)
    except (SystemExit, ValueError, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    else:
        print(_manifest_summary(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

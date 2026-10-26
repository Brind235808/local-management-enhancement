#!/usr/bin/env python3
"""Scan the local machine for agent tools, isolation capabilities, and image backends.

The scanner is intentionally read-only. It never reads prompt archives or image
contents; it only checks for executables, config directories, and local ports.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
DEFAULT_PORTS = {"comfyui": 8188, "ollama": 11434, "lmstudio": 1234}


def _reconfigure_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _run(cmd: list[str], timeout: float = 5.0) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        out = completed.stdout or b""
        err = completed.stderr or b""
        return (
            int(completed.returncode),
            out.decode("utf-8", errors="replace").strip(),
            err.decode("utf-8", errors="replace").strip(),
        )
    except FileNotFoundError:
        return 127, "", "not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as exc:  # noqa: BLE001 - scanner must never crash on probes
        return 1, "", str(exc)


def _first_line(text: str, limit: int = 120) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line[:limit]


def _which(name: str) -> str | None:
    return shutil.which(name)


def _expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


def _path_exists(value: str) -> bool:
    try:
        return Path(_expand(value)).exists()
    except OSError:
        return False


def _unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.lower() if os.name == "nt" else item
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _decode_windows_output(data: bytes) -> str:
    for encoding in ("utf-8", "utf-16-le"):
        try:
            text = data.decode(encoding, errors="strict").replace("\x00", "")
            if text.strip():
                return text.strip()
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace").replace("\x00", "").strip()


def _wsl_installed() -> bool:
    if os.name != "nt" or not _which("wsl"):
        return False
    try:
        completed = subprocess.run(
            ["wsl.exe", "-l", "-q"],
            capture_output=True,
            timeout=6,
            check=False,
        )
        text = _decode_windows_output(completed.stdout or b"")
        if int(completed.returncode) != 0 or not text:
            return False
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return any(not re.search(r"install|没有|未安装", line, re.I) for line in lines)
    except Exception:
        return False


def _windows_process_paths(pattern: str) -> list[str]:
    if os.name != "nt" or not _which("powershell.exe"):
        return []
    script = (
        "Get-Process | Where-Object { $_.ProcessName -match '"
        + pattern.replace("'", "''")
        + "' } | Select-Object -ExpandProperty Path -Unique"
    )
    code, out, _ = _run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=8,
    )
    if code != 0 or not out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _http_json(url: str, timeout: float = 2.0) -> Any:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "local-management-enhancement-scan"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if int(response.status) != 200:
                return None
            return json.loads(response.read(2_000_000).decode("utf-8", errors="replace"))
    except Exception:
        return None


def _find_windows_shortcut_targets(names: list[str]) -> list[str]:
    if os.name != "nt" or not _which("powershell.exe"):
        return []
    start_menu = Path(
        _expand(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs")
    )
    if not start_menu.exists():
        return []
    candidates: list[Path] = []
    for name in names:
        try:
            candidates.extend(start_menu.rglob(f"{name}*.lnk"))
        except OSError:
            continue
    targets: list[str] = []
    for shortcut in candidates[:12]:
        script = (
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('"
            + str(shortcut).replace("'", "''")
            + "'); if ($s.TargetPath) { $s.TargetPath }"
        )
        code, out, _ = _run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=5,
        )
        if code == 0 and out:
            targets.append(out.splitlines()[0].strip())
    return _unique(targets)


def _find_drive_app(patterns: list[str]) -> list[str]:
    """Find app executables one or two levels below existing drive roots."""
    found: list[str] = []
    if os.name != "nt":
        return found
    for letter in "CDEFGH":
        root = Path(f"{letter}:/")
        if not root.exists():
            continue
        for pattern in patterns:
            try:
                found.extend(str(path) for path in root.glob(pattern))
            except OSError:
                continue
    return _unique(found)


def _find_comfyui_dirs() -> list[str]:
    candidates: list[str] = []
    for env_name in ("COMFYUI_PATH", "COMFYUI_HOME", "COMFYUI_DIR"):
        value = os.environ.get(env_name)
        if value:
            candidates.append(value)
    home = Path.home()
    candidates.extend(
        [
            str(home / "ComfyUI"),
            str(home / "Documents" / "ComfyUI"),
            str(home / "Desktop" / "ComfyUI"),
            str(home / "comfy" / "ComfyUI"),
            str(home / "AIGC" / "ComfyUI"),
            "/opt/ComfyUI",
            "/opt/comfyui",
            "/workspace/ComfyUI",
            r"C:\ComfyUI",
            r"D:\ComfyUI",
            r"E:\ComfyUI",
            r"F:\ComfyUI",
            _expand(r"%APPDATA%\ComfyUI"),
            _expand(r"%LOCALAPPDATA%\Programs\@comfyorgcomfyui-electron"),
            _expand(r"%LOCALAPPDATA%\ComfyUI"),
        ]
    )
    if os.name == "nt":
        for letter in "CDEFGH":
            root = Path(f"{letter}:/")
            if not root.exists():
                continue
            try:
                for path in root.glob("ComfyUI*"):
                    candidates.append(str(path))
            except OSError:
                continue
    valid: list[str] = []
    for value in candidates:
        path = Path(_expand(value))
        try:
            if path.is_dir() and (
                (path / "main.py").exists()
                or (path / "custom_nodes").is_dir()
                or (path / "ComfyUI" / "main.py").exists()
                or path.name.lower().startswith("comfyui")
            ):
                valid.append(str(path.resolve()))
        except OSError:
            continue
    return _unique(valid)


def _find_model_dirs(kind: str) -> list[str]:
    home = Path.home()
    candidates: list[str] = []
    if kind == "ollama":
        candidates = [
            str(home / ".ollama"),
            "/usr/share/ollama/.ollama",
            "/usr/local/var/ollama",
            _expand(r"%LOCALAPPDATA%\Programs\Ollama"),
            _expand(r"%APPDATA%\Ollama"),
        ]
    elif kind == "lmstudio":
        candidates = [
            str(home / ".lmstudio"),
            str(home / ".cache" / "lm-studio"),
            str(home / "Library" / "Application Support" / "LM Studio"),
            _expand(r"%LOCALAPPDATA%\LM-Studio"),
            _expand(r"%APPDATA%\LM Studio"),
        ]
    return _unique([c for c in candidates if _path_exists(c)])


def _detect_host() -> dict[str, Any]:
    host: dict[str, Any] = {
        "platform": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "is_windows": os.name == "nt",
        "shell": os.environ.get("SHELL") or os.environ.get("COMSPEC") or "",
        "home": str(Path.home()),
    }
    if os.name == "nt":
        try:
            import winreg  # type: ignore

            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
            )
            try:
                host["windows_product"] = str(winreg.QueryValueEx(key, "ProductName")[0])
                host["windows_edition"] = str(winreg.QueryValueEx(key, "EditionID")[0])
                host["windows_build"] = str(winreg.QueryValueEx(key, "CurrentBuildNumber")[0])
            finally:
                winreg.CloseKey(key)
        except Exception:
            pass
    node = _which("node")
    if node:
        host["node"] = _first_line(_run([node, "--version"], 3)[1])
    host["node_path"] = node or ""
    host["python_path"] = sys.executable
    host["git_path"] = _which("git") or ""
    return host


def _detect_isolation() -> dict[str, Any]:
    docker_path = _which("docker")
    docker: dict[str, Any] = {
        "installed": bool(docker_path),
        "path": docker_path or "",
        "daemon": False,
        "version": "",
        "compose": False,
    }
    if docker_path:
        code, out, _ = _run([docker_path, "--version"], 4)
        docker["version"] = _first_line(out) if code == 0 else ""
        code, out, _ = _run([docker_path, "info", "--format", "{{.ServerVersion}}"], 6)
        if code == 0 and out and "error" not in out.lower():
            docker["daemon"] = True
            docker["server_version"] = _first_line(out)
        code, out, _ = _run([docker_path, "compose", "version", "--short"], 4)
        docker["compose"] = bool(code == 0 and out)
    podman_path = _which("podman")
    podman: dict[str, Any] = {
        "installed": bool(podman_path),
        "path": podman_path or "",
        "daemon": False,
    }
    if podman_path:
        code, out, _ = _run([podman_path, "info", "--format", "json"], 6)
        podman["daemon"] = code == 0 and bool(out)
    windows_sandbox = os.name == "nt" and Path(
        r"C:\Windows\System32\WindowsSandbox.exe"
    ).exists()
    hyperv = os.name == "nt" and (
        Path(r"C:\Windows\System32\vmms.exe").exists()
        or Path(r"C:\Windows\System32\vmcompute.exe").exists()
    )
    sandbox_tools = {
        "linux": [tool for tool in ("bwrap", "firejail", "nsjail") if _which(tool)],
        "macos": [tool for tool in ("sandbox-exec", "limactl") if _which(tool)],
    }
    wsl = _wsl_installed()
    return {
        "docker": docker,
        "podman": podman,
        "wsl": {"installed": wsl, "path": _which("wsl.exe") or _which("wsl") or ""},
        "windows_sandbox": {"available": bool(windows_sandbox)},
        "hyperv": {"available": bool(hyperv)},
        "sandbox_tools": sandbox_tools,
        "container_runtime_ready": bool(docker["daemon"] or podman["daemon"]),
        "hard_boundary_possible": bool(
            docker["daemon"]
            or podman["daemon"]
            or wsl
            or windows_sandbox
            or hyperv
            or sandbox_tools["linux"]
            or sandbox_tools["macos"]
        ),
    }


AGENT_DEFINITIONS: list[dict[str, Any]] = [
    {
        "id": "codex",
        "name": "Codex CLI",
        "kind": "cli",
        "binaries": ["codex"],
        "dirs": ["~/.codex"],
        "app_patterns": [],
        "process_pattern": "codex",
        "rules": ["AGENTS.md", ".codex/config.toml"],
        "notes": "Shell-capable; workspace-write can still read files outside the workspace.",
    },
    {
        "id": "qoder",
        "name": "Qoder IDE / CLI",
        "kind": "ide",
        "binaries": ["qodercli", "qoder"],
        "dirs": ["~/.qoder-cn", "~/.qoder"],
        "app_patterns": [
            "*/.qoder-versions/*/Qoder CN.exe",
            "*/.qoder-versions/*/Qoder.exe",
            "Qoder/Qoder.exe",
            "Qoder CN/Qoder CN.exe",
        ],
        "process_pattern": "Qoder",
        "rules": ["AGENTS.md", "QODER.md", ".qoder/rules/*.md"],
        "notes": "GUI app; hard isolation needs VM or a separate OS user, not just rules.",
    },
    {
        "id": "trae",
        "name": "Trae IDE",
        "kind": "ide",
        "binaries": ["trae"],
        "dirs": ["~/.trae", "~/.trae-cn", "%APPDATA%\\Trae", "%APPDATA%\\Trae CN"],
        "app_patterns": [
            "Trae/Trae.exe",
            "Trae CN/Trae CN.exe",
            "Programs/Trae/Trae.exe",
        ],
        "process_pattern": "Trae",
        "rules": ["AGENTS.md", ".trae/rules/project_rules.md", ".trae/rules/*.md"],
        "notes": "GUI app; project rules and .trae/mcp.json are project-scoped.",
    },
    {
        "id": "cursor",
        "name": "Cursor",
        "kind": "ide",
        "binaries": ["cursor"],
        "dirs": ["~/.cursor", "%APPDATA%\\Cursor"],
        "app_patterns": ["Programs/cursor/Cursor.exe", "Cursor/Cursor.exe"],
        "process_pattern": "Cursor",
        "rules": [".cursor/rules/*.mdc", ".cursorrules"],
        "notes": "Rules are project-scoped; .cursor/mcp.json is supported.",
    },
    {
        "id": "claude",
        "name": "Claude Code",
        "kind": "cli",
        "binaries": ["claude"],
        "dirs": ["~/.claude"],
        "app_patterns": [],
        "process_pattern": "claude",
        "rules": ["CLAUDE.md", ".mcp.json"],
        "notes": "CLI agent with shell; use a container or OS sandbox for a hard boundary.",
    },
    {
        "id": "gemini",
        "name": "Gemini CLI",
        "kind": "cli",
        "binaries": ["gemini"],
        "dirs": ["~/.gemini"],
        "app_patterns": [],
        "process_pattern": "gemini",
        "rules": ["GEMINI.md", ".gemini/settings.json"],
        "notes": "CLI agent with shell; use a container or OS sandbox for a hard boundary.",
    },
    {
        "id": "windsurf",
        "name": "Windsurf",
        "kind": "ide",
        "binaries": ["windsurf"],
        "dirs": ["~/.windsurf", "~/.codeium/windsurf"],
        "app_patterns": ["Programs/Windsurf/Windsurf.exe", "Windsurf/Windsurf.exe"],
        "process_pattern": "Windsurf",
        "rules": [".windsurf/rules/*.md", ".windsurfrules"],
        "notes": "MCP configuration is typically global; rely on the gateway being OFF when not isolating.",
    },
    {
        "id": "cline",
        "name": "Cline",
        "kind": "ide-extension",
        "binaries": [],
        "dirs": ["~/.cline", "%APPDATA%\\Code\\User\\globalStorage\\saoudrizwan.claude-dev"],
        "app_patterns": [],
        "process_pattern": "",
        "rules": [".clinerules"],
        "notes": "VS Code extension; MCP configuration is global to the editor profile.",
    },
    {
        "id": "copilot",
        "name": "GitHub Copilot",
        "kind": "ide-extension",
        "binaries": [],
        "dirs": ["~/.copilot"],
        "app_patterns": [],
        "process_pattern": "",
        "rules": [".github/copilot-instructions.md"],
        "notes": "Rules are project-scoped; MCP support depends on the host editor.",
    },
]


def _detect_agents() -> list[dict[str, Any]]:
    running: dict[str, list[str]] = {}
    if os.name == "nt":
        for agent in AGENT_DEFINITIONS:
            pattern = agent.get("process_pattern") or ""
            if pattern:
                running[agent["id"]] = _windows_process_paths(pattern)
    results: list[dict[str, Any]] = []
    for agent in AGENT_DEFINITIONS:
        evidence: list[str] = []
        cli_paths: list[str] = []
        for binary in agent["binaries"]:
            found = _which(binary)
            if found:
                cli_paths.append(found)
                evidence.append(f"PATH:{found}")
        for directory in agent["dirs"]:
            if _path_exists(directory):
                evidence.append(f"DIR:{_expand(directory)}")
        if agent["id"] == "qoder":
            evidence.extend(f"PROC:{p}" for p in running.get("qoder", []))
        elif agent["id"] == "trae":
            evidence.extend(f"PROC:{p}" for p in running.get("trae", []))
        elif agent["id"] == "cursor":
            evidence.extend(f"PROC:{p}" for p in running.get("cursor", []))
        else:
            evidence.extend(f"PROC:{p}" for p in running.get(agent["id"], []))
        if os.name == "nt" and agent["kind"] == "ide":
            for target in _find_windows_shortcut_targets([agent["name"], agent["id"]]):
                evidence.append(f"LNK:{target}")
            for exe in _find_drive_app(agent["app_patterns"]):
                evidence.append(f"APP:{exe}")
        evidence = _unique(evidence)
        version = ""
        for cli in cli_paths[:1]:
            code, out, _ = _run([cli, "--version"], 3)
            if code == 0 and out:
                version = _first_line(out)
        detected = bool(evidence)
        if detected:
            results.append(
                {
                    "id": agent["id"],
                    "name": agent["name"],
                    "kind": agent["kind"],
                    "detected": True,
                    "version": version,
                    "cli_paths": cli_paths,
                    "rules_files": list(agent["rules"]),
                    "evidence": evidence[:12],
                    "shell_capable": True,
                    "hard_boundary_required": True,
                    "notes": agent["notes"],
                }
            )
        else:
            results.append(
                {
                    "id": agent["id"],
                    "name": agent["name"],
                    "kind": agent["kind"],
                    "detected": False,
                    "version": "",
                    "cli_paths": [],
                    "rules_files": list(agent["rules"]),
                    "evidence": [],
                    "shell_capable": True,
                    "hard_boundary_required": True,
                    "notes": agent["notes"],
                }
            )
    return results


def _detect_backends() -> dict[str, Any]:
    comfy_dirs = _find_comfyui_dirs()
    comfy_running = _port_open(DEFAULT_PORTS["comfyui"])
    comfy_info: dict[str, Any] = {
        "detected": bool(comfy_dirs or comfy_running),
        "paths": comfy_dirs,
        "port": DEFAULT_PORTS["comfyui"],
        "running_on_localhost": comfy_running,
        "api_reachable": False,
        "metadata_flag_advice": (
            "Keep ComfyUI metadata by default so images can be reproduced. "
            "--disable-metadata is optional and removes reproducibility."
        ),
    }
    if comfy_running:
        stats = _http_json(f"http://127.0.0.1:{DEFAULT_PORTS['comfyui']}/system_stats", 2.5)
        comfy_info["api_reachable"] = isinstance(stats, dict)
        if isinstance(stats, dict):
            devices = stats.get("devices")
            if isinstance(devices, list):
                comfy_info["device_count"] = len(devices)
    ollama_running = _port_open(DEFAULT_PORTS["ollama"])
    ollama_info: dict[str, Any] = {
        "detected": bool(_which("ollama") or _find_model_dirs("ollama") or ollama_running),
        "path": _which("ollama") or "",
        "data_dirs": _find_model_dirs("ollama"),
        "port": DEFAULT_PORTS["ollama"],
        "running_on_localhost": ollama_running,
        "model_count": None,
    }
    if ollama_running:
        tags = _http_json(f"http://127.0.0.1:{DEFAULT_PORTS['ollama']}/api/tags", 2.5)
        if isinstance(tags, dict) and isinstance(tags.get("models"), list):
            ollama_info["model_count"] = len(tags["models"])
    lmstudio_running = _port_open(DEFAULT_PORTS["lmstudio"])
    lmstudio_info: dict[str, Any] = {
        "detected": bool(_find_model_dirs("lmstudio") or lmstudio_running),
        "data_dirs": _find_model_dirs("lmstudio"),
        "port": DEFAULT_PORTS["lmstudio"],
        "running_on_localhost": lmstudio_running,
        "model_count": None,
    }
    if lmstudio_running:
        models = _http_json(f"http://127.0.0.1:{DEFAULT_PORTS['lmstudio']}/v1/models", 2.5)
        if isinstance(models, dict) and isinstance(models.get("data"), list):
            lmstudio_info["model_count"] = len(models["data"])
    return {"comfyui": comfy_info, "ollama": ollama_info, "lmstudio": lmstudio_info}


def _recommend(
    host: dict[str, Any],
    isolation: dict[str, Any],
    agents: list[dict[str, Any]],
    backends: dict[str, Any],
) -> dict[str, Any]:
    detected_agents = [a for a in agents if a["detected"]]
    runtime_ready = bool(isolation["container_runtime_ready"])
    tier = 2 if runtime_ready else 1
    reasons: list[str] = []
    warnings: list[str] = []
    next_steps: list[str] = []

    if runtime_ready:
        reasons.append(
            "A container runtime is available; Tier 2 (agent container + gateway sidecar) "
            "is the recommended hard boundary."
        )
    else:
        reasons.append(
            "No running container runtime was detected; start with Tier 1 (host gateway + "
            "dedicated session workspace). Tier 1 is workflow isolation, not a hard boundary."
        )
        next_steps.append(
            "Install Docker Desktop with WSL2, or use a VM, when you need to stop a shell-capable "
            "agent from reading outside the workspace."
        )
    if isolation["wsl"]["installed"] and not runtime_ready:
        reasons.append("WSL is installed but Docker is not running; Docker Desktop can use it for Tier 2.")
    if isolation["windows_sandbox"]["available"] or isolation["hyperv"]["available"]:
        next_steps.append(
            "Windows Sandbox/Hyper-V is available. Use VM-based isolation for Qoder/Trae if you "
            "need a hard boundary for GUI agents."
        )
    ide_agents = [a for a in detected_agents if a["kind"] in ("ide", "ide-extension")]
    cli_agents = [a for a in detected_agents if a["kind"] == "cli"]
    if ide_agents:
        warnings.append(
            "Detected GUI agents ("
            + ", ".join(a["name"] for a in ide_agents)
            + "). Rules and project MCP configs do not stop the app from reading other folders; "
            "use a VM or a separate OS user for a real boundary."
        )
    if cli_agents:
        warnings.append(
            "Detected shell-capable CLI agents ("
            + ", ".join(a["name"] for a in cli_agents)
            + "). Codex workspace-write can still read outside the workspace; use the Docker "
            "agent box for a hard boundary."
        )
    comfy = backends["comfyui"]
    if not comfy["detected"]:
        next_steps.append(
            "ComfyUI was not detected. Install it or set COMFYUI_PATH before bootstrapping; "
            "keep metadata enabled for reproducibility and bind it to 127.0.0.1."
        )
    elif not comfy["running_on_localhost"]:
        next_steps.append(
            "ComfyUI files were found but port 8188 is closed. Start ComfyUI with "
            "--listen 127.0.0.1 --port 8188 before enabling the gateway."
        )
    if comfy["detected"] and not comfy["running_on_localhost"]:
        reasons.append("ComfyUI is installed but not running; the gateway health check will fail until it starts.")
    if not (backends["ollama"]["detected"] or backends["lmstudio"]["detected"]):
        next_steps.append(
            "No local LLM backend was detected. Cloud DeepSeek/other APIs work normally; only "
            "add prompt redaction if the prompt leaves the machine."
        )
    if detected_agents:
        reasons.append(
            "Detected agents: " + ", ".join(a["name"] for a in detected_agents) + "."
        )
    else:
        reasons.append("No known agent was detected; the skill can still generate generic rules and MCP configs.")
    if not detected_agents:
        next_steps.append("Install or open your agent once, re-run the scan, then bootstrap the session.")
    warnings.append(
        "Folder nesting and prompt rewriting are not isolation. The enforced boundary is the "
        "agent's OS/container/VM permissions plus a single narrow gateway tool."
    )

    return {
        "recommended_tier": tier,
        "hard_boundary_available": bool(isolation["hard_boundary_possible"]),
        "container_runtime_ready": runtime_ready,
        "reasons": _unique(reasons),
        "warnings": _unique(warnings),
        "next_steps": _unique(next_steps),
        "detected_agents": [a["id"] for a in detected_agents],
    }


def scan() -> dict[str, Any]:
    host = _detect_host()
    isolation = _detect_isolation()
    agents = _detect_agents()
    backends = _detect_backends()
    recommendation = _recommend(host, isolation, agents, backends)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": host,
        "isolation_capabilities": isolation,
        "agents": agents,
        "backends": backends,
        "recommendation": recommendation,
    }


def _fmt_bool(value: bool) -> str:
    return "yes" if value else "no"


def to_markdown(report: dict[str, Any]) -> str:
    host = report["host"]
    iso = report["isolation_capabilities"]
    backends = report["backends"]
    rec = report["recommendation"]
    lines: list[str] = []
    lines.append("# Local management capability scan")
    lines.append("")
    lines.append(f"Generated: {report['generated_at']}")
    lines.append("")
    lines.append("## Host")
    lines.append("")
    lines.append(f"- Platform: {host['platform']} {host['release']} ({host['machine']})")
    if host.get("windows_product"):
        lines.append(f"- Windows: {host['windows_product']} ({host.get('windows_edition', '')}, build {host.get('windows_build', '')})")
    lines.append(f"- Python: {host['python']} ({host['python_path']})")
    if host.get("node"):
        lines.append(f"- Node: {host['node']}")
    lines.append(f"- Home: {host['home']}")
    lines.append("")
    lines.append("## Isolation capabilities")
    lines.append("")
    docker = iso["docker"]
    lines.append(
        f"- Docker: installed={_fmt_bool(docker['installed'])}, daemon={_fmt_bool(docker['daemon'])}, compose={_fmt_bool(docker['compose'])}"
    )
    lines.append(f"- Podman: installed={_fmt_bool(iso['podman']['installed'])}, daemon={_fmt_bool(iso['podman']['daemon'])}")
    lines.append(f"- WSL: {_fmt_bool(iso['wsl']['installed'])}")
    lines.append(f"- Windows Sandbox: {_fmt_bool(iso['windows_sandbox']['available'])}")
    lines.append(f"- Hyper-V: {_fmt_bool(iso['hyperv']['available'])}")
    tools = iso["sandbox_tools"]["linux"] + iso["sandbox_tools"]["macos"]
    lines.append(f"- OS sandbox tools: {', '.join(tools) if tools else 'none detected'}")
    lines.append("")
    lines.append("## Agents")
    lines.append("")
    lines.append("| Agent | Detected | Version | Rules | Evidence |")
    lines.append("|---|---|---|---|---|")
    for agent in report["agents"]:
        if not agent["detected"]:
            continue
        evidence = "<br>".join(item.replace("|", "\\|") for item in agent["evidence"][:4])
        lines.append(
            f"| {agent['name']} | yes | {agent['version'] or '-'} | {', '.join(agent['rules_files'])} | {evidence or '-'} |"
        )
    if not rec["detected_agents"]:
        lines.append("| - | none | - | - | - |")
    lines.append("")
    lines.append("## Image / LLM backends")
    lines.append("")
    comfy = backends["comfyui"]
    lines.append(
        f"- ComfyUI: detected={_fmt_bool(comfy['detected'])}, localhost:{comfy['port']}={_fmt_bool(comfy['running_on_localhost'])}, api={_fmt_bool(comfy['api_reachable'])}"
    )
    if comfy["paths"]:
        lines.append(f"  - paths: {', '.join(comfy['paths'])}")
    ollama = backends["ollama"]
    lines.append(
        f"- Ollama: detected={_fmt_bool(ollama['detected'])}, localhost:{ollama['port']}={_fmt_bool(ollama['running_on_localhost'])}, models={ollama['model_count'] if ollama['model_count'] is not None else 'unknown'}"
    )
    lmstudio = backends["lmstudio"]
    lines.append(
        f"- LM Studio: detected={_fmt_bool(lmstudio['detected'])}, localhost:{lmstudio['port']}={_fmt_bool(lmstudio['running_on_localhost'])}"
    )
    lines.append("")
    lines.append("## Recommendation")
    lines.append("")
    lines.append(f"- Recommended tier: **L{rec['recommended_tier']}**")
    lines.append(f"- Hard boundary possible: {_fmt_bool(rec['hard_boundary_available'])}")
    lines.append(f"- Detected agents: {', '.join(rec['detected_agents']) or 'none'}")
    lines.append("")
    lines.append("Reasons:")
    for item in rec["reasons"]:
        lines.append(f"- {item}")
    if rec["warnings"]:
        lines.append("")
        lines.append("Warnings:")
        for item in rec["warnings"]:
            lines.append(f"- {item}")
    if rec["next_steps"]:
        lines.append("")
        lines.append("Next steps:")
        for item in rec["next_steps"]:
            lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    _reconfigure_stdout()
    parser = argparse.ArgumentParser(description="Scan local agent and isolation capabilities.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of Markdown.")
    parser.add_argument("--report", metavar="PATH", help="Write a Markdown report to PATH.")
    parser.add_argument("--json-out", metavar="PATH", help="Write the raw JSON report to PATH.")
    args = parser.parse_args()

    started = time.time()
    report = scan()
    report["scan_duration_ms"] = int((time.time() - started) * 1000)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    markdown = to_markdown(report)

    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload + "\n", encoding="utf-8")

    if args.json:
        print(payload)
    else:
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

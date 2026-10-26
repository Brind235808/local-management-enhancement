#!/usr/bin/env python3
"""Turn the isolated session on/off without touching normal agent usage."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import image_gateway as gatewaymod  # noqa: E402
import scan_environment as scanmod  # noqa: E402

NAME = "local-management-enhancement"
STATE_VERSION = 1
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ControlError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, payload: Any, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if mode is not None and os.name != "nt":
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def _resolve_root(value: str | None) -> Path:
    root = (
        value
        or os.environ.get("LOCAL_MANAGEMENT_ROOT")
        or str(Path.cwd() / "local-management-runtime")
    )
    return Path(root).expanduser().resolve()


def _load_manifest(root: Path) -> dict[str, Any]:
    manifest = _read_json(root / "MANIFEST.json")
    if not manifest or manifest.get("name") != NAME:
        raise ControlError(
            f"No {NAME} runtime found at {root}. Run bootstrap_isolation.py first "
            "or pass --root."
        )
    return manifest


def _token(manifest: dict[str, Any]) -> str:
    token_path = Path(manifest["gateway"]["token_file"])
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ControlError(f"Cannot read gateway token: {exc}") from exc
    if not token:
        raise ControlError("Gateway token is empty. Re-run bootstrap --reset-config.")
    return token


def _state_path(root: Path) -> Path:
    return root / "private" / "state.json"


def _read_state(root: Path) -> dict[str, Any]:
    state = _read_json(_state_path(root))
    if not state:
        return {"version": STATE_VERSION, "enabled": False}
    return state


def _write_state(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    state = dict(state)
    state["version"] = STATE_VERSION
    state["updated_at"] = _now()
    _write_json(_state_path(root), state, mode=0o600)
    return state


def _health(url: str, timeout: float = 2.5) -> dict[str, Any] | None:
    try:
        request = urllib.request.Request(
            url.rstrip("/") + "/health",
            headers={"User-Agent": NAME},
            method="GET",
        )
        with _OPENER.open(request, timeout=timeout) as response:
            if int(response.status) != 200:
                return None
            data = json.loads(response.read(512 * 1024).decode("utf-8", errors="replace"))
            return data if isinstance(data, dict) else None
    except Exception:
        return None


def _health_matches(health: dict[str, Any] | None, manifest: dict[str, Any]) -> bool:
    return bool(health and health.get("server") == manifest["gateway"]["server"])


def _tail(path: Path, lines: int = 25) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:])
    except OSError:
        return "(no gateway log)"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pid_command(pid: int) -> str:
    if os.name == "nt":
        script = (
            f"(Get-CimInstance Win32_Process -Filter \"ProcessId = {int(pid)}\")"
            ".CommandLine"
        )
        code, out, _ = scanmod._run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=6,
        )
        return out if code == 0 else ""
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            timeout=4,
            check=False,
        )
        return completed.stdout.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _pid_is_gateway(pid: int, gateway_script: str) -> bool:
    command = _pid_command(pid)
    return bool(command) and Path(gateway_script).name in command


def _kill_pid(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + 5
    while time.time() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _wait_for_ready(
    url: str,
    timeout: float,
    process: subprocess.Popen[Any] | None = None,
    require_comfy: bool = True,
) -> dict[str, Any] | None:
    deadline = time.time() + timeout
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        health = _health(url)
        if health:
            last = health
            if health.get("workflow_ready") and (
                health.get("comfy_reachable") or not require_comfy
            ):
                return health
        if process is not None and process.poll() is not None:
            return last
        time.sleep(0.4)
    return last


def _compose(manifest: dict[str, Any], *args: str) -> list[str]:
    docker = shutil.which("docker")
    if not docker:
        raise ControlError("docker was not found on PATH.")
    compose = Path(manifest["paths"]["compose"])
    env_file = compose.parent / ".env"
    command = [
        docker,
        "compose",
        "--env-file",
        str(env_file),
        "-f",
        str(compose),
        *args,
    ]
    return command


def _docker_ready() -> None:
    docker = shutil.which("docker")
    if not docker:
        raise ControlError("docker was not found on PATH.")
    code, out, err = scanmod._run(
        [docker, "info", "--format", "{{.ServerVersion}}"],
        timeout=10,
    )
    if code != 0:
        detail = (err or out or "daemon unavailable").splitlines()[0]
        raise ControlError(f"Docker daemon is not available: {detail}")


def _run_compose(manifest: dict[str, Any], args: list[str], timeout: float = 900) -> None:
    command = _compose(manifest, *args)
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "").strip()
        raise ControlError(
            "docker compose failed: " + (message[-1200:] if message else "unknown error")
        )


def _external_commands(manifest: dict[str, Any], key: str) -> list[Any]:
    switch = _read_json(Path(manifest["paths"]["private"]) / "config" / "switch.json")
    external = switch.get("external")
    if not isinstance(external, dict):
        return []
    commands = external.get(key)
    return commands if isinstance(commands, list) else []


def _run_external(commands: list[Any]) -> None:
    for command in commands:
        if isinstance(command, str):
            completed = subprocess.run(command, shell=True, check=False)
        elif isinstance(command, list) and all(isinstance(item, str) for item in command):
            completed = subprocess.run(command, check=False)
        else:
            raise ControlError("External start/stop commands must be strings or string arrays.")
        if completed.returncode != 0:
            raise ControlError(
                f"External command failed with exit code {completed.returncode}: {command}"
            )


def _preflight(manifest: dict[str, Any], allow_placeholder: bool) -> None:
    if not manifest.get("workflow_ready") and not allow_placeholder:
        raise ControlError(
            "The ComfyUI API workflow is still the example or invalid. Replace "
            f"{manifest['paths']['workflow']} with your exported API-format workflow, "
            "or pass --allow-placeholder-workflow."
        )
    try:
        gatewaymod.load_config(manifest["paths"]["config"])
    except gatewaymod.GatewayError as exc:
        raise ControlError(f"Gateway config is invalid: {exc}") from exc


def _enable_host(
    manifest: dict[str, Any],
    allow_missing_comfy: bool,
    allow_placeholder: bool,
) -> dict[str, Any]:
    root = Path(manifest["root"])
    url = manifest["gateway"]["url"]
    token = _token(manifest)
    _preflight(manifest, allow_placeholder)

    health = _health(url)
    if _health_matches(health, manifest):
        state = _read_state(root)
        if state.get("enabled") and state.get("backend") in ("docker", "external"):
            raise ControlError(
                f"Backend '{state.get('backend')}' is active on this port. Run "
                "`isolationctl off` before switching backends."
            )
        if not health.get("comfy_reachable") and not allow_missing_comfy:
            raise ControlError(
                "A gateway is already running, but ComfyUI is unreachable. Start ComfyUI "
                "or pass --allow-missing-comfy."
            )
        state = _write_state(
            root,
            {
                "enabled": True,
                "backend": "host",
                "pid": health.get("pid"),
                "url": url,
                "health_url": url,
                "tier": manifest.get("tier"),
                "started_at": _now(),
                "with_agent": False,
            },
        )
        return state

    port = int(manifest["gateway"]["port"])
    # Refuse to silently share a port held by another process.
    if _health(url) is None:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                raise ControlError(
                    f"Port {port} is already in use by another process. Stop it or re-bootstrap "
                    "with --reset-config to choose a new port."
                )

    log_path = Path(manifest["paths"]["private"]) / "logs" / "gateway.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(log_path, "ab", buffering=0)  # noqa: SIM115 - inherited by child
    env = os.environ.copy()
    env["IMAGE_GATEWAY_TOKEN"] = token
    command = [
        sys.executable,
        manifest["paths"]["gateway_script"],
        "http",
        "--config",
        manifest["paths"]["config"],
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    process = subprocess.Popen(  # noqa: S603 - fixed local command
        command,
        cwd=str(Path(manifest["paths"]["private"])),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )
    log_handle.close()
    health = _wait_for_ready(
        url,
        timeout=30,
        process=process,
        require_comfy=not allow_missing_comfy,
    )
    if process.poll() is not None or not _health_matches(health, manifest):
        _kill_pid(process.pid)
        raise ControlError(
            "Gateway failed to start.\n" + _tail(log_path)
        )
    if not health.get("comfy_reachable") and not allow_missing_comfy:
        _kill_pid(process.pid)
        raise ControlError(
            "ComfyUI is unreachable. Start ComfyUI with --disable-metadata, or pass "
            "--allow-missing-comfy if you intentionally want the gateway up without it."
        )
    if not health.get("workflow_ready") and not allow_placeholder:
        _kill_pid(process.pid)
        raise ControlError("The gateway workflow is invalid. Fix the workflow and retry.")
    return _write_state(
        root,
        {
            "enabled": True,
            "backend": "host",
            "pid": health.get("pid") or process.pid,
            "url": url,
            "health_url": url,
            "tier": manifest.get("tier"),
            "started_at": _now(),
            "with_agent": False,
        },
    )


def _enable_docker(
    manifest: dict[str, Any],
    allow_missing_comfy: bool,
    allow_placeholder: bool,
    with_agent: bool,
) -> dict[str, Any]:
    root = Path(manifest["root"])
    url = manifest["gateway"]["url"]
    _preflight(manifest, allow_placeholder)
    _docker_ready()
    current = _health(url)
    state = _read_state(root)
    if _health_matches(current, manifest) and state.get("enabled") and state.get("backend") == "host":
        raise ControlError(
            "A host gateway is active on this port. Run `isolationctl off` before starting Docker."
        )
    compose = Path(manifest["paths"]["compose"])
    if not compose.exists():
        raise ControlError(f"Compose file not found: {compose}")
    args = ["up", "-d", "--build"]
    if with_agent:
        args.extend(["--profile", "agent"])
    try:
        _run_compose(manifest, args, timeout=1200)
    except (ControlError, subprocess.TimeoutExpired) as exc:
        try:
            _run_compose(manifest, ["down"], timeout=120)
        except Exception:
            pass
        raise ControlError(str(exc)) from exc
    health = _wait_for_ready(
        url,
        timeout=180,
        process=None,
        require_comfy=not allow_missing_comfy,
    )
    if not _health_matches(health, manifest):
        logs = ""
        try:
            completed = subprocess.run(
                _compose(manifest, "logs", "--tail", "40", "gateway"),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            logs = (completed.stdout or completed.stderr or "").strip()
        except Exception:
            pass
        try:
            _run_compose(manifest, ["down"], timeout=120)
        except Exception:
            pass
        raise ControlError("Docker gateway did not become healthy.\n" + logs[-2000:])
    if not health.get("comfy_reachable") and not allow_missing_comfy:
        try:
            _run_compose(manifest, ["down"], timeout=120)
        except Exception:
            pass
        raise ControlError(
            "Docker gateway is up, but ComfyUI is unreachable inside the stack. "
            "Check the comfyui service, or pass --allow-missing-comfy."
        )
    return _write_state(
        root,
        {
            "enabled": True,
            "backend": "docker",
            "pid": None,
            "url": url,
            "health_url": url,
            "tier": manifest.get("tier"),
            "started_at": _now(),
            "with_agent": with_agent,
        },
    )


def _enable_external(
    manifest: dict[str, Any],
    allow_missing_comfy: bool,
    allow_placeholder: bool,
) -> dict[str, Any]:
    root = Path(manifest["root"])
    _preflight(manifest, allow_placeholder)
    current = _health(manifest["gateway"]["url"])
    state = _read_state(root)
    if _health_matches(current, manifest) and state.get("enabled") and state.get("backend") == "host":
        raise ControlError(
            "A host gateway is active on this port. Run `isolationctl off` before starting the external backend."
        )
    start_commands = _external_commands(manifest, "start")
    if not start_commands:
        raise ControlError(
            "External backend selected, but private/config/switch.json has no "
            "external.start commands."
        )
    switch = _read_json(Path(manifest["paths"]["private"]) / "config" / "switch.json")
    external = switch.get("external") if isinstance(switch.get("external"), dict) else {}
    health_url = str(external.get("health_url") or manifest["gateway"]["url"])
    _run_external(start_commands)
    health = _wait_for_ready(
        health_url,
        timeout=180,
        process=None,
        require_comfy=not allow_missing_comfy,
    )
    if not _health_matches(health, manifest):
        try:
            _run_external(_external_commands(manifest, "stop"))
        except Exception:
            pass
        raise ControlError("External gateway did not become healthy.")
    if not health.get("comfy_reachable") and not allow_missing_comfy:
        try:
            _run_external(_external_commands(manifest, "stop"))
        except Exception:
            pass
        raise ControlError("External gateway is up, but ComfyUI is unreachable.")
    return _write_state(
        root,
        {
            "enabled": True,
            "backend": "external",
            "pid": None,
            "url": health_url,
            "health_url": health_url,
            "tier": manifest.get("tier"),
            "started_at": _now(),
            "with_agent": False,
        },
    )


def enable(
    root: Path,
    backend: str | None,
    allow_missing_comfy: bool,
    allow_placeholder: bool,
    with_agent: bool,
) -> dict[str, Any]:
    manifest = _load_manifest(root)
    selected = backend or str(manifest.get("backend") or "host")
    if selected == "host":
        return _enable_host(manifest, allow_missing_comfy, allow_placeholder)
    if selected == "docker":
        return _enable_docker(
            manifest, allow_missing_comfy, allow_placeholder, with_agent
        )
    if selected == "external":
        return _enable_external(manifest, allow_missing_comfy, allow_placeholder)
    raise ControlError(f"Unknown backend: {selected}")


def disable(root: Path) -> dict[str, Any]:
    manifest = _load_manifest(root)
    state = _read_state(root)
    backend = str(state.get("backend") or manifest.get("backend") or "host")
    health = _health(str(state.get("health_url") or manifest["gateway"]["url"]))
    if backend == "host":
        pid = int(state.get("pid") or 0)
        if _health_matches(health, manifest) and health.get("pid"):
            pid = int(health["pid"])
        if not pid and _health_matches(health, manifest):
            pid = int(health.get("pid") or 0)
        if pid and _pid_is_gateway(pid, manifest["paths"]["gateway_script"]):
            _kill_pid(pid)
        elif pid and _health_matches(health, manifest):
            # The health endpoint is authoritative even if command-line probing fails.
            _kill_pid(pid)
        elif not state.get("enabled") and _health_matches(health, manifest):
            orphan_pid = int(health.get("pid") or 0)
            if orphan_pid and _pid_is_gateway(orphan_pid, manifest["paths"]["gateway_script"]):
                _kill_pid(orphan_pid)
    elif backend == "docker":
        if shutil.which("docker"):
            try:
                _run_compose(manifest, ["down"], timeout=180)
            except Exception as exc:
                print(f"warning: docker compose down failed: {exc}", file=sys.stderr)
        else:
            print("warning: docker not found; containers may still be running.", file=sys.stderr)
    elif backend == "external":
        try:
            _run_external(_external_commands(manifest, "stop"))
        except Exception as exc:
            print(f"warning: external stop failed: {exc}", file=sys.stderr)
    return _write_state(
        root,
        {
            "enabled": False,
            "backend": backend,
            "pid": None,
            "url": state.get("url") or manifest["gateway"]["url"],
            "health_url": state.get("health_url") or manifest["gateway"]["url"],
            "tier": manifest.get("tier"),
            "disabled_at": _now(),
            "with_agent": False,
        },
    )


def status(root: Path) -> dict[str, Any]:
    manifest = _load_manifest(root)
    state = _read_state(root)
    url = str(state.get("health_url") or manifest["gateway"]["url"])
    health = _health(url)
    healthy = _health_matches(health, manifest)
    warnings = list(manifest.get("warnings", []))
    if healthy and not bool(state.get("enabled")):
        warnings.append(
            "An orphan gateway is running while the switch state is OFF. Run "
            "`isolationctl off` to stop it."
        )
    return {
        "name": NAME,
        "root": str(root),
        "enabled": bool(state.get("enabled")) and healthy,
        "state_enabled": bool(state.get("enabled")),
        "healthy": healthy,
        "backend": state.get("backend") or manifest.get("backend"),
        "tier": manifest.get("tier"),
        "gateway": manifest["gateway"]["url"],
        "health_url": url,
        "comfy_reachable": bool(health.get("comfy_reachable")) if health else False,
        "workflow_ready": bool(manifest.get("workflow_ready")),
        "gateway_workflow_ready": bool(health.get("workflow_ready")) if health else False,
        "session": manifest["paths"]["session"],
        "private": manifest["paths"]["private"],
        "agents": manifest.get("agents", []),
        "warnings": warnings,
        "state": state,
    }


def _format_command(command: list[str], token: str) -> str:
    masked = [item.replace(token, "***") if token else item for item in command]
    if os.name == "nt":
        return subprocess.list2cmdline(masked)
    return shlex.join(masked)


def _codex_command() -> list[str]:
    path = shutil.which("codex")
    if not path:
        raise ControlError("codex was not found on PATH.")
    if os.name == "nt" and path.lower().endswith((".cmd", ".bat")):
        js = Path(path).parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        node = shutil.which("node")
        if js.exists() and node:
            return [node, str(js)]
    return [path]


def _toml_quote(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_quote(value) for value in values) + "]"


GUI_PATTERNS = {
    "qoder": {
        "env": "QODER_EXE",
        "patterns": [
            "*/*/.qoder-versions/*/Qoder CN.exe",
            "*/*/.qoder-versions/*/Qoder.exe",
            "*/.qoder-versions/*/Qoder CN.exe",
            "*/.qoder-versions/*/Qoder.exe",
            "Qoder/Qoder.exe",
            "Qoder CN/Qoder CN.exe",
        ],
        "process": "Qoder",
        "shortcuts": ["Qoder", "Qoder CN"],
    },
    "trae": {
        "env": "TRAE_EXE",
        "patterns": [
            "*/*/Trae CN.exe",
            "*/*/Trae.exe",
            "Trae/Trae.exe",
            "Trae CN/Trae CN.exe",
            "Trae/Trae CN.exe",
        ],
        "process": "Trae",
        "shortcuts": ["Trae", "Trae CN"],
    },
    "cursor": {
        "env": "CURSOR_EXE",
        "patterns": ["*/*/Cursor.exe", "Programs/cursor/Cursor.exe"],
        "process": "Cursor",
        "shortcuts": ["Cursor"],
    },
}


def _find_gui_exe(agent: str) -> str | None:
    config = GUI_PATTERNS[agent]
    env_value = os.environ.get(config["env"])
    if env_value and Path(env_value).exists():
        return env_value
    if os.name == "nt":
        for path in scanmod._windows_process_paths(config["process"]):
            if path.lower().endswith(".exe") and Path(path).exists():
                return path
        for path in scanmod._find_drive_app(config["patterns"]):
            if Path(path).exists():
                return path
        for path in scanmod._find_windows_shortcut_targets(config["shortcuts"]):
            if Path(path).exists():
                return path
    candidates = {
        "qoder": ["qoder", "qodercli"],
        "trae": ["trae"],
        "cursor": ["cursor"],
    }.get(agent, [])
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    if sys.platform == "darwin":
        names = {"qoder": ["Qoder"], "trae": ["Trae"], "cursor": ["Cursor"]}
        for name in names.get(agent, []):
            path = Path("/Applications") / f"{name}.app"
            if path.exists():
                return str(path)
    return None


def _gui_command(agent: str, exe: str, session: Path) -> list[str]:
    if agent == "qoder" and Path(exe).name.lower().startswith("qodercli"):
        return [exe, "-w", str(session)]
    return [exe, str(session)]


def _launch_gui(agent: str, session: Path, env: dict[str, str], dry_run: bool) -> int:
    exe = _find_gui_exe(agent)
    if not exe:
        raise ControlError(
            f"Could not locate {agent}. Open this folder manually in the app:\n  {session}"
        )
    command = _gui_command(agent, exe, session)
    if dry_run:
        print(_format_command(command, ""))
        return 0
    kwargs: dict[str, Any] = {"cwd": str(session), "env": env, "close_fds": True}
    if os.name != "nt":
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(command, **kwargs)
    except OSError as exc:
        raise ControlError(
            f"Failed to launch {agent}: {exc}\nOpen this folder manually:\n  {session}"
        ) from exc
    print(f"Opened {agent} with isolated workspace: {session}")
    print("If the app did not open a window, open that folder manually.")
    return 0


def launch(root: Path, agent: str, dry_run: bool, as_json: bool) -> int:
    current = status(root)
    if not current["enabled"]:
        raise ControlError(
            "Isolation is OFF or the gateway is unhealthy. Run `isolationctl on` first."
        )
    manifest = _load_manifest(root)
    session = Path(manifest["paths"]["session"])
    token = _token(manifest)
    env = os.environ.copy()
    env["IMAGE_GATEWAY_TOKEN"] = token
    env["IMAGE_GATEWAY_URL"] = manifest["gateway"]["url"]

    if agent == "codex":
        command = _codex_command() + [
            "-C",
            str(session),
            "-s",
            "workspace-write",
            "-a",
            "on-request",
            "-c",
            "mcp_servers.image_gateway.command=" + _toml_quote(sys.executable),
            "-c",
            "mcp_servers.image_gateway.args="
            + _toml_array(
                [
                    manifest["paths"]["gateway_script"],
                    "proxy",
                    "--url",
                    manifest["gateway"]["url"],
                ]
            ),
            "-c",
            "mcp_servers.image_gateway.env={ IMAGE_GATEWAY_TOKEN = "
            + _toml_quote(token)
            + " }",
        ]
        if dry_run:
            output = {"agent": agent, "command": _format_command(command, token)}
            print(json.dumps(output, ensure_ascii=False) if as_json else output["command"])
            return 0
        try:
            completed = subprocess.run(  # noqa: S603 - fixed local command
                command,
                cwd=str(session),
                env=env,
                check=False,
            )
        except OSError as exc:
            raise ControlError(f"Failed to launch Codex: {exc}") from exc
        return int(completed.returncode)

    if agent in ("qoder", "trae", "cursor"):
        if dry_run and as_json:
            exe = _find_gui_exe(agent)
            print(
                json.dumps(
                    {
                        "agent": agent,
                        "command": _format_command(
                            _gui_command(agent, exe or f"<{agent} executable>", session), ""
                        ),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        return _launch_gui(agent, session, env, dry_run)

    if agent in ("claude", "gemini"):
        executable = shutil.which(agent)
        if not executable:
            raise ControlError(f"{agent} was not found on PATH.")
        command = [executable]
        if dry_run:
            output = {"agent": agent, "command": _format_command(command, token)}
            print(json.dumps(output, ensure_ascii=False) if as_json else output["command"])
            return 0
        try:
            completed = subprocess.run(  # noqa: S603 - fixed local command
                command,
                cwd=str(session),
                env=env,
                check=False,
            )
        except OSError as exc:
            raise ControlError(f"Failed to launch {agent}: {exc}") from exc
        return int(completed.returncode)

    raise ControlError(f"Unsupported agent: {agent}")


def _print_status(current: dict[str, Any]) -> None:
    state = "ON" if current["enabled"] else ("ON (unhealthy)" if current["state_enabled"] else "OFF")
    print(f"{NAME}: {state}")
    print(f"Root:      {current['root']}")
    print(f"Backend:   {current['backend']} (tier L{current['tier']})")
    print(f"Gateway:   {current['gateway']}")
    print(f"ComfyUI:   {'reachable' if current['comfy_reachable'] else 'unreachable'}")
    print(
        "Workflow:  "
        + ("ready" if current["workflow_ready"] else "NOT READY (replace the example workflow)")
    )
    print(f"Session:   {current['session']}")
    print(f"Private:   {current['private']}")
    print(f"Agents:    {', '.join(current['agents']) or '-'}")
    if current.get("warnings"):
        print("Warnings:")
        for warning in current["warnings"]:
            print(f"- {warning}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Toggle the local isolated session.")
    parser.add_argument("--root", help="Runtime root created by bootstrap_isolation.py.")
    sub = parser.add_subparsers(dest="command", required=True)

    enable_parser = sub.add_parser("enable", aliases=["on"], help="Start the gateway/stack.")
    enable_parser.add_argument("--backend", choices=["host", "docker", "external"])
    enable_parser.add_argument("--allow-missing-comfy", action="store_true")
    enable_parser.add_argument("--allow-placeholder-workflow", action="store_true")
    enable_parser.add_argument("--with-agent", action="store_true", help="Docker only: start the agent box too.")
    enable_parser.add_argument("--json", action="store_true")

    disable_parser = sub.add_parser("disable", aliases=["off"], help="Stop the gateway/stack.")
    disable_parser.add_argument("--json", action="store_true")

    status_parser = sub.add_parser("status", help="Show on/off and health.")
    status_parser.add_argument("--json", action="store_true")

    launch_parser = sub.add_parser("launch", help="Launch an agent in the isolated session.")
    launch_parser.add_argument("agent", choices=["codex", "qoder", "trae", "cursor", "claude", "gemini"])
    launch_parser.add_argument("--dry-run", action="store_true", help="Print the command only.")
    launch_parser.add_argument("--json", action="store_true")

    toggle_parser = sub.add_parser("toggle", help="Flip the switch.")
    toggle_parser.add_argument("--json", action="store_true")

    args = parser.parse_args()
    command = {"on": "enable", "off": "disable"}.get(args.command, args.command)
    root = _resolve_root(args.root)
    try:
        if command == "enable":
            state = enable(
                root,
                args.backend,
                args.allow_missing_comfy,
                args.allow_placeholder_workflow,
                args.with_agent,
            )
            if args.json:
                print(json.dumps(status(root), ensure_ascii=False, indent=2))
            else:
                _print_status(status(root))
                print("\nIsolation ON. Normal agent usage is unchanged; use an isolated launcher.")
            return 0
        if command == "disable":
            state = disable(root)
            if args.json:
                print(json.dumps({**status(root), "state": state}, ensure_ascii=False, indent=2))
            else:
                print(f"{NAME}: OFF")
                print("Gateway/stack stopped. Archives and configuration were preserved.")
                print("Normal agent usage is unchanged.")
            return 0
        if command == "status":
            current = status(root)
            if args.json:
                print(json.dumps(current, ensure_ascii=False, indent=2))
            else:
                _print_status(current)
            return 0 if not current["state_enabled"] or current["enabled"] else 1
        if command == "toggle":
            current = status(root)
            if current["enabled"] or current["state_enabled"]:
                state = disable(root)
                if args.json:
                    print(json.dumps({**status(root), "state": state}, ensure_ascii=False, indent=2))
                else:
                    print(f"{NAME}: OFF")
                return 0
            state = enable(root, None, False, False, False)
            if args.json:
                print(json.dumps(status(root), ensure_ascii=False, indent=2))
            else:
                _print_status(status(root))
            return 0
        if command == "launch":
            return launch(root, args.agent, args.dry_run, args.json)
    except (ControlError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

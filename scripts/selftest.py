#!/usr/bin/env python3
"""End-to-end self-test with a stub ComfyUI server.

No real ComfyUI, GPU, Docker, or network access is required. The test starts a
local stub, bootstraps a temporary runtime, toggles isolation on, exercises the
HTTP and MCP transports, verifies metadata stripping, and checks fail-closed
behavior after toggling off.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS  {name}")
        return
    print(f"FAIL  {name}" + (f" :: {detail}" if detail else ""))
    _FAILURES.append(name)


def require(name: str, condition: bool, detail: str = "") -> None:
    check(name, condition, detail)
    if not condition:
        raise RuntimeError(name + (f": {detail}" if detail else ""))


def run(
    command: list[str],
    timeout: float = 120,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged["PYTHONIOENCODING"] = "utf-8"
    if env:
        merged.update(env)
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=merged,
        cwd=str(cwd) if cwd else None,
        check=False,
    )


def run_json(command: list[str], timeout: float = 120) -> tuple[subprocess.CompletedProcess[str], Any]:
    completed = run(command, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"STDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        )
    try:
        return completed, json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"expected JSON from {' '.join(command)}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        ) from exc


def http_json(
    url: str, payload: dict[str, Any], token: str | None = None, timeout: float = 15
) -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json", "User-Agent": "selftest"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return int(response.status), json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return int(exc.code), json.loads(body)
        except json.JSONDecodeError:
            return int(exc.code), {"raw": body}


def http_get_json(url: str, timeout: float = 10) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url, headers={"User-Agent": "selftest"}, method="GET")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return int(response.status), json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return int(exc.code), {}
    except (urllib.error.URLError, OSError):
        return 0, {}


def png_with_text() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00")
    return (
        signature
        + chunk(b"IHDR", ihdr)
        + chunk(b"tEXt", b"prompt\x00secret prompt text")
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


class StubState:
    def __init__(self) -> None:
        self.history: dict[str, dict[str, Any]] = {}
        self.payloads: dict[str, dict[str, Any]] = {}
        self.image = png_with_text()
        self.lock = threading.Lock()

    def submit(self, body: dict[str, Any]) -> str:
        prompt_id = "test-" + uuid.uuid4().hex[:12]
        workflow = body.get("prompt")
        with self.lock:
            self.payloads[prompt_id] = body
            self.history[prompt_id] = {
                "status": {
                    "status_str": "success",
                    "completed": True,
                    "messages": [],
                },
                "outputs": {
                    "9": {
                        "images": [
                            {
                                "filename": "stub.png",
                                "subfolder": "",
                                "type": "output",
                            }
                        ]
                    }
                },
            }
        return prompt_id


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/prompt":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        state: StubState = self.server.state  # type: ignore[attr-defined]
        prompt_id = state.submit(body)
        self._json(200, {"prompt_id": prompt_id, "number": 1, "node_errors": {}})

    def do_GET(self) -> None:  # noqa: N802
        state: StubState = self.server.state  # type: ignore[attr-defined]
        if self.path == "/system_stats":
            self._json(200, {"system": {}, "devices": []})
            return
        if self.path == "/queue":
            self._json(200, {"queue_running": [], "queue_pending": []})
            return
        if self.path.startswith("/history/"):
            prompt_id = self.path.split("/history/", 1)[1]
            entry = state.history.get(prompt_id)
            self._json(200, {prompt_id: entry} if entry else {})
            return
        if self.path.startswith("/view"):
            data = state.image
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._json(404, {"error": "not found"})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


class MCPClient:
    def __init__(self, command: list[str], env: dict[str, str]) -> None:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        merged = os.environ.copy()
        merged.update(env)
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=merged,
            creationflags=creationflags,
        )
        self.messages: list[dict[str, Any]] = []
        self.condition = threading.Condition()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                message = {"_raw": line}
            with self.condition:
                self.messages.append(message)
                self.condition.notify_all()

    def send(self, payload: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 15) -> dict[str, Any]:
        message_id = uuid.uuid4().hex[:8]
        self.send(
            {
                "jsonrpc": "2.0",
                "id": message_id,
                "method": method,
                "params": params or {},
            }
        )
        deadline = time.time() + timeout
        with self.condition:
            while time.time() < deadline:
                for index, message in enumerate(self.messages):
                    if message.get("id") == message_id:
                        self.messages.pop(index)
                        return message
                self.condition.wait(timeout=min(0.2, max(0.01, deadline - time.time())))
        raise RuntimeError(f"MCP request timed out: {method}")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self) -> None:
        try:
            if self.process.stdin:
                self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)


def tool_text(response: dict[str, Any]) -> tuple[bool, dict[str, Any] | str]:
    result = response.get("result")
    if not isinstance(result, dict):
        return False, str(response)
    is_error = bool(result.get("isError"))
    content = result.get("content")
    if not isinstance(content, list) or not content:
        return is_error, ""
    text = content[0].get("text", "") if isinstance(content[0], dict) else ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = text
    return is_error, parsed


def main() -> int:
    parser = argparse.ArgumentParser(description="End-to-end self-test.")
    parser.add_argument("--keep", action="store_true", help="Keep the temporary runtime.")
    args = parser.parse_args()

    temp_dir = Path(tempfile.mkdtemp(prefix="lme-selftest-"))
    root = temp_dir / "runtime"
    stub = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    stub.state = StubState()  # type: ignore[attr-defined]
    stub.daemon_threads = True
    stub_thread = threading.Thread(target=stub.serve_forever, daemon=True)
    stub_thread.start()
    stub_port = int(stub.server_address[1])
    comfy_url = f"http://127.0.0.1:{stub_port}"
    gateway_url: str | None = None
    enabled = False

    try:
        print(f"Stub ComfyUI: {comfy_url}")
        print("== scan ==")
        _, scan_report = run_json([PYTHON, str(SCRIPT_DIR / "scan_environment.py"), "--json"], timeout=180)
        require("scan: JSON schema", scan_report.get("schema_version") == 1)
        require("scan: host detected", bool(scan_report.get("host", {}).get("platform")))
        require("scan: agents list", isinstance(scan_report.get("agents"), list))

        print("== privacy scripts ==")
        import prompt_redact
        import strip_metadata

        png_path = temp_dir / "meta.png"
        png_path.write_bytes(png_with_text())
        result = strip_metadata.strip_file(png_path)
        require("metadata: tEXt detected and stripped", result["changed"] and "tEXt" in result["removed"])
        require("metadata: tEXt removed", b"tEXt" not in png_path.read_bytes())
        result = strip_metadata.strip_file(png_path)
        require("metadata: second pass clean", not result["changed"])

        map_path = temp_dir / "redaction.json"
        token = prompt_redact.add_term(map_path, "SecretProjectX")
        redacted = prompt_redact.redact("project SecretProjectX at night", prompt_redact.load_map(map_path))
        require("redaction: token replaced", "SecretProjectX" not in redacted and token in redacted)
        require(
            "redaction: round-trip",
            prompt_redact.restore(redacted, prompt_redact.load_map(map_path))
            == "project SecretProjectX at night",
        )

        print("== bootstrap ==")
        workflow = json.loads((SCRIPT_DIR.parent / "assets" / "comfy_api_workflow.example.json").read_text(encoding="utf-8"))
        workflow["4"]["inputs"]["ckpt_name"] = "selftest-model.safetensors"
        workflow_path = temp_dir / "workflow.json"
        workflow_path.write_text(json.dumps(workflow, indent=2), encoding="utf-8")
        _, manifest = run_json(
            [
                PYTHON,
                str(SCRIPT_DIR / "bootstrap_isolation.py"),
                "--root",
                str(root),
                "--tier",
                "1",
                "--agents",
                "codex,qoder,trae",
                "--comfy-url",
                comfy_url,
                "--workflow",
                str(workflow_path),
                "--json",
            ],
            timeout=180,
        )
        require("bootstrap: workflow ready", manifest.get("workflow_ready") is True)
        require("bootstrap: metadata preserved by default", manifest.get("metadata_mode") == "preserve")
        session = Path(manifest["paths"]["session"])
        private = Path(manifest["paths"]["private"])
        require("bootstrap: session exists", session.is_dir())
        require("bootstrap: private exists", private.is_dir())
        require("bootstrap: output outside session", not str(Path(manifest["paths"]["archive"])).startswith(str(session)))
        require("bootstrap: AGENTS rules", (session / "AGENTS.md").exists())
        require("bootstrap: Qoder rules", (session / ".qoder" / "rules" / "isolation.md").exists())
        require("bootstrap: Trae rules", (session / ".trae" / "rules" / "project_rules.md").exists())
        require("bootstrap: Cursor rules", (session / ".cursor" / "rules" / "isolation.mdc").exists())
        require("bootstrap: MCP config", (session / ".mcp.json").exists())
        require("bootstrap: Codex config", (session / ".codex" / "config.toml").exists())
        require("bootstrap: compose", Path(manifest["paths"]["compose"]).exists())
        compose_text = Path(manifest["paths"]["compose"]).read_text(encoding="utf-8")
        comfy_args = next(line for line in compose_text.splitlines() if "COMFYUI_ARGS=" in line)
        require("compose: metadata kept by default", "--disable-metadata" not in comfy_args, comfy_args)
        require("compose: internal networks", "internal: true" in compose_text)
        require("compose: no private mount in agent", "archive" not in compose_text.split("agent:", 1)[1].split("networks:", 1)[0])

        print("== switch on ==")
        gateway_url = manifest["gateway"]["url"]
        _, status_off = run_json([PYTHON, str(SCRIPT_DIR / "isolationctl.py"), "--root", str(root), "status", "--json"])
        require("switch: starts OFF", status_off.get("enabled") is False)
        _, status_on = run_json([PYTHON, str(SCRIPT_DIR / "isolationctl.py"), "--root", str(root), "on", "--json"])
        enabled = True
        require("switch: ON", status_on.get("enabled") is True)
        require("switch: comfy reachable", status_on.get("comfy_reachable") is True)

        token_value = Path(manifest["gateway"]["token_file"]).read_text(encoding="utf-8").strip()
        require("switch: token exists", bool(token_value))

        print("== HTTP gateway ==")
        code, submitted = http_json(
            gateway_url + "/submit",
            {
                "prompt": "a red fox",
                "negative_prompt": "blurry",
                "width": 512,
                "height": 512,
                "steps": 20,
                "cfg": 7.5,
                "seed": 42,
            },
            token_value,
        )
        require("http: submit ok", code == 200 and submitted.get("ok") is True, str(submitted))
        job_id = submitted.get("job_id")
        require("http: job id", isinstance(job_id, str) and job_id)

        status_result: dict[str, Any] = {}
        for _ in range(50):
            code, status_result = http_json(gateway_url + "/status", {"job_id": job_id}, token_value)
            if status_result.get("status") in ("completed", "failed"):
                break
            time.sleep(0.15)
        require("http: completed", status_result.get("status") == "completed", str(status_result))
        require("http: image count", status_result.get("image_count") == 1, str(status_result))
        require("http: metadata mode preserve", status_result.get("metadata_mode") == "preserve", str(status_result))

        archive = Path(manifest["paths"]["archive"]) / "outputs"
        saved_files = list(archive.rglob("*.png"))
        require("http: archived image exists", len(saved_files) == 1, str(saved_files))
        require(
            "http: original metadata preserved",
            saved_files[0].read_bytes() == stub.state.image,  # type: ignore[attr-defined]
            "saved bytes differ from the original ComfyUI response",
        )

        stored = stub.state.payloads.get(job_id, {})  # type: ignore[attr-defined]
        filled = stored.get("prompt", {})
        require("http: prompt substituted", filled.get("6", {}).get("inputs", {}).get("text") == "a red fox", str(filled.get("6")))
        require("http: negative substituted", filled.get("7", {}).get("inputs", {}).get("text") == "blurry")
        require("http: size substituted", filled.get("5", {}).get("inputs", {}).get("width") == 512)
        require("http: seed substituted", filled.get("3", {}).get("inputs", {}).get("seed") == 42)

        code, rejected = http_json(
            gateway_url + "/submit",
            {"prompt": "x", "image_path": "C:/secret.png"},
            token_value,
        )
        require("http: path argument rejected", code == 400 and "not accepted" in str(rejected.get("error", "")).lower(), str(rejected))
        code, rejected = http_json(gateway_url + "/status", {"job_id": "../../etc/passwd"}, token_value)
        require("http: invalid job id rejected", code == 400, str(rejected))

        print("== MCP proxy ==")
        proxy = MCPClient(
            [PYTHON, str(SCRIPT_DIR / "image_gateway.py"), "proxy", "--url", gateway_url],
            {"IMAGE_GATEWAY_TOKEN": token_value},
        )
        try:
            init = proxy.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
            require("mcp: initialize", isinstance(init.get("result"), dict))
            proxy.notify("notifications/initialized")
            tools_response = proxy.request("tools/list")
            tools = tools_response.get("result", {}).get("tools", [])
            names = {tool.get("name") for tool in tools}
            require("mcp: exactly two tools", names == {"submit_image_generation", "get_generation_status"}, str(names))
            submit_schema = next(tool for tool in tools if tool["name"] == "submit_image_generation")["inputSchema"]
            properties = set(submit_schema.get("properties", {}))
            require("mcp: no path argument", "image_path" not in properties and "file" not in properties, str(properties))
            require("mcp: strict schema", submit_schema.get("additionalProperties") is False)

            response = proxy.request(
                "tools/call",
                {
                    "name": "submit_image_generation",
                    "arguments": {
                        "prompt": "mcp fox",
                        "width": 64,
                        "height": 64,
                        "steps": 4,
                        "seed": 7,
                    },
                },
            )
            is_error, payload = tool_text(response)
            require("mcp: submit ok", not is_error and isinstance(payload, dict) and payload.get("job_id"), str(payload))
            mcp_job = payload["job_id"] if isinstance(payload, dict) else ""
            mcp_status: dict[str, Any] = {}
            for _ in range(50):
                response = proxy.request(
                    "tools/call",
                    {"name": "get_generation_status", "arguments": {"job_id": mcp_job}},
                )
                is_error, payload = tool_text(response)
                if isinstance(payload, dict):
                    mcp_status = payload
                    if payload.get("status") in ("completed", "failed"):
                        break
                time.sleep(0.15)
            require("mcp: completed", mcp_status.get("status") == "completed", str(mcp_status))
            response = proxy.request(
                "tools/call",
                {"name": "submit_image_generation", "arguments": {"prompt": "x", "file": "C:/secret"}},
            )
            is_error, payload = tool_text(response)
            require("mcp: extra path arg is an error", is_error, str(payload))
        finally:
            proxy.close()

        print("== launchers (dry-run) ==")
        _, launch = run_json(
            [PYTHON, str(SCRIPT_DIR / "isolationctl.py"), "--root", str(root), "launch", "codex", "--dry-run", "--json"]
        )
        command = launch.get("command", "")
        require("codex: workspace-write", "workspace-write" in command, command)
        require("codex: gateway MCP", "image_gateway" in command and "proxy" in command, command)
        require("codex: token masked", token_value not in command, command)
        _, launch = run_json(
            [PYTHON, str(SCRIPT_DIR / "isolationctl.py"), "--root", str(root), "launch", "qoder", "--dry-run", "--json"]
        )
        require("qoder: launch includes session", str(session) in launch.get("command", ""), str(launch))
        _, launch = run_json(
            [PYTHON, str(SCRIPT_DIR / "isolationctl.py"), "--root", str(root), "launch", "trae", "--dry-run", "--json"]
        )
        require("trae: launch includes session", str(session) in launch.get("command", ""), str(launch))

        print("== config guard ==")
        config = json.loads(Path(manifest["paths"]["config"]).read_text(encoding="utf-8"))
        bad_config = dict(config)
        bad_config["output_dir"] = str(session / "out")
        bad_config_path = temp_dir / "bad-config.json"
        bad_config_path.write_text(json.dumps(bad_config), encoding="utf-8")
        bad = run(
            [PYTHON, str(SCRIPT_DIR / "image_gateway.py"), "check-config", "--config", str(bad_config_path)]
        )
        require(
            "config: output inside workspace rejected",
            bad.returncode != 0 and "outside workspace_root" in (bad.stdout + bad.stderr),
            bad.stdout + bad.stderr,
        )

        print("== switch off / fail closed ==")
        _, status_off = run_json([PYTHON, str(SCRIPT_DIR / "isolationctl.py"), "--root", str(root), "off", "--json"])
        enabled = False
        require("switch: OFF", status_off.get("state", {}).get("enabled") is False, str(status_off))
        code, health = http_get_json(gateway_url + "/health")
        require("switch: health gone", code != 200 or not health.get("ok"), str(health))

        proxy = MCPClient(
            [PYTHON, str(SCRIPT_DIR / "image_gateway.py"), "proxy", "--url", gateway_url],
            {"IMAGE_GATEWAY_TOKEN": token_value},
        )
        try:
            proxy.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
            response = proxy.request(
                "tools/call",
                {"name": "submit_image_generation", "arguments": {"prompt": "must fail"}},
            )
            is_error, payload = tool_text(response)
            require("off: gateway fails closed", is_error and "OFF or unreachable" in str(payload), str(payload))
        finally:
            proxy.close()

        print("== status ==")
        _, status = run_json([PYTHON, str(SCRIPT_DIR / "isolationctl.py"), "--root", str(root), "status", "--json"])
        require("status: reports OFF", status.get("enabled") is False)
        require("status: session path", status.get("session") == str(session))

        print()
        if _FAILURES:
            print(f"SELFTEST FAILED ({len(_FAILURES)} failures): " + ", ".join(_FAILURES))
            return 1
        print("SELFTEST PASSED")
        return 0
    finally:
        if enabled:
            try:
                run([PYTHON, str(SCRIPT_DIR / "isolationctl.py"), "--root", str(root), "off"], timeout=60)
            except Exception:
                pass
        stub.shutdown()
        stub.server_close()
        if args.keep:
            print(f"Kept runtime: {root}")
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

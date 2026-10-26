#!/usr/bin/env python3
"""Narrow image-generation gateway for isolated agent sessions.

The only agent-facing capabilities are:
  * submit_image_generation(prompt, negative_prompt, width, height, steps, cfg, seed)
  * get_generation_status(job_id)

There is deliberately no read_file, list_files, glob, shell, or arbitrary path
parameter. ComfyUI is reached by the gateway, not by the agent.

Transports:
  stdio   MCP over stdio, direct ComfyUI access (advanced/local testing)
  http    HTTP JSON API used by the session switch (localhost or sidecar)
  proxy   MCP over stdio that forwards to the running HTTP gateway
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import random
import re
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from strip_metadata import MetadataError, detect_image_format, strip_image_bytes

SERVER_NAME = "local-management-enhancement-image-gateway"
SERVER_VERSION = "1.0.0"
DEFAULT_HTTP_PORT = 18765
PROXY_OFF_MESSAGE = (
    "Isolation gateway is OFF or unreachable. Run `isolationctl on` "
    "(local-management-enhancement skill), then retry."
)
MAX_BODY_BYTES = 64 * 1024
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
PLACEHOLDER_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
KNOWN_PLACEHOLDERS = {
    "PROMPT",
    "NEGATIVE_PROMPT",
    "WIDTH",
    "HEIGHT",
    "STEPS",
    "CFG",
    "SEED",
    "BATCH_SIZE",
}
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class GatewayError(Exception):
    http_status = 400


class ConfigError(GatewayError):
    http_status = 500


class BackendError(GatewayError):
    http_status = 502


TOOLS: list[dict[str, Any]] = [
    {
        "name": "submit_image_generation",
        "description": (
            "Queue an image generation from text. Accepts prompt parameters only; "
            "it does not accept, read, or resolve local file paths."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "prompt": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 6000,
                    "description": "Positive text prompt. No file paths.",
                },
                "negative_prompt": {
                    "type": "string",
                    "maxLength": 3000,
                    "default": "",
                    "description": "Negative text prompt.",
                },
                "width": {
                    "type": "integer",
                    "minimum": 64,
                    "maximum": 2048,
                    "default": 512,
                },
                "height": {
                    "type": "integer",
                    "minimum": 64,
                    "maximum": 2048,
                    "default": 512,
                },
                "steps": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 150,
                    "default": 20,
                },
                "cfg": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 30,
                    "default": 7.0,
                },
                "seed": {
                    "type": "integer",
                    "minimum": -1,
                    "maximum": 2147483647,
                    "default": -1,
                    "description": "-1 selects a random seed.",
                },
            },
            "required": ["prompt"],
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "get_generation_status",
        "description": (
            "Return the status of a previously submitted job. On completion, the "
            "original image bytes are stored in the gateway's private archive by default; "
            "set metadata_mode to strip only when you need a sanitized copy."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "job_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": "The job_id returned by submit_image_generation.",
                }
            },
            "required": ["job_id"],
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
]


def _resolve_path(base: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or _is_within(first, second) or _is_within(second, first)


def _find_placeholders(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for value in node.values():
            found.update(_find_placeholders(value))
    elif isinstance(node, list):
        for value in node:
            found.update(_find_placeholders(value))
    elif isinstance(node, str):
        found.update(PLACEHOLDER_RE.findall(node))
    return found


def _fill(node: Any, values: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        return {key: _fill(value, values) for key, value in node.items()}
    if isinstance(node, list):
        return [_fill(value, values) for value in node]
    if isinstance(node, str):
        exact = PLACEHOLDER_RE.fullmatch(node)
        if exact and exact.group(1) in values:
            replacement = values[exact.group(1)]
            if isinstance(replacement, (int, float)) and not isinstance(replacement, bool):
                return replacement
            return str(replacement)
        result = node
        for name, value in values.items():
            result = result.replace("{{" + name + "}}", str(value))
        return result
    return node


def _read_workflow(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(
            "Workflow file not found. Export an API-format workflow from ComfyUI and "
            "set workflow_path in the gateway config."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot parse workflow JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("Workflow must be a ComfyUI API-format JSON object.")
    placeholders = _find_placeholders(data)
    unknown = placeholders - KNOWN_PLACEHOLDERS
    if unknown:
        raise ConfigError(
            "Workflow contains unknown placeholders: " + ", ".join(sorted(unknown))
        )
    if "PROMPT" not in placeholders:
        raise ConfigError(
            "Workflow must contain the {{PROMPT}} placeholder in at least one input."
        )
    return data


def load_config(path: str | Path, create_output: bool = True) -> dict[str, Any]:
    config_path = Path(path).resolve()
    if not config_path.exists():
        raise ConfigError(f"Gateway config not found: {config_path}")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot parse gateway config: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("Gateway config must be a JSON object.")
    config_dir = config_path.parent

    comfy_url = str(raw.get("comfy_url", "http://127.0.0.1:8188")).rstrip("/")
    parsed = urllib.parse.urlparse(comfy_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError("comfy_url must be an http(s) URL.")
    if parsed.username or parsed.password:
        raise ConfigError("comfy_url must not contain credentials.")

    workflow_path = _resolve_path(config_dir, str(raw.get("workflow_path", "")))
    if not str(raw.get("workflow_path", "")).strip():
        raise ConfigError("workflow_path is required.")
    workflow = _read_workflow(workflow_path)

    output_value = str(raw.get("output_dir", "")).strip()
    if not output_value:
        raise ConfigError("output_dir is required.")
    output_dir = _resolve_path(config_dir, output_value)
    if create_output:
        output_dir.mkdir(parents=True, exist_ok=True)
    if not output_dir.is_dir():
        raise ConfigError(f"output_dir is not a directory: {output_dir}")

    workspace_value = str(raw.get("workspace_root", "") or "").strip()
    workspace_root: Path | None = None
    if workspace_value:
        workspace_root = _resolve_path(config_dir, workspace_value)
        if _paths_overlap(output_dir, workspace_root):
            raise ConfigError(
                "output_dir must be outside workspace_root. The agent workspace must "
                "never contain the private image archive."
            )
        if _paths_overlap(workflow_path, workspace_root):
            raise ConfigError("workflow_path must be outside workspace_root.")

    token = os.environ.get("IMAGE_GATEWAY_TOKEN") or str(raw.get("token", "") or "")
    metadata_mode = str(raw.get("metadata_mode", "preserve") or "preserve").strip().lower()
    if metadata_mode not in ("preserve", "strip"):
        raise ConfigError("metadata_mode must be 'preserve' or 'strip'.")
    try:
        request_timeout = float(raw.get("request_timeout_sec", 30))
        download_timeout = float(raw.get("download_timeout_sec", 60))
        max_output_images = int(raw.get("max_output_images", 8))
        max_download_bytes = int(raw.get("max_download_bytes", 64 * 1024 * 1024))
        http_port = int(raw.get("http_port", DEFAULT_HTTP_PORT))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid numeric setting in gateway config: {exc}") from exc
    if not 1 <= request_timeout <= 300:
        raise ConfigError("request_timeout_sec must be between 1 and 300.")
    if not 1 <= download_timeout <= 300:
        raise ConfigError("download_timeout_sec must be between 1 and 300.")
    if not 1 <= max_output_images <= 16:
        raise ConfigError("max_output_images must be between 1 and 16.")
    if not 1024 * 1024 <= max_download_bytes <= 256 * 1024 * 1024:
        raise ConfigError("max_download_bytes must be between 1 MiB and 256 MiB.")
    if not 1 <= http_port <= 65535:
        raise ConfigError("http_port must be a valid TCP port.")

    return {
        "config_path": config_path,
        "comfy_url": comfy_url,
        "workflow_path": workflow_path,
        "workflow": workflow,
        "output_dir": output_dir,
        "workspace_root": workspace_root,
        "token": token,
        "request_timeout_sec": request_timeout,
        "download_timeout_sec": download_timeout,
        "max_output_images": max_output_images,
        "max_download_bytes": max_download_bytes,
        "http_host": str(raw.get("http_host", "127.0.0.1")),
        "http_port": http_port,
        "metadata_mode": metadata_mode,
    }


def _safe_comfy_error(code: int, body: bytes) -> str:
    message = ""
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or error.get("type") or "")
            elif error:
                message = str(error)
            node_errors = data.get("node_errors")
            if not message and isinstance(node_errors, dict):
                for node in node_errors.values():
                    if not isinstance(node, dict):
                        continue
                    errors = node.get("errors")
                    if isinstance(errors, list) and errors:
                        first = errors[0]
                        if isinstance(first, dict):
                            message = str(first.get("message") or first.get("details") or "")
                        else:
                            message = str(first)
                        if message:
                            break
    except Exception:
        message = ""
    message = re.sub(r"\s+", " ", message).strip()
    if len(message) > 300:
        message = message[:297] + "..."
    if not message:
        message = "ComfyUI rejected the request."
    return f"ComfyUI HTTP {code}: {message}"


class ComfyClient:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
        raw: bool = False,
    ) -> Any:
        data = None
        headers = {"User-Agent": SERVER_NAME}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with _OPENER.open(request, timeout=timeout or self.timeout) as response:
                if raw:
                    payload_bytes = response.read(1024 * 1024 * 4)
                    return payload_bytes
                body = response.read(2 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            body = exc.read(1024 * 1024)
            raise BackendError(_safe_comfy_error(int(exc.code), body)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BackendError(
                "ComfyUI is unreachable. Start ComfyUI or check comfy_url in the gateway config."
            ) from exc
        if raw:
            return body
        try:
            return json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise BackendError("ComfyUI returned a non-JSON response.") from exc

    def submit(self, workflow: dict[str, Any]) -> str:
        response = self._request(
            "POST",
            "/prompt",
            {"prompt": workflow, "client_id": f"lm-gateway-{uuid.uuid4().hex[:8]}"},
            self.timeout,
        )
        if not isinstance(response, dict):
            raise BackendError("ComfyUI returned an unexpected /prompt response.")
        if response.get("error") or response.get("node_errors"):
            raise BackendError("ComfyUI rejected the workflow; check the ComfyUI log.")
        prompt_id = str(response.get("prompt_id", "") or "")
        if not JOB_ID_RE.fullmatch(prompt_id):
            raise BackendError("ComfyUI did not return a valid prompt_id.")
        return prompt_id

    def history(self, prompt_id: str) -> dict[str, Any] | None:
        quoted = urllib.parse.quote(prompt_id, safe="")
        response = self._request("GET", f"/history/{quoted}", timeout=self.timeout)
        if not isinstance(response, dict):
            return None
        entry = response.get(prompt_id)
        return entry if isinstance(entry, dict) else None

    def queue_state(self, prompt_id: str) -> str:
        try:
            response = self._request("GET", "/queue", timeout=min(self.timeout, 10))
        except BackendError:
            return "unknown"
        if not isinstance(response, dict):
            return "unknown"
        for key, state in (("queue_running", "running"), ("queue_pending", "queued")):
            items = response.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                identifier = ""
                if isinstance(item, list) and len(item) > 1:
                    identifier = str(item[1])
                elif isinstance(item, dict):
                    identifier = str(item.get("prompt_id", ""))
                if identifier == prompt_id:
                    return state
        return "unknown"

    def download(self, image: dict[str, Any], max_bytes: int) -> bytes:
        query = urllib.parse.urlencode(
            {
                "filename": str(image.get("filename", "")),
                "subfolder": str(image.get("subfolder", "")),
                "type": str(image.get("type", "output")),
            }
        )
        request = urllib.request.Request(
            self.base_url + "/view?" + query,
            headers={"User-Agent": SERVER_NAME},
            method="GET",
        )
        try:
            with _OPENER.open(request, timeout=self.timeout) as response:
                data = response.read(max_bytes + 1)
        except urllib.error.HTTPError as exc:
            body = exc.read(256 * 1024)
            raise BackendError(_safe_comfy_error(int(exc.code), body)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BackendError("Failed to download a generated image from ComfyUI.") from exc
        if len(data) > max_bytes:
            raise BackendError("Generated image exceeds max_download_bytes.")
        return data

    def reachable(self) -> bool:
        try:
            response = self._request("GET", "/system_stats", timeout=2.5)
            return isinstance(response, dict)
        except GatewayError:
            return False


def _reject_bool(name: str, value: Any) -> None:
    if isinstance(value, bool):
        raise GatewayError(f"{name} must be a number, not a boolean.")


def _validate_submit(args: Any) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise GatewayError("Arguments must be a JSON object.")
    allowed = {"prompt", "negative_prompt", "width", "height", "steps", "cfg", "seed"}
    unknown = set(args) - allowed
    if unknown:
        raise GatewayError(
            "Unsupported argument(s): "
            + ", ".join(sorted(unknown))
            + ". This tool accepts prompts and generation parameters only; local paths are not accepted."
        )
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise GatewayError("prompt is required and must be a non-empty string.")
    if len(prompt) > 6000:
        raise GatewayError("prompt exceeds 6000 characters.")
    negative = args.get("negative_prompt", "")
    if negative is None:
        negative = ""
    if not isinstance(negative, str):
        raise GatewayError("negative_prompt must be a string.")
    if len(negative) > 3000:
        raise GatewayError("negative_prompt exceeds 3000 characters.")

    def integer(name: str, default: int, minimum: int, maximum: int) -> int:
        value = args.get(name, default)
        _reject_bool(name, value)
        if not isinstance(value, int):
            raise GatewayError(f"{name} must be an integer.")
        if not minimum <= value <= maximum:
            raise GatewayError(f"{name} must be between {minimum} and {maximum}.")
        return value

    width = integer("width", 512, 64, 2048)
    height = integer("height", 512, 64, 2048)
    if width % 8 or height % 8:
        raise GatewayError("width and height must be multiples of 8.")
    steps = integer("steps", 20, 1, 150)
    seed = integer("seed", -1, -1, 2147483647)
    cfg_value = args.get("cfg", 7.0)
    _reject_bool("cfg", cfg_value)
    if not isinstance(cfg_value, (int, float)):
        raise GatewayError("cfg must be a number.")
    cfg = float(cfg_value)
    if not 0 <= cfg <= 30:
        raise GatewayError("cfg must be between 0 and 30.")
    return {
        "prompt": prompt,
        "negative_prompt": negative,
        "width": width,
        "height": height,
        "steps": steps,
        "cfg": cfg,
        "seed": seed,
    }


class Gateway:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.client = ComfyClient(
            config["comfy_url"],
            timeout=float(config["request_timeout_sec"]),
        )
        self._saved: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def submit(self, raw_args: Any) -> dict[str, Any]:
        args = _validate_submit(raw_args)
        seed = args["seed"]
        if seed == -1:
            seed = random.SystemRandom().randint(0, 2147483647)
        workflow = _read_workflow(self.config["workflow_path"])
        values = {
            "PROMPT": args["prompt"],
            "NEGATIVE_PROMPT": args["negative_prompt"],
            "WIDTH": args["width"],
            "HEIGHT": args["height"],
            "STEPS": args["steps"],
            "CFG": args["cfg"],
            "SEED": seed,
            "BATCH_SIZE": 1,
        }
        filled = _fill(workflow, values)
        prompt_id = self.client.submit(filled)
        return {
            "job_id": prompt_id,
            "status": "queued",
            "seed": seed,
            "width": args["width"],
            "height": args["height"],
        }

    def status(self, raw_args: Any) -> dict[str, Any]:
        if not isinstance(raw_args, dict):
            raise GatewayError("Arguments must be a JSON object.")
        job_id = raw_args.get("job_id")
        if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
            raise GatewayError("job_id is invalid.")
        entry = self.client.history(job_id)
        if entry is None:
            state = self.client.queue_state(job_id)
            return {"job_id": job_id, "status": state}
        status_info = entry.get("status")
        if not isinstance(status_info, dict):
            status_info = {}
        status_str = str(status_info.get("status_str", "") or "")
        if status_str == "error":
            return {
                "job_id": job_id,
                "status": "failed",
                "error": "ComfyUI reported an execution error. Check the ComfyUI log.",
            }
        completed = bool(status_info.get("completed")) or status_str == "success"
        if not completed:
            return {"job_id": job_id, "status": "running"}
        saved = self._ensure_images_saved(job_id, entry)
        return {
            "job_id": job_id,
            "status": "completed",
            "image_count": saved["image_count"],
            "files": saved["files"],
            "metadata_mode": saved["metadata_mode"],
            "metadata_removed": saved["metadata_removed"],
        }

    def _ensure_images_saved(
        self, prompt_id: str, entry: dict[str, Any]
    ) -> dict[str, Any]:
        with self._lock:
            cached = self._saved.get(prompt_id)
            if cached is not None:
                return cached
            outputs = entry.get("outputs")
            if not isinstance(outputs, dict):
                outputs = {}
            images: list[dict[str, Any]] = []
            for node in outputs.values():
                if not isinstance(node, dict):
                    continue
                node_images = node.get("images")
                if not isinstance(node_images, list):
                    continue
                for image in node_images:
                    if isinstance(image, dict) and image.get("filename"):
                        images.append(image)
            output_images = [
                image for image in images if str(image.get("type", "output")) == "output"
            ]
            if output_images:
                images = output_images
            if len(images) > int(self.config["max_output_images"]):
                raise BackendError(
                    "ComfyUI returned more images than max_output_images. Reduce the batch size."
                )
            output_dir: Path = self.config["output_dir"]
            date_dir = output_dir / datetime.now().strftime("%Y%m%d")
            date_dir.mkdir(parents=True, exist_ok=True)
            files: list[str] = []
            removed_count = 0
            metadata_mode = str(self.config.get("metadata_mode", "preserve"))
            for index, image in enumerate(images):
                data = self.client.download(image, int(self.config["max_download_bytes"]))
                try:
                    image_format = detect_image_format(data)
                except MetadataError as exc:
                    raise BackendError(str(exc)) from exc
                payload = data
                removed: list[str] = []
                if metadata_mode == "strip":
                    try:
                        payload, removed, image_format = strip_image_bytes(data)
                    except MetadataError as exc:
                        raise BackendError(str(exc)) from exc
                suffix = ".png" if image_format == "png" else ".jpg"
                filename = f"{prompt_id[:8]}_{index}{suffix}"
                target = (date_dir / filename).resolve()
                if not _is_within(target, output_dir):
                    raise BackendError("Refusing to write outside output_dir.")
                handle, temp_name = tempfile.mkstemp(
                    prefix=".gateway-", suffix=suffix, dir=str(date_dir)
                )
                try:
                    with os.fdopen(handle, "wb") as stream:
                        stream.write(payload)
                    os.replace(temp_name, target)
                except BaseException:
                    try:
                        os.unlink(temp_name)
                    except OSError:
                        pass
                    raise
                files.append(target.relative_to(output_dir).as_posix())
                removed_count += len(removed)
            result = {
                "image_count": len(files),
                "files": files,
                "metadata_mode": metadata_mode,
                "metadata_removed": removed_count,
            }
            self._saved[prompt_id] = result
            return result

    def health(self) -> dict[str, Any]:
        workflow_ok = True
        try:
            _read_workflow(self.config["workflow_path"])
        except ConfigError:
            workflow_ok = False
        return {
            "ok": True,
            "server": SERVER_NAME,
            "version": SERVER_VERSION,
            "pid": os.getpid(),
            "comfy_reachable": self.client.reachable(),
            "workflow_ready": workflow_ok,
            "metadata_mode": self.config.get("metadata_mode", "preserve"),
        }


class HttpGatewayClient:
    def __init__(self, base_url: str, token: str, timeout: float = 20.0) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ConfigError("--url must be an http(s) URL.")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": SERVER_NAME,
            },
            method="POST",
        )
        try:
            with _OPENER.open(request, timeout=self.timeout) as response:
                body = response.read(2 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            body = exc.read(2 * 1024 * 1024)
            try:
                data = json.loads(body.decode("utf-8", errors="replace"))
                message = str(data.get("error", "") or "")
            except Exception:
                message = ""
            raise BackendError(message or f"Gateway HTTP {exc.code}.") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BackendError(PROXY_OFF_MESSAGE) from exc
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise BackendError("Isolation gateway returned a non-JSON response.") from exc
        if not isinstance(data, dict):
            raise BackendError("Isolation gateway returned an unexpected response.")
        if not data.get("ok", False):
            raise BackendError(str(data.get("error", "") or "Gateway request failed."))
        return data

    def submit(self, args: Any) -> dict[str, Any]:
        return self._post("/submit", args)

    def status(self, args: Any) -> dict[str, Any]:
        return self._post("/status", args)


def _mcp_error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": code, "message": message},
    }


def _mcp_result(message_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def handle_rpc(message: Any, backend: Any) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return _mcp_error(None, -32700, "Parse error")
    message_id = message.get("id")
    method = message.get("method")
    if not isinstance(method, str):
        return _mcp_error(message_id, -32600, "Invalid request")
    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        params = message.get("params")
        requested = "2024-11-05"
        if isinstance(params, dict) and isinstance(params.get("protocolVersion"), str):
            requested = params["protocolVersion"]
        return _mcp_result(
            message_id,
            {
                "protocolVersion": requested,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "ping":
        return _mcp_result(message_id, {})
    if method == "tools/list":
        return _mcp_result(message_id, {"tools": TOOLS})
    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict):
            return _mcp_result(
                message_id,
                {
                    "content": [{"type": "text", "text": "Invalid tool parameters."}],
                    "isError": True,
                },
            )
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            if name == "submit_image_generation":
                result = backend.submit(arguments)
            elif name == "get_generation_status":
                result = backend.status(arguments)
            else:
                raise GatewayError(f"Unknown tool: {name}")
            return _mcp_result(
                message_id,
                {
                    "content": [
                        {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                    ],
                    "isError": False,
                },
            )
        except GatewayError as exc:
            return _mcp_result(
                message_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"ok": False, "error": str(exc)}, ensure_ascii=False
                            ),
                        }
                    ],
                    "isError": True,
                },
            )
        except Exception:
            return _mcp_result(
                message_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"ok": False, "error": "Internal gateway error."},
                                ensure_ascii=False,
                            ),
                        }
                    ],
                    "isError": True,
                },
            )
    return _mcp_error(message_id, -32601, f"Method not found: {method}")


def _write_mcp(payload: dict[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    sys.stdout.flush()


def run_stdio(backend: Any) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _write_mcp(_mcp_error(None, -32700, "Parse error"))
            continue
        try:
            response = handle_rpc(message, backend)
        except Exception:
            response = _mcp_error(
                message.get("id") if isinstance(message, dict) else None,
                -32603,
                "Internal error",
            )
        if response is not None:
            _write_mcp(response)
    return 0


class GatewayRequestHandler(BaseHTTPRequestHandler):
    server_version = f"{SERVER_NAME}/{SERVER_VERSION}"
    protocol_version = "HTTP/1.1"

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        token = self.server.gateway_token  # type: ignore[attr-defined]
        header = self.headers.get("Authorization", "")
        if not token:
            return False
        return hmac.compare_digest(header, f"Bearer {token}")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/health":
            gateway: Gateway = self.server.gateway  # type: ignore[attr-defined]
            self._send_json(200, gateway.health())
            return
        self._send_json(404, {"ok": False, "error": "Not found."})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path not in ("/submit", "/status"):
            self._send_json(404, {"ok": False, "error": "Not found."})
            return
        if not self._authorized():
            self._send_json(401, {"ok": False, "error": "Unauthorized."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(400, {"ok": False, "error": "Invalid request body size."})
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "Invalid JSON."})
            return
        gateway: Gateway = self.server.gateway  # type: ignore[attr-defined]
        try:
            if self.path == "/submit":
                result = gateway.submit(payload)
            else:
                result = gateway.status(payload)
            self._send_json(200, {"ok": True, **result})
        except GatewayError as exc:
            self._send_json(exc.http_status, {"ok": False, "error": str(exc)})
        except Exception:
            debug = bool(getattr(self.server, "debug", False))
            if debug:
                traceback.print_exc(file=sys.stderr)
            self._send_json(500, {"ok": False, "error": "Internal gateway error."})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if getattr(self.server, "debug", False):
            super().log_message(format, *args)


def run_http(
    gateway: Gateway,
    host: str,
    port: int,
    token: str,
    debug: bool = False,
) -> int:
    if not token:
        raise ConfigError("HTTP mode requires IMAGE_GATEWAY_TOKEN or a token in the config.")
    server = ThreadingHTTPServer((host, int(port)), GatewayRequestHandler)
    server.daemon_threads = True
    server.gateway = gateway  # type: ignore[attr-defined]
    server.gateway_token = token  # type: ignore[attr-defined]
    server.debug = debug  # type: ignore[attr-defined]
    print(
        f"{SERVER_NAME} {SERVER_VERSION} listening on {host}:{port}",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def check_config(path: str | Path) -> dict[str, Any]:
    config = load_config(path)
    workflow = _read_workflow(config["workflow_path"])
    placeholders = sorted(_find_placeholders(workflow) & KNOWN_PLACEHOLDERS)
    return {
        "ok": True,
        "config_path": str(config["config_path"]),
        "comfy_url": config["comfy_url"],
        "workflow_path": str(config["workflow_path"]),
        "output_dir": str(config["output_dir"]),
        "workspace_root": str(config["workspace_root"]) if config["workspace_root"] else None,
        "output_outside_workspace": (
            True
            if config["workspace_root"] is None
            else not _paths_overlap(config["output_dir"], config["workspace_root"])
        ),
        "workflow_placeholders": placeholders,
        "metadata_mode": config["metadata_mode"],
        "http_host": config["http_host"],
        "http_port": config["http_port"],
        "token_configured": bool(config["token"]),
    }


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Narrow image-generation gateway.")
    parser.add_argument("--debug", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    stdio_parser = sub.add_parser("stdio", help="Run MCP over stdio with direct ComfyUI access.")
    stdio_parser.add_argument("--config", required=True)

    http_parser = sub.add_parser("http", help="Run the HTTP gateway used by isolationctl.")
    http_parser.add_argument("--config", required=True)
    http_parser.add_argument("--host")
    http_parser.add_argument("--port", type=int)
    http_parser.add_argument("--token-env", default="IMAGE_GATEWAY_TOKEN")

    proxy_parser = sub.add_parser("proxy", help="Run MCP over stdio and forward to the HTTP gateway.")
    proxy_parser.add_argument("--url", required=True)
    proxy_parser.add_argument("--token-env", default="IMAGE_GATEWAY_TOKEN")

    check_parser = sub.add_parser("check-config", help="Validate config and workflow.")
    check_parser.add_argument("--config", required=True)

    sub.add_parser("version", help="Print the gateway version.")

    args = parser.parse_args(argv)
    try:
        if args.command == "version":
            print(f"{SERVER_NAME} {SERVER_VERSION}")
            return 0
        if args.command == "check-config":
            print(json.dumps(check_config(args.config), ensure_ascii=False, indent=2))
            return 0
        if args.command == "stdio":
            config = load_config(args.config)
            gateway = Gateway(config)
            return run_stdio(gateway)
        if args.command == "http":
            config = load_config(args.config)
            token = os.environ.get(args.token_env) or config["token"]
            gateway = Gateway(config)
            return run_http(
                gateway,
                args.host or config["http_host"],
                args.port or config["http_port"],
                token,
                debug=args.debug,
            )
        if args.command == "proxy":
            token = os.environ.get(args.token_env, "")
            if not token:
                raise ConfigError(
                    f"Missing {args.token_env}. Configure it in the MCP server environment."
                )
            client = HttpGatewayClient(args.url, token)
            return run_stdio(client)
    except GatewayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

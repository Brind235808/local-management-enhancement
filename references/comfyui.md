# ComfyUI gateway, workflow, and metadata

## What the gateway is

`image_gateway.py` is the only image capability exposed to an agent. It has no
`read_file`, `list_files`, `glob`, shell, or arbitrary path argument. It accepts text
and numeric parameters, fills a ComfyUI API-format workflow, submits it, and downloads
completed images into a private archive. The agent sees a `job_id` and a status, not a
local filesystem path.

Transports:

- `http` — used by the session switch and the host MCP proxy.
- `proxy` — MCP over stdio, forwards to the running HTTP gateway; fail-closed when OFF.
- `stdio` — direct MCP to ComfyUI, for local testing/advanced use only.

MCP tools:

1. `submit_image_generation(prompt, negative_prompt?, width?, height?, steps?, cfg?, seed?)`
2. `get_generation_status(job_id)`

There is deliberately no image upload/reference tool. If the user needs a reference,
put one approved file in `session/in/`; that is a file-access decision, not a gateway
parameter.

## Prepare the API workflow

1. Start ComfyUI and build a working workflow with your model.
2. Enable **Settings → Enable Dev mode Options**.
3. Use **Save (API Format)** to export JSON, not the normal UI workflow.
4. Edit the exported file and replace the dynamic inputs with placeholders:

| Placeholder | Value |
|---|---|
| `{{PROMPT}}` | positive prompt (required) |
| `{{NEGATIVE_PROMPT}}` | negative prompt |
| `{{WIDTH}}` | width integer |
| `{{HEIGHT}}` | height integer |
| `{{STEPS}}` | steps |
| `{{CFG}}` | CFG scale |
| `{{SEED}}` | seed (gateway randomizes `-1`) |
| `{{BATCH_SIZE}}` | currently fixed at 1 |

The workflow must contain `{{PROMPT}}` in at least one string input. Unknown
`{{...}}` placeholders are rejected at startup. Replace the example checkpoint name
(`REPLACE_WITH_YOUR_MODEL.safetensors`) with a model that exists in your ComfyUI.

The example at `assets/comfy_api_workflow.example.json` is a minimal SD1.5-style graph.
It is a format example, not a guarantee that your checkpoint/nodes exist.

## Metadata and reproducibility

Default: **preserve**. The gateway saves the exact bytes downloaded from ComfyUI into
`private/archive/outputs/YYYYMMDD/`. ComfyUI's PNG prompt/workflow metadata stays intact.
The source output in ComfyUI is not deleted. Keep `private/` private, exclude it from
cloud backups you do not control, and do not mount it into an agent.

Optional: set `"metadata_mode": "strip"` in `private/config/gateway.json` (or bootstrap
with `--metadata-mode strip`) to save sanitized copies without PNG `tEXt`/`zTXt`/`iTXt`/
`eXIf` or JPEG EXIF/COM segments. This breaks reproduction from the image metadata.
Use it only when the user explicitly wants a shareable copy and does not need the
original. `scripts/strip_metadata.py` can sanitize a **copy** later.

Do not add `--disable-metadata` to ComfyUI by default. It removes the metadata at the
source and makes reproducibility worse. The isolation boundary is the private archive
plus agent permissions, not metadata destruction.

## Gateway config

`private/config/gateway.json` (host):

```json
{
  "comfy_url": "http://127.0.0.1:8188",
  "workflow_path": ".../private/config/comfy_api_workflow.json",
  "output_dir": ".../private/archive/outputs",
  "workspace_root": ".../session",
  "http_host": "127.0.0.1",
  "http_port": 18765,
  "token": "...",
  "metadata_mode": "preserve",
  "max_output_images": 8,
  "max_download_bytes": 67108864,
  "request_timeout_sec": 30,
  "download_timeout_sec": 60
}
```

Invariants enforced at load time:

- `output_dir` and `workflow_path` must be outside `workspace_root`.
- `metadata_mode` must be `preserve` or `strip`.
- numeric limits are bounded;
- the workflow exists, is a JSON object, has `{{PROMPT}}`, and has no unknown placeholders.

Run a dry validation:

```bash
python scripts/image_gateway.py check-config --config <runtime>/private/config/gateway.json
```

## HTTP API

All POST bodies are JSON; `/submit` and `/status` require
`Authorization: Bearer <token>`.

```bash
curl -s http://127.0.0.1:18765/health
curl -s -X POST http://127.0.0.1:18765/submit \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"prompt":"a red fox in snow","width":512,"height":512,"steps":20,"seed":42}'
curl -s -X POST http://127.0.0.1:18765/status \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"job_id":"<job-id>"}'
```

`/health` returns `ok`, `server`, `pid`, `comfy_reachable`, `workflow_ready`, and
`metadata_mode` without exposing prompts or paths. The switch uses it to decide ON/OFF.

## DeepSeek / scheduler example

Keep the LLM tool signature identical to the gateway schema. The scheduler validates the
model's JSON and calls the gateway; the model never receives a filesystem tool.

```python
import json, urllib.request

params = {"prompt": "...", "negative_prompt": "...", "width": 512, "height": 512,
          "steps": 20, "cfg": 7.0, "seed": -1}
allowed = {"prompt", "negative_prompt", "width", "height", "steps", "cfg", "seed"}
if set(params) - allowed:
    raise ValueError("model returned unsupported fields")

token = open(r"<runtime>/private/config/token.txt", encoding="utf-8").read().strip()
request = urllib.request.Request(
    "http://127.0.0.1:18765/submit",
    data=json.dumps(params).encode(),
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
)
with urllib.request.urlopen(request, timeout=30) as response:
    print(json.load(response)["job_id"])
```

The scheduler can live on the private side (it knows the token and archive). Do not hand
the scheduler's file-reading functions to the LLM. If the prompt goes to a cloud LLM,
apply `prompt_redact.py` before sending and restore tokens locally afterward.

## ComfyUI startup

Host:

```bash
python main.py --listen 127.0.0.1 --port 8188
```

ComfyUI's `/history` endpoint exposes prompt/workflow data to anyone who can reach the
port. Keep it on localhost, or only on the Docker `ai-net` with no host port. Do not
publish 8188 to a LAN or the internet. If you must expose it, put an authenticated
reverse proxy in front and restrict sources.

Docker:

- `comfyui` runs on internal `ai-net`, no host port.
- `gateway` is the only service attached to both `ai-net` and the agent network.
- `agent` has no `ai-net`, so it cannot call ComfyUI directly.
- Models mount from `private/models/`; output/history stay in the container/named volume.
- `COMFYUI_ARGS` keeps metadata by default. Set `--disable-metadata` only if
  reproducibility is not needed.

## Failure modes

| Symptom | Check |
|---|---|
| `workflow_ready: false` / `{{PROMPT}}` error | Export API format, add placeholders, ensure JSON object |
| ComfyUI checkpoint error | Replace `REPLACE_WITH_YOUR_MODEL.safetensors` with an installed model |
| Gateway unreachable | `isolationctl status --json`; is the switch ON? Is the port busy? |
| ComfyUI unreachable | Start it on 127.0.0.1:8188 or fix `comfy_url`; Docker: check `comfyui` service |
| `Unsupported argument(s): image_path` | By design. The tool accepts prompts/numbers only |
| `ComfyUI returned more images than max_output_images` | Reduce batch size or raise the limit |
| `unsupported image format` | Configure SaveImage to PNG/JPEG; WebP is not accepted |
| Gateway OFF/unreachable error from MCP | Turn isolation ON; this is the fail-closed contract |
| Docker gateway unhealthy | `docker compose ... logs gateway`; check UID/permission on `private/archive` |

## Security checklist

- Keep `private/` off shared/cloud-synced folders and out of every agent workspace.
- Do not mount the host home, SSH keys, Docker socket, or model archive into the agent.
- Keep the gateway port on `127.0.0.1`.
- Use a long random token; rotate with `bootstrap_isolation.py --reset-config` if leaked.
- Treat the token as capability-limited, not secret from the agent: it only submits and
  polls images.
- Add rate limiting if cost/resource abuse is a concern; the gateway does not rate-limit.
- For GUI agents, use a VM/ACL; rules and MCP config are not a hard boundary.
- Preserve originals; sanitize copies only, and only when sharing.

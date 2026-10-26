#!/bin/sh
set -eu

# The agent home is a named volume, so write only the gateway MCP profile here.
# The token is injected by Docker Compose and is not stored in this script.
mkdir -p "$HOME/.codex"
cat > "$HOME/.codex/config.toml" <<EOF
[mcp_servers.image_gateway]
command = "python3"
args = ["/opt/image-gateway/image_gateway.py", "proxy", "--url", "${IMAGE_GATEWAY_URL:-http://gateway:8765}"]
env = { IMAGE_GATEWAY_TOKEN = "${IMAGE_GATEWAY_TOKEN}" }
EOF

exec sleep infinity

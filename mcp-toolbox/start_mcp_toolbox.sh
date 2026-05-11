#!/usr/bin/env bash
set -euo pipefail

# Start the ADK MCP Toolbox for Spanner access.
# Downloads the toolbox binary if not present.
#
# Usage:
#   ./start_mcp_toolbox.sh                    # default port 5000
#   PORT=8080 ./start_mcp_toolbox.sh          # custom port
#
# The toolbox serves MCP over HTTP at http://localhost:$PORT/mcp.
# ReflexAgent reads TOOLBOX_URL from .env or env var.

PORT="${PORT:-5000}"
TOOLBOX_DIR="$(cd "$(dirname "$0")" && pwd)"
BINARY="${TOOLBOX_DIR}/mcp_toolbox"

if [ ! -f "$BINARY" ]; then
    echo "[MCP] Downloading ADK MCP Toolbox binary..."
    OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
    ARCH="$(uname -m)"
    case "$ARCH" in
        x86_64) ARCH="amd64" ;;
        aarch64|arm64) ARCH="arm64" ;;
    esac
    URL="https://storage.googleapis.com/adk-mcp-toolbox/${OS}-${ARCH}/latest/mcp_toolbox"
    curl -fsSL "$URL" -o "$BINARY"
    chmod +x "$BINARY"
    echo "[MCP] Downloaded to $BINARY"
fi

echo "[MCP] Starting ADK MCP Toolbox on port $PORT..."
echo "[MCP] Tools config: ${TOOLBOX_DIR}/tools.yaml"
echo "[MCP] Endpoint: http://localhost:${PORT}/mcp"

exec "$BINARY" \
    --port "$PORT" \
    --tools "${TOOLBOX_DIR}/tools.yaml" \
    --project "$(grep GOOGLE_CLOUD_PROJECT "${TOOLBOX_DIR}/../.env" 2>/dev/null | cut -d= -f2 || echo "poc-z-in2300756")"

#!/bin/bash
# ============================================================
# start_mcp_toolbox.sh
# Starts MCP Toolbox for Databases v1.2.0 (googleapis/mcp-toolbox)
#
# This is Terminal 1 — keep it running while main.py runs.
# Terminal 2: python main.py  (no changes needed there)
#
# Auth: gcloud auth application-default login (run once, shared)
# ============================================================

set -e

TOOLBOX_VERSION="1.2.0"
TOOLBOX_PORT="${TOOLBOX_PORT:-5000}"
TOOLS_YAML="${TOOLS_YAML:-./tools.yaml}"

OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)
if [ "$ARCH" = "x86_64" ];  then ARCH="amd64"; fi
if [ "$ARCH" = "aarch64" ]; then ARCH="arm64"; fi

# v1.2.0: binary is named "toolbox" (URL includes OS/arch in path)
BINARY="toolbox"
DOWNLOAD_URL="https://storage.googleapis.com/mcp-toolbox-for-databases/v${TOOLBOX_VERSION}/${OS}/${ARCH}/toolbox"

echo "================================================"
echo "  MCP Toolbox for Databases v${TOOLBOX_VERSION}"
echo "================================================"
echo "  OS/Arch : ${OS}/${ARCH}"
echo "  Port    : ${TOOLBOX_PORT}"
echo "  Config  : ${TOOLS_YAML}"
echo "================================================"

# Download binary if not present
if [ ! -f "./${BINARY}" ]; then
    echo ""
    echo "[1/3] Downloading MCP Toolbox v${TOOLBOX_VERSION}..."
    curl -O -L "${DOWNLOAD_URL}"
    chmod +x "./${BINARY}"
    echo "      Done."
else
    echo "[1/3] Binary already present: ./${BINARY}"
fi

# Verify tools.yaml exists
if [ ! -f "${TOOLS_YAML}" ]; then
    echo ""
    echo "ERROR: tools.yaml not found at: ${TOOLS_YAML}"
    exit 1
fi
echo "[2/3] tools.yaml found: ${TOOLS_YAML}"

# Check gcloud auth
if ! gcloud auth application-default print-access-token > /dev/null 2>&1; then
    echo ""
    echo "WARNING: No application-default credentials found."
    echo "Run: gcloud auth application-default login"
fi
echo "[3/3] Auth check complete."

echo ""
echo "Starting MCP Toolbox on port ${TOOLBOX_PORT}..."
echo "ReflexAgent detects it via: GET http://localhost:${TOOLBOX_PORT}/api/tool"
echo "Press Ctrl+C to stop."
echo "================================================"

"./${BINARY}" \
    --tools-file "${TOOLS_YAML}" \
    --port "${TOOLBOX_PORT}"
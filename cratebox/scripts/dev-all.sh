#!/bin/sh
set -eu

api_dir=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
repository_root=$(CDPATH= cd "$api_dir/.." && pwd)
mcp_dir=$(CDPATH= cd "$api_dir/rekordbx-mcp" && pwd)
existing_mcp_env="$repository_root/rekordbx-mcp/.venv"
if [ -x "$existing_mcp_env/bin/python" ] && [ ! -x "$mcp_dir/.venv/bin/python" ]; then
  export UV_PROJECT_ENVIRONMENT="$existing_mcp_env"
fi
uv --directory "$mcp_dir" run --locked --with fastapi --with uvicorn --with mutagen --with ddgs --with playwright \
  --with onnxruntime --with opencv-python-headless \
  uvicorn --app-dir "$api_dir" --reload-dir "$api_dir" --reload-dir "$mcp_dir/src" \
  server:app --port 8000 --reload &
api_pid=$!

cleanup() {
  kill "$api_pid" 2>/dev/null || true
  wait "$api_pid" 2>/dev/null || true
}

trap cleanup EXIT INT TERM
npm run dev

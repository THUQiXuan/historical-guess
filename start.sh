#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
umask 077
if [[ ! -x .venv/bin/python ]] || ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
  if command -v uv >/dev/null 2>&1; then
    uv venv --seed --allow-existing .venv
  else
    python3 -m venv .venv
  fi
fi
if ! .venv/bin/python -c 'import fastapi, uvicorn, httpx, dotenv' >/dev/null 2>&1; then
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python .venv/bin/python -r requirements.txt
  else
    .venv/bin/python -m pip install -r requirements.txt
  fi
fi
mkdir -p var/agent-workspace
echo "问古：http://${HOST:-127.0.0.1}:${PORT:-7992} · Ctrl+C 关闭服务"
env_args=()
if [[ -f .env ]]; then env_args=(--env-file .env); fi
exec .venv/bin/python -m uvicorn server.app:create_app --factory --host "${HOST:-127.0.0.1}" --port "${PORT:-7992}" --workers 1 "${env_args[@]}"

#!/usr/bin/env bash
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_DIR="$(cd "${AGENT_DIR}/../../.." && pwd)"
SITE_NAME="${SHIPKIA_SITE:-development.localhost}"
ENV_FILE="${SHIPKIA_ENV_FILE:-${BENCH_DIR}/sites/${SITE_NAME}/private/shipkia_livekit/.env.local}"
VENV_DIR="${SHIPKIA_VENV_DIR:-/home/harsh/.local/share/shipkia-livekit/.venv}"
LOCK_FILE="${SHIPKIA_WORKER_LOCK_FILE:-${BENCH_DIR}/sites/${SITE_NAME}/private/shipkia_livekit/worker.lock}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "ShipKia environment file not found: ${ENV_FILE}" >&2
  echo "Run confluence_ai.shipkia_setup.configure_shipkia_voice first." >&2
  exit 1
fi

set -a
source "${ENV_FILE}"
set +a

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  echo "ShipKia worker environment not found: ${VENV_DIR}" >&2
  echo "Create it with uv venv and install livekit_agent/requirements.txt." >&2
  exit 1
fi

if ! command -v flock >/dev/null 2>&1; then
  echo "The flock command is required to enforce one ShipKia worker." >&2
  exit 1
fi

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "A ShipKia LiveKit worker is already running; refusing to start a duplicate." >&2
  exit 73
fi

cd "${AGENT_DIR}"
exec "${VENV_DIR}/bin/python" agent.py dev

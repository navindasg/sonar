#!/usr/bin/env bash
#
# Install the Sonar brief-builder launchd LaunchAgent from the template.
#
# This substitutes real paths into com.sonar.brief-builder.plist and copies it
# to ~/Library/LaunchAgents, then `launchctl load`s it. RunAtLoad is false, so
# loading does NOT run a brief immediately — the first real brief is run by you.
#
# Usage:  ./install.sh
# Requires: uv on PATH.

set -euo pipefail

LABEL="com.sonar.brief-builder"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="${SCRIPT_DIR}/${LABEL}.plist"
AGENTS_DIR="${HOME}/Library/LaunchAgents"
TARGET="${AGENTS_DIR}/${LABEL}.plist"
LOGDIR="${HOME}/Library/Logs/sonar"

UV_BIN="$(command -v uv || true)"
if [[ -z "${UV_BIN}" ]]; then
    echo "error: 'uv' not found on PATH; install uv first." >&2
    exit 1
fi

if [[ ! -f "${TEMPLATE}" ]]; then
    echo "error: template not found: ${TEMPLATE}" >&2
    exit 1
fi

mkdir -p "${AGENTS_DIR}" "${LOGDIR}"

# Substitute placeholders into the target plist.
sed \
    -e "s|__UV_BIN__|${UV_BIN}|g" \
    -e "s|__WORKDIR__|${SCRIPT_DIR}|g" \
    -e "s|__LOGDIR__|${LOGDIR}|g" \
    "${TEMPLATE}" > "${TARGET}"

# Reload cleanly if already present.
launchctl unload "${TARGET}" 2>/dev/null || true
launchctl load "${TARGET}"

echo "installed: ${TARGET}"
echo "scheduled: daily at 07:00 (window=morning), RunAtLoad=false"
echo "logs:      ${LOGDIR}/brief-builder.{out,err}.log"
echo
echo "Run the first brief yourself, e.g.:"
echo "  uv run --directory '${SCRIPT_DIR}' python -m brief_builder --window any --vault '<your vault>'"

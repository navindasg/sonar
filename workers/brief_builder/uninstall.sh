#!/usr/bin/env bash
#
# Uninstall the Sonar brief-builder launchd LaunchAgent.
#
# Usage:  ./uninstall.sh

set -euo pipefail

LABEL="com.sonar.brief-builder"
TARGET="${HOME}/Library/LaunchAgents/${LABEL}.plist"

if [[ -f "${TARGET}" ]]; then
    launchctl unload "${TARGET}" 2>/dev/null || true
    rm -f "${TARGET}"
    echo "removed: ${TARGET}"
else
    echo "nothing to remove: ${TARGET} not found"
fi

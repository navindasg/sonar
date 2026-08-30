#!/usr/bin/env bash
#
# Build the SonarApp binary with SwiftPM and assemble it into a runnable
# app/build/Sonar.app bundle. No Xcode project involved.
#
#   ./build-app.sh            # release build (default)
#   ./build-app.sh debug      # debug build
#
# Launch with `open build/Sonar.app` — running the bare .build/<cfg>/SonarApp
# binary won't pick up LSUIElement, the status item, or a bundle TCC identity.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CONFIG="${1:-release}"

echo "==> swift build -c ${CONFIG}"
swift build -c "${CONFIG}"

BIN=".build/${CONFIG}/SonarApp"
if [ ! -x "${BIN}" ]; then
	echo "error: build did not produce ${BIN}" >&2
	exit 1
fi

APP="build/Sonar.app"
echo "==> assembling ${APP}"
rm -rf "${APP}"
mkdir -p "${APP}/Contents/MacOS" "${APP}/Contents/Resources"

cp "${BIN}" "${APP}/Contents/MacOS/SonarApp"
cp "Info.plist" "${APP}/Contents/Info.plist"
printf 'APPL????' > "${APP}/Contents/PkgInfo"

# Stamp the checkout path so a bundle dragged out of the repo (into
# /Applications, say) can still find voice/ for the notes backend — Config
# reads this after its bundle/executable walks fail. The plist is re-copied
# fresh above every build, so `Add` never collides with an existing key. Must
# precede the codesign below, or the signature won't cover the edited plist.
REPO_ROOT="$(dirname "${SCRIPT_DIR}")"
/usr/libexec/PlistBuddy -c "Add :SONARRepoRoot string ${REPO_ROOT}" \
	"${APP}/Contents/Info.plist" >/dev/null
echo "==> stamped SONARRepoRoot=${REPO_ROOT}"

# Stable ad-hoc identity so a later mic-TCC grant can stick across launches.
if command -v codesign >/dev/null 2>&1; then
	if codesign --force --deep --sign - "${APP}" >/dev/null 2>&1; then
		echo "==> ad-hoc signed"
	else
		echo "==> codesign skipped (non-fatal)"
	fi
fi

echo "==> built ${SCRIPT_DIR}/${APP}"
echo "    open '${SCRIPT_DIR}/${APP}'"

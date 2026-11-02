#!/bin/bash
# Assemble "StrataSync PON Audit.app" from the release build and the sheets
# bridge, sign it ad hoc, and zip it to dist/StrataSync-PON-Audit-macOS.zip.
#   cargo build --release && python sheets_bridge/build.py && packaging/macos/make_app.sh
set -euo pipefail
cd "$(dirname "$0")/../.."

NAME="StrataSync PON Audit"
APP="dist/$NAME.app"
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' Cargo.toml | head -1)

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp target/release/stratasync-pon-audit "$APP/Contents/MacOS/$NAME"
# The app looks for the bridge next to itself.
cp dist/sheets-bridge "$APP/Contents/MacOS/sheets-bridge"
sed "s/__VERSION__/$VERSION/g" packaging/macos/Info.plist > "$APP/Contents/Info.plist"

# Not notarized; an ad-hoc signature keeps Apple silicon and Gatekeeper's checks consistent.
codesign --force --deep --sign - "$APP"

rm -f dist/StrataSync-PON-Audit-macOS.zip
# ditto keeps the bundle's symlinks and metadata intact (zip -r doesn't).
ditto -c -k --keepParent "$APP" dist/StrataSync-PON-Audit-macOS.zip
echo "Built dist/StrataSync-PON-Audit-macOS.zip"

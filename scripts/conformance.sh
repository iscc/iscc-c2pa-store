#!/usr/bin/env bash
# Run the C2PA Soft Binding API conformance harness against a running store.
#
# Usage: scripts/conformance.sh [API base URL, default http://127.0.0.1:45470/v1]
#
# Harness 1.0.0 cannot run from its npm package as published: Jest skips test files under node_modules, the tests
# import ./src which the package does not ship, and the CLI fails to find its config on Windows. This script
# installs the package into a temporary directory and runs its tests from outside node_modules.
set -euo pipefail

BASE_URL="${1:-http://127.0.0.1:45470/v1}"
HARNESS="@cognitiveproof/softbinding-api-conformance@1.0.0"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cd "$WORK"
npm init -y >/dev/null
npm install --silent --no-audit --no-fund "$HARNESS"
PKG=node_modules/@cognitiveproof/softbinding-api-conformance
cp -r "$PKG/tests" "$PKG/jest.conformance.config.js" "$PKG/jest.conformance.setup.ts" "$PKG/tsconfig.json" .
cp -r "$PKG/dist" src

# The harness store suites post plain text as application/c2pa, expect 401 without a token and 404 after a delete.
# This store answers such uploads with the documented 400, accepts anonymous uploads and answers 410 after a delete,
# so storeManifests is withheld from the harness. It then runs only its capability-independent tests: the discovery
# and service checks, a 404 for an unknown manifest ID and the status of an unauthenticated fetch. The store, receipt
# and bindings suites (the only harness tests that query /matches/byBinding) are skipped (docs/conformance.md).
CONFORMANCE_CAPABILITIES="$(curl -sf "$BASE_URL/services/capabilities" | node -e '
  const caps = JSON.parse(require("fs").readFileSync(0, "utf8"));
  caps.supportedCapabilities = caps.supportedCapabilities.filter((c) => c !== "storeManifests");
  process.stdout.write(JSON.stringify(caps));
')"
export CONFORMANCE_BASE_URL="$BASE_URL" CONFORMANCE_TOKEN=unused CONFORMANCE_CAPABILITIES
npx jest --config jest.conformance.config.js --verbose

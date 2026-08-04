#!/usr/bin/env bash
# Double-click launcher for the user-owned MSX-AI TCP test instance.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$PROJECT_DIR/open-msx.command" mcp "$@"

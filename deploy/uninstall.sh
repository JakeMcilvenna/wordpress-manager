#!/bin/bash
set -euo pipefail
# --dry-run previews; default asks for REMOVE PANEL; --yes approves automation.
# Recovery data stays unless --purge-recovery is explicitly selected. See README.
# No PATH lookup, inherited Python configuration, or shell evaluation of arguments.
SCRIPT_DIR="$(cd -- "$(/usr/bin/dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec /usr/bin/python3 -I -B "$SCRIPT_DIR/manage.py" uninstall "$@"

#!/bin/bash
set -euo pipefail
# No PATH lookup, inherited Python configuration, or shell evaluation of arguments.
SCRIPT_DIR="$(cd -- "$(/usr/bin/dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec /usr/bin/python3 -I -B "$SCRIPT_DIR/manage.py" upgrade "$@"

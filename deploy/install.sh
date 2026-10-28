#!/bin/bash
set -euo pipefail
# No PATH lookup, inherited Python configuration, or shell evaluation of arguments.
SCRIPT_DIR="$(cd -- "$(/usr/bin/dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# Ubuntu cloud images include Python for cloud-init; minimal supported images may not.
if [[ ! -x /usr/bin/python3 ]]; then
  [[ $EUID -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
  source /etc/os-release
  [[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]] || { echo 'Only Ubuntu 24.04 is supported.' >&2; exit 1; }
  [[ " $* " == *' --non-interactive '* ]] || { echo 'Python bootstrap requires explicit --non-interactive.' >&2; exit 1; }
  /usr/bin/apt-get update
  DEBIAN_FRONTEND=noninteractive /usr/bin/apt-get install --yes --no-install-recommends python3
fi
exec /usr/bin/python3 -I -B "$SCRIPT_DIR/manage.py" install "$@"

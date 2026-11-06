#!/usr/bin/env bash
set -euo pipefail
package_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec python3 "$package_dir/install.py" "$@"

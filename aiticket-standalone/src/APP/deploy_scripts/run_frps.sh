#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/frp_common.sh"

ensure_frp_binary "frps"
render_frps_config

cd "$FRP_DIR"
exec ./frps -c frps.ini

#!/usr/bin/env bash
# Owner-operated only. No deploy, restart, settings fetch, or external network call.
set +x
set -euo pipefail
umask 077
[[ "$EUID" == 0 ]] || { echo 'reason=app_role_root_required ok=false'; exit 1; }
exec 9>/var/lock/mia-state.lock
flock -n 9 || { echo 'reason=app_role_operation_busy ok=false'; exit 1; }
exec python3 "$(dirname "$(readlink -f "$0")")/app_role_demote.py" "$@"

#!/usr/bin/env bash
# Owner-operated only. No deploy, restart, settings fetch, or external network call.
# Takes the shared state lock, then runs the helper. Flags: none (stage), --check, --activate,
# --enforce, --rollback, --reapply (the post-restore repair; idempotent, writes no env file).
# A caller that already holds /var/lock/mia-state.lock (`mia restore`, V2-10) must run
# app_role_demote.py --reapply directly: a second flock on the same file would report busy.
set +x
set -euo pipefail
umask 077
[[ "$EUID" == 0 ]] || { echo 'reason=app_role_root_required ok=false'; exit 1; }
exec 9>/var/lock/mia-state.lock
flock -n 9 || { echo 'reason=app_role_operation_busy ok=false'; exit 1; }
exec python3 "$(dirname "$(readlink -f "$0")")/app_role_demote.py" "$@"

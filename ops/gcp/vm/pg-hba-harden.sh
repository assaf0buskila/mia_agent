#!/usr/bin/env bash
# Close loopback `trust` in the VM's PostgreSQL (V2_PG_CONTRACTS.md section 22, E21 and E28). Owner-operated.
#
#   sudo pg-hba-harden.sh --check      read-only: say what pg_hba.conf allows and whether --apply is safe
#   sudo pg-hba-harden.sh --apply      back up pg_hba.conf, trust -> scram-sha-256 on TCP lines, reload
#   sudo pg-hba-harden.sh --rollback   put the newest backup back and reload
#
# Why: the postgres image runs initdb with no --auth flags, so pg_hba.conf starts with
# `host all all 127.0.0.1/32 trust` and `::1/128 trust`, ahead of the scram line the image appends.
# The database and the app share the host network, so any process on the VM could open 127.0.0.1:5432
# as the superuser `mia` with no password, which defeats the V2-14a, V2-15 and V2-14b role separation.
#
# What it changes: every `host*` line (TCP: 127.0.0.1, ::1, and `replication` too) whose method is
# `trust` becomes `scram-sha-256`, keeping the rest of the line, the comments and the layout. `local`
# (unix socket) lines are NOT touched, deliberately: the socket exists only inside the postgres
# container, and `mia backup|restore`, app_role_demote.py, backup_offsite.py, provision_auth.py and
# the restore drill all reach PostgreSQL as `mia` through `docker compose exec` over it. Rewriting
# them would lock the operator out of the very tools that restore and demote. This is the rule E28
# records: E21 hardens the TCP (`host`) lines only, and the container-local socket stays `trust`.
#
# Safety, in this order:
#   1. every role a live DSN uses must have a SCRAM verifier, or nothing is changed. The fixed list is
#      mia, mia_app (when present), assaf_os_session, assaf_os_ledger and assaf_os_retention; the
#      users found in the DSN files are added. A role that is absent, or cannot log in, is
#      not a DSN role. `mia` must exist. A DSN with no password, or in a form this script cannot
#      parse (so cannot log in with afterwards), is refused too. Since V2-14b, V2-15 creates
#      assaf_os_retention as LOGIN with no password (E8), so after that provision it blocks --apply
#      until `mia os-provision retention` gives it a verifier: run this before the V2-15 provision
#      (FORK-PROD-ORDER), or after the retention one.
#      Which DSNs (E28): the keys listed in dsn_sources below (the app's and the jobs', including
#      MIA_OWNER_OS_READ_DATABASE_URL, MIA_OWNER_OS_ACTION_DATABASE_URL and the job-only
#      os-retention.env key), PLUS every key that looks like `*DATABASE_URL` in runtime.env,
#      settings.env, os-ledger-migrate.env and os-retention.env. An empty value is skipped. Each one
#      is scram-checked before the change and logged in with after the reload. --check prints the
#      KEY NAMES it would refuse (never a value), so a surprise key can be fixed before the window.
#   2. pg_hba.conf is copied (cmp-verified) to pg_hba.conf.pre-e21-<UTC time> next to it.
#   3. the new file must parse (pg_hba_file_rules shows no error and no TCP trust line), or the backup
#      is put back before any reload.
#   4. after the reload a wrong password must be REFUSED over TCP (proof the reload took effect) and
#      every readable live DSN must log in. If either fails the backup is restored and reloaded and the
#      script exits 1, so a bad password never leaves the app locked out.
#
# Idempotent: --apply on a hardened cluster changes nothing and makes no backup. Run it with the jobs
# paused, a verified backup and Assaf's go; check /health/ready after it (docs/runbooks/APP_ROLE_DEMOTE.md,
# step 1a). It takes /var/lock/mia-state.lock (apply, rollback), never starts, stops or deploys anything,
# and prints reason codes and booleans only: never a DSN, a password or the contents of pg_hba.conf.
#
# Exit codes: 0 done (check: hardened); 1 not hardened (check), or refused/rolled back (apply, rollback);
# 2 usage, or the database could not be read, or a precondition failed (nothing was changed).
set +x
set -euo pipefail
umask 077

# Knobs for the tests; sudo strips the environment, so they only matter to the caller who sets them.
etc_dir="${MIA_HBA_ETC_DIR:-/etc/mia}"
compose_file="${MIA_HBA_COMPOSE_FILE:-/opt/mia/compose.yaml}"
tag_env="${MIA_HBA_TAG_ENV:-/opt/mia/tag.env}"
state_lock="${MIA_HBA_STATE_LOCK:-/var/lock/mia-state.lock}"
euid="${MIA_HBA_EUID:-$EUID}"

# Roles whose DSNs the VM runs. Others found in its DSN files are added to this list.
fixed_roles=(mia mia_app assaf_os_session assaf_os_ledger assaf_os_retention)
# file (under $etc_dir) and key of every DSN a service of the VM reads (E28: the floor of the scan)
dsn_sources=(
  "runtime.env MIA_DATABASE_URL"
  "runtime.env MIA_OWNER_OS_LEDGER_DATABASE_URL"
  "os-ledger-migrate.env MIA_OS_LEDGER_MIGRATION_DATABASE_URL"
  "settings.env MIA_OWNER_OS_SESSION_DATABASE_URL"
  "settings.env MIA_OWNER_OS_READ_DATABASE_URL"
  "settings.env MIA_OWNER_OS_ACTION_DATABASE_URL"
  "os-retention.env MIA_OS_LEDGER_RETENTION_DATABASE_URL"
)
# Files in which EVERY `*DATABASE_URL` key is scanned too, so a DSN no list names cannot slip past.
dsn_scan_files=(runtime.env settings.env os-ledger-migrate.env os-retention.env)

usage() {
  echo "usage: pg-hba-harden.sh --check | --apply | --rollback" >&2
  exit 2
}

refuse() {
  echo "reason=$1 ok=false"
  exit "${2:-1}"
}

[ "$#" -eq 1 ] || usage
mode="$1"
case "$mode" in
  --check | --apply | --rollback) ;;
  *) usage ;;
esac
[ "$euid" = 0 ] || refuse hba_root_required 1

# A command in the postgres container as its own OS user, the door every VM tool uses.
pg() {
  docker compose -p mia -f "$compose_file" --env-file "$tag_env" exec -T -u postgres postgres "$@"
}

# SQL as the superuser mia over the container's unix socket (never TCP, never a password).
sql() {
  pg psql -X -qAt -v ON_ERROR_STOP=1 -U mia -d postgres -c "$1"
}

if [ "$mode" != --check ]; then
  exec 9>"$state_lock"
  flock -n 9 || refuse hba_operation_busy 1
fi

# --- what pg_hba.conf says now ----------------------------------------------------------------

hba_file="" trust_tcp="" trust_local="" rule_errors=""

read_hba() {
  hba_file="$(sql 'SHOW hba_file')" || refuse hba_database_unreachable 2
  [[ "$hba_file" =~ ^/[A-Za-z0-9_./-]+$ ]] || refuse hba_file_unusable 2
  local counts
  counts="$(sql "SELECT count(*) FILTER (WHERE auth_method = 'trust' AND type LIKE 'host%'), count(*) FILTER (WHERE auth_method = 'trust' AND type = 'local'), count(*) FILTER (WHERE error IS NOT NULL) FROM pg_hba_file_rules")" \
    || refuse hba_rules_unreadable 2
  IFS='|' read -r trust_tcp trust_local rule_errors <<<"$counts"
  [[ "$trust_tcp" =~ ^[0-9]+$ && "$trust_local" =~ ^[0-9]+$ && "$rule_errors" =~ ^[0-9]+$ ]] \
    || refuse hba_rules_unreadable 2
}

# --- the live DSNs ----------------------------------------------------------------------------

dsn_keys=() dsn_roles=() dsn_passwords=() dsn_ports=() dsn_databases=()
dsn_without_password=() dsn_unverified=()
roles_checked=() roles_without_scram=()

dsn_ok='^postgres(ql)?([+][a-z0-9]+)?://([a-z_][a-z0-9_]*):([^@:/[:space:]]+)@127\.0\.0\.1:([0-9]{2,5})/([a-z_][a-z0-9_]*)$'
dsn_bare='^postgres(ql)?([+][a-z0-9]+)?://([a-z_][a-z0-9_]*)@127\.0\.0\.1:([0-9]{2,5})/([a-z_][a-z0-9_]*)$'

dsn_targets=()

# One "file key" target, once: a key listed in dsn_sources and also found by the scan counts once.
add_dsn_target() {
  local candidate="$1" seen
  for seen in "${dsn_targets[@]+"${dsn_targets[@]}"}"; do
    if [ "$seen" = "$candidate" ]; then return 0; fi
  done
  dsn_targets+=("$candidate")
}

# Reads the DSN files for the role, password, port and database of each live DSN. Values are kept in
# shell variables only; they are never printed, and never an argument.
scan_dsns() {
  local entry file key path line value found
  dsn_keys=() dsn_roles=() dsn_passwords=() dsn_ports=() dsn_databases=()
  dsn_without_password=() dsn_unverified=()
  dsn_targets=()
  for entry in "${dsn_sources[@]}"; do
    add_dsn_target "$entry"
  done
  # E28: every `*DATABASE_URL` key of the scanned files, whatever its name (key names only, here).
  for file in "${dsn_scan_files[@]}"; do
    path="$etc_dir/$file"
    [ -r "$path" ] || continue
    found="$(grep -oE '^[A-Z0-9_]*DATABASE_URL=' "$path" || true)"
    while IFS= read -r key; do
      if [ -n "$key" ]; then add_dsn_target "$file ${key%=}"; fi
    done <<<"$found"
  done
  for entry in "${dsn_targets[@]}"; do
    file="${entry%% *}"
    key="${entry#* }"
    path="$etc_dir/$file"
    [ -r "$path" ] || continue
    line="$(grep -E "^${key}=" "$path" | head -n 1 || true)"
    [ -n "$line" ] || continue
    value="${line#*=}"
    case "$value" in
      \'*\' | \"*\") value="${value:1:${#value}-2}" ;;
    esac
    # An empty value is no DSN: nothing connects with it, so nothing can lock out.
    [ -n "$value" ] || continue
    if [[ "$value" =~ $dsn_ok ]]; then
      dsn_keys+=("$key")
      dsn_roles+=("${BASH_REMATCH[3]}")
      dsn_passwords+=("${BASH_REMATCH[4]}")
      dsn_ports+=("${BASH_REMATCH[5]}")
      dsn_databases+=("${BASH_REMATCH[6]}")
    elif [[ "$value" =~ $dsn_bare ]]; then
      dsn_without_password+=("$key")
    else
      dsn_unverified+=("$key")
    fi
  done
}

join() {
  local IFS=,
  if [ "$#" -eq 0 ]; then echo none; else echo "$*"; fi
}

# Every role a live DSN uses must hold a SCRAM verifier, or scram-sha-256 would lock it out.
check_roles() {
  local role listed row name login scram seen
  roles_checked=("${fixed_roles[@]}")
  for role in "${dsn_roles[@]}"; do
    seen=0
    for name in "${roles_checked[@]}"; do [ "$name" = "$role" ] && seen=1; done
    [ "$seen" = 1 ] || roles_checked+=("$role")
  done
  listed="$(printf "'%s'," "${roles_checked[@]}")"
  listed="${listed%,}"
  roles_without_scram=()
  local found=" " rows
  rows="$(sql "SELECT rolname, rolcanlogin, coalesce(rolpassword LIKE 'SCRAM-SHA-256\$%', false) FROM pg_authid WHERE rolname IN ($listed) ORDER BY rolname")" \
    || refuse hba_roles_unreadable 2
  while IFS='|' read -r name login scram; do
    [ -n "$name" ] || continue
    found+="$name "
    if [ "$login" = t ] && [ "$scram" != t ]; then
      roles_without_scram+=("$name")
    fi
  done <<<"$rows"
  case "$found" in *" mia "*) ;; *) roles_without_scram+=("mia") ;; esac
}

# --- logging in over TCP with a DSN's own password --------------------------------------------

# psql over 127.0.0.1 inside the container. The password travels on stdin only.
tcp_login() {
  local role="$1" password="$2" port="$3" database="$4"
  printf '%s\n' "$password" | pg sh -c \
    'IFS= read -r PGPASSWORD; export PGPASSWORD; exec psql -X -qAt -v ON_ERROR_STOP=1 -h 127.0.0.1 -p "$1" -U "$2" -d "$3" -c "SELECT 1"' \
    sh "$port" "$role" "$database" >/dev/null 2>&1
}

# A random password no role has: over TCP it can only work while the line is still `trust`.
wrong_password() {
  head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'
}

# --- the rewrite ------------------------------------------------------------------------------

# trust -> scram-sha-256 on host* lines only, keeping every other byte of the line. The method is
# field 5, or field 6 when the address is a bare IP followed by a netmask.
IFS= read -r -d '' awk_program <<'AWK' || true
function swap(line, count,    prefix, k) {
  prefix = ""
  for (k = 1; k < count; k++) {
    match(line, /^[[:blank:]]*[^[:blank:]]+/)
    prefix = prefix substr(line, 1, RLENGTH)
    line = substr(line, RLENGTH + 1)
  }
  k = index(line, "trust")
  return prefix substr(line, 1, k - 1) "scram-sha-256" substr(line, k + 5)
}
{
  n = 0
  for (i = 1; i <= NF; i++) {
    if (substr($i, 1, 1) == "#") break
    n = i
  }
  if (n >= 5 && ($1 == "host" || $1 == "hostssl" || $1 == "hostnossl" || $1 == "hostgssenc" || $1 == "hostnogssenc")) {
    m = 5
    if ($4 ~ /^[0-9]+[.][0-9]+[.][0-9]+[.][0-9]+$/ || $4 ~ /^[0-9a-fA-F:]*:[0-9a-fA-F:.]*$/) m = 6
    if (n >= m && $m == "trust") { print swap($0, m); next }
  }
  print
}
AWK

# Runs inside the container as the postgres user. The backup is made and compared first; the new text
# is built in a side file; only a complete, same-length result replaces pg_hba.conf, in place, so its
# owner, mode and inode stay.
IFS= read -r -d '' rewrite_script <<'SH' || true
set -e
hba="$1"; backup="$2"; program="$3"
cp -p "$hba" "$backup"
cmp -s "$hba" "$backup"
awk "$program" "$hba" > "$hba.e21-new"
[ -s "$hba.e21-new" ]
[ "$(awk 'END { print NR }' "$hba")" = "$(awk 'END { print NR }' "$hba.e21-new")" ]
cat "$hba.e21-new" > "$hba"
rm -f "$hba.e21-new"
SH

reload_conf() {
  [ "$(sql 'SELECT pg_reload_conf()')" = t ]
}

restore_backup() {
  pg sh -c 'cat "$1" > "$2"' sh "$1" "$hba_file"
  reload_conf || true
}

emit_state() {
  echo "trust_tcp_lines=$trust_tcp"
  echo "trust_local_lines=$trust_local"
  echo "hba_rule_errors=$rule_errors"
  if [ "$trust_tcp" = 0 ] && [ "$rule_errors" = 0 ]; then echo "hardened=true"; else echo "hardened=false"; fi
}

# --- the three modes --------------------------------------------------------------------------

read_hba
if [ "$mode" != --rollback ]; then
  scan_dsns
  check_roles
fi

case "$mode" in
  --check)
    emit_state
    echo "roles_checked=$(join "${roles_checked[@]}")"
    echo "roles_without_scram=$(join "${roles_without_scram[@]}")"
    # KEY NAMES only (E28): which DSNs --apply will log in with, and which it would refuse.
    echo "dsn_keys_checked=$(join "${dsn_keys[@]}")"
    echo "dsn_without_password=$(join "${dsn_without_password[@]}")"
    echo "dsn_unverified=$(join "${dsn_unverified[@]}")"
    if [ "${#roles_without_scram[@]}" -eq 0 ] && [ "${#dsn_without_password[@]}" -eq 0 ] \
      && [ "${#dsn_unverified[@]}" -eq 0 ]; then
      echo "apply_ready=true"
    else
      echo "apply_ready=false"
    fi
    [ "$trust_tcp" = 0 ] && [ "$rule_errors" = 0 ] && exit 0
    exit 1
    ;;

  --apply)
    if [ "$trust_tcp" = 0 ] && [ "$rule_errors" = 0 ]; then
      emit_state
      echo "applied=false"
      echo "reason=hba_already_hardened"
      exit 0
    fi
    if [ "${#roles_without_scram[@]}" -gt 0 ]; then
      echo "roles_without_scram=$(join "${roles_without_scram[@]}")"
      refuse hba_role_without_scram 2
    fi
    if [ "${#dsn_without_password[@]}" -gt 0 ]; then
      echo "dsn_without_password=$(join "${dsn_without_password[@]}")"
      refuse hba_dsn_without_password 2
    fi
    # A DSN in a form this script cannot parse (another host, localhost, a query string) is never
    # logged in with after the reload and its role is not in the scram check, so a password that was
    # never really checked under `trust` would fail afterwards with no rollback. Rewrite it first.
    if [ "${#dsn_unverified[@]}" -gt 0 ]; then
      echo "dsn_unverified=$(join "${dsn_unverified[@]}")"
      refuse hba_dsn_unverified 2
    fi
    [ "$rule_errors" = 0 ] || refuse hba_rules_invalid_before_change 2
    backup="$hba_file.pre-e21-$(date -u +%Y%m%dT%H%M%SZ)"
    pg sh -c "$rewrite_script" sh "$hba_file" "$backup" "$awk_program" || {
      # The backup is made before anything is written, so a failure before the last copy changed
      # nothing. If the file does differ from a backup that exists, put the backup back.
      if pg sh -c '[ -f "$1" ]' sh "$backup" && ! pg cmp -s "$backup" "$hba_file"; then
        restore_backup "$backup"
      fi
      refuse hba_rewrite_failed 1
    }
    read_hba
    if [ "$rule_errors" != 0 ] || [ "$trust_tcp" != 0 ]; then
      restore_backup "$backup"
      refuse hba_rewrite_invalid_restored 1
    fi
    reload_conf || { restore_backup "$backup"; refuse hba_reload_failed_restored 1; }

    # Proof the reload took effect: a wrong password must now be refused over TCP.
    probe_role="${dsn_roles[0]:-mia}"
    probe_port="${dsn_ports[0]:-5432}"
    probe_database="${dsn_databases[0]:-postgres}"
    refused=0
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      sleep 1
      if ! tcp_login "$probe_role" "$(wrong_password)" "$probe_port" "$probe_database"; then
        refused=1
        break
      fi
    done
    if [ "$refused" != 1 ]; then
      restore_backup "$backup"
      refuse hba_reload_not_effective_restored 1
    fi

    # Every readable live DSN must still log in with its own password.
    for index in "${!dsn_keys[@]}"; do
      if ! tcp_login "${dsn_roles[$index]}" "${dsn_passwords[$index]}" "${dsn_ports[$index]}" "${dsn_databases[$index]}"; then
        restore_backup "$backup"
        echo "key=${dsn_keys[$index]}"
        refuse hba_login_failed_rolled_back 1
      fi
    done
    read_hba
    emit_state
    echo "applied=true"
    echo "backup=$backup"
    echo "logins_verified=${#dsn_keys[@]}"
    echo "dsn_keys_checked=$(join "${dsn_keys[@]}")"
    echo "dsn_unverified=$(join "${dsn_unverified[@]}")"
    ;;

  --rollback)
    backup="$(pg sh -c 'ls -1 "$1".pre-e21-* 2>/dev/null | sort | tail -n 1' sh "$hba_file")" || backup=""
    backup="${backup//$'\r'/}"
    [ -n "$backup" ] || refuse hba_no_backup 1
    [[ "$backup" =~ ^/[A-Za-z0-9_./-]+\.pre-e21-[0-9]{8}T[0-9]{6}Z$ ]] || refuse hba_backup_unusable 1
    if pg cmp -s "$backup" "$hba_file"; then
      echo "rolled_back=false"
      echo "reason=hba_already_restored"
    else
      restore_backup "$backup"
      echo "rolled_back=true"
    fi
    read_hba
    emit_state
    echo "restored_from=$backup"
    [ "$rule_errors" = 0 ] || refuse hba_rules_invalid_after_rollback 1
    ;;
esac

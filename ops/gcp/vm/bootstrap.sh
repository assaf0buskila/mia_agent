#!/usr/bin/env bash
# VM startup script. Runs as root on every boot and is safe to repeat.
# Installs Docker, adds swap for image builds, creates Mia's directories and generates
# the database password, which never leaves this VM.
set -euo pipefail

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y docker.io docker-compose-v2 docker-buildx
  systemctl enable --now docker
fi

if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
swapon --show | grep -q /swapfile || swapon /swapfile

install -d -m 700 /etc/mia
install -d -m 755 /opt/mia /opt/mia/src /var/lib/mia
# The postgres image runs as uid 999 and must own its data directory.
install -d -m 700 -o 999 -g 999 /var/lib/mia/postgres
install -d -m 700 /var/lib/mia/caddy-data /var/lib/mia/caddy-config /var/lib/mia/backups

if [ ! -f /etc/mia/runtime.env ]; then
  db_password="$(openssl rand -hex 24)"
  umask 077
  printf 'POSTGRES_DB=mia\nPOSTGRES_USER=mia\nPOSTGRES_PASSWORD=%s\n' "$db_password" > /etc/mia/postgres.env
  printf 'MIA_DATABASE_URL=postgresql://mia:%s@127.0.0.1:5432/mia\n' "$db_password" > /etc/mia/runtime.env
fi

touch /var/lib/mia/.bootstrap-done

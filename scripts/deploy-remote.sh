#!/usr/bin/env bash
# Internal installer invoked by publish.sh on a Linux VPS.
set -euo pipefail
[[ $# == 8 ]] || { echo 'Run scripts/publish.sh from your checkout.' >&2; exit 2; }
upload=$1; base=$2; service=$3; port=$4; python=$5; configure=$6; origin=$7; preview=$8
[[ $upload =~ ^/tmp/prayer-publish\.[a-zA-Z0-9]+$ ]] || exit 2
[[ $base == /* ]] || base="$HOME/$base"
[[ $base =~ ^/[a-zA-Z0-9_/.-]+$ && /$base/ != *'/../'* ]] || exit 2
[[ $service =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]] || exit 2
[[ $port =~ ^[0-9]+$ && ${#port} -le 5 ]] && ((10#$port > 0 && 10#$port < 65536)) || exit 2
[[ $python =~ ^[a-zA-Z0-9_/.-]+$ && $python != -* ]] || exit 2
[[ $configure =~ ^[01]$ && $preview =~ ^[01]$ ]] || exit 2
activated=0
previous=
release=
cleanup() {
  status=$?
  trap - EXIT
  if ((status != 0 && activated)); then
    echo 'Deployment failed; restoring previous application release.' >&2
    if [[ -n $previous ]]; then
      ln -s "$previous" "$base/.rollback-$$"
      mv -Tf "$base/.rollback-$$" "$base/current"
      sudo systemctl restart "$service.service" || true
    else
      sudo systemctl stop "$service.service" || true
      rm -f "$base/current"
    fi
    echo 'Database migrations are not rolled back. Inspect the journal and database before retrying.' >&2
  fi
  rm -rf "$upload"
  exit "$status"
}
trap cleanup EXIT
[[ $(uname -s) == Linux ]] || { echo 'This installer requires Linux with systemd.' >&2; exit 1; }
if [[ -r /etc/os-release ]]; then cat /etc/os-release; fi
for dependency in "$python" curl tar systemctl sudo flock ss; do
  command -v "$dependency" >/dev/null || { echo "Missing prerequisite: $dependency. See docs/DEPLOYMENT.md." >&2; exit 1; }
done
"$python" -c 'import sys; assert sys.version_info >= (3,12), "Python 3.12+ required"'
[[ -d /run/systemd/system ]] || { echo 'systemd is not running.' >&2; exit 1; }
sudo -v
# Inspect listener ownership as root so a different user's process is visible.
listeners_for_port() { sudo ss -H -ltnp "sport = :$port"; }
listeners_owned_by() {
  local listeners=$1 pid=$2 line
  [[ $pid =~ ^[1-9][0-9]*$ && -n $listeners ]] || return 1
  while IFS= read -r line; do
    [[ $line == *"pid=$pid,"* ]] || return 1
  done <<<"$listeners"
}
listeners=$(listeners_for_port)
existing_pid=$(systemctl show "$service.service" --property=MainPID --value)
if [[ -n $listeners ]] && ! listeners_owned_by "$listeners" "$existing_pid"; then
  echo "Port $port is used by another process; choose a free --port. No activation attempted." >&2
  exit 1
fi
umask 077
mkdir -p "$base/releases" "$base/shared"
base=$(cd "$base" && pwd -P)
[[ $base =~ ^/[a-zA-Z0-9_/.-]+$ ]] || { echo 'Resolved application directory contains unsupported characters.' >&2; exit 1; }
exec 9>"$base/shared/deploy.lock"
flock -n 9 || { echo 'Another deployment is running.' >&2; exit 1; }
env_file="$base/shared/prayer-app.env"
if [[ ! -f $env_file ]]; then
  ((configure)) || { echo "Missing $env_file. Rerun with --configure and --origin https://YOUR_HOST, or --configure --preview." >&2; exit 1; }
  if ((preview)); then
    origin="http://localhost:$port"
    secure=false
  else
    [[ $origin =~ ^https://[a-zA-Z0-9.-]+(:[0-9]+)?$ ]] || { echo 'First production setup requires --origin https://YOUR_HOST.' >&2; exit 1; }
    secure=true
  fi
  printf 'PostgreSQL connection URL (input hidden): ' >/dev/tty
  IFS= read -r -s database_url </dev/tty
  printf '\n' >/dev/tty
  # Percent-encode password punctuation. This keeps the file compatible with shell and systemd.
  [[ $database_url =~ ^postgres(ql)?://[a-zA-Z0-9_.~:/?@%+=\&,-]+$ ]] || { echo 'Invalid URL. Percent-encode special password characters; no whitespace/quotes/backslashes.' >&2; exit 1; }
  printf "PRAYER_DATABASE_URL='%s'\nPRAYER_DATABASE_SCHEMA=public\nPRAYER_COOKIE_SECURE=%s\nPRAYER_TRUSTED_ORIGIN=%s\n" "$database_url" "$secure" "$origin" >"$env_file"
  unset database_url
elif ((configure)); then
  echo "Keeping existing $env_file; edit it on the server to change configuration."
fi
chmod 600 "$env_file"
# This is a trusted, administrator-owned configuration file, never uploaded source.
set -a
source "$env_file"
set +a
[[ ${PRAYER_DATABASE_URL:-} == postgres://* || ${PRAYER_DATABASE_URL:-} == postgresql://* ]] || { echo 'PRAYER_DATABASE_URL must select PostgreSQL.' >&2; exit 1; }
[[ -n ${PRAYER_TRUSTED_ORIGIN:-} ]] || { echo 'Set PRAYER_TRUSTED_ORIGIN in the environment file.' >&2; exit 1; }
release="$base/releases/$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir "$release"
tar -xzf "$upload/source.tar.gz" -C "$release"
"$python" -m venv "$release/.venv"
"$release/.venv/bin/python" -m pip install --disable-pip-version-check -r "$release/backend/requirements.txt"
unit="$upload/$service.service"
cat >"$unit" <<EOF
# Managed by prayer-app scripts/publish.sh
[Unit]
Description=Prayer app
After=network.target

[Service]
Type=simple
User=$(id -un)
Group=$(id -gn)
WorkingDirectory=$base/current
EnvironmentFile=$env_file
ExecStart=$base/current/.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port $port --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
target="/etc/systemd/system/$service.service"
if sudo test -e "$target"; then
  sudo cmp -s "$unit" "$target" || { echo "Existing $target differs. Review it manually or choose another --service; it will not be overwritten." >&2; exit 1; }
else
  # Refuse a vendor unit with the same name too.
  [[ -z $(systemctl show "$service.service" --property=FragmentPath --value) ]] || { echo 'A service with this name already exists; choose --service.' >&2; exit 1; }
  sudo install -m 644 "$unit" "$target"
  sudo systemctl daemon-reload
fi
if [[ -e $base/current && ! -L $base/current ]]; then echo 'current must be a symlink; refusing to replace it.' >&2; exit 1; fi
if [[ -L $base/current ]]; then previous=$(readlink "$base/current"); fi
(cd "$release" && .venv/bin/python -m backend.database migrate)
ln -s "$release" "$base/.activate-$$"
mv -Tf "$base/.activate-$$" "$base/current"
activated=1
sudo systemctl restart "$service.service"
healthy=0
for attempt in {1..30}; do
  pid=$(systemctl show "$service.service" --property=MainPID --value)
  if systemctl is-active --quiet "$service.service" &&
     [[ $pid =~ ^[1-9][0-9]*$ && $(readlink "/proc/$pid/cwd") == "$release" ]] &&
     listeners_owned_by "$(listeners_for_port)" "$pid" &&
     response=$(curl --fail --silent --max-time 2 "http://127.0.0.1:$port/api/v1/health") &&
     printf '%s' "$response" | "$release/.venv/bin/python" -c 'import json,sys; assert json.load(sys.stdin).get("ok") is True'; then
    healthy=1; break
  fi
  sleep 1
done
((healthy)) || { echo "Health check failed. Inspect: sudo journalctl -u $service.service -n 100" >&2; exit 1; }
sudo systemctl enable "$service.service"
activated=0
printf 'Published %s\nEnvironment: %s\nPrevious release: %s\n' "$release" "$env_file" "${previous:-none}"

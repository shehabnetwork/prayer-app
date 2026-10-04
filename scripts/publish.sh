#!/usr/bin/env bash
# Upload only application source, then run the interactive installer over SSH.
set -euo pipefail
usage() {
  cat <<'EOF'
Usage: scripts/publish.sh [options]
  --host USER@HOST   SSH destination (administrator@mafisco.com)
  --dir PATH         Remote directory relative to home, or absolute (www/prayer-app)
  --service NAME     Dedicated systemd service name (prayer-app)
  --port NUMBER      Loopback application port (8000)
  --python COMMAND   Remote Python 3.12+ executable (python3)
  --configure        Prompt on the server to create its missing environment file
  --origin ORIGIN    HTTPS public origin for initial configuration
  --nginx-host HOST  Configure a dedicated Nginx HTTPS host using Certbot webroot
  --preview         Explicitly configure HTTP localhost for SSH tunnel preview
  --dry-run         Show the plan without opening an SSH connection
  --help            Show this help
Existing server configuration is preserved. See docs/DEPLOYMENT.md.
EOF
}
host=administrator@mafisco.com
remote_dir=www/prayer-app
service=prayer-app
port=8000
python=python3
configure=0
preview=0
origin=
nginx_host=
dry_run=0
while (($#)); do
  case "$1" in
    --host|--dir|--service|--port|--python|--origin|--nginx-host)
      [[ $# -ge 2 ]] || { echo "Missing value for $1" >&2; exit 2; }
      case "$1" in
        --host) host=$2;; --dir) remote_dir=$2;; --service) service=$2;;
        --port) port=$2;; --python) python=$2;; --origin) origin=$2;; --nginx-host) nginx_host=$2;;
      esac
      shift 2;;
    --configure) configure=1; shift;;
    --preview) preview=1; shift;;
    --dry-run) dry_run=1; shift;;
    --help|-h) usage; exit 0;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2;;
  esac
done
[[ $host =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.@:-]*$ ]] || { echo 'Invalid SSH destination' >&2; exit 2; }
[[ $remote_dir =~ ^[a-zA-Z0-9_/.-]+$ && /$remote_dir/ != *'/../'* ]] || { echo 'Invalid remote directory; use a path without spaces or ..' >&2; exit 2; }
[[ $service =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]] || { echo 'Invalid service name' >&2; exit 2; }
[[ $port =~ ^[0-9]+$ && ${#port} -le 5 ]] && ((10#$port > 0 && 10#$port < 65536)) || { echo 'Invalid port' >&2; exit 2; }
[[ $python =~ ^[a-zA-Z0-9_/.-]+$ && $python != -* ]] || { echo 'Invalid Python command' >&2; exit 2; }
[[ -z $origin || $origin =~ ^https://[a-zA-Z0-9.-]+(:[0-9]+)?$ ]] || { echo '--origin must be an HTTPS origin without a path' >&2; exit 2; }
if [[ -n $nginx_host ]]; then
  [[ $nginx_host =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z0-9-]+$ && $nginx_host != *..* ]] || { echo 'Invalid Nginx hostname' >&2; exit 2; }
  [[ $preview == 0 ]] || { echo '--nginx-host requires production configuration; omit --preview.' >&2; exit 2; }
  [[ -z $origin || $origin == "https://$nginx_host" ]] || { echo '--origin must match --nginx-host.' >&2; exit 2; }
  origin="https://$nginx_host"
fi
[[ $preview == 0 || -z $origin ]] || { echo 'Use either --preview or --origin' >&2; exit 2; }
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
printf 'Publish %s to %s:%s; service %s on 127.0.0.1:%s\n' "$root" "$host" "$remote_dir" "$service" "$port"
if ((dry_run)); then
  echo 'Package backend Python/migrations/requirements and frontend; upload; create venv; migrate; activate; health-check. SSH and sudo may prompt.'
  [[ -z $nginx_host ]] || printf 'Then configure dedicated Nginx host %s and obtain HTTPS certificate with interactive Certbot webroot.\n' "$nginx_host"
  exit 0
fi
for command in ssh scp tar python3; do command -v "$command" >/dev/null || { echo "Missing local command: $command" >&2; exit 1; }; done
temp=$(mktemp -d)
trap 'rm -rf "$temp"' EXIT
# Build a bounded allowlist. Never dereference symlinks or package local secrets/data.
python3 - "$root" "$temp/source.tar.gz" <<'PY'
import pathlib, sys, tarfile
root = pathlib.Path(sys.argv[1])
files = list((root / 'backend').glob('*.py'))
files += list((root / 'backend/migrations').glob('*.sql'))
files += [root / 'backend/requirements.txt']
files += [p for p in (root / 'frontend').rglob('*') if p.is_file()]
with tarfile.open(sys.argv[2], 'w:gz') as archive:
    for path in sorted(files):
        relative = path.relative_to(root)
        if any(part.startswith('.') or part in ('__pycache__', 'node_modules', 'data') for part in relative.parts):
            continue
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root):
            raise SystemExit(f'Refusing source symlink: {relative}')
        archive.add(path, arcname=str(relative), recursive=False)
PY
# OpenSSH owns password prompts and host-key verification; no password is stored.
remote_temp=$(ssh "$host" 'umask 077; mktemp -d /tmp/prayer-publish.XXXXXXXX')
[[ $remote_temp =~ ^/tmp/prayer-publish\.[a-zA-Z0-9]+$ ]] || { echo 'Unexpected remote temporary path' >&2; exit 1; }
scp "$temp/source.tar.gz" "$root/scripts/deploy-remote.sh" "$host:$remote_temp/"
printf -v command 'bash %q %q %q %q %q %q %q %q %q' "$remote_temp/deploy-remote.sh" "$remote_temp" "$remote_dir" "$service" "$port" "$python" "$configure" "$origin" "$preview"
ssh -t "$host" "$command"
if [[ -n $nginx_host ]]; then
  remote_temp=$(ssh "$host" 'umask 077; mktemp -d /tmp/prayer-publish.XXXXXXXX')
  [[ $remote_temp =~ ^/tmp/prayer-publish\.[a-zA-Z0-9]+$ ]] || { echo 'Unexpected remote temporary path' >&2; exit 1; }
  scp "$root/scripts/configure-nginx-remote.sh" "$host:$remote_temp/"
  printf -v command 'bash %q %q %q %q %q %q %q' "$remote_temp/configure-nginx-remote.sh" "$remote_temp" "$remote_dir" "$service" "$port" "$nginx_host" "$python"
  ssh -t "$host" "$command"
fi

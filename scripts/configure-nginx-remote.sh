#!/usr/bin/env bash
# Optional dedicated virtual host. Certbot never edits existing Nginx sites.
set -euo pipefail
[[ $# == 6 ]] || { echo 'Run scripts/publish.sh --nginx-host HOST.' >&2; exit 2; }
upload=$1; base=$2; service=$3; port=$4; hostname=$5; python=$6
[[ $upload =~ ^/tmp/prayer-publish\.[a-zA-Z0-9]+$ ]] || exit 2
[[ $base == /* ]] || base="$HOME/$base"
[[ $base =~ ^/[a-zA-Z0-9_/.-]+$ && /$base/ != *'/../'* ]] || exit 2
[[ $service =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]] || exit 2
[[ $port =~ ^[0-9]+$ && ${#port} -le 5 ]] && ((10#$port > 0 && 10#$port < 65536)) || exit 2
[[ $hostname =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z0-9-]+$ && $hostname != *..* ]] || exit 2
[[ $python =~ ^[a-zA-Z0-9_/.-]+$ && $python != -* ]] || exit 2
changed=0
had_previous=0
config="/etc/nginx/conf.d/prayer-app-$service.conf"
cleanup() {
  status=$?
  trap - EXIT
  if ((status != 0 && changed)); then
    echo 'HTTPS setup failed; restoring the previous dedicated Nginx configuration.' >&2
    if ((had_previous)); then
      sudo install -m 644 "$upload/previous.conf" "$config" || true
    else
      sudo rm -f "$config" || true
    fi
    if sudo nginx -t; then sudo systemctl reload nginx || true; fi
    echo 'The deployed application remains available on loopback. Any issued certificate is retained.' >&2
  fi
  rm -rf "$upload"
  exit "$status"
}
trap cleanup EXIT
for dependency in sudo nginx certbot systemctl flock curl "$python"; do
  command -v "$dependency" >/dev/null || { echo "Missing prerequisite: $dependency. Install it using your distribution's supported packages, then rerun with --nginx-host." >&2; exit 1; }
done
[[ -d /etc/nginx/conf.d ]] || { echo 'This setup expects /etc/nginx/conf.d; configure Nginx manually for this distribution.' >&2; exit 1; }
base=$(cd "$base" && pwd -P)
[[ $base =~ ^/[a-zA-Z0-9_/.-]+$ ]] || { echo 'Resolved application directory contains unsupported characters.' >&2; exit 1; }
umask 077
exec 9>"$base/shared/deploy.lock"
flock -n 9 || { echo 'Another deployment is running.' >&2; exit 1; }
set -a
source "$base/shared/prayer-app.env"
set +a
[[ ${PRAYER_TRUSTED_ORIGIN:-} == "https://$hostname" && ${PRAYER_COOKIE_SECURE:-} == true ]] || {
  echo "Set PRAYER_TRUSTED_ORIGIN=https://$hostname and PRAYER_COOKIE_SECURE=true in shared/prayer-app.env, restart the application, and retry. Preview settings cannot be exposed publicly." >&2
  exit 1
}
sudo -v
sudo nginx -t
marker="# Managed by prayer-app publish: $service $hostname"
if sudo test -e "$config" || sudo test -L "$config"; then
  sudo test ! -L "$config" || { echo "Refusing existing symlink: $config" >&2; exit 1; }
  [[ $(sudo head -n 1 "$config") == "$marker" ]] || { echo "Refusing unrelated configuration: $config" >&2; exit 1; }
  sudo cp "$config" "$upload/previous.conf"
  sudo chown "$(id -u):$(id -g)" "$upload/previous.conf"
  had_previous=1
fi
# Inspect other loaded files before making changes. Regex hosts require manual
# review because Nginx's PCRE matching cannot safely be inferred here.
sudo nginx -T >"$upload/nginx-expanded.conf" 2>"$upload/nginx-expanded.err"
"$python" - "$hostname" "$config" "$upload/nginx-expanded.conf" <<'PY'
import pathlib, shlex, sys
host, own_file, expanded_file = sys.argv[1:]
current_file = None
lines = []
for line in pathlib.Path(expanded_file).read_text().splitlines():
    if line.startswith('# configuration file ') and line.endswith(':'):
        current_file = line[len('# configuration file '):-1]
    if current_file != own_file:
        lines.append(line)
lexer = shlex.shlex('\n'.join(lines), posix=True, punctuation_chars=';{}')
lexer.whitespace_split = True
lexer.commenters = '#'
try:
    tokens = []
    for token in lexer:
        tokens.extend(token if token and set(token) <= set(';{}') else [token])
except ValueError:
    raise SystemExit('Cannot safely parse existing Nginx hosts; configure manually.')
for index, token in enumerate(tokens):
    if token != 'server_name' or (index and tokens[index - 1] not in (';', '{', '}')):
        continue
    for name in tokens[index + 1:]:
        if name == ';':
            break
        collision = (name == host or
                     (name.startswith('*.') and host.endswith(name[1:])) or
                     (name.startswith('.') and (host == name[1:] or host.endswith(name))) or
                     (name.endswith('.*') and host.startswith(name[:-1])))
        if name.startswith('~') or collision:
            raise SystemExit('Another Nginx host overlaps this hostname or uses regex matching; review manually.')
PY
# Install a dedicated reload hook, refusing unrelated contents.
hook="/etc/letsencrypt/renewal-hooks/deploy/prayer-app-$service"
cat >"$upload/renewal-hook" <<'EOF'
#!/bin/sh
# Managed by prayer-app publish: certificate reload
nginx -t && systemctl reload nginx
EOF
if sudo test -e "$hook" || sudo test -L "$hook"; then
  sudo test ! -L "$hook" && sudo cmp -s "$upload/renewal-hook" "$hook" || { echo "Refusing unrelated renewal hook: $hook" >&2; exit 1; }
fi
# This public challenge directory contains no app source or credentials.
# Its path is outside the deploy user's private home directory for Nginx access.
webroot="/var/lib/prayer-app-acme/$service"
sudo install -d -m 755 /var/lib/prayer-app-acme "$webroot"
cat >"$upload/http.conf" <<EOF
$marker
server {
    listen 80;
    server_name $hostname;
    location ^~ /.well-known/acme-challenge/ {
        root $webroot;
    }
    location / { return 503; }
}
EOF
# Bootstrap serves certificate challenges only; secure-cookie app traffic stays private.
if ((had_previous == 0)); then
  changed=1
  sudo install -m 644 "$upload/http.conf" "$config"
fi
sudo nginx -t
# nginx -T may contain other sites' sensitive settings; inspect privately and never print it.
sudo nginx -T >"$upload/nginx-expanded.conf" 2>"$upload/nginx-expanded.err"
if ! grep -Fq "# configuration file $config:" "$upload/nginx-expanded.conf"; then
  echo '/etc/nginx/conf.d is not loaded by this Nginx installation. Configure this distribution manually.' >&2
  exit 1
fi
if ((had_previous == 0)); then sudo systemctl reload nginx; fi
echo "Certbot will ask for email and agreement as needed for $hostname."
sudo certbot certonly --webroot --webroot-path "$webroot" --cert-name "$hostname" -d "$hostname"
sudo test -f "/etc/letsencrypt/live/$hostname/fullchain.pem"
sudo test -f "/etc/letsencrypt/live/$hostname/privkey.pem"
cat >"$upload/https.conf" <<EOF
$marker
server {
    listen 80;
    server_name $hostname;
    location ^~ /.well-known/acme-challenge/ {
        root $webroot;
    }
    location / { return 301 https://$hostname\$request_uri; }
}
server {
    listen 443 ssl;
    server_name $hostname;
    ssl_certificate /etc/letsencrypt/live/$hostname/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$hostname/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    location / {
        proxy_pass http://127.0.0.1:$port;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    }
}
EOF
changed=1
sudo install -m 644 "$upload/https.conf" "$config"
sudo nginx -t
sudo systemctl reload nginx
# Reload completion does not guarantee new workers are serving TLS yet.
# Keep responses and errors private; parse health JSON only after curl succeeds.
https_ready=0
for attempt in {1..15}; do
  if curl --fail --silent --show-error --max-time 3 --resolve "$hostname:443:127.0.0.1" \
      "https://$hostname/api/v1/health" >"$upload/https-health.json" 2>"$upload/https-health.err"; then
    if "$python" - "$upload/https-health.json" 2>"$upload/https-health.err" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as response:
        health = json.load(response)
except (ValueError, UnicodeError):
    raise SystemExit('HTTPS health response is not valid JSON.')
if not isinstance(health, dict) or health.get('ok') is not True:
    raise SystemExit('HTTPS health response does not report ok=true.')
PY
    then
      https_ready=1
      break
    fi
  fi
  if ((attempt < 15)); then sleep 1; fi
done
if ((https_ready == 0)); then
  echo "HTTPS readiness failed after 15 attempts for $hostname. Final check error:" >&2
  cat "$upload/https-health.err" >&2
  # nginx -T can contain secrets; only print listener and host directives.
  if sudo nginx -T >"$upload/nginx-expanded.conf" 2>"$upload/nginx-expanded.err"; then
    "$python" - "$upload/nginx-expanded.conf" <<'PY' >&2
import pathlib, shlex, sys
lexer = shlex.shlex(pathlib.Path(sys.argv[1]).read_text(), posix=True, punctuation_chars=';{}')
lexer.whitespace_split = True
lexer.commenters = '#'
try:
    tokens = []
    for token in lexer:
        tokens.extend(token if token and set(token) <= set(';{}') else [token])
except ValueError:
    raise SystemExit('Cannot parse Nginx listener diagnostics; review listen/server_name directives manually.')
print('Loaded Nginx listen/server_name directives (up to 100):')
count = 0
for index, token in enumerate(tokens):
    if token not in ('listen', 'server_name') or (index and tokens[index - 1] not in (';', '{', '}')):
        continue
    values = []
    for value in tokens[index + 1:]:
        if value in (';', '{', '}'):
            break
        values.append(value)
    print(shlex.join([token, *values])[:512] + ';')
    count += 1
    if count == 100:
        break
PY
  fi
  echo 'Review address-specific 443 listeners and server_name selection; see docs/DEPLOYMENT.md.' >&2
  exit 1
fi
sudo install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy
sudo install -m 755 "$upload/renewal-hook" "$hook"
changed=0
echo "HTTPS configured: https://$hostname"
echo 'Nginx reload renewal hook installed. Verify Certbot renewal scheduling; see docs/DEPLOYMENT.md.'

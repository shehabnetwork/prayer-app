# Publish to a Linux VPS without Docker

Run `scripts/publish.sh` from this checkout. It connects to
`administrator@mafisco.com` and installs under `~/www/prayer-app` by default.
`mafisco.com` is only the SSH destination; it is **not** the app's public domain.
The chosen public hostname is `praytracker.freedynamicdns.org`. DNS remains
managed through its provider. Without `--nginx-host`, publishing changes only
the application. The optional flag configures a dedicated Nginx host and obtains
a certificate without editing unrelated websites.

The frontend needs no build. Each upload contains backend Python source, SQL
migrations, pinned requirements, and frontend assets. Local `.env` files,
databases, caches, tests, and Git metadata are excluded. Source symlinks are
refused. SSH and sudo passwords use terminal prompts and are never stored.
OpenSSH's normal host-key verification remains enabled. Multiple SSH/scp steps
can prompt more than once; an SSH key may be used instead.

## Server prerequisites

The server needs Linux with systemd, Python **3.12+** with its venv module, tar,
curl, flock (usually util-linux), ss (usually iproute2), sudo, and a running native PostgreSQL server.
The deploy user needs permission to install/manage a dedicated systemd service
and write its application directory. Nginx is needed when exposing the app
publicly. The installer prints `/etc/os-release` and checks prerequisites; package
installation is manual because the distribution is not yet known. If `python3`
is older, install an appropriate interpreter and pass `--python python3.12`.

On the VPS, create a dedicated database role and database using PostgreSQL's
administrative account. Enter SQL interactively so the password stays out of
shell history:

```sh
sudo -u postgres psql
```

```sql
CREATE ROLE prayer_app LOGIN;
\password prayer_app
CREATE DATABASE prayer_app OWNER prayer_app;
\q
```

Use the VPS's actual PostgreSQL port and authentication configuration. The
connection URL will generally be
`postgresql://prayer_app:ENCODED_PASSWORD@127.0.0.1:5432/prayer_app`.
Percent-encode special characters in the password before placing it in the URL.
The setup prompt rejects shell-sensitive characters; do not put credentials in
command-line arguments or commit them. PostgreSQL needs no public listening port.

## Initial preview before choosing DNS

From your local terminal:

```sh
scripts/publish.sh --dry-run
scripts/publish.sh --configure --preview
```

Enter the PostgreSQL URL at the hidden remote prompt. Preview explicitly uses
`PRAYER_TRUSTED_ORIGIN=http://localhost:8000` and `PRAYER_COOKIE_SECURE=false`.
The service binds only to `127.0.0.1:8000` on the VPS. In a separate terminal:

```sh
ssh -N -L 8000:127.0.0.1:8000 administrator@mafisco.com
```

Visit `http://localhost:8000` through that tunnel. Use the exact `localhost`
origin, since cookie mutation requests validate it. For `--port 8080`, use 8080
on both sides of the tunnel and in the local URL. This HTTP configuration is
for the tunnel preview; configure HTTPS before exposing a public endpoint.

## Production at praytracker.freedynamicdns.org

The selected hostname's A record was checked on October 4, 2026 and matched the
SSH server address, `38.247.141.227`. Recheck DNS if it changes:

```sh
dig +short praytracker.freedynamicdns.org A
dig +short mafisco.com A
```

The VPS must have Nginx and Certbot installed, with public inbound ports 80 and
443 available. The script does not install packages or change firewall rules.
Any IPv6 DNS record must also reach this VPS. Run from your local checkout:

```sh
scripts/publish.sh --configure --nginx-host praytracker.freedynamicdns.org
```

The flag derives `--origin https://praytracker.freedynamicdns.org`, rejects
`--preview` or a conflicting explicit origin, and asks for the PostgreSQL URL
remotely on first setup. After app deployment succeeds, the Nginx installer
verifies production origin and secure cookies, refuses conflicting host files,
and tests a dedicated challenge-only HTTP configuration for first setup. It runs
interactive `certbot certonly --webroot`; respond to email/agreement prompts.
It then tests and reloads the HTTPS proxy and HTTP redirect, and verifies the
health response through loopback HTTPS with certificate validation. The readiness
check makes up to 15 attempts, with a one-second delay between attempts and a
three-second timeout per request, allowing time for new Nginx workers to serve
the reloaded configuration. Certbot does
not edit Nginx sites. An existing managed HTTPS host stays active during renewal.

The dedicated file is `/etc/nginx/conf.d/prayer-app-prayer-app.conf`.
Distributions that do not load `conf.d` are refused for manual setup, as are
overlapping existing hosts or regex hosts that cannot safely be checked.
Certificate challenges live in `/var/lib/prayer-app-acme/prayer-app`, with no
source or secrets. On HTTPS setup failure, changed Nginx configuration is restored;
the issued certificate is retained, and the deployed app remains on loopback.

If the final readiness error reports a certificate hostname mismatch, issuance
may have succeeded while the loopback request received another host's certificate.
The script prints the final curl error and only the loaded `listen` and
`server_name` directives before restoring its dedicated configuration. A reload
transition is one possible cause; a persistent mismatch can also indicate an
existing address-specific listener such as `listen 127.0.0.1:443 ssl` that takes
precedence over the dedicated wildcard listener. Review the relevant existing
server blocks and certificate paths privately on the VPS. Resolve listener/host
selection deliberately, test with `sudo nginx -t`, and rerun
`scripts/publish.sh --nginx-host praytracker.freedynamicdns.org`. The installer
preserves other virtual hosts and retains TLS verification throughout the check.
An invalid or unhealthy JSON response is reported separately without a Python
traceback.

A dedicated root-owned executable renewal hook is installed under
`/etc/letsencrypt/renewal-hooks/deploy/prayer-app-prayer-app`. It tests and reloads
Nginx when certificates renew; unrelated existing hook contents are refused.
Verify that your distribution schedules Certbot renewal (for example its
systemd timer), and run `sudo certbot renew --dry-run` after setup.

## Application configuration without automatic Nginx setup

To deploy the app while configuring Nginx separately, use:

```sh
scripts/publish.sh --configure --origin https://praytracker.freedynamicdns.org
```

Initial setup saves `shared/prayer-app.env` with mode `600`, outside all releases.
It contains `PRAYER_DATABASE_URL`, `PRAYER_DATABASE_SCHEMA=public`,
`PRAYER_COOKIE_SECURE=true`, and the exact `PRAYER_TRUSTED_ORIGIN`. Existing
configuration is always retained, including when `--configure` is repeated.

To convert an existing preview, edit the file on the
VPS (do not upload a local environment file):

```sh
nano ~/www/prayer-app/shared/prayer-app.env
chmod 600 ~/www/prayer-app/shared/prayer-app.env
sudo systemctl restart prayer-app.service
```

Set `PRAYER_COOKIE_SECURE=true` and
`PRAYER_TRUSTED_ORIGIN=https://praytracker.freedynamicdns.org`. Restart the app,
then rerun `scripts/publish.sh --nginx-host praytracker.freedynamicdns.org` for
HTTPS setup. Keep
one `KEY=value` per line; quote the database URL with single quotes. The file must
be compatible with both shell sourcing and systemd `EnvironmentFile`; avoid
shell expansions or commands. It is trusted administrator-owned configuration.

## Manual Nginx alternative

Point the chosen hostname at the VPS. Confirm the server address and provider
instructions independently. Place a
new dedicated Nginx configuration in the distribution's usual location, replacing
`YOUR_CHOSEN_HOST` below. Do not overwrite existing hosts or make this a default
server. This is an HTTP template for initial routing/certificate setup:

```nginx
server {
    listen 80;
    server_name YOUR_CHOSEN_HOST;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

Run `sudo nginx -t` before `sudo systemctl reload nginx`. Install a certificate
for the selected hostname using your distribution's supported certificate tool,
configure the corresponding HTTPS server, and redirect HTTP to HTTPS. Do not use
the app publicly until HTTPS and secure cookies are configured. Uvicorn trusts
forwarded headers only from loopback; keep its port private. This manual route
is an alternative to `--nginx-host`.

## Subsequent publishing and recovery

```sh
scripts/publish.sh
# Optional settings; keep service/port/directory consistent after first install:
scripts/publish.sh --host administrator@mafisco.com --dir www/prayer-app \
  --service prayer-app --port 8000 --python python3.12
```

The installer prevents concurrent deployment, creates a new release and its own
venv, installs requirements, migrates PostgreSQL, atomically changes the `current`
symlink, and restarts a single-worker Uvicorn service. It checks
`/api/v1/health` for JSON `ok: true`, verifies that the service process runs from
the new release and owns the port, then enables the service at boot. A port owned
by another process is refused before activation. The
single worker matches the current in-memory authentication rate limiter.
Existing releases and the environment file are preserved. An existing systemd
unit must match the generated configuration exactly; a different unit is refused
for manual review or use of another `--service` name.

If activation or health verification fails, the script restores the prior code
symlink and restarts it, or stops a failed first deployment. **Database migrations
are not automatically reversed**; code rollback depends on compatibility with
the migrated schema. Back up PostgreSQL before upgrades and retain a tested
database recovery procedure. The script does not transfer local SQLite data;
see [POSTGRESQL-MIGRATION.md](POSTGRESQL-MIGRATION.md) for deliberate import.

Useful VPS diagnostics:

```sh
sudo systemctl status prayer-app.service
sudo journalctl -u prayer-app.service -n 100
curl --fail http://127.0.0.1:8000/api/v1/health
readlink ~/www/prayer-app/current
ls ~/www/prayer-app/releases
```

For manual code rollback, select an existing **absolute** release directory,
create a temporary symlink beside `current`, atomically replace `current`, and
restart the service. Verify health afterward and check database compatibility
first. Release cleanup is manual; never delete `current`'s target, the desired
rollback release, or `shared/`. Failed setup can leave a release for diagnosis.
An interrupted upload may leave a `/tmp/prayer-publish.*` directory; remove only
the known abandoned upload. Automatic setup is not claimed to have run until an
actual VPS deployment and health check succeed.

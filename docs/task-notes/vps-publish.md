# VPS publishing

Objective: provide repeatable SSH publishing to `administrator@mafisco.com`,
under `~/www/prayer-app`, using native PostgreSQL and no Docker.

Acceptance: source upload excludes credentials/local databases; Linux systemd
keeps Uvicorn running; migrations precede activation; health check verifies the
service; SSH passwords remain terminal prompts. Document prerequisites, first
setup and subsequent deployments. See `../repo-map.md` for application flow.

Relevant entries: `backend.main:app`, `backend/requirements.txt`,
`python -m backend.database migrate`, `/api/v1/health`.

Nginx and sudo are available; distribution is unknown. Public hostname is
`praytracker.freedynamicdns.org`; both it and the SSH hostname `mafisco.com`
resolved to `38.247.141.227` during this task. Initial tunnel preview is supported.

Implemented `scripts/publish.sh`, `scripts/deploy-remote.sh`, optional
`scripts/configure-nginx-remote.sh`, and `docs/DEPLOYMENT.md`; affected
repository-map deployment section is updated. `--nginx-host` configures a
dedicated host, interactive Certbot webroot issuance, HTTPS verification and a
renewal reload hook; repeat setup preserves the existing HTTPS configuration.
Validation: Bash syntax, help, dry-run, whitespace, extracted-code mocks for
PostgreSQL URLs with query separators and port ownership. Review fixed quoted
environment values, unrelated listener rejection, and physical-path comparison.
Nginx extracted-code checks cover initial/repeat setup, multiline/inline and
wildcard collisions, and conflicting CLI flags. Bounded Astra review passed.
No remote connection or actual systemd deployment has been performed. Database
migrations require independent backup/recovery; automatic rollback restores code.

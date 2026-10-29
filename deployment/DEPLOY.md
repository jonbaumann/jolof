# Deploying with `jolof-deploy`

An optional, opinionated way to run Jolof on one Linux host with Docker. If
you'd rather use `docker compose` or your own tooling, skip this; the
[README](../README.md) covers what any deployment needs.

One script, run as root, does the whole thing:

```bash
jolof-deploy --check    # pre-flight only, changes nothing
jolof-deploy            # full deploy
jolof-deploy --rollback # back to the previous image
```

It assumes:

- Postgres runs in its own container (`DB_CONTAINER`, default `postgres_jolof`)
  on a Docker network (`DOCKER_NETWORK`, default `jolof_net`).
- TLS is handled in front of the app by your own reverse proxy. The app
  container listens on `127.0.0.1:8000` and joins the Docker network as `jolof`,
  so a proxy container on the same network can reach it at `http://jolof:8000`.
  The script never configures or reloads the proxy.

## Install (once)

From a checkout of the repo, as root:

```bash
./deployment/install_deploy.sh
```

This creates `/etc/jolof/jolof.env` (from `.env.example`, mode 0600) and
`/etc/jolof/deploy.conf`, and installs the script as `/usr/local/sbin/jolof-deploy`.
Fill both files in, then run `jolof-deploy --check`.

In `deploy.conf`, at minimum set:

- `REPO_URL`: an `https://` URL for a public repo, or `git@github.com:…` with a
  `DEPLOY_KEY` for a private one.
- `HEALTH_HOST_HEADER`: one of the hostnames in `ALLOWED_HOSTS`.
- `NGINX_CONTAINER` (optional): your proxy's container, to have pre-flight check
  it's on the network and the deploy finish with an HTTPS check through it.

### Layout

```
/etc/jolof/
  deploy.conf     deploy settings (sourced by bash; quoting is fine here)
  jolof.env       app env, fed to docker --env-file (NO quotes; see below)
  known_hosts     pinned GitHub host keys (ssh repos only)
/opt/jolof/
  releases/<timestamp>/   clone + build context, last 3 kept
  current -> releases/…   symlink to the deployed release
  backups/                pre-migrate database dumps
/var/log/jolof-deploy.log
```

## jolof.env has no quotes

`docker --env-file` passes values through literally, quotes included, so
`AWS_SECRET_ACCESS_KEY="abc"` reaches the app as `"abc"` and S3 rejects it.
Write every value bare. Pre-flight treats quotes, CRLF line endings, `export`
and malformed lines as hard errors.

## Pre-flight checks

`--check` runs all of these and changes nothing:

- running as root; `docker`, `git`, `ssh`, `gzip` present; docker daemon responding
- `REPO_URL` and `HEALTH_HOST_HEADER` set
- `jolof.env` exists, is 0600, is docker-safe, and every required key is non-empty
- for ssh repos: the deploy key is readable, GitHub's host key is pinned and
  verified against its published fingerprint, and the key authenticates
- the ref exists on the remote
- the Docker network exists (offers to create it); the database container is
  running, on that network, and `pg_isready`
- the proxy container, if set, is running and on that network
- host port 8000 isn't held by another container
- at least 2GB free on `/var/lib/docker`

## What a deploy does

1. clone the ref into `releases/<timestamp>/`
2. build `jolof-latest:<sha>` and `:latest` from the repo's `Dockerfile`
3. tag whatever is currently running as `jolof-latest:previous`
4. `pg_dump | gzip` into `backups/`; a failed dump aborts the deploy
5. `migrate`, then `collectstatic` (no `--clear`; it trips a django-storages bug)
6. stop and remove the old container, start the new one
7. health check `127.0.0.1:8000` for up to 30s, with the right `Host:` header
8. on failure: show the container log and offer a rollback to `:previous`
9. if a proxy is set, check the site end to end through it (warns only)
10. prune old releases and dangling images, repoint `current`

The old container keeps serving through steps 1–5, so downtime is just the
container swap.

## Rollback

```bash
jolof-deploy --rollback
```

Restarts the container from `jolof-latest:previous`. **It does not revert
migrations.** If the failed deploy migrated, restore the dump it took first:

```bash
gunzip -c /opt/jolof/backups/pre-deploy-<timestamp>-<sha>.sql.gz \
  | docker exec -i postgres_jolof psql -U <DB_USER> -d <DB_NAME>
```

(Restore into an empty database: drop and recreate it first.)

## Private repos and passphrase-protected keys

For an ssh `REPO_URL`, the script uses `DEPLOY_KEY`. Root can read the key
wherever it lives. A read-only GitHub Deploy Key without a passphrase is the
simplest choice:

```bash
install -d -m 700 /etc/jolof/ssh
ssh-keygen -t ed25519 -N '' -C jolof-deploy -f /etc/jolof/ssh/github_deploy_key
# add the .pub to the repo's Settings → Deploy keys (read-only)
```

If the key has a passphrase, the script starts a private `ssh-agent`, prompts
once, and kills the agent when it exits. To deploy several times without
retyping it, load the key into your own agent first; the script reuses it and
never kills an agent it didn't start:

```bash
eval $(ssh-agent -s)
ssh-add /path/to/key
jolof-deploy && jolof-deploy --ref some-branch
```

`--yes` answers the script's own prompts, not the passphrase.

## Other flags

| flag | effect |
|---|---|
| `--ref <branch\|tag\|sha>` | deploy something other than `main` |
| `--self-install` | install the current release's copy of the script, if newer |
| `--no-backup` | skip the pre-migrate dump |
| `--no-collectstatic` | skip collectstatic |
| `-y`, `--yes` | non-interactive; assume yes at every prompt |

## The script updates itself

After cloning, it compares `/usr/local/sbin/jolof-deploy` with the release's
`deployment/deploy_jolof.sh`. If the release's `SCRIPT_VERSION` is newer, it
offers to install it, and the new copy runs from the next deploy on. It never
downgrades. `--self-install` installs it later from `/opt/jolof/current`.
**Bump `SCRIPT_VERSION` on every edit**, or the update is skipped.

## Overriding the Dockerfile

Set `DOCKERFILE` (and optionally `DOCKERIGNORE`) in `deploy.conf` to build with
your own files instead of the repo's; they're copied into each release before
the build.

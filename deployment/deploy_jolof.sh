#!/usr/bin/env bash
#
# deploy_jolof.sh — single-shot deployment for the jolof Django app, on one
# Docker host with Postgres in a container. An optional example of one way to
# run Jolof; see deployment/DEPLOY.md.
#
# Run as root:  ./deploy_jolof.sh
#
# Config lives in /etc/jolof/ (see deploy.conf.example). Nothing is read from
# the current working directory, so this is safe to run from anywhere.
#
set -Eeuo pipefail

SCRIPT_VERSION="1.8.0"   # bump when editing; printed in the banner

# ---------------------------------------------------------------------------
# Defaults (override any of these in /etc/jolof/deploy.conf)
# ---------------------------------------------------------------------------
CONFIG_DIR="${JOLOF_CONFIG_DIR:-/etc/jolof}"
CONFIG_FILE="$CONFIG_DIR/deploy.conf"

REPO_URL=""                     # required: set in deploy.conf
GIT_REF="main"
DEPLOY_KEY="/home/jolof/.ssh/github-jolof.priv"
KNOWN_HOSTS="$CONFIG_DIR/known_hosts"

APP_ENV_FILE="$CONFIG_DIR/jolof.env"
# Empty means build with the Dockerfile and .dockerignore in the repo. Point
# these at your own files to override them.
DOCKERFILE=""
DOCKERIGNORE=""

BASE_DIR="/opt/jolof"
RELEASES_DIR="$BASE_DIR/releases"
BACKUP_DIR="$BASE_DIR/backups"
STATE_DIR="$BASE_DIR/state"
KEEP_RELEASES=3

IMAGE_NAME="jolof-latest"
CONTAINER_NAME="jolof-latest"
DOCKER_NETWORK="jolof_net"
NETWORK_ALIAS="jolof"
HOST_BIND="127.0.0.1:8000:8000"
RUN_AS="1000:1000"

DB_CONTAINER="postgres_jolof"
# Optional: the reverse proxy container that terminates TLS in front of the app.
# When set, pre-flight checks it's on DOCKER_NETWORK and the deploy finishes
# with an end-to-end HTTPS check through it. The script never configures or
# reloads it. Leave empty if TLS is handled some other way.
NGINX_CONTAINER=""

HEALTH_URL="http://127.0.0.1:8000/"
HEALTH_HOST_HEADER=""           # required: a hostname in ALLOWED_HOSTS
HEALTH_RETRIES=30
HEALTH_LAST_CODE=""
# End-to-end probe through nginx. An nginx that resolves the app with
# `resolver valid=30s` can 502 for up to 30s after the swap while the old
# container IP is still cached -- retry past that before warning.
EDGE_RETRIES=45
PREVIOUS_IMAGE=""
KEY_ENCRYPTED=0
AGENT_STARTED=0
AGENT_PUBKEY=""

REQUIRED_ENV_KEYS=(SECRET_KEY DJANGO_SETTINGS_MODULE ALLOWED_HOSTS DB_NAME DB_USER DB_PASSWORD DB_HOST DB_PORT
                   AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_S3_REGION_NAME
                   AWS_STATIC_BUCKET_NAME AWS_MEDIA_BUCKET_NAME)

MIN_FREE_MB=2048
# Where the installed copy lives. Each release carries the repo's copy at
# deployment/deploy_jolof.sh; offer_self_update() installs it here when newer,
# since a stale copy here once caused a long, confusing debug loop.
INSTALL_PATH="/usr/local/sbin/jolof-deploy"
SELF_PATH="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"
LOG_FILE="/var/log/jolof-deploy.log"

# GitHub's published ed25519 host key fingerprint. Used to verify a freshly
# scanned known_hosts entry so first-run isn't blind trust-on-first-use.
GITHUB_ED25519_FP="SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"

# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------
DRY_RUN=0
ASSUME_YES=0
DO_SELF_INSTALL=0
DO_BACKUP=1
DO_COLLECTSTATIC=1
DO_ROLLBACK=0
REF_OVERRIDE=""

usage() {
    cat <<'USAGE'
Usage: deploy_jolof.sh [options]

  --ref <branch|tag|sha>  Deploy a specific git ref (default: from deploy.conf)
  --check                 Run pre-flight checks only, change nothing
  --rollback              Restart the previous image; no clone, build, or migrate
  --self-install          Install the deployed release's copy of this script, if newer
  --no-backup             Skip the pre-migrate database dump
  --no-collectstatic      Skip collectstatic
  -y, --yes               Non-interactive; assume yes at every prompt
  -h, --help              This message
      --version           Print the script version and exit
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ref)             REF_OVERRIDE="$2"; shift 2 ;;
        --check|--dry-run) DRY_RUN=1; shift ;;
        --rollback)        DO_ROLLBACK=1; shift ;;
        --self-install)    DO_SELF_INSTALL=1; shift ;;
        --no-backup)       DO_BACKUP=0; shift ;;
        --no-collectstatic) DO_COLLECTSTATIC=0; shift ;;
        -y|--yes)          ASSUME_YES=1; shift ;;
        -h|--help)         usage; exit 0 ;;
        --version)         echo "deploy_jolof.sh $SCRIPT_VERSION"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
    C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'
    C_BLU=$'\033[34m'; C_BLD=$'\033[1m';  C_RST=$'\033[0m'
else
    C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_BLD=""; C_RST=""
fi

WARN_COUNT=0
step()  { echo; echo "${C_BLU}${C_BLD}==> $*${C_RST}"; }
ok()    { echo "  ${C_GRN}ok${C_RST}   $*"; }
warn()  { WARN_COUNT=$((WARN_COUNT + 1)); echo "  ${C_YEL}warn${C_RST} $*"; }
info()  { echo "       $*"; }
die()   { echo; echo "${C_RED}${C_BLD}FAILED:${C_RST} $*" >&2; exit 1; }

confirm() {
    [[ $ASSUME_YES -eq 1 ]] && return 0
    [[ -t 0 || -e /dev/tty ]] || die "non-interactive shell and no --yes: $1"
    local reply
    # Straight to the tty: stdout is piped through tee, which buffers a prompt
    # that has no trailing newline and makes the script look hung.
    printf '  %s [y/N] ' "$1" > /dev/tty
    read -r reply < /dev/tty
    [[ "$reply" == "y" || "$reply" == "Y" ]]
}

# ---------------------------------------------------------------------------
# Load config
# ---------------------------------------------------------------------------
if [[ -f "$CONFIG_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$CONFIG_FILE"
fi
[[ -n "$REF_OVERRIDE" ]] && GIT_REF="$REF_OVERRIDE"

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
RELEASE_DIR="$RELEASES_DIR/$TIMESTAMP"

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
exec > >(tee -a "$LOG_FILE") 2>&1

echo "${C_BLD}jolof deploy v$SCRIPT_VERSION${C_RST}  $(date '+%Y-%m-%d %H:%M:%S %Z')  ref=$GIT_REF  host=$(hostname)"

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
env_get() {
    # Read a single key out of the app env file, unquoted, first match wins.
    grep -E "^${1}=" "$APP_ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2- || true
}

container_exists() { docker container inspect "$1" >/dev/null 2>&1; }
container_running() { [[ "$(docker container inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" == "true" ]]; }
image_exists()     { docker image inspect "$1" >/dev/null 2>&1; }

###############################################################################
# PRE-FLIGHT
###############################################################################
preflight() {
    step "Pre-flight checks"

    # --- identity -----------------------------------------------------------
    [[ $EUID -eq 0 ]] || die "must run as root (needed to read $DEPLOY_KEY and drive docker)"
    ok "running as root"

    # --- required commands --------------------------------------------------
    local missing=()
    for cmd in docker git ssh ssh-keygen gzip; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    [[ ${#missing[@]} -eq 0 ]] || die "missing required commands: ${missing[*]}"
    ok "required commands present"

    command -v curl >/dev/null 2>&1 || warn "curl not installed — post-deploy health check will be skipped"

    # --- docker daemon ------------------------------------------------------
    docker info >/dev/null 2>&1 || die "docker daemon is not responding (try: systemctl start docker)"
    ok "docker daemon responding"

    # --- config files -------------------------------------------------------
    [[ -f "$CONFIG_FILE" ]] || warn "no $CONFIG_FILE — using built-in defaults"
    [[ -f "$APP_ENV_FILE" ]] || die "app env file not found: $APP_ENV_FILE"
    ok "app env file: $APP_ENV_FILE"

    local perms
    perms="$(stat -c '%a' "$APP_ENV_FILE")"
    if [[ "$perms" != "600" && "$perms" != "400" ]]; then
        warn "$APP_ENV_FILE is mode $perms and holds secrets — recommend: chmod 600"
    else
        ok "app env file permissions ($perms)"
    fi

    validate_env_file

    # --- required settings --------------------------------------------------
    [[ -n "$REPO_URL" ]] || die "REPO_URL is not set — add it to $CONFIG_FILE"
    [[ -n "$HEALTH_HOST_HEADER" ]] || die "HEALTH_HOST_HEADER is not set — add it to $CONFIG_FILE (a host in ALLOWED_HOSTS)"

    # --- Dockerfile ---------------------------------------------------------
    if [[ -n "$DOCKERFILE" ]]; then
        [[ -f "$DOCKERFILE" ]] || die "Dockerfile not found: $DOCKERFILE"
        ok "Dockerfile override: $DOCKERFILE"
    else
        ok "Dockerfile: the repo's own"
    fi
    if [[ -n "$DOCKERIGNORE" ]]; then
        [[ -f "$DOCKERIGNORE" ]] || die ".dockerignore not found: $DOCKERIGNORE"
        ok ".dockerignore override: $DOCKERIGNORE"
    fi

    # --- ssh deploy key -----------------------------------------------------
    # Only for ssh URLs. A public repo cloned over https needs no key.
    if [[ "$REPO_URL" == https://* ]]; then
        ok "https repo — no deploy key needed"
    else
        [[ -f "$DEPLOY_KEY" ]] || die "deploy key not found: $DEPLOY_KEY"
        [[ -r "$DEPLOY_KEY" ]] || die "deploy key not readable: $DEPLOY_KEY"
        # -P "" and </dev/null together: without them ssh-keygen PROMPTS for a
        # passphrase on a tty, and a human typing it makes an encrypted key look
        # unencrypted to this check.
        if ssh-keygen -y -P "" -f "$DEPLOY_KEY" >/dev/null 2>&1 </dev/null; then
            KEY_ENCRYPTED=0
            ok "deploy key is unencrypted and usable: $DEPLOY_KEY"
        elif ssh-keygen -l -f "$DEPLOY_KEY" >/dev/null 2>&1 </dev/null; then
            KEY_ENCRYPTED=1
            ok "deploy key is passphrase-protected: $DEPLOY_KEY"
        else
            die "deploy key $DEPLOY_KEY is not a readable private key"
        fi

        ensure_known_hosts
        setup_git_ssh

        # --- github reachability ------------------------------------------------
        local ssh_out
        # `ssh -T git@github.com` always exits 1; we care about the greeting text.
        ssh_out="$(ssh -T -o BatchMode=yes -o ConnectTimeout=10 git@github.com 2>&1 || true)"
        if grep -q "successfully authenticated" <<<"$ssh_out"; then
            ok "github ssh auth: ${ssh_out%%$'\n'*}"
        else
            die "github ssh auth failed with $DEPLOY_KEY:
           $ssh_out"
        fi
    fi

    git ls-remote --exit-code --heads --tags "$REPO_URL" >/dev/null 2>&1 \
        || die "cannot list refs on $REPO_URL"
    ok "repo reachable: $REPO_URL"

    if ! git ls-remote --exit-code "$REPO_URL" "$GIT_REF" >/dev/null 2>&1; then
        # Could still be a raw commit sha, which ls-remote won't match.
        if [[ "$GIT_REF" =~ ^[0-9a-f]{7,40}$ ]]; then
            warn "ref '$GIT_REF' looks like a commit sha — will resolve after clone"
        else
            die "ref '$GIT_REF' does not exist on the remote"
        fi
    else
        ok "ref exists on remote: $GIT_REF"
    fi

    # --- docker network -----------------------------------------------------
    if docker network inspect "$DOCKER_NETWORK" >/dev/null 2>&1; then
        ok "docker network exists: $DOCKER_NETWORK"
    else
        warn "docker network '$DOCKER_NETWORK' does not exist"
        if [[ $DRY_RUN -eq 0 ]] && confirm "Create network '$DOCKER_NETWORK'?"; then
            docker network create "$DOCKER_NETWORK" >/dev/null
            ok "created network $DOCKER_NETWORK"
        else
            die "network '$DOCKER_NETWORK' is required (migrate and collectstatic resolve the DB through it)"
        fi
    fi

    # --- database -----------------------------------------------------------
    local db_host; db_host="$(env_get DB_HOST)"
    if container_running "$DB_CONTAINER"; then
        ok "database container running: $DB_CONTAINER"
        if docker network inspect "$DOCKER_NETWORK" -f '{{range .Containers}}{{.Name}} {{end}}' \
             | grep -qw "$DB_CONTAINER"; then
            ok "$DB_CONTAINER is attached to $DOCKER_NETWORK"
        else
            warn "$DB_CONTAINER is NOT attached to $DOCKER_NETWORK — migrate will fail to resolve '$db_host'"
        fi
        if docker exec "$DB_CONTAINER" pg_isready -U "$(env_get DB_USER)" >/dev/null 2>&1; then
            ok "postgres accepting connections"
        else
            warn "pg_isready reports postgres not ready inside $DB_CONTAINER"
        fi
    else
        warn "database container '$DB_CONTAINER' is not running — migrate will fail unless DB_HOST=$db_host points elsewhere"
    fi

    # --- nginx --------------------------------------------------------------
    if [[ -z "$NGINX_CONTAINER" ]]; then
        info "no NGINX_CONTAINER set — skipping the proxy checks"
    elif container_running "$NGINX_CONTAINER"; then
        ok "nginx container running: $NGINX_CONTAINER"
        if docker network inspect "$DOCKER_NETWORK" -f '{{range .Containers}}{{.Name}} {{end}}' \
             | grep -qw "$NGINX_CONTAINER"; then
            ok "$NGINX_CONTAINER is attached to $DOCKER_NETWORK"
        else
            warn "$NGINX_CONTAINER is NOT attached to $DOCKER_NETWORK — it won't be able to reach '$NETWORK_ALIAS'"
        fi
    else
        warn "nginx container '$NGINX_CONTAINER' is not running — the site will be unreachable from outside"
    fi

    # --- port conflict ------------------------------------------------------
    local bind_port="${HOST_BIND##*:}"
    local occupant
    occupant="$(docker ps --format '{{.Names}} {{.Ports}}' | grep ":${bind_port}->" | awk '{print $1}' | grep -v "^${CONTAINER_NAME}$" || true)"
    if [[ -n "$occupant" ]]; then
        die "port $bind_port is already published by container(s): $occupant"
    fi
    ok "host port $bind_port is free (or held by the container being replaced)"

    # --- disk ---------------------------------------------------------------
    local free_mb
    free_mb="$(df -Pm /var/lib/docker 2>/dev/null | awk 'NR==2 {print $4}')"
    if [[ -n "$free_mb" && "$free_mb" -lt "$MIN_FREE_MB" ]]; then
        warn "only ${free_mb}MB free on /var/lib/docker (want >= ${MIN_FREE_MB}MB); consider: docker image prune -f"
    else
        ok "disk space on /var/lib/docker: ${free_mb}MB free"
    fi

    # --- current state ------------------------------------------------------
    if container_exists "$CONTAINER_NAME"; then
        info "current container: $CONTAINER_NAME ($(docker container inspect -f '{{.State.Status}}' "$CONTAINER_NAME"))"
    else
        info "no existing '$CONTAINER_NAME' container — this is a fresh deploy"
    fi
}

validate_env_file() {
    # Docker's --env-file is a dumb parser: it does not strip quotes, does not
    # understand `export`, and chokes on CRLF. Every one of these has bitten
    # this deployment before, so they are hard errors.
    local problems=()
    local lineno=0 line

    if grep -q $'\r' "$APP_ENV_FILE"; then
        problems+=("file has CRLF line endings (run: sed -i 's/\r\$//' $APP_ENV_FILE)")
    fi

    while IFS= read -r line || [[ -n "$line" ]]; do
        lineno=$((lineno + 1))
        [[ -z "${line//[[:space:]]/}" ]] && continue
        [[ "$line" =~ ^[[:space:]]*# ]] && continue

        if [[ "$line" =~ ^[[:space:]]*export[[:space:]] ]]; then
            problems+=("line $lineno: starts with 'export' — docker --env-file does not understand it")
            continue
        fi
        if [[ ! "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
            problems+=("line $lineno: not a KEY=value assignment: ${line:0:40}")
            continue
        fi

        local value="${line#*=}"
        if [[ "$value" =~ ^\".*\"$ || "$value" =~ ^\'.*\'$ ]]; then
            problems+=("line $lineno: value is quoted — docker passes the quotes through literally: ${line%%=*}")
        fi
        if [[ "${line%%=*}" =~ [[:space:]] ]]; then
            problems+=("line $lineno: whitespace in key name: ${line%%=*}")
        fi
    done < "$APP_ENV_FILE"

    if [[ ${#problems[@]} -gt 0 ]]; then
        printf '  %s%s%s %s\n' "$C_RED" "bad" "$C_RST" "${problems[0]}"
        local i
        for ((i = 1; i < ${#problems[@]}; i++)); do printf '       %s\n' "${problems[$i]}"; done
        die "$APP_ENV_FILE is not safe for 'docker --env-file'"
    fi
    ok "app env file syntax is docker-safe (unquoted, LF, no export)"

    local absent=()
    for key in "${REQUIRED_ENV_KEYS[@]}"; do
        grep -qE "^${key}=." "$APP_ENV_FILE" || absent+=("$key")
    done
    if [[ ${#absent[@]} -gt 0 ]]; then
        die "missing or empty required keys in $APP_ENV_FILE: ${absent[*]}"
    fi
    ok "all ${#REQUIRED_ENV_KEYS[@]} required env keys present"
}

ensure_known_hosts() {
    # Prefer host keys the jolof user already trusts; otherwise scan and verify
    # the ed25519 fingerprint against GitHub's published value.
    if [[ -s "$KNOWN_HOSTS" ]] && grep -q "github.com" "$KNOWN_HOSTS"; then
        ok "github host keys pinned: $KNOWN_HOSTS"
        return
    fi

    mkdir -p "$(dirname "$KNOWN_HOSTS")"

    local src
    for src in "$(dirname "$DEPLOY_KEY")/known_hosts" /root/.ssh/known_hosts; do
        if [[ -f "$src" ]] && grep -q "github.com" "$src"; then
            grep "github.com" "$src" >> "$KNOWN_HOSTS"
            chmod 644 "$KNOWN_HOSTS"
            ok "github host keys imported from $src → $KNOWN_HOSTS"
            return
        fi
    done

    info "no pinned github host keys; scanning and verifying fingerprint..."
    local scanned; scanned="$(ssh-keyscan -t rsa,ecdsa,ed25519 github.com 2>/dev/null || true)"
    [[ -n "$scanned" ]] || die "ssh-keyscan github.com returned nothing (network or DNS problem?)"

    local fp
    fp="$(ssh-keygen -lf /dev/stdin <<<"$scanned" 2>/dev/null | awk '$4=="(ED25519)" {print $2}')"
    if [[ "$fp" != "$GITHUB_ED25519_FP" ]]; then
        die "scanned github ed25519 fingerprint does not match the published one.
       scanned:   ${fp:-<none>}
       published: $GITHUB_ED25519_FP
       Refusing to trust these host keys. Verify against
       https://docs.github.com/authentication/keeping-your-account-secure/githubs-ssh-key-fingerprints"
    fi
    printf '%s\n' "$scanned" > "$KNOWN_HOSTS"
    chmod 644 "$KNOWN_HOSTS"
    ok "github host keys scanned and fingerprint verified → $KNOWN_HOSTS"
}

setup_git_ssh() {
    # The old process switched to the 'jolof' user only because the key lives in
    # that user's home. Root can read it regardless of mode, so no su is needed.
    # The passphrase is the real constraint, handled by load_key_into_agent().
    export GIT_TERMINAL_PROMPT=0

    if [[ $KEY_ENCRYPTED -eq 0 ]]; then
        # IdentitiesOnly + IdentityAgent=none stop ssh from offering root's own
        # keys, or an inherited agent's, before ours.
        export GIT_SSH_COMMAND="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes -o IdentityAgent=none -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KNOWN_HOSTS -o BatchMode=yes"
        return
    fi

    load_key_into_agent
    # -i pointing at a PUBLIC key is the documented way to say "use exactly this
    # identity, the agent holds its private half". Keeps IdentitiesOnly honest
    # without needing the decrypted key on disk.
    export GIT_SSH_COMMAND="ssh -i $AGENT_PUBKEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KNOWN_HOSTS -o BatchMode=yes"
}

load_key_into_agent() {
    local fp
    fp="$(ssh-keygen -lf "$DEPLOY_KEY" 2>/dev/null | awk '{print $2}')"

    if [[ -n "${SSH_AUTH_SOCK:-}" ]] && ssh-add -l 2>/dev/null | grep -qF "$fp"; then
        ok "key already loaded in the inherited ssh-agent — no passphrase needed"
    else
        [[ -e /dev/tty ]] || die "deploy key needs a passphrase but there is no terminal to prompt on.
       For unattended runs, pre-load the key into an agent and export SSH_AUTH_SOCK:
         eval \$(ssh-agent -s) && ssh-add $DEPLOY_KEY"

        eval "$(ssh-agent -s)" >/dev/null
        AGENT_STARTED=1

        echo "  ${C_BLD}The deploy key is passphrase-protected.${C_RST}"
        # ssh-add prompts on /dev/tty directly, which is what we want: stdout
        # here is piped through tee and would buffer the prompt.
        ssh-add "$DEPLOY_KEY" </dev/tty \
            || die "ssh-add failed — wrong passphrase, or the key was rejected"
        ok "key loaded into a private ssh-agent for this run"
    fi

    # Write out the public half so ssh can name this exact identity. The .pub
    # suffix matters: that is how ssh recognises the file as a public key and
    # goes to the agent for the private half instead of trying to parse it.
    local old_umask; old_umask="$(umask)"
    umask 077
    AGENT_PUBKEY="$(mktemp --suffix=.pub /tmp/jolof-deploy-XXXXXX)"
    umask "$old_umask"

    if [[ -f "$DEPLOY_KEY.pub" ]]; then
        cat "$DEPLOY_KEY.pub" > "$AGENT_PUBKEY"
    else
        # No .pub alongside the key, so pick our identity out of the agent by
        # fingerprint. Matters when an inherited agent holds several keys.
        local line lfp
        while IFS= read -r line; do
            [[ -n "$line" ]] || continue
            lfp="$(ssh-keygen -lf /dev/stdin <<<"$line" 2>/dev/null | awk '{print $2}')"
            if [[ "$lfp" == "$fp" ]]; then
                printf '%s\n' "$line" > "$AGENT_PUBKEY"
                break
            fi
        done < <(ssh-add -L)
    fi

    [[ -s "$AGENT_PUBKEY" ]] || die "could not identify the deploy key ($fp) among the agent's loaded keys"
    ok "identity pinned for git: $(awk '{print $1}' "$AGENT_PUBKEY") $fp"
}

cleanup_ssh() {
    [[ -n "$AGENT_PUBKEY" ]] && rm -f "$AGENT_PUBKEY"
    # Only kill an agent this script started; never one the user handed us.
    [[ $AGENT_STARTED -eq 1 ]] && ssh-agent -k >/dev/null 2>&1
    return 0
}
trap cleanup_ssh EXIT

###############################################################################
# DEPLOY STEPS
###############################################################################
clone_repo() {
    step "Cloning $REPO_URL ($GIT_REF)"
    mkdir -p "$RELEASES_DIR"
    if [[ "$GIT_REF" =~ ^[0-9a-f]{7,40}$ ]] && ! git ls-remote --exit-code "$REPO_URL" "$GIT_REF" >/dev/null 2>&1; then
        # A raw commit sha: --branch won't take it, so clone then check out.
        git clone "$REPO_URL" "$RELEASE_DIR" 2>&1 | sed 's/^/       /'
        git -C "$RELEASE_DIR" checkout --quiet "$GIT_REF" \
            || die "commit $GIT_REF not found in $REPO_URL"
    else
        git clone --depth 1 --branch "$GIT_REF" "$REPO_URL" "$RELEASE_DIR" 2>&1 | sed 's/^/       /'
    fi

    GIT_SHA="$(git -C "$RELEASE_DIR" rev-parse --short HEAD)"
    GIT_SUBJECT="$(git -C "$RELEASE_DIR" log -1 --pretty=%s)"
    ok "cloned to $RELEASE_DIR"
    info "commit $GIT_SHA — $GIT_SUBJECT"

    if [[ -n "$DOCKERFILE" ]]; then
        cp "$DOCKERFILE" "$RELEASE_DIR/Dockerfile"
        ok "Dockerfile override staged into the release"
    fi
    if [[ -n "$DOCKERIGNORE" ]]; then
        cp "$DOCKERIGNORE" "$RELEASE_DIR/.dockerignore"
        ok ".dockerignore override staged into the release"
    fi
    [[ -f "$RELEASE_DIR/Dockerfile" ]] || die "no Dockerfile in the release — set DOCKERFILE in $CONFIG_FILE"
}

build_image() {
    step "Building image"
    # Tag by sha as well so rollback targets are identifiable in `docker images`.
    docker build -t "$IMAGE_NAME:$GIT_SHA" -t "$IMAGE_NAME:latest" "$RELEASE_DIR" 2>&1 | sed 's/^/       /'
    ok "built $IMAGE_NAME:$GIT_SHA (also tagged :latest)"
}

tag_previous() {
    # Record what is running now so --rollback has a target. Runs BEFORE the
    # build, while :latest still points at the old image.
    PREVIOUS_IMAGE=""
    if ! container_exists "$CONTAINER_NAME"; then
        info "nothing running; no :previous tag to record"
        return 0
    fi

    # The container's own image ID is the only authoritative answer. Names are
    # mutable: an aborted earlier deploy can leave :latest pointing at the NEW
    # image while the old container still runs, and resolving the name then
    # silently tags the wrong thing as :previous. So resolve candidate names,
    # but only accept one that still maps to the running image's ID.
    local running_id ref resolved_id
    running_id="$(docker container inspect -f '{{.Image}}' "$CONTAINER_NAME" 2>/dev/null)"

    for ref in "$(docker container inspect -f '{{.Config.Image}}' "$CONTAINER_NAME" 2>/dev/null)" \
               "$running_id"; do
        [[ -n "$ref" ]] || continue
        resolved_id="$(docker image inspect -f '{{.Id}}' "$ref" 2>/dev/null || true)"
        [[ -n "$resolved_id" ]] || continue
        if [[ "$resolved_id" != "$running_id" ]]; then
            warn "'$ref' now resolves to a different image than $CONTAINER_NAME is running — ignoring it"
            continue
        fi
        if docker tag "$ref" "$IMAGE_NAME:previous" 2>/dev/null; then
            PREVIOUS_IMAGE="$ref"
            ok "tagged the running image ($ref) as $IMAGE_NAME:previous"
            return 0
        fi
    done

    # Not fatal: the pre-migrate dump is the real safety net, and from this
    # deploy on, the state file below gives rollback an immutable sha tag.
    warn "could not tag the running image as $IMAGE_NAME:previous"
    info "the database backup below is unaffected, and $CONTAINER_NAME keeps running until the swap"
    if [[ $ASSUME_YES -eq 0 ]]; then
        confirm "Continue without a :previous tag?" || die "aborted before any change was made"
    fi
    return 0
}

record_state() {
    # Immutable anchor for rollback: every build is tagged $IMAGE_NAME:<gitsha>,
    # and a sha tag never moves. Written only after a successful health check.
    mkdir -p "$STATE_DIR"
    if [[ -f "$STATE_DIR/deployed" ]]; then
        cp "$STATE_DIR/deployed" "$STATE_DIR/previous"
    fi
    printf '%s\n' "$GIT_SHA" > "$STATE_DIR/deployed"
}

backup_database() {
    [[ $DO_BACKUP -eq 1 ]] || { info "database backup skipped (--no-backup)"; return; }
    if ! container_running "$DB_CONTAINER"; then
        warn "skipping backup — $DB_CONTAINER is not running"
        return
    fi
    step "Backing up the database before migrate"
    mkdir -p "$BACKUP_DIR"
    local out="$BACKUP_DIR/pre-deploy-${TIMESTAMP}-${GIT_SHA:-unknown}.sql.gz"
    if docker exec "$DB_CONTAINER" pg_dump -U "$(env_get DB_USER)" "$(env_get DB_NAME)" \
         | gzip > "$out"; then
        ok "backup written: $out ($(du -h "$out" | cut -f1))"
    else
        rm -f "$out"
        die "pre-deploy database backup failed — refusing to migrate"
    fi
}

run_management_command() {
    # One-shot container on the app network so DB_HOST resolves.
    docker run --rm --network "$DOCKER_NETWORK" --env-file "$APP_ENV_FILE" \
        "$IMAGE_NAME:$GIT_SHA" python manage.py "$@"
}

run_migrations() {
    step "Running migrations"
    run_management_command migrate --noinput 2>&1 | sed 's/^/       /'
    ok "migrations applied"
}

run_collectstatic() {
    [[ $DO_COLLECTSTATIC -eq 1 ]] || { info "collectstatic skipped (--no-collectstatic)"; return; }
    step "Collecting static files to S3"
    # Deliberately no --clear: it triggers a HeadObject-with-empty-key bug in
    # django-storages. Empty the bucket by hand if you ever need a clean sweep.
    run_management_command collectstatic --noinput 2>&1 | tail -n 5 | sed 's/^/       /'
    ok "static files collected"
}

swap_container() {
    step "Swapping the running container"
    if container_exists "$CONTAINER_NAME"; then
        docker stop "$CONTAINER_NAME" >/dev/null
        docker rm "$CONTAINER_NAME" >/dev/null
        ok "stopped and removed the old container"
    fi
    start_container "$IMAGE_NAME:$GIT_SHA"
    ok "started $CONTAINER_NAME from $IMAGE_NAME:$GIT_SHA"
}

start_container() {
    docker run -d --name "$CONTAINER_NAME" \
        --network "$DOCKER_NETWORK" --network-alias "$NETWORK_ALIAS" \
        -p "$HOST_BIND" \
        -u "$RUN_AS" \
        --env-file "$APP_ENV_FILE" \
        --restart unless-stopped \
        "$1" >/dev/null
}

health_check() {
    step "Health check"
    if ! command -v curl >/dev/null 2>&1; then
        warn "curl not available — skipping HTTP health check"
        return 0
    fi

    local i
    HEALTH_LAST_CODE=""
    for ((i = 1; i <= HEALTH_RETRIES; i++)); do
        if ! container_running "$CONTAINER_NAME"; then
            warn "container exited during startup"
            break
        fi
        HEALTH_LAST_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
                 -H "Host: $HEALTH_HOST_HEADER" "$HEALTH_URL" || true)"
        if [[ "$HEALTH_LAST_CODE" =~ ^[23] ]]; then
            ok "app responding on $HEALTH_URL (HTTP $HEALTH_LAST_CODE after ${i}s)"
            return 0
        fi
        if [[ "$HEALTH_LAST_CODE" == "400" ]]; then
            warn "HTTP 400 — app is up but rejected Host '$HEALTH_HOST_HEADER' (check ALLOWED_HOSTS)"
            return 0
        fi
        sleep 1
    done

    echo
    echo "  ${C_RED}last 40 lines of container log:${C_RST}"
    docker logs --tail 40 "$CONTAINER_NAME" 2>&1 | sed 's/^/       /' || true
    return 1
}

edge_check() {
    # Warn-only: the app itself already passed health_check, and rolling it
    # back wouldn't fix nginx. Never reload nginx from here -- it may serve
    # other sites, and a jolof deploy must not be able to take them down.
    [[ -n "$NGINX_CONTAINER" ]] || return 0
    step "Edge check (through $NGINX_CONTAINER)"
    if ! command -v curl >/dev/null 2>&1; then
        warn "curl not available — skipping edge check"
        return 0
    fi

    local i code=""
    for ((i = 1; i <= EDGE_RETRIES; i++)); do
        code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 \
                 --resolve "$HEALTH_HOST_HEADER:443:127.0.0.1" "https://$HEALTH_HOST_HEADER/" || true)"
        if [[ "$code" =~ ^[23] ]]; then
            ok "https://$HEALTH_HOST_HEADER/ via nginx (HTTP $code after ${i}s)"
            return 0
        fi
        sleep 1
    done
    warn "https://$HEALTH_HOST_HEADER/ via nginx still failing after ${EDGE_RETRIES}s (last status: ${code:-none})"
    info "the app is up on $HEALTH_URL — this is an nginx problem (502 = can't reach '$NETWORK_ALIAS' on $DOCKER_NETWORK)"
    info "check the proxy's config and: docker logs --tail 40 $NGINX_CONTAINER"
}

offer_self_update() {
    # Install a newer copy of this script from a release checkout. Safe mid-run:
    # install(1) writes a new file, and bash keeps reading the one it opened.
    local candidate="$1" new_ver
    [[ -f "$candidate" ]] || return 0
    if [[ "$SELF_PATH" != "$(readlink -f "$INSTALL_PATH" 2>/dev/null)" ]]; then
        info "not running from $INSTALL_PATH — skipping the self-update check"
        return 0
    fi
    if cmp -s "$SELF_PATH" "$candidate"; then
        ok "deploy script already up to date ($SCRIPT_VERSION)"
        return 0
    fi

    new_ver="$(grep -m1 '^SCRIPT_VERSION=' "$candidate" | cut -d'"' -f2 || true)"
    if [[ -z "$new_ver" || "$new_ver" == "$SCRIPT_VERSION" ]]; then
        warn "$INSTALL_PATH differs from the release's copy but both say ${SCRIPT_VERSION} — not installing"
        info "someone edited one without bumping SCRIPT_VERSION; compare: diff $SELF_PATH $candidate"
        return 0
    fi
    # --ref to an older commit carries an older script; never downgrade.
    if [[ "$(printf '%s\n%s\n' "$SCRIPT_VERSION" "$new_ver" | sort -V | tail -n1)" == "$SCRIPT_VERSION" ]]; then
        info "release carries an older deploy script ($new_ver); keeping $SCRIPT_VERSION"
        return 0
    fi

    step "Deploy script update: $SCRIPT_VERSION → $new_ver"
    if confirm "Install $new_ver to $INSTALL_PATH? (takes effect on the next run)"; then
        install -m 0755 "$candidate" "$INSTALL_PATH"
        ok "installed $("$INSTALL_PATH" --version) — this run continues on $SCRIPT_VERSION"
    else
        warn "deploy script not updated; this copy ($SCRIPT_VERSION) is behind the release ($new_ver)"
        info "later: jolof-deploy --self-install"
    fi
}

recover() {
    echo
    warn "health check failed after ${HEALTH_RETRIES}s (last status: ${HEALTH_LAST_CODE:-none})"
    if ! image_exists "$IMAGE_NAME:previous"; then
        die "no $IMAGE_NAME:previous image to roll back to — the app is DOWN, investigate the log above"
    fi
    if [[ $ASSUME_YES -eq 1 ]] || confirm "Roll back to $IMAGE_NAME:previous?"; then
        rollback
        die "deploy failed and was rolled back; the new code is still at $RELEASE_DIR"
    fi
    die "deploy failed and was left in place for debugging; the app may be DOWN"
}

rollback() {
    local target="" prev=""
    [[ -f "$STATE_DIR/previous" ]] && prev="$(<"$STATE_DIR/previous")"

    if [[ -n "$prev" ]] && image_exists "$IMAGE_NAME:$prev"; then
        target="$IMAGE_NAME:$prev"
        step "Rolling back to the previously deployed commit ($prev)"
    elif image_exists "$IMAGE_NAME:previous"; then
        target="$IMAGE_NAME:previous"
        step "Rolling back to $IMAGE_NAME:previous"
        warn "no recorded previous commit — using the :previous tag, which is mutable"
        info "verify what you land on: docker exec $CONTAINER_NAME python -c \\"
        info "  'import jolof.settings.base as s; print(s.VERSION)'"
    else
        die "nothing to roll back to: no recorded previous commit and no $IMAGE_NAME:previous image"
    fi

    if container_exists "$CONTAINER_NAME"; then
        docker stop "$CONTAINER_NAME" >/dev/null 2>&1 || true
        docker rm "$CONTAINER_NAME" >/dev/null 2>&1 || true
    fi
    start_container "$target"
    ok "rolled back — $CONTAINER_NAME is running $target"

    # Point the state at what is actually running now, and drop the previous
    # entry so a second --rollback can't ping-pong.
    if [[ -n "$prev" ]]; then
        mkdir -p "$STATE_DIR"
        printf '%s\n' "$prev" > "$STATE_DIR/deployed"
        rm -f "$STATE_DIR/previous"
    fi

    info "NOTE: database migrations are NOT reverted. If the failed deploy migrated,"
    info "      restore from $BACKUP_DIR with deployment/restore_backup.sh."
}

prune_releases() {
    step "Housekeeping"
    ln -sfn "$RELEASE_DIR" "$BASE_DIR/current"
    ok "$BASE_DIR/current → $RELEASE_DIR"

    local old
    mapfile -t old < <(ls -1d "$RELEASES_DIR"/*/ 2>/dev/null | sort -r | tail -n +$((KEEP_RELEASES + 1)))
    if [[ ${#old[@]} -gt 0 ]]; then
        rm -rf "${old[@]}"
        ok "pruned ${#old[@]} old release director$([[ ${#old[@]} -eq 1 ]] && echo y || echo ies)"
    fi

    # Only ever touch our own images. A bare `docker image prune -f` deletes
    # dangling images host-wide, which on a shared box means other apps'.
    # Retire sha-tagged builds beyond the last KEEP_RELEASES, never touching the
    # currently deployed one or the rollback target.
    local keep_shas protected tag
    protected="$GIT_SHA"
    [[ -f "$STATE_DIR/previous" ]] && protected+=" $(<"$STATE_DIR/previous")"
    keep_shas="$(docker images "$IMAGE_NAME" --format '{{.Tag}}' \
                 | grep -E '^[0-9a-f]{7,40}$' | head -n "$KEEP_RELEASES")"
    while read -r tag; do
        [[ -n "$tag" ]] || continue
        grep -qw "$tag" <<<"$keep_shas $protected" && continue
        docker rmi "$IMAGE_NAME:$tag" >/dev/null 2>&1 \
            && info "retired old build $IMAGE_NAME:$tag"
    done < <(docker images "$IMAGE_NAME" --format '{{.Tag}}' | grep -E '^[0-9a-f]{7,40}$' || true)

    local stale; stale="$(docker images "$IMAGE_NAME" --filter dangling=true -q | sort -u)"
    if [[ -n "$stale" ]]; then
        local count; count="$(wc -l <<<"$stale")"
        # shellcheck disable=SC2086
        docker rmi $stale >/dev/null 2>&1 || true
        ok "removed $count dangling $IMAGE_NAME image(s); other apps' images untouched"
    fi
}

###############################################################################
# MAIN
###############################################################################
if [[ $DO_SELF_INSTALL -eq 1 ]]; then
    [[ $EUID -eq 0 ]] || die "must run as root"
    candidate="$BASE_DIR/current/deployment/deploy_jolof.sh"
    [[ -f "$candidate" ]] || die "no deploy script in the current release: $candidate"
    offer_self_update "$candidate"
    exit 0
fi

if [[ $DO_ROLLBACK -eq 1 ]]; then
    [[ $EUID -eq 0 ]] || die "must run as root"
    docker info >/dev/null 2>&1 || die "docker daemon is not responding"
    rollback
    health_check && edge_check || warn "the previous image is not passing the health check either"
    exit 0
fi

preflight

if [[ $DRY_RUN -eq 1 ]]; then
    echo
    if [[ $WARN_COUNT -eq 0 ]]; then
        echo "${C_GRN}${C_BLD}Pre-flight passed. Nothing was changed.${C_RST}"
    else
        echo "${C_YEL}${C_BLD}Pre-flight passed with $WARN_COUNT warning(s). Nothing was changed.${C_RST}"
    fi
    exit 0
fi

if [[ $WARN_COUNT -gt 0 ]]; then
    echo
    confirm "Pre-flight raised $WARN_COUNT warning(s). Continue with the deploy?" \
        || die "aborted at the pre-flight prompt"
fi

# From here on, a mid-flight failure should say so loudly rather than exiting quietly.
trap 'echo; echo "${C_RED}${C_BLD}Deploy aborted (line $LINENO).${C_RST} Container state may be inconsistent — check: docker ps -a" >&2' ERR

clone_repo
offer_self_update "$RELEASE_DIR/deployment/deploy_jolof.sh"
tag_previous      # must precede build_image: the build moves the :latest tag
build_image
backup_database
run_migrations
run_collectstatic
swap_container

trap - ERR
health_check || recover
record_state
edge_check
prune_releases

echo
echo "${C_GRN}${C_BLD}Deploy complete.${C_RST}  $IMAGE_NAME:$GIT_SHA  ($GIT_REF)"
info "$GIT_SUBJECT"
info "release: $RELEASE_DIR"
info "log:     $LOG_FILE"
[[ $WARN_COUNT -gt 0 ]] && info "${C_YEL}$WARN_COUNT warning(s) above${C_RST}"
exit 0

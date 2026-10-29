#!/usr/bin/env bash
#
# install_deploy.sh — one-time setup of /etc/jolof for deploy_jolof.sh.
# Run once as root from a checkout of the repo:  ./deployment/install_deploy.sh
# Safe to re-run: existing files are kept.
#
set -Eeuo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$HERE")"
CONFIG_DIR="/etc/jolof"

echo "Setting up $CONFIG_DIR"
install -d -m 0755 "$CONFIG_DIR"
install -d -m 0755 /opt/jolof/releases /opt/jolof/backups

# App env: the file docker --env-file consumes. Secrets, so 0600.
if [[ -f "$CONFIG_DIR/jolof.env" ]]; then
    echo "  keeping existing $CONFIG_DIR/jolof.env"
else
    install -m 0600 -o root -g root "$REPO_DIR/.env.example" "$CONFIG_DIR/jolof.env"
    echo "  created $CONFIG_DIR/jolof.env from .env.example (0600) — fill it in"
fi

if [[ -f "$CONFIG_DIR/deploy.conf" ]]; then
    echo "  keeping existing $CONFIG_DIR/deploy.conf"
else
    install -m 0644 "$HERE/deploy.conf.example" "$CONFIG_DIR/deploy.conf"
    echo "  created $CONFIG_DIR/deploy.conf — set REPO_URL and HEALTH_HOST_HEADER"
fi

# The script itself, on PATH for root.
install -m 0755 "$HERE/deploy_jolof.sh" /usr/local/sbin/jolof-deploy
echo "  installed /usr/local/sbin/jolof-deploy"

echo
echo "Next:"
echo "  1. fill in $CONFIG_DIR/jolof.env and $CONFIG_DIR/deploy.conf"
echo "  2. jolof-deploy --check      # pre-flight only, changes nothing"
echo "  3. jolof-deploy              # full deploy"

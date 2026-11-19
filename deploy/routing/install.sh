#!/usr/bin/env bash
# One-time (and idempotent) setup of the BusTrack routing node.
# Target: a fresh Ubuntu 24.04 Linode, run as root from a checkout of this
# directory:
#
#   ROUTING_HOST=routing.example.org MOTIS_CHANNEL=production ./install.sh
#
# The bearer token is read from /etc/caddy/motis.env when it exists, otherwise
# generated once and printed a single time so it can be stored as the backend
# secret MOTIS_TOKEN. It is never written anywhere else.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "run as root" >&2; exit 1; fi
: "${ROUTING_HOST:?set ROUTING_HOST to the DNS name of the routing node}"
MOTIS_CHANNEL="${MOTIS_CHANNEL:-production}"
case "$MOTIS_CHANNEL" in production|beta) ;; *) echo "MOTIS_CHANNEL must be production or beta" >&2; exit 1;; esac
SWAP_SIZE="${SWAP_SIZE:-1G}"
here="$(cd "$(dirname "$0")" && pwd)"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 zstd curl debian-keyring debian-archive-keyring apt-transport-https ufw \
  unattended-upgrades update-notifier-common
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy
fi

# Swap absorbs the short overlap of two MOTIS processes during a swap.
if ! swapon --show=NAME --noheadings | grep -q '^/swapfile$'; then
  [[ -f /swapfile ]] || { fallocate -l "$SWAP_SIZE" /swapfile; chmod 600 /swapfile; mkswap /swapfile >/dev/null; }
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# Security updates install automatically; a required reboot happens at 09:00
# UTC. Automatic-Reboot-Time is local time, so the node runs on UTC.
timedatectl set-timezone Etc/UTC
install -o root -g root -m 0644 "$here/20auto-upgrades" /etc/apt/apt.conf.d/20auto-upgrades
install -o root -g root -m 0644 "$here/52bustrack-unattended-upgrades" /etc/apt/apt.conf.d/52bustrack-unattended-upgrades

id motis >/dev/null 2>&1 || useradd --system --home-dir /srv/motis --shell /usr/sbin/nologin motis
install -d -o root -g root -m 0755 /srv/motis /srv/motis/bin /srv/motis/downloads /srv/motis/public
install -d -o motis -g motis -m 0755 /srv/motis/releases
install -o root -g root -m 0755 "$here/motis_update.py" /srv/motis/motis_update.py
install -o root -g root -m 0644 "$here/motis@.service" "$here/motis-update.service" "$here/motis-update.timer" \
  "$here/routing-status.service" "$here/routing-status.timer" /etc/systemd/system/

umask 077
if [[ ! -f /etc/caddy/motis.env ]]; then
  token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  printf 'ROUTING_HOST=%s\nMOTIS_TOKEN=%s\n' "$ROUTING_HOST" "$token" > /etc/caddy/motis.env
  echo "MOTIS_TOKEN (store it as the backend secret now; it is not shown again):"
  echo "$token"
else
  sed -i "s/^ROUTING_HOST=.*/ROUTING_HOST=$ROUTING_HOST/" /etc/caddy/motis.env
fi
chown root:caddy /etc/caddy/motis.env && chmod 0640 /etc/caddy/motis.env
grep -q '^MOTIS_TOKEN=.\{32,\}$' /etc/caddy/motis.env || { echo "MOTIS_TOKEN in /etc/caddy/motis.env is missing or too short" >&2; exit 1; }
printf 'MOTIS_CHANNEL=%s\n' "$MOTIS_CHANNEL" > /etc/default/motis-update
umask 022

install -d /etc/systemd/system/caddy.service.d
printf '[Service]\nEnvironmentFile=/etc/caddy/motis.env\n' > /etc/systemd/system/caddy.service.d/motis.conf
install -o root -g root -m 0644 "$here/Caddyfile" /etc/caddy/Caddyfile
# Until the first dataset is active the proxy answers 503.
[[ -f /etc/caddy/motis-upstream.caddy ]] || echo 'respond 503' > /etc/caddy/motis-upstream.caddy
install -d -o caddy -g caddy /var/log/caddy

ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

# Refuse to restart Caddy into a broken config (it would take the API down).
( set -a; . /etc/caddy/motis.env; set +a; caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile >/dev/null )

systemctl daemon-reload
systemctl enable caddy >/dev/null
systemctl restart caddy
systemctl enable --now motis-update.timer
systemctl enable --now routing-status.timer
echo "first update (downloads the dataset and MOTIS, then swaps):"
systemctl start motis-update.service || { journalctl -u motis-update -n 50 --no-pager; exit 1; }
python3 -I /srv/motis/motis_update.py status

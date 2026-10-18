#!/bin/bash
# One-time VPS setup for the Minecraft Server Manager. Run as root:  bash vps_setup.sh
# - mctunnel: can ONLY open the Minecraft port (no shell, no file access)
# - mcsync:   can ONLY use SFTP inside /srv/mcworld (world zips + the host lock)
# Nothing here touches 3x-ui / Xray and it adds no background processes (no extra RAM).
set -e
[ "$(id -u)" = 0 ] || { echo "Run this as root."; exit 1; }

# Public keys come from the environment (never stored in this repo):
#   SYNC_PUB='ssh-ed25519 AAAA... name' TUNNEL_PUB='ssh-ed25519 AAAA... name' bash vps_setup.sh
# Leave them empty to create the accounts only; keys are then added later (mc-hub / mc-add-friend).
SYNC_PUB="${SYNC_PUB:-}"
TUNNEL_PUB="${TUNNEL_PUB:-}"
PORT=25565
NOLOGIN="$(command -v nologin || echo /usr/sbin/nologin)"

mkuser() {
  id "$1" >/dev/null 2>&1 || useradd -m -s "$NOLOGIN" "$1"
  usermod -p '*' "$1"                       # unlocked for key login, no password possible
  install -d -o root -g root -m 755 "/home/$1/.ssh"   # root-owned: the account cannot edit its own keys
}

addkey() {  # user, options, pubkey
  f="/home/$1/.ssh/authorized_keys"
  echo "$2 $3" > "$f"
  chown root:root "$f"; chmod 644 "$f"
}

mkuser mcsync
mkuser mctunnel
install -d -o mcsync -g mcsync -m 700 /srv/mcworld

[ -z "$SYNC_PUB" ] || addkey mcsync   'restrict,command="internal-sftp -d /srv/mcworld"' "$SYNC_PUB"
[ -z "$TUNNEL_PUB" ] || addkey mctunnel "restrict,port-forwarding,permitlisten=\"0.0.0.0:$PORT\"" "$TUNNEL_PUB"

# a 0.0.0.0 reverse-forward is only reachable from the internet with GatewayPorts
if ! grep -Eqsi '^\s*GatewayPorts\s+(yes|clientspecified)' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf; then
  echo 'GatewayPorts clientspecified' >> /etc/ssh/sshd_config
fi
sshd -t
systemctl reload ssh 2>/dev/null || systemctl reload sshd

if command -v ufw >/dev/null && ufw status | grep -q 'Status: active'; then
  ufw allow "$PORT/tcp"
fi
echo "Done. Users: mctunnel (tunnel only), mcsync (world storage only). Port $PORT is free for Minecraft."

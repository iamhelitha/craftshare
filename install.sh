#!/bin/bash
# CraftShare - VPS installer. Run as root:
#   bash <(curl -Ls https://raw.githubusercontent.com/iamhelitha/craftshare/main/install.sh)
#   bash <(curl -Ls .../install.sh) v1.2.0        install a specific release
#   bash <(curl -Ls .../install.sh) uninstall     remove everything (offers a world backup download first)
#   Afterwards, manage it with the command:  mc-hub   (mc-hub help; mc-hub update; mc-hub uninstall)
#   MC_FIREWALL=yes bash <(curl -Ls .../install.sh)   open the firewall ports without asking
# Installs mc-hub: a tiny service that lets friends join with an invite code. It never touches
# 3x-ui / Xray or any existing world files, and running it again just updates the hub (keeps friends and world).
set -euo pipefail

REPO="${MC_REPO:-iamhelitha/craftshare}"
ARG="${1:-latest}"

red() { printf '\033[31m%s\033[0m\n' "$*" >&2; }
[ "$(id -u)" = 0 ] || { red "Please run this as root (try: sudo -i, then run the command again)."; exit 1; }
[ "$(uname -s)" = Linux ] || { red "This installer is for Linux servers."; exit 1; }

if ! command -v python3 >/dev/null; then
  echo "Installing python3..."
  if command -v apt-get >/dev/null; then apt-get update -qq && apt-get install -y -qq python3
  elif command -v dnf >/dev/null; then dnf install -y -q python3
  elif command -v yum >/dev/null; then yum install -y -q python3
  else red "Please install python3 and run this again."; exit 1; fi
fi
command -v curl >/dev/null || { red "curl is required."; exit 1; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
if [ "$ARG" = latest ] || [ "$ARG" = uninstall ]; then
  BASE="https://github.com/$REPO/releases/latest/download"
else
  BASE="https://github.com/$REPO/releases/download/$ARG"
fi

echo "Downloading mc-hub ($ARG)..."
curl -fsSL "$BASE/mc_hub.py" -o "$TMP/mc_hub.py"
curl -fsSL "$BASE/SHA256SUMS.txt" -o "$TMP/SHA256SUMS.txt"
want="$(grep -E ' \*?mc_hub\.py$' "$TMP/SHA256SUMS.txt" | cut -d' ' -f1)"
have="$(sha256sum "$TMP/mc_hub.py" | cut -d' ' -f1)"
if [ -z "$want" ] || [ "$want" != "$have" ]; then
  red "Checksum mismatch - download is not what the release published. Aborting."; exit 1
fi

if [ "$ARG" = uninstall ]; then
  python3 "$TMP/mc_hub.py" uninstall < /dev/tty
  exit 0
fi

# Firewall: ask first. MC_FIREWALL=yes|no skips the question (for scripts).
FW="${MC_FIREWALL:-}"
if [ -z "$FW" ]; then
  if [ -r /dev/tty ]; then
    echo
    echo "Friends need two TCP ports open on this server: 25565 (Minecraft) and 8766 (invites)."
    printf 'Add these rules to the server firewall (ufw/firewalld) now? [y/N] '
    read -r FW < /dev/tty || FW=no
  else
    FW=no
  fi
fi
case "$FW" in
  y|Y|yes|YES) python3 "$TMP/mc_hub.py" install --firewall ;;
  *)           python3 "$TMP/mc_hub.py" install --no-firewall ;;
esac

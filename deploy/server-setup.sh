#!/usr/bin/env bash
#
# One-time bootstrap for a fresh Lightsail instance. Run it once, by hand, as
# the `ubuntu` user; after that every deploy is just a push to main.
#
#   curl -fsSL https://raw.githubusercontent.com/Immanueldavidckp/trading/main/deploy/server-setup.sh | bash
#
# Idempotent — safe to re-run.
set -euo pipefail

log() { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }

log "System packages (python venv + build headers, nodejs for pm2)"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-dev build-essential git curl

PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$PYV" in
  3.1[2-9]|3.[2-9]*) echo "python $PYV — ok" ;;
  *) echo "WARNING: python $PYV is too old; requirements.txt needs >= 3.12 (upstox-totp)."
     echo "         On Ubuntu 22.04: sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.12-venv"
     echo "         then re-run the deploy with:  python3 -m venv -> python3.12 -m venv" ;;
esac

if ! command -v pm2 >/dev/null; then
  log "Installing pm2"
  command -v npm >/dev/null || { curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -; sudo apt-get install -y -qq nodejs; }
  sudo npm install -g pm2
fi

log "Making pm2 survive reboots"
# Prints (and runs) the systemd unit install for the current user.
sudo env PATH="$PATH:$(dirname "$(command -v node)")" \
  pm2 startup systemd -u "$USER" --hp "$HOME" | tail -1
pm2 save --force || true

log "Staging directory for deploy scripts + rendered .env"
mkdir -p "$HOME/.trading-deploy"
chmod 700 "$HOME/.trading-deploy"

log "Firewall reminder"
cat <<'NOTE'
The backend listens on 0.0.0.0:8000. In the Lightsail console open
Networking -> IPv4 Firewall and allow TCP 8000 (or put nginx in front and
only expose 80/443).

Remaining one-time steps, off the server:
  1. Create a deploy SSH key and authorise it:
       ssh-keygen -t ed25519 -f ~/.ssh/trading_deploy -N ''
       ssh-copy-id -i ~/.ssh/trading_deploy.pub ubuntu@<lightsail-ip>
  2. Add these GitHub Actions secrets (Settings -> Secrets -> Actions):
       SSH_PRIVATE_KEY      contents of ~/.ssh/trading_deploy
       SERVER_HOST          Lightsail public IP or hostname
       UPSTOX_API_KEY, UPSTOX_API_SECRET,
       SHOONYA_USER_ID, SHOONYA_PASSWORD, SHOONYA_VENDOR_CODE,
       SHOONYA_API_KEY, SHOONYA_TOTP_SECRET,
       UPSTOX_USERNAME, UPSTOX_PIN_CODE, UPSTOX_TOTP_SECRET,
       GEMINI_API_KEY, AUTH_SECRET
  3. Optional repo variables: SERVER_USER (default ubuntu), SSH_PORT (22),
     PUBLIC_BASE_URL (e.g. http://api.mtandtiot.in:8000), MOCK_MODE (true).
NOTE

log "Bootstrap complete"

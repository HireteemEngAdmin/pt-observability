#!/usr/bin/env bash
# Install Grafana Alloy on the application EC2 to ship the PM2 logs to Loki.
#
# The password is read from STDIN, never from an argument or a command-line
# variable: process arguments are visible to any local user through `ps`.
#
# Usage (from the stack host, chaining both SSH hops):
#   ssh vps-obs 'cat /opt/observability/secrets/loki_push_password' \
#     | ssh ec2-pt 'cd /tmp/alloy-install && LOKI_PUSH_USER=alloy ./install-alloy.sh'
#
# ALLOY_VERSION pins the package. Leave it pinned: an unpinned `dnf install
# alloy` silently drifts to whatever is newest, which makes a broken log
# pipeline impossible to reproduce against the version that was reviewed.
set -euo pipefail

ALLOY_VERSION="${ALLOY_VERSION:-1.18.0}"
DIR="$(cd "$(dirname "$0")" && pwd)"
: "${LOKI_PUSH_USER:?set LOKI_PUSH_USER}"
read -r LOKI_PUSH_PASSWORD || true
[ -n "${LOKI_PUSH_PASSWORD:-}" ] || { echo "FAILED: send the password on stdin"; exit 1; }

# Grafana's official repository (Amazon Linux 2023 uses dnf/rpm).
sudo tee /etc/yum.repos.d/grafana.repo >/dev/null <<'EOF'
[grafana]
name=grafana
baseurl=https://rpm.grafana.com
repo_gpgcheck=1
enabled=1
gpgcheck=1
gpgkey=https://rpm.grafana.com/gpg.key
sslverify=1
EOF

sudo dnf install -y "alloy-${ALLOY_VERSION}"

INSTALLED="$(rpm -q --queryformat '%{VERSION}' alloy)"
[ "$INSTALLED" = "$ALLOY_VERSION" ] || {
  echo "FAILED: expected alloy ${ALLOY_VERSION}, got ${INSTALLED}"
  exit 1
}
echo "OK: alloy ${INSTALLED} installed"

sudo mkdir -p /etc/alloy
sudo cp "${DIR}/alloy-config.alloy" /etc/alloy/config.alloy

# The credential goes in the EnvironmentFile, off the command line.
sudo tee /etc/alloy/alloy.env >/dev/null <<EOF
LOKI_PUSH_USER=${LOKI_PUSH_USER}
LOKI_PUSH_PASSWORD=${LOKI_PUSH_PASSWORD}
EOF
sudo chmod 600 /etc/alloy/alloy.env

# The alloy user needs to traverse /home/ec2-user to reach the logs. Measured on
# the host: .pm2 and .pm2/logs are already 755 and the files already 644 — the
# only barrier is the home directory at 700. Hence g+x (group) and not o+x
# (everyone): the ec2-user group has no other members, so in practice only alloy
# gains access.
sudo usermod -a -G ec2-user alloy
sudo chmod g+x /home/ec2-user

# The package already points alloy at /etc/alloy/config.alloy; the override only
# injects the credential.
sudo mkdir -p /etc/systemd/system/alloy.service.d
sudo tee /etc/systemd/system/alloy.service.d/override.conf >/dev/null <<'EOF'
[Service]
EnvironmentFile=/etc/alloy/alloy.env
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now alloy
sudo systemctl restart alloy

echo "--- validation ---"
for _ in $(seq 15); do
  systemctl is-active --quiet alloy && break
  sleep 1
done
systemctl is-active --quiet alloy || { echo "FAILED: alloy is not active"; sudo journalctl -u alloy -n 30 --no-pager; exit 1; }
echo "OK: alloy active"

echo "--- nothing reachable from outside ---"
# Alloy binds its own UI on 127.0.0.1:12345 by default, which is acceptable: it
# is reachable neither from the internet nor from the VPC private IP. What must
# not happen is a bind on 0.0.0.0 or on an interface address.
#
# The wait is not optional: without it the check runs before the bind and always
# "passes", reporting OK exactly when there is nothing to verify yet.
for _ in $(seq 15); do
  sudo ss -tlnp 2>/dev/null | grep -q "alloy" && break
  sleep 1
done
EXPOSED=$(sudo ss -tlnp 2>/dev/null | grep "alloy" | awk '{print $4}' | grep -vE '^(127\.0\.0\.1|\[::1\]):' || true)
if [ -n "$EXPOSED" ]; then
  echo "FAILED: alloy listening outside loopback:"
  echo "$EXPOSED"
  exit 1
fi
echo "OK: alloy on loopback only ($(sudo ss -tlnp 2>/dev/null | grep alloy | awk '{print $4}' | tr '\n' ' '))"

#!/usr/bin/env bash
# Install node_exporter as a systemd service on the application EC2, with TLS
# and basic auth. Port 9100 is open to 0.0.0.0/0 in the security group, so the
# content has to be unreadable without a credential.
#
# Usage:
#   NODE_EXPORTER_BCRYPT='$2b$12$...' PUBLIC_DNS=ec2-x.compute-1.amazonaws.com \
#     ./install-node-exporter.sh [version]     (version defaults to 1.9.1)
#
# The bcrypt hash comes from the README section "node_exporter on the EC2"; the
# plaintext password stays in secrets/ on the stack host and never reaches here.
set -euo pipefail

VERSION="${1:-1.9.1}"
DIR="$(cd "$(dirname "$0")" && pwd)"
: "${NODE_EXPORTER_BCRYPT:?set NODE_EXPORTER_BCRYPT to the password's bcrypt hash}"
: "${PUBLIC_DNS:?set PUBLIC_DNS to the EC2 public DNS (the certificate SAN)}"
PUBLIC_IP="$(curl -fsS --max-time 5 https://api.ipify.org || true)"

cd /tmp
curl -fsSLO "https://github.com/prometheus/node_exporter/releases/download/v${VERSION}/node_exporter-${VERSION}.linux-amd64.tar.gz"
tar xzf "node_exporter-${VERSION}.linux-amd64.tar.gz"
sudo mv "node_exporter-${VERSION}.linux-amd64/node_exporter" /usr/local/bin/
id -u node_exporter >/dev/null 2>&1 || sudo useradd -rs /bin/false node_exporter

# Self-signed certificate. The SAN must match the target in prometheus.yml,
# which pins this cert as its CA instead of using insecure_skip_verify.
sudo mkdir -p /etc/node_exporter
sudo openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout /etc/node_exporter/node_exporter.key \
  -out /etc/node_exporter/node_exporter.crt \
  -subj "/CN=${PUBLIC_DNS}" \
  -addext "subjectAltName=DNS:${PUBLIC_DNS}${PUBLIC_IP:+,IP:${PUBLIC_IP}}" 2>/dev/null

sudo tee /etc/node_exporter/web-config.yml >/dev/null <<EOF
tls_server_config:
  cert_file: /etc/node_exporter/node_exporter.crt
  key_file: /etc/node_exporter/node_exporter.key

basic_auth_users:
  prometheus: ${NODE_EXPORTER_BCRYPT}
EOF

sudo chown -R node_exporter:node_exporter /etc/node_exporter
sudo chmod 600 /etc/node_exporter/node_exporter.key /etc/node_exporter/web-config.yml
sudo chmod 644 /etc/node_exporter/node_exporter.crt

sudo cp "${DIR}/node_exporter.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now node_exporter

echo "--- validation ---"
# systemctl --now returns before the socket is ready; without the wait the curl
# comes back empty and the check "passes" even on a broken service.
for _ in $(seq 10); do
  curl -sk --max-time 3 https://localhost:9100/metrics >/dev/null 2>&1 && break
  sleep 1
done
code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 https://localhost:9100/metrics || true)"
[ "$code" = "401" ] || { echo "FAILED: expected 401 without a credential, got '${code}'"; exit 1; }
echo "OK: TLS active and basic auth demanding a credential (401)"
echo "Copy /etc/node_exporter/node_exporter.crt to prometheus/node_exporter-ca.crt in the stack repo."

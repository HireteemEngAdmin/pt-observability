#!/usr/bin/env bash
# Instala o node_exporter como servico systemd na EC2 da aplicacao, com TLS +
# basic auth (a porta 9100 fica aberta em 0.0.0.0/0 no security group, entao o
# conteudo precisa ser ilegivel sem credencial).
#
# Uso:
#   NODE_EXPORTER_BCRYPT='$2b$12$...' PUBLIC_DNS=ec2-x.compute-1.amazonaws.com \
#     ./install-node-exporter.sh [versao]     (versao default 1.9.1)
#
# O hash bcrypt vem do README (secao "node_exporter na EC2"); a senha em claro
# fica em secrets/ no host do stack e nunca chega aqui.
set -euo pipefail

VERSION="${1:-1.9.1}"
DIR="$(cd "$(dirname "$0")" && pwd)"
: "${NODE_EXPORTER_BCRYPT:?defina NODE_EXPORTER_BCRYPT com o hash bcrypt da senha}"
: "${PUBLIC_DNS:?defina PUBLIC_DNS com o DNS publico da EC2 (SAN do certificado)}"
PUBLIC_IP="$(curl -fsS --max-time 5 https://api.ipify.org || true)"

cd /tmp
curl -fsSLO "https://github.com/prometheus/node_exporter/releases/download/v${VERSION}/node_exporter-${VERSION}.linux-amd64.tar.gz"
tar xzf "node_exporter-${VERSION}.linux-amd64.tar.gz"
sudo mv "node_exporter-${VERSION}.linux-amd64/node_exporter" /usr/local/bin/
id -u node_exporter >/dev/null 2>&1 || sudo useradd -rs /bin/false node_exporter

# Certificado autoassinado; o SAN precisa bater com o target do prometheus.yml,
# que pina este cert como CA (sem insecure_skip_verify).
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

echo "--- validacao ---"
# systemctl --now retorna antes do socket estar pronto; sem a espera o curl sai
# vazio e a validacao "passa" mesmo com o servico quebrado.
for _ in $(seq 10); do
  curl -sk --max-time 3 https://localhost:9100/metrics >/dev/null 2>&1 && break
  sleep 1
done
code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 https://localhost:9100/metrics || true)"
[ "$code" = "401" ] || { echo "FALHOU: esperava 401 sem credencial, veio '${code}'"; exit 1; }
echo "OK: TLS ativo e basic auth exigindo credencial (401)"
echo "Copie /etc/node_exporter/node_exporter.crt para prometheus/node_exporter-ca.crt no repo do stack."

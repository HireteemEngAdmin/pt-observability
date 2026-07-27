#!/usr/bin/env bash
# Instala o node_exporter como servico systemd na EC2 da aplicacao.
# Uso: ./install-node-exporter.sh [versao]   (default 1.9.1)
set -euo pipefail

VERSION="${1:-1.9.1}"
DIR="$(cd "$(dirname "$0")" && pwd)"

cd /tmp
curl -fsSLO "https://github.com/prometheus/node_exporter/releases/download/v${VERSION}/node_exporter-${VERSION}.linux-amd64.tar.gz"
tar xzf "node_exporter-${VERSION}.linux-amd64.tar.gz"
sudo mv "node_exporter-${VERSION}.linux-amd64/node_exporter" /usr/local/bin/
id -u node_exporter >/dev/null 2>&1 || sudo useradd -rs /bin/false node_exporter
sudo cp "${DIR}/node_exporter.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now node_exporter

echo "--- validacao ---"
curl -s localhost:9100/metrics | head -3

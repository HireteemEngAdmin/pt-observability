#!/usr/bin/env bash
# Restaura ./backup/*.tgz nos volumes nomeados (rodar ANTES do primeiro
# docker compose up -d no host novo).
set -euo pipefail
cd "$(dirname "$0")/.."

for vol in pt-obs-prom-data pt-obs-grafana-data pt-obs-caddy-data pt-obs-loki-data; do
  [ -f "backup/${vol}.tgz" ] || { echo "faltando backup/${vol}.tgz"; exit 1; }
  docker volume create "${vol}" >/dev/null
  echo "restaurando ${vol}..."
  docker run --rm -v "${vol}":/data -v "${PWD}/backup":/backup alpine \
    sh -c "cd /data && tar xzf /backup/${vol}.tgz"
done

echo "volumes restaurados; agora: docker compose up -d"

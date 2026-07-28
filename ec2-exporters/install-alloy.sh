#!/usr/bin/env bash
# Instala o Grafana Alloy na EC2 da aplicacao para enviar os logs do PM2 ao Loki.
#
# A senha e lida do STDIN, nunca de argumento nem de variavel na linha de
# comando: argumento de processo e visivel para qualquer usuario local via `ps`.
#
# Uso (a partir do host do stack, encadeando os dois SSH):
#   ssh vps-obs 'cat /opt/observability/secrets/loki_push_password' \
#     | ssh ec2-pt 'cd /tmp/alloy-install && LOKI_PUSH_USER=alloy ./install-alloy.sh'
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
: "${LOKI_PUSH_USER:?defina LOKI_PUSH_USER}"
read -r LOKI_PUSH_PASSWORD || true
[ -n "${LOKI_PUSH_PASSWORD:-}" ] || { echo "FALHOU: envie a senha pelo stdin"; exit 1; }

# Repo oficial da Grafana (Amazon Linux 2023 usa dnf/rpm).
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

sudo dnf install -y alloy

sudo mkdir -p /etc/alloy
sudo cp "${DIR}/alloy-config.alloy" /etc/alloy/config.alloy

# A credencial vai no EnvironmentFile, fora da linha de comando.
sudo tee /etc/alloy/alloy.env >/dev/null <<EOF
LOKI_PUSH_USER=${LOKI_PUSH_USER}
LOKI_PUSH_PASSWORD=${LOKI_PUSH_PASSWORD}
EOF
sudo chmod 600 /etc/alloy/alloy.env

# O usuario alloy precisa atravessar /home/ec2-user para chegar aos logs.
# Medido na EC2: .pm2 e .pm2/logs ja sao 755 e os arquivos ja sao 644 — o unico
# bloqueio e o home, que e 700. Por isso g+x (grupo) e nao o+x (todos): o grupo
# ec2-user nao tem outros membros, entao na pratica so o alloy ganha travessia.
sudo usermod -a -G ec2-user alloy
sudo chmod g+x /home/ec2-user

# O pacote sobe o alloy apontando para /etc/alloy/config.alloy por padrao; o
# override injeta so a credencial.
sudo mkdir -p /etc/systemd/system/alloy.service.d
sudo tee /etc/systemd/system/alloy.service.d/override.conf >/dev/null <<'EOF'
[Service]
EnvironmentFile=/etc/alloy/alloy.env
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now alloy
sudo systemctl restart alloy

echo "--- validacao ---"
for _ in $(seq 15); do
  systemctl is-active --quiet alloy && break
  sleep 1
done
systemctl is-active --quiet alloy || { echo "FALHOU: alloy nao esta ativo"; sudo journalctl -u alloy -n 30 --no-pager; exit 1; }
echo "OK: alloy ativo"

echo "--- nenhuma porta alcancavel de fora ---"
# O alloy abre a UI dele em 127.0.0.1:12345 por padrao, o que e aceitavel: nao
# e alcancavel nem pela internet nem pelo IP privado da VPC. O que nao pode e
# ele escutar em 0.0.0.0 ou num IP de interface.
#
# A espera nao e opcional: sem ela o check roda antes do bind e "passa" sempre,
# reportando OK justamente quando ainda nao ha o que verificar.
for _ in $(seq 15); do
  sudo ss -tlnp 2>/dev/null | grep -q "alloy" && break
  sleep 1
done
EXPOSTA=$(sudo ss -tlnp 2>/dev/null | grep "alloy" | awk '{print $4}' | grep -vE '^(127\.0\.0\.1|\[::1\]):' || true)
if [ -n "$EXPOSTA" ]; then
  echo "FALHOU: alloy escutando fora do loopback:"
  echo "$EXPOSTA"
  exit 1
fi
echo "OK: alloy so em loopback ($(sudo ss -tlnp 2>/dev/null | grep alloy | awk '{print $4}' | tr '\n' ' '))"

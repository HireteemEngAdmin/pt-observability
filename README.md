# pt-observability

Stack de observabilidade do Performance Tracker: Prometheus + Grafana + Caddy em
docker-compose, com scrape remoto da EC2 da aplicacao (API, cron e node_exporter),
restrito por security group ao IP do host que roda este stack.

Todo o stack e codigo: clone + `.env` + `docker compose up -d` reproduz o ambiente
em qualquer host com Docker. Hoje roda na VPS; se for aprovado migrar para uma EC2
dedicada, seguir o runbook no fim deste arquivo.

Plano completo (fases, alertas, decisoes): pagina "Plano de Implementacao:
Observabilidade Performance Tracker" no Notion.

## Estrutura

```
docker-compose.yml            # prometheus + loki + grafana + caddy (TLS automatico)
.env.example                  # copiar para .env (nunca commitado)
secrets/                      # senhas e credenciais (NUNCA commitado)
caddy/Caddyfile               # dominio via env; roteia /loki/* com basic auth
prometheus/prometheus.yml     # scrape configs (targets = EC2 da aplicacao)
loki/loki-config.yml          # storage em disco, retencao 30d
grafana/provisioning/
  datasources/                # datasources Prometheus e Loki
  dashboards/                 # provider: carrega ../dashboards/*.json
  alerting/                   # templates .example p/ Teams (fase futura)
grafana/dashboards/           # JSONs dos dashboards + o gerador do custom
ec2-exporters/                # node_exporter (metricas) e alloy (logs) na EC2
scripts/                      # backup/restore dos volumes (migracao de host)
```

## Subir do zero (qualquer host com Docker)

```bash
git clone <url-deste-repo> /opt/observability
cd /opt/observability
cp .env.example .env   # preencher senha do Grafana e dominio
docker compose up -d
```

Pre-requisitos do host: Docker + compose plugin; DNS A do `GRAFANA_DOMAIN`
apontando para o IP do host (portas 80/443 abertas); IP do host presente na
allowlist do security group da EC2 da aplicacao (portas de metrica).

Validacao: `curl -s localhost:9090/api/v1/targets | grep health` deve mostrar os
3 jobs up; `https://$GRAFANA_DOMAIN` deve abrir com TLS valido.

## Regra de ouro: nada existe so na UI

Qualquer coisa criada/editada na UI do Grafana deve voltar para o repo, senao a
migracao de host perde tudo:

- **Dashboard**: Share > Export > Save JSON, salvar em `grafana/dashboards/`
  e commitar. O provisioning recarrega sozinho.
- **Alert rules**: Alerting > Alert rules > Export > YAML, salvar em
  `grafana/provisioning/alerting/` e commitar.
- Datasource ja e provisionado por arquivo; nao criar duplicado na UI. Contact
  point e policy entram na fase de alertas (secao abaixo).

## Alertas (fase futura, apos aprovacao do manager)

O nucleo sobe sem notificacoes: dashboards mostram o estado, ninguem e notificado.
Quando a feature for aprovada:

1. Criar o webhook do canal do Microsoft Teams via Workflows/Power Automate
   ("When a Teams webhook request is received"); os connectors classicos do O365
   foram descontinuados pela Microsoft.
2. Adicionar `TEAMS_WEBHOOK_URL` ao `.env` e descomentar a linha correspondente
   no `docker-compose.yml` (environment do grafana).
3. Renomear os `.example` de `grafana/provisioning/alerting/` para `.yml` e rodar
   `docker compose up -d`.
4. Criar as regras de alerta (tabela na pagina do Notion), testar fire + resolve
   e exportar o YAML das regras de volta para o repo.

## Exposicao das portas de metrica

As 3 portas ficam abertas em `0.0.0.0/0` no security group da EC2: optou-se por
nao colocar o IP do host de observabilidade nas regras da AWS. Consequencia
pratica, por porta:

| Porta | Job | Protecao | Por que |
|-------|-----|----------|---------|
| 3000 | api | nenhuma | `/metrics` da API ja era publico antes deste stack |
| 9464 | cron | nenhuma | mesma classe de dado que a 3000 ja expunha |
| 9100 | node | **TLS + basic auth** | sem isso vazaria versao do kernel, IP privado e topologia do host para qualquer scanner |

Restringir o `/metrics` publico da API (`METRICS_ALLOWED_IPS`) segue pendente na
fase de codigo, no repo `performance-tracker`.

## node_exporter na EC2 da aplicacao

Gere a senha e o hash no host do stack (a senha em claro nunca vai para a EC2
nem para o git):

```bash
mkdir -p secrets && umask 077
openssl rand -base64 30 | tr -d '\n' > secrets/node_exporter_password
python3 -c "import bcrypt;print(bcrypt.hashpw(open('secrets/node_exporter_password','rb').read(),bcrypt.gensalt(rounds=12)).decode())"
# O container do Prometheus roda como nobody (uid 65534); sem este chown o
# scrape do node falha com "permission denied", nao com erro de auth. O
# diretorio precisa ir junto: 700 do root impede o container de atravessa-lo,
# mesmo com o arquivo dentro ja pertencendo ao uid certo.
sudo chown 65534:65534 secrets secrets/node_exporter_password
```

Na EC2, com o hash impresso acima:

```bash
NODE_EXPORTER_BCRYPT='<hash>' PUBLIC_DNS='<dns-publico-da-ec2>' \
  ./install-node-exporter.sh
```

O script gera o certificado autoassinado, escreve o `web-config.yml` e valida
que a porta responde 401 sem credencial. Por fim copie o certificado para o
repo, que o Prometheus o pina como CA:

```bash
scp <ec2>:/etc/node_exporter/node_exporter.crt prometheus/node_exporter-ca.crt
```

Trocar a senha depois: repetir os dois blocos e `systemctl restart node_exporter`.

## Logs (Loki)

Os logs do PM2 da EC2 chegam ao Loki pelo Grafana Alloy, que empurra por HTTPS
em `/loki/api/v1/push` — mesma porta 443 do Grafana, sem subdominio novo.
Retencao de 30 dias; os logs contem PII (nome de empresa, IDs), diferente das
metricas.

Gerar a credencial do push (a senha em claro fica so em `secrets/`):

```bash
umask 077
openssl rand -base64 30 | tr -d '\n' > secrets/loki_push_password
HASH=$( (cat secrets/loki_push_password; echo) | docker run --rm -i caddy:2 \
  caddy hash-password --algorithm bcrypt )
# O $ precisa ir dobrado: o Compose interpola $2a$14$<salt> como variavel e
# apaga o trecho do salt — inclusive via env_file. O hash chega mutilado, o
# `caddy validate` aprova assim mesmo, e toda autenticacao passa a dar 401.
ESC=$(printf '%s' "$HASH" | sed 's/\$/$$/g')
{ echo "LOKI_PUSH_USER=alloy"; printf 'LOKI_PUSH_BCRYPT=%s\n' "$ESC"; } > secrets/caddy.env
chmod 600 secrets/caddy.env
docker compose up -d --force-recreate caddy
```

Validar (bcrypt tem 60 caracteres; menos que isso e o hash chegou cortado):

```bash
docker compose exec -T caddy printenv LOKI_PUSH_BCRYPT | tr -d '\r\n' | wc -c
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://$GRAFANA_DOMAIN/loki/api/v1/push   # 401
```

Instalacao do agente na EC2: ver `ec2-exporters/install-alloy.sh`.

## Migracao para outro host (ex.: EC2 dedicada)

O scrape de metricas e pull: na EC2 nada muda alem de uma regra de SG. Os logs
sao push, entao a EC2 precisa de um passo a mais (item 5).

1. Provisionar o host novo com Docker + compose; apontar o DNS A do
   `GRAFANA_DOMAIN` para o IP novo (Caddy reemite o certificado sozinho).
2. Clonar o repo e copiar `.env` e o diretorio `secrets/` inteiro (segredos vem
   do gerenciador de senhas; `secrets/` nao esta no git).
3. **Mantendo o historico**: no host antigo, `docker compose stop prometheus loki grafana`,
   rodar `scripts/backup-volumes.sh`, copiar `backup/` para o host novo e rodar
   `scripts/restore-volumes.sh` la. **Sem historico**: pular; dashboards e alertas
   vem do git, perde-se o TSDB e os logs.
4. No SG da EC2 da aplicacao: as 3 portas de metrica estao em `0.0.0.0/0`, entao
   nada muda (se o host novo estiver na MESMA VPC, vale trocar por referencia de
   SG + IP privado e fechar a exposicao publica).
5. Na EC2, apontar o Alloy para o dominio novo se ele mudar: editar o `url` em
   `/etc/alloy/config.alloy` e `systemctl restart alloy`. Se o `GRAFANA_DOMAIN`
   for o mesmo, nada a fazer — o push segue o DNS.
6. `docker compose up -d --force-recreate` no host novo; validar targets up,
   login no Grafana e log chegando (`{job="cron"}` no Explore).
7. `docker compose down` no host antigo.

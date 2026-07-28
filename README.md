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
docker-compose.yml            # prometheus + grafana + caddy (TLS automatico)
.env.example                  # copiar para .env (nunca commitado)
caddy/Caddyfile               # dominio via env GRAFANA_DOMAIN
prometheus/prometheus.yml     # scrape configs (targets = EC2 da aplicacao)
grafana/provisioning/
  datasources/                # datasource Prometheus
  dashboards/                 # provider: carrega ../dashboards/*.json
  alerting/                   # templates .example p/ Teams (fase futura)
grafana/dashboards/           # JSONs dos dashboards (versionados)
ec2-exporters/                # instalacao do node_exporter na EC2 da aplicacao
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

## node_exporter na EC2 da aplicacao

```bash
cd ec2-exporters && ./install-node-exporter.sh
```

## Migracao para outro host (ex.: EC2 dedicada)

O scrape e pull: na EC2 da aplicacao nada muda alem de uma regra de SG.

1. Provisionar o host novo com Docker + compose; apontar o DNS A do
   `GRAFANA_DOMAIN` para o IP novo (Caddy reemite o certificado sozinho).
2. Clonar o repo e copiar o `.env` (segredos vem do gerenciador de senhas).
3. **Mantendo o historico**: no host antigo, `docker compose stop prometheus grafana`,
   rodar `scripts/backup-volumes.sh`, copiar `backup/` para o host novo e rodar
   `scripts/restore-volumes.sh` la. **Sem historico**: pular; dashboards e alertas
   vem do git, perde-se apenas o TSDB.
4. No SG da EC2 da aplicacao: trocar o /32 antigo pelo IP do host novo nas 3
   portas de metrica (se o host novo estiver na mesma VPC, usar referencia de SG
   e IP privado no lugar de IP publico).
5. `docker compose up -d` no host novo; validar targets up + login no Grafana.
6. `docker compose down` no host antigo.

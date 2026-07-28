# Logs de erro e warning no Grafana (Loki)

**Data:** 2026-07-28 · **Status:** aprovado, aguardando plano de implementacao
**Fase:** 7 do plano de observabilidade (Notion) · **Repos:** pt-observability, EC2 da aplicacao

## Objetivo

Dar ao time acesso aos logs de warning e erro do Performance Tracker pelo Grafana:
buscar, filtrar e alertar sem SSH na EC2.

## Nao-objetivo: isto nao e um Sentry

Decidido explicitamente com o time. Loki entrega **logs pesquisaveis com alerta**.
Nao entrega o fluxo de triagem do Sentry:

| Capacidade | Loki | Sentry |
|---|---|---|
| Buscar e filtrar logs | sim | sim |
| Alertar por padrao/volume | sim | sim |
| Agrupar excecoes iguais num "issue" | **nao** | sim |
| Stack trace com contexto de codigo | **nao** | sim |
| Marcar resolvido / atribuir | **nao** | sim |
| Detectar regressao por release | **nao** | sim |

O time confirmou que precisa de "ver, buscar e ser alertado", nao do fluxo de
triagem. Se a necessidade mudar, a conversa e adotar Sentry de verdade, nao
tentar reconstrui-lo em cima do Loki.

## Estado atual (medido em 2026-07-28, nao presumido)

O plano do Notion afirma que "o backend ja loga JSON estruturado via pino".
**Isso nao corresponde ao codigo em producao.** O que foi medido:

- **200 arquivos** usam `console.error`, **42** usam `console.warn`, apenas
  **15** usam `logger.error` (pino).
- Amostra de 500 linhas: `cron-error.log` tem **9/500** linhas em JSON valido;
  `cron-out.log` tem **0/500**.
- **A semantica esta invertida.** `console.error` vai para stderr e cai em
  `*-error.log`. O pino escreve tudo em stdout, entao `logger.error` cai em
  `*-out.log`, misturado com `console.log`. Filtrar erro por arquivo nao e
  confiavel.
- **Ha PII nos logs.** Exemplo real de `cron-out.log`: nome de empresa,
  `stripeCustomerId`, estado. Dumps de objeto multi-linha.
- **Volume:** 157 MB em `~/.pm2/logs/`; `cron-out` rotaciona a cada 10 MB tres
  vezes ao dia — na ordem de 100 MB/dia.
- Rotacao ja configurada por `pm2-logrotate`: 10 MB, retem 5, comprime.

Decisao do time: **enviar os logs como estao e melhorar depois.** Loki sobe em
dias em vez de semanas; migrar `console.*` para pino fica como trabalho futuro,
sem refazer a infraestrutura.

## Arquitetura

```
EC2 da aplicacao (so saida)             VPS de observabilidade
┌──────────────────────┐                ┌───────────────────────────┐
│ Grafana Alloy        │  HTTPS + auth  │ Caddy :443                │
│ le os 4 logs do PM2  ├───────────────►│  /loki/*  -> loki:3100    │
│ systemd, sem porta   │                │  /*       -> grafana:3000 │
│ inbound nova         │                │                           │
└──────────────────────┘                │ Loki (30d) <- Grafana     │
                                        └───────────────────────────┘
```

### Sem novo registro DNS

O Caddy roteia por prefixo de path no dominio existente. O endpoint de push do
Loki ja e `/loki/api/v1/push`, entao `/loki/*` -> `loki:3100` funciona sem
reescrita e o certificado atual ja cobre. Alternativa descartada: subdominio
`loki.<dominio>`, que exigiria mais um registro A sem ganho.

### Componentes

**Loki** — container novo no compose existente. Storage `filesystem` em volume
nomeado `pt-obs-loki-data` (mesmo padrao dos volumes atuais, para os scripts de
backup/restore continuarem funcionando). Sem porta publicada: so o Caddy e o
Grafana falam com ele pela rede interna do compose.

**Caddy** — nova rota `/loki/*` com `basicauth`, antes do `reverse_proxy` do
Grafana. A credencial e gerada na VPS e nunca entra no git, mesmo fluxo do
`node_exporter`: senha em `secrets/`, hash bcrypt na config.

**Grafana** — datasource Loki provisionado por arquivo, ao lado do Prometheus.

**Alloy** — binario + servico systemd na EC2. Le os arquivos do PM2, aplica o
tratamento de multi-linha, rotula e empurra por HTTPS.

## Decisoes de desenho

### Labels enxutos

Apenas `job` (server/cron), `stream` (out/err) e `host`. Em Loki cada combinacao
de labels vira um stream; rotular por valor variavel (ID, mensagem, rota) explode
a cardinalidade e degrada a consulta. Busca por conteudo e full-text no LogQL,
nao label.

### Excluir arquivos rotacionados

`pm2-logrotate` gera `cron-out__2026-07-27_17-00-42.log`. Um glob `*.log` cru faz
o Alloy tratar cada rotacao como arquivo novo e reingerir o conteudo inteiro —
log duplicado e disco desperdicado. O glob **deve** excluir `*__*`.

### Multi-linha e best-effort

Dumps de objeto ocupam varias linhas. O Alloy junta continuacoes na entrada
anterior usando a forma observada nos logs reais:

- **Nova entrada**: linha comecando com `[` (prefixo `[modulo]` do `console.*`)
  ou com `{"` (linha JSON do pino, que e sempre unica).
- **Continuacao**: linha comecando com espaco/tab (campos indentados do dump) ou
  com `}` (fechamento do objeto).

Acerta a maioria, nao todas — uma mensagem de `console.error` sem prefixo `[` e
sem indentacao vira entrada propria. E a consequencia aceita de enviar log nao
estruturado, e some quando a migracao para pino acontecer.

### Autenticacao no push

Basic auth + TLS no endpoint. Sem isso qualquer um na internet injeta log falso
no Grafana do time — inclusive apagando sinal real com ruido.

## Seguranca e retencao

**Retencao: 30 dias.** Cobre investigacao de incidente sem virar arquivo de dados
de cliente. ~100 MB/dia bruto, comprimido pelo Loki fica bem abaixo de 3 GB em
30 dias — irrelevante nos 184 GB livres da VPS.

**Risco aceito pelo time: conta admin unica.** Os logs contem PII e o Grafana
esta exposto na internet com uma unica credencial compartilhada. Consequencia
explicita: **nao ha como saber quem consultou dado de cliente**, e a senha
circula no time. O time optou por isso em favor de velocidade operacional.
Mitigacao futura: usuarios nominais com papel Viewer, admin so para
administracao.

**PII nao e filtrada antes do envio.** Descartado porque regex sobre texto nao
estruturado erra nos dois sentidos e remove justamente o contexto necessario para
depurar. Revisar quando os logs forem estruturados.

## Escopo do que e enviado

Enviados: `server-out.log`, `server-error.log`, `cron-out.log`,
`cron-error.log`.

Nao enviados: arquivos rotacionados (`*__*.log`) e `bizopsmcp-*.log` (500 bytes,
sem escrita desde maio).

## Dependencia da Fase 6

Ver e buscar log funciona assim que isto subir. **Notificacao nao.** Alertas
precisam do contact point do Microsoft Teams, que aguarda aprovacao do manager
(Fase 6). Ate la a regra de alerta pode existir e ficar vermelha na tela, mas
ninguem recebe mensagem.

## Criterios de aceite

- [ ] Loki respondendo, datasource visivel no Grafana
- [ ] `/loki/api/v1/push` exige credencial: 401 sem, 204 com
- [ ] Alloy ativo na EC2, sem porta alcancavel de fora (validar com `ss -tlnp`).
      O Alloy abre a UI dele em `127.0.0.1:12345`; loopback e aceitavel, o que
      nao pode e bind em `0.0.0.0` ou num IP de interface. Verificado tambem
      pelo IP publico e pelo IP privado da VPC — inalcancavel nos dois.
- [ ] Log das ultimas horas dos 4 arquivos consultavel no Explore do Grafana
- [ ] Um erro provocado de proposito aparece no Grafana em menos de 30s
- [ ] Nenhuma reingestao de arquivo rotacionado (contagem estavel apos rotacao)
- [ ] Painel de log de erro no dashboard, correlacionado com restarts e latencia
- [ ] Retencao de 30d configurada e verificada na config aplicada
- [ ] Config do Alloy versionada neste repo (regra de ouro: nada so no servidor)

## Trabalho futuro (fora desta spec)

1. Migrar `console.error`/`console.warn` para pino, comecando pelos modulos que
   mais falham. Destrava filtro real por nivel, stack integro e fim da
   heuristica de multi-linha.
2. Usuarios nominais no Grafana, para rastrear acesso a PII.
3. Regras de alerta sobre log, quando a Fase 6 for aprovada.

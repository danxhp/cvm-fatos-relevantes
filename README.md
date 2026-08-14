# cvm-fatos-relevantes

Monitora, na CVM, várias categorias de documentos de empresas brasileiras listadas
(setor saúde/educação/telecom) — **Fato Relevante**, **Aviso aos Acionistas**,
**Comunicado ao Mercado**, **ITR - Informações Trimestrais** e **Dados Econômico-Financeiros** —
e formulários equivalentes (6-K/20-F/8-K) na SEC para emissoras listadas no exterior (ex.: Afya).
Gera **resumos automáticos com Claude** e envia **alertas no Telegram** assim que um novo documento
é publicado. (O envio por **email** existe no código, mas está **desativado** — `enabled: false`
no `email_config.json`; basta voltar a `true` e re-subir o secret para reativar.)

- Canais de alerta: **Telegram** (ativo, múltiplos destinos) e **email** (desativado). Ambos usam
  o mesmo `email_config.json`.
- Categorias monitoradas: definidas em `CVM_ALLOWED_CATEGORIES` no script (buscamos **todas** as
  categorias numa requisição e filtramos por nome — para incluir/remover, edite esse conjunto).
- Fonte CVM: endpoint interno do RAD/CVM (`frmConsultaExternaCVM.aspx/ListarDocumentos`)
- Fonte SEC: API oficial `data.sec.gov`
- Deduplicação por protocolo/accession — silencioso quando não há nada novo
- Roda 24/7 no **GitHub Actions**, disparado de fora para não depender do cron interno do GitHub

---

## Sumário

- [Como funciona](#como-funciona)
- [Arquivos](#arquivos)
- [Uso local](#uso-local)
- [Configuração (`email_config.json`)](#configuração-email_configjson)
- [Deploy online no GitHub Actions](#deploy-online-no-github-actions) ← **o coração da operação**
  - [1. O problema do cron do GitHub](#1-o-problema-do-cron-do-github)
  - [2. Disparo externo confiável (cron-job.org → workflow_dispatch)](#2-disparo-externo-confiável-cron-joborg--workflow_dispatch)
  - [3. Heartbeat / dead-man's-switch (healthchecks.io)](#3-heartbeat--dead-mans-switch-healthchecksio)
  - [4. Secrets necessários](#4-secrets-necessários)
- [Operação: como saber se está vivo](#operação-como-saber-se-está-vivo)
- [Reaproveitar este método em outros monitores](#reaproveitar-este-método-em-outros-monitores)
- [Watcher pontual de resultados (`earnings_watch.py`)](#watcher-pontual-de-resultados-earnings_watchpy)

---

## Como funciona

```
cron-job.org (a cada 15 min)
      │  POST autenticado → workflow_dispatch
      ▼
GitHub Actions  (.github/workflows/monitor.yml)
      │  roda cvm_fatos_relevantes_claude.py
      ▼
CVM (endpoint interno) + SEC (API oficial)
      │  lista documentos recentes das empresas monitoradas
      ▼
Deduplica contra seen_protocols.json
      │  o que é novo →
      ▼
Claude (Haiku) resume o documento em bullets
      ▼
Telegram (um bot por destino)   ·   email (SMTP/Brevo) desativado (enabled=false)
      │
      └── ao final: ping no healthchecks.io ("estou vivo")
```

O estado (`seen_protocols.json`) é commitado de volta ao repositório pelo próprio Actions,
para sobreviver entre execuções.

---

## Arquivos

| Arquivo | Papel |
|---|---|
| `cvm_fatos_relevantes_claude.py` | O monitor. Toda a lógica está aqui. |
| `.github/workflows/monitor.yml` | Workflow do GitHub Actions que executa o monitor. |
| `seen_protocols.json` | "Memória" do que já foi visto. **Autoritativo no GitHub** (o Actions commita de volta). |
| `send_log.txt` | Registro **durável** de cada envio (email/telegram), separado por ` \| `. Versionado e commitado de volta pelo Actions. |
| `fatos_arquivo.csv` | **Base histórica** dos fatos relevantes/avisos detectados (`data, ticker, empresa, categoria, assunto, protocolo, link` — sem resumo). CSV, versionado e commitado de volta. Acumula a partir de 2026-07-26. |
| `email_config.json` | Credenciais SMTP, chave da Anthropic, destinatários, URL do heartbeat, bloco Telegram. **Email desativado** (`enabled: false`). **Não versionado** (`.gitignore`); na nuvem é recriado do secret. |
| `monitor_log.txt` | Log de execução local (texto livre, verboso). **Efêmero na nuvem** — não versionado. |
| `earnings_watch.py` | **Watcher pontual de resultados** — standalone, roda local, reaproveita o fetch/parse do monitor. Vigia tickers (CVM + SEC) de minuto a minuto e avisa no **Telegram + pop-up + voz** na tela. Ver [seção dedicada](#watcher-pontual-de-resultados-earnings_watchpy). |
| `earnings_watch_seen.json` / `earnings_watch_log.txt` | Estado (protocolos já entregues) e log do watcher. **Não versionados** (`.gitignore`). |
| `requirements.txt` | Dependências Python. |

---

## Uso local

```bash
pip install -r requirements.txt

python cvm_fatos_relevantes_claude.py            # execução silenciosa (modo cron)
python cvm_fatos_relevantes_claude.py --once     # execução manual com status na tela
python cvm_fatos_relevantes_claude.py --test-telegram # Telegram de teste para todos os destinos
python cvm_fatos_relevantes_claude.py --test-email   # email de teste (só se enabled=true)
python cvm_fatos_relevantes_claude.py --bootstrap    # marca tudo que existe hoje como "visto" (não alerta)
```

> No Windows, use `py` no lugar de `python` se o alias `python` abrir a Microsoft Store.

Rode `--bootstrap` uma vez ao configurar um ambiente novo, para não ser inundado com alertas
de documentos antigos.

---

## Configuração (`email_config.json`)

Todo o segredo do monitor mora aqui: SMTP (email), chave da Anthropic, `healthcheck_url` e o bloco
`telegram`. **Neste deploy o email está desativado** (`enabled: false`) — só o Telegram envia; para
reativar o email, ponha `true` e re-suba o secret. Na primeira execução um template é criado
automaticamente. Estrutura:

```json
{
  "enabled": false,
  "smtp_host": "smtp-relay.brevo.com",
  "smtp_port": 587,
  "smtp_username": "SEU_USUARIO_SMTP",
  "smtp_password": "SUA_SENHA_SMTP",
  "from_addr": "remetente@exemplo.com",
  "to_addrs": ["dest1@exemplo.com", "dest2@exemplo.com"],
  "subject_prefix": "[Fato Relevante]",
  "anthropic_api_key": "sk-ant-...",
  "healthcheck_url": "https://hc-ping.com/SEU-UUID",
  "telegram": {
    "enabled": true,
    "destinations": [
      {"bot_token": "111:AA...", "chat_id": 123},
      {"bot_token": "222:BB...", "chat_id": 456}
    ]
  }
}
```

| Campo | Descrição |
|---|---|
| `enabled` | `false` desliga o envio de email por completo. |
| `smtp_*` | Credenciais do relay SMTP (aqui: Brevo). |
| `from_addr` / `to_addrs` | Remetente e lista de destinatários. |
| `anthropic_api_key` | Chave da API da Anthropic para os resumos. Sem ela, os emails saem sem o resumo. |
| `healthcheck_url` | URL de ping do heartbeat (ver [seção 3](#3-heartbeat--dead-mans-switch-healthchecksio)). Vazio = desativado. |
| `telegram` | Alerta paralelo no Telegram (ver abaixo). `enabled: false` ou bloco ausente = desativado. |

> `anthropic_api_key` e `healthcheck_url` também podem vir das variáveis de ambiente
> `ANTHROPIC_API_KEY` e `HEALTHCHECK_URL`; o Telegram, de `TELEGRAM_BOT_TOKEN` /
> `TELEGRAM_CHAT_ID` (todas têm prioridade sobre o arquivo).

### Alertas no Telegram (opcional)

O Telegram é o **canal ativo** hoje: cada documento novo é enviado para um ou mais destinos, cada um
um par **bot + chat** próprio (`destinations`) — então pessoas diferentes recebem pelo **seu próprio
bot**. É o resumo (bullets do Claude), formatado, com link para o documento.

1. Crie um bot com o [@BotFather](https://t.me/BotFather) e copie o **bot token**.
2. Descubra o **chat_id** de destino: o destinatário manda **qualquer mensagem ao bot primeiro**
   (bots não podem iniciar conversa), depois consulte
   `https://api.telegram.org/bot<TOKEN>/getUpdates` e pegue o `message.chat.id`.
   Chat privado = id positivo; grupo/canal = id negativo.
3. Adicione um objeto `{"bot_token": "...", "chat_id": ...}` por destino em `telegram.destinations`
   e teste (envia para **todos** os destinos):

> Formato antigo `{"bot_token": "...", "chat_id": ...}` (destino único) ainda é aceito.

```bash
python cvm_fatos_relevantes_claude.py --test-telegram
```

---

## Deploy online no GitHub Actions

Esta é a parte que faz o monitor rodar sozinho 24/7. São três peças:
**(A) o workflow**, **(B) o disparo externo confiável**, **(C) o heartbeat**.

### 1. O problema do cron do GitHub

O jeito "óbvio" é usar o gatilho `schedule:` do próprio GitHub Actions. **Não confie nele para
cadência curta.** O cron interno do GitHub é *best-effort*: em horários de pico ele **atrasa ou
simplesmente pula a execução**, sem gerar erro. Na prática, um `schedule` de 15 min chega a rodar
apenas ~1 a cada 6 vezes, com buracos de horas. Isso vale para repositório público e privado, e
**não melhora em planos pagos** — é uma limitação da plataforma, não da sua conta.

Solução: **manter o Actions como executor, mas tirar o agendamento de dentro dele.** Quem puxa o
gatilho passa a ser um serviço de cron externo confiável, via `workflow_dispatch`.

O workflow já expõe os dois gatilhos ([`.github/workflows/monitor.yml`](.github/workflows/monitor.yml)):

```yaml
on:
  schedule:
    - cron: '7,22,37,52 * * * *'   # backup — pode disparar de vez em quando, inofensivo (dedup)
  workflow_dispatch:                # gatilho manual/externo — é o caminho principal
```

> Deixar o `schedule` ligado como backup não custa nada: se o cron-job.org e o GitHub dispararem
> juntos, a `concurrency group` enfileira e o `seen_protocols.json` deduplica — ninguém recebe
> alerta repetido.

### 2. Disparo externo confiável (cron-job.org → workflow_dispatch)

**a) Crie um token de acesso (fine-grained PAT) no GitHub**

GitHub → *Settings → Developer settings → Personal access tokens → Fine-grained tokens →
Generate new token*:

- **Repository access:** *Only select repositories* → este repositório
- **Permissions → Repository → Actions:** `Read and write`
- **Expiration:** o mais longo possível (⚠️ quando o token expira, o disparo silenciosamente para
  — o heartbeat da seção 3 é o que te avisa disso)

Copie o token (`github_pat_...`).

**b) Teste o token** (deve responder `HTTP 204`):

```bash
curl -i -X POST \
  -H "Authorization: Bearer SEU_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/USUARIO/REPO/actions/workflows/monitor.yml/dispatches \
  -d '{"ref":"main"}'
```

**c) Configure o job no [cron-job.org](https://cron-job.org)** (grátis, dispara no minuto certo —
ao contrário do cron do GitHub, ele *não* pula execuções):

Atenção à interface: **URL, Request method e Request body são campos do formulário**; a caixa
**Headers** é só para cabeçalhos HTTP. Não misture.

| Onde | Valor |
|---|---|
| **URL** (campo Address) | `https://api.github.com/repos/USUARIO/REPO/actions/workflows/monitor.yml/dispatches` |
| **Request method** | `POST` |
| **Request body** | `{"ref":"main"}` |
| **Execution schedule** | a cada 15 min (`*/15`) |

Na seção **Headers**, adicione exatamente estas 4 linhas (Key → Value):

| Key | Value |
|---|---|
| `Authorization` | `Bearer github_pat_...` (o token **inteiro**, com o prefixo `Bearer `) |
| `Accept` | `application/vnd.github+json` |
| `X-GitHub-Api-Version` | `2022-11-28` |
| `Content-Type` | `application/json` |

O GitHub responde **204 No Content** em sucesso, que o cron-job.org já trata como ✅.
Use o botão **Test run** para validar antes de salvar. Confirme que o job está **Enabled** e salvo.

### 3. Heartbeat / dead-man's-switch (healthchecks.io)

O disparo externo resolve a *cadência*, mas cria pontos cegos: o cron-job.org pode cair, o token
pode expirar, ou a CVM pode ficar fora do ar — e nesse último caso **o run fica "verde" mesmo sem
ter checado nada** (o código captura o erro de fetch e segue). Em todos esses casos você
simplesmente para de receber alertas, sem saber se é "não teve fato relevante" ou "está quebrado".

O heartbeat inverte a lógica: em vez de esperar o sistema quebrado avisar que quebrou, um vigia
**externo** alerta você quando o monitor **para de dar sinal de vida**.

Como está implementado no código (ao final de [`main()`](cvm_fatos_relevantes_claude.py)):
- **CVM respondeu** (`cvm_ok`) → pinga a URL base (sinal de vida);
- **CVM falhou** (transitório) → **não pinga**. Um ping perdido é absorvido pela *grace*;
  só uma queda **sustentada** (vários runs seguidos sem pingar) ou um crash disparam o alerta;
- se o script travar antes disso, **nenhum ping é enviado** → o watchdog alerta pelo silêncio;
- sem `healthcheck_url` configurado, é um **no-op** (não quebra nada).

> **Não use `/fail`** para falhas transitórias da CVM: o endpoint da CVM é instável e se recupera
> no run seguinte; um `/fail` derruba o check na hora e gera *flapping* (enxurrada de emails
> "down/up"). Por isso o design é "pinga no sucesso, silencia na falha".

**Configuração do Period/Grace — precisa casar com a cadência real, senão gera flapping.**
O ping chega a cada ~5 min (a cadência do cron), mas com folga: runs que processam um fato
relevante demoram mais (download + resumo do Claude + envio), e "pular" um ping numa falha
transitória cria um intervalo de ~2 ciclos. Regra: **`Period + Grace` deve cobrir ~2–3 ciclos.**

1. Crie conta grátis em [healthchecks.io](https://healthchecks.io).
2. **Add Check** → **Period = 5 min** (igual ao cron), **Grace = 10 min**
   (tolera jitter, runs longos e 1–2 falhas transitórias sem alarme falso).
3. Copie a **ping URL** (`https://hc-ping.com/<uuid>`) → coloque em `healthcheck_url`
   (no `email_config.json` local **e** no secret — ver abaixo).
4. Por padrão os alertas vão para o email do cadastro (⚠️ *o email da conta healthchecks.io, que
   pode ser diferente do de destino dos fatos relevantes*). **Roteie para o Telegram** — ver abaixo.

#### 3.1 Alerta do heartbeat no Telegram (não só email)

Um watchdog que avisa por um canal que você não olha não é watchdog. Como o email está desativado
(`enabled: false`) e a operação toda vive no Telegram, o alerta de "monitor caiu" tem que chegar
**no mesmo lugar onde chegam os fatos relevantes**.

Em vez da integração nativa do healthchecks (que usa o bot *deles*, num chat novo), use a
integração **Webhook** apontando para o **seu** bot — assim o alerta cai no chat que você já lê.

No healthchecks.io: **Integrations** → **Add Integration** → **Webhook**, e preencha os
**dois** blocos (down e up):

| Campo | Valor |
|---|---|
| Request method | `POST` |
| URL (ambos) | `https://api.telegram.org/bot<BOT_TOKEN>/sendMessage` |
| Request header | `Content-Type: application/json` |

**Body — "when a check goes down":**
```json
{"chat_id":"<CHAT_ID>","text":"🔴 MONITOR CVM FORA DO AR\n\nSem sinal de vida desde $NOW.\nFatos relevantes podem estar passando sem alerta.\n\nActions: https://github.com/danxhp/cvm-fatos-relevantes/actions","disable_web_page_preview":true}
```

**Body — "when a check goes up":**
```json
{"chat_id":"<CHAT_ID>","text":"✅ MONITOR CVM NORMALIZADO\n\nVoltou a reportar em $NOW."}
```

> ⚠️ `<BOT_TOKEN>` e `<CHAT_ID>` são os mesmos do bloco `telegram` do `email_config.json` — pegue
> de lá e cole **só no painel do healthchecks**. **Nunca** commite o token: este repositório é
> **público**.

`$NOW` é uma variável do healthchecks (também existem `$NAME`, `$STATUS`, `$CODE`). Evite `$NAME`
dentro do JSON: se o nome do check tiver aspas, quebra o payload.

Para alertar mais de um destino, crie **uma integração Webhook por `chat_id`**. Na prática,
alerta operacional só interessa a quem mantém o sistema — os demais destinatários querem os fatos
relevantes, não o ruído de infraestrutura.

### 4. Secrets necessários

O workflow monta o `email_config.json` a partir de um único secret, roda o monitor e depois
**apaga o arquivo** (para não commitar credenciais):

```yaml
- name: Write email_config.json from secret
  env:
    EMAIL_CONFIG: ${{ secrets.EMAIL_CONFIG_JSON }}
  run: printf '%s' "$EMAIL_CONFIG" > email_config.json
```

Portanto, **tudo que precisa existir na nuvem tem que estar dentro do secret `EMAIL_CONFIG_JSON`**
— inclusive o `healthcheck_url`. Para atualizar o secret a partir do arquivo local:

```bash
gh secret set EMAIL_CONFIG_JSON < email_config.json
```

> O secret é *write-only*: você não consegue lê-lo depois para comparar. Se rotacionar alguma
> credencial só na nuvem, lembre de refletir no arquivo local antes do próximo `gh secret set`.

**Resumo dos secrets:**

| Secret | Conteúdo |
|---|---|
| `EMAIL_CONFIG_JSON` | O `email_config.json` inteiro (SMTP + chave Anthropic + destinatários + `healthcheck_url`). |

---

## Operação: como saber se está vivo

- **Execuções:** aba *Actions* do repositório, ou `gh run list --workflow=monitor.yml`.
- **Cadência real:** compare os horários dos runs — devem estar espaçados ~5 min (os disparos do
  cron-job.org). Runs `event=schedule` esparsos são o backup do GitHub; o principal é
  `event=workflow_dispatch`.
- **Heartbeat:** o painel do healthchecks.io mostra o último ping; se ficar vermelho, você recebe
  o alerta por email — é o seu sinal de que algo na cadeia travou.
- **Histórico de envios:** [`send_log.txt`](send_log.txt) tem uma linha por envio (email e
  telegram separados), com `status` `OK`/`FAIL` e detalhe do erro quando falha. É durável (o
  Actions commita de volta), então serve de auditoria: `2026-... | telegram | OK | RDOR3 | ...`.
- **Base histórica de FRs:** [`fatos_arquivo.csv`](fatos_arquivo.csv) acumula todo fato
  relevante/aviso detectado (`data, ticker, empresa, categoria, assunto, protocolo, link`). Abre
  no Excel/Sheets — dá para filtrar por empresa, contar por período, etc. Só metadados + título,
  sem o resumo do Claude.
- **Falha silenciosa da CVM:** se a CVM estiver fora, o run passa "verde" mas o heartbeat **não
  pinga**. Uma falha isolada é absorvida pela *grace*; se a CVM ficar fora por vários runs, os
  pings cessam e o healthchecks.io te alerta pelo silêncio.

---

## Reaproveitar este método em outros monitores

O padrão **disparo externo + heartbeat** é genérico e se aplica a qualquer job agendado no GitHub
Actions. Veja o playbook portátil em
**[`docs/reliable-github-actions.md`](docs/reliable-github-actions.md)** — passo a passo para
levar qualquer repositório com `schedule:` para uma cadência confiável e observável.

---

## Watcher pontual de resultados (`earnings_watch.py`)

Ferramenta **standalone** para dias de divulgação de resultados: vigia um conjunto de tickers,
**de minuto a minuto**, avisa **só no Telegram** (não manda email) e ainda dá **pop-up + voz** na
tela (Windows). Roda **local** (não no GitHub Actions) e reaproveita o fetch/parse do monitor
principal (`import cvm_fatos_relevantes_claude`).

Já foi usado nos resultados do **2T26** (FLRY3/HYPE3, BLAU3 e o lote de saúde/educação).

### Comportamento

- **Avisa no primeiro documento de CADA empresa (um por empresa):** assim que sai o **primeiro**
  documento de resultados de uma empresa — de qualquer categoria em `WATCH_CATEGORIES` (ex.: *Dados
  Econômico-Financeiros* + *ITR*), o que vier primeiro (DF, ITR ou release) — dispara **som + pop-up
  + Telegram** e marca a empresa como pronta (não repete os docs seguintes dela). **Encerra quando
  todas** saírem, ou à meia-noite (horário local) com um resumo do que saiu / faltou.
- **Só documentos de hoje:** considera apenas docs com data de hoje — inclusive os já publicados
  quando o watcher começou (então um start "atrasado" no mesmo dia ainda entrega o que saiu antes).
- **CVM + SEC/EDGAR:** tickers na B3 vêm da CVM (filtrados por `WATCH_CATEGORIES`); tickers com
  `sec_cik` (ex.: **AFYA**, na NASDAQ) vêm via **6-K/20-F na SEC**. Datas `dd/mm/aaaa` (CVM) e ISO
  (SEC) são ambas reconhecidas.
- **Dedup por (protocolo, chat), com estado persistido** (`earnings_watch_seen.json`): reiniciar
  **não reenvia** o já mandado (sobrevive a crash/restart); uma falha de envio a um destino é
  **retentada** no minuto seguinte só para quem faltou (só marca entregue no sucesso).
- **Destinos = `email_config.json`:** envia para **todos** os `telegram.destinations` (os mesmos do
  monitor principal), cada pessoa pelo seu próprio bot. Nunca envia email.
- **Pop-up na tela (Windows):** `MessageBox` centralizado (1x por empresa + um na inicialização,
  como confirmação). Roda em thread — não trava o loop. Aparece na sessão interativa (por isso o
  agendamento usa `LogonType Interactive`).
- **Alerta de voz (Windows):** bips + **fala "saiu &lt;nome&gt;" 3x** (TTS via SAPI, sem dependência
  extra; nome por ticker em `SPEAK_NAMES`). Uma vez por empresa, em thread.

### Uso

```bash
py earnings_watch.py            # inicia o watcher (roda até meia-noite ou até todos saírem)
py earnings_watch.py --once     # uma checagem só: lista o estado atual, NÃO envia nada
py earnings_watch.py --test     # manda um Telegram de teste para TODOS os destinos e sai
```

### Reaproveitar no próximo trimestre

Edite o bloco de config no topo do script e rode de novo. Quem recebe é controlado pelo
`email_config.json` (`telegram.destinations`), não pelo script.

```python
WATCH_TICKERS    = {"YDUQ3", "DASA3", "AFYA"}       # tickers (CVM e/ou SEC via sec_cik)
WATCH_CATEGORIES = {"Dados Econômico-Financeiros", "ITR - Informações Trimestrais"}  # 1º doc de qualquer uma
WATCH_LABEL      = "Resultados 2T26"                # texto do cabeçalho da mensagem
SPEAK_NAMES      = {"YDUQ3": "Yduqs", "DASA3": "Dasa", "AFYA": "Afya"}  # falado: "saiu <nome>"
POLL_SECONDS     = 60                               # de minuto a minuto
```

> Antes de um novo alvo, zere o estado: `echo [] > earnings_watch_seen.json`.

### Agendar para um dia específico (Windows)

O watcher roda até a meia-noite **do dia em que começa**, então para um resultado futuro agende o
início via Task Scheduler (ou só rode o script na hora). Exemplo — início às 17:00 (after-market):

```powershell
$action  = New-ScheduledTaskAction -Execute 'C:\Windows\py.exe' -Argument 'C:\dev\GS\CVM_Fatos_relevantes\earnings_watch.py' -WorkingDirectory 'C:\dev\GS\CVM_Fatos_relevantes'
$trigger = New-ScheduledTaskTrigger -Once -At '2026-08-13T17:00:00'
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 9)
Register-ScheduledTask -TaskName 'EarningsWatch_2T26' -Action $action -Trigger $trigger -Settings $settings -RunLevel Limited -Force
```

O PC precisa estar ligado ou suspenso (a tarefa o acorda) — não desligado. Consultar / rodar já /
remover: `Get-ScheduledTask EarningsWatch_2T26` · `Start-ScheduledTask EarningsWatch_2T26` ·
`Unregister-ScheduledTask EarningsWatch_2T26 -Confirm:$false`.

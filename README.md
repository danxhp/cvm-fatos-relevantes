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
- [Cobertura por fonte (e o que ela não cobre)](#cobertura-por-fonte-e-o-que-ela-não-cobre)
- [Arquivos](#arquivos)
- [Uso local](#uso-local)
- [Configuração (`email_config.json`)](#configuração-email_configjson)
- [Deploy online no GitHub Actions](#deploy-online-no-github-actions) ← **o coração da operação**
  - [1. O problema do cron do GitHub](#1-o-problema-do-cron-do-github)
  - [2. Disparo externo confiável (cron-job.org → workflow_dispatch)](#2-disparo-externo-confiável-cron-joborg--workflow_dispatch)
  - [3. Heartbeat / dead-man's-switch (healthchecks.io)](#3-heartbeat--dead-mans-switch-healthchecksio)
  - [4. Secrets necessários](#4-secrets-necessários)
- [Recompra mensal da AMX (`amx_recompra.py`)](#recompra-mensal-da-amx-amx_recomprapy)
- [Informe mensal de insiders e controlador](#informe-mensal-de-insiders-e-controlador)
- [Por que o alerta traz DOIS links](#por-que-o-alerta-traz-dois-links)
- [Falhas silenciosas: o que o heartbeat NÃO vê](#falhas-silenciosas-o-que-o-heartbeat-não-vê)
- [Operação: como saber se está vivo](#operação-como-saber-se-está-vivo)
- [Reaproveitar este método em outros monitores](#reaproveitar-este-método-em-outros-monitores)
- [Watcher pontual de resultados (`earnings_watch.py`)](#watcher-pontual-de-resultados-earnings_watchpy)

---

## Como funciona

```
cron-job.org (a cada 5 min)
      │  POST autenticado → workflow_dispatch
      ▼
GitHub Actions  (.github/workflows/monitor.yml)
      │  roda cvm_fatos_relevantes_claude.py
      ▼
CVM (endpoint interno) + SEC (API oficial)
      │  lista documentos recentes das empresas monitoradas
      │  → sanidade do payload (raw_response_is_sane): resposta vazia ou
      │    fora do layout = falha, NÃO "não teve fato relevante"
      ▼
Deduplica contra seen_protocols.json
      │  o que é novo →
      ▼
Claude (Haiku) resume o documento em bullets
      ▼
Telegram (um bot por destino)   ·   email (SMTP/Brevo) desativado (enabled=false)
      │  → só o que foi CONFIRMADO entra em seen_protocols.json;
      │    o resto fica em pending_deliveries.json e é retentado
      ▼
ping no healthchecks.io ("estou vivo") — só se a CVM respondeu de forma sã
```

O estado (`seen_protocols.json` e `pending_deliveries.json`) é commitado de volta ao repositório
pelo próprio Actions, para sobreviver entre execuções.

> Quem **executa** é sempre o GitHub Actions. O cron-job.org não roda código nenhum: ele só toca a
> campainha (`workflow_dispatch`). Isso importa na hora de diagnosticar — ver
> [seção 1](#1-o-problema-do-cron-do-github).

---

## Cobertura por fonte (e o que ela não cobre)

| Fonte | Empresas | O que chega |
|---|---|---|
| **CVM / RAD** | 15 tickers B3 | Fato Relevante, Aviso aos Acionistas, Comunicado ao Mercado, ITR, Dados Econômico-Financeiros |
| **SEC / EDGAR** | `AFYA`, `AMX` | 6-K, 20-F, 8-K (e as versões `/A`) — ver `SEC_FORMS_OF_INTEREST` |

| **BMV** (bolsa mexicana) | `AMX` | Recompra de ações, mensal — workflow separado, ver [`amx_recompra.py`](#recompra-mensal-da-amx-amx_recomprapy) |

A América Móvil entra pela SEC como emissora estrangeira com ADR na NYSE (CIK `0001129137`) — o
mesmo caminho da Afya, sem código novo. Isso cobre **resultados e eventos materiais** (6-K, 20-F).

> ⚠️ **Recompra da AMX não sai pela SEC.** A divulgação de recompra é exigência mexicana
> (Circular Única de Emisoras) e vai para a **BMV**, num relatório por dia de operação. Não vira
> 6-K — a AMX protocola de 9 a 29 6-K por ano, nunca em cadência diária — e o 20-F só traz a
> tabela mensal uma vez por ano, com até 16 meses de defasagem. Por isso a recompra tem
> **fonte e workflow próprios**.

---

## Arquivos

| Arquivo | Papel |
|---|---|
| `cvm_fatos_relevantes_claude.py` | O monitor. Toda a lógica está aqui. |
| `.github/workflows/monitor.yml` | Workflow do GitHub Actions que executa o monitor. |
| `seen_protocols.json` | "Memória" do que já foi **entregue** (não do que foi detectado — ver [Falhas silenciosas](#falhas-silenciosas-o-que-o-heartbeat-não-vê)). **Autoritativo no GitHub** (o Actions commita de volta). |
| `pending_deliveries.json` | Fatos detectados e **ainda não entregues** em todos os destinos: tentativas, destinos já confirmados e o resumo do Claude já gerado. Normalmente `{}`. Versionado e commitado de volta — sem isso o contador de tentativas zeraria a cada run na nuvem. |
| `send_log.txt` | Registro **durável** de cada envio (email/telegram), separado por ` \| `. Versionado e commitado de volta pelo Actions. |
| `fatos_arquivo.csv` | **Base histórica** dos fatos relevantes/avisos detectados (`data, ticker, empresa, categoria, assunto, protocolo, link` — sem resumo). CSV, versionado e commitado de volta. Acumula a partir de 2026-07-26. |
| `email_config.json` | Credenciais SMTP, chave da Anthropic, destinatários, URL do heartbeat, bloco Telegram. **Email desativado** (`enabled: false`). **Não versionado** (`.gitignore`); na nuvem é recriado do secret. |
| `monitor_log.txt` | Log de execução local (texto livre, verboso). **Efêmero na nuvem** — não versionado. |
| `earnings_watch.py` | **Watcher pontual de resultados** — standalone, roda local, reaproveita o fetch/parse do monitor. Vigia tickers (CVM + SEC) de minuto a minuto e avisa no **Telegram + pop-up + voz** na tela. Ver [seção dedicada](#watcher-pontual-de-resultados-earnings_watchpy). |
| `earnings_watch_seen.json` / `earnings_watch_log.txt` | Estado (protocolos já entregues) e log do watcher. **Não versionados** (`.gitignore`). |
| `amx_recompra.py` | **Recompra mensal da AMX** via BMV. Workflow próprio (mensal), fonte própria. Ver [seção dedicada](#recompra-mensal-da-amx-amx_recomprapy). |
| `.github/workflows/amx_recompra.yml` | Workflow mensal (`cron: 10 13 2 * *`) do relatório de recompra. |
| `amx_recompra_state.json` | Último fechamento mensal da AMX (id, data, remanente, tesouraria). Versionado — é a base de comparação do mês seguinte, sem ele o cálculo exige varredura profunda. |
| `amx_recompra_log.txt` | Log da varredura de IDs na BMV. **Não versionado.** |
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
simplesmente pula a execução**, sem gerar erro. Isso vale para repositório público e privado, e
**não melhora em planos pagos** — é uma limitação da plataforma, não da sua conta.

O ponto importante é *qual* das duas coisas falha: o Actions é excelente **executando** e ruim
**acordando na hora**. Medição de 14/08/2026, 200 runs numa janela de 15,5h:

| | Disparo externo (`workflow_dispatch`) | `schedule` nativo do GitHub |
|---|---|---|
| Atraso | ~3 a 7 s | **1min40s a 14min40s** (mediana ~9 min) |
| Gap mediano entre runs | **300 s** (exatos 5 min) | — |
| Conclusão | 200/200 `success` | 200/200 `success` |

Ou seja: a execução nunca foi o problema. Por isso o `schedule` continua ligado como rede de
segurança de *"eventualmente"*, mas **não** como garantia de latência.

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
| **Execution schedule** | a cada 5 min (`*/5`) |

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

No healthchecks.io: **Integrations** → **Add Integration** → **Webhook**. O formulário tem dois
blocos independentes — *"Execute when a check goes **down**"* e *"...goes **up**"* — e você
preenche **os dois**.

> ⚠️ **Primeiro mude o dropdown de `GET` para `POST`, nos dois lados.** Enquanto estiver em `GET`
> o formulário mostra só *URL* e *Request Headers*; o campo **Request Body** só aparece depois de
> trocar o método. É o passo que trava todo mundo.

| Campo | Valor |
|---|---|
| Name | `Check Telegram CVM Fatos Relevantes` (livre) |
| Método (ambos os lados) | **`POST`** ← trocar antes de tudo |
| URL (ambos os lados) | `https://api.telegram.org/bot<BOT_TOKEN>/sendMessage` |
| Request Headers (ambos) | `Content-Type: application/json` |

**Body — "when a check goes down":**
```json
{"chat_id":"<CHAT_ID>","text":"🔴 MONITOR CVM FORA DO AR\n\nParou de dar sinal de vida — fatos relevantes podem estar passando sem alerta.\n\nActions: https://github.com/danxhp/cvm-fatos-relevantes/actions","disable_web_page_preview":true}
```

**Body — "when a check goes up":**
```json
{"chat_id":"<CHAT_ID>","text":"✅ MONITOR CVM NORMALIZADO\n\nVoltou a reportar normalmente."}
```

**Por que não tem horário na mensagem.** Tentador colocar `$NOW`, mas ele erra duas vezes:

- **Fuso.** `$NOW` é a hora **UTC** em ISO 8601 (`2026-08-14T20:02:02+00:00`) — 3h à frente de
  Brasília e num formato ilegível. Os placeholders do healthchecks são substituição literal de
  string, não um template engine: **não há formatação de data nem conversão de fuso**, e muito
  menos lógica de "ontem/hoje".
- **Semântica.** `$NOW` é a hora em que o **alerta dispara**, não a hora em que o monitor morreu.
  O check só vira `down` depois de `Period + Grace` (~15 min) de silêncio — então "sem sinal desde
  $NOW" na verdade queria dizer "desde agora", errando ~15 min *além* das 3h de fuso.

O **Telegram já resolve isso de graça**: cada mensagem carrega o horário no seu fuso local, e o
app agrupa a conversa sob separadores **"Hoje" / "Ontem" / "14 de agosto de 2026"**. É exatamente
o formato desejado, renderizado nativamente e sempre correto. Duplicar isso no corpo da mensagem
só acrescenta um segundo horário — em UTC e defasado — para conflitar com o que o app já mostra.

Para saber o horário exato do último ping bem-sucedido, o painel do healthchecks mostra no fuso da
conta. A mensagem serve para **avisar**, não para ser o registro forense.

> ⚠️ `<BOT_TOKEN>` e `<CHAT_ID>` são os mesmos do bloco `telegram` do `email_config.json` — pegue
> de lá e cole **só no painel do healthchecks**. **Nunca** commite o token: este repositório é
> **público**.

Os placeholders disponíveis (`$NAME`, `$STATUS`, `$CODE`, `$NOW`, `$TAGS`…) estão linkados no
próprio formulário, em *"(available placeholders)"*. Evite `$NAME`
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

## Recompra mensal da AMX (`amx_recompra.py`)

Workflow **separado** ([`.github/workflows/amx_recompra.yml`](.github/workflows/amx_recompra.yml)),
`cron: 10 13 2 * *` — dia 2 de cada mês. Reporta no Telegram a recompra da América Móvil no
**último mês fechado**, em MXN e USD.

### O problema: a fonte não tem índice

A AMX divulga recompra à **BMV**, num relatório *"Información de recompra"* por dia de operação,
publicado **no mesmo dia**. Não vira 6-K, e o 20-F só traz a tabela mensal uma vez por ano com até
16 meses de defasagem — a BMV é a única fonte tempestiva. Mas não há listagem pública dos
documentos, e os IDs são opacos.

### A descoberta: o contador é global e sequencial

A BMV numera **todos** os documentos públicos num único contador, ~254/dia, independente do tipo.
Validado extrapolando de um documento de 19/12/2024 (`id 1428024`) até 01/09/2026: previsto
`1585758`, real `1586058` — **erro de 300 IDs em 621 dias**. Então dá para sondar:

```
HEAD /docs-pub/recompra/recompra_<id>_1.pdf   ->  200 = é relatório de recompra
```

A âncora do topo do espaço de IDs vem da home da BMV, que lista documentos recém-publicados.

### Por que mensal, e não varredura diária

O **remanente do fundo é saldo corrente**, então a recompra do mês sai de **dois** relatórios: o
último do mês N e o último do mês N-1. Com o estado persistido em `amx_recompra_state.json`, cada
execução mensal só varre para trás até achar o fechamento do mês — algumas centenas de IDs, em vez
dos ~7.600 de um mês inteiro. O `--bootstrap` (primeira execução) varre mais fundo porque precisa
achar os dois.

### A armadilha: recomposição do fundo

O delta do remanente **só vale se o fundo não tiver sido recomposto**. A assembleia anual autoriza
recursos novos e o remanente **sobe**. Medido na tabela do 20-F FY2025:

| | MXN |
|---|---|
| Remanente fim ABR/2025 | 10.611.365.225 |
| Remanente fim MAI/2025 | 18.145.547.145 |
| **Delta ingênuo** | **−7.534.181.919** ← negativo |
| Recompra real de maio | 2.479.745.073 (146.904.329 ações @ 16,88) |

A assembleia de 14/05/2025 injetou ~MXN 10,0 bi. O método ingênuo erraria **404%**, uma vez por
ano. Por isso, **se o remanente sobe, o script não reporta número** — avisa que houve recomposição
e que o mês precisa de apuração à parte.

### Conferências embutidas

- **Por relatório:** a soma dos importes das operações do dia tem que bater com o consumo do
  remanente. Bateu exato no relatório de 01/09/2026 (MXN 49.065.590 nos dois cálculos). Se
  divergir, o alerta sai marcado.
- **Entre relatórios:** o campo `remanente al último reporte` do fechamento seguinte tem que ser
  igual ao `remanente` do anterior. Confirmado: 31/08 e 01/09 fecharam em 15.126.294.893.

### Detalhes de extração

O remanente e a tabela de operações saem limpos. O bloco `SALDOS`, não — o `pypdf` embaralha a
ordem das colunas de forma **inconsistente entre as duas linhas do mesmo PDF**:

```
Al último reporte    59,899,000,000 0271,000,000
Al presente reporte  273,500,000 59,896,500,000 0
```

Por isso a tesouraria é identificada por **magnitude** (tesouraria ~271 mi vs circulação ~59,9 bi),
não por posição.

**Câmbio:** média simples das cotações diárias MXN→USD do período, via
[frankfurter.app](https://api.frankfurter.app) (sem chave).

> ⚠️ **Fragilidade assumida.** A varredura por ID depende de a BMV manter a numeração sequencial e
> o padrão de URL. Não é API contratada, é comportamento observado. Se uma varredura inteira não
> achar **nenhum** relatório de recompra de nenhuma emissora, isso é anomalia — foram encontrados
> 19 a 32 por chunk de 400 IDs nos testes.

---

## Informe mensal de insiders e controlador

A categoria **"Valores Mobiliários Negociados e Detidos"** entrega o formulário mensal
*"Negociação de Administradores e Pessoas Ligadas"* (tipos `Posição Consolidada` e
`Posição Individual`). Por grupo — **Controlador**, Conselho de Administração, Diretoria,
Conselho Fiscal — traz **saldo inicial**, **movimentações do mês** (dia, quantidade, preço,
volume em R$) e **saldo final**, por espécie (ON/PN).

> ⚠️ **Isto não é recompra da companhia.** É negociação de *insiders* e do controlador. A
> recompra de ações próprias (tesouraria) é outra divulgação e **não sai por esta categoria**.

**Tempestividade (medida, não estimada):** referência é o **mês fechado** (`08/2026`) e a
entrega observada foi em **01/09/2026** — primeiro dia do mês seguinte, para as 18 companhias
que protocolaram naquele momento. O prazo regulamentar é mais folgado que isso, então a cauda
se estende pelos primeiros dias do mês; o dado chega **dias após o fechamento, não em tempo real**.

Como o formulário é tabular, o prompt genérico produzia resumo vago. `CATEGORY_PROMPT_HINTS`
injeta instrução específica para essa categoria, pedindo saldo final por espécie e as
movimentações com preço e volume, e um único bullet curto quando não houve operações no mês.

> **Ruído esperado:** o informe é mensal e obrigatório mesmo sem operações. Boa parte dos
> alertas será "não houve movimentação no mês", para cada empresa e cada tipo de posição.

---

## Por que o alerta traz DOIS links

Cada mensagem termina com **"Abrir documento"** e **"baixar PDF"**. Não é redundância:

O endpoint de download do RAD (`frmDownloadDocumento.aspx`) devolve o PDF com o header
**errado** — `Content-Type: text/html` — e depende do `Content-Disposition: attachment` para o
navegador entender que é um arquivo:

```
HTTP/1.1 200 OK
Content-Type: text/html                                  <- errado, o corpo e um PDF
Content-disposition: attachment; filename=017671000101011.pdf
%PDF-1.7 ...
```

Navegador de desktop honra o `Content-Disposition` e baixa certo. **O navegador embutido do
Telegram no celular ignora**, obedece o `Content-Type` e tenta renderizar os bytes do PDF como
HTML — o resultado é a tela cheia de `%PDF-1.7 ... obj ... xref`, que parece "um monte de código".

Por isso o link primário passou a ser `frmExibirArquivoIPEExterno.aspx?NumeroProtocoloEntrega=<protocolo>`
(`build_viewer_url()`), a página de visualização do RAD — HTML de verdade, com o PDF num iframe.
Funciona em qualquer navegador. O link de download direto continua como segundo, porque no
desktop baixar o arquivo costuma ser o que se quer.

> Filings da SEC (AFYA) não passam por isso: a URL já é uma página normal. `build_viewer_url()`
> devolve `None` e a mensagem sai com um link só.

---

## Falhas silenciosas: o que o heartbeat NÃO vê

O heartbeat cobre bem o eixo *"o monitor parou de rodar"* — GitHub fora do ar, CVM fora do ar.
São falhas **ruidosas**: o ping some e o alerta dispara.

Ele é cego para o outro eixo: *"o monitor roda, fica verde, e mesmo assim você não recebe"*.
Essa é a falha que machuca, porque não tem sintoma — você não recebe nada e conclui que não houve
fato relevante. Duas dessas foram fechadas em código:

### 1. Entrega confirmada antes de marcar como visto

**O que acontecia:** `check_once()` marcava o protocolo como visto **na detecção**, antes de
qualquer envio, e `send_telegram_alert()` descartava o retorno de `_send_one_telegram()`. Com o
Telegram fora do ar o run saía com **exit 0**, pingava o heartbeat (**verde**), e o Actions
commitava o protocolo como visto. O ciclo seguinte não retentava: o fato relevante era detectado,
arquivado e **nunca entregue**, deixando como único vestígio uma linha `FAIL` no `send_log.txt`.

**Como funciona agora** (`deliver_filings()`):

- um protocolo só entra em `seen_protocols.json` depois que **todos** os destinos confirmam;
- o que não foi entregue fica em `pending_deliveries.json` e, como continua fora de `seen`,
  reaparece naturalmente como "novo" no ciclo seguinte e é retentado;
- o rastreio é **por destino** (`telegram:<chat_id>`, `email`): a retentativa pula quem já
  recebeu, então ninguém leva mensagem duplicada;
- o resumo do Claude fica guardado na pendência — retentar não repaga a API;
- após `MAX_DELIVERY_ATTEMPTS` (6 ≈ 30 min) desiste, mas grava um registro `giveup` no
  `send_log.txt`. Desistir é aceitável; desistir **em silêncio** não é.

> Por que existe um teto: sem ele, um filing que o Telegram rejeita de forma determinística
> (ex.: HTML malformado devolvendo 400) seria retentado para sempre, a cada 5 minutos.

### 2. Canário do parser (`raw_response_is_sane()`)

**O que acontecia:** `cvm_ok` significava apenas *"não lançou exceção"*. Se o RAD mudasse o
formato do payload, `parse_rows()` devolveria **lista vazia sem erro** — e "parser quebrado"
ficaria indistinguível de "não teve fato relevante hoje". O check ficaria verde para sempre e o
silêncio pareceria normal.

**Como funciona agora:** a consulta é ampla (todas as categorias, período "esta semana", todas as
empresas — o filtro por empresa é local), então uma resposta saudável **sempre** traz muitas
linhas, mesmo que nenhuma seja das empresas monitoradas. Referência: em 14/08/2026 o endpoint
devolvia **2.320 linhas**. Duas anomalias derrubam `cvm_ok` e, via heartbeat, viram alerta:

| Anomalia | Significa |
|---|---|
| zero linhas | endpoint mudou, ou passou a exigir sessão/captcha |
| nenhuma linha com ≥ 11 colunas | o layout de colunas mudou (`parse_rows` descarta em silêncio o que não bate) |

### O que continua descoberto

- **Telegram fora do ar por > 30 min** — passa do teto de tentativas. Fica registrado como
  `giveup` no `send_log.txt`, mas o alerta disso também iria pelo Telegram, que é justamente o
  canal quebrado. Ponto cego assumido.
- **cron-job.org cair** — o `schedule` do GitHub assume e você degrada de 5 para ~25 min, possivelmente
  sem alerta, porque o heartbeat pode continuar sendo pingado dentro da janela de 15 min.
  Mitigação seria um segundo gatilho independente (não implementado).
- **healthchecks.io cair** — ninguém vigia o vigia.

---

## Operação: como saber se está vivo

- **Execuções:** aba *Actions* do repositório, ou `gh run list --workflow=monitor.yml`.
- **Cadência real:** compare os horários dos runs — devem estar espaçados ~5 min (os disparos do
  cron-job.org). Runs `event=schedule` esparsos são o backup do GitHub; o principal é
  `event=workflow_dispatch`.
- **Heartbeat:** o painel do healthchecks.io mostra o último ping; se ficar vermelho, o alerta cai
  no **Telegram** (ver [seção 3.1](#31-alerta-do-heartbeat-no-telegram-não-só-email)) e no email
  da conta — é o seu sinal de que algo na cadeia travou.
- **Entregas pendentes:** [`pending_deliveries.json`](pending_deliveries.json) deve estar `{}` em
  regime normal. Se tiver conteúdo, há fato relevante detectado que **ainda não chegou** em algum
  destino — o campo `attempts` mostra há quantos ciclos. Procure `giveup` no `send_log.txt` para
  ver o que foi abandonado após o teto de tentativas.
- **Histórico de envios:** [`send_log.txt`](send_log.txt) tem uma linha por envio (email e
  telegram separados), com `status` `OK`/`FAIL` e detalhe do erro quando falha. É durável (o
  Actions commita de volta), então serve de auditoria: `2026-... | telegram | OK | RDOR3 | ...`.
- **Base histórica de FRs:** [`fatos_arquivo.csv`](fatos_arquivo.csv) acumula todo fato
  relevante/aviso detectado (`data, ticker, empresa, categoria, assunto, protocolo, link`). Abre
  no Excel/Sheets — dá para filtrar por empresa, contar por período, etc. Só metadados + título,
  sem o resumo do Claude.
- **Falha silenciosa da CVM:** se a CVM estiver fora — ou responder num formato que o parser não
  reconhece (ver [canário](#2-canário-do-parser-raw_response_is_sane)) — o run passa "verde" mas o
  heartbeat **não pinga**. Uma falha isolada é absorvida pela *grace*; se persistir por vários
  runs, os pings cessam e o healthchecks.io te alerta pelo silêncio.

> ⚠️ **O que NÃO é monitorado.** O heartbeat só enxerga o eixo "parou de rodar". Falha de entrega
> por mais de ~30 min, queda do cron-job.org (degrada a cadência sem necessariamente alertar) e
> queda do próprio healthchecks.io continuam descobertos — ver
> [Falhas silenciosas](#falhas-silenciosas-o-que-o-heartbeat-não-vê).

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

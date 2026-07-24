# cvm-fatos-relevantes

Monitora **Fatos Relevantes** e **Avisos aos Acionistas** de empresas brasileiras listadas
(setor saúde/educação/telecom) na CVM, e formulários equivalentes (6-K/20-F/8-K) na SEC para
emissoras listadas no exterior (ex.: Afya). Gera **resumos automáticos com Claude** e envia
**alertas por email** assim que um novo documento é publicado.

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
Email (SMTP/Brevo) para os destinatários
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
| `email_config.json` | Credenciais SMTP, chave da Anthropic, destinatários, URL do heartbeat. **Não versionado** (`.gitignore`); na nuvem é recriado do secret. |
| `monitor_log.txt` | Log de execução local. Não versionado. |
| `requirements.txt` | Dependências Python. |

---

## Uso local

```bash
pip install -r requirements.txt

python cvm_fatos_relevantes_claude.py            # execução silenciosa (modo cron)
python cvm_fatos_relevantes_claude.py --once     # execução manual com status na tela
python cvm_fatos_relevantes_claude.py --test-email   # envia um email de teste aos destinatários
python cvm_fatos_relevantes_claude.py --bootstrap    # marca tudo que existe hoje como "visto" (não alerta)
```

> No Windows, use `py` no lugar de `python` se o alias `python` abrir a Microsoft Store.

Rode `--bootstrap` uma vez ao configurar um ambiente novo, para não ser inundado com alertas
de documentos antigos.

---

## Configuração (`email_config.json`)

Na primeira execução, um template é criado automaticamente. Preencha:

```json
{
  "enabled": true,
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
    "bot_token": "123456789:AA...",
    "chat_id": 123456789
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

Além do email, cada fato relevante pode ser enviado para um chat do Telegram via bot. É o mesmo
resumo (bullets do Claude) numa mensagem formatada, com link para o documento.

1. Crie um bot com o [@BotFather](https://t.me/BotFather) e copie o **bot token**.
2. Descubra o **chat_id** de destino (mande uma mensagem ao bot e consulte
   `https://api.telegram.org/bot<TOKEN>/getUpdates`, ou use um bot como `@userinfobot`).
   Chat privado = id positivo; grupo/canal = id negativo.
3. Preencha o bloco `telegram` no config e teste:

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
> email repetido.

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
simplesmente para de receber emails, sem saber se é "não teve fato relevante" ou "está quebrado".

O heartbeat inverte a lógica: em vez de esperar o sistema quebrado avisar que quebrou, um vigia
**externo** alerta você quando o monitor **para de dar sinal de vida**.

Como está implementado no código:
- ao final de cada checagem real, [`main()`](cvm_fatos_relevantes_claude.py) chama
  `ping_healthcheck(url, fail=not cvm_ok)`;
- **sucesso** → pinga a URL base (CVM respondeu);
- **falha** → pinga `.../fail` (a checagem rodou, mas a CVM estava indisponível);
- se o script travar antes disso, **nenhum ping é enviado** → o watchdog alerta pelo silêncio;
- sem `healthcheck_url` configurado, é um **no-op** (não quebra nada).

Setup (~2 min):

1. Crie conta grátis em [healthchecks.io](https://healthchecks.io).
2. **Add Check** → **Period = 15 min** (igual ao cron), **Grace = ~5 min**.
3. Copie a **ping URL** (`https://hc-ping.com/<uuid>`) → coloque em `healthcheck_url`
   (no `email_config.json` local **e** no secret — ver abaixo).
4. Os alertas vão para o email do cadastro por padrão.

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
- **Cadência real:** compare os horários dos runs — devem estar espaçados ~15 min (os disparos do
  cron-job.org). Runs `event=schedule` esparsos são o backup do GitHub; o principal é
  `event=workflow_dispatch`.
- **Heartbeat:** o painel do healthchecks.io mostra o último ping; se ficar vermelho, você recebe
  o alerta por email — é o seu sinal de que algo na cadeia travou.
- **"Verde mas sem checar":** se a CVM estiver fora, o run passa mas o heartbeat recebe um `/fail`,
  então o healthchecks.io ainda te avisa.

---

## Reaproveitar este método em outros monitores

O padrão **disparo externo + heartbeat** é genérico e se aplica a qualquer job agendado no GitHub
Actions. Veja o playbook portátil em
**[`docs/reliable-github-actions.md`](docs/reliable-github-actions.md)** — passo a passo para
levar qualquer repositório com `schedule:` para uma cadência confiável e observável.

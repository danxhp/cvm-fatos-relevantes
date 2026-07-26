# Playbook: cron confiável e observável no GitHub Actions

Padrão genérico para **qualquer** job agendado no GitHub Actions. Resolve os dois problemas que
afetam todo `schedule:` do GitHub:

1. **Cadência não confiável** — o cron interno do GitHub atrasa/pula execuções (best-effort).
2. **Falha silenciosa** — quando o job "passa" mas não fez o trabalho (fonte fora do ar, etc.),
   nada te avisa.

A ideia central: **o GitHub Actions continua sendo o executor, mas quem agenda e quem vigia ficam
fora dele.**

```
[cron externo confiável] → workflow_dispatch → [GitHub Actions roda o job] → [ping heartbeat]
   cron-job.org / Cloud Scheduler                                              healthchecks.io
```

> Este documento nasceu no monitor CVM (ver README), mas nada aqui é específico dele. Troque
> `USUARIO/REPO` e o nome do workflow e aplique em qualquer repositório.

---

## Por que não confiar no `schedule:` do GitHub

- É *best-effort*: em horários de pico, atrasa muitos minutos ou **pula a execução inteira**.
- Um `schedule` de 15 min chega a rodar ~1 a cada 6 vezes, com buracos de horas.
- Vale igual para repo **público e privado** e **não melhora em plano pago** — é limitação da
  plataforma, não da conta.

Conclusão: use `workflow_dispatch` como gatilho principal e um cron externo para acioná-lo.
Deixe o `schedule` como backup opcional (inofensivo se o job for idempotente).

---

## Passo 1 — Habilitar `workflow_dispatch` no workflow

No `.github/workflows/SEU_WORKFLOW.yml`:

```yaml
on:
  workflow_dispatch:            # gatilho principal (externo)
  schedule:
    - cron: '*/15 * * * *'      # backup opcional
```

Se o job grava estado de volta no repo, garanta idempotência (dedup) para que disparos duplicados
— externo + backup — não causem efeito colateral.

---

## Passo 2 — Token para disparar via API (fine-grained PAT)

GitHub → *Settings → Developer settings → Personal access tokens → Fine-grained tokens →
Generate new token*:

- **Repository access:** *Only select repositories* → o repositório alvo
- **Permissions → Repository → Actions:** `Read and write`
- **Expiration:** o mais longo possível. ⚠️ Token expirado = disparo para silenciosamente. O
  heartbeat (passo 4) é o que te avisa.

Teste (espera-se `HTTP 204`):

```bash
curl -i -X POST \
  -H "Authorization: Bearer SEU_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/USUARIO/REPO/actions/workflows/SEU_WORKFLOW.yml/dispatches \
  -d '{"ref":"main"}'
```

Um mesmo token *fine-grained* pode dar acesso a **vários repositórios** de uma vez, então dá para
ter um só token para todos os seus monitores (ou um por repo, se preferir isolar).

---

## Passo 3 — Cron externo confiável

### Opção A — cron-job.org (grátis)

Dispara no minuto certo, **não pula execuções** como o GitHub. Sem SLA, mas mais que suficiente
para monitores. Config de um job:

| Onde | Valor |
|---|---|
| **URL** (Address) | `https://api.github.com/repos/USUARIO/REPO/actions/workflows/SEU_WORKFLOW.yml/dispatches` |
| **Request method** | `POST` |
| **Request body** | `{"ref":"main"}` |
| **Execution schedule** | ex.: `*/15` |

Headers (a caixa **Headers** é só para cabeçalhos HTTP — URL/method/body são campos do formulário):

| Key | Value |
|---|---|
| `Authorization` | `Bearer github_pat_...` (token inteiro, com `Bearer `) |
| `Accept` | `application/vnd.github+json` |
| `X-GitHub-Api-Version` | `2022-11-28` |
| `Content-Type` | `application/json` |

Valide com **Test run** (deve dar `204`), confirme **Enabled** e salve.

### Opção B — Google Cloud Scheduler (upgrade pago, ~US$0,10/job/mês)

Dispara no segundo, com retries e garantias. Vale se você quer confiabilidade contratual. Crie um
job HTTP com o mesmo endpoint, método `POST`, corpo `{"ref":"main"}` e os mesmos headers (o
`Authorization: Bearer` vai em *Auth header* / headers customizados).

---

## Passo 4 — Heartbeat / dead-man's-switch

O cron externo garante que o gatilho *saiu*, não que o trabalho *aconteceu*. O heartbeat é um
vigia **externo** que alerta quando o job para de dar sinal de vida — cobre cron externo fora do
ar, token expirado, fonte de dados indisponível e crash do script.

**Setup (healthchecks.io, grátis):**

1. Conta em [healthchecks.io](https://healthchecks.io) → **Add Check**.
2. **Period** = intervalo do cron; **Grace** generosa. Regra: `Period + Grace` deve cobrir
   **~2–3 ciclos** do cron. Ex.: cron de 5 min → Period 5, Grace 10 (ou mais). Isso absorve
   jitter, runs longos e 1–2 falhas transitórias da fonte **sem alarme falso**.
3. Copie a **ping URL** (`https://hc-ping.com/<uuid>`).
4. Alertas vão para o email do cadastro (dá para plugar Slack, etc.).

**No código do job**, pingue a URL ao final **de uma checagem real** (a fonte respondeu). Padrão
mínimo (Python, mas a ideia vale para qualquer linguagem/shell):

```python
import os, requests

def ping_healthcheck(url: str) -> None:
    if not url:
        return                       # no-op se não configurado
    try:
        requests.get(url, timeout=10)
    except Exception:
        pass                          # nunca deixe o ping derrubar o job

# ... ao final da execução:
hc = os.environ.get("HEALTHCHECK_URL")
if hc and fonte_respondeu:           # pinga SÓ quando a checagem foi real
    ping_healthcheck(hc)
# Se a fonte falhou (transitório), NÃO pingue. A grace absorve um ping perdido;
# uma queda sustentada (vários runs sem pingar) para de pingar e dispara o alerta.
```

Em shell, o equivalente é `curl -fsS -m 10 --retry 3 "$HEALTHCHECK_URL" || true` (só no caminho
de sucesso).

**⚠️ Não use `/fail` para falhas transitórias.** É tentador pingar `hc-ping.com/<uuid>/fail`
quando "rodou mas não checou de verdade" — mas isso **derruba o check na hora**, e se a fonte é
instável (recupera no run seguinte) você recebe uma enxurrada de emails *down/up* (*flapping*).
O design correto é **"pinga no sucesso, silencia na falha"**: deixe a *grace* distinguir um hiccup
isolado (ignorado) de uma queda real (vários runs mudos → alerta).

---

## Passo 5 — Secrets e configuração na nuvem

Tudo que o job precisa em runtime tem que estar em **secrets do repositório** (o filesystem do
runner é efêmero). Padrões comuns:

- Um secret único com um JSON de config (`gh secret set NOME < arquivo.json`), montado em arquivo
  no início do workflow e **apagado antes de commitar** qualquer estado — para não vazar
  credenciais.
- Ou secrets individuais expostos como `env:` nos steps.

Inclua a `HEALTHCHECK_URL` (ou o campo equivalente na config) entre os secrets — senão o ping não
acontece na nuvem.

---

## Checklist para um novo repositório

- [ ] `workflow_dispatch:` habilitado no workflow (e `schedule:` como backup opcional)
- [ ] Job idempotente (dedup) se grava estado de volta
- [ ] Fine-grained PAT com `Actions: Read and write` no(s) repo(s) — testado com `curl` (204)
- [ ] Job no cron externo (cron-job.org / Cloud Scheduler) com URL + método + body + 4 headers
- [ ] Test run do cron externo retornou `204` e o run apareceu no Actions
- [ ] Check no healthchecks.io com `Period + Grace` cobrindo ~2–3 ciclos do cron
- [ ] Código pinga a URL **só no sucesso** (silencia na falha transitória — sem `/fail`)
- [ ] `HEALTHCHECK_URL` (e demais credenciais) nos secrets do repo
- [ ] Confirmado: um travamento simulado (ou token errado) faz o healthchecks.io alertar

---

## Como aplicar aos monitores existentes

Para cada repositório de monitor que hoje usa só `schedule:`:

1. Adicione `workflow_dispatch:` ao workflow (passo 1).
2. Reaproveite o **mesmo PAT** (se o token já dá acesso àquele repo) ou gere um novo (passo 2).
3. Crie um job no cron-job.org apontando para o `dispatches` daquele workflow (passo 3).
4. Adicione o snippet de heartbeat ao código e um check no healthchecks.io (passo 4).
5. Ajuste os secrets (passo 5).

O trabalho por repo é pequeno e mecânico. O PAT e as contas de cron-job.org/healthchecks.io são
compartilháveis entre todos os projetos.

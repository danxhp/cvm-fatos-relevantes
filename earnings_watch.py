#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
earnings_watch.py — vigia pontual de resultados na CVM (categoria "Dados Econômico-Financeiros").

Baseado no monitor principal: importa o fetch/parse de cvm_fatos_relevantes_claude.py.
Roda de minuto a minuto, avisa APENAS no Telegram e APENAS no chat alvo (o Danilo — o
chat do Enzo NÃO é referenciado neste arquivo, de propósito). Não envia email.
Para sozinho à meia-noite (horário local) — ou antes, se capturar todas as empresas.

Uso:
    py earnings_watch.py            # inicia o watcher (roda até meia-noite ou até capturar tudo)
    py earnings_watch.py --once     # uma checagem só (mostra o estado atual; NÃO envia nada)
    py earnings_watch.py --test     # manda um Telegram de teste pro chat alvo e sai

Reaproveitar no próximo trimestre: ajuste WATCH_TICKERS e WATCH_LABEL abaixo e rode de novo.
"""
from __future__ import annotations

import html
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import cvm_fatos_relevantes_claude as cvm  # reaproveita make_session/fetch/parse/build_url

# ============================ CONFIG (edite para reusar) ============================
WATCH_TICKERS  = {"FLRY3", "HYPE3"}                 # empresas a vigiar
WATCH_CATEGORY = "Dados Econômico-Financeiros"      # categoria do release de resultados
WATCH_LABEL    = "Resultados 2T26"                  # texto no cabeçalho da mensagem
TARGET_CHAT_ID = "1496332324"                       # SÓ este chat (o Danilo). NÃO enviar ao Enzo.
POLL_SECONDS   = 60                                 # de minuto a minuto
# ===================================================================================

LOG_FILE = SCRIPT_DIR / "earnings_watch_log.txt"
SEEN_FILE = SCRIPT_DIR / "earnings_watch_seen.json"   # protocolos já ENTREGUES (persiste entre restarts)
EMAIL_CONFIG_FILE = SCRIPT_DIR / "email_config.json"


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_bot_token(chat_id: str) -> str:
    """Lê o bot_token do email_config.json para o chat alvo (não hardcoda o segredo)."""
    with open(EMAIL_CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    tg = cfg.get("telegram", {}) or {}
    for d in (tg.get("destinations") or []):
        if str(d.get("chat_id")) == str(chat_id):
            return str(d["bot_token"]).strip()
    if str(tg.get("chat_id")) == str(chat_id):      # formato antigo (destino único)
        return str(tg.get("bot_token")).strip()
    raise RuntimeError(f"bot_token do chat {chat_id} não encontrado em email_config.json")


BOT_TOKEN = load_bot_token(TARGET_CHAT_ID)


def send_telegram(text: str) -> bool:
    """Envia SEMPRE e SOMENTE para TARGET_CHAT_ID."""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": TARGET_CHAT_ID, "text": text[:4000],
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=30,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log(f"ERRO ao enviar Telegram: {e}")
        return False


def fetch_watch_filings(session) -> list:
    """Documentos da categoria alvo, apenas das empresas vigiadas."""
    raw = cvm.fetch_raw_documents(session, cvm.CVM_ALL_CATEGORIES)
    return [f for f in cvm.parse_rows(raw)
            if f.ticker in WATCH_TICKERS and f.category == WATCH_CATEGORY]


def render(f) -> str:
    e = html.escape
    return (
        f"🔔 <b>{e(WATCH_LABEL)} — {e(f.ticker)} · {e(f.company_name)}</b>\n"
        f"<b>{e(f.category)}</b>\n"
        f"{e(f.subject or f.doc_type or 'Documento')}\n"
        f"🗓 {e(f.filing_time or '')}\n\n"
        f'<a href="{e(cvm.build_download_url(f))}">Baixar documento original</a>'
    )


def _is_today(f, today_str: str) -> bool:
    return (f.filing_time or "").strip().startswith(today_str)


def load_seen() -> set:
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen: set) -> None:
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(seen), f)
    except Exception as e:
        log(f"WARNING: falha ao salvar estado: {e}")


def run_watch() -> int:
    start = datetime.now()
    today_str = start.strftime("%d/%m/%Y")
    midnight = start.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    log(f"Watcher iniciado. Vigiando {sorted(WATCH_TICKERS)} | categoria '{WATCH_CATEGORY}' | hoje={today_str}.")
    log(f"Encerra automaticamente à meia-noite: {midnight:%Y-%m-%d %H:%M}.")

    seen = load_seen()          # protocolos JÁ ENTREGUES (persistido — sobrevive a restart)
    triggered: set = set()      # tickers com pelo menos um doc entregue hoje
    announced_both = False

    # Reconciliação de restart: se docs de hoje já estão em seen (entregues antes),
    # marca o ticker como entregue SEM reenviar — mantém relatório/anúncio corretos.
    try:
        for f in fetch_watch_filings(cvm.make_session()):
            if _is_today(f, today_str) and f.protocol in seen:
                triggered.add(f.ticker)
    except Exception as e:
        log(f"WARNING: reconciliação inicial falhou: {e}")
    if triggered:
        log(f"Reconciliação: já entregues hoje (não reenvio): {sorted(triggered)}")
    if triggered == WATCH_TICKERS:
        announced_both = True

    send_telegram(
        f"🟢 <b>Watcher {html.escape(WATCH_LABEL)} iniciado</b>\n"
        f"Vigiando <b>{', '.join(sorted(WATCH_TICKERS))}</b> (categoria {html.escape(WATCH_CATEGORY)}) "
        f"de minuto a minuto. Envio os docs de hoje e sigo até a meia-noite ({midnight:%H:%M})."
    )

    while True:
        now = datetime.now()
        if now >= midnight:
            faltou = sorted(WATCH_TICKERS - triggered)
            send_telegram(
                "🌙 <b>Watcher encerrado (meia-noite).</b>\n"
                + (f"Capturados hoje: {', '.join(sorted(triggered))}.\n" if triggered else "Nada capturado hoje.\n")
                + (f"Não saiu: {', '.join(faltou)}." if faltou else "Todas saíram. ✅")
            )
            log(f"Encerrado à meia-noite. Triggered={sorted(triggered)} Faltou={faltou}")
            return 0

        try:
            # Entrega qualquer doc da categoria alvo COM DATA DE HOJE, dedup por protocolo.
            # BUGFIX: só marca 'seen'/'triggered' se o envio deu certo — assim uma falha
            # transitória do Telegram é RETENTADA no próximo minuto (não perde nem para cedo).
            for f in fetch_watch_filings(cvm.make_session()):
                if f.protocol in seen:
                    continue
                if not _is_today(f, today_str):
                    continue
                ok = send_telegram(render(f))
                if ok:
                    seen.add(f.protocol)
                    save_seen(seen)
                    triggered.add(f.ticker)
                    log(f"TRIGGER {f.ticker} | {(f.subject or f.doc_type)} | protocolo {f.protocol} | {f.filing_time} | telegram=OK")
                else:
                    log(f"FALHA no envio de {f.ticker} | protocolo {f.protocol} — retento no próximo minuto")
        except Exception as e:
            log(f"WARNING: erro no poll (segue no próximo minuto): {e}")

        # Aviso ÚNICO quando ambos saíram — mas NÃO paramos: seguimos até a meia-noite
        # para não perder complementos do lote (DF/ITR que saem minutos depois).
        if triggered == WATCH_TICKERS and not announced_both:
            announced_both = True
            send_telegram(
                f"✅ <b>Resultados de {', '.join(sorted(WATCH_TICKERS))} capturados.</b>\n"
                f"Sigo vigiando até a meia-noite caso saiam versões/complementos."
            )
            log("Ambos capturados — seguindo até meia-noite (sem early-stop).")

        time.sleep(POLL_SECONDS)


def main() -> int:
    if "--test" in sys.argv:
        ok = send_telegram(f"✅ Teste do watcher <b>{html.escape(WATCH_LABEL)}</b> — chegou? (só você recebe isto)")
        print("Telegram de teste:", "OK" if ok else "FALHOU")
        return 0 if ok else 1

    if "--once" in sys.argv:
        fs = fetch_watch_filings(cvm.make_session())
        print(f"{len(fs)} doc(s) '{WATCH_CATEGORY}' agora para {sorted(WATCH_TICKERS)}:")
        for f in fs:
            print(f"  {f.ticker} | {(f.subject or f.doc_type)} | protocolo {f.protocol} | {f.filing_time}")
        return 0

    return run_watch()


if __name__ == "__main__":
    raise SystemExit(main())

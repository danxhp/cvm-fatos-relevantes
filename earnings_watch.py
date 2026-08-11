#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
earnings_watch.py — vigia pontual de resultados na CVM (categoria "Dados Econômico-Financeiros").

Baseado no monitor principal: importa o fetch/parse de cvm_fatos_relevantes_claude.py.
Roda de minuto a minuto e avisa APENAS no Telegram (não envia email). Os destinos são
lidos de email_config.json (bloco telegram.destinations) — os mesmos do monitor principal,
ou seja, TODOS os configurados recebem (hoje: Danilo + Enzo). Dedup é por (protocolo, chat),
então cada pessoa recebe cada doc uma vez e uma falha de envio é retentada só para quem faltou.
Para sozinho à meia-noite (horário local).

Uso:
    py earnings_watch.py            # inicia o watcher (roda até meia-noite)
    py earnings_watch.py --once     # uma checagem só (mostra o estado atual; NÃO envia nada)
    py earnings_watch.py --test     # manda um Telegram de teste para todos os destinos e sai

Reaproveitar no próximo trimestre: ajuste WATCH_TICKERS e WATCH_LABEL abaixo e rode de novo.
Quem recebe é controlado por email_config.json (telegram.destinations).
"""
from __future__ import annotations

import ctypes
import html
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import cvm_fatos_relevantes_claude as cvm  # reaproveita make_session/fetch/parse/build_url/config

# ============================ CONFIG (edite para reusar) ============================
WATCH_TICKERS  = {"BLAU3"}                          # empresas a vigiar
WATCH_CATEGORY = "Dados Econômico-Financeiros"      # categoria do release de resultados
WATCH_LABEL    = "Resultados 2T26"                  # texto no cabeçalho da mensagem
POLL_SECONDS   = 60                                 # de minuto a minuto
# Destinos do Telegram: lidos de email_config.json (telegram.destinations). Todos recebem.
# ===================================================================================

LOG_FILE = SCRIPT_DIR / "earnings_watch_log.txt"
SEEN_FILE = SCRIPT_DIR / "earnings_watch_seen.json"   # chaves "protocolo|chat" já ENTREGUES (persiste)

import json


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def popup(title: str, text: str) -> None:
    """Mostra um pop-up centralizado na tela (MessageBox do Windows), sem bloquear o loop.
    Roda numa thread — se ninguém fechar, o watcher continua vigiando normalmente."""
    def _show():
        try:
            # MB_OK | MB_ICONINFORMATION | MB_SYSTEMMODAL | MB_SETFOREGROUND -> centralizado, no topo
            ctypes.windll.user32.MessageBoxW(0, str(text), str(title), 0x40 | 0x1000 | 0x10000)
        except Exception as e:
            log(f"WARNING: pop-up falhou: {e}")
    try:
        threading.Thread(target=_show, daemon=True).start()
    except Exception as e:
        log(f"WARNING: pop-up thread falhou: {e}")


def load_destinations() -> list:
    """Todos os destinos de Telegram (email_config.json → telegram.destinations).
    Cada um: {'bot_token', 'chat_id'}. Reaproveita a config do monitor principal."""
    tg = cvm.get_telegram_config()   # {"destinations": [...]} ou None
    return list((tg or {}).get("destinations") or [])


DESTINATIONS = load_destinations()


def _post(bot_token: str, chat_id, text: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000],
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=30,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log(f"ERRO Telegram (chat {chat_id}): {e}")
        return False


def broadcast(text: str) -> None:
    """Mensagem avulsa (início/resumo/teste) para TODOS os destinos."""
    for d in DESTINATIONS:
        _post(d["bot_token"], d["chat_id"], text)


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


def _keys_for(f) -> list:
    return [f"{f.protocol}|{d['chat_id']}" for d in DESTINATIONS]


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
    if not DESTINATIONS:
        log("ERRO: nenhum destino de Telegram em email_config.json (telegram.destinations). Abortando.")
        return 1

    start = datetime.now()
    today_str = start.strftime("%d/%m/%Y")
    midnight = start.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    chats = ", ".join(str(d["chat_id"]) for d in DESTINATIONS)
    log(f"Watcher iniciado. Vigiando {sorted(WATCH_TICKERS)} | '{WATCH_CATEGORY}' | hoje={today_str}.")
    log(f"Destinos ({len(DESTINATIONS)}): {chats}. Encerra à meia-noite: {midnight:%Y-%m-%d %H:%M}.")

    seen = load_seen()          # chaves "protocolo|chat" JÁ ENTREGUES (persistido)
    triggered: set = set()      # tickers com pelo menos um doc entregue a TODOS os destinos
    announced_done = False
    popped: set = set()         # protocolos que já geraram pop-up na tela (evita repetir)

    # Reconciliação de restart: ticker cujos docs de hoje já foram entregues a todos (sem reenviar)
    try:
        for f in fetch_watch_filings(cvm.make_session()):
            if _is_today(f, today_str) and all(k in seen for k in _keys_for(f)):
                triggered.add(f.ticker)
    except Exception as e:
        log(f"WARNING: reconciliação inicial falhou: {e}")
    if triggered:
        log(f"Reconciliação: já entregues hoje (não reenvio): {sorted(triggered)}")
    if triggered == WATCH_TICKERS:
        announced_done = True

    broadcast(
        f"🟢 <b>Watcher {html.escape(WATCH_LABEL)} iniciado</b>\n"
        f"Vigiando <b>{', '.join(sorted(WATCH_TICKERS))}</b> (categoria {html.escape(WATCH_CATEGORY)}) "
        f"de minuto a minuto. Envio os docs de hoje e sigo até a meia-noite ({midnight:%H:%M})."
    )
    popup(f"Watcher {WATCH_LABEL} iniciado",
          f"Vigiando {', '.join(sorted(WATCH_TICKERS))} de minuto a minuto.\n"
          f"Vou abrir um pop-up na tela assim que o resultado sair. Encerro à meia-noite.")

    while True:
        now = datetime.now()
        if now >= midnight:
            faltou = sorted(WATCH_TICKERS - triggered)
            broadcast(
                "🌙 <b>Watcher encerrado (meia-noite).</b>\n"
                + (f"Capturados hoje: {', '.join(sorted(triggered))}.\n" if triggered else "Nada capturado hoje.\n")
                + (f"Não saiu: {', '.join(faltou)}." if faltou else "Tudo capturado. ✅")
            )
            log(f"Encerrado à meia-noite. Triggered={sorted(triggered)} Faltou={faltou}")
            return 0

        try:
            # Entrega docs da categoria alvo COM DATA DE HOJE, dedup por (protocolo, chat).
            # Só marca a chave como entregue se o envio deu certo — falha de um destino é
            # RETENTADA no próximo minuto só para ele (os que já receberam não recebem de novo).
            for f in fetch_watch_filings(cvm.make_session()):
                if not _is_today(f, today_str):
                    continue
                keys = _keys_for(f)
                if all(k in seen for k in keys):
                    continue  # já entregue a todos
                if f.protocol not in popped:
                    popped.add(f.protocol)
                    popup(f"🔔 {WATCH_LABEL} — {f.ticker}",
                          f"{f.company_name}\n{f.category}\n{f.subject or f.doc_type or ''}\n\n"
                          f"Protocolo {f.protocol} · {f.filing_time}")
                text = render(f)
                for d in DESTINATIONS:
                    key = f"{f.protocol}|{d['chat_id']}"
                    if key in seen:
                        continue
                    if _post(d["bot_token"], d["chat_id"], text):
                        seen.add(key)
                        save_seen(seen)
                        log(f"ENTREGUE {f.ticker} | {(f.subject or f.doc_type)} | proto {f.protocol} | {f.filing_time} -> chat {d['chat_id']}")
                    else:
                        log(f"FALHA {f.ticker} | proto {f.protocol} -> chat {d['chat_id']} (retento no próximo minuto)")
                if all(k in seen for k in keys):
                    triggered.add(f.ticker)
        except Exception as e:
            log(f"WARNING: erro no poll (segue no próximo minuto): {e}")

        # Aviso ÚNICO quando tudo saiu — mas NÃO paramos: seguimos até a meia-noite
        # para não perder complementos do lote (DF/ITR que saem minutos depois).
        if triggered == WATCH_TICKERS and not announced_done:
            announced_done = True
            broadcast(
                f"✅ <b>Resultados de {', '.join(sorted(WATCH_TICKERS))} capturados.</b>\n"
                f"Sigo vigiando até a meia-noite caso saiam versões/complementos."
            )
            log("Tudo capturado — seguindo até meia-noite (sem early-stop).")

        time.sleep(POLL_SECONDS)


def main() -> int:
    if "--test" in sys.argv:
        if not DESTINATIONS:
            print("Nenhum destino em email_config.json (telegram.destinations).")
            return 1
        broadcast(f"✅ Teste do watcher <b>{html.escape(WATCH_LABEL)}</b> — chegou?")
        print(f"Telegram de teste enviado para {len(DESTINATIONS)} destino(s): "
              + ", ".join(str(d['chat_id']) for d in DESTINATIONS))
        return 0

    if "--once" in sys.argv:
        fs = fetch_watch_filings(cvm.make_session())
        print(f"{len(fs)} doc(s) '{WATCH_CATEGORY}' agora para {sorted(WATCH_TICKERS)}:")
        for f in fs:
            print(f"  {f.ticker} | {(f.subject or f.doc_type)} | protocolo {f.protocol} | {f.filing_time}")
        print(f"Destinos configurados: {[str(d['chat_id']) for d in DESTINATIONS]}")
        return 0

    return run_watch()


if __name__ == "__main__":
    raise SystemExit(main())

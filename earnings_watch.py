#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
earnings_watch.py — vigia pontual de resultados na CVM (categoria "Dados Econômico-Financeiros").

Baseado no monitor principal: importa o fetch/parse de cvm_fatos_relevantes_claude.py.
Roda de minuto a minuto e avisa APENAS no Telegram (não envia email). Os destinos são
lidos de email_config.json (bloco telegram.destinations) — os mesmos do monitor principal,
ou seja, TODOS os configurados recebem (hoje: Danilo + Enzo). Dedup é por (protocolo, chat),
então cada pessoa recebe cada doc uma vez e uma falha de envio é retentada só para quem faltou.
Avisa no PRIMEIRO documento de resultados de cada empresa (DF, ITR ou release), com pop-up + som + Telegram; encerra quando todas saírem (ou à meia-noite).

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
import subprocess
import sys
import threading
import time
import winsound
from datetime import datetime, timedelta
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import cvm_fatos_relevantes_claude as cvm  # reaproveita make_session/fetch/parse/build_url/config

# ============================ CONFIG (edite para reusar) ============================
WATCH_TICKERS  = {"YDUQ3", "DASA3", "AFYA"}         # YDUQ3/DASA3 na CVM; AFYA na SEC/EDGAR
# Avisa no PRIMEIRO documento de resultados de cada empresa, de qualquer um destes tipos:
WATCH_CATEGORIES = {"Dados Econômico-Financeiros", "ITR - Informações Trimestrais"}
WATCH_LABEL    = "Resultados 2T26"                  # texto no cabeçalho da mensagem
SPEAK_NAMES    = {"YDUQ3": "Yduqs", "DASA3": "Dasa", "AFYA": "Afya"}  # falado: "saiu <nome>"
POLL_SECONDS   = 60                                 # de minuto a minuto
# Destinos do Telegram: lidos de email_config.json (telegram.destinations). Todos recebem.
# ===================================================================================

LOG_FILE = SCRIPT_DIR / "earnings_watch_log.txt"
SEEN_FILE = SCRIPT_DIR / "earnings_watch_seen.json"   # chaves "protocolo|chat" já ENTREGUES (persiste)

import json


def _doc_links(f) -> str:
    """
    Dois links: "Abrir" (viewer HTML do RAD) e "baixar PDF" (download direto).
    O download direto quebra no navegador embutido do Telegram no celular — a
    CVM serve o PDF com Content-Type: text/html e conta com o
    Content-Disposition para o navegador baixar; o webview do Telegram ignora
    isso e renderiza os bytes do PDF como texto. Ver build_viewer_url().
    """
    e = html.escape
    dl = e(cvm.build_download_url(f))
    viewer = cvm.build_viewer_url(f)
    if viewer:
        return (f'📄 <a href="{e(viewer)}">Abrir documento</a>'
                f'  ·  <a href="{dl}">baixar PDF</a>')
    return f'📄 <a href="{dl}">Abrir documento original</a>'


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


def announce(phrase: str) -> None:
    """Bip + fala em voz alta (TTS via SAPI do Windows) — não bloqueia o loop."""
    safe = str(phrase).replace("'", "''")
    def _run():
        try:
            for _ in range(2):
                winsound.Beep(880, 180)
                winsound.Beep(1245, 180)
        except Exception:
            pass
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(New-Object -ComObject SAPI.SpVoice).Speak('{safe}') | Out-Null"],
                creationflags=0x08000000, timeout=30,   # CREATE_NO_WINDOW
            )
        except Exception as e:
            log(f"WARNING: TTS falhou: {e}")
    try:
        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        pass


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
    """Documentos de resultados das empresas vigiadas: categorias alvo na CVM (DF/ITR/release)
    + qualquer 6-K/20-F na SEC/EDGAR (ex.: AFYA, que não está na CVM)."""
    out = [f for f in cvm.parse_rows(cvm.fetch_raw_documents(session, cvm.CVM_ALL_CATEGORIES))
           if f.ticker in WATCH_TICKERS and f.category in WATCH_CATEGORIES]
    for c in cvm.COMPANIES:  # SEC/EDGAR para tickers vigiados que têm sec_cik (ex.: AFYA)
        if c.get("ticker") in WATCH_TICKERS and c.get("sec_cik"):
            out += cvm.fetch_sec_filings(session, c)
    return out


def render(f) -> str:
    e = html.escape
    return (
        f"🔔 <b>{e(WATCH_LABEL)} — {e(f.ticker)} · {e(f.company_name)}</b>\n"
        f"<b>{e(f.category)}</b>\n"
        f"{e(f.subject or f.doc_type or 'Documento')}\n"
        f"🗓 {e(f.filing_time or '')}\n\n"
        + _doc_links(f)
    )


def _is_today(f, today_str: str, today_iso: str) -> bool:
    # CVM usa dd/mm/aaaa; SEC/EDGAR usa ISO (aaaa-mm-ddT...). Aceita os dois.
    ft = (f.filing_time or "").strip()
    return ft.startswith(today_str) or ft.startswith(today_iso)


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
    today_iso = start.strftime("%Y-%m-%d")   # para docs da SEC/EDGAR (data em ISO)
    midnight = start.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    chats = ", ".join(str(d["chat_id"]) for d in DESTINATIONS)
    log(f"Watcher iniciado. Vigiando {sorted(WATCH_TICKERS)} | {sorted(WATCH_CATEGORIES)} | hoje={today_str}.")
    log(f"Destinos ({len(DESTINATIONS)}): {chats}. Encerra à meia-noite: {midnight:%Y-%m-%d %H:%M}.")

    seen = load_seen()          # chaves "protocolo|chat" JÁ ENTREGUES (persistido)
    done: set = set()           # tickers cujo 1º doc já foi ENTREGUE a todos os destinos
    alerted: set = set()        # tickers que já dispararam pop-up + som (uma vez cada)

    broadcast(
        f"🟢 <b>Watcher {html.escape(WATCH_LABEL)} iniciado</b>\n"
        f"Vigiando <b>{', '.join(sorted(WATCH_TICKERS))}</b> — aviso no <b>1º documento</b> de resultados "
        f"de cada empresa (DF, ITR ou release). Encerro quando todas saírem "
        f"(ou à meia-noite {midnight:%H:%M})."
    )
    popup(f"Watcher {WATCH_LABEL} iniciado",
          f"Vigiando {', '.join(sorted(WATCH_TICKERS))} de minuto a minuto.\n"
          f"Pop-up + som + Telegram no primeiro documento de cada empresa.")

    while True:
        now = datetime.now()
        if now >= midnight:
            faltou = sorted(WATCH_TICKERS - done)
            broadcast(
                "🌙 <b>Watcher encerrado (meia-noite).</b>\n"
                + (f"Saíram: {', '.join(sorted(done))}.\n" if done else "Nada saiu hoje.\n")
                + (f"Não saíram: {', '.join(faltou)}." if faltou else "Todas saíram. ✅")
            )
            log(f"Encerrado à meia-noite. Saíram={sorted(done)} Faltou={faltou}")
            return 0

        try:
            # Avisa no 1º documento (DF/ITR/release) de CADA empresa e a marca 'done' (não repete).
            # Só marca entregue se o envio deu certo — retenta em falha.
            for f in fetch_watch_filings(cvm.make_session()):
                if not _is_today(f, today_str, today_iso) or f.ticker in done:
                    continue
                keys = _keys_for(f)
                if all(k in seen for k in keys):
                    done.add(f.ticker)      # já entregue (ex.: restart) — marca sem reenviar
                    continue
                if f.ticker not in alerted:  # pop-up + som UMA vez por empresa, na detecção
                    alerted.add(f.ticker)
                    name = SPEAK_NAMES.get(f.ticker, f.ticker)
                    popup(f"🔔 {WATCH_LABEL} — {f.ticker}",
                          f"{f.company_name}\n{f.category}\n{f.subject or f.doc_type or ''}\n\n"
                          f"Protocolo {f.protocol} · {f.filing_time}")
                    announce(("Saiu " + name + "! ") * 3)
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
                    done.add(f.ticker)
                    log(f"{f.ticker} capturada ({len(done)}/{len(WATCH_TICKERS)}).")
        except Exception as e:
            log(f"WARNING: erro no poll (segue no próximo minuto): {e}")

        if done == WATCH_TICKERS:
            broadcast(f"✅ <b>Todas reportaram</b> ({', '.join(sorted(WATCH_TICKERS))}). Encerrando o watcher.")
            log("Todas as empresas capturadas — encerrando (job done).")
            return 0

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
        print(f"{len(fs)} doc(s) {sorted(WATCH_CATEGORIES)} agora para {sorted(WATCH_TICKERS)}:")
        for f in fs:
            print(f"  {f.ticker} | {(f.subject or f.doc_type)} | protocolo {f.protocol} | {f.filing_time}")
        print(f"Destinos configurados: {[str(d['chat_id']) for d in DESTINATIONS]}")
        return 0

    return run_watch()


if __name__ == "__main__":
    raise SystemExit(main())

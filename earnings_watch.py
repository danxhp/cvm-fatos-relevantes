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

Roda SEM janela (pyw.exe): não há console para fechar sem querer. Para acompanhar, o log
ganha uma linha "vivo" a cada 10 min; um erro inesperado também vai para o log. Só um
watcher por vez (mutex do Windows) — um segundo se recusa a iniciar.

Uso:
    iniciar_earnings_watch.cmd       # dois cliques: inicia sem janela, a cada 20s
    pyw earnings_watch.py --poll 20  # o mesmo, pela linha de comando
    py earnings_watch.py --status    # está rodando? + últimas linhas do log
    py earnings_watch.py --stop      # pede para o watcher parar (em até --poll segundos)
    py earnings_watch.py --once      # uma checagem só (mostra o estado atual; NÃO envia nada)
    py earnings_watch.py --test      # manda um Telegram de teste para todos os destinos e sai

Reaproveitar no próximo trimestre: ajuste WATCH_TICKERS e WATCH_LABEL abaixo e rode de novo.
Quem recebe é controlado por email_config.json (telegram.destinations).
"""
from __future__ import annotations

import ctypes
import html
import os
import subprocess
import sys
import threading
import time
import traceback
import winsound
from datetime import datetime, timedelta
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import cvm_fatos_relevantes_claude as cvm  # reaproveita make_session/fetch/parse/build_url/config

# ============================ CONFIG (edite para reusar) ============================
WATCH_TICKERS  = {"AXIA3", "VBBR3"}                 # (ODPV3 = BRADSAÚDE S.A. na CVM, cód. 20125)
# Empresas FORA do monitor normal (COMPANIES), vigiadas só aqui: ticker -> (código CVM, nome).
EXTRA_COMPANIES = {"AXIA3": ("2437", "AXIA Energia"),    # AXIA ENERGIA S.A. (ex-Eletrobras)
                   "VBBR3": ("24295", "Vibra Energia")}  # VIBRA ENERGIA S/A
# Avisa no PRIMEIRO documento de resultados de cada empresa, de qualquer um destes tipos:
WATCH_CATEGORIES = {"Dados Econômico-Financeiros", "ITR - Informações Trimestrais"}
WATCH_LABEL    = "Resultados 3T26"                  # texto no cabeçalho da mensagem
SPEAK_NAMES    = {"AXIA3": "Axia", "VBBR3": "Vibra", "HYPE3": "Hypera", "ODPV3": "Bradesco Saúde"}  # falado: "saiu <nome>"
SPEAK_TIMES    = 2                                  # quantas vezes fala "saiu <nome>"
POLL_SECONDS   = 30                                 # intervalo entre checagens (sobrescreva com --poll N)
# Destinos do Telegram: lidos de email_config.json (telegram.destinations). Todos recebem.
# ===================================================================================

# EXTRA_COMPANIES entram no mapa de códigos só neste processo (o monitor normal não as vê).
for _t, (_code, _name) in EXTRA_COMPANIES.items():
    cvm.COMPANY_CODES.setdefault(cvm.normalize_cvm_code(_code),
                                 {"ticker": _t, "name": _name, "cvm_code": _code})

LOG_FILE = SCRIPT_DIR / "earnings_watch_log.txt"
SEEN_FILE = SCRIPT_DIR / "earnings_watch_seen.json"   # chaves "protocolo|chat" já ENTREGUES (persiste)
STOP_FILE = SCRIPT_DIR / "earnings_watch.stop"        # criado por --stop; o watcher para ao vê-lo
HEARTBEAT_SECONDS = 600                               # linha "vivo" no log a cada 10 min
MUTEX_NAME = "Local\\earnings_watch_cvm"             # um watcher por vez nesta sessão do Windows

# Sem console (pyw.exe) sys.stdout/stderr são None: print vira no-op e um traceback solto
# (ex.: numa thread) sumiria. Manda o stderr para o log, para nada morrer sem registro.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(LOG_FILE, "a", encoding="utf-8", buffering=1)

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateMutexW.restype = ctypes.c_void_p
_k32.OpenMutexW.restype = ctypes.c_void_p
_mutex = None   # handle mantido vivo enquanto o processo roda; o Windows o solta se o processo morrer

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
                f'  ·  <a href="{dl}">baixar {"PDF" if f.desc_type == "IPE" else "ZIP"}</a>')
    return f'📄 <a href="{dl}">Abrir documento original</a>'


def _desc(f) -> str:
    """Assunto do documento. ITR/DFP vêm sem assunto e com tipo "-" (a categoria já vai no alerta)."""
    for x in (f.subject, f.doc_type):
        if x and x.strip(" -"):
            return x
    return "Documento"


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {cvm.redact_secrets(msg)}"  # token na URL de erro
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _claim_single_instance() -> bool:
    """True se este é o único watcher. Dois ao mesmo tempo mandariam cada doc em dobro."""
    global _mutex
    _mutex = _k32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() != 183          # ERROR_ALREADY_EXISTS


def _watcher_running() -> bool:
    h = _k32.OpenMutexW(0x00100000, False, MUTEX_NAME)   # SYNCHRONIZE
    if h:
        _k32.CloseHandle(ctypes.c_void_p(h))
    return bool(h)


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
    # Passa WATCH_CATEGORIES ao parse: o monitor normal NÃO vigia ITR (fora de
    # CVM_ALLOWED_CATEGORIES), mas aqui o ITR costuma ser o 1º documento de resultados.
    raw = cvm.fetch_raw_documents(session, cvm.CVM_ALL_CATEGORIES)
    out = [f for f in cvm.parse_rows(raw, WATCH_CATEGORIES) if f.ticker in WATCH_TICKERS]
    for c in cvm.COMPANIES:  # SEC/EDGAR para tickers vigiados que têm sec_cik (ex.: AFYA)
        if c.get("ticker") in WATCH_TICKERS and c.get("sec_cik"):
            out += cvm.fetch_sec_filings(session, c)
    return out


def render(f) -> str:
    e = html.escape
    return (
        f"🔔 <b>{e(WATCH_LABEL)} — {e(f.ticker)} · {e(f.company_name)}</b>\n"
        f"<b>{e(f.category)}</b>\n"
        f"{e(_desc(f))}\n"
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

    if not _claim_single_instance():
        log("ERRO: já existe um watcher rodando (veja py earnings_watch.py --status). Não inicio outro.")
        return 1
    STOP_FILE.unlink(missing_ok=True)   # pedido de parada antigo não vale para esta execução

    start = datetime.now()
    today_str = start.strftime("%d/%m/%Y")
    today_iso = start.strftime("%Y-%m-%d")   # para docs da SEC/EDGAR (data em ISO)
    midnight = start.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    chats = ", ".join(str(d["chat_id"]) for d in DESTINATIONS)
    log(f"Watcher iniciado. Vigiando {sorted(WATCH_TICKERS)} | {sorted(WATCH_CATEGORIES)} | hoje={today_str} | a cada {POLL_SECONDS}s.")
    log(f"Destinos ({len(DESTINATIONS)}): {chats}. Encerra à meia-noite: {midnight:%Y-%m-%d %H:%M}. "
        f"PID {os.getpid()} ({Path(sys.executable).name}).")
    n_ok = n_fail = 0           # checagens na CVM que deram certo / com erro (para a linha "vivo")
    last_ok = None
    next_beat = start + timedelta(seconds=HEARTBEAT_SECONDS)

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
          f"Vigiando {', '.join(sorted(WATCH_TICKERS))} a cada {POLL_SECONDS}s.\n"
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
        if STOP_FILE.exists():
            STOP_FILE.unlink(missing_ok=True)
            log(f"Parado a pedido (--stop). Saíram={sorted(done)} Faltou={sorted(WATCH_TICKERS - done)}")
            return 0

        try:
            # Avisa no 1º documento (DF/ITR/release) de CADA empresa e a marca 'done' (não repete).
            # Só marca entregue se o envio deu certo — retenta em falha.
            filings = fetch_watch_filings(cvm.make_session())
            n_ok, last_ok = n_ok + 1, datetime.now()
            for f in filings:
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
                          f"{f.company_name}\n{f.category}\n{_desc(f)}\n\n"
                          f"Protocolo {f.protocol} · {f.filing_time}")
                    announce(("Saiu " + name + "! ") * SPEAK_TIMES)
                text = render(f)
                for d in DESTINATIONS:
                    key = f"{f.protocol}|{d['chat_id']}"
                    if key in seen:
                        continue
                    if _post(d["bot_token"], d["chat_id"], text):
                        seen.add(key)
                        save_seen(seen)
                        log(f"ENTREGUE {f.ticker} | {_desc(f)} | proto {f.protocol} | {f.filing_time} -> chat {d['chat_id']}")
                    else:
                        log(f"FALHA {f.ticker} | proto {f.protocol} -> chat {d['chat_id']} (retento no próximo minuto)")
                if all(k in seen for k in keys):
                    done.add(f.ticker)
                    log(f"{f.ticker} capturada ({len(done)}/{len(WATCH_TICKERS)}).")
        except Exception as e:
            n_fail += 1
            log(f"WARNING: erro no poll (segue no próximo minuto): {e}")

        if now >= next_beat:   # prova de vida: se o processo morrer, o buraco aparece no log
            ult = f"{last_ok:%H:%M:%S}" if last_ok else "nenhuma"
            log(f"vivo — {n_ok} checagens OK, {n_fail} com erro; última OK às {ult}.")
            next_beat = now + timedelta(seconds=HEARTBEAT_SECONDS)

        if done == WATCH_TICKERS:
            broadcast(f"✅ <b>Todas reportaram</b> ({', '.join(sorted(WATCH_TICKERS))}). Encerrando o watcher.")
            log("Todas as empresas capturadas — encerrando (job done).")
            return 0

        time.sleep(POLL_SECONDS)


def main() -> int:
    global POLL_SECONDS
    if "--poll" in sys.argv:   # ex.: --poll 20 → checa a cada 20s só nesta execução
        POLL_SECONDS = int(sys.argv[sys.argv.index("--poll") + 1])

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
            print(f"  {f.ticker} | {_desc(f)} | protocolo {f.protocol} | {f.filing_time}")
        print(f"Destinos configurados: {[str(d['chat_id']) for d in DESTINATIONS]}")
        return 0

    if "--status" in sys.argv:
        print("Watcher RODANDO." if _watcher_running() else "Nenhum watcher rodando.")
        try:
            print("Últimas linhas do log:\n" + "".join(LOG_FILE.read_text(encoding="utf-8").splitlines(True)[-4:]))
        except Exception:
            pass
        return 0

    if "--stop" in sys.argv:
        if not _watcher_running():
            print("Nenhum watcher rodando.")
            return 0
        STOP_FILE.touch()
        print(f"Pedido de parada enviado; o watcher para em até {POLL_SECONDS}s (confira com --status).")
        return 0

    try:
        return run_watch()
    except BaseException:
        # Sem console (pyw), o traceback sumiria: registra no log antes de sair.
        log("ENCERRADO POR ERRO INESPERADO:\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    raise SystemExit(main())

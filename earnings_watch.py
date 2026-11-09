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
    pyw earnings_watch.py --start-at 17:00   # abre agora, só começa a vigiar às 17h (ver START_AT)
    py earnings_watch.py --status    # está rodando? + últimas linhas do log
    py earnings_watch.py --stop      # pede para o watcher parar (em até --poll segundos)
    py earnings_watch.py --once      # uma checagem só (mostra o estado atual; NÃO envia nada)
    py earnings_watch.py --test      # manda um Telegram de teste para todos os destinos e sai

Reaproveitar no próximo trimestre: ajuste WATCH_TICKERS e WATCH_LABEL abaixo e rode de novo.
Quem recebe é controlado por email_config.json (telegram.destinations).
"""
from __future__ import annotations

import ctypes
import functools
import html
import io
import math
import os
import struct
import subprocess
import sys
import threading
import time
import traceback
import wave
import winsound
from datetime import datetime, timedelta
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import cvm_fatos_relevantes_claude as cvm  # reaproveita make_session/fetch/parse/build_url/config

# ============================ CONFIG (edite para reusar) ============================
WATCH_TICKERS  = {"BRAV3", "MATD3", "TIMS3"}   # (ODPV3 = BRADSAÚDE S.A. na CVM, cód. 20125)
# Empresas FORA do monitor normal (COMPANIES), vigiadas só aqui: ticker -> (código CVM, nome).
# Ex.: {"AXIA3": ("2437", "AXIA Energia"), "VBBR3": ("24295", "Vibra Energia"),
#       "RECV3": ("25780", "PetroRecôncavo")}
# BRAV3: cód. 25291 (BRAVA ENERGIA S.A., ex-3R) no cadastro de companhias abertas da CVM
# (cad_cia_aberta.csv). MATD3/TIMS3 já estão no COMPANIES.
EXTRA_COMPANIES = {"BRAV3": ("25291", "Brava Energia")}
# Avisa no PRIMEIRO documento de resultados de cada empresa, de qualquer um destes tipos:
WATCH_CATEGORIES = {"Dados Econômico-Financeiros", "ITR - Informações Trimestrais"}
WATCH_LABEL    = "Resultados 3T26"                  # texto no cabeçalho da mensagem
# A CVM também põe comentários de agência de rating em "Dados Econômico-Financeiros"
# (ex.: ANIM3 29/10 "Moodys Local - Comentário..."). Sem filtro, um deles dispararia o
# alarme e marcaria a empresa como reportada — e o resultado de verdade passaria batido.
# Idem para "Relatório de Agente Fiduciário" de debêntures (BRAV3/MATD3/TIMS3, fim de abril).
IGNORE_SUBJECTS = ("moody", "fitch", "s&p", "standard & poor", "rating",
                   "agente fiduci")   # trecho do assunto, sem caixa
SPEAK_NAMES    = {"ANIM3": "Ânima", "DASA3": "Dasa", "FLRY3": "Fleury",
                  "AXIA3": "Axia", "VBBR3": "Vibra", "HYPE3": "Hypera", "ODPV3": "Bradesco Saúde",
                  "RECV3": "PetroRecôncavo", "BRAV3": "Brava", "MATD3": "Mater Dei",
                  "TIMS3": "TIM"}  # falado: "saiu <nome>"
SPEAK_TIMES    = 2                                  # quantas vezes fala "saiu <nome>"
POLL_SECONDS   = 30                                 # intervalo entre checagens (sobrescreva com --poll N)
# Só começa a vigiar neste horário (hora do PC = BRT): até lá o processo fica aberto, calado,
# sem consultar a CVM. "AAAA-MM-DD HH:MM" ou "HH:MM" (hoje). None = começa na hora.
# Data/hora que já passou = começa na hora (config velha não trava um uso futuro).
# Sobrescreva com --start-at HH:MM ou --start-at agora.
START_AT       = None
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


@functools.lru_cache(maxsize=1)
def _chime_wav() -> bytes:
    """
    Gongo curto e suave antes da fala, no estilo dos avisos de estação de metrô: 3 notas
    subindo (Mi maior: Mi5, Sol#5, Si5), timbre de vibrafone (fundamental + parcial 4x que
    some rápido), ataque de 8 ms para não estalar. Gerado em memória — sem arquivo de som.
    """
    rate = 44100
    notes = ((0.00, 659.25, 0.45), (0.17, 830.61, 0.45), (0.34, 987.77, 0.80))  # (início s, Hz, decaimento s)
    total = 1.7
    n = int(rate * total)
    buf = [0.0] * n
    for start, freq, tau in notes:
        i0 = int(start * rate)
        for i in range(n - i0):
            t = i / rate
            env = min(1.0, t / 0.008) * math.exp(-t / tau)
            if t > 0.008 and env < 1e-4:          # já decaiu (o ataque começa em 0: não pare nele)
                break
            buf[i0 + i] += env * (math.sin(2 * math.pi * freq * t)
                                  + 0.12 * math.exp(-t / 0.08) * math.sin(2 * math.pi * 4 * freq * t))
    fade = int(0.05 * rate)                       # rampa final: sem clique no fim
    for i in range(fade):
        buf[n - 1 - i] *= i / fade
    peak = max(abs(x) for x in buf) or 1.0
    scale = 0.45 * 32767 / peak                   # volume moderado (45% do máximo)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{n}h", *(int(x * scale) for x in buf)))
    return out.getvalue()


def _play_chime() -> None:
    try:
        winsound.PlaySound(_chime_wav(), winsound.SND_MEMORY)   # síncrono (roda na thread do announce)
    except Exception as e:
        log(f"WARNING: gongo falhou ({e}); usando bip.")
        try:
            winsound.Beep(880, 180)
            winsound.Beep(1245, 180)
        except Exception:
            pass


def announce(phrase: str) -> None:
    """Gongo + fala em voz alta (TTS via SAPI do Windows) — não bloqueia o loop."""
    safe = str(phrase).replace("'", "''")
    def _run():
        _play_chime()
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


def _parse_start_at(value):
    """'AAAA-MM-DD HH:MM' ou 'HH:MM' (hoje) -> datetime; None/''/'agora' -> None."""
    if not value or str(value).strip().lower() in ("agora", "now", "none"):
        return None
    value = str(value).strip()
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M")
    except ValueError:
        pass
    try:
        hm = datetime.strptime(value, "%H:%M")
    except ValueError:
        raise ValueError(f"START_AT inválido: {value!r} (use 'AAAA-MM-DD HH:MM', 'HH:MM' ou 'agora')")
    return datetime.now().replace(hour=hm.hour, minute=hm.minute, second=0, microsecond=0)


def _wait_until(start_at: datetime) -> bool:
    """
    Fica aberto e calado até start_at (sem consultar a CVM, sem Telegram). Segura o mutex,
    então --status mostra RODANDO e um 2º watcher se recusa. False se pediram --stop.
    """
    log(f"Agendado: começa a vigiar {sorted(WATCH_TICKERS)} às {start_at:%d/%m/%Y %H:%M}. "
        f"Aguardando (PID {os.getpid()}).")
    popup(f"Watcher {WATCH_LABEL} agendado",
          f"Começa às {start_at:%H:%M} — {', '.join(sorted(WATCH_TICKERS))}.\n"
          f"Até lá fica aguardando, sem checar a CVM.")
    next_beat = datetime.now() + timedelta(minutes=30)
    while True:
        now = datetime.now()
        if now >= start_at:
            return True
        if STOP_FILE.exists():
            STOP_FILE.unlink(missing_ok=True)
            log("Parado a pedido (--stop) antes do horário de início.")
            return False
        if now >= next_beat:   # prova de vida também na espera
            log(f"aguardando — começa às {start_at:%H:%M}.")
            next_beat = now + timedelta(minutes=30)
        time.sleep(max(1.0, min(15.0, (start_at - now).total_seconds())))


def run_watch() -> int:
    if not DESTINATIONS:
        log("ERRO: nenhum destino de Telegram em email_config.json (telegram.destinations). Abortando.")
        return 1

    if not _claim_single_instance():
        log("ERRO: já existe um watcher rodando (veja py earnings_watch.py --status). Não inicio outro.")
        return 1
    STOP_FILE.unlink(missing_ok=True)   # pedido de parada antigo não vale para esta execução

    start_at = _parse_start_at(START_AT)
    if start_at and start_at > datetime.now() and not _wait_until(start_at):
        return 0

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
    ignored: set = set()        # protocolos descartados por IGNORE_SUBJECTS (loga uma vez)

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
                if any(s in _desc(f).lower() for s in IGNORE_SUBJECTS):
                    if f.protocol not in ignored:
                        ignored.add(f.protocol)
                        log(f"Ignorado (rating, não é resultado): {f.ticker} | {_desc(f)} | proto {f.protocol}")
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
    global POLL_SECONDS, START_AT
    if "--poll" in sys.argv:   # ex.: --poll 20 → checa a cada 20s só nesta execução
        POLL_SECONDS = int(sys.argv[sys.argv.index("--poll") + 1])
    if "--start-at" in sys.argv:   # ex.: --start-at 17:00 | --start-at agora
        START_AT = sys.argv[sys.argv.index("--start-at") + 1]
    _parse_start_at(START_AT)      # valida já: erro de digitação aparece antes de agendar

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

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Recompra mensal da América Móvil (AMX) via BMV -> Telegram.

POR QUE ESTE SCRIPT EXISTE
--------------------------
A AMX divulga recompra à BMV num relatório "Información de recompra" por dia de
operação, publicado NO MESMO DIA. Isso não vira 6-K na SEC (a AMX protocola de 9
a 29 6-K por ano, nunca em cadência diária), e o 20-F só traz a tabela mensal uma
vez por ano, com até 16 meses de defasagem. Ou seja: a BMV é a única fonte
tempestiva, e ela não tem índice público.

COMO ACHAMOS OS DOCUMENTOS SEM ÍNDICE
-------------------------------------
A BMV numera TODOS os documentos públicos num contador global e sequencial
(~254/dia), independente do tipo. Validado extrapolando de um documento de
19/12/2024 (id 1428024) até 01/09/2026: previsto 1585758, real 1586058 — erro de
300 IDs em 621 dias. Então basta sondar o espaço de IDs:

    HEAD /docs-pub/recompra/recompra_<id>_1.pdf   ->  200 = é recompra

CUSTO: por que mensal e não diário
----------------------------------
O remanente do fundo é saldo corrente, então a recompra do mês sai de DOIS
relatórios: o último do mês N e o último do mês N-1. Com o estado persistido,
cada execução mensal só varre de trás para frente até achar o relatório mais
recente da AMX — algumas centenas a poucos milhares de IDs, em vez dos ~7.600
que uma varredura do mês inteiro exigiria.

A ARMADILHA: RECOMPOSIÇÃO DO FUNDO
----------------------------------
O delta do remanente só vale se o fundo não tiver sido recomposto no meio. A
assembleia anual autoriza recursos novos e o remanente SOBE. Medido na tabela do
20-F FY2025: remanente fim de ABR/2025 = 10.611.365.225 e fim de MAI/2025 =
18.145.547.145, enquanto a recompra real de maio foi 146.904.329 ações @ 16,88 =
MXN 2.479.745.073. O delta ingênuo daria MENOS 7,5 bilhões — erro de 404%.
Por isso: se o remanente SOBE, o script NÃO reporta número; ele avisa.

Uso:
    python amx_recompra.py --bootstrap   # 1a vez: acha 2 relatórios (varredura maior)
    python amx_recompra.py               # execução mensal normal
    python amx_recompra.py --dry-run     # calcula e imprime, não manda no Telegram
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import io
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pypdf
import requests

import cvm_fatos_relevantes_claude as cvm


SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE = SCRIPT_DIR / "amx_recompra_state.json"
LOG_FILE = SCRIPT_DIR / "amx_recompra_log.txt"

BMV_HOME = "https://www.bmv.com.mx/"
RECOMPRA_URL = "https://www.bmv.com.mx/docs-pub/recompra/recompra_{}_1.pdf"
CLAVE = "AMX"

# Contador global da BMV avança ~254 IDs/dia. A varredura normal só precisa
# alcançar o relatório mais recente da AMX; o teto existe porque a AMX não compra
# todo dia (blackout perto de resultado) e a profundidade é imprevisível.
SCAN_CHUNK = 400
SCAN_MAX_NORMAL = 8000      # ~31 dias
SCAN_MAX_BOOTSTRAP = 20000  # ~79 dias, o bastante para cruzar 2 fechamentos
MAX_WORKERS = 20  # medido: 8w=9 IDs/s, 20w=27, 40w=44. 20 equilibra velocidade e educação com o servidor da BMV

FX_URL = "https://api.frankfurter.app/{ini}..{fim}?from=MXN&to=USD"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def log(msg: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ============================================================================
# ESTADO
# ============================================================================

def load_state() -> Dict:
    if not STATE_FILE.exists():
        return {"reports": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {"reports": {}}
    except Exception as e:
        log(f"WARNING: estado ilegível ({e}); começando vazio.")
        return {"reports": {}}


def save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


# ============================================================================
# DESCOBERTA E PARSE
# ============================================================================

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    # Sem pool do tamanho do fan-out, as threads brigam por conexão e a
    # varredura fica 3x mais lenta.
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS, max_retries=1
    )
    s.mount("https://", adapter)
    return s


def current_max_id(session: requests.Session) -> Optional[int]:
    """
    Âncora da varredura: a home da BMV lista documentos recém-publicados, de
    vários tipos, todos com IDs do mesmo contador global. O maior deles é o
    topo aproximado do espaço de IDs.
    """
    try:
        r = session.get(BMV_HOME, timeout=40)
        r.raise_for_status()
    except Exception as e:
        log(f"ERROR: não consegui ler a home da BMV: {e}")
        return None
    ids = [int(x) for x in re.findall(r"/docs-pub/[a-z_]+/[a-z_]+_(\d{6,9})_\d+\.pdf", r.text)]
    if not ids:
        log("ERROR: home da BMV não trouxe nenhum ID — o layout mudou?")
        return None
    return max(ids)


def _num(s: str) -> float:
    """'59,899,000,000' -> 59899000000.0 ; tolera zero colado à esquerda."""
    return float(s.replace(",", "").lstrip("0") or "0")


def parse_report(pdf_bytes: bytes) -> Optional[Dict]:
    """
    Extrai um relatório de recompra. Devolve None se não for da clave vigiada.

    O remanente e a tabela de operações extraem limpo. O bloco SALDOS, não: o
    pypdf embaralha a ordem das colunas de forma inconsistente entre as duas
    linhas do MESMO pdf —
        'Al último reporte  59,899,000,000 0271,000,000'
        'Al presente reporte 273,500,000 59,896,500,000 0'
    Por isso a tesouraria é identificada por MAGNITUDE (tesouraria ~271M vs
    circulação ~59,9bi, 200x de diferença), não por posição.
    """
    try:
        text = "\n".join((p.extract_text() or "") for p in pypdf.PdfReader(io.BytesIO(pdf_bytes)).pages)
    except Exception:
        return None

    m_clave = re.search(r"CLAVE DE COTIZACIÓN\s+(\S+)", text)
    if not m_clave or m_clave.group(1).strip().upper() != CLAVE:
        return None

    m_fecha = re.search(r"FECHA DE OPERACIÓN\s+(\d{2}/\d{2}/\d{4})", text)
    m_rem = re.search(
        r"REMANENTE DE RECURSOS\s*([\d,]+)\s*Al presente\s*Al último reporte\s*([\d,]+)", text
    )
    if not (m_fecha and m_rem):
        return None

    # Operações do dia: FOLIO, qtd, Compra/Venta, preço unitário, importe.
    ops = re.findall(r"^\d+\s+([\d,]+)(Compra|Venta)\s+\$\s*([\d.]+)\s+\$\s*([\d,]+)", text, re.M)
    qtd_dia = sum(_num(o[0]) for o in ops)
    imp_dia = sum(_num(o[3]) for o in ops)

    rem_now, rem_prev = _num(m_rem.group(1)), _num(m_rem.group(2))

    # Conferência interna: a soma das operações do dia tem que bater com o
    # consumo do remanente. Se divergir, a extração quebrou.
    consistente = abs(imp_dia - (rem_prev - rem_now)) < 1.0

    tesoreria = None
    m_pres = re.search(r"Al presente reporte\s+([\d,\s]+)", text)
    if m_pres:
        cand = [_num(x) for x in re.findall(r"[\d,]{4,}", m_pres.group(1))]
        cand = [c for c in cand if c > 0]
        if cand:
            tesoreria = min(cand)  # tesouraria << circulação

    return {
        "fecha": m_fecha.group(1),
        "remanente": rem_now,
        "remanente_anterior": rem_prev,
        "tesoreria": tesoreria,
        "acoes_dia": qtd_dia,
        "importe_dia": imp_dia,
        "operacoes_dia": len(ops),
        "consistente": consistente,
    }


def probe(session: requests.Session, doc_id: int) -> bool:
    try:
        return session.head(RECOMPRA_URL.format(doc_id), timeout=12).status_code == 200
    except Exception:
        return False


def fetch(session: requests.Session, doc_id: int) -> Optional[bytes]:
    try:
        r = session.get(RECOMPRA_URL.format(doc_id), timeout=40)
        return r.content if r.status_code == 200 else None
    except Exception:
        return None


def find_latest_amx(
    session: requests.Session, from_id: int, scan_max: int, before: Optional[date] = None
) -> Optional[Dict]:
    """
    Varre de `from_id` para trás procurando o relatório de recompra da AMX mais
    recente. Se `before` for dado, ignora relatórios com data de operação
    posterior — usado no bootstrap para achar o fechamento do mês anterior.
    """
    scanned = 0
    top = from_id
    while scanned < scan_max:
        lo = max(1, top - SCAN_CHUNK)
        ids = list(range(lo, top))
        hits: List[int] = []
        with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            for doc_id, ok in zip(ids, ex.map(lambda i: probe(session, i), ids)):
                if ok:
                    hits.append(doc_id)
        # Baixar/parsear em série custava ~40s por chunk (são ~30 recompra a cada
        # 400 IDs, de todas as emissoras). Em paralelo cai para ~5s.
        ordenados = sorted(hits, reverse=True)
        with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            baixados = list(ex.map(lambda i: (i, fetch(session, i)), ordenados))
        for doc_id, data in baixados:
            if not data:
                continue
            rep = parse_report(data)
            if not rep:
                continue
            dt = datetime.strptime(rep["fecha"], "%d/%m/%Y").date()
            if before and dt >= before:
                continue
            rep["id"] = doc_id
            log(f"  achado: id={doc_id} fecha={rep['fecha']} "
                f"remanente={rep['remanente']:,.0f} consistente={rep['consistente']}")
            return rep
        scanned += len(ids)
        top = lo
        log(f"  ...varridos {scanned} IDs, {len(hits)} recompra (nenhum AMX ainda)")
    return None


# ============================================================================
# CÂMBIO
# ============================================================================

def fx_media_periodo(ini: date, fim: date) -> Optional[float]:
    """Média simples das cotações diárias MXN->USD no período (dias úteis)."""
    try:
        r = requests.get(FX_URL.format(ini=ini.isoformat(), fim=fim.isoformat()), timeout=30)
        r.raise_for_status()
        rates = r.json().get("rates", {})
        vals = [list(v.values())[0] for v in rates.values() if v]
        if not vals:
            return None
        return sum(vals) / len(vals)
    except Exception as e:
        log(f"WARNING: câmbio indisponível ({e}) — reporto só em MXN.")
        return None


# ============================================================================
# RELATÓRIO
# ============================================================================

def _mxn(v: float) -> str:
    """MXN compacto: bilhoes acima de 1bn, senao milhoes. Uma casa decimal."""
    if abs(v) >= 1_000_000_000:
        return f"MXN {v/1_000_000_000:,.1f}bn"
    return f"MXN {v/1_000_000:,.1f}mn"


def montar_mensagem(mes: str, atual: Dict, anterior: Dict, fx: Optional[float]) -> str:
    e = __import__("html").escape
    delta_rem = anterior["remanente"] - atual["remanente"]

    linhas = [f"<b>🇲🇽 AMX — Recompra de ações · {e(mes)}</b>", ""]

    if delta_rem < 0:
        # Assembleia recompôs o fundo: o delta perde o sentido econômico.
        linhas += [
            "⚠️ <b>Recomposição do fundo detectada.</b>",
            f"A verba disponível <b>subiu</b> de {_mxn(anterior['remanente'])} para "
            f"{_mxn(atual['remanente'])} — a assembleia autorizou recursos novos "
            f"no período.",
            "",
            "O valor recomprado no mês <b>não pode ser deduzido do saldo do fundo</b> "
            "e precisa de apuração à parte. Ver a resolução da assembleia.",
        ]
    else:
        acoes = None
        if atual.get("tesoreria") and anterior.get("tesoreria"):
            d = atual["tesoreria"] - anterior["tesoreria"]
            if d > 0:
                acoes = d
        linhas.append(f"💰 Recomprado: <b>{_mxn(delta_rem)}</b>")
        if fx:
            linhas.append(f"       ≈ <b>USD {delta_rem * fx / 1_000_000:,.1f}mn</b> "
                          f"(câmbio médio: 1 USD = {1/fx:,.2f} MXN)")
        if acoes:
            linhas.append(f"📊 Ações: <b>{acoes/1_000_000:,.1f}mn</b>  ·  preço médio "
                          f"MXN {delta_rem/acoes:,.2f}")
        linhas.append("")
        # "Remanente de recursos" no relatorio da BMV: quanto AINDA resta da verba
        # que a assembleia autorizou para recompra. E o limite de fogo restante,
        # nao caixa da companhia.
        linhas.append(f"🏦 Verba de recompra ainda disponível: <b>{_mxn(atual['remanente'])}</b>")

    linhas += [
        "",
        f"<i>Fechamento: {e(atual['fecha'])} (relatório {atual['id']}) · "
        f"base: {e(anterior['fecha'])} (relatório {anterior['id']})</i>",
        "<i>Fonte: BMV — Información de recompra</i>",
    ]
    if not atual.get("consistente", True):
        linhas.append("⚠️ <i>Extração do relatório de fechamento não fechou a conferência interna.</i>")
    return "\n".join(linhas)


def enviar(texto: str) -> bool:
    cfg = cvm.get_telegram_config()
    if not cfg:
        log("ERROR: Telegram não configurado.")
        return False
    ok = True
    for dest in cfg["destinations"]:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{dest['bot_token']}/sendMessage",
                json={"chat_id": dest["chat_id"], "text": texto,
                      "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=30,
            )
            r.raise_for_status()
            log(f"Telegram enviado (chat {dest['chat_id']}).")
        except Exception as ex:
            log(f"ERROR ao enviar Telegram (chat {dest['chat_id']}): {ex}")
            ok = False
    return ok


# ============================================================================
# MAIN
# ============================================================================

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bootstrap", action="store_true",
                    help="primeira execução: varre mais fundo para achar 2 fechamentos")
    ap.add_argument("--dry-run", action="store_true", help="não envia no Telegram")
    ap.add_argument("--mes-ate", type=lambda x: datetime.strptime(x, "%Y-%m-%d").date(),
                    default=None, metavar="AAAA-MM-DD",
                    help="finge que hoje é esta data (teste/backfill)")
    args = ap.parse_args()

    session = make_session()
    state = load_state()

    top = current_max_id(session)
    if not top:
        return 1
    log(f"Topo do espaço de IDs na BMV: {top}")

    scan_max = SCAN_MAX_BOOTSTRAP if args.bootstrap else SCAN_MAX_NORMAL

    # O alvo é sempre o ÚLTIMO MÊS FECHADO, não o mês corrente: rodando no dia 1º
    # de setembro, queremos o fechamento de agosto. Por isso o corte exclui os
    # relatórios do mês em curso.
    hoje = args.mes_ate or date.today()
    primeiro_do_mes_atual = hoje.replace(day=1)
    fim_mes_alvo = primeiro_do_mes_atual - timedelta(days=1)
    mes_ref = fim_mes_alvo.strftime("%m/%Y")
    log(f"Mês de referência: {mes_ref} (fechamento até {fim_mes_alvo})")

    log(f"Procurando o fechamento da AMX em {mes_ref} (teto de {scan_max} IDs)...")
    atual = find_latest_amx(session, top, scan_max, before=primeiro_do_mes_atual)
    if not atual:
        log("Nenhum relatório de recompra da AMX no período varrido — "
            "possível janela de blackout ou programa parado.")
        return 0

    dt_atual = datetime.strptime(atual["fecha"], "%d/%m/%Y").date()

    # Base de comparação: o fechamento guardado da execução anterior.
    anterior = state.get("ultimo_fechamento")
    if args.bootstrap or not anterior:
        inicio_mes = fim_mes_alvo.replace(day=1)
        log(f"Bootstrap: procurando o fechamento anterior a {inicio_mes}...")
        anterior = find_latest_amx(session, atual["id"], scan_max, before=inicio_mes)
        if not anterior:
            log("Não achei um segundo relatório para servir de base. "
                "Guardei o atual; rode de novo no próximo mês.")
            state["ultimo_fechamento"] = atual
            save_state(state)
            return 0

    dt_ant = datetime.strptime(anterior["fecha"], "%d/%m/%Y").date()
    if dt_ant >= dt_atual:
        log("Base não é anterior ao fechamento atual — nada novo a reportar.")
        return 0

    fx = fx_media_periodo(dt_ant, dt_atual)
    texto = montar_mensagem(mes_ref, atual, anterior, fx)

    print()
    print(re.sub(r"<[^>]+>", "", texto))
    print()

    if args.dry_run:
        log("--dry-run: não enviei.")
        return 0

    if enviar(texto):
        state["ultimo_fechamento"] = atual
        state.setdefault("reports", {})[mes_ref] = atual
        save_state(state)
        log("Estado atualizado.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

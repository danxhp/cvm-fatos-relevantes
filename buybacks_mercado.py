#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Dados de mercado para o painel de recompras: cotação diária (B3) e quantidade de
ações (CVM). Tudo de fonte oficial e sem chave de API, com cache versionado em
buybacks_mercado.json — a execução mensal só baixa o mês novo.

COTAÇÃO — B3, "Séries históricas" (COTAHIST_M<mm><aaaa>.ZIP)
-------------------------------------------------------------
Arquivo texto de largura fixa com um registro por ativo por pregão (~10 MB
zipado por mês). Guardamos, por ticker e pregão, abertura, fechamento, mínima,
máxima, volume financeiro e quantidade — só do mercado à vista (TPMERC 010).
O código BDI não é filtrado como "02" (lote padrão) porque empresa em
recuperação muda de BDI: a ONCO3 passou para 07 (recuperação extrajudicial) em
15/07/2026 e sumiria do painel.

Ticker que muda de código continua sendo a mesma série (B3_ALIASES): a ODPV3
virou SAUD3 (Bradsaúde) em 05/05/2026.

EVENTOS SOCIETÁRIOS — grupamento, desdobramento, bonificação
------------------------------------------------------------
Mudam a quantidade de ações sem mudar o valor da empresa. Sem ajuste, a HAPV3
"sobe" de R$ 2,70 para R$ 40,00 num pregão (grupamento 15:1) e a contagem de
ações recompradas mistura bases diferentes. Tudo no painel fica na base de
ações ATUAL: antes do 1º pregão "ex", quantidades são multiplicadas e preços
divididos pelo fator. A tabela é curada — ver EVENTOS_ACOES.

AÇÕES — CVM, ITR/DFP "composição do capital"
--------------------------------------------
Trimestral (ITR: 03, 06, 09; DFP: 12). Total de ações integralizadas e em
tesouraria. ARMADILHA: há quem informe em MILHARES (YDUQ3: 274.089 = 274,1 mi
de ações). Corrigimos quando o total é implausível (< 10 mi); o painel ainda
compara a tesouraria do ITR com a do formulário mensal e avisa se divergir.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import requests

import cvm_fatos_relevantes_claude as cvm


SCRIPT_DIR = Path(__file__).resolve().parent
MARKET_FILE = SCRIPT_DIR / "buybacks_mercado.json"

COTAHIST_URL = "https://bvmf.bmfbovespa.com.br/InstDados/SerHist/COTAHIST_M{mm}{yyyy}.ZIP"
CVM_DOC_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/{kind}/DADOS/{kind_l}_cia_aberta_{year}.zip"

# Outros códigos de negociação da MESMA ação (mudança de ticker). A chave é o
# ticker usado no monitor (COMPANIES); os aliases são procurados no COTAHIST.
B3_ALIASES: Dict[str, List[str]] = {
    "ODPV3": ["SAUD3"],  # Odontoprev -> Bradsaúde, a partir de 05/05/2026
}

# Eventos que mudam a QUANTIDADE de ações sem mudar o valor da empresa.
# (1º pregão ex, fator, 1º ITR/DFP já na base nova) — fator = ações depois /
# ações antes. Data e fator conferidos no salto de abertura do COTAHIST e na
# linha "bonificação" / saldo dos formulários mensais (ex.: BLAU3 tesouraria
# 1.713.200 -> 2.227.160). A 3ª data existe porque o ITR/DFP pode já contar as
# ações novas antes do 1º pregão ex (BLAU3: DFP de 31/12/2025, ex em 06/01/2026).
# Evento novo: o build avisa quando um formulário traz linha de bonificação/
# desdobramento/grupamento sem evento aqui, ou quando a abertura de um pregão
# difere mais de 25% do fechamento anterior. Confirme no ITR (composição do
# capital) ou no fato relevante e acrescente aqui.
EVENTOS_ACOES: Dict[str, List[Tuple[str, float, str]]] = {
    "VIVT3": [("2025-04-15", 2.0, "2025-06-30")],       # desdobramento 1:2
    "HAPV3": [("2025-06-06", 1 / 15, "2025-06-30")],    # grupamento 15:1
    "COGN3": [("2025-12-26", 1.1, "2025-12-31")],       # bonificação de 10%
    "BLAU3": [("2026-01-06", 1.3, "2025-12-31")],       # bonificação de 30%
}
GAP_ALERTA = 0.25  # salto de abertura acima disso sem evento cadastrado = aviso

# Re-baixa o ITR/DFP do ano corrente (e o DFP do ano anterior, que sai em março)
# quando o cache tem mais que isso — é como um trimestre novo entra no painel.
CVM_REFRESH_DAYS = 7
# Abaixo disso o "total de ações" só pode estar em milhares (nenhuma companhia da
# cobertura tem menos de 10 milhões de ações).
MIN_PLAUSIBLE_SHARES = 10_000_000

UA = {"User-Agent": cvm.USER_AGENT}

# Campos de cada pregão guardado: [dia, abertura, fechamento, mínima, máxima,
# volume R$, quantidade]
_D_DIA, _D_ABE, _D_FECH, _D_MIN, _D_MAX, _D_VOL, _D_QTD = range(7)


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def load_market() -> Dict:
    if MARKET_FILE.exists():
        with open(MARKET_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}
    data.setdefault("pregoes", {})    # {"AAAA-MM": {ticker: [[dia, abe, fech, ...], ...]}}
    data.setdefault("acoes", {})      # {ticker: {"AAAA-MM-DD": {...}}}
    data.setdefault("baixados", {})   # {"ITR_2026": "AAAA-MM-DD"} — quando foi baixado
    data.pop("cotacoes", None)        # formato antigo (só agregados mensais)
    return data


def save_market(data: Dict) -> None:
    with open(MARKET_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)


def months_between(start: str, end: str) -> List[str]:
    """['AAAA-MM', ...] de start a end, inclusive."""
    y, m = int(start[:4]), int(start[5:7])
    out = []
    while f"{y:04d}-{m:02d}" <= end:
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


# ============================================================================
# AJUSTE POR EVENTOS SOCIETÁRIOS
# ============================================================================

def fator_base_atual(ticker: str, dia: str) -> float:
    """Multiplicador que leva uma QUANTIDADE de ações na data `dia` (AAAA-MM-DD)
    para a base atual. Preço: divida por ele. Volume em R$: não muda."""
    f = 1.0
    for ex, fator, _itr in EVENTOS_ACOES.get(ticker, []):
        if dia < ex:
            f *= fator
    return f


# ============================================================================
# COTAÇÕES (B3)
# ============================================================================

def _parse_cotahist(raw: bytes, codes: Dict[str, str]) -> Dict[str, List[List]]:
    """codes: código B3 -> ticker do monitor. Devolve {ticker: [pregão, ...]}."""
    want = {c.encode("ascii").ljust(12): t for c, t in codes.items()}
    out: Dict[str, Dict[int, List]] = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        with z.open(z.namelist()[0]) as fh:
            for line in fh:
                # layout B3: TIPREG 1-2, DATA 3-10, CODNEG 13-24, TPMERC 25-27,
                # PREABE 57-69, PREMAX 70-82, PREMIN 83-95, PREULT 109-121,
                # QUATOT 153-170, VOLTOT 171-188 (preços e volume com 2 decimais)
                if line[:2] != b"01" or line[24:27] != b"010":
                    continue
                t = want.get(line[12:24])
                if not t:
                    continue
                dia = int(line[8:10])
                row = [dia, int(line[56:69]) / 100, int(line[108:121]) / 100,
                       int(line[82:95]) / 100, int(line[69:82]) / 100,
                       int(line[170:188]) / 100, int(line[152:170])]
                # Mudança de ticker no meio do mês: um pregão por dia; se os dois
                # códigos negociaram no mesmo dia, fica o de maior volume.
                cur = out.setdefault(t, {}).get(dia)
                if cur is None or row[_D_VOL] > cur[_D_VOL]:
                    out[t][dia] = row
    return {t: [rows[d] for d in sorted(rows)] for t, rows in out.items()}


def update_cotacoes(data: Dict, meses: List[str], tickers: List[str],
                    session: requests.Session) -> None:
    """Baixa os meses FECHADOS que faltam no cache. O mês corrente nunca entra:
    o arquivo dele ainda está incompleto."""
    corrente = date.today().strftime("%Y-%m")
    codes = {t: t for t in tickers}
    for t, aliases in B3_ALIASES.items():
        if t in tickers:
            codes.update({a: t for a in aliases})
    # Códigos usados em cada mês baixado: se um alias for acrescentado depois, o
    # mês é baixado de novo (senão o cache ficaria para sempre sem o código novo).
    config = {t: sorted([t] + B3_ALIASES.get(t, [])) for t in tickers}
    usados = data.setdefault("pregoes_codigos", {})
    for mes in data["pregoes"]:
        usados.setdefault(mes, config)  # cache anterior a este controle
    for mes in meses:
        if mes >= corrente:
            continue
        cached = data["pregoes"].get(mes) or {}
        if all(t in cached for t in tickers) and all(
                usados.get(mes, {}).get(t) == config[t] for t in tickers):
            continue
        url = COTAHIST_URL.format(mm=mes[5:7], yyyy=mes[:4])
        try:
            r = session.get(url, headers=UA, timeout=180)
            r.raise_for_status()
            parsed = _parse_cotahist(r.content, codes)
        except Exception as e:
            log(f"WARNING: cotação B3 {mes} indisponível: {e}")
            continue
        faltando = sorted(set(tickers) - set(parsed))
        if faltando:
            # Sem negócio no mês inteiro = mudou de código (falta alias) ou foi
            # suspenso. Fica registrado vazio para não rebaixar o mês toda vez.
            log(f"WARNING: B3 {mes}: sem cotação para {', '.join(faltando)} "
                f"— se mudou de ticker, acrescente em B3_ALIASES")
        data["pregoes"][mes] = {**parsed, **{t: [] for t in faltando}}
        usados[mes] = config
        log(f"Cotações B3 {mes}: {len(parsed)} tickers")


def cotacao_mes(data: Dict, ticker: str, mes: str) -> Optional[Dict]:
    """Estatísticas do mês na base de ações atual: fechamento do último pregão,
    VWAP, mínima e máxima. None sem pregões."""
    rows = (data["pregoes"].get(mes) or {}).get(ticker) or []
    if not rows:
        return None
    vol = qtd = 0.0
    lo = hi = None
    fech = 0.0
    for r in rows:
        f = fator_base_atual(ticker, f"{mes}-{r[_D_DIA]:02d}")
        vol += r[_D_VOL]
        qtd += r[_D_QTD] * f
        lo = r[_D_MIN] / f if lo is None else min(lo, r[_D_MIN] / f)
        hi = r[_D_MAX] / f if hi is None else max(hi, r[_D_MAX] / f)
        fech = r[_D_FECH] / f
    return {"fech": fech, "vwap": vol / qtd if qtd else None, "min": lo, "max": hi,
            "volume": vol, "pregoes": len(rows)}


def saltos_sem_evento(data: Dict, ticker: str) -> List[Tuple[str, float]]:
    """Pregões cuja abertura difere mais que GAP_ALERTA do fechamento anterior,
    já descontados os eventos cadastrados — candidatos a evento não cadastrado."""
    out = []
    prev = None
    for mes in sorted(data["pregoes"]):
        for r in (data["pregoes"][mes] or {}).get(ticker) or []:
            dia = f"{mes}-{r[_D_DIA]:02d}"
            abe = r[_D_ABE] / fator_base_atual(ticker, dia)
            if prev and abe and abs(abe / prev - 1) > GAP_ALERTA:
                out.append((dia, abe / prev))
            prev = r[_D_FECH] / fator_base_atual(ticker, dia)
    return out


# ============================================================================
# QUANTIDADE DE AÇÕES (CVM ITR/DFP)
# ============================================================================

def _read_zip_csv(z: zipfile.ZipFile, suffix: str) -> List[Dict]:
    name = next((n for n in z.namelist() if n.endswith(suffix)), None)
    if not name:
        return []
    with z.open(name) as fh:
        return list(csv.DictReader(io.TextIOWrapper(fh, encoding="latin-1"), delimiter=";"))


def update_acoes(data: Dict, years: List[int], session: requests.Session) -> None:
    hoje = date.today()
    code_to_ticker = {cvm.normalize_cvm_code(c["cvm_code"]): c["ticker"]
                      for c in cvm.COMPANIES if c.get("cvm_code")}
    # Empresa nova na cobertura: relê todos os anos uma vez para ela ter ações.
    nova = any(t not in data["acoes"] for t in code_to_ticker.values())
    for year in years:
        for kind in ("ITR", "DFP"):
            if kind == "DFP" and year >= hoje.year:
                continue  # DFP do ano corrente só existe no ano seguinte
            key = f"{kind}_{year}"
            last = data["baixados"].get(key)
            # ITR e DFP do ano anterior continuam "vivos" no ano seguinte: em
            # janeiro o ITR de 30/09 ainda pode estar chegando, e o DFP sai em março.
            vivo = year >= hoje.year - 1
            if last and not nova and not (
                    vivo and (hoje - date.fromisoformat(last)).days > CVM_REFRESH_DAYS):
                continue
            url = CVM_DOC_URL.format(kind=kind, kind_l=kind.lower(), year=year)
            try:
                r = session.get(url, headers=UA, timeout=300)
                if r.status_code == 404:
                    continue  # ano novo, ainda sem nenhum ITR/DFP publicado
                r.raise_for_status()
                z = zipfile.ZipFile(io.BytesIO(r.content))
            except Exception as e:
                log(f"WARNING: {kind} {year} indisponível: {e}")
                continue
            cnpj = {row["CNPJ_CIA"]: code_to_ticker.get(cvm.normalize_cvm_code(row["CD_CVM"]))
                    for row in _read_zip_csv(z, f"_cia_aberta_{year}.csv")}
            n = 0
            for row in _read_zip_csv(z, f"composicao_capital_{year}.csv"):
                t = cnpj.get(row["CNPJ_CIA"])
                if not t:
                    continue
                dt, ver = row["DT_REFER"], int(row.get("VERSAO") or 1)
                prev = data["acoes"].setdefault(t, {}).get(dt)
                if prev and prev.get("versao", 0) > ver:
                    continue  # já temos uma reapresentação mais nova
                data["acoes"][t][dt] = {
                    "total": int(row["QT_ACAO_TOTAL_CAP_INTEGR"] or 0),
                    "tesouraria": int(row["QT_ACAO_TOTAL_TESOURO"] or 0),
                    "versao": ver,
                    "fonte": f"{kind} {dt}",
                }
                n += 1
            data["baixados"][key] = hoje.isoformat()
            log(f"Composição do capital {kind} {year}: {n} registros")


def acoes_em(data: Dict, ticker: str, mes: str) -> Optional[Dict]:
    """Último trimestre informado até o fim do mês `mes`, com a escala corrigida
    (milhares -> unidades) e levado à base de ações atual. None se não houver."""
    serie = data["acoes"].get(ticker) or {}
    datas = [d for d in serie if d[:7] <= mes]
    if not datas:
        return None
    d = max(datas)
    reg = dict(serie[d], data=d)
    escala = 1000 if 0 < reg["total"] < MIN_PLAUSIBLE_SHARES else 1
    f = float(escala)
    for ex, fator, itr_desde in EVENTOS_ACOES.get(ticker, []):
        if d < itr_desde:  # ITR ainda na base antiga
            f *= fator
            continue
        # Depois de um evento, há quem informe o "total" já SEM a tesouraria
        # (BLAU3: 230.984.962 = 233.212.121 da FRE − 2.227.160 em tesouraria).
        # Confere contra o último total antes do evento × fator.
        antes = [k for k in serie if k < itr_desde]
        if antes:
            esperado = serie[max(antes)]["total"] * fator
            t, tes = reg["total"], reg["tesouraria"]
            if abs(t + tes - esperado) <= max(10, esperado * 1e-6) < abs(t - esperado):
                reg["total"] = t + tes
                reg["total_sem_tesouraria"] = True
    reg["total"] = round(reg["total"] * f)
    reg["tesouraria"] = round(reg["tesouraria"] * f)
    if escala != 1:
        reg["escala"] = "milhares"
    return reg


def vwap_dia(data: Dict, ticker: str, mes: str, dia: int) -> Optional[float]:
    """VWAP do pregão (preço NÃO ajustado, mesma base da quantidade informada no
    formulário daquele dia). Sem pregão no dia: VWAP do mês. None sem cotação."""
    rows = (data["pregoes"].get(mes) or {}).get(ticker) or []
    for r in rows:
        if r[_D_DIA] == dia and r[_D_QTD]:
            return r[_D_VOL] / r[_D_QTD]
    vol, qtd = sum(r[_D_VOL] for r in rows), sum(r[_D_QTD] for r in rows)
    return vol / qtd if qtd else None


def refresh(meses: List[str], tickers: List[str], offline: bool = False) -> Dict:
    data = load_market()
    if not offline:
        session = requests.Session()
        update_cotacoes(data, meses, tickers, session)
        years = list(range(int(meses[0][:4]), date.today().year + 1))
        update_acoes(data, years, session)
        save_market(data)
    return data

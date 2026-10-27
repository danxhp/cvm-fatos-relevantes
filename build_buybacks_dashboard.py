#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Painel HTML de recompras da cobertura -> buybacks_dashboard.html.

Junta três fontes, todas oficiais e já em cache no repositório:
  - buybacks_cache.json   formulários mensais da CVM (buybacks_cvm.py): recompra
                          da companhia, negociação de controladores e
                          administradores, saldo em tesouraria;
  - buybacks_mercado.json cotação diária da B3 e quantidade de ações dos
                          ITR/DFP (buybacks_mercado.py).

O HTML é autocontido (dados embutidos, gráficos em SVG desenhados no próprio
navegador, sem biblioteca) e é regerado por `py buybacks_cvm.py` junto com a
planilha. O layout vive em buybacks_dashboard_template.html; aqui só se
calculam os números.

CONVENÇÕES DOS NÚMEROS
----------------------
- Recompra = compras de AÇÕES no mercado pela companhia/controladas
  (formulário "Posição Individual"), as mesmas regras da planilha.
- Quantidades e preços na base de ações ATUAL (grupamento/desdobramento/
  bonificação ajustados por data — ver buybacks_mercado.EVENTOS_ACOES).
- LTM = os 12 meses de referência mais recentes; "LTM anterior" = os 12 antes.
- Último mês (botão com o nome do mês no painel) = só o mês de referência mais
  recente, comparado com o mês anterior e com a média mensal do LTM.
- Mês sem formulário entregue fica sem dado (não vira zero) e o LTM avisa.
- Valor de mercado = ações ex-tesouraria x fechamento do último mês com pregão;
  ações ex-tesouraria = total do último ITR/DFP - cancelamentos posteriores -
  tesouraria do último formulário mensal (sem formulário depois do ITR:
  tesouraria do ITR e recompra líquida posterior). Não é o free float.

Uso:
    py build_buybacks_dashboard.py            # baixa só o que falta de cotação/ITR
    py build_buybacks_dashboard.py --offline  # só com o cache
    py build_buybacks_dashboard.py --artifact <arquivo>
        # grava também a versão "fragmento" (sem <html>/<head>), formato que a
        # publicação como Artifact do Claude exige
"""

from __future__ import annotations

import argparse
import calendar
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import buybacks_cvm as b
import buybacks_mercado as bm
import cvm_fatos_relevantes_claude as cvm


SCRIPT_DIR = Path(__file__).resolve().parent
OUT_HTML = SCRIPT_DIR / "buybacks_dashboard.html"
TEMPLATE = SCRIPT_DIR / "buybacks_dashboard_template.html"
PRAZO_DIAS = 10  # Res. CVM 44, art. 11: até 10 dias após o fim do mês

# Saltos de preço > 25% já conferidos: são movimento de mercado, não evento
# societário. Silenciam o aviso de buybacks_mercado.saltos_sem_evento.
SALTOS_CONFERIDOS = {
    ("HAPV3", "2025-11-13"),  # -31% após o resultado do 3T25 (ITR: total de ações igual)
    ("ODPV3", "2026-02-27"),  # +26% na véspera da incorporação pela Bradsaúde
    ("ANIM3", "2026-07-15"),  # -29%; total de ações inalterado no ITR
}

# Linhas de bonificação/desdobramento/grupamento nos formulários já conferidas
# e que NÃO são evento a ajustar. Silenciam o aviso de evento não cadastrado.
EVENTOS_FORM_CONFERIDOS = {
    ("TIMS3", "2025-07"),  # grupamento + desdobramento simultâneos (fator líquido 1: limpa frações)
}

ENTIDADE_COBERTURA = "COBERTURA"

# Setor de cada empresa da cobertura — agrupa o seletor do painel. Ticker novo
# em COMPANIES sem setor aqui aparece em "Outros".
SETOR = {
    "RDOR3": "Saúde", "HAPV3": "Saúde", "FLRY3": "Saúde", "DASA3": "Saúde",
    "ONCO3": "Saúde", "MATD3": "Saúde", "BLAU3": "Saúde", "ODPV3": "Saúde",
    "HYPE3": "Saúde", "QUAL3": "Saúde",
    "COGN3": "Educação", "YDUQ3": "Educação", "ANIM3": "Educação",
    "VIVT3": "Telecom", "TIMS3": "Telecom",
}

# Nome de exibição quando a companhia mudou de nome depois de entrar no monitor
# (o monitor e a planilha continuam usando o nome/ticker de COMPANIES).
NOME_ATUAL = {
    "ODPV3": "Bradsaúde",  # ex-Odontoprev; negocia como SAUD3 desde 05/05/2026
}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def fim_do_mes(mes: str) -> str:
    y, m = int(mes[:4]), int(mes[5:7])
    return f"{mes}-{calendar.monthrange(y, m)[1]:02d}"


def prazo_entrega(mes: str) -> date:
    return date.fromisoformat(fim_do_mes(mes)) + timedelta(days=PRAZO_DIAS)


def _r(x: Optional[float], nd: int = 0):
    """Arredonda para o JSON ficar enxuto; preserva None."""
    if x is None:
        return None
    return round(x, nd) if nd else int(round(x))


def _soma(vals: List[Optional[float]]) -> float:
    return sum(v for v in vals if v)


def _janela(arr: List, ini: int, fim: int) -> List:
    return arr[max(ini, 0):fim]


# ============================================================================
# SÉRIES POR EMPRESA
# ============================================================================

EVENTO_PALAVRAS = ("bonifica", "desdobr", "grupament")
EMISSAO_PALAVRAS = ("subscri", "homologa", "incorpora", "aumento de capital", "conversao")
CHAVES_EMPRESA = ("rec_v", "rec_q", "tv_v", "tv_q", "canc_q")
CHAVES_CTRL = ("ctrl_cv", "ctrl_vv", "ctrl_cq", "ctrl_vq")
CHAVES_ADM = ("adm_cv", "adm_vv", "adm_cq", "adm_vq")
# Parte estimada (compra/venda declarada sem preço, valorizada pelo VWAP do dia).
CHAVES_ESTIMADAS = ("rec_v_e", "rec_q_e", "tv_v_e", "ctrl_cv_e", "ctrl_vv_e", "adm_cv_e", "adm_vv_e")


def _abas_consolidado(entry: Dict) -> set:
    """Grupos que o formulário consolidado traz. O art. 11 da Res. CVM 44 não
    obriga o controlador: a VIVT3 nunca informa a Telefónica S.A. Grupo ausente
    fica sem dado, não zero."""
    return {b.aba_da_secao(b.FORM_CONSOLIDADO, sec) for sec in entry["parse"].get("secoes", [])}


def _lancou_evento(entry: Dict) -> bool:
    """O formulário já lançou bonificação/desdobramento/grupamento no mês?"""
    return any(any(k in b.fold(op["desc"]) for k in EVENTO_PALAVRAS)
               for sec in entry["parse"].get("secoes", []) if sec.get("emissor") == "Companhia"
               for op in sec["ops"])


def _fator_saldo(ticker: str, mes: str, entry: Dict) -> float:
    """Leva o saldo de fim de mês para a base atual. Há quem lance as ações novas
    no último dia "com" (COGN3: linha de bonificação no dia 23, ex em 26/12): se o
    formulário do mês já lançou o evento, o saldo já está na base nova mesmo com
    o 1º pregão ex caindo no mês seguinte."""
    fim = fim_do_mes(mes)
    f = bm.fator_base_atual(ticker, fim)
    if _lancou_evento(entry):
        limite = (date.fromisoformat(fim) + timedelta(days=20)).isoformat()
        for ex, fator, _itr in bm.EVENTOS_ACOES.get(ticker, []):
            if fim < ex <= limite:
                f /= fator
    return f


def series_empresa(ticker: str, meses: List[str], cache: Dict, latest: Dict,
                   ops: List[Dict], market: Dict, hoje: date) -> Dict:
    n = len(meses)
    idx = {m: i for i, m in enumerate(meses)}
    s = {k: [None] * n for k in (
        CHAVES_EMPRESA + CHAVES_CTRL + CHAVES_ADM + CHAVES_ESTIMADAS
        + ("tes", "vwap", "fech", "lo", "hi", "prot_i", "prot_c"))}
    status = []

    for i, mes in enumerate(meses):
        p_i = latest.get((ticker, mes, b.FORM_INDIVIDUAL))
        p_c = latest.get((ticker, mes, b.FORM_CONSOLIDADO))
        # Documento listado mas não lido (download falhou, PDF fora do padrão)
        # NÃO vira zero: fica sem dado, e a próxima execução tenta de novo.
        ok_i = bool(p_i) and b.documento_lido(cache[p_i])
        ok_c = bool(p_c) and b.documento_lido(cache[p_c])
        s["prot_i"][i], s["prot_c"][i] = p_i, p_c
        if ok_i:
            for k in CHAVES_EMPRESA + ("rec_v_e", "rec_q_e", "tv_v_e"):
                s[k][i] = 0
            tes = b.saldo_tesouraria(cache[p_i])
            if tes is not None:
                s["tes"][i] = tes * _fator_saldo(ticker, mes, cache[p_i])
        if ok_c:
            abas = _abas_consolidado(cache[p_c])
            if b.ABA_CONTROLADORES in abas:
                for k in CHAVES_CTRL + ("ctrl_cv_e", "ctrl_vv_e"):
                    s[k][i] = 0
            if b.ABA_ADMINISTRADORES in abas:
                for k in CHAVES_ADM + ("adm_cv_e", "adm_vv_e"):
                    s[k][i] = 0
        # ok | parcial (só um dos formulários) | erro (listado, não lido) |
        # aguardando (dentro do prazo) | falta
        if ok_i and ok_c:
            status.append("ok")
        elif (p_i and not ok_i) or (p_c and not ok_c):
            status.append("erro")
            log(f"WARNING: {ticker} {mes}: formulário listado mas não lido "
                f"({p_i if p_i and not ok_i else p_c}) — fica sem dado; a próxima execução tenta de novo")
        elif ok_i or ok_c:
            status.append("parcial")
        else:
            status.append("aguardando" if hoje <= prazo_entrega(mes) else "falta")
        cot = bm.cotacao_mes(market, ticker, mes)
        if cot:
            s["vwap"][i], s["fech"][i] = cot["vwap"], cot["fech"]
            s["lo"][i], s["hi"][i] = cot["min"], cot["max"]

    for r in ops:
        m = r["meta"]
        if m["ticker"] != ticker or r["vm"] != "Ações":
            continue
        i = idx.get(m["ref"])
        if i is None:
            continue
        dia = min(max(int(r["dia"] or 1), 1), int(fim_do_mes(m["ref"])[-2:]))
        f = bm.fator_base_atual(ticker, f"{m['ref']}-{dia:02d}")
        if (r["op"] == b.OP_OUTROS and r["aba"] == b.ABA_EMPRESA
                and "cancelamento" in b.fold(r["desc"]) and s["canc_q"][i] is not None):
            s["canc_q"][i] += abs(r["qtd"]) * f
            continue
        if r["op"] not in (b.OP_COMPRA, b.OP_VENDA, b.OP_COMPRA_SP, b.OP_VENDA_SP):
            continue
        compra = r["op"] in (b.OP_COMPRA, b.OP_COMPRA_SP)
        estimado = r["op"] in (b.OP_COMPRA_SP, b.OP_VENDA_SP)
        q = r["qtd"] * f
        if estimado:
            # Declarado sem preço: vale pelo VWAP do pregão (preço não ajustado,
            # mesma base da quantidade do formulário). Sem cotação, fica só a qtd.
            v = r["qtd"] * (bm.vwap_dia(market, ticker, m["ref"], dia) or 0)
        else:
            v = r["volume"]
        if r["aba"] == b.ABA_EMPRESA:
            kv, kq = ("rec_v", "rec_q") if compra else ("tv_v", "tv_q")
        elif r["aba"] == b.ABA_CONTROLADORES:
            kv, kq = ("ctrl_cv", "ctrl_cq") if compra else ("ctrl_vv", "ctrl_vq")
        elif r["aba"] == b.ABA_ADMINISTRADORES:
            kv, kq = ("adm_cv", "adm_cq") if compra else ("adm_vv", "adm_vq")
        else:
            continue
        s[kv][i] = (s[kv][i] or 0) + v
        s[kq][i] = (s[kq][i] or 0) + q
        if estimado:
            s[kv + "_e"][i] = (s[kv + "_e"][i] or 0) + v
            if kv == "rec_v":
                s["rec_q_e"][i] = (s["rec_q_e"][i] or 0) + q

    # Preço médio só com o que tem preço informado.
    s["rec_px"] = [((v or 0) - (ve or 0)) / ((q or 0) - (qe or 0))
                   if (q or 0) - (qe or 0) > 0 and (v or 0) - (ve or 0) > 0 else None
                   for v, q, ve, qe in zip(s["rec_v"], s["rec_q"], s["rec_v_e"], s["rec_q_e"])]
    s["st"] = status
    return s


def rolling_ltm(vals: List[Optional[float]], st: List[str]) -> Tuple[List, List]:
    """Soma móvel de 12 meses e, por ponto, quantos meses da janela estão sem
    dado (para o gráfico avisar quando o LTM é parcial)."""
    out, faltas = [], []
    for i in range(len(vals)):
        if i < 11:
            out.append(None)
            faltas.append(None)
            continue
        jan = vals[i - 11:i + 1]
        out.append(_soma(jan))
        faltas.append(sum(1 for v in jan if v is None))
    return out, faltas


# ============================================================================
# INDICADORES (KPIs)
# ============================================================================

def kpis_empresa(ticker: str, s: Dict, meses: List[str], market: Dict) -> Dict:
    n = len(meses)
    ltm = slice(max(n - 12, 0), n)
    prev = slice(max(n - 24, 0), max(n - 12, 0))
    k: Dict = {}
    k["ltm_v"] = _soma(s["rec_v"][ltm])
    k["ltm_q"] = _soma(s["rec_q"][ltm])
    k["ltm_v_est"] = _soma(s["rec_v_e"][ltm])
    com_preco_q = k["ltm_q"] - _soma(s["rec_q_e"][ltm])
    k["ltm_px"] = (k["ltm_v"] - k["ltm_v_est"]) / com_preco_q if com_preco_q > 0 else None
    k["ltm_faltas"] = [meses[i] for i in range(n)[ltm] if s["rec_v"][i] is None]
    k["prev_v"] = _soma(s["rec_v"][prev]) if n >= 24 else None
    k["prev_faltas"] = [meses[i] for i in range(n)[prev] if s["rec_v"][i] is None]
    # Grupos que o formulário nunca traz na janela (VIVT3: controlador).
    k["sem_ctrl"] = all(v is None for v in s["ctrl_cv"][ltm])
    k["sem_adm"] = all(v is None for v in s["adm_cv"][ltm])

    # Último fechamento disponível (o mês mais recente com pregão).
    fech_i = next((i for i in range(n - 1, -1, -1) if s["fech"][i]), None)
    k["fech"] = s["fech"][fech_i] if fech_i is not None else None
    k["fech_mes"] = meses[fech_i] if fech_i is not None else None

    tes_i = next((i for i in range(n - 1, -1, -1) if s["tes"][i] is not None), None)
    acoes = bm.acoes_em(market, ticker, meses[-1])
    if acoes:
        mes_itr = acoes["data"][:7]
        depois = [i for i, m in enumerate(meses) if m > mes_itr]
        # Total emitido: o do ITR menos os cancelamentos lançados depois dele.
        total = acoes["total"] - sum(s["canc_q"][i] or 0 for i in depois)
        if tes_i is not None and meses[tes_i] >= mes_itr:
            # Ações ex-tesouraria = total − tesouraria do último formulário mensal
            # (pega recompra, entrega a planos, doação etc. feitas depois do ITR).
            circ = total - s["tes"][tes_i]
        else:
            liquida = sum((s["rec_q"][i] or 0) - (s["tv_q"][i] or 0) for i in depois)
            circ = acoes["total"] - acoes["tesouraria"] - liquida
        k["acoes_total"] = total
        k["acoes_circ"] = circ
        k["acoes_data"] = acoes["data"]
        k["acoes_fonte"] = acoes["fonte"] + (" (em milhares no ITR)" if acoes.get("escala") else "") + (
            " (total informado sem a tesouraria; somada de volta)" if acoes.get("total_sem_tesouraria") else "")
        k["ltm_pct_circ"] = k["ltm_q"] / circ if circ else None
        if k["fech"]:
            k["mcap"] = circ * k["fech"]
            k["yield"] = k["ltm_v"] / k["mcap"] if k["mcap"] else None

    if tes_i is not None:
        k["tes"] = s["tes"][tes_i]
        k["tes_mes"] = meses[tes_i]
        if k.get("acoes_total"):
            k["tes_pct"] = k["tes"] / k["acoes_total"]

    # Último mês (modo "mês" do painel): os mesmos números só do mês de referência
    # mais recente. Sem formulário no mês -> None, não zero.
    u = n - 1
    k["mes_v"] = s["rec_v"][u]
    k["mes_q"] = s["rec_q"][u]
    k["mes_v_est"] = s["rec_v_e"][u]
    k["mes_px"] = s["rec_px"][u]
    k["mes_vwap"] = s["vwap"][u]
    k["mes_ant_v"] = s["rec_v"][u - 1] if n > 1 else None
    # Média mensal do LTM só com os meses que têm formulário.
    com_dado = [v for v in s["rec_v"][ltm] if v is not None]
    k["mes_media"] = sum(com_dado) / len(com_dado) if com_dado else None
    k["mes_media_n"] = len(com_dado)
    k["mes_pct_circ"] = (k["mes_q"] / k["acoes_circ"]
                         if k["mes_q"] is not None and k.get("acoes_circ") else None)
    k["mes_yield"] = k["mes_v"] / k["mcap"] if k["mes_v"] is not None and k.get("mcap") else None
    k["mes_tes"] = s["tes"][u]
    # Saldos inteiros (como na série do JSON): a fração de uma bonificação não vira variação.
    k["mes_tes_var"] = (round(s["tes"][u]) - round(s["tes"][u - 1])
                        if n > 1 and s["tes"][u] is not None and s["tes"][u - 1] is not None else None)
    # Cancelamento no mês: explica a queda da tesouraria (não é venda nem entrega).
    k["mes_canc_q"] = s["canc_q"][u]
    return k


def kpis_cobertura(empresas: List[Dict], meses: List[str]) -> Dict:
    k = {
        "ltm_v": sum(e["kpi"]["ltm_v"] for e in empresas),
        "prev_v": (sum(e["kpi"]["prev_v"] or 0 for e in empresas)
                   if all(e["kpi"]["prev_v"] is not None for e in empresas) else None),
        "ltm_faltas": sorted({m for e in empresas for m in e["kpi"]["ltm_faltas"]}),
        "prev_faltas": sorted({m for e in empresas for m in e["kpi"]["prev_faltas"]}),
        # Para o aviso nomear cada empresa com os seus meses (não só a do último mês).
        "faltas_por_empresa": {e["id"]: e["kpi"]["ltm_faltas"]
                               for e in empresas if e["kpi"]["ltm_faltas"]},
        "ltm_v_est": sum(e["kpi"]["ltm_v_est"] for e in empresas),
        "n_recompraram": sum(1 for e in empresas if e["kpi"]["ltm_v"] > 0),
        "n_empresas": len(empresas),
    }
    com_mcap = [e for e in empresas if e["kpi"].get("mcap")]
    mcap = sum(e["kpi"]["mcap"] for e in com_mcap)
    k["mcap"] = mcap
    k["yield"] = sum(e["kpi"]["ltm_v"] for e in com_mcap) / mcap if mcap else None
    lider = max(empresas, key=lambda e: e["kpi"]["ltm_v"])
    k["lider"] = lider["id"] if lider["kpi"]["ltm_v"] > 0 else None
    k["lider_pct"] = lider["kpi"]["ltm_v"] / k["ltm_v"] if k["ltm_v"] else None

    # Último mês: soma das empresas com formulário no mês; as sem formulário ficam
    # fora da soma, das comparações (mês anterior e média) e do valor de mercado da
    # razão, e são nomeadas. Comparação com o mesmo conjunto de empresas.
    n = len(meses)
    com_mes = [e for e in empresas if e["kpi"]["mes_v"] is not None]
    k["mes_v"] = sum(e["kpi"]["mes_v"] for e in com_mes)
    k["mes_v_est"] = sum(e["kpi"]["mes_v_est"] or 0 for e in com_mes)
    k["mes_ant_v"] = sum(e["s"]["rec_v"][n - 2] or 0 for e in com_mes) if n > 1 else None
    # mês anterior sem formulário entra como zero na comparação; o painel avisa
    k["mes_ant_faltas"] = [e["id"] for e in com_mes if n > 1 and e["s"]["rec_v"][n - 2] is None]
    k["mes_media"] = sum(e["kpi"]["ltm_v"] for e in com_mes) / min(n, 12)
    k["mes_media_n"] = min(n, 12)
    k["mes_n_com_form"] = len(com_mes)
    k["mes_faltas"] = [e["id"] for e in empresas if e["kpi"]["mes_v"] is None]
    k["mes_n_recompraram"] = sum(1 for e in com_mes if e["kpi"]["mes_v"] > 0)
    mcap_mes = [e for e in com_mcap if e["kpi"]["mes_v"] is not None]
    k["mes_mcap"] = sum(e["kpi"]["mcap"] for e in mcap_mes)
    k["mes_yield"] = (sum(e["kpi"]["mes_v"] for e in mcap_mes) / k["mes_mcap"]
                      if k["mes_mcap"] else None)
    lider_mes = max(empresas, key=lambda e: e["kpi"]["mes_v"] or 0)
    k["mes_lider"] = lider_mes["id"] if (lider_mes["kpi"]["mes_v"] or 0) > 0 else None
    k["mes_lider_pct"] = lider_mes["kpi"]["mes_v"] / k["mes_v"] if k["mes_lider"] else None
    return k


# ============================================================================
# MONTAGEM
# ============================================================================

def ultimo_mes(latest: Dict, tickers: List[str], hoje: date) -> str:
    """Último mês de referência do painel: o mais recente cujo prazo de entrega
    já passou, ou em que pelo menos 80% das empresas já entregaram. Sem isso, a
    primeira empresa que entregar outubro em 03/11 empurraria a janela de 12
    meses de TODAS para outubro, tirando um mês inteiro da soma das outras."""
    meses = sorted({k[1] for k in latest})
    def entregues(m: str) -> int:
        return sum(1 for t in tickers if (t, m, b.FORM_INDIVIDUAL) in latest)
    ok = [m for m in meses if hoje > prazo_entrega(m) or entregues(m) >= 0.8 * len(tickers)]
    return max(ok) if ok else meses[-1]


def _avisos_empresa(t: str, s: Dict, k: Dict, meses: List[str], ops: List[Dict],
                    market: Dict) -> None:
    """Conferências que não mudam número nenhum, só avisam no fim da execução."""
    acoes = bm.acoes_em(market, t, meses[-1])
    if acoes:
        mes_itr = acoes["data"][:7]
        # Escala do ITR: tesouraria do ITR x formulário do mesmo mês.
        if mes_itr in meses:
            tes_form = s["tes"][meses.index(mes_itr)]
            if tes_form and acoes["tesouraria"] and not (0.5 < tes_form / acoes["tesouraria"] < 2):
                log(f"WARNING: {t}: tesouraria no {acoes['fonte']} ({acoes['tesouraria']:,}) "
                    f"x formulário ({tes_form:,.0f}) — escala do ITR suspeita")
        # Emissão de ações depois do ITR (aumento de capital, incorporação): o
        # total do ITR fica velho até o próximo trimestre.
        emissao = sorted({r["meta"]["ref"] for r in ops
                          if r["meta"]["ticker"] == t and r["meta"]["ref"] > mes_itr
                          and r["vm"] == "Ações"
                          and any(w in b.fold(r["desc"]) for w in EMISSAO_PALAVRAS)})
        if emissao:
            k["acoes_desatualizadas"] = emissao
            log(f"WARNING: {t}: emissão de ações em {', '.join(emissao)}, depois do "
                f"{acoes['fonte']} — valor de mercado usa o total antigo até o próximo ITR")
    for dia, ratio in bm.saltos_sem_evento(market, t):
        if (t, dia) not in SALTOS_CONFERIDOS:
            log(f"WARNING: {t} {dia}: abertura {ratio:.3f}x o fechamento anterior — "
                "evento societário não cadastrado? (buybacks_mercado.EVENTOS_ACOES)")
    # Linha de bonificação/desdobramento/grupamento sem evento cadastrado perto:
    # pega bônus pequenos (5–20%), que não dão salto de 25% no preço. O leilão
    # das frações sai até ~3 meses depois do evento (COGN3: fev–mar/2026).
    evs = [date.fromisoformat(ex) for ex, _f, _i in bm.EVENTOS_ACOES.get(t, [])]
    vistos = set()
    for r in ops:
        m = r["meta"]
        if m["ticker"] != t or not any(w in b.fold(r["desc"]) for w in EVENTO_PALAVRAS):
            continue
        if (t, m["ref"]) in EVENTOS_FORM_CONFERIDOS:
            continue
        dia = min(max(int(r["dia"] or 1), 1), int(fim_do_mes(m["ref"])[-2:]))
        d = date.fromisoformat(f"{m['ref']}-{dia:02d}")
        if not any(-20 <= (d - ex).days <= 120 for ex in evs) and m["ref"] not in vistos:
            vistos.add(m["ref"])
            log(f"WARNING: {t} {m['ref']}: formulário {m['protocolo']} lança "
                f"'{r['desc'][:60]}' sem evento em buybacks_mercado.EVENTOS_ACOES")
    # Preço informado fora da faixa do mês na B3 (±2%): erro de digitação no
    # formulário (ODPV3 11/2024: R$ 1,10 com a ação a R$ 10,7) ou negócio fora de
    # bolsa. O número fica como foi entregue; o aviso manda conferir o PDF.
    for r in ops:
        m = r["meta"]
        if (m["ticker"] != t or r["vm"] != "Ações" or r["op"] not in (b.OP_COMPRA, b.OP_VENDA)
                or r["aba"] not in (b.ABA_EMPRESA, b.ABA_CONTROLADORES, b.ABA_ADMINISTRADORES)
                or "termo" in b.fold(r["desc"])):  # preço a termo embute o custo do termo
            continue
        rows = (market["pregoes"].get(m["ref"]) or {}).get(t) or []
        if not rows:
            continue
        lo, hi = min(x[3] for x in rows), max(x[4] for x in rows)
        if not (lo * 0.98 <= r["preco"] <= hi * 1.02):
            log(f"WARNING: {t} {m['ref']} dia {r['dia']}: {r['aba'].lower()} {r['op'].lower()} "
                f"a R$ {r['preco']:.2f}, fora da faixa da B3 no mês (R$ {lo:.2f}–{hi:.2f}) — "
                f"conferir protocolo {m['protocolo']}")
    if k.get("fech_mes") and k["fech_mes"] != meses[-1]:
        log(f"WARNING: {t}: sem cotação em {meses[-1]}; valor de mercado usa o fechamento de "
            f"{k['fech_mes']}")


def compute(cache: Dict, market: Dict, hoje: date) -> Dict:
    latest = b.vigentes(cache)
    ops, _ = b.build_rows(cache)
    tickers = [c["ticker"] for c in cvm.COMPANIES if c.get("cvm_code")]
    ultimo = ultimo_mes(latest, tickers, hoje)
    meses = bm.months_between(b.START_REF, ultimo)
    n = len(meses)

    empresas = []
    for c in cvm.COMPANIES:
        if not c.get("cvm_code"):
            continue
        t = c["ticker"]
        s = series_empresa(t, meses, cache, latest, ops, market, hoje)
        k = kpis_empresa(t, s, meses, market)
        _avisos_empresa(t, s, k, meses, ops, market)
        s["roll"], s["roll_faltas"] = rolling_ltm(s["rec_v"], s["st"])
        # Alias = código novo (ODPV3 -> SAUD3); o painel mostra o código de hoje.
        b3_atual = (bm.B3_ALIASES.get(t) or [t])[-1]
        empresas.append({"id": t, "nome": NOME_ATUAL.get(t, c["name"]), "b3": b3_atual,
                         "setor": SETOR.get(t, "Outros"), "kpi": k, "s": s})

    # Cobertura: soma das empresas. Mês sem dado em alguma empresa continua
    # somando as demais, mas a contagem de faltas vai junto.
    agg = {"st": []}
    for key in ("rec_v", "tv_v", "ctrl_cv", "ctrl_vv", "adm_cv", "adm_vv",
                "rec_v_e", "ctrl_cv_e", "ctrl_vv_e", "adm_cv_e", "adm_vv_e"):
        agg[key] = [_soma([e["s"][key][i] for e in empresas]) for i in range(n)]
    agg["faltas"] = [sum(1 for e in empresas if e["s"]["rec_v"][i] is None) for i in range(n)]
    agg["st"] = ["ok" if f == 0 else "parcial" for f in agg["faltas"]]
    agg["roll"], _ = rolling_ltm(agg["rec_v"], agg["st"])
    agg["roll_faltas"] = [
        None if i < 11 else sum(agg["faltas"][i - 11:i + 1]) for i in range(n)]
    cobertura = {"id": ENTIDADE_COBERTURA, "nome": "Cobertura", "b3": None,
                 "kpi": kpis_cobertura(empresas, meses), "s": agg}

    # Arredonda para o JSON: R$ inteiros, quantidades inteiras, preços com 4 casas.
    # Séries de estimativa só vão quando têm algum valor (quase sempre vazias).
    for e in [cobertura] + empresas:
        e["s"].pop("canc_q", None)
        for key in CHAVES_ESTIMADAS:
            if key in e["s"] and not any(e["s"][key]):
                del e["s"][key]
        for key, arr in e["s"].items():
            if key in ("st", "prot_i", "prot_c", "roll_faltas", "faltas"):
                continue
            nd = 4 if key in ("vwap", "fech", "rec_px", "lo", "hi") else 0
            e["s"][key] = [_r(v, nd) for v in arr]

    pendentes = [e["b3"] or e["id"] for e in empresas
                 if e["s"]["st"][-1] in ("aguardando", "falta", "parcial", "erro")]
    return {
        "meta": {
            "gerado": datetime.now().strftime("%d/%m/%Y %H:%M"),
            "hoje": hoje.isoformat(),
            "inicio": meses[0],
            "ultimo": ultimo,
            "prazo_ultimo": prazo_entrega(ultimo).isoformat(),
            "pendentes_ultimo": pendentes,
            "eventos": {t: ev for t, ev in bm.EVENTOS_ACOES.items()},
            "aliases": bm.B3_ALIASES,
            "viewer": cvm.VIEWER_URL,
        },
        "meses": meses,
        "entidades": [cobertura] + empresas,
    }


def render(data: Dict, fragment: bool) -> str:
    tpl = TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # "</" dentro de <script> fecharia a tag; JSON não precisa da barra literal.
    payload = payload.replace("</", "<\\/")
    page = tpl.replace("/*__DADOS__*/null", payload)
    if fragment:
        return page
    # Fora do Artifact, o mesmo conteúdo num documento completo: <title>, fontes
    # e <style> (tudo antes do primeiro </style>) vão para o <head>, com o mesmo
    # reset que a publicação aplica.
    head, sep, body = page.partition("</style>")
    return ("<!doctype html>\n<html lang=\"pt-BR\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
            "<style>html{color-scheme:light}body{margin:0;font:14px/1.5 system-ui,sans-serif}"
            "img{max-width:100%}[hidden]{display:none!important}</style>\n"
            + head + sep + "\n</head>\n<body>\n" + body + "\n</body>\n</html>\n")


def build(offline: bool = False, artifact: Optional[Path] = None) -> Path:
    cache = b.load_cache()
    latest = b.vigentes(cache)
    ultimo = max(k[1] for k in latest)
    tickers = [c["ticker"] for c in cvm.COMPANIES if c.get("cvm_code")]
    market = bm.refresh(bm.months_between(b.START_REF, ultimo), tickers, offline=offline)
    data = compute(cache, market, date.today())
    OUT_HTML.write_text(render(data, fragment=False), encoding="utf-8")
    if artifact:
        Path(artifact).write_text(render(data, fragment=True), encoding="utf-8")
    cob = data["entidades"][0]["kpi"]
    log(f"Painel gravado: {OUT_HTML.name} ({data['meses'][0]} a {data['meses'][-1]}; "
        f"recompra LTM da cobertura R$ {cob['ltm_v'] / 1e6:,.1f} mi; "
        f"em {data['meses'][-1]} R$ {cob['mes_v'] / 1e6:,.1f} mi)")
    return OUT_HTML


def main() -> int:
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="não baixa nada; usa só o cache")
    ap.add_argument("--artifact", type=Path, help="grava também o fragmento para Artifact")
    args = ap.parse_args()
    build(offline=args.offline, artifact=args.artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
CVM RAD + SEC EDGAR monitor for listed Brazilian companies.
- CVM: Fatos Relevantes for B3-listed issuers
- SEC EDGAR: 6-K / 20-F for foreign-listed issuers (e.g. Afya on NASDAQ)
- Deduplicates by protocol / accession
- Silent if nothing new
- Optional email alerts (configure email_config.json)

Recommended use:
    python cvm_fatos_relevantes_claude.py            # silent run, for cron
    python cvm_fatos_relevantes_claude.py --once     # manual run with status line
    python cvm_fatos_relevantes_claude.py --bootstrap  # mark all current as seen

Then schedule it every 15 minutes with:
- Windows Task Scheduler
- cron
- systemd timer
"""

from __future__ import annotations

import csv
import html
import io
import json
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import anthropic
import pypdf
import requests
from bs4 import BeautifulSoup


# Ensure UTF-8 output on Windows consoles (cp1252 default would mangle accents).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


# ============================================================================
# CONFIG
# ============================================================================

BASE_URL = "https://www.rad.cvm.gov.br/ENET/"
LIST_URL = BASE_URL + "frmConsultaExternaCVM.aspx/ListarDocumentos"
DOWNLOAD_URL = BASE_URL + "frmDownloadDocumento.aspx"
CONSULTA_URL = BASE_URL + "frmConsultaExternaCVM.aspx"

# Categorias monitoradas na CVM (IPE).
# IPE_4 = Fato Relevante, IPE_3 = Aviso aos Acionistas.
CVM_CATEGORIES = [
    ("IPE_4_-1_-1", "Fato Relevante"),
    ("IPE_3_-1_-1", "Aviso aos Acionistas"),
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Change this if you want another folder
SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE = SCRIPT_DIR / "seen_protocols.json"
LOG_FILE = SCRIPT_DIR / "monitor_log.txt"
SEND_LOG_FILE = SCRIPT_DIR / "send_log.txt"
SEND_LOG_HEADER = "timestamp | canal | status | ticker | protocolo | categoria | assunto | detalhe"
ARCHIVE_FILE = SCRIPT_DIR / "fatos_arquivo.csv"
ARCHIVE_HEADER = ["data", "ticker", "empresa", "categoria", "assunto", "protocolo", "link"]
EMAIL_CONFIG_FILE = SCRIPT_DIR / "email_config.json"

# SEC EDGAR — for foreign-listed issuers like Afya (NASDAQ)
SEC_BASE = "https://data.sec.gov"
SEC_USER_AGENT = "CVM Fatos Relevantes Monitor (contact: danxhp@gmail.com)"
# Forms equivalent to "fato relevante" for foreign private issuers / domestic issuers.
SEC_FORMS_OF_INTEREST = {"6-K", "6-K/A", "20-F", "20-F/A", "8-K", "8-K/A"}
SEC_LOOKBACK_DAYS = 14

# AFYA trades on NASDAQ (not on B3/CVM); we monitor it via SEC EDGAR.
COMPANIES = [
    {"ticker": "RDOR3", "name": "Rede D'Or", "cvm_code": "24821"},
    {"ticker": "HAPV3", "name": "Hapvida", "cvm_code": "24392"},
    {"ticker": "FLRY3", "name": "Fleury", "cvm_code": "21881"},
    {"ticker": "DASA3", "name": "Dasa", "cvm_code": "19623"},
    {"ticker": "ONCO3", "name": "Oncoclínicas", "cvm_code": "26123"},
    {"ticker": "MATD3", "name": "Mater Dei", "cvm_code": "25690"},
    {"ticker": "BLAU3", "name": "Blau", "cvm_code": "24627"},
    {"ticker": "ODPV3", "name": "Odontoprev", "cvm_code": "20125"},
    {"ticker": "HYPE3", "name": "Hypera", "cvm_code": "21431"},
    {"ticker": "QUAL3", "name": "Qualicorp", "cvm_code": "22497"},
    {"ticker": "COGN3", "name": "Cogna", "cvm_code": "17973"},
    {"ticker": "YDUQ3", "name": "Yduqs", "cvm_code": "21016"},
    {"ticker": "ANIM3", "name": "Ânima", "cvm_code": "23248"},
    {"ticker": "AFYA", "name": "Afya", "cvm_code": None, "sec_cik": "0001771007"},
    {"ticker": "VIVT3", "name": "Telefônica Brasil / Vivo", "cvm_code": "17671"},
    {"ticker": "TIMS3", "name": "TIM", "cvm_code": "24290"},
]

def normalize_cvm_code(raw_code: Optional[str]) -> str:
    """
    Normalize a CVM emitter code to a canonical form for comparison.
    The RAD/CVM API returns codes like '02612-3' (5-digit + check digit).
    Configs may use '26123' or '02612-3'. We compare by digits-only, no leading zeros.
    """
    if not raw_code:
        return ""
    digits = re.sub(r"\D", "", raw_code)
    return digits.lstrip("0") or "0"


COMPANY_CODES: Dict[str, Dict[str, Optional[str]]] = {
    normalize_cvm_code(c["cvm_code"]): c for c in COMPANIES if c["cvm_code"]
}


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class Filing:
    cvm_code: str
    company_name: str
    ticker: str
    protocol: str
    sequence: str
    version: str
    desc_type: str
    category: str
    doc_type: str
    subject: str
    filing_time: str
    download_params: Dict[str, str]

    @property
    def unique_key(self) -> str:
        return self.protocol

    @property
    def identifier(self) -> str:
        return f"{self.ticker} | protocolo {self.protocol}"


# ============================================================================
# UTILS
# ============================================================================

def log(msg: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _sanitize_log_field(value) -> str:
    """Remove | e quebras de linha para não corromper o formato pipe-separated."""
    return re.sub(r"[|\r\n]+", " ", str(value if value is not None else "")).strip()


def log_send(channel: str, filing: "Filing", ok: bool, detail: str = "") -> None:
    """
    Registra uma tentativa de envio no send_log.txt — durável (commitado de volta
    pelo Actions) e separado por ' | '. Uma linha por canal (email/telegram) por filing.
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = " | ".join([
        stamp,
        channel,
        "OK" if ok else "FAIL",
        _sanitize_log_field(filing.ticker),
        _sanitize_log_field(filing.protocol),
        _sanitize_log_field(filing.category),
        _sanitize_log_field(filing.subject or filing.doc_type),
        _sanitize_log_field(detail),
    ])
    try:
        new_file = (not SEND_LOG_FILE.exists()) or SEND_LOG_FILE.stat().st_size == 0
        with open(SEND_LOG_FILE, "a", encoding="utf-8") as f:
            if new_file:
                f.write(SEND_LOG_HEADER + "\n")
            f.write(row + "\n")
    except Exception as e:
        log(f"WARNING: falha ao gravar send_log: {e}")


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def load_seen_protocols() -> Set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(str(x) for x in data)
        return set()
    except Exception as e:
        log(f"WARNING: failed to load state file: {e}")
        return set()


def save_seen_protocols(protocols: Set[str]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(protocols), f, ensure_ascii=False, indent=2)


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
            "Referer": CONSULTA_URL,
        }
    )
    try:
        s.get(CONSULTA_URL, timeout=20)
    except Exception:
        pass
    return s


# ============================================================================
# RAD/CVM FETCH
# ============================================================================

def fetch_raw_documents(session: requests.Session, categoria: str) -> str:
    """
    Queries all rows of a given IPE category in 'this week' and filters locally
    by CVM code. Same robust workaround used previously.
    """
    payload = {
        "dataDe": "",
        "dataAte": "",
        "empresa": "",
        "setorAtividade": "-1",
        "categoriaEmissor": "-1",
        "situacaoEmissor": "A",
        "tipoParticipante": "1",
        "dataReferencia": "",
        "categoria": categoria,
        "periodo": "1",  # this week
        "horaIni": "",
        "horaFim": "",
        "palavraChave": "",
        "ultimaDtRef": "false",
        "tipoEmpresa": "2",  # CVM
        "token": "",
        "versaoCaptcha": "V3",
    }

    resp = session.post(
        LIST_URL,
        json=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=40,
    )
    resp.raise_for_status()

    data = resp.json()
    d = data.get("d", {})

    if isinstance(d, dict):
        if d.get("temErro"):
            raise RuntimeError(f"RAD/CVM API error: {d.get('msgErro', 'unknown')}")
        if d.get("expirouSessao"):
            raise RuntimeError("RAD/CVM session expired.")
        return d.get("dados", "") or ""

    if isinstance(d, str):
        return d

    raise RuntimeError(f"Unexpected API payload type: {type(d)}")


# ============================================================================
# PARSE
# ============================================================================

def parse_rows(raw: str) -> List[Filing]:
    """
    API row format:
      rows split by &*
      cols split by $&

    Observed order:
      0 CódigoCVM
      1 Empresa
      2 Categoria
      3 Tipo
      4 Espécie
      5 DataRef
      6 DataEntrega
      7 Status
      8 V
      9 Modalidade
      10 Ações (contains OpenDownloadDocumentos(...))
      11 Assunto
    """
    filings: List[Filing] = []

    if not raw.strip():
        return filings

    for row in raw.split("&*"):
        if not row.strip():
            continue

        cols = row.split("$&")
        if len(cols) < 11:
            continue

        cvm_code = normalize_cvm_code(strip_html(cols[0]))
        if cvm_code not in COMPANY_CODES:
            continue

        company_cfg = COMPANY_CODES[cvm_code]
        company_name = strip_html(cols[1])
        category = strip_html(cols[2])
        doc_type = strip_html(cols[3])
        subject = strip_html(cols[11]) if len(cols) > 11 else ""

        raw_date = strip_html(cols[6])
        raw_date = re.sub(r"^\d{8}\s*", "", raw_date).strip()

        m = re.search(
            r"OpenDownloadDocumentos\(\s*'(\d+)'\s*,\s*'(\d+)'\s*,\s*'(\d+)'\s*,\s*'([^']+)'\s*\)",
            cols[10],
        )
        if not m:
            continue

        sequence, version, protocol, desc_type = m.groups()

        filing = Filing(
            cvm_code=cvm_code,
            company_name=company_name or company_cfg["name"],
            ticker=company_cfg["ticker"],
            protocol=protocol,
            sequence=sequence,
            version=version,
            desc_type=desc_type,
            category=category,
            doc_type=doc_type,
            subject=subject,
            filing_time=raw_date,
            download_params={
                "Tela": "ext",
                "numSequencia": sequence,
                "numVersao": version,
                "numProtocolo": protocol,
                "descTipo": desc_type,
                "CodigoInstituicao": "1",
            },
        )
        filings.append(filing)

    return filings


# ============================================================================
# SEC EDGAR FETCH (for non-CVM issuers like Afya)
# ============================================================================

def fetch_sec_filings(
    session: requests.Session, company_cfg: Dict[str, Optional[str]]
) -> List[Filing]:
    cik = company_cfg.get("sec_cik")
    if not cik:
        return []

    cik_padded = str(cik).zfill(10)
    url = f"{SEC_BASE}/submissions/CIK{cik_padded}.json"
    try:
        r = session.get(
            url,
            headers={"User-Agent": SEC_USER_AGENT, "Accept": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log(f"WARNING: SEC fetch failed for {company_cfg.get('ticker')}: {e}")
        return []

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accs = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])
    accept_times = recent.get("acceptanceDateTime", [])
    docs = recent.get("primaryDocument", [])
    descs = recent.get("primaryDocDescription", [""] * len(forms))

    cik_int = str(int(cik))
    cutoff = (datetime.now() - timedelta(days=SEC_LOOKBACK_DAYS)).date().isoformat()

    out: List[Filing] = []
    for i, form in enumerate(forms):
        if form not in SEC_FORMS_OF_INTEREST:
            continue
        if i < len(dates) and dates[i] < cutoff:
            continue
        acc = accs[i]
        doc = (docs[i] or "") if i < len(docs) else ""
        desc = descs[i] if i < len(descs) else ""
        accept = accept_times[i] if i < len(accept_times) else ""
        acc_nodash = acc.replace("-", "")
        primary_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{doc}"
        out.append(
            Filing(
                cvm_code="",
                company_name=company_cfg.get("name") or "",
                ticker=company_cfg.get("ticker") or "",
                protocol=acc,
                sequence="",
                version="",
                desc_type=form,
                category=f"SEC {form}",
                doc_type=desc or form,
                subject=desc or form,
                filing_time=accept or (dates[i] if i < len(dates) else ""),
                download_params={"_sec_url": primary_url},
            )
        )
    return out


# ============================================================================
# OPTIONAL DOWNLOAD / URL BUILD
# ============================================================================

def build_download_url(filing: Filing) -> str:
    if "_sec_url" in filing.download_params:
        return filing.download_params["_sec_url"]
    from urllib.parse import urlencode
    return DOWNLOAD_URL + "?" + urlencode(filing.download_params)


# ============================================================================
# MONITOR LOGIC
# ============================================================================

def gather_all_filings(session: requests.Session) -> Tuple[List[Filing], bool]:
    filings: List[Filing] = []
    cvm_ok = False  # True se ao menos uma categoria da CVM respondeu sem erro

    # CVM (B3-listed issuers) — Fato Relevante + Aviso aos Acionistas
    for cat_code, cat_label in CVM_CATEGORIES:
        try:
            raw = fetch_raw_documents(session, cat_code)
            filings.extend(parse_rows(raw))
            cvm_ok = True
        except Exception as e:
            log(f"WARNING: CVM fetch failed para {cat_label}: {e}")

    # SEC EDGAR (Afya and any other foreign-listed)
    for company in COMPANIES:
        if company.get("sec_cik"):
            filings.extend(fetch_sec_filings(session, company))

    return filings, cvm_ok


def check_once(
    session: requests.Session, bootstrap: bool = False
) -> Tuple[List[Filing], int, bool]:
    filings, cvm_ok = gather_all_filings(session)

    seen = load_seen_protocols()

    if bootstrap:
        for f in filings:
            seen.add(f.unique_key)
        save_seen_protocols(seen)
        log(f"Bootstrap complete. Marked {len(filings)} protocols as seen.")
        return [], len(filings), cvm_ok

    new_filings = [f for f in filings if f.unique_key not in seen]

    if new_filings:
        for f in new_filings:
            seen.add(f.unique_key)
        save_seen_protocols(seen)

    return new_filings, len(filings), cvm_ok


def archive_filings(new_filings: List[Filing]) -> None:
    """
    Acrescenta os filings detectados ao arquivo histórico CSV (base consultável,
    sem resumo do Claude — só metadados + título). Uma linha por filing.
    Durável: o Actions commita fatos_arquivo.csv de volta ao repositório.
    """
    if not new_filings:
        return
    try:
        new_file = (not ARCHIVE_FILE.exists()) or ARCHIVE_FILE.stat().st_size == 0
        with open(ARCHIVE_FILE, "a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(ARCHIVE_HEADER)
            for fl in new_filings:
                writer.writerow([
                    fl.filing_time or "",
                    fl.ticker or "",
                    fl.company_name or "",
                    fl.category or "",
                    fl.subject or fl.doc_type or "",
                    fl.protocol or "",
                    build_download_url(fl),
                ])
    except Exception as e:
        log(f"WARNING: falha ao gravar arquivo histórico: {e}")


# ============================================================================
# DOCUMENT FETCH + SUMMARIZATION
# ============================================================================

SUMMARY_MODEL = "claude-haiku-4-5"
SUMMARY_MAX_DOC_CHARS = 80_000
SUMMARY_SYSTEM_PROMPT = (
    "Você é um analista financeiro experiente especializado em mercado de capitais brasileiro. "
    "Sua tarefa é resumir comunicados de empresas listadas (Fatos Relevantes, Avisos aos Acionistas, "
    "etc.) em bullets curtos e objetivos em português brasileiro. Foque no que é importante para "
    "o investidor: o que aconteceu, valores, datas, partes envolvidas e potencial impacto. "
    "Use apenas informações que estão no documento — nunca invente dados. "
    "Se um número estiver no documento, cite-o; se não estiver, não especule."
)


def get_anthropic_api_key() -> Optional[str]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key.strip()
    if EMAIL_CONFIG_FILE.exists():
        try:
            with open(EMAIL_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            v = cfg.get("anthropic_api_key")
            if v:
                return str(v).strip()
        except Exception:
            pass
    return None


def get_healthcheck_url() -> Optional[str]:
    """
    URL de ping do dead-man's-switch (ex.: healthchecks.io).
    Lida do env HEALTHCHECK_URL ou do campo healthcheck_url em email_config.json.
    """
    url = os.environ.get("HEALTHCHECK_URL")
    if url:
        return url.strip()
    if EMAIL_CONFIG_FILE.exists():
        try:
            with open(EMAIL_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            v = cfg.get("healthcheck_url")
            if v:
                return str(v).strip()
        except Exception:
            pass
    return None


def ping_healthcheck(url: str) -> None:
    """Sinaliza vida ao watchdog externo. Nunca deixa o monitor quebrar pelo ping."""
    try:
        requests.get(url, timeout=10)
    except Exception as e:
        log(f"WARNING: heartbeat ping falhou: {e}")


def fetch_document_text(
    session: requests.Session, filing: Filing
) -> Optional[str]:
    url = build_download_url(filing)
    headers = {}
    if "_sec_url" in filing.download_params:
        headers["User-Agent"] = SEC_USER_AGENT
    try:
        r = session.get(url, headers=headers, timeout=60)
        r.raise_for_status()
    except Exception as e:
        log(f"WARNING: download falhou ({filing.identifier}): {e}")
        return None

    content = r.content
    if content[:4] == b"%PDF":
        try:
            reader = pypdf.PdfReader(io.BytesIO(content))
            text = ""
            for p in reader.pages:
                text += (p.extract_text() or "") + "\n"
            return text[:SUMMARY_MAX_DOC_CHARS]
        except Exception as e:
            log(f"WARNING: extração de PDF falhou ({filing.identifier}): {e}")
            return None

    try:
        soup = BeautifulSoup(content, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        return text[:SUMMARY_MAX_DOC_CHARS]
    except Exception as e:
        log(f"WARNING: extração de HTML falhou ({filing.identifier}): {e}")
        return None


def summarize_filing(
    client: anthropic.Anthropic, filing: Filing, doc_text: str
) -> Optional[List[str]]:
    user_prompt = (
        f"Empresa: {filing.company_name} ({filing.ticker})\n"
        f"Assunto: {filing.subject or filing.doc_type}\n"
        f"Data do arquivamento: {filing.filing_time}\n"
        f"Categoria: {filing.category}\n\n"
        f"--- INÍCIO DO DOCUMENTO ---\n{doc_text}\n--- FIM DO DOCUMENTO ---\n\n"
        "Resuma o documento em 3 a 7 bullets curtos, cada um em uma linha começando com '- '. "
        "Sem introdução, sem conclusão — apenas os bullets. "
        "Priorize: o evento principal, valores monetários, datas-chave, partes envolvidas, "
        "e impacto esperado para acionistas."
    )

    try:
        response = client.messages.create(
            model=SUMMARY_MODEL,
            max_tokens=1024,
            cache_control={"type": "ephemeral"},
            system=SUMMARY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except anthropic.APIError as e:
        log(f"WARNING: Claude API falhou ({filing.identifier}): {e}")
        return None
    except Exception as e:
        log(f"WARNING: erro inesperado no resumo ({filing.identifier}): {e}")
        return None

    raw = "".join(b.text for b in response.content if b.type == "text")
    bullets: List[str] = []
    for line in raw.split("\n"):
        line = line.strip()
        if line[:2] in ("- ", "• ", "* "):
            bullets.append(line[2:].strip())
        elif re.match(r"^\d+[\.\)]\s", line):
            bullets.append(re.sub(r"^\d+[\.\)]\s+", "", line).strip())
    return bullets or None


def gather_summaries(
    session: requests.Session, filings: List[Filing]
) -> Dict[str, List[str]]:
    if not filings:
        return {}
    api_key = get_anthropic_api_key()
    if not api_key:
        log(
            "INFO: ANTHROPIC_API_KEY não configurada — emails sairão sem resumo. "
            "Defina a variável de ambiente ou o campo anthropic_api_key em email_config.json."
        )
        return {}

    summaries: Dict[str, List[str]] = {}
    try:
        client = anthropic.Anthropic(api_key=api_key)
    except Exception as e:
        log(f"WARNING: falha ao inicializar cliente Anthropic: {e}")
        return {}

    for f in filings:
        text = fetch_document_text(session, f)
        if not text:
            continue
        bullets = summarize_filing(client, f, text)
        if bullets:
            summaries[f.protocol] = bullets
            log(f"Resumo gerado para {f.identifier} ({len(bullets)} bullets).")
    return summaries


# ============================================================================
# EMAIL ALERTS
# ============================================================================

DEFAULT_EMAIL_CONFIG = {
    "enabled": False,
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "smtp_username": "",
    "smtp_password": "",
    "from_addr": "",
    "to_addrs": ["d.xhp@hotmail.com"],
    "subject_prefix": "[Fato Relevante]",
    "anthropic_api_key": "",
    "healthcheck_url": "",
}


def load_email_config() -> Optional[Dict]:
    if not EMAIL_CONFIG_FILE.exists():
        with open(EMAIL_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_EMAIL_CONFIG, f, indent=2, ensure_ascii=False)
        log(
            f"Created template {EMAIL_CONFIG_FILE.name}. "
            "Edit it (set smtp credentials and enabled=true) to receive emails."
        )
        return None

    try:
        with open(EMAIL_CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        log(f"WARNING: failed to load email config: {e}")
        return None

    if not cfg.get("enabled"):
        return None
    if not cfg.get("smtp_username") or not cfg.get("smtp_password"):
        log("WARNING: email_config.json is missing smtp_username/smtp_password.")
        return None
    if not cfg.get("to_addrs"):
        log("WARNING: email_config.json has empty to_addrs.")
        return None
    return cfg


def _render_filing_html(filing: Filing, bullets: Optional[List[str]]) -> str:
    e = html.escape
    download_url = build_download_url(filing)

    if bullets:
        bullets_html = "\n".join(
            f'        <li style="margin-bottom:8px;">{e(b)}</li>'
            for b in bullets
        )
        summary_block = f"""
      <tr><td style="padding:8px 32px 24px 32px;">
        <div style="font-size:11px;font-weight:600;color:#718096;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px;">Resumo</div>
        <ul style="margin:0;padding-left:22px;color:#2d3748;font-size:15px;line-height:1.55;">
{bullets_html}
        </ul>
      </td></tr>"""
    else:
        summary_block = """
      <tr><td style="padding:8px 32px 24px 32px;">
        <div style="font-size:13px;color:#a0aec0;font-style:italic;">Resumo indisponível.</div>
      </td></tr>"""

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f1f3f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1a202c;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f1f3f5;padding:24px 12px;">
    <tr><td align="center">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="background:#ffffff;border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.06),0 4px 16px rgba(0,0,0,0.04);max-width:600px;width:100%;">
        <tr><td bgcolor="#1e3a8a" style="padding:28px 32px 24px 32px;background-color:#1e3a8a;">
          <div style="font-size:11px;font-weight:700;color:#bfdbfe;letter-spacing:1.5px;text-transform:uppercase;">{e(filing.category or 'Comunicado')}</div>
          <div style="margin-top:10px;font-size:22px;font-weight:700;color:#ffffff;line-height:1.3;">{e(filing.company_name)}</div>
          <div style="margin-top:10px;font-size:13px;color:#dbeafe;">
            <span style="display:inline-block;background-color:#3b82f6;color:#ffffff;padding:3px 10px;border-radius:12px;font-weight:700;letter-spacing:0.5px;">{e(filing.ticker)}</span>
            <span style="margin-left:10px;color:#dbeafe;">{e(filing.filing_time or 'Data N/D')}</span>
          </div>
        </td></tr>
        <tr><td style="padding:24px 32px 4px 32px;">
          <div style="font-size:11px;font-weight:600;color:#718096;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;">Assunto</div>
          <div style="font-size:17px;font-weight:600;color:#1a202c;line-height:1.4;">{e(filing.subject or filing.doc_type or 'Sem assunto')}</div>
        </td></tr>
        {summary_block}
        <tr><td style="padding:0 32px 28px 32px;">
          <a href="{e(download_url)}" style="display:inline-block;background:#3182ce;color:#ffffff;padding:12px 22px;border-radius:6px;text-decoration:none;font-weight:600;font-size:14px;letter-spacing:0.3px;">Baixar documento original</a>
        </td></tr>
        <tr><td style="padding:14px 32px;background:#f7fafc;border-top:1px solid #e2e8f0;font-size:12px;color:#718096;">
          <span style="display:inline-block;margin-right:14px;"><b style="color:#4a5568;">Protocolo:</b> {e(filing.protocol)}</span>
          <span style="display:inline-block;"><b style="color:#4a5568;">Categoria:</b> {e(filing.category or '—')}</span>
        </td></tr>
      </table>
      <div style="margin-top:14px;font-size:11px;color:#a0aec0;">
        Enviado pelo monitor CVM/SEC • {e(datetime.now().strftime('%d/%m/%Y %H:%M'))}
      </div>
    </td></tr>
  </table>
</body>
</html>"""


def _render_filing_text(filing: Filing, bullets: Optional[List[str]]) -> str:
    category_label = (filing.category or "Comunicado").upper()
    lines = [
        f"{category_label} — {filing.company_name} ({filing.ticker})",
        f"Data: {filing.filing_time or 'N/D'}",
        "",
        f"Assunto: {filing.subject or filing.doc_type or 'Sem assunto'}",
        "",
    ]
    if bullets:
        lines.append("Resumo:")
        lines.extend(f"  • {b}" for b in bullets)
    else:
        lines.append("Resumo indisponível.")
    lines += [
        "",
        f"Documento: {build_download_url(filing)}",
        "",
        f"Protocolo {filing.protocol} • Categoria: {filing.category or '—'}",
    ]
    return "\n".join(lines)


def _build_subject(cfg: Dict, filing: Filing) -> str:
    category = filing.category or cfg.get("subject_prefix", "[Fato Relevante]").strip("[] ")
    subject_text = filing.subject or filing.doc_type or "Sem assunto"
    return f"[{category}] {filing.ticker} - {subject_text}"


def _send_one_email(cfg: Dict, filing: Filing, bullets: Optional[List[str]]) -> bool:
    msg = EmailMessage()
    msg["Subject"] = _build_subject(cfg, filing)
    msg["From"] = cfg.get("from_addr") or cfg["smtp_username"]
    to_addrs = cfg["to_addrs"]
    if isinstance(to_addrs, str):
        to_addrs = [to_addrs]
    msg["To"] = ", ".join(to_addrs)

    msg.set_content(_render_filing_text(filing, bullets))
    msg.add_alternative(_render_filing_html(filing, bullets), subtype="html")

    try:
        port = int(cfg.get("smtp_port", 587))
        host = cfg["smtp_host"]
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as server:
                server.login(cfg["smtp_username"], cfg["smtp_password"])
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.starttls()
                server.login(cfg["smtp_username"], cfg["smtp_password"])
                server.send_message(msg)
        log(f"Email enviado: {msg['Subject']}")
        log_send("email", filing, True)
        return True
    except Exception as e:
        log(f"ERROR ao enviar email para {filing.identifier}: {e}")
        log_send("email", filing, False, str(e))
        return False


def send_email_alert(
    cfg: Dict,
    new_filings: List[Filing],
    summaries: Optional[Dict[str, List[str]]] = None,
) -> None:
    summaries = summaries or {}
    for filing in new_filings:
        _send_one_email(cfg, filing, summaries.get(filing.protocol))


# ============================================================================
# TELEGRAM ALERTS
# ============================================================================

def get_telegram_config() -> Optional[Dict]:
    """
    Retorna {'destinations': [{'bot_token', 'chat_id'}, ...]} se habilitado, senão None.
    Cada destino tem seu próprio bot (permite pessoas diferentes com bots diferentes).
    Fontes, em ordem:
      - envs TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (destino único), ou
      - bloco "telegram" em email_config.json, no formato novo
        {"enabled": true, "destinations": [{"bot_token": "...", "chat_id": ...}, ...]}
        ou no formato antigo {"enabled": true, "bot_token": "...", "chat_id": ...}.
    """
    def _dest(bt, ci) -> Optional[Dict]:
        if bt and ci not in (None, ""):
            return {"bot_token": str(bt).strip(), "chat_id": str(ci).strip()}
        return None

    destinations: List[Dict] = []

    env = _dest(os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID"))
    if env:
        destinations.append(env)

    if not destinations and EMAIL_CONFIG_FILE.exists():
        try:
            with open(EMAIL_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            tg = cfg.get("telegram") or {}
            if tg.get("enabled"):
                if isinstance(tg.get("destinations"), list):
                    for d in tg["destinations"]:
                        dest = _dest(d.get("bot_token"), d.get("chat_id"))
                        if dest:
                            destinations.append(dest)
                else:  # formato antigo (destino único)
                    dest = _dest(tg.get("bot_token"), tg.get("chat_id"))
                    if dest:
                        destinations.append(dest)
        except Exception as e:
            log(f"WARNING: failed to load telegram config: {e}")

    if not destinations:
        return None
    return {"destinations": destinations}


def _render_filing_telegram(filing: Filing, bullets: Optional[List[str]]) -> str:
    e = html.escape
    category = filing.category or "Comunicado"
    lines = [
        f"<b>[{e(category)}] {e(filing.ticker)} — {e(filing.company_name)}</b>",
        f"<i>{e(filing.subject or filing.doc_type or 'Sem assunto')}</i>",
    ]
    if filing.filing_time:
        lines.append(f"🗓 {e(filing.filing_time)}")
    lines.append("")
    if bullets:
        lines.extend(f"• {e(b)}" for b in bullets)
    else:
        lines.append("<i>Resumo indisponível.</i>")
    lines.append("")
    url = e(build_download_url(filing))
    lines.append(f'<a href="{url}">Baixar documento original</a>')
    # Limite do Telegram é 4096 caracteres por mensagem.
    return "\n".join(lines)[:4000]


def _send_one_telegram(cfg: Dict, filing: Filing, bullets: Optional[List[str]]) -> bool:
    text = _render_filing_telegram(filing, bullets)
    ok_all = True
    for dest in cfg["destinations"]:
        chat_id = dest["chat_id"]
        api_url = f"https://api.telegram.org/bot{dest['bot_token']}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            r = requests.post(api_url, json=payload, timeout=30)
            r.raise_for_status()
            log(f"Telegram enviado (chat {chat_id}): {filing.identifier}")
            log_send("telegram", filing, True, f"chat {chat_id}")
        except Exception as ex:
            log(f"ERROR ao enviar Telegram (chat {chat_id}) para {filing.identifier}: {ex}")
            log_send("telegram", filing, False, f"chat {chat_id}: {ex}")
            ok_all = False
    return ok_all


def send_telegram_alert(
    cfg: Dict,
    new_filings: List[Filing],
    summaries: Optional[Dict[str, List[str]]] = None,
) -> None:
    summaries = summaries or {}
    for filing in new_filings:
        _send_one_telegram(cfg, filing, summaries.get(filing.protocol))


def format_new_filings(
    new_filings: List[Filing],
    summaries: Optional[Dict[str, List[str]]] = None,
) -> str:
    parts: List[str] = []
    for f in new_filings:
        block = [
            f"Empresa: {f.company_name} ({f.ticker})",
            f"Data: {f.filing_time or 'N/A'}",
            f"Assunto: {f.subject or '(sem assunto no índice)'}",
            f"Protocolo: {f.protocol}",
            f"Categoria: {f.category}",
            f"Tipo: {f.doc_type}",
            f"Download: {build_download_url(f)}",
        ]
        if summaries and f.protocol in summaries and summaries[f.protocol]:
            block.append("")
            block.append("Resumo:")
            for b in summaries[f.protocol]:
                block.append(f"  • {b}")
        parts.append("\n".join(block))
    return "\n\n---\n\n".join(parts)


# ============================================================================
# ENTRYPOINTS
# ============================================================================

def main() -> int:
    bootstrap = "--bootstrap" in sys.argv
    once = "--once" in sys.argv
    test_email = "--test-email" in sys.argv
    test_telegram = "--test-telegram" in sys.argv

    session = make_session()

    try:
        if test_email or test_telegram:
            sample = Filing(
                cvm_code="0", company_name="Teste", ticker="TEST",
                protocol="TEST-0001", sequence="0", version="0",
                desc_type="Teste", category="Teste",
                doc_type="Mensagem de teste", subject="Verificação de envio",
                filing_time=datetime.now().strftime("%d/%m/%Y %H:%M"),
                download_params={"_sec_url": "https://example.com/teste"},
            )

        if test_email:
            cfg = load_email_config()
            if not cfg:
                print("Email não está configurado/habilitado em email_config.json.")
                return 1
            send_email_alert(cfg, [sample])
            return 0

        if test_telegram:
            tg_cfg = get_telegram_config()
            if not tg_cfg:
                print("Telegram não está configurado/habilitado em email_config.json.")
                return 1
            send_telegram_alert(tg_cfg, [sample])
            return 0

        if bootstrap:
            _ = check_once(session, bootstrap=True)
            return 0

        new_filings, total_matched, cvm_ok = check_once(session, bootstrap=False)

        summaries: Dict[str, List[str]] = {}
        if new_filings:
            summaries = gather_summaries(session, new_filings)

        if once:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(
                f"[{stamp}] Checked CVM + SEC: {total_matched} filing(s) "
                f"recently from monitored companies, {len(new_filings)} new."
            )
            if new_filings:
                print()
                print(format_new_filings(new_filings, summaries))
        else:
            if new_filings:
                print(format_new_filings(new_filings, summaries))

        if new_filings:
            archive_filings(new_filings)   # base histórica (registra o que foi detectado)
            email_cfg = load_email_config()
            if email_cfg:
                send_email_alert(email_cfg, new_filings, summaries)
            tg_cfg = get_telegram_config()
            if tg_cfg:
                send_telegram_alert(tg_cfg, new_filings, summaries)

        # Heartbeat / dead-man's-switch: pinga só quando a checagem foi real
        # (CVM respondeu). Numa falha transitória da CVM NÃO pingamos — a "grace"
        # do healthchecks absorve um ping perdido; só uma queda *sustentada*
        # (vários runs seguidos sem pingar) ou um crash disparam o alerta. Isso
        # evita o flapping que o /fail causava a cada hiccup da CVM.
        hc_url = get_healthcheck_url()
        if hc_url and cvm_ok:
            ping_healthcheck(hc_url)

        return 0

    except Exception as e:
        log(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
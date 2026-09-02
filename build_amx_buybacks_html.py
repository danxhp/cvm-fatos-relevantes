# -*- coding: utf-8 -*-
"""Gera amx_buybacks_ltm.html a partir das series apuradas."""
import json, io, os

OUT = r"c:\dev\GS\CVM_Fatos_relevantes\amx_buybacks_ltm.html"

# (mes, MXN, acoes, fonte)  -- fonte: 20F | BMV | DERIVED
DADOS = [
    ("2025-09", "Sep 2025",   638015532.32,  34250000, "20F"),
    ("2025-10", "Oct 2025",   281269371.00,  14250000, "20F"),
    ("2025-11", "Nov 2025",    79129971.13,   3750000, "20F"),
    ("2025-12", "Dec 2025",   769179293.33,  40250000, "20F"),
    ("2026-01", "Jan 2026",   446646418.00,  24600000, "BMV"),
    ("2026-02", "Feb 2026",   283003795.00,  13660000, "BMV"),
    ("2026-03", "Mar 2026",   649827619.00,  30240000, "BMV"),
    ("2026-04", "Apr 2026",   930361141.00,      None, "DERIVED"),
    ("2026-05", "May 2026",  1607925051.00,  70850000, "BMV"),
    ("2026-06", "Jun 2026",   682235976.00,  30150000, "BMV"),
    ("2026-07", "Jul 2026",  1381730659.00,  61500000, "BMV"),
    ("2026-08", "Aug 2026",  1913814704.00,  94000000, "BMV"),
]

FX = {  # media simples das cotacoes diarias do mes, MXN->USD
    "2025-09": 0.054052, "2025-10": 0.054271, "2025-11": 0.054284, "2025-12": 0.055286,
    "2026-01": 0.056634, "2026-02": 0.058017, "2026-03": 0.056308, "2026-04": 0.057381,
    "2026-05": 0.057773, "2026-06": 0.057559, "2026-07": 0.057234, "2026-08": 0.058607,
}

FONTE_LABEL = {
    "20F": "20-F Item 16E",
    "BMV": "BMV daily reports",
    "DERIVED": "Derived (see note 2)",
}

rows = []
for key, label, mxn, sh, src in DADOS:
    fx = FX[key]
    usd = mxn * fx / 1e6
    rows.append({
        "key": key, "label": label, "short": label.split()[0],
        "mxn_mn": mxn / 1e6, "usd_mn": usd, "shares": sh,
        "px": (mxn / sh) if sh else None,
        "fx": 1 / fx, "src": src,
    })

tot_usd = sum(r["usd_mn"] for r in rows)
tot_mxn = sum(r["mxn_mn"] for r in rows)
tot_sh = sum(r["shares"] for r in rows if r["shares"])
last3 = sum(r["usd_mn"] for r in rows[-3:])
prev3 = sum(r["usd_mn"] for r in rows[-6:-3])

# ---------------------------------------------------------------- SVG
W, H = 840, 300
ML, MR, MT, MB = 52, 12, 26, 42
pw, ph = W - ML - MR, H - MT - MB
vmax = max(r["usd_mn"] for r in rows)
# topo do eixo arredondado para cima
step = 25
top = step * (int(vmax / step) + 1)
n = len(rows)
slot = pw / n
bw = slot * 0.52  # marca fina: ~52% do slot deixa o eixo respirar e evita a barra "gorda"

def y_of(v):
    return MT + ph - (v / top) * ph

grid, bars, xlab = [], [], []
v = 0
while v <= top + 0.01:
    y = y_of(v)
    grid.append(
        f'<line class="grid" x1="{ML}" y1="{y:.1f}" x2="{ML+pw}" y2="{y:.1f}"/>'
        f'<text class="ytick" x="{ML-8}" y="{y+3.5:.1f}">{int(v)}</text>'
    )
    v += step

rot = [r["usd_mn"] for r in rows]
i_max = rot.index(max(rot))
i_min = rot.index(min(rot))
destaque = {i_max, i_min, len(rows) - 1, 7}  # pico, minimo, ultimo, derivado

for i, r in enumerate(rows):
    x = ML + i * slot + (slot - bw) / 2
    y = y_of(r["usd_mn"])
    h = MT + ph - y
    fill = "url(#hatch)" if r["src"] == "DERIVED" else "var(--series-1)"
    bars.append(
        f'<g class="bar" tabindex="0" data-m="{r["label"]}" data-usd="{r["usd_mn"]:.1f}" '
        f'data-mxn="{r["mxn_mn"]:,.0f}" data-sh="{(("%.1f mn" % (r["shares"]/1e6)) if r["shares"] else "n/a")}" '
        f'data-src="{FONTE_LABEL[r["src"]]}">'
        f'<rect class="hit" x="{ML+i*slot:.1f}" y="{MT}" width="{slot:.1f}" height="{ph}"/>'
        f'<rect class="mark" x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{max(h,1):.1f}" rx="4" fill="{fill}"/>'
        f'</g>'
    )
    if i in destaque:
        bars.append(f'<text class="vlab" x="{x+bw/2:.1f}" y="{y-7:.1f}">{r["usd_mn"]:.0f}</text>')
    xlab.append(
        f'<text class="xtick" x="{x+bw/2:.1f}" y="{MT+ph+16}">{r["short"]}</text>'
        + (f'<text class="xtick yr" x="{x+bw/2:.1f}" y="{MT+ph+29}">{r["key"][:4]}</text>'
           if i == 0 or r["key"][5:7] == "01" else "")
    )

svg = f'''<svg viewBox="0 0 {W} {H}" role="img" aria-label="Monthly share buybacks in USD millions, September 2025 to August 2026">
<defs>
  <pattern id="hatch" width="6" height="6" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
    <rect width="6" height="6" fill="var(--series-1)" opacity="0.30"/>
    <line x1="0" y1="0" x2="0" y2="6" stroke="var(--series-1)" stroke-width="3"/>
  </pattern>
</defs>
<line class="axis" x1="{ML}" y1="{MT+ph}" x2="{ML+pw}" y2="{MT+ph}"/>
{''.join(grid)}
{''.join(bars)}
{''.join(xlab)}
</svg>'''

# ---------------------------------------------------------------- tabela
trs = []
for r in rows:
    sh = f'{r["shares"]/1e6:,.2f}' if r["shares"] else '<span class="na">n/a</span>'
    px = f'{r["px"]:,.2f}' if r["px"] else '<span class="na">n/a</span>'
    tag = ' <span class="tag">derived</span>' if r["src"] == "DERIVED" else ""
    trs.append(
        "<tr>"
        f'<th scope="row">{r["label"]}{tag}</th>'
        f'<td class="num">{sh}</td>'
        f'<td class="num">{px}</td>'
        f'<td class="num">{r["mxn_mn"]:,.0f}</td>'
        f'<td class="num strong">{r["usd_mn"]:,.1f}</td>'
        f'<td class="num muted">{r["fx"]:,.2f}</td>'
        "</tr>"
    )

html = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AMX Share Buybacks — LTM</title>
<style>
  :root {{
    color-scheme: light;
    --surface-0: #f4f4f2;
    --surface-1: #fcfcfb;
    --border:    #e2e1dd;
    --text-primary:   #0b0b0b;
    --text-secondary: #52514e;
    --text-muted:     #86857f;
    --series-1:  #2a78d6;
    --grid:      #e8e7e3;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      color-scheme: dark;
      --surface-0: #121211;
      --surface-1: #1a1a19;
      --border:    #333331;
      --text-primary:   #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted:     #8d8c84;
      --series-1:  #3987e5;
      --grid:      #2b2b29;
    }}
  }}
  :root[data-theme="dark"] {{
    color-scheme: dark;
    --surface-0: #121211;
    --surface-1: #1a1a19;
    --border:    #333331;
    --text-primary:   #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted:     #8d8c84;
    --series-1:  #3987e5;
    --grid:      #2b2b29;
  }}

  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--surface-0); color: var(--text-primary);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
  }}
  .wrap {{ max-width: 960px; margin: 0 auto; padding: 40px 24px 64px; }}

  header {{ margin-bottom: 28px; }}
  h1 {{ font-size: 24px; line-height: 1.25; margin: 0 0 6px; letter-spacing: -0.01em; }}
  .sub {{ color: var(--text-secondary); font-size: 14px; margin: 0; }}

  .kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin: 24px 0 28px; }}
  .kpi {{ background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }}
  .kpi .k {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted); margin-bottom: 6px; }}
  .kpi .v {{ font-size: 22px; font-weight: 600; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }}
  .kpi .u {{ font-size: 12px; color: var(--text-secondary); font-weight: 400; margin-left: 3px; }}

  .card {{ background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 20px 20px 12px; margin-bottom: 24px; }}
  .card h2 {{ font-size: 14px; font-weight: 600; margin: 0 0 2px; }}
  .card .cap {{ font-size: 12px; color: var(--text-secondary); margin: 0 0 14px; }}

  .chartbox {{ position: relative; }}
  svg {{ display: block; width: 100%; height: auto; overflow: visible; }}
  .grid {{ stroke: var(--grid); stroke-width: 1; }}
  .axis {{ stroke: var(--border); stroke-width: 1; }}
  .ytick {{ fill: var(--text-muted); font-size: 10px; text-anchor: end; font-variant-numeric: tabular-nums; }}
  .xtick {{ fill: var(--text-secondary); font-size: 10px; text-anchor: middle; }}
  .xtick.yr {{ fill: var(--text-muted); font-size: 9px; }}
  .vlab {{ fill: var(--text-primary); font-size: 10px; font-weight: 600; text-anchor: middle; font-variant-numeric: tabular-nums; }}
  .hit {{ fill: transparent; }}
  .bar {{ cursor: default; outline: none; }}
  .bar .mark {{ transition: opacity .12s ease; }}
  .bar:hover .mark, .bar:focus-visible .mark {{ opacity: .78; }}
  .bar:focus-visible .mark {{ stroke: var(--text-primary); stroke-width: 1.5; }}

  .tip {{
    position: absolute; pointer-events: none; opacity: 0; transform: translate(-50%, -100%);
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
    padding: 8px 10px; font-size: 12px; white-space: nowrap; z-index: 5;
    box-shadow: 0 4px 14px rgba(0,0,0,.14); transition: opacity .1s ease;
  }}
  .tip.on {{ opacity: 1; }}
  .tip b {{ display: block; margin-bottom: 3px; font-size: 12px; }}
  .tip .r {{ color: var(--text-secondary); font-variant-numeric: tabular-nums; }}

  table {{ width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }}
  caption {{ text-align: left; font-size: 12px; color: var(--text-secondary); padding-bottom: 10px; }}
  th, td {{ padding: 7px 10px; border-bottom: 1px solid var(--border); text-align: left; font-weight: 400; }}
  thead th {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); font-weight: 600; border-bottom-width: 1px; }}
  tbody th {{ font-weight: 500; white-space: nowrap; }}
  .num {{ text-align: right; }}
  .strong {{ font-weight: 600; }}
  .muted {{ color: var(--text-muted); }}
  .na {{ color: var(--text-muted); }}
  tfoot td, tfoot th {{ font-weight: 600; border-top: 2px solid var(--border); border-bottom: none; padding-top: 10px; }}
  .tag {{ font-size: 10px; color: var(--text-muted); border: 1px solid var(--border); border-radius: 4px; padding: 1px 5px; margin-left: 5px; font-weight: 400; }}
  .tblwrap {{ overflow-x: auto; }}

  .notes {{ font-size: 12px; color: var(--text-secondary); }}
  .notes h2 {{ font-size: 13px; color: var(--text-primary); margin: 0 0 8px; }}
  .notes ol {{ margin: 0; padding-left: 18px; }}
  .notes li {{ margin-bottom: 7px; }}
  .notes code {{ font-size: 11px; background: var(--surface-0); padding: 1px 4px; border-radius: 3px; }}
</style>
</head>
<body>
<div class="wrap">

  <header>
    <h1>América Móvil — Share Buybacks</h1>
    <p class="sub">Last twelve months · September 2025 – August 2026 · amounts in USD millions</p>
  </header>

  <div class="kpis">
    <div class="kpi"><div class="k">LTM buybacks</div><div class="v">{tot_usd:,.0f}<span class="u">USD mn</span></div></div>
    <div class="kpi"><div class="k">LTM, local currency</div><div class="v">{tot_mxn/1000:,.2f}<span class="u">MXN bn</span></div></div>
    <div class="kpi"><div class="k">Shares repurchased</div><div class="v">{tot_sh/1e6:,.0f}<span class="u">mn</span></div></div>
    <div class="kpi"><div class="k">Last 3M vs prior 3M</div><div class="v">{last3:,.0f}<span class="u">vs {prev3:,.0f}</span></div></div>
  </div>

  <div class="card">
    <h2>Monthly buybacks</h2>
    <p class="cap">USD mn, converted at each month's average daily MXN/USD rate. Hatched bar is derived, not directly observed.</p>
    <div class="chartbox">
      {svg}
      <div class="tip" id="tip"></div>
    </div>
  </div>

  <div class="card">
    <div class="tblwrap">
    <table>
      <caption>Monthly detail. Amounts are the fund's actual cash consumption; share counts from treasury balances.</caption>
      <thead>
        <tr>
          <th scope="col">Month</th>
          <th scope="col" class="num">Shares (mn)</th>
          <th scope="col" class="num">Avg price (MXN)</th>
          <th scope="col" class="num">Amount (MXN mn)</th>
          <th scope="col" class="num">Amount (USD mn)</th>
          <th scope="col" class="num">MXN/USD</th>
        </tr>
      </thead>
      <tbody>
        {''.join(trs)}
      </tbody>
      <tfoot>
        <tr>
          <th scope="row">LTM total</th>
          <td class="num">{tot_sh/1e6:,.2f}</td>
          <td class="num"><span class="na">—</span></td>
          <td class="num">{tot_mxn:,.0f}</td>
          <td class="num">{tot_usd:,.1f}</td>
          <td class="num"><span class="na">—</span></td>
        </tr>
      </tfoot>
    </table>
    </div>
  </div>

  <div class="card notes">
    <h2>Sources and method</h2>
    <ol>
      <li><b>Sep–Dec 2025</b> come from the FY2025 <b>Form 20-F, Item 16E</b> (monthly repurchase table).
          <b>Jan–Aug 2026</b> are computed from América Móvil's daily <i>"Información de recompra"</i> filings
          with the <b>Bolsa Mexicana de Valores</b>. Each month's amount is the decline in the buyback fund's
          remaining balance between consecutive month-end filings — i.e. cash actually deployed.</li>
      <li><b>April 2026 is derived, not observed.</b> The 23 April 2026 shareholders' meeting authorised up to
          Ps.10bn of new funds, so the fund balance <i>rose</i> that month and the month-over-month method breaks.
          The figure shown is the residual of the 1H26 buyback total disclosed in the 2Q26 6-K
          (Ps.4.6bn) less the five clean months (Ps.3.67bn). The implied fund injection of Ps.10.03bn
          matches the authorised Ps.10bn, which corroborates the residual.</li>
      <li><b>April 2026 share count is not available.</b> The same meeting cancelled treasury shares
          (balance fell from 1,050.0mn to 14.5mn), so the treasury delta cannot isolate purchases that month.
          The LTM share total therefore excludes April.</li>
      <li><b>FX:</b> each month is converted at the simple average of that month's daily MXN/USD rates
          (ECB reference series via frankfurter.app), per the period-average convention.</li>
      <li><b>Cross-checks passed.</b> The BMV filing for 31 Dec 2025 reports a remaining fund balance of
          MXN 12,990,090,502 against MXN 12,990,090,518 in the audited 20-F — a 16-peso difference on
          13bn. Within each daily filing, the sum of individual trade amounts reconciles exactly to the
          change in fund balance.</li>
    </ol>
  </div>

</div>

<script>
(function () {{
  var tip = document.getElementById('tip');
  var box = tip.parentElement;
  function show(g) {{
    tip.innerHTML = '<b>' + g.dataset.m + '</b>'
      + '<div class="r">USD ' + g.dataset.usd + ' mn</div>'
      + '<div class="r">MXN ' + g.dataset.mxn + ' mn</div>'
      + '<div class="r">Shares: ' + g.dataset.sh + '</div>'
      + '<div class="r">' + g.dataset.src + '</div>';
    var m = g.querySelector('.mark').getBoundingClientRect();
    var b = box.getBoundingClientRect();
    tip.style.left = (m.left - b.left + m.width / 2) + 'px';
    tip.style.top = (m.top - b.top - 8) + 'px';
    tip.classList.add('on');
  }}
  function hide() {{ tip.classList.remove('on'); }}
  Array.prototype.forEach.call(document.querySelectorAll('.bar'), function (g) {{
    g.addEventListener('mouseenter', function () {{ show(g); }});
    g.addEventListener('focus', function () {{ show(g); }});
    g.addEventListener('mouseleave', hide);
    g.addEventListener('blur', hide);
  }});
  box.addEventListener('mouseleave', hide);
}})();
</script>
</body>
</html>
'''

with io.open(OUT, "w", encoding="utf-8") as f:
    f.write(html)

print("gerado:", OUT)
print()
print("%-10s %12s %12s %10s" % ("MONTH", "MXN mn", "USD mn", "SHARES mn"))
for r in rows:
    print("%-10s %12s %12s %10s" % (
        r["label"], format(r["mxn_mn"], ",.0f"), format(r["usd_mn"], ",.1f"),
        (format(r["shares"] / 1e6, ",.2f") if r["shares"] else "n/a")))
print("-" * 48)
print("%-10s %12s %12s %10s" % ("LTM", format(tot_mxn, ",.0f"), format(tot_usd, ",.1f"), format(tot_sh / 1e6, ",.2f")))

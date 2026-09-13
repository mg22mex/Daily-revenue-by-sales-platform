"""Hosted Daily Revenue Dashboard HTML matching the WEATHERMAN layout."""
from __future__ import annotations

import html
import logging
import shutil
from decimal import Decimal
from pathlib import Path

from reporting.models import (
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    PLATFORM_KEYS,
    PLATFORM_SHORT,
    DailyReport,
    money_str,
    mult_str,
    pct_str,
)

log = logging.getLogger("daily_report.dashboard")


def _esc(value: object) -> str:
    return html.escape(str(value))


def build_dashboard_html(report: DailyReport) -> str:
    # Always derive header date from the active report target (never hardcode).
    long_date = report.format_target_date()
    subtitle_channels = (
        "Amazon + Shopify Direct + DICK'S SPORTING GOODS + NORDSTROM + Walmart"
    )

    period_cards = []
    for card in report.period_cards:
        period_cards.append(
            f"""
            <div class="period-card" style="background:{_esc(card.color)};">
              <div class="period-label">{_esc(card.label)}</div>
              <div class="period-value">{_esc(money_str(card.revenue))}</div>
              <div class="period-sub">{_esc(card.subtitle)}</div>
            </div>
            """
        )

    platform_cards = []
    for platform in report.ordered_platforms():
        if not platform.available:
            sub = f"Unavailable · {_esc(platform.error or 'source error')}"
            value = "Unavailable"
        else:
            bits = [f"{platform.units} units"]
            if platform.key == "amazon" and report.ad_metrics.amazon_real_acos is not None:
                bits.append(f"Real ACOS {pct_str(report.ad_metrics.amazon_real_acos)}")
            elif platform.key == "shopify_direct":
                bits.append(f"{platform.order_count} orders")
            elif platform.key in {"dsg", "nordstrom"}:
                bits.append(f"{platform.order_count} orders")
            elif platform.key == "walmart":
                bits.append(f"{platform.order_count} orders")
                if platform.note:
                    bits.append(platform.note)
            elif platform.note:
                bits.append(platform.note)
            sub = " · ".join(bits)
            value = money_str(platform.revenue)
        platform_cards.append(
            f"""
            <div class="metric-card">
              <div class="metric-label">{_esc(platform.label)} REVENUE</div>
              <div class="metric-value">{_esc(value)}</div>
              <div class="metric-sub">{sub}</div>
            </div>
            """
        )

    ads = report.ad_metrics
    ad_cards = f"""
      <div class="metric-card">
        <div class="metric-label">SHOPIFY BLENDED COST OF SALES</div>
        <div class="metric-value">{_esc(pct_str(ads.shopify_blended_cos if ads.shopify_blended_cos is not None else Decimal('0')))}</div>
        <div class="metric-sub">Ad spend ÷ Shopify order revenue · lower is better</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">SHOPIFY REVENUE PER AD DOLLAR</div>
        <div class="metric-value">{_esc(mult_str(ads.shopify_revenue_per_ad_dollar if ads.shopify_revenue_per_ad_dollar is not None else Decimal('0')))}</div>
        <div class="metric-sub">Shopify order revenue ÷ ad spend · higher is better</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">SHOPIFY AD SPEND</div>
        <div class="metric-value">{_esc(money_str(ads.shopify_ad_spend if ads.shopify_ad_spend is not None else Decimal('0')))}</div>
        <div class="metric-sub">Amazon excluded</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">SHOPIFY RECONCILIATION</div>
        <div class="metric-value">{_esc(money_str(report.reconciliation_revenue_variance))}</div>
        <div class="metric-sub">Direct + DSG + Nordstrom variance · {report.reconciliation_unit_variance} units</div>
      </div>
    """

    total_rows = []
    for key in CATEGORY_ORDER:
        total_rows.append(
            f"<tr><td>{_esc(CATEGORY_LABELS[key])}</td>"
            f"<td class='num'>{report.category_totals.get(key, 0)}</td></tr>"
        )

    by_platform_rows = []
    for key in CATEGORY_ORDER:
        cells = [f"<td>{_esc(CATEGORY_LABELS[key])}</td>"]
        row_total = 0
        for platform_key in PLATFORM_KEYS:
            count = report.category_by_platform.get(platform_key, {}).get(key, 0)
            row_total += count
            cells.append(f"<td class='num'>{count}</td>")
        cells.append(f"<td class='num'>{row_total}</td>")
        by_platform_rows.append("<tr>" + "".join(cells) + "</tr>")

    sku_rows = []
    for row in report.sku_rows:
        sku_rows.append(
            "<tr>"
            f"<td>{_esc(row.platform)}</td>"
            f"<td>{_esc(row.sku)}</td>"
            f"<td>{_esc(row.item)}</td>"
            f"<td class='num'>{row.units}</td>"
            f"<td class='num'>{_esc(money_str(row.revenue))}</td>"
            "</tr>"
        )
    if not sku_rows:
        sku_rows.append(
            "<tr><td colspan='5' class='empty'>No SKU rows available for this report date.</td></tr>"
        )

    platform_header_cells = "".join(
        f"<th>{_esc(PLATFORM_SHORT[k])}</th>" for k in PLATFORM_KEYS
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Daily Revenue Dashboard · {_esc(long_date)}</title>
  <style>
    :root {{
      --navy: #0B192C;
      --bg: #F3F5F7;
      --card: #ffffff;
      --text: #1F2937;
      --muted: #6B7280;
      --blue: #3F51B5;
      --teal: #0F766E;
      --teal-mid: #14919B;
      --green: #2E7D32;
      --border: #E5E7EB;
      --accent-bar: #5C6BC0;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", Inter, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    .header {{
      background: var(--navy);
      color: #fff;
      padding: 22px 28px 20px;
    }}
    .brand {{
      font-size: 13px;
      letter-spacing: 0.14em;
      font-weight: 700;
      opacity: 0.95;
    }}
    .header h1 {{
      margin: 8px 0 6px;
      font-size: 28px;
      font-weight: 700;
    }}
    .header .sub {{
      font-size: 13px;
      opacity: 0.85;
    }}
    .wrap {{
      padding: 20px 24px 40px;
      max-width: 1400px;
      margin: 0 auto;
    }}
    .section-label {{
      margin: 18px 0 10px;
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
      font-weight: 700;
    }}
    .period-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
    }}
    .period-card {{
      border-radius: 10px;
      padding: 18px 16px;
      color: #fff;
      min-height: 110px;
    }}
    .period-label {{
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      font-weight: 700;
      opacity: 0.9;
    }}
    .period-value {{
      margin-top: 10px;
      font-size: 28px;
      font-weight: 700;
    }}
    .period-sub {{
      margin-top: 6px;
      font-size: 12px;
      opacity: 0.9;
    }}
    .platform-grid {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 12px;
    }}
    .ad-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }}
    .metric-card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-top: 3px solid var(--accent-bar);
      border-radius: 10px;
      padding: 14px 14px 16px;
      box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
    }}
    .metric-label {{
      font-size: 11px;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--muted);
      font-weight: 700;
    }}
    .metric-value {{
      margin-top: 8px;
      font-size: 24px;
      font-weight: 700;
      color: #1a237e;
    }}
    .metric-sub {{
      margin-top: 6px;
      font-size: 12px;
      color: var(--muted);
    }}
    .tables {{
      display: grid;
      grid-template-columns: 1fr 1.6fr;
      gap: 14px;
    }}
    .panel {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      overflow: hidden;
      box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
    }}
    .panel h3 {{
      margin: 0;
      padding: 12px 14px;
      font-size: 13px;
      background: #EEF2F7;
      border-bottom: 1px solid var(--border);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      padding: 10px 12px;
      border-bottom: 1px solid var(--border);
      text-align: left;
      vertical-align: top;
    }}
    th {{
      background: #F8FAFC;
      color: var(--muted);
      font-size: 11px;
      letter-spacing: 0.05em;
      text-transform: uppercase;
    }}
    td.num, th.num {{ text-align: right; white-space: nowrap; }}
    td.empty {{ color: var(--muted); text-align: center; padding: 18px; }}
    .sku-panel {{ margin-top: 8px; }}
    @media (max-width: 1100px) {{
      .period-grid, .platform-grid, .ad-grid, .tables {{
        grid-template-columns: 1fr 1fr;
      }}
    }}
    @media (max-width: 700px) {{
      .period-grid, .platform-grid, .ad-grid, .tables {{
        grid-template-columns: 1fr;
      }}
      .wrap {{ padding: 14px; }}
      .header {{ padding-left: 14px; padding-right: 14px; }}
    }}
  </style>
</head>
<body>
  <header class="header">
    <div class="brand">WEATHERMAN</div>
    <h1>Daily Revenue Dashboard</h1>
    <div class="sub">{_esc(subtitle_channels)} · {_esc(long_date)}</div>
  </header>

  <main class="wrap">
    <div class="period-grid">
      {''.join(period_cards)}
    </div>

    <div class="section-label">Sales platform performance</div>
    <div class="platform-grid">
      {''.join(platform_cards)}
    </div>

    <div class="section-label">Ad metrics &amp; reconciliation</div>
    <div class="ad-grid">
      {ad_cards}
    </div>

    <div class="section-label">Category unit performance</div>
    <div class="tables">
      <div class="panel">
        <h3>All-platform category unit totals</h3>
        <table>
          <thead><tr><th>Category</th><th class="num">Total</th></tr></thead>
          <tbody>
            {''.join(total_rows)}
          </tbody>
        </table>
      </div>
      <div class="panel">
        <h3>Category units by platform</h3>
        <table>
          <thead>
            <tr>
              <th>Category</th>
              {platform_header_cells}
              <th class="num">Total</th>
            </tr>
          </thead>
          <tbody>
            {''.join(by_platform_rows)}
          </tbody>
        </table>
      </div>
    </div>

    <div class="section-label">SKU drilldown · five channels</div>
    <div class="panel sku-panel">
      <table>
        <thead>
          <tr>
            <th>Platform</th>
            <th>SKU</th>
            <th>Item</th>
            <th class="num">Units</th>
            <th class="num">Revenue</th>
          </tr>
        </thead>
        <tbody>
          {''.join(sku_rows)}
        </tbody>
      </table>
    </div>
  </main>
</body>
</html>
"""


def write_dashboard(report: DailyReport, docs_dir: Path) -> tuple[Path, Path]:
    """Write dated dashboard HTML, then copy it to docs/index.html (latest)."""
    docs_dir.mkdir(parents=True, exist_ok=True)
    html_body = build_dashboard_html(report)

    dated = docs_dir / f"{report.target_date.isoformat()}.html"
    latest = docs_dir / "index.html"

    dated.write_text(html_body, encoding="utf-8")
    # Always refresh the Pages landing page from the dated artifact for this run.
    shutil.copyfile(dated, latest)

    log.info(
        "Dashboard written for target_date=%s → %s (copied to %s)",
        report.format_target_date(),
        dated.name,
        latest.name,
    )
    return dated, latest

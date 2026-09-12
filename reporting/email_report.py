"""HTML email body matching the TripleWhale-style daily revenue layout."""
from __future__ import annotations

from reporting.models import (
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    DailyReport,
    money_str,
    mult_str,
    pct_str,
)


def build_subject(report: DailyReport) -> str:
    return f"Daily revenue by sales platform — {report.report_day.isoformat()}"


def build_email_html(report: DailyReport) -> str:
    day = report.report_day
    try:
        long_date = day.strftime("%A, %B %-d, %Y")
    except ValueError:
        long_date = day.strftime("%A, %B %d, %Y").replace(" 0", " ")

    platform_lines = []
    for platform in report.ordered_platforms():
        value = money_str(platform.revenue, unavailable=not platform.available)
        platform_lines.append(f"{platform.label}: {value}")

    total = money_str(report.total_revenue())
    platform_block = "<br>\n".join(platform_lines + [f"Total: {total}"])

    category_lines = []
    for key in CATEGORY_ORDER:
        label = CATEGORY_LABELS[key]
        category_lines.append(f"{label}: {report.category_totals.get(key, 0)}")
    category_block = "<br>\n".join(category_lines)

    ads = report.ad_metrics
    ad_block = "<br>\n".join(
        [
            f"Amazon Real ACOS: {pct_str(ads.amazon_real_acos, unavailable=ads.amazon_real_acos is None)}",
            f"Shopify Blended Cost of Sales: {pct_str(ads.shopify_blended_cos if ads.shopify_blended_cos is not None else 0)}",
            f"Shopify Revenue per Ad Dollar: {mult_str(ads.shopify_revenue_per_ad_dollar if ads.shopify_revenue_per_ad_dollar is not None else 0)}",
        ]
    )

    button_label = f"Daily Revenue Dashboard {day.strftime('%m %d %y')}"
    dashboard_url = report.dashboard_url or "#"

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{build_subject(report)}</title>
</head>
<body style="margin:0;padding:0;background:#ffffff;color:#202124;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;">
  <div style="max-width:640px;padding:20px 16px;">
    <p style="margin:0 0 16px;">Hi {report.greeting_name},</p>
    <p style="margin:0 0 16px;">Here is yesterday's revenue report for {long_date}.</p>

    <p style="margin:0 0 8px;">Revenue by sales platform</p>
    <p style="margin:0 0 16px;">{platform_block}</p>

    <p style="margin:0 0 8px;">Category unit totals</p>
    <p style="margin:0 0 16px;">{category_block}</p>

    <p style="margin:0 0 16px;">{ad_block}</p>

    <p style="margin:0 0 12px;">Item revenue dashboard attached with the complete table.</p>
    <p style="margin:0;">
      <a href="{dashboard_url}"
         style="display:inline-block;background:#1A73E8;color:#ffffff;text-decoration:none;
                font-family:Arial,Helvetica,sans-serif;font-size:14px;font-weight:bold;
                padding:12px 28px;border-radius:6px;border:1px solid #1A73E8;">
        {button_label}
      </a>
    </p>
  </div>
</body>
</html>"""

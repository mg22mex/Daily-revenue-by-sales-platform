"""Reporting package: email + hosted dashboard generators."""

from reporting.dashboard import build_dashboard_html, write_dashboard
from reporting.email_report import build_email_html, build_subject
from reporting.models import DailyReport

__all__ = [
    "DailyReport",
    "build_dashboard_html",
    "build_email_html",
    "build_subject",
    "write_dashboard",
]

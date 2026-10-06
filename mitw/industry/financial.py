"""Financial-industry handling.

Banks, insurers, brokers and financial holding companies file different statement forms (the exchanges publish a separate
income-statement feed per form). Membership of those feeds identifies the sub-type from the exchange's own data, not from
company names.  General-corporate models (EV/EBITDA, EV/Sales, P/S) do not apply; this phase only keeps official P/E for reference.
"""
from __future__ import annotations

from dataclasses import replace

from ..contracts.models import Company

FINANCIAL = "FINANCIAL"
FORM_LABEL = {
    "BASI": "銀行業",
    "BD": "證券期貨業",
    "FH": "金控業",
    "INS": "保險業",
}
FINANCIAL_WARNING = "金融業：EV/EBITDA、EV/Sales、P/S 等一般企業估值模型不適用，此處的 P/E 僅供參考，尚未設計金融業專用估值。"


def is_financial(company: Company) -> bool:
    return company.financialForm in FORM_LABEL


def attach_financial_forms(companies: list[Company], forms: dict[str, str]) -> list[Company]:
    return [replace(c, financialForm=forms.get(c.ticker, "GENERAL" if forms else None)) for c in companies]


def special_industry(company: Company) -> str | None:
    return FINANCIAL if is_financial(company) else None

"""Shared low-level helpers for the Wealthsimple modules -
parsing GraphQL amount/quote fields and resolving accounts.
Used by the transport (account), paper ledger and mapping
modules so none of them import each other for these."""

from typing import List

from core.config import WSAccountConfig


def _amount(node) -> float:
    """Amount of a {amount, currency} GraphQL field, 0 when absent."""
    if not node:
        return 0.0
    try:
        return float(node.get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def _amount_opt(node):
    """Amount of a {amount, currency} field, None when absent -
    distinguishes a missing value from a real zero."""
    if not node:
        return None
    try:
        return float(node.get("amount"))
    except (TypeError, ValueError):
        return None


def _quote_price(node) -> float:
    """Price of a quoteV2 field ({price, ...}), 0 when absent."""
    if not node:
        return 0.0
    try:
        return float(node.get("price") or 0)
    except (TypeError, ValueError):
        return 0.0


def effective_accounts(cfg) -> List[WSAccountConfig]:
    accounts = [a for a in cfg.wealthsimple.accounts if a.enabled]
    if accounts:
        return accounts
    return [WSAccountConfig(account_id="", label="default")]


def account_label(acct: WSAccountConfig) -> str:
    return acct.label or acct.account_id or "default"


def resolve_account_id(ws, cfg):
    if cfg.wealthsimple.accounts:
        for acct in cfg.wealthsimple.accounts:
            if acct.account_id and acct.enabled:
                return acct.account_id
    accounts = ws.get_accounts()
    active = [a for a in accounts if a.get("status") == "ACTIVE"]
    pool = active or accounts
    return pool[0]["id"]

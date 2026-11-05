"""Security identity.

The engine keys everything on a ``SecurityId``. In V1 it equals the upper-case
ticker symbol. Strategies speak in tickers; conversion happens only at the
``ctx`` boundary and when writing outputs, so a later mapping table (renames,
reused symbols) changes this module only.
"""

from __future__ import annotations

from typing import NewType

SecurityId = NewType("SecurityId", str)


class SecurityMaster:
    """Maps tickers to security ids and back. Identity mapping in V1."""

    def to_id(self, ticker: str) -> SecurityId:
        if not isinstance(ticker, str) or not ticker.strip():
            raise ValueError(f"invalid ticker: {ticker!r}")
        return SecurityId(ticker.strip().upper())

    def to_ticker(self, security_id: SecurityId) -> str:
        return str(security_id)

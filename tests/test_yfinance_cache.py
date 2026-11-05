"""Price cache: fetch once, refresh incrementally, refetch when Yahoo re-adjusts history."""

import pandas as pd
import pytest

from backtester.data import yfinance_provider as yp
from backtester.data.yfinance_provider import YFinanceProvider, to_adjusted_bars
from backtester.ids import SecurityId
from backtester.testing import nyse_sessions


def raw_frame(start, end, close=100.0, adj_ratio=0.9):
    idx = nyse_sessions(start, end)
    df = pd.DataFrame({
        "open": close, "high": close + 1, "low": close - 1, "close": close,
        "adj_close": close * adj_ratio, "volume": 1000.0, "dividends": 0.0, "splits": 0.0,
    }, index=idx)
    df.index.name = "date"
    return df


class FakeYahoo:
    """Stands in for the network: serves slices of a 'true' history and counts calls."""

    def __init__(self, history):
        self.history = history
        self.calls = []

    def __call__(self, sid, start):
        self.calls.append("full" if start is None else "incremental")
        df = self.history if start is None else self.history[self.history.index >= pd.Timestamp(start)]
        return df.copy()


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setattr(yp, "last_completed_session", lambda now=None: pd.Timestamp("2024-03-28"))
    p = YFinanceProvider(cache_dir=tmp_path, min_refresh_hours=0)
    return p


def test_first_fetch_is_full_then_cached(provider, monkeypatch):
    fake = FakeYahoo(raw_frame("2024-01-02", "2024-03-28"))
    monkeypatch.setattr(provider, "_fetch", fake)
    bars = provider.get_bars(SecurityId("AAA"))
    assert fake.calls == ["full"]
    assert bars.index[-1] == pd.Timestamp("2024-03-28")
    provider.get_bars(SecurityId("AAA"))  # up to date: no network
    assert fake.calls == ["full"]


def test_incremental_refresh_appends(provider, monkeypatch):
    fake = FakeYahoo(raw_frame("2024-01-02", "2024-03-15"))
    monkeypatch.setattr(provider, "_fetch", fake)
    provider.get_bars(SecurityId("AAA"))
    fake.history = raw_frame("2024-01-02", "2024-03-28")
    bars = provider.get_bars(SecurityId("AAA"))
    assert fake.calls == ["full", "incremental"]
    assert bars.index[-1] == pd.Timestamp("2024-03-28")
    assert bars.index.is_unique and bars.index.is_monotonic_increasing


def test_readjusted_history_triggers_full_refetch(provider, monkeypatch):
    fake = FakeYahoo(raw_frame("2024-01-02", "2024-03-15", adj_ratio=0.9))
    monkeypatch.setattr(provider, "_fetch", fake)
    provider.get_bars(SecurityId("AAA"))
    fake.history = raw_frame("2024-01-02", "2024-03-28", adj_ratio=0.88)  # new dividend re-adjusted history
    bars = provider.get_bars(SecurityId("AAA"))
    assert fake.calls == ["full", "incremental", "full"]
    assert bars["close"].iloc[0] == pytest.approx(88.0)


def test_unknown_ticker_is_missing_not_an_error(provider, monkeypatch):
    monkeypatch.setattr(provider, "_fetch", lambda sid, start: None)
    assert provider.get_bars(SecurityId("NOPE")) is None
    assert provider.manifest([SecurityId("NOPE")])["missing_tickers"] == ["NOPE"]


@pytest.mark.parametrize("ticker", ["NYSE/TRN", "BWINA / B", "..\\X"])
def test_a_malformed_ticker_is_missing_and_never_a_path(provider, monkeypatch, ticker):
    # Feeds carry tickers as filings typed them; one with a slash must not reach the file system.
    fake = FakeYahoo(raw_frame("2024-01-02", "2024-03-28"))
    monkeypatch.setattr(provider, "_fetch", fake)
    assert provider.get_bars(SecurityId(ticker)) is None
    assert fake.calls == []
    assert provider.manifest([SecurityId(ticker)])["missing_tickers"] == [ticker]


def test_adjusted_bars_and_raw_close_across_a_split():
    df = raw_frame("2024-03-01", "2024-03-08", close=50.0, adj_ratio=1.0)
    df.loc[pd.Timestamp("2024-03-06"), "splits"] = 2.0  # 2:1 split effective Mar 6
    bars = to_adjusted_bars(df)
    # Yahoo's close is split-adjusted, so raw close before the split is double.
    assert bars.loc["2024-03-05", "close_raw"] == 100.0
    assert bars.loc["2024-03-06", "close_raw"] == 50.0
    assert bars["close"].eq(50.0).all()

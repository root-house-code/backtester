from event_hold import EventHold

from backtester.testing import make_fixture, run_on_fixture

# Sessions start Tue 2024-01-02: session 1 is Wed 01-03, session 2 is Thu 01-04.
PRICES = {"AAA": [10.0] * 30, "BBB": [20.0] * 30}


def signals(*records):
    return {"sample_signals": [dict(zip(("ticker", "available_at", "score", "note"), r)) for r in records]}


def test_a_signal_after_the_close_buys_at_the_next_open():
    fx = make_fixture(prices=PRICES, events=signals(("AAA", "2024-01-03 17:00", 0.9, "x")))
    result = run_on_fixture(EventHold, fx)
    assert result.orders[["session", "ticker", "status"]].values.tolist() == [[2, "AAA", "filled"]]


def test_low_scores_are_rejected_with_a_reason():
    fx = make_fixture(prices=PRICES, events=signals(("BBB", "2024-01-03 17:00", 0.2, "x")))
    result = run_on_fixture(EventHold, fx)
    assert result.orders.empty
    audit = result.signals[result.signals["decision"] == "event"].iloc[0]
    assert (audit["ticker"], audit["outcome"], audit["outcome_reason"]) == ("BBB", "rejected", "score below min_score")


def test_positions_exit_after_hold_days():
    fx = make_fixture(prices=PRICES, events=signals(("AAA", "2024-01-03 17:00", 0.9, "x")))
    trade = run_on_fixture(EventHold, fx, params={"hold_days": 5}).trades.iloc[0]
    assert (trade["session"], trade["exit_reason"], trade["sessions_held"]) == (2, "exit_after", 5)


def test_universe_only_includes_tickers_already_mentioned():
    seen = []

    class Spy(EventHold):
        def on_bar(self, ctx):
            seen.append(list(ctx.universe))

    fx = make_fixture(prices=PRICES, events=signals(("AAA", "2024-01-03 17:00", 0.9, "x"),
                                                    ("BBB", "2024-01-05 17:00", 0.9, "x")))
    run_on_fixture(Spy, fx)
    assert seen[0] == [] and seen[2] == ["AAA"] and seen[4] == ["AAA", "BBB"]

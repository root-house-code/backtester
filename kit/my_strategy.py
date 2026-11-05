from backtester import Param, Strategy


class MyStrategy(Strategy):
    api_version = 1
    name = "My strategy"
    description = "Buy a close above the prior N-session high; exit after a fixed number of sessions."
    requires = ["prices"]
    universe = ["SPY", "QQQ", "IWM"]
    params = {
        "lookback": Param(int, default=20, min=5, max=250, description="Sessions in the prior high"),
        "hold": Param(int, default=10, min=1, max=100, description="Sessions to hold each entry"),
        "size": Param(float, default=0.25, min=0.01, max=0.33, description="Fraction of equity per entry"),
    }

    def on_bar(self, ctx):
        # Decided before today's open; the last row is yesterday's close.
        closes = ctx.history("close", self.p.lookback + 1)
        if len(closes) < self.p.lookback + 1:
            return  # not enough history yet
        for ticker in ctx.universe:
            if ctx.position(ticker).qty > 0:
                continue  # already holding; the exit is scheduled
            prior_high = closes[ticker].iloc[:-1].max()
            if closes[ticker].iloc[-1] > prior_high:
                ctx.order(ticker, pct=self.p.size, at="open", exit_after=self.p.hold, tag="breakout")

from backtester import Strategy


class BuyAndHold(Strategy):
    api_version = 1
    name = "Buy and hold"
    description = "Buy the universe in equal weights at the first open and hold to the end."
    hypothesis = ("The baseline: what you earn by buying the tickers in equal weights on day one and never "
                  "trading again. Other strategies have to beat this to be worth the effort.")
    requires = ["prices"]
    universe = ["SPY"]
    params = {}

    def on_bar(self, ctx):
        # Retry each session until the position exists (e.g. if a ticker has
        # no bar on the first session).
        if ctx.positions:
            return
        weight = 1 / len(ctx.universe)
        ctx.target_weights({t: weight for t in ctx.universe}, at="open")

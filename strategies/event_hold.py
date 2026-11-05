from backtester import Param, Strategy


class EventHold(Strategy):
    api_version = 1
    name = "Event hold"
    description = "Buy at the open after a qualifying signal; hold N sessions."
    hypothesis = ("Worked example: do stocks rise after a signal in the sample feed? Buys 20% of equity at the "
                  "next open when a signal scores 0.5 or more, and sells 10 sessions later.")
    requires = ["prices", "sample_signals"]
    universe = "from_feed:sample_signals"
    params = {
        "hold_days": Param(int, default=10, min=1, max=250, description="Sessions to hold each position"),
        "min_score": Param(float, default=0.5, min=0.0, max=1.0, description="Skip signals scoring below this"),
        "size": Param(float, default=0.2, min=0.01, max=1.0, description="Fraction of equity per position"),
    }

    def on_bar(self, ctx):
        for e in ctx.events("sample_signals"):
            if e.score < self.p.min_score:
                ctx.reject(e, "score below min_score")
                continue
            if ctx.position(e.ticker).qty > 0:
                ctx.reject(e, "already holding")
                continue
            ctx.order(e.ticker, pct=self.p.size, at="open", exit_after=self.p.hold_days, tag=e.id)

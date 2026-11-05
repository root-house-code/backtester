from backtester import Param, Strategy


class MomentumRotation(Strategy):
    api_version = 1
    name = "Momentum rotation"
    description = "Hold the top N tickers by trailing return; rebalance monthly."
    hypothesis = ("Worked example: do recent winners keep winning? Each month, holds the 2 of SPY, QQQ, IWM, "
                  "EFA, TLT and GLD with the best 6-month return, in equal weights.")
    requires = ["prices"]
    universe = ["SPY", "QQQ", "IWM", "EFA", "TLT", "GLD"]
    params = {
        "lookback": Param(int, default=126, min=20, max=252),
        "top_n": Param(int, default=2, min=1, max=6),
    }

    def on_bar(self, ctx):
        if not ctx.is_first_session_of("month"):
            return
        closes = ctx.history("close", self.p.lookback + 1)
        rets = closes.iloc[-1] / closes.iloc[0] - 1
        picks = rets.nlargest(self.p.top_n).index
        ctx.target_weights({t: 1 / self.p.top_n for t in picks}, at="open")

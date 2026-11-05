"""Instructions: step-by-step guide to using Backtester."""

import streamlit as st

# Keep the text to a readable line length on wide screens.
page, _ = st.columns([3, 1])

page.title("Instructions")
page.caption("From a trading idea to a result you can trust, one step at a time.")

# ---------------------------------------------------------------------------
page.header("1. Start the app", divider="gray")
page.markdown("""
1. Open a terminal in the project folder.
2. The first time only, install it:
""")
page.code("python -m venv .venv\n.venv\\Scripts\\python -m pip install -e \".[dev]\"", language=None)
page.markdown("3. Start the app. It opens in your browser:")
page.code(".venv\\Scripts\\backtest ui", language=None)

# ---------------------------------------------------------------------------
page.header("2. Write a strategy", divider="gray")
page.markdown("""
A strategy is one Python file in the `strategies/` folder. The easiest way to write one is to
describe your idea to Claude.

1. Go to **Strategies** and click **Download the authoring kit**. Unzip it.
2. Give Claude `STRATEGY_API.md` from the kit, along with your idea in plain words. Say which
   tickers to trade, when to buy, when to sell and how much to put in each position.
3. Ask Claude for a test file too (`test_<name>.py`). Tests check the rules on small, made-up
   price data, so you know the strategy does what you described.
4. Save both files in `strategies/`.

`strategies/momentum_rotation.py` and `strategies/event_hold.py` are worked examples.

**With Claude Code** open in the project folder, type `/new-strategy` followed by your idea instead.
Claude agrees a short brief with you, writes the tests and the strategy, validates it, tunes it
on in-sample data only and asks before the one out-of-sample run. It keeps a research log at
`strategies/<name>.md`.
""")

# ---------------------------------------------------------------------------
page.header("3. Validate it", divider="gray")
page.markdown("""
1. Go to **Strategies**. Your file appears in the table with its **Validation** status.
2. Choose it under **Report** to read the full report.
3. If anything failed, copy the report and paste it back to Claude. Each problem lists the line,
   the rule it broke and how to fix it.
4. Save the fixed file and click **Revalidate**. Repeat until it passes.

A strategy has to pass validation before it can run.
""")

# ---------------------------------------------------------------------------
page.header("4. Run a backtest", divider="gray")
page.markdown("""
1. Go to **Run** and choose your **Strategy**.
2. Set the **Start** and **End** dates.
3. Check the **Tickers**. They default to the strategy's own list.
4. Set the **Capital** and any strategy settings shown below it.
5. Open **Advanced** and set an **In-sample end** date (see step 7). You can also add costs
   here: commission, slippage and a liquidity cap.
6. Click **Run**. Prices download the first time, so this can take a minute. When it
   finishes, the Results page opens.
""")

# ---------------------------------------------------------------------------
page.header("5. Read the results", divider="gray")
page.markdown("""
1. **Results** shows the run chosen in the **Run** box at the top of the page. Use that box to
   switch between saved runs. Each one is labelled with when it ran, the strategy, any
   parameters you changed, its dates and its total return. **Sort by**, next to it, lists the runs
   best first by total return, CAGR, Sharpe, max drawdown or another headline number, and adds
   that number to each label.
2. Hover over the **?** next to each headline number for what it means and how it is measured.
3. Below are the equity curve against the benchmark, the drawdown chart, monthly returns and
   **All metrics**.
4. If you set an in-sample end, switch between **Full period**, **In-sample** and
   **Out-of-sample** at the top.
""")

# ---------------------------------------------------------------------------
page.header("6. Check the trades", divider="gray")
page.markdown("""
1. Go to **Audit**.
2. On the **Trades** tab, select a trade to see it on a price chart with its entry, exit and
   any stop or target.
3. The **Signals** tab lists every order, every rejected order and every feed event, with what
   happened to each. Use it to answer "why didn't it buy that?"
4. Click **Export CSV** to take either list into a spreadsheet.
""")

# ---------------------------------------------------------------------------
page.header("7. Avoid fooling yourself", divider="gray")
page.markdown("""
Try enough variants and one will look great by luck. To guard against that:

1. **Pick an in-sample end date before your first run, and don't move it.** Data before it is
   for building and tuning. Data after it is the test.
2. **Tune on the in-sample results only.** Don't look at out-of-sample numbers while you are
   still changing the strategy.
3. **Judge the final version on out-of-sample.** If it holds up there, the idea may be real.
4. For a stronger check, set **Walk-forward** in Advanced (for example `3y,1y`). It repeats the
   test over rolling windows to show whether the edge lasts.
5. Check the **95% confidence interval** for the mean trade return, in the line under the
   headline numbers on Results. If it includes zero, the result could be luck.
6. Keep an eye on the **Runs** count on the Strategies page. The more variants you try, the more
   skeptical you should be of the best one.
""")

# ---------------------------------------------------------------------------
page.header("8. Compare versions", divider="gray")
page.markdown("""
1. Go to **Compare** and pick two to four runs.
2. It shows out-of-sample results by default when the runs have an in-sample end.
3. Read the metrics side by side, the overlaid growth curves, and **Settings that differ** to
   see exactly what changed between them.
""")

# ---------------------------------------------------------------------------
page.header("9. Add your own data (optional)", divider="gray")
page.markdown("""
Strategies can use more than prices: earnings dates, filings, your own signals, sector labels.

1. Go to **Data** and open **Add a feed from a file**.
2. Upload a CSV or Parquet file. It needs a ticker column and a column saying **when each
   record became known**.
3. Give the feed a name, choose its columns and click **Check and add**. Fix anything the
   report flags.
4. Tell Claude the feed's name and columns when you describe your strategy. The strategy only
   sees each record from the next market open after it became known, so it can't trade on
   news before it happened.

The **Feeds** table on the same page shows every feed and its checks, and **Price cache** shows
which tickers have prices downloaded.
""")

# ---------------------------------------------------------------------------
page.header("Using the command line instead", divider="gray")
page.markdown("Everything above also works from a terminal:")
page.code(
    ".venv\\Scripts\\backtest kit                                  # write the authoring kit to kit/\n"
    ".venv\\Scripts\\backtest validate strategies/my_strategy.py   # validation report\n"
    ".venv\\Scripts\\backtest run strategies/my_strategy.py --start 2015-01-01 --end 2024-12-31 "
    "--in-sample-end 2020-12-31\n"
    ".venv\\Scripts\\backtest feeds                                # list feeds and check their data",
    language=None,
)
page.markdown("Each run is saved to its own folder under `runs/` and appears in the app.")

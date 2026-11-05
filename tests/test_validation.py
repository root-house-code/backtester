"""The strategy validation pipeline: each stage catches its class of mistake and
reports it with the file, line, rule and a fix hint."""

import textwrap

import pytest
from helpers import STRATEGIES

from backtester.cli import main as cli_main
from backtester.errors import StrategyError
from backtester.kit import TEMPLATE_STRATEGY, TEMPLATE_TEST
from backtester.run import run_backtest
from backtester.validation import FAILED, PASSED, SKIPPED, validate

HEADER = '''\
from backtester import Param, Strategy


class S(Strategy):
    api_version = 1
    name = "S"
    description = "Test strategy."
    requires = ["prices"]
    universe = ["AAA", "BBB"]
    params = {}
'''


def write(tmp_path, name, body, header=True, in_class=True):
    """Write HEADER plus ``body``; the body goes inside class S unless ``in_class`` is False."""
    path = tmp_path / name
    body = textwrap.dedent(body)
    text = (HEADER if header else "") + (textwrap.indent(body, "    ") if in_class else body)
    path.write_text(text, encoding="utf-8")
    return path


def check(path, tmp_path, quick=False):
    return validate(path, cache_dir=tmp_path / "empty_cache", quick=quick)


def stage(report, name):
    return next(s for s in report.stages if s.name == name)


def rules(report):
    return [(i.rule, i.line) for i in report.issues]


# --- passing files ----------------------------------------------------------------

@pytest.mark.parametrize("name", ["momentum_rotation.py", "buy_and_hold.py"])
def test_reference_strategies_pass(name, tmp_path):
    report = check(STRATEGIES / name, tmp_path)
    assert report.ok, report.text()
    assert [s.status for s in report.stages][:2] == [PASSED, PASSED]
    assert stage(report, "determinism").status == PASSED


def test_reference_strategy_tests_run(tmp_path):
    report = check(STRATEGIES / "momentum_rotation.py", tmp_path)
    assert stage(report, "tests").status == PASSED
    assert "2 passed" in stage(report, "tests").detail


def test_template_and_its_tests_pass(tmp_path):
    (tmp_path / "my_strategy.py").write_text(TEMPLATE_STRATEGY)
    (tmp_path / "test_my_strategy.py").write_text(TEMPLATE_TEST)
    report = check(tmp_path / "my_strategy.py", tmp_path)
    assert report.ok, report.text()
    assert stage(report, "tests").status == PASSED


def test_private_self_attributes_and_super_init_are_allowed(tmp_path):
    path = write(tmp_path, "ok.py", '''
        def __init__(self, params=None):
            super().__init__(params)
            self._seen = set()

        def on_bar(self, ctx):
            self._seen.add(ctx.today)
    ''')
    assert check(path, tmp_path, quick=True).failed_stage is None


# --- stage 1: static --------------------------------------------------------------

STATIC_CASES = {
    "import requests\n": ("import-not-allowed", 1),
    "import os\n": ("import-not-allowed", 1),
    "from backtester.engine import Engine\n": ("import-not-allowed", 1),
    "import numpy.random\n": ("unseeded-random", 1),
    "from numpy import random\n": ("unseeded-random", 1),
}


@pytest.mark.parametrize("line,expected", STATIC_CASES.items())
def test_banned_imports(line, expected, tmp_path):
    path = tmp_path / "s.py"
    path.write_text(line + HEADER + "\n    def on_bar(self, ctx):\n        pass\n")
    report = check(path, tmp_path, quick=True)
    assert report.failed_stage.name == "static"
    assert rules(report) == [expected]


BODY_CASES = {
    "x = open('prices.csv')": "banned-call",
    "x = eval('1 + 1')": "banned-call",
    "x = getattr(ctx, 'cash')": "banned-call",
    "e = ctx._engine": "private-access",
    "c = ctx.__class__": "private-access",
    "x = np.random.normal()": "unseeded-random",
    "x = np.load('a.npy')": "file-access",
    "x = pd.read_csv('a.csv')": "file-access",
    "ctx.history('close', 5).to_csv('out.csv')": "file-access",
    "x = pd.Timestamp.now()": "wall-clock",
    "x = datetime.date.today()": "wall-clock",
}


@pytest.mark.parametrize("code,rule", BODY_CASES.items())
def test_banned_calls_and_attributes(code, rule, tmp_path):
    path = tmp_path / "s.py"
    path.write_text("import datetime\nimport numpy as np\nimport pandas as pd\n" + HEADER
                    + f"\n    def on_bar(self, ctx):\n        {code}\n")
    report = check(path, tmp_path, quick=True)
    assert report.failed_stage.name == "static"
    issue = report.issues[0]
    assert issue.rule == rule
    assert issue.line == 16 and issue.code.strip() == code
    assert issue.hint


def test_syntax_error_points_at_the_line(tmp_path):
    path = write(tmp_path, "s.py", "\n    def on_bar(self, ctx)\n        pass\n")
    report = check(path, tmp_path, quick=True)
    assert rules(report) == [("syntax-error", 12)]


def test_needs_exactly_one_strategy_class_with_a_supported_api_version(tmp_path):
    path = write(tmp_path, "s.py", '''
    class Other(Strategy):
        api_version = 2
    ''', in_class=False)
    report = check(path, tmp_path, quick=True)
    assert rules(report) == [("strategy-class", 12), ("api-version", 13)]

    path.write_text("from backtester import Strategy\n\nx = 1\n")
    assert rules(check(path, tmp_path, quick=True)) == [("strategy-class", None)]


def test_reports_every_static_issue_at_once(tmp_path):
    path = write(tmp_path, "s.py", '''
        def on_bar(self, ctx):
            x = open("a")
            y = ctx._engine
            z = eval("1")
    ''')
    report = check(path, tmp_path, quick=True)
    assert rules(report) == [("banned-call", 13), ("private-access", 14), ("banned-call", 15)]


# --- stage 2: declarations -------------------------------------------------------

def test_declaration_problems_are_all_reported_with_lines(tmp_path):
    path = tmp_path / "s.py"
    path.write_text(textwrap.dedent('''\
        from backtester import Param, Strategy


        class S(Strategy):
            api_version = 1
            name = "S"
            description = ""
            requires = ["prices", "insider_filings"]
            universe = []
            params = {}

            def on_bar(self, ctx, extra):
                pass
    '''))
    report = check(path, tmp_path, quick=True)
    assert report.failed_stage.name == "declarations"
    assert rules(report) == [("declaration", 7), ("requires", 8), ("universe", 9), ("hook-signature", 12)]


def test_param_default_outside_its_range(tmp_path):
    path = tmp_path / "s.py"
    path.write_text(HEADER.replace("params = {}", 'params = {\n        "top_n": Param(int, default=10, min=1, max=6),\n    }')
                    + "\n    def on_bar(self, ctx):\n        pass\n")
    report = check(path, tmp_path, quick=True)
    assert rules(report) == [("param-invalid", 11)]
    assert "above max 6" in report.issues[0].message


def test_missing_on_bar(tmp_path):
    path = write(tmp_path, "s.py", "")
    assert rules(check(path, tmp_path, quick=True)) == [("on-bar", 4)]


def test_import_errors_point_at_the_line(tmp_path):
    path = write(tmp_path, "s.py", '''
        threshold = 1 / 0

        def on_bar(self, ctx):
            pass
    ''')
    report = check(path, tmp_path, quick=True)
    assert rules(report) == [("import-error", 12)]
    assert "ZeroDivisionError" in report.issues[0].message


# --- stage 3: strategy tests --------------------------------------------------------

def test_failing_strategy_tests_are_reported_with_output(tmp_path):
    path = write(tmp_path, "never.py", "\n    def on_bar(self, ctx):\n        pass\n")
    (tmp_path / "test_never.py").write_text(textwrap.dedent('''\
        from never import S
        from backtester.testing import make_fixture, run_on_fixture


        def test_buys_on_session_2():
            result = run_on_fixture(S, make_fixture(prices={"AAA": [10.0] * 5}))
            assert result.orders["session"].tolist() == [2]
    '''))
    report = check(path, tmp_path)
    assert report.failed_stage.name == "tests"
    issue = report.issues[0]
    assert issue.rule == "tests-failed" and issue.file == "test_never.py" and issue.line == 7
    assert "test_buys_on_session_2" in issue.message
    assert "assert [] == [2]" in issue.detail


def test_no_test_file_skips_the_stage(tmp_path):
    path = write(tmp_path, "s.py", "\n    def on_bar(self, ctx):\n        pass\n")
    report = check(path, tmp_path)
    assert stage(report, "tests").status == SKIPPED
    assert report.ok


# --- stages 4 and 5: dry run and determinism -----------------------------------------

def test_lookahead_in_the_dry_run(tmp_path):
    path = write(tmp_path, "s.py", '''
        def on_bar(self, ctx):
            bar = ctx.bar("AAA", ctx.today)
    ''')
    report = check(path, tmp_path)
    assert report.failed_stage.name == "dry run"
    issue = report.issues[0]
    assert (issue.rule, issue.line) == ("exception", 13)
    assert issue.message.startswith("LookaheadError")
    assert "ctx.history" in issue.hint


def test_malformed_order_in_the_dry_run(tmp_path):
    path = write(tmp_path, "s.py", '''
        def on_bar(self, ctx):
            ctx.order("AAA", pct=1.5)
    ''')
    report = check(path, tmp_path)
    assert rules(report) == [("exception", 13)]
    assert "OrderError" in report.issues[0].message


def test_order_outside_the_universe(tmp_path):
    path = write(tmp_path, "s.py", '''
        def on_bar(self, ctx):
            if not ctx.positions:
                ctx.order("TSLA", pct=0.5)
    ''')
    report = check(path, tmp_path)
    assert rules(report) == [("order-ticker", 14)]


def test_nondeterminism_is_caught(tmp_path):
    path = write(tmp_path, "s.py", '''
        def on_bar(self, ctx):
            # str hashes differ between processes, so this pick does too.
            pick = sorted(ctx.universe, key=hash)[0]
            ctx.target_weights({pick: 0.9})
    ''')
    report = check(path, tmp_path)
    assert stage(report, "dry run").status == PASSED
    assert report.failed_stage.name == "determinism"
    assert report.issues[0].rule == "nondeterministic"


# --- the report ------------------------------------------------------------------------

def test_report_text_is_self_contained(tmp_path):
    path = write(tmp_path, "my_idea.py", '''
        def on_bar(self, ctx):
            e = ctx._engine
    ''')
    text = check(path, tmp_path, quick=True).text()
    assert text.startswith("Validation FAILED: my_idea.py\nStage 1 of 5 (static) failed with 1 issue.")
    assert "[1] my_idea.py:13  private-access" in text
    assert "Code:  e = ctx._engine" in text
    assert "Error: access to private attribute `._engine`" in text
    assert "Fix:   " in text
    assert "STRATEGY_API.md" in text


def test_a_broken_file_is_fixed_in_one_round_from_its_report(tmp_path):
    """The done-when for M3: every issue in the failing stage is reported, so
    fixing what the report lists produces a file that passes."""
    broken = write(tmp_path, "idea.py", '''
        def on_bar(self, ctx):
            noise = np.random.normal()
            if noise > 0 and ctx._engine is not None:
                ctx.order("AAA", pct=0.5)
    ''')
    broken.write_text("import numpy as np\n" + broken.read_text())
    report = check(broken, tmp_path)
    assert {i.rule for i in report.issues} == {"unseeded-random", "private-access"}
    # Apply the two fix hints: ctx.rng instead of np.random, and no private access.
    fixed = broken.read_text().replace("np.random.normal()", "ctx.rng.normal()").replace(
        " and ctx._engine is not None", "")
    broken.write_text(fixed)
    assert check(broken, tmp_path).ok


# --- other entry points -------------------------------------------------------------------

def test_runs_refuse_files_that_fail_static_checks(tmp_path):
    path = write(tmp_path, "s.py", '''
        def on_bar(self, ctx):
            e = ctx._engine
    ''')
    with pytest.raises(StrategyError, match="private-access"):
        run_backtest(path, {"start": "2024-01-02", "end": "2024-02-01"})


def test_cli_validate_exit_codes(tmp_path, capsys):
    good = write(tmp_path, "good.py", "\n    def on_bar(self, ctx):\n        pass\n")
    bad = write(tmp_path, "bad.py", "\n    def on_bar(self, ctx):\n        x = open('a')\n")
    assert cli_main(["validate", "--quick", str(good)]) == 0
    assert cli_main(["validate", "--quick", str(good), str(bad)]) == 1
    out = capsys.readouterr().out
    assert "Validation passed: good.py" in out and "Validation FAILED: bad.py" in out
    assert stage(check(bad, tmp_path, quick=True), "static").status == FAILED

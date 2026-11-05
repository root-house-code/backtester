"""Strategy validation pipeline.

Stages run in order and stop at the first one that fails:

1. static        Parses; exactly one Strategy subclass; supported api_version;
                 imports on the allowlist; no file, network, subprocess, eval,
                 wall-clock or unseeded-random use; no private attribute access.
2. declarations  The file imports; name, description, requires, universe and
                 params are well-formed; on_bar is implemented.
3. tests         Runs the strategy's own ``test_<name>.py``, if present.
4. dry run       60 sessions on up to 5 tickers (cached prices, or synthetic
                 prices when the cache does not have them); no exceptions;
                 every order is for a ticker in the universe.
5. determinism   The dry run again, in a process with a different hash seed;
                 the output hash must match.

Every issue names the file, line, rule broken and a one-line fix hint, and
``ValidationReport.text()`` formats them as a report that can be pasted
straight back to whoever wrote the strategy.

The static checks guard against mistakes, not malice.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path

# --- rules -------------------------------------------------------------------

#: Modules a strategy file may import (top-level name). ``backtester`` only as
#: ``from backtester import ...``.
ALLOWED_IMPORTS = ("numpy", "pandas", "math", "statistics", "datetime", "dataclasses", "typing")
ALWAYS_ALLOWED = ("__future__",)

#: Built-in functions a strategy may not call.
BANNED_CALLS = {
    "open": "file access",
    "eval": "dynamic code execution",
    "exec": "dynamic code execution",
    "compile": "dynamic code execution",
    "__import__": "dynamic imports",
    "input": "reading from the console",
    "breakpoint": "the debugger",
    "globals": "reaching outside the strategy",
    "locals": "reaching outside the strategy",
    "vars": "reaching outside the strategy",
    "getattr": "dynamic attribute access",
    "setattr": "dynamic attribute access",
    "delattr": "dynamic attribute access",
    "exit": "stopping the process",
    "quit": "stopping the process",
}

#: numpy functions that read or write files.
NUMPY_FILE_FUNCS = {"load", "save", "savez", "savez_compressed", "loadtxt", "savetxt",
                    "genfromtxt", "fromfile", "memmap", "DataSource"}
#: DataFrame/Series/array methods that write files.
FILE_METHODS = {"to_csv", "to_parquet", "to_pickle", "to_excel", "to_json", "to_hdf", "to_sql",
                "to_feather", "to_stata", "to_clipboard", "to_orc", "tofile"}
#: Calls that read the wall clock (``datetime.now()``, ``pd.Timestamp.today()``, ...).
CLOCK_METHODS = {"now", "utcnow", "today"}

DRY_RUN_SESSIONS = 60
DRY_RUN_TICKERS = 5
DRY_RUN_TIMEOUT = 180
TESTS_TIMEOUT = 300

STAGES = (
    ("static", "Parses; exactly one Strategy subclass; supported api_version; imports on the "
               "allowlist; no file, network, subprocess, eval, clock or unseeded-random use; "
               "no private attribute access"),
    ("declarations", "Imports cleanly; name and description present; every param typed with a "
                     "default inside its range; requires lists only known feeds; universe resolves; "
                     "on_bar implemented"),
    ("tests", "Runs the strategy's own test_<name>.py fixture tests, if present"),
    ("dry run", f"{DRY_RUN_SESSIONS} sessions on up to {DRY_RUN_TICKERS} tickers from cached data; "
                "no exceptions; every order is for a ticker in the universe"),
    ("determinism", "Dry run again with a different hash seed; output hashes must match"),
)
STAGE_NAMES = [name for name, _ in STAGES]

PASSED, FAILED, SKIPPED, NOT_RUN = "passed", "failed", "skipped", "not run"


# --- results -----------------------------------------------------------------

@dataclass
class Issue:
    """One problem found in a strategy file."""

    rule: str
    message: str
    hint: str
    file: str
    line: int | None = None
    code: str | None = None      # the offending source line
    detail: str | None = None    # extra output, e.g. a failing test's traceback

    def text(self, k: int) -> str:
        where = self.file if self.line is None else f"{self.file}:{self.line}"
        out = [f"[{k}] {where}  {self.rule}"]
        if self.code:
            out.append(f"    Code:  {self.code.strip()}")
        out.append(f"    Error: {self.message}")
        out.append(f"    Fix:   {self.hint}")
        if self.detail:
            out.append("    Output:")
            out.extend(f"      {ln}" for ln in self.detail.rstrip().splitlines())
        return "\n".join(out)


@dataclass
class StageResult:
    name: str
    status: str = NOT_RUN
    detail: str = ""
    issues: list[Issue] = field(default_factory=list)


@dataclass
class ValidationReport:
    file: str
    stages: list[StageResult]
    strategy_name: str | None = None
    api_version: int | None = None

    @property
    def ok(self) -> bool:
        return all(s.status in (PASSED, SKIPPED) for s in self.stages)

    @property
    def failed_stage(self) -> StageResult | None:
        return next((s for s in self.stages if s.status == FAILED), None)

    @property
    def issues(self) -> list[Issue]:
        return [i for s in self.stages for i in s.issues]

    def status_label(self) -> str:
        failed = self.failed_stage
        if failed is not None:
            return f"Failed: {failed.name}"
        if any(s.status == NOT_RUN for s in self.stages):
            return "Partly checked"
        return "Passed"

    def text(self) -> str:
        """The report as plain text, ready to paste back to the strategy's author."""
        width = max(len(n) for n in STAGE_NAMES)
        failed = self.failed_stage
        lines = [f"Validation {'passed' if failed is None else 'FAILED'}: {self.file}"]
        if failed is not None:
            k = STAGE_NAMES.index(failed.name) + 1
            lines.append(f"Stage {k} of {len(STAGES)} ({failed.name}) failed with "
                         f"{len(failed.issues)} issue{'s' if len(failed.issues) != 1 else ''}.")
        lines.append("")
        for s in self.stages:
            detail = f"  ({s.detail})" if s.detail else ""
            lines.append(f"  {s.name:<{width}}  {s.status}{detail}")
        if failed is not None:
            lines.append("")
            lines.extend(issue.text(k) + "\n" for k, issue in enumerate(failed.issues, start=1))
            lines.append("The strategy API and the rules for strategy files are in STRATEGY_API.md.")
        return "\n".join(lines).rstrip() + "\n"

    def to_dict(self) -> dict:
        return asdict(self)


# --- helpers -----------------------------------------------------------------

def _src_line(lines: list[str], lineno: int | None) -> str | None:
    if lineno is None or not 1 <= lineno <= len(lines):
        return None
    return lines[lineno - 1]


def _dotted(node: ast.AST) -> str | None:
    """``np.random.default_rng`` -> "np.random.default_rng"; None for anything else."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def strategy_classes(tree: ast.Module) -> list[ast.ClassDef]:
    """Top-level classes that subclass ``Strategy`` (by name)."""
    out = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", None)
                if name == "Strategy":
                    out.append(node)
                    break
    return out


def _class_attr_lines(cls_node: ast.ClassDef) -> dict[str, int]:
    out = {}
    for stmt in cls_node.body:
        targets = []
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
        elif isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[stmt.name] = stmt.lineno
        for t in targets:
            if isinstance(t, ast.Name):
                out[t.id] = stmt.lineno
    return out


def _param_lines(cls_node: ast.ClassDef) -> dict[str, int]:
    for stmt in cls_node.body:
        if (isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "params" for t in stmt.targets)
                and isinstance(stmt.value, ast.Dict)):
            return {k.value: k.lineno for k in stmt.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    return {}


# --- stage 1: static ---------------------------------------------------------

class _StaticVisitor(ast.NodeVisitor):
    def __init__(self, add):
        self.add = add
        self.aliases: dict[str, str] = {}   # local name -> module path ("np" -> "numpy")

    # imports
    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top == "backtester":
                self.add(node, "import-not-allowed", f"`import {alias.name}` is not allowed",
                         "Import what you need by name: `from backtester import Strategy, Param`.")
            elif top not in ALLOWED_IMPORTS + ALWAYS_ALLOWED:
                self._bad_import(node, alias.name)
            elif alias.name.startswith("numpy.random"):
                self._random(node)
            else:
                self.aliases[alias.asname or top] = alias.name if alias.asname else top
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        mod = node.module or ""
        if node.level:
            self.add(node, "import-not-allowed", "relative imports are not allowed",
                     "Put everything the strategy needs in this one file.")
            return
        top = mod.split(".")[0]
        if top == "backtester":
            if mod != "backtester":
                self.add(node, "import-not-allowed", f"`from {mod} import ...` is not allowed",
                         "Import only from the package itself: `from backtester import Strategy, Param`.")
        elif top not in ALLOWED_IMPORTS + ALWAYS_ALLOWED:
            self._bad_import(node, mod)
        elif mod.startswith("numpy.random") or (mod == "numpy" and any(a.name == "random" for a in node.names)):
            self._random(node)
        else:
            for alias in node.names:
                self.aliases[alias.asname or alias.name] = f"{mod}.{alias.name}"
        self.generic_visit(node)

    def _bad_import(self, node, name):
        self.add(node, "import-not-allowed", f"module `{name}` is not on the import allowlist",
                 f"Remove it. Strategy files may import only {', '.join(ALLOWED_IMPORTS)}, and "
                 "`from backtester import ...`. All market data comes through ctx.")

    def _random(self, node):
        self.add(node, "unseeded-random", "numpy.random is not allowed",
                 "Use ctx.rng (a seeded numpy Generator), e.g. `ctx.rng.random()`, so runs are reproducible.")

    def _resolve(self, dotted: str) -> str:
        head, _, rest = dotted.partition(".")
        base = self.aliases.get(head, head)
        return f"{base}.{rest}" if rest else base

    # calls and attributes
    def visit_Call(self, node: ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id in BANNED_CALLS:
            what = BANNED_CALLS[func.id]
            hint = {
                "open": "Strategies may not read or write files. Market data comes through ctx.",
                "getattr": "Access attributes directly by name (e.g. `self.p.lookback`).",
                "setattr": "Store state in ctx.state or on self with a normal assignment.",
                "delattr": "Store state in ctx.state and remove keys with `del ctx.state[key]`.",
            }.get(func.id, "Remove this call; it is not allowed in strategy files.")
            self.add(node, "banned-call", f"`{func.id}()` is not allowed ({what})", hint)
        dotted = _dotted(func)
        if dotted:
            full = self._resolve(dotted)
            last = full.rsplit(".", 1)[-1]
            if full.startswith("numpy.") and last in NUMPY_FILE_FUNCS:
                self.add(node, "file-access", f"`{dotted}()` reads or writes files",
                         "Strategies may not read or write files. Market data comes through ctx.")
            elif full.startswith("pandas.") and last.startswith("read_"):
                self.add(node, "file-access", f"`{dotted}()` reads files or the network",
                         "Strategies may not load data themselves. Use ctx.history, ctx.price and ctx.bar.")
        if isinstance(func, ast.Attribute):
            if func.attr in FILE_METHODS:
                self.add(node, "file-access", f"`.{func.attr}()` writes a file",
                         "Strategies may not write files. Use ctx.log(...) to record values in the run log.")
            elif func.attr in CLOCK_METHODS and not (isinstance(func.value, ast.Name) and func.value.id == "ctx"):
                self.add(node, "wall-clock", f"`.{func.attr}()` reads the computer's clock",
                         "Use ctx.today, the current session date, so results do not depend on when the run happens.")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        dotted = _dotted(node)
        if dotted and self._resolve(dotted) == "numpy.random":
            self._random(node)
        attr = node.attr
        if attr.startswith("_"):
            is_self = isinstance(node.value, ast.Name) and node.value.id == "self"
            is_dunder = attr.startswith("__") and attr.endswith("__")
            super_init = (attr == "__init__" and isinstance(node.value, ast.Call)
                          and isinstance(node.value.func, ast.Name) and node.value.func.id == "super")
            if not super_init and (is_dunder or not is_self):
                self.add(node, "private-access", f"access to private attribute `.{attr}`",
                         "Use only the public API (ctx, self.p and your own attributes on self). "
                         "Private attributes are engine internals and can bypass lookahead protection.")
        self.generic_visit(node)


def static_check(source: str, filename: str) -> tuple[list[Issue], int | None, str | None]:
    """Stage 1. Returns (issues, api_version literal or None, class name or None)."""
    lines = source.splitlines()
    issues: list[Issue] = []

    def add(node, rule, message, hint):
        lineno = getattr(node, "lineno", None)
        issues.append(Issue(rule, message, hint, filename, lineno, _src_line(lines, lineno)))

    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        issues.append(Issue("syntax-error", f"{exc.msg}", "Fix the Python syntax at this line.",
                            filename, exc.lineno, exc.text.rstrip("\n") if exc.text else _src_line(lines, exc.lineno)))
        return issues, None, None

    _StaticVisitor(add).visit(tree)

    from .strategy import SUPPORTED_API_VERSIONS
    supported = sorted(SUPPORTED_API_VERSIONS)
    classes = strategy_classes(tree)
    api_version = cls_name = None
    if not classes:
        issues.append(Issue("strategy-class", "no Strategy subclass found",
                            "Define one class that subclasses Strategy: `class MyStrategy(Strategy):`.",
                            filename, None))
    elif len(classes) > 1:
        names = ", ".join(c.name for c in classes)
        issues.append(Issue("strategy-class", f"found {len(classes)} Strategy subclasses ({names}); expected exactly one",
                            "Keep one Strategy subclass per file; move the others to their own files.",
                            filename, classes[1].lineno, _src_line(lines, classes[1].lineno)))
    else:
        cls_name = classes[0].name
    for cls in classes:
        node = next((s for s in cls.body if isinstance(s, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == "api_version" for t in s.targets)), None)
        if node is None:
            issues.append(Issue("api-version", f"{cls.name}: api_version is not declared",
                                f"Add `api_version = {supported[-1]}` to the class body.",
                                filename, cls.lineno, _src_line(lines, cls.lineno)))
        elif not (isinstance(node.value, ast.Constant) and node.value.value in SUPPORTED_API_VERSIONS):
            shown = ast.unparse(node.value)
            issues.append(Issue("api-version", f"api_version {shown} is not supported (supported: {supported})",
                                f"Set `api_version = {supported[-1]}`.",
                                filename, node.lineno, _src_line(lines, node.lineno)))
        elif len(classes) == 1:
            api_version = node.value.value
    issues.sort(key=lambda i: (i.line is None, i.line or 0))
    return issues, api_version, cls_name


# --- stage 2: declarations ---------------------------------------------------

def _strategy_frame_line(tb, path: Path) -> int | None:
    """Deepest traceback line inside the strategy file."""
    target = str(path.resolve()).lower()
    line = None
    for frame in traceback.extract_tb(tb):
        if str(Path(frame.filename).resolve()).lower() == target:
            line = frame.lineno
    return line


def _feed_issues(requires, feeds, make) -> list[Issue]:
    """Every required feed exists, and file feeds pass their data checks."""
    from .errors import BacktesterError
    if feeds is None:
        return []
    try:
        names = set(feeds.names())
    except BacktesterError as exc:
        return [make("feed-registry", str(exc), "Fix the feed folder or plug-in named in the error.")]
    issues = []
    unknown = [r for r in requires if r != "prices" and r not in names]
    if unknown:
        issues.append(make("requires", f"unknown feeds {unknown} (known: {['prices', *sorted(names)]})",
                           "Use the name of a feed in data/feeds/ or feeds/, or a built-in "
                           "(prices, shares_outstanding, market_cap)."))
    for name in requires:
        if name in names and feeds.origin(name) == "file":
            try:
                data = feeds.load(name, check=False)
            except BacktesterError as exc:
                issues.append(make("feed-invalid", f"feed {name!r}: {exc}",
                                   f"Fix data/feeds/{name}/ (check it with `backtest feeds`)."))
                continue
            if not data.validation.ok:
                issues.append(make("feed-invalid", f"feed {name!r} fails its checks: "
                                   + "; ".join(data.validation.errors),
                                   f"Fix the files in data/feeds/{name}/ or its feed.yaml "
                                   "(check it with `backtest feeds`)."))
    return issues


def check_declarations(path: Path, source: str, universes_dir: Path | None = None, feeds=None):
    """Stage 2. Returns (issues, strategy class or None)."""
    from .config import resolve_universe, universe_feed
    from .errors import BacktesterError
    from .strategy import Param, Strategy, load_strategy_file

    filename = path.name
    lines = source.splitlines()
    tree = ast.parse(source)
    cls_node = strategy_classes(tree)[0]
    attr_lines = _class_attr_lines(cls_node)
    param_lines = _param_lines(cls_node)

    def issue(rule, message, hint, line=None):
        line = line if line is not None else cls_node.lineno
        return Issue(rule, message, hint, filename, line, _src_line(lines, line))

    try:
        cls = load_strategy_file(path)
    except BacktesterError as exc:
        cause = exc.__cause__
        if cause is None:
            # load_strategy_file found the class but check_strategy_class rejected it;
            # fall through to the detailed checks below using the module's class.
            cls = _import_class_unchecked(path)
            if cls is None:
                return [issue("import-error", str(exc), "Fix the error so the file imports cleanly.")], None
        else:
            line = _strategy_frame_line(cause.__traceback__, path)
            msg = str(cause)
            hint = "Fix the error so the file imports cleanly."
            if "param" in msg and ("min" in msg or "max" in msg or "valid" in msg or "one of" in msg):
                rule, hint = "param-invalid", "Make the default a valid value of the declared type, inside [min, max] and in choices."
            elif "Param type" in msg:
                rule, hint = "param-invalid", "Use one of int, float, bool or str as the Param type."
            else:
                rule = "import-error"
            return [issue(rule, f"{type(cause).__name__}: {msg}", hint, line)], None

    issues = []
    for attr in ("name", "description"):
        value = getattr(cls, attr, None)
        if not isinstance(value, str) or not value.strip():
            issues.append(issue("declaration", f"{attr} is missing or empty",
                                f"Add `{attr} = \"...\"` to the class body (shown in the strategy library).",
                                attr_lines.get(attr)))
    if not isinstance(getattr(cls, "hypothesis", ""), str):
        issues.append(issue("declaration", "hypothesis must be a string",
                            'Write `hypothesis = "..."`: what the strategy tests, in plain sentences.',
                            attr_lines.get("hypothesis")))
    requires = cls.requires
    if not isinstance(requires, (list, tuple)):
        issues.append(issue("requires", "requires must be a list of feed names",
                            'Set `requires = ["prices"]`.', attr_lines.get("requires")))
    else:
        issues += _feed_issues(requires, feeds, lambda rule, msg, hint: issue(rule, msg, hint, attr_lines.get("requires")))
    if not isinstance(cls.params, dict):
        issues.append(issue("params", "params must be a dict of name -> Param(...)",
                            'Use `params = {"lookback": Param(int, default=20, min=5, max=250)}`.',
                            attr_lines.get("params")))
    else:
        for pname, p in cls.params.items():
            line = param_lines.get(pname, attr_lines.get("params"))
            if not isinstance(p, Param):
                issues.append(issue("params", f"param {pname!r} is not a Param(...)",
                                    f'Declare it as `"{pname}": Param(int, default=..., min=..., max=...)`.', line))
            elif not (isinstance(pname, str) and pname.isidentifier() and not pname.startswith("_")):
                issues.append(issue("params", f"param name {pname!r} is not a valid name",
                                    "Use a lowercase identifier such as `lookback`, without a leading underscore.", line))
            elif pname in ("as_dict",):
                issues.append(issue("params", f"param name {pname!r} is reserved", "Choose another name.", line))
    universe = cls.universe
    if not isinstance(universe, (list, tuple, str)):
        issues.append(issue("universe", "universe must be a list of tickers or the name of a ticker list",
                            'Set e.g. `universe = ["SPY", "QQQ"]`.', attr_lines.get("universe")))
    else:
        try:
            feed = universe_feed(universe)
            if feed is None:
                resolve_universe(universe, universes_dir)
            elif isinstance(requires, (list, tuple)) and feed not in requires:
                issues.append(issue("universe", f"universe from_feed:{feed} needs {feed!r} in requires",
                                    f'Add "{feed}" to requires.', attr_lines.get("universe")))
        except BacktesterError as exc:
            issues.append(issue("universe", str(exc),
                                'Use a non-empty list such as `["SPY", "QQQ"]`, the name of a file in '
                                'data/universes/, or "from_feed:<feed name>".', attr_lines.get("universe")))
    if cls.on_bar is Strategy.on_bar:
        issues.append(issue("on-bar", "on_bar(ctx) is not implemented",
                            "Define `def on_bar(self, ctx):`; it is called once per session before the open."))
    import inspect
    for hook, args in (("on_bar", ["ctx"]), ("on_start", ["ctx"]), ("on_end", ["ctx"]), ("on_fill", ["ctx", "fill"])):
        fn = getattr(cls, hook)
        if fn is getattr(Strategy, hook):
            continue
        try:
            inspect.signature(fn).bind(None, *args)
        except TypeError:
            issues.append(issue("hook-signature", f"{hook} has the wrong arguments",
                                f"Define it as `def {hook}(self, {', '.join(args)}):`.", attr_lines.get(hook)))
    issues.sort(key=lambda i: (i.line is None, i.line or 0))
    return issues, (cls if not issues else None)


def _import_class_unchecked(path: Path):
    import importlib.util
    import inspect

    from .strategy import Strategy
    spec = importlib.util.spec_from_file_location(f"_bt_check_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    classes = [o for o in vars(module).values() if inspect.isclass(o) and issubclass(o, Strategy)
               and o is not Strategy and o.__module__ == module.__name__]
    return classes[0] if len(classes) == 1 else None


# --- stage 3: the strategy's own tests ----------------------------------------

def test_file_for(path: Path) -> Path:
    return path.with_name(f"test_{path.stem}.py")


def run_strategy_tests(path: Path) -> StageResult:
    stage = StageResult("tests")
    test_path = test_file_for(path)
    if not test_path.is_file():
        stage.status, stage.detail = SKIPPED, f"no {test_path.name}"
        return stage
    cmd = [sys.executable, "-m", "pytest", test_path.name, "-q", "-p", "no:cacheprovider",
           "--no-header", "-rf", "--tb=short", "-o", "addopts=", "-p", "no:warnings"]
    try:
        proc = subprocess.run(cmd, cwd=test_path.parent, capture_output=True, text=True,
                              timeout=TESTS_TIMEOUT, env=_child_env())
    except subprocess.TimeoutExpired:
        stage.status = FAILED
        stage.issues.append(Issue("tests-timeout", f"tests took longer than {TESTS_TIMEOUT} s",
                                  "Check the tests and the strategy for loops that never end.", test_path.name))
        return stage
    out = (proc.stdout + proc.stderr).strip()
    summary = out.splitlines()[-1] if out else ""
    if proc.returncode == 0:
        stage.status, stage.detail = PASSED, re.sub(r"\s+in [\d.]+s.*$", "", summary.strip("= "))
        return stage
    stage.status = FAILED
    if proc.returncode == 5:
        stage.issues.append(Issue("tests-none", f"no tests found in {test_path.name}",
                                  "Name test functions `test_...`, or delete the file to skip this stage.",
                                  test_path.name))
        return stage
    failed = re.findall(r"^FAILED (\S+?)::(\S+)(?: - (.*))?$", out, flags=re.M)
    lines_by_test = {}
    for m in re.finditer(rf"^{re.escape(test_path.name)}:(\d+): ", out, flags=re.M):
        lines_by_test.setdefault("first", int(m.group(1)))
    excerpt = "\n".join(out.splitlines()[-40:])
    if failed:
        names = ", ".join(name for _, name, _ in failed)
        message = f"{len(failed)} test{'s' if len(failed) != 1 else ''} failed: {names}"
    else:
        message = f"pytest exited with code {proc.returncode} (the tests could not run)"
    stage.issues.append(Issue("tests-failed", message,
                              "Fix the strategy (or the test, if its expectation is wrong) so every test passes.",
                              test_path.name, lines_by_test.get("first"), detail=excerpt))
    return stage


# --- stages 4 and 5: dry run and determinism -----------------------------------

def _child_env(hash_seed: str | None = None) -> dict:
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONWARNINGS"] = "ignore"
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # keep __pycache__ out of the strategies folder
    if hash_seed is not None:
        env["PYTHONHASHSEED"] = hash_seed
    return env


def _start_dry_run(path: Path, cache_dir: Path | None, hash_seed: str, universes_dir: Path | None,
                   feeds_dir: Path | None = None, plugins_dir: Path | None = None):
    cmd = [sys.executable, "-m", "backtester.dryrun", str(path.resolve())]
    for flag, value in (("--cache-dir", cache_dir), ("--universes-dir", universes_dir),
                        ("--feeds-dir", feeds_dir), ("--plugins-dir", plugins_dir)):
        if value is not None:
            cmd += [flag, str(Path(value).resolve())]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=_child_env(hash_seed), cwd=os.getcwd())


def _finish_dry_run(proc) -> dict:
    try:
        out, err = proc.communicate(timeout=DRY_RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return {"ok": False, "timeout": True}
    for line in reversed(out.strip().splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                break
    return {"ok": False, "crash": (err or out).strip()[-2000:]}


def _dry_run_issues(result: dict, filename: str) -> list[Issue]:
    if result.get("timeout"):
        return [Issue("timeout", f"the dry run took longer than {DRY_RUN_TIMEOUT} s",
                      "Look for loops that never end, or work that grows with every session.", filename)]
    if "crash" in result:
        return [Issue("crash", "the dry run process stopped unexpectedly",
                      "Run the strategy from the command line to see the full error.", filename,
                      detail=result["crash"])]
    return [Issue(**i) for i in result.get("issues", [])]


# --- pipeline ----------------------------------------------------------------

def validate(path, cache_dir=None, universes_dir=None, quick: bool = False,
             feeds_dir=None, plugins_dir=None) -> ValidationReport:
    """Run the validation pipeline on a strategy file.

    Args:
        path: The strategy ``.py`` file.
        cache_dir: Price cache for the dry run. Tickers it does not hold get
            synthetic prices. Defaults to ``data/cache``.
        universes_dir: Folder of named ticker lists (default ``data/universes``).
        quick: Run only the static and declaration stages (milliseconds); the
            other stages are reported as "not run".
        feeds_dir, plugins_dir: Where file feeds and feed plug-ins live
            (default ``data/feeds`` and ``feeds``).
    """
    from .feeds.registry import DEFAULT_CACHE_DIR, DEFAULT_FEEDS_DIR, DEFAULT_PLUGINS_DIR, FeedRegistry

    path = Path(path)
    feeds = FeedRegistry(feeds_dir or DEFAULT_FEEDS_DIR, plugins_dir or DEFAULT_PLUGINS_DIR,
                         cache_dir or DEFAULT_CACHE_DIR, refresh=False)
    stages = [StageResult(name) for name in STAGE_NAMES]
    report = ValidationReport(path.name, stages)
    by_name = {s.name: s for s in stages}

    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        by_name["static"].status = FAILED
        by_name["static"].issues.append(Issue("unreadable", str(exc), "Save the file as UTF-8 text.", path.name))
        return report

    # 1. static
    issues, api_version, _ = static_check(source, path.name)
    report.api_version = api_version
    s = by_name["static"]
    if issues:
        s.status, s.issues = FAILED, issues
        return report
    s.status = PASSED

    # 2. declarations
    issues, cls = check_declarations(path, source, universes_dir, feeds)
    s = by_name["declarations"]
    if issues:
        s.status, s.issues = FAILED, issues
        return report
    s.status = PASSED
    report.strategy_name = cls.name
    if quick:
        return report

    # Start both dry runs now; they run while the strategy's tests do.
    procs = [_start_dry_run(path, cache_dir, seed, universes_dir, feeds_dir, plugins_dir) for seed in ("1", "2")]
    try:
        # 3. tests
        s = by_name["tests"]
        tests = run_strategy_tests(path)
        s.status, s.detail, s.issues = tests.status, tests.detail, tests.issues
        if s.status == FAILED:
            return report

        # 4. dry run
        first, second = (_finish_dry_run(p) for p in procs)
        s = by_name["dry run"]
        if not first.get("ok"):
            s.status, s.issues = FAILED, _dry_run_issues(first, path.name)
            return report
        s.status, s.detail = PASSED, first.get("summary", "")

        # 5. determinism
        s = by_name["determinism"]
        if not second.get("ok"):
            s.status = FAILED
            s.issues = [Issue("nondeterministic", "a second dry run failed where the first passed",
                              "Make the strategy depend only on ctx: no randomness except ctx.rng, "
                              "and no reliance on set or dict ordering of unsorted data.", path.name)]
            s.issues += _dry_run_issues(second, path.name)
        elif first["hash"] != second["hash"]:
            s.status = FAILED
            s.issues = [Issue("nondeterministic", "two dry runs with the same inputs gave different results",
                              "Use ctx.rng for any randomness, sort sets before iterating over them, and keep "
                              "state in ctx.state or on self rather than at module level.", path.name)]
        else:
            s.status = PASSED
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.communicate()
    return report


def static_report(path, source: str | None = None) -> ValidationReport | None:
    """Stage 1 only, for checking a file before every run. The report if it fails, else None."""
    path = Path(path)
    source = path.read_text(encoding="utf-8") if source is None else source
    issues, api_version, _ = static_check(source, path.name)
    if not issues:
        return None
    stages = [StageResult(name) for name in STAGE_NAMES]
    stages[0].status, stages[0].issues = FAILED, issues
    return ValidationReport(path.name, stages, api_version=api_version)

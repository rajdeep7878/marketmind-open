"""Source-scan test: every tick_* job has assert_paper_only() as its first statement.

Mirrors `test_trader_no_anthropic_import.py`'s philosophy — drift
in a load-bearing safety invariant should fail at PR time, not at
runtime in production.

Two checks:
  1. Every function in `jobs.py` whose name starts with `tick_`
     has, as its literal first statement after the optional
     docstring, a call to `assert_paper_only()`.
  2. The runner.main() function calls `assert_paper_only()` as
     ITS first statement (after the docstring).

Implementation: parses the source with `ast`, walks the
module-level FunctionDef nodes, inspects body[0] / body[1].
"""

from __future__ import annotations

import ast
import inspect

from marketmind_workers.trader import jobs as jobs_module
from marketmind_workers.trader import runner as runner_module


def _is_docstring(node: ast.stmt) -> bool:
    if not isinstance(node, ast.Expr):
        return False
    value = node.value
    return isinstance(value, ast.Constant) and isinstance(value.value, str)


def _is_assert_paper_only_call(node: ast.stmt) -> bool:
    if not isinstance(node, ast.Expr):
        return False
    call = node.value
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    return isinstance(func, ast.Name) and func.id == "assert_paper_only"


def _first_executable_stmt(func: ast.FunctionDef) -> ast.stmt:
    """The first body statement that isn't the docstring."""
    body = func.body
    if body and _is_docstring(body[0]):
        return body[1] if len(body) > 1 else body[0]
    return body[0]


def test_every_tick_function_has_assert_paper_only_as_first_stmt() -> None:
    """The load-bearing assertion. Every tick_* function in
    jobs.py must call assert_paper_only() as its first
    executable statement.
    """
    source = inspect.getsource(jobs_module)
    tree = ast.parse(source)
    offenders: list[str] = []
    tick_funcs: list[ast.FunctionDef] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("tick_"):
            tick_funcs.append(node)
            first = _first_executable_stmt(node)
            if not _is_assert_paper_only_call(first):
                offenders.append(node.name)
    assert tick_funcs, (
        "Expected at least one tick_* function in jobs.py; "
        "either the file was renamed or all callables were removed."
    )
    assert not offenders, (
        "These tick_* functions are missing `assert_paper_only()` "
        f"as their first executable statement: {offenders}"
    )


def test_runner_main_has_assert_paper_only_as_first_stmt() -> None:
    """The runner's main() must also call assert_paper_only()
    first — before logging, settings, DB, or anything else that
    could leak the process past the live-execution gate.
    """
    source = inspect.getsource(runner_module)
    tree = ast.parse(source)
    main_func: ast.FunctionDef | None = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            main_func = node
            break
    assert main_func is not None, "runner.main() not found"
    first = _first_executable_stmt(main_func)
    assert _is_assert_paper_only_call(first), (
        "runner.main() must call assert_paper_only() as its first executable statement"
    )


def test_jobs_self_check_helper_runs_clean() -> None:
    """The module's own AST self-check (called at runner boot)
    must pass against the source. If a future commit drops the
    assert, this raises — which is the design.
    """
    # Just calling it counts as the assertion; it raises on drift.
    jobs_module.verify_paper_only_first_line()

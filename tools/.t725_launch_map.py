"""T7.25 §1/§15 - the launch-path map, with ownership as a column.

T7.24 asked "who shuts this down?" and found ten production call sites that are
handed a browser and never own its shutdown. T7.25 is the migration, so the same
question is asked again but with a sharper verdict: not "does the call site
record a lease?" but "is there any production call site outside the launch layer
that obtains a browser without an owning operation?".

The columns are the ones the task names:

    CALLSITE / OWNER TYPE / OPERATION LIFETIME / CURRENT ACQUIRE /
    CURRENT RELEASE / RISK

``OWNER TYPE`` is read from the *enclosing* ``with hh_operation(...)`` /
``with managed_headless_session(...)``, never guessed. A call site with no such
enclosing block is ``UNOWNED``, which is a finding.

    python tools/.t725_launch_map.py [--json out.json]
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]

# The three functions that hand out a browser. Two of them can also *start* one.
LAUNCH_PRIMITIVES = ("ensure_hh_browser", "ensure_headless_browser", "launch_headless")

# The ownership contract. A call site lexically inside one of these has an owner.
OWNING_CONTEXT_MANAGERS = ("hh_operation", "managed_headless_session")

# The launch layer itself. These modules *are* the primitives: their internal
# calls are the implementation, not a production path that forgot an owner.
LAUNCH_LAYER = (
    "ai_assistant/hh_browser_lease.py",
    "ai_assistant/hh_browser_provider.py",
    "ai_assistant/hh_browser_launcher.py",
)

RISK_UNOWNED = "UNOWNED"
RISK_OWNED = "OWNED"
RISK_PRIMITIVE = "PRIMITIVE"
RISK_SESSION = "SESSION-OBJECT"


def _modules() -> list[pathlib.Path]:
    files = sorted(REPO.glob("ai_assistant/**/*.py"))
    for pattern in ("tools/*.py", "tests/*.py"):
        files.extend(sorted(REPO.glob(pattern)))
    return files


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _segment(src: str, node: ast.AST) -> str:
    return ast.get_source_segment(src, node) or "<unavailable>"


def _owning_with(tree: ast.Module) -> dict[int, dict]:
    """Map every line inside an owning ``with`` to what that ``with`` says.

    Keyed by line number because that is the only thing a call site and its
    enclosing statement reliably share. The innermost owning block wins.
    """
    found: dict[int, dict] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            expr = item.context_expr
            if not isinstance(expr, ast.Call):
                continue
            name = _call_name(expr)
            if name not in OWNING_CONTEXT_MANAGERS:
                continue
            owner = "<expr>"
            if expr.args and isinstance(expr.args[0], ast.Constant):
                owner = str(expr.args[0].value)
            release = (
                "finally (hh_operation)"
                if name == "hh_operation"
                else "finally (managed_headless_session)"
            )
            lo = node.lineno
            hi = node.end_lineno or node.lineno
            for line in range(lo, hi + 1):
                found[line] = {
                    "context_manager": name,
                    "owner_type": owner,
                    "current_release": release,
                    "with_line": node.lineno,
                }
    return found


def _enclosing_functions(tree: ast.Module) -> list[tuple[int, int, str]]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((node.lineno, node.end_lineno or node.lineno, node.name))
    return sorted(out)


def _owner_function(funcs: list[tuple[int, int, str]], line: int) -> tuple[str, int, int]:
    """The innermost function containing ``line`` - the operation's lifetime.

    Innermost, not first: a call inside a nested helper belongs to that helper,
    and naming the outer function would make the lifetime column a lie.
    """
    best: tuple[int, int, str] | None = None
    for lo, hi, name in funcs:
        if lo <= line <= hi and (best is None or lo >= best[0]):
            best = (lo, hi, name)
    if best is None:
        return ("<module>", line, line)
    return (best[2], best[0], best[1])


def _module_calls_stop(tree: ast.Module) -> bool:
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "stop"
        for n in ast.walk(tree)
    )


def scan() -> list[dict]:
    rows: list[dict] = []
    for path in _modules():
        rel = path.relative_to(REPO).as_posix()
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (SyntaxError, UnicodeDecodeError) as exc:
            rows.append({"callsite": rel, "error": f"{type(exc).__name__}: {exc}"})
            continue

        owning = _owning_with(tree)
        funcs = _enclosing_functions(tree)
        module_stop = _module_calls_stop(tree)
        is_production = rel.startswith("ai_assistant/")
        in_layer = rel in LAUNCH_LAYER

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name not in LAUNCH_PRIMITIVES:
                continue
            line = node.lineno
            fn, lo, hi = _owner_function(funcs, line)
            ctx = owning.get(line)
            if ctx is not None:
                owner_type = ctx["owner_type"]
                release = ctx["current_release"]
                risk = RISK_OWNED
            elif in_layer:
                owner_type = "n/a"
                release = "n/a (launch layer)"
                risk = RISK_PRIMITIVE
            elif name == "launch_headless" or name == "ensure_headless_browser":
                owner_type = "none"
                release = "none"
                risk = RISK_UNOWNED
            else:
                owner_type = "none"
                release = "explicit .stop()" if module_stop else "none"
                risk = RISK_UNOWNED

            rows.append({
                "callsite": f"{rel}:{line}",
                "file": rel,
                "line": line,
                "production": is_production,
                "launch_layer": in_layer,
                "call": name,
                "acquire": _segment(src, node),
                "owner_type": owner_type,
                "operation_lifetime": f"{fn}() lines {lo}-{hi}",
                "current_release": release,
                "risk": risk,
            })
    return rows


def _report(rows: list[dict], operations: list[dict]) -> int:
    good = [r for r in rows if "error" not in r]
    print("=" * 100)
    print("§1  LAUNCH-PATH MAP  -  CALLSITE / OWNER TYPE / OPERATION LIFETIME / "
          "CURRENT ACQUIRE / CURRENT RELEASE / RISK")
    print("=" * 100)
    header = f"{'CALLSITE':<44} {'OWNER TYPE':<13} {'CURRENT RELEASE':<22} RISK"
    print(header)
    print("-" * 100)
    for row in sorted(good, key=lambda r: (r["file"], r["line"])):
        if not row["production"]:
            continue
        print(f"{row['callsite']:<44} {row['owner_type']:<13} "
              f"{row['current_release']:<22} {row['risk']}")

    print()
    print("  acquire expressions, and what lifetime they sit in:")
    for row in sorted(good, key=lambda r: (r["file"], r["line"])):
        if not row["production"]:
            continue
        print(f"    {row['callsite']:<44} {row['acquire']}")
        print(f"    {'':<44} lifetime: {row['operation_lifetime']}")

    unowned = [r for r in good
               if r["production"] and not r["launch_layer"] and r["risk"] == RISK_UNOWNED]
    owned = [r for r in good
             if r["production"] and not r["launch_layer"] and r["risk"] == RISK_OWNED]
    layer = [r for r in good if r["production"] and r["launch_layer"]]

    print()
    print("=" * 100)
    print("§15  THE VERDICT")
    print("=" * 100)
    print(f"  production call sites inside the launch layer (the primitives) : {len(layer)}")
    for row in layer:
        print(f"      {row['callsite']}  {row['call']}")
    print(f"  production call sites inside an owning operation               : {len(owned)}")
    for row in owned:
        print(f"      {row['callsite']}  {row['call']}  owner={row['owner_type']}")
    print(f"  PRODUCTION UNOWNED LAUNCH PATHS                                : {len(unowned)}")
    for row in unowned:
        print(f"      {row['callsite']}  {row['call']}")

    print()
    print(f"  owning operations in production (acquire -> work -> finally release): "
          f"{len(operations)}")
    for row in sorted(operations, key=lambda r: (r["file"], r["line"])):
        print(f"      {row['callsite']:<44} owner={row['owner_type']:<12} "
              f"purpose={row['purpose'] or '(none)'}  via {row['manager']}")

    print()
    print("  tests/tools (separate accounting, never a production finding):")
    for prefix in ("tests/", "tools/"):
        subset = [r for r in good if r["file"].startswith(prefix)]
        counts: dict[str, int] = {}
        for row in subset:
            counts[row["call"]] = counts.get(row["call"], 0) + 1
        print(f"    {prefix:<8} " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return len(unowned)


def _owner_literal(node: ast.AST) -> str:
    """The owner type as written: a string literal, or a constant's name.

    ``OWNER_CLI`` is normally spelled ``lease.OWNER_CLI`` or ``_lease.OWNER_CLI``,
    so the attribute name *is* the owner type - that is the whole point of the
    named constants, and reporting ``<expr>`` would hide it.
    """
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return "<expr>"


def scan_operations() -> list[dict]:
    """The other half of §15: where production *does* own a browser.

    A map that only counts unowned paths cannot show that the migration happened;
    it can only show that the old calls are gone. These rows name the owner and the
    purpose for every ``with hh_operation(...)`` / ``managed_headless_session(...)``
    in a production module.
    """
    rows: list[dict] = []
    for path in sorted(REPO.glob("ai_assistant/**/*.py")):
        rel = path.relative_to(REPO).as_posix()
        if rel in LAUNCH_LAYER:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            for item in node.items:
                expr = item.context_expr
                if not isinstance(expr, ast.Call):
                    continue
                name = _call_name(expr)
                if name not in OWNING_CONTEXT_MANAGERS:
                    continue
                owner = "<expr>"
                if expr.args:
                    owner = _owner_literal(expr.args[0])
                for kw in expr.keywords:
                    if kw.arg == "owner_type":
                        owner = _owner_literal(kw.value)
                purpose = ""
                if len(expr.args) > 1 and isinstance(expr.args[1], ast.Constant):
                    purpose = str(expr.args[1].value)
                for kw in expr.keywords:
                    if kw.arg == "purpose" and isinstance(kw.value, ast.Constant):
                        purpose = str(kw.value.value)
                rows.append({
                    "callsite": f"{rel}:{node.lineno}",
                    "file": rel,
                    "line": node.lineno,
                    "manager": name,
                    "owner_type": owner,
                    "purpose": purpose,
                })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default="")
    parser.add_argument("--expect-zero", action="store_true",
                        help="exit 1 when any production unowned launch path remains")
    args = parser.parse_args()

    rows = scan()
    unowned = _report(rows, scan_operations())

    if args.json:
        out = pathlib.Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\njson: {out}")

    if args.expect_zero and unowned:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

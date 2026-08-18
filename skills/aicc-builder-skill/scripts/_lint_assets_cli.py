# ---------------------------------------------------------------------------
# CLI driver (appended by scripts/_extract_prompts.py — not part of the backend
# module). Runs every library-tier linter above over an <output_dir> bundle.
# ---------------------------------------------------------------------------

import json as _cli_json
import re as _cli_re
import sys as _cli_sys
from pathlib import Path as _CliPath


def _cli_assets_root(output_dir):
    """Newest ``assets/vN`` directory, falling back to a flat ``assets/``."""
    base = output_dir / "assets"
    versioned = sorted(
        (d for d in base.glob("v*") if d.is_dir() and _cli_re.fullmatch(r"v\d+", d.name)),
        key=lambda d: int(d.name[1:]),
    )
    return versioned[-1] if versioned else base


def _cli_run(output_dir, apply_fixes):
    """Return (report, error_count, pending_fix_count)."""
    assets = _cli_assets_root(output_dir)
    report = []
    errors = 0
    pending = 0

    def add(path, kind, errs, warns, fixes, fixed_text=None, target=None):
        nonlocal errors, pending
        errs = [str(e) for e in (errs or [])]
        warns = [str(w) for w in (warns or [])]
        fixes = [str(f) for f in (fixes or [])]
        if not (errs or warns or fixes):
            return          # clean file: stay quiet rather than print a bare header
        errors += len(errs)
        if fixes:
            if apply_fixes and fixed_text is not None:
                (target or path).write_text(fixed_text)
            else:
                pending += len(fixes)
        report.append({"file": str(path), "linter": kind, "errors": errs,
                       "warnings": warns, "fixes_applied": fixes,
                       "written": bool(fixes and apply_fixes and fixed_text is not None)})

    # ---- Lambda handlers: Python syntax + BUSINESS_OUTCOME_200 status codes ----
    lam_dir = assets / "lambda"
    if lam_dir.is_dir():
        for p in sorted(lam_dir.rglob("*.py")):
            code = p.read_text()
            syn = lint_python_source(code)
            if not syn["ok"]:
                add(p, "python_syntax",
                    [f"line {e.get('line')}: {e.get('message')}" for e in syn["errors"]],
                    [], [])
                continue    # a file that doesn't compile can't be status-linted
            status = lint_lambda_status_codes(code)
            if status["fixes_applied"] or status["warnings"]:
                add(p, "lambda_status_codes", [], status["warnings"],
                    status["fixes_applied"], status["fixed_code"])

    # ---- CloudFormation ----
    infra_dir = assets / "infrastructure"
    if infra_dir.is_dir():
        for p in sorted(infra_dir.glob("*.y*ml")):
            res = lint_and_autofix_cfn(p.read_text())
            if not res["available"]:
                report.append({"file": str(p), "linter": "cloudformation",
                               "errors": [], "warnings": [
                                   "cfn-lint not installed — autofixes applied but the "
                                   "template was NOT validated (pip install cfn-lint)"],
                               "fixes_applied": res["fixes_applied"], "written": False})
                continue
            add(p, "cloudformation",
                [f"{e.get('id')}@L{e.get('line')}: {e.get('message')}" for e in res["errors"]],
                [f"{w.get('id')}@L{w.get('line')}: {w.get('message')}" for w in res["warnings"]],
                res["fixes_applied"], res["fixed_yaml"])

    # ---- OpenAPI ----
    oapi_dir = assets / "openapi"
    if oapi_dir.is_dir():
        for p in sorted(oapi_dir.glob("*.y*ml")):
            res = lint_and_autofix_openapi(p.read_text())
            warns = [] if res["available"] else [
                "openapi-spec-validator not installed — autofixes applied but the "
                "document was NOT validated (pip install openapi-spec-validator)"]
            add(p, "openapi", res["errors"], warns,
                res["fixes_applied"], res["fixed_yaml"])

    # ---- Contact Flow ----
    cf_dir = assets / "contact_flow"
    if cf_dir.is_dir():
        for p in sorted(cf_dir.rglob("*.json")):
            res = lint_contact_flow(p.read_text())
            add(p, "contact_flow", res["errors"], res["warnings"],
                res["fixes_applied"], res.get("fixed_json"))

    # ---- AI prompt (qconnect variable-once rule) ----
    pr_dir = assets / "prompt"
    if pr_dir.is_dir():
        for p in sorted(pr_dir.rglob("*")):
            if p.suffix.lower() not in (".yaml", ".yml", ".md", ".txt") or not p.is_file():
                continue
            res = lint_ai_prompt(p.read_text())
            if res["errors"] or res["fixes_applied"] or res["warnings"]:
                add(p, "ai_prompt", res["errors"], res["warnings"],
                    res["fixes_applied"], res.get("fixed_text"))

    return report, errors, pending


def _cli_main():
    argv = _cli_sys.argv[1:]
    flags = {a for a in argv if a.startswith("-")}
    args = [a for a in argv if not a.startswith("-")]
    unknown = flags - {"--fix", "--json"}
    if len(args) != 1 or unknown:
        print(f"Usage: {_cli_sys.argv[0]} <output_dir> [--fix] [--json]\n"
              f"  --fix   write the deterministic auto-fixes back to the files\n"
              f"  --json  machine-readable report on stdout",
              file=_cli_sys.stderr)
        return 2
    out_dir = _CliPath(args[0]).resolve()
    if not out_dir.is_dir():
        print(f"ERROR: not a directory: {out_dir}", file=_cli_sys.stderr)
        return 2

    apply_fixes = "--fix" in flags
    report, errors, pending = _cli_run(out_dir, apply_fixes)

    if "--json" in flags:
        print(_cli_json.dumps({"success": errors == 0 and pending == 0,
                               "errors": errors, "pending_fixes": pending,
                               "results": report}, indent=2, ensure_ascii=False))
        return 0 if (errors == 0 and pending == 0) else 1

    if not report:
        print(f"OK — nothing to lint under {_cli_assets_root(out_dir)}")
        return 0
    for r in report:
        head = f"{r['linter']}: {r['file']}"
        print(head)
        for e in r["errors"]:
            print(f"  ERROR   {e}")
        for w in r["warnings"]:
            print(f"  WARN    {w}")
        for f in r["fixes_applied"]:
            print(f"  {'FIXED  ' if r['written'] else 'NEEDS FIX'} {f}")
    print("")
    if errors == 0 and pending == 0:
        print("OK — no blocking lint findings"
              + (" (auto-fixes written)" if apply_fixes else ""))
        return 0
    print(f"FAIL — {errors} error(s), {pending} pending auto-fix(es).")
    if pending and not apply_fixes:
        print("Re-run with --fix to apply the deterministic fixes, then re-lint.")
    return 1


if __name__ == "__main__":
    _cli_sys.exit(_cli_main())

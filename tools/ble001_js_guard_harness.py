"""BLE001 finding #34: run the real submission JS on a real JS engine.

Why this exists
---------------
The screening-form guard lives inside two Python strings that Python never
executes, and every pytest test that walks the submission flow stubs
``evaluate_fn`` out. So pytest can only pin the *text* of the guard, not what it
does. A mutation that leaves the text intact but changes the behaviour survives
the whole suite - that is a measured fact, not a worry: see the mutation table
in docs/ble001_triage.md (finding #34, M11).

This tool closes that gap by hand: it takes the exact JS the code hands to the
browser, runs it on node against the real control list of a live hh.ru response
form (artifacts/hh_manual_form_snapshot.json - 48 controls, 17 names,
11 questions), and checks the numbers.

Nothing goes out to the network and no browser is involved.

Usage
-----
    .venv/Scripts/python.exe tools/ble001_js_guard_harness.py

Exit 0 means every case matched. Node must be on PATH (or set NODE_BIN); if it
is missing the tool exits non-zero rather than reporting a silent success.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent

# The node harness. It implements the selector for real - a stub that returned
# every control for any selector mentioning "task_" would hide a selector that
# stopped matching textareas, which is exactly one of the mutations.
NODE_HARNESS = r"""
const fs = require('fs');
const DIR = process.argv[2];
const inspectSrc = fs.readFileSync(DIR + '/live.js', 'utf8');
const clickSrc = fs.readFileSync(DIR + '/click.js', 'utf8');
const rawControls = JSON.parse(fs.readFileSync(DIR + '/controls.json', 'utf8'));

function makeField(o) {
  return {
    name: o.name, tag: o.tag, type: o.type,
    checked: !!o.checked, value: o.value || '', disabled: !!o.disabled,
    getAttribute(k) { return k === 'name' ? o.name : null; },
  };
}

function compile(selector) {
  return String(selector).split(',').map((part) => {
    let s = part.trim();
    let tag = null;
    const tagM = s.match(/^([a-z]+)/i);
    if (tagM) { tag = tagM[1].toLowerCase(); s = s.slice(tagM[0].length); }
    const attrs = [];
    const re = /\[\s*([a-zA-Z-]+)\s*(\^=|=)\s*'([^']*)'\s*\]/g;
    let m;
    while ((m = re.exec(s)) !== null) attrs.push({ name: m[1], op: m[2], val: m[3] });
    return (f) => {
      if (tag && f.tag !== tag) return false;
      for (const a of attrs) {
        const actual = a.name === 'type' ? f.type : a.name === 'name' ? f.name : '';
        if (a.op === '=' && actual !== a.val) return false;
        if (a.op === '^=' && !String(actual).startsWith(a.val)) return false;
      }
      return true;
    };
  });
}

function makeDoc(rawFields) {
  let clicked = 0;
  const fields = rawFields.map(makeField);
  const button = { disabled: false, innerText: 'Откликнуться', click() { clicked++; } };
  return {
    _clicked: () => clicked,
    querySelectorAll(selector) {
      const preds = compile(selector);
      return fields.filter((f) => preds.some((p) => p(f)));
    },
    querySelector(selector) {
      const s = String(selector);
      if (s.indexOf('response-submit') !== -1 || s.indexOf('submit') !== -1 ||
          s.indexOf('vacancy-response-link') !== -1) return button;
      return null;
    },
    title: 'Вакансия',
    body: { innerText: 'Откликнуться' },
  };
}

function fill(fields, how) {
  const out = fields.map((f) => Object.assign({}, f));
  if (how === 'all') {
    for (const f of out) {
      if (f.type === 'radio' || f.type === 'checkbox') f.checked = true;
      else f.value = 'answer';
    }
  } else if (how === 'choice-only') {
    for (const f of out) if (f.type === 'radio' || f.type === 'checkbox') f.checked = true;
  }
  return out;
}

const cases = {
  'form as it is': rawControls,
  'every field filled': fill(rawControls, 'all'),
  'choice groups filled only': fill(rawControls, 'choice-only'),
  'no form on the page': [],
};

const results = {};
for (const [label, fields] of Object.entries(cases)) {
  const docI = makeDoc(fields);
  const loc = { href: 'https://hh.ru/vacancy/136591579' };
  const win = { location: loc };
  const parsedI = JSON.parse(new Function('document', 'location', 'window',
    'return (' + inspectSrc + ')')(docI, loc, win));
  const docC = makeDoc(fields);
  const parsedC = JSON.parse(new Function('document', 'location', 'window',
    'return (' + clickSrc + ')')(docC, loc, win));
  results[label] = {
    control_count: parsedI.screening_control_count,
    unanswered: parsedI.screening_unanswered_count,
    refused: parsedC.refused === true,
    clicked: docC._clicked() > 0,
    error: parsedI.error || null,
  };
}
console.log(JSON.stringify(results, null, 2));
"""

# label -> (control_count, unanswered, refused, clicked)
EXPECTED = {
    "form as it is": (48, 11, True, False),
    "every field filled": (48, 0, False, True),
    "choice groups filled only": (48, 5, True, False),
    "no form on the page": (0, 0, False, True),
}


def find_node() -> str | None:
    env = os.environ.get("NODE_BIN")
    if env and Path(env).exists():
        return env
    found = shutil.which("node")
    if found:
        return found
    managed = Path.home() / ".workbuddy-ai" / "binaries" / "node"
    for candidate in sorted(managed.glob("versions/*/node.exe"), reverse=True):
        return str(candidate)
    return None


def main() -> int:
    node = find_node()
    if node is None:
        print("node not found: set NODE_BIN or put node on PATH. "
              "Refusing to report success without running the JS.")
        return 2

    os.chdir(REPO)
    sys.path.insert(0, str(REPO))
    os.environ["SUBMIT_ALLOWED"] = "true"

    workdir = Path(tempfile.mkdtemp(prefix="ble34js_"))
    try:
        # A throwaway copy of the database: execute_hh_submission writes claims.
        tmp_db = workdir / "probe.db"
        shutil.copy2(REPO / "state.db", tmp_db)
        os.environ["DB_FILE"] = str(tmp_db)

        from ai_assistant import hh_live_page_checks as live
        from ai_assistant.hh_live_page_checks import check_live_page

        captured: dict[str, str] = {}

        def capture(js: str) -> str:
            if "hh_live_page_inspect" in js:
                captured["inspect"] = js
                return json.dumps({
                    "ok": True, "url": "https://hh.ru/vacancy/136591579",
                    "title": "Senior AI Automation Engineer",
                    "is_404": False, "is_captcha": False, "is_access_denied": False,
                    "is_login_required": False, "already_responded": False,
                    "has_submit_btn": False, "submit_btn_disabled": False,
                    "has_apply_btn": True, "has_response_modal": False,
                })
            if "hh_submit_click" in js:
                captured["click"] = js
            return json.dumps({"ok": True})

        # The real live-inspection call.
        check_live_page(capture, expected_vacancy_id="hh:136591579",
                        expected_title="Senior AI Automation Engineer")

        # The real click call, with the gates stubbed green so the flow reaches
        # the click (the gates have their own tests).
        import ai_assistant.db as dbmod
        import ai_assistant.hh_submission as hs
        from ai_assistant.application_review import (
            ApplicationReview,
            ReviewStatus,
            save_application_review,
        )
        from ai_assistant.application_tracking import (
            ApplicationStatus,
            set_application_status,
        )
        from ai_assistant.db import init_db
        from ai_assistant.hh_submission import (
            GateCheckResult,
            clear_submitted_reviews,
            execute_hh_submission,
        )

        init_db()
        clear_submitted_reviews()
        vid = "999000901"
        sid = f"hh:{vid}"
        save_application_review(ApplicationReview(
            vacancy_stable_id=sid, status=ReviewStatus.APPROVED,
            form_fingerprint=f"fp_{vid}", review_id=f"rev_{vid}"))
        set_application_status(sid, ApplicationStatus.READY_TO_APPLY)

        live.check_live_page = lambda *a, **kw: live.LivePageResult(
            is_ok=True, status="READY", reason="READY",
            current_url=f"https://hh.ru/vacancy/{vid}",
            numeric_id_match=True, title_matched=True, has_apply_btn=True)
        dbmod.get_vacancy_by_id = lambda vacancy_id: ("row",)
        dbmod._row_to_vacancy = lambda row: SimpleNamespace(title="Python Developer")
        hs.HHSubmissionGates.check_all_gates = classmethod(
            lambda cls, *a, **kw: GateCheckResult(passed=True, reason="stub: gates green"))

        execute_hh_submission(sid, evaluate_fn=capture, human_confirmed=True)

        for key in ("inspect", "click"):
            if key not in captured:
                print(f"could not capture the {key} JS from the code path")
                return 1
            if live._SCREENING_GUARD_PLACEHOLDER in captured[key]:
                print(f"the {key} JS still carries the un-substituted "
                      f"{live._SCREENING_GUARD_PLACEHOLDER} placeholder")
                return 1

        with open(REPO / "artifacts" / "hh_manual_form_snapshot.json",
                  encoding="utf-8") as fh:
            snapshot = json.load(fh)
        controls = snapshot["snapshot"]["controls"]
        (workdir / "live.js").write_text(captured["inspect"], encoding="utf-8")
        (workdir / "click.js").write_text(captured["click"], encoding="utf-8")
        (workdir / "controls.json").write_text(
            json.dumps([{"name": c.get("name", ""),
                         "tag": (c.get("tag") or "").lower(),
                         "type": (c.get("type") or "").lower()}
                        for c in controls], ensure_ascii=False),
            encoding="utf-8")

        (workdir / "harness.js").write_text(NODE_HARNESS, encoding="utf-8")
        proc = subprocess.run([node, str(workdir / "harness.js"), str(workdir)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", check=False)
        if proc.returncode != 0:
            print("node harness failed:")
            print(proc.stdout, proc.stderr)
            return 1
        results = json.loads(proc.stdout)

        print(f"real control list: {len(controls)} controls, "
              f"{len({c.get('name') for c in controls})} distinct names")
        print(f"{'case':<28} {'controls':>8} {'unanswered':>10} "
              f"{'refused':>8} {'clicked':>8}")
        bad = []
        for label, want in EXPECTED.items():
            got = results.get(label)
            if got is None:
                bad.append(f"{label}: missing from the harness output")
                continue
            row = (got["control_count"], got["unanswered"], got["refused"], got["clicked"])
            mark = "ok" if row == want else "MISMATCH"
            if row != want:
                bad.append(f"{label}: got {row}, expected {want}")
            print(f"{label:<28} {row[0]:>8} {row[1]:>10} {row[2]!s:>8} "
                  f"{row[3]!s:>8}  {mark}")
            if got["error"]:
                bad.append(f"{label}: the inspection JS raised {got['error']}")

        if bad:
            print()
            for line in bad:
                print("FAIL:", line)
            return 1
        print()
        print("all cases matched")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

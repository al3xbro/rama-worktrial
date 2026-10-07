"""Score the verification tool on the trial orders against the hand-verified answer key.

It scores the latest batch run of each order (`python -m agent batch`): the tool's own output.
Runs made by the agent while handling an email are left out, because the agent may have resolved
items or added the customer's referenced terms since.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config, state

KEY = config.SERVER_DIR / "eval" / "answer_key.json"
ISSUE = {"fail", "needs_approval", "needs_review"}


def latest_result(so: str) -> dict | None:
    for run in state.runs_for_order(int(so)):  # newest first
        if not run.get("message_id") and run.get("source") != "upload" and run.get("report_path"):
            path = Path(run["report_path"]).with_name("result.json")
            if path.exists():
                return json.loads(path.read_text())
    return None


def evaluate() -> dict:
    key = json.loads(KEY.read_text())
    rows, caught, expected, unexpected_total = [], 0, 0, 0
    for so, spec in key.items():
        if so.startswith("_"):
            continue
        r = latest_result(so)
        if r is None:
            rows.append({"so": so, "name": spec["name"], "error": "no run"})
            continue
        findings = r["findings"]
        hits, misses = [], []
        matched = set()
        for e in spec["expect"]:
            expected += 1
            found = [i for i, f in enumerate(findings) if f["check_id"] == e["check_id"] and ("line" not in e or f.get("line") == e["line"])
                     and e.get("title_contains", "") in f["title"]]
            ok = [i for i in found if findings[i]["status"] == e["status"]]
            if ok:
                caught += 1
                hits.append(e)
                matched.update(ok)
            else:
                misses.append({**e, "got": sorted({findings[i]["status"] for i in found}) or ["no finding"]})
        unexpected = [f"{f['status']} {f['check_id']}{' line ' + str(f['line']) if f.get('line') else ''}: {f['message'][:110]}"
                      for i, f in enumerate(findings) if f["status"] in ISSUE and i not in matched]
        unexpected_total += len(unexpected)
        rows.append({"so": so, "name": spec["name"], "recommendation": r["recommendation"], "caught": len(hits),
                     "expected": len(spec["expect"]), "misses": misses, "unexpected": unexpected, "not_built": spec["not_built"]})
    return {"caught": caught, "expected": expected, "unexpected": unexpected_total, "orders": rows}


def print_report(res: dict) -> None:
    for row in res["orders"]:
        if "error" in row:
            print(f"{row['so']} {row['name']}: {row['error']}")
            continue
        print(f"{row['so']} {row['name']:<26} {row['recommendation']:<15} caught {row['caught']}/{row['expected']}")
        for m in row["misses"]:
            print(f"    MISSED   {m['check_id']}{' line ' + str(m['line']) if 'line' in m else ''}: expected {m['status']}, got {', '.join(m['got'])}")
        for u in row["unexpected"]:
            print(f"    EXTRA    {u}")
    print(f"\nCaught {res['caught']} of {res['expected']} expected findings; {res['unexpected']} issue-level findings not in the key "
          "(each is a false positive or a real issue the key missed).")

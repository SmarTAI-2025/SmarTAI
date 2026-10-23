"""Run independent B2 safety preparation on disposable storage, never live DB."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess


class Results:
    def __init__(self):
        self.rows = []
        self.selected = []
    def pytest_collection_finish(self, session):
        self.selected = [item.nodeid.split("::", 1)[-1] for item in session.items]
    def pytest_runtest_logreport(self, report):
        if report.when == "call" or ((report.failed or report.skipped) and report.when in {"setup", "teardown"}):
            self.rows.append(dict(id=report.nodeid.split("::", 1)[-1], phase=report.when,
                outcome=report.outcome, duration_seconds=report.duration,
                properties=dict(report.user_properties),
                failure=(str(report.longrepr).splitlines()[-1] if report.failed else None)))
    def case_rows(self):
        rows = []
        for id in self.selected:
            observations = [r for r in self.rows if r["id"] == id]
            rows.append(next((r for r in observations if r["outcome"] == "failed"),
                             observations[-1] if observations else dict(id=id, phase="not_run", outcome="not_run",
                                 duration_seconds=0, properties={}, failure="No terminal test observation")))
        return rows


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    from .run import isolated_imports
    from .reliability_cases import BASE, corpus_hash
    import pytest
    root = Path(__file__).resolve().parents[3]
    test_path = root / "backend/tests/math_grading_evaluation/acceptance_reliability_safety.py"
    before_hash = corpus_hash()
    results = Results()
    with isolated_imports():
        code = pytest.main(["-q", "-p", "no:cacheprovider", "--tb=short", str(test_path)], plugins=[results])
    assert corpus_hash() == before_hash, "B1 semantic oracle changed during safety preparation"
    rows = results.case_rows()
    report = dict(kind="B2_preparation_not_final_acceptance", recorded_at=datetime.now(timezone.utc).isoformat(),
        revision=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        production_baseline=BASE, b1_corpus_sha256=before_hash,
        test_source_sha256=hashlib.sha256(test_path.read_bytes()).hexdigest(),
        exit_code=int(code), selected_cases=len(results.selected), totals=dict(Counter(r["outcome"] for r in rows)), results=rows,
        upstream_calls=0, billed_cost=0,
        limits=["Disposable SQLite only; no PostgreSQL or production storage.",
                "Synthetic raw JSON provider; no model quality claim.",
                "Reporter fault injection is distinct from intentionally best-effort progress sinks.",
                "Final candidate acceptance requires CORE integration_ready plus exact commit/path."])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    lines = ["# B2 safety preparation", "", f"Revision: `{report['revision']}`; production baseline `{BASE}`.",
             "", "Not final B2 acceptance. Disposable SQLite and synthetic provider only.",
             "", f"All-case denominator {len(rows)}; outcomes: `{report['totals']}`; exit {code}.", "",
             "| Probe | Outcome | Evidence |", "|---|---|---|"]
    for row in rows:
        details = json.dumps(row["properties"], ensure_ascii=False) + ("; " + row["failure"] if row["failure"] else "")
        lines.append(f"| {row['id']} | {row['outcome']} | {details.replace('|', '/')} |")
    args.output.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print(json.dumps(dict(exit_code=int(code), totals=report["totals"], upstream_calls=0)))
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())

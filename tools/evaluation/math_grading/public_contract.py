"""Export both real FastAPI schemas in disposable directories and compare."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

BASE = "0304ff2e0cc294ada51c99af8d5ab07540cb61a8"


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def shape(value):
    if isinstance(value, dict):
        return {k: shape(v) for k, v in value.items()
                if k not in {"description", "summary", "title", "examples", "example"}}
    if isinstance(value, list):
        return [shape(v) for v in value]
    return value


def changes(before, after, path=""):
    if isinstance(before, dict) and isinstance(after, dict):
        output = []
        for key in sorted(before.keys() | after.keys()):
            here = path + "/" + key
            if key not in before or key not in after:
                output.append({"path": here, "before": before.get(key), "after": after.get(key)})
            else:
                output.extend(changes(before[key], after[key], here))
        return output
    return [] if before == after else [{"path": path, "before": before, "after": after}]


def export_at(revision, directory):
    archive = subprocess.check_output(["git", "archive", revision, "backend"])
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        bundle.extractall(directory, filter="data")
    output = Path(directory, "openapi.json")
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "TMPDIR", "LANG", "SYSTEMROOT"}}
    env.update(
        PYTHONPATH=f"{directory}:/private/tmp/math-grading-deps",
        PYTHONDONTWRITEBYTECODE="1",
        SMARTAI_RUNTIME_ENVIRONMENT="test",
        SMARTAI_DATABASE_URL=f"sqlite:///{directory}/test.db",
        SMARTAI_STORAGE_ROOT=f"{directory}/uploads",
        SMARTAI_PROVIDER_ENCRYPTION_KEY="evaluation-only-fake-master-key-0123456789",
        SMARTAI_JWT_SECRET="evaluation-only-fake-jwt-secret-0123456789",
        SMARTAI_HTTP_PROXY="", SMARTAI_HTTPS_PROXY="",
    )
    program = """
import json
from pathlib import Path
from fastapi.routing import iter_route_contexts
from backend.main import app
from backend.models import ProblemInfo
result = {"openapi": app.openapi(),
          "routes": sorted((r.path, sorted(getattr(r.original_route, "methods", []) or []))
                           for r in iter_route_contexts(app.routes)),
          "problem_schema": ProblemInfo.model_json_schema()}
Path("openapi.json").write_text(json.dumps(result, ensure_ascii=False, sort_keys=True))
"""
    process = subprocess.run([sys.executable, "-c", program], cwd=directory, env=env,
                             text=True, capture_output=True, timeout=60)
    if process.returncode:
        raise RuntimeError(f"schema export failed at {revision}: {process.stderr[-5000:]}")
    return json.loads(output.read_text())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="math-api-base-") as base_dir, \
            tempfile.TemporaryDirectory(prefix="math-api-candidate-") as candidate_dir:
        before = export_at(BASE, base_dir)
        after = export_at(args.candidate, candidate_dir)
    semantic = changes(shape(before), shape(after))
    retired = [route for route in after["routes"]
               if route[0].startswith(("/ai_grading", "/prob_preview", "/hw_preview"))]
    result = {"base": BASE, "candidate": args.candidate,
              "base_schema_sha256": fingerprint(before), "candidate_schema_sha256": fingerprint(after),
              "base_route_count": len(before["routes"]), "candidate_route_count": len(after["routes"]),
              "semantic_differences": semantic, "documentation_differences": changes(before, after),
              "retired_legacy_routes_present": retired,
              "scope": "actual app routes/OpenAPI and ProblemInfo JSON schema; not runtime acceptance",
              "passed": not semantic and not retired}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 1 if semantic or retired else 0


if __name__ == "__main__":
    raise SystemExit(main())

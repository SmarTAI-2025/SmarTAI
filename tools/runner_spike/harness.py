#!/usr/bin/env python3
"""Build, invoke, and probe the Week 1 OCI runner candidate."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable


SCHEMA_VERSION = 1
DEFAULT_IMAGE = "smartai-runner-spike:week1"
DEFAULT_PIDS_LIMIT = 16
DEFAULT_CPU_LIMIT = 0.5
DEFAULT_TMPFS_MB = 16
MAX_REQUEST_BYTES = 256 * 1024
CONTAINER_STARTUP_GRACE_SECONDS = 10.0
SPIKE_TICKET = "XTS-W1-RUNNER-SPIKE"
RUNNER_DIR = Path(__file__).resolve().parent
_SECRET_ENV_NAME = "SMARTAI_SPIKE_PRODUCTION_SECRET"
_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w.])/(?:[^/\s\"'():,]+/)+[^/\s\"'():,]+")


class RunnerSpikeError(RuntimeError):
    """A sanitized operator-facing failure from the prototype boundary."""


def find_runtime(explicit: str | None = None) -> str | None:
    """Return a Docker/Podman executable without starting a container."""

    if explicit:
        candidate = shutil.which(explicit)
        if (
            candidate is None
            and Path(explicit).is_file()
            and os.access(explicit, os.X_OK)
        ):
            candidate = str(Path(explicit).resolve())
        if candidate and Path(candidate).name in {"docker", "podman"}:
            return candidate
        return None
    return shutil.which("podman") or shutil.which("docker")


def _request_limits(request: dict[str, Any]) -> tuple[float, int, int]:
    limits = request.get("limits", {})
    if not isinstance(limits, dict):
        raise RunnerSpikeError("request limits must be an object")
    try:
        timeout_seconds = float(limits.get("timeout_seconds", 10.0))
        memory_mb = int(limits.get("memory_mb", 256))
        max_output_chars = int(limits.get("max_output_chars", 8192))
    except (TypeError, ValueError) as exc:
        raise RunnerSpikeError("request limits are invalid") from exc
    if not 0.05 <= timeout_seconds <= 60.0:
        raise RunnerSpikeError("timeout_seconds is outside the spike range")
    if not 64 <= memory_mb <= 512:
        raise RunnerSpikeError("memory_mb is outside the spike range")
    if not 256 <= max_output_chars <= 65_536:
        raise RunnerSpikeError("max_output_chars is outside the spike range")
    return timeout_seconds, memory_mb, max_output_chars


def build_container_command(
    *,
    runtime: str,
    image: str,
    request_file: Path,
    container_name: str,
    memory_mb: int,
) -> list[str]:
    """Build the auditable OCI invocation used by every spike request."""

    runtime_name = Path(runtime).name
    security_option = (
        "--security-opt=no-new-privileges"
        if runtime_name == "podman"
        else "--security-opt=no-new-privileges=true"
    )
    mount_option = (
        "--mount=type=bind,"
        f"source={request_file.resolve()},"
        "target=/input/request.json,readonly"
    )
    runtime_specific: list[str] = []
    if runtime_name == "podman":
        # Podman otherwise creates writable /tmp, /var/tmp and /run mounts for
        # a read-only container and may copy proxy variables from the host.
        runtime_specific.extend(
            [
                "--read-only-tmpfs=false",
                "--http-proxy=false",
            ]
        )
        # Required on enforcing SELinux Oracle hosts for the ephemeral bind.
        mount_option += ",relabel=private"

    return [
        runtime,
        "run",
        "--rm",
        f"--name={container_name}",
        "--network=none",
        "--read-only",
        "--ipc=none",
        "--cap-drop=ALL",
        security_option,
        *runtime_specific,
        f"--pids-limit={DEFAULT_PIDS_LIMIT}",
        f"--memory={memory_mb}m",
        f"--cpus={DEFAULT_CPU_LIMIT}",
        "--ulimit=nofile=64:64",
        "--ulimit=core=0:0",
        "--user=65534:65534",
        "--hostname=smartai-runner",
        "--workdir=/work",
        (
            "--tmpfs=/work:rw,nosuid,nodev,noexec,"
            f"size={DEFAULT_TMPFS_MB}m,mode=0700,uid=65534,gid=65534"
        ),
        mount_option,
        "--env=HOME=/work",
        "--env=LANG=C.UTF-8",
        "--env=LC_ALL=C.UTF-8",
        "--env=PYTHONDONTWRITEBYTECODE=1",
        "--env=PYTHONHASHSEED=0",
        "--env=PYTHONNOUSERSITE=1",
        "--env=TMPDIR=/work",
        image,
        "/input/request.json",
    ]


def _sanitize_runtime_error(value: str) -> str:
    value = _ABSOLUTE_PATH_RE.sub("<redacted-path>", value.strip())
    lines = value.splitlines()
    return (lines[-1] if lines else "container runtime failed")[:500]


def _parse_worker_result(stdout: str) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise RunnerSpikeError("container returned no runner result")
    try:
        result = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RunnerSpikeError("container returned an invalid runner result") from exc
    required = {
        "schema_version",
        "execution_id",
        "executed",
        "exit_reason",
        "stdout",
        "stderr",
        "duration_ms",
        "resource_status",
        "isolation_mode",
    }
    if not isinstance(result, dict) or not required.issubset(result):
        raise RunnerSpikeError("container returned an incomplete runner result")
    if result["schema_version"] != SCHEMA_VERSION:
        raise RunnerSpikeError("container returned an unsupported schema_version")
    if result["isolation_mode"] != "oci_container":
        raise RunnerSpikeError("container did not declare OCI isolation")
    return result


def _remove_container(runtime: str, container_name: str) -> None:
    try:
        subprocess.run(
            [runtime, "rm", "--force", container_name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def run_request(
    request: dict[str, Any],
    *,
    runtime: str,
    image: str = DEFAULT_IMAGE,
    launcher_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run one request with an outer wall timeout and guaranteed cleanup."""

    timeout_seconds, memory_mb, _ = _request_limits(request)
    container_name = f"smartai-runner-spike-{uuid.uuid4().hex[:16]}"
    with tempfile.TemporaryDirectory(prefix="smartai-runner-spike-") as temp_dir:
        request_file = Path(temp_dir) / "request.json"
        serialized_request = json.dumps(
            request,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(serialized_request.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise RunnerSpikeError("serialized request is too large")
        request_file.write_text(serialized_request, encoding="utf-8")
        request_file.chmod(0o444)
        command = build_container_command(
            runtime=runtime,
            image=image,
            request_file=request_file,
            container_name=container_name,
            memory_mb=memory_mb,
        )
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds + CONTAINER_STARTUP_GRACE_SECONDS,
                check=False,
                env=launcher_env,
            )
        except subprocess.TimeoutExpired as exc:
            raise RunnerSpikeError("container exceeded the outer wall timeout") from exc
        finally:
            _remove_container(runtime, container_name)
        if completed.returncode != 0:
            detail = _sanitize_runtime_error(completed.stderr)
            raise RunnerSpikeError(
                f"container runtime failed ({completed.returncode}): {detail}"
            )
        return _parse_worker_result(completed.stdout)


def _base_request(execution_id: str, code: str, *, timeout: float = 5.0) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "execution_id": execution_id,
        "task_type": "programming",
        "language": "python",
        "code": code,
        "test_cases": [],
        "limits": {
            "timeout_seconds": timeout,
            "memory_mb": 256,
            "max_output_chars": 4096,
        },
    }


def _probe_result(
    name: str,
    response: dict[str, Any],
    predicate: Callable[[dict[str, Any]], bool],
) -> dict[str, Any]:
    if response.get("exit_reason") != "success":
        return {
            "name": name,
            "passed": False,
            "detail": f"runner exit_reason={response.get('exit_reason', 'missing')}",
        }
    try:
        detail = json.loads(response.get("stdout", ""))
    except (TypeError, json.JSONDecodeError):
        return {"name": name, "passed": False, "detail": "probe returned invalid JSON"}
    passed = isinstance(detail, dict) and predicate(detail)
    return {"name": name, "passed": bool(passed), "detail": detail}


def run_probe(*, runtime: str, image: str = DEFAULT_IMAGE) -> dict[str, Any]:
    """Exercise the four Week 1 isolation acceptance properties."""

    checks: list[dict[str, Any]] = []
    launcher_env = os.environ.copy()
    launcher_env[_SECRET_ENV_NAME] = "must-not-cross-container-boundary"

    env_code = f"""import json, os
from pathlib import Path
prefixes = ("SMARTAI_", "OPENAI_", "ANTHROPIC_", "GEMINI_", "GOOGLE_",
            "ZHIPU_", "DATABASE_", "AWS_", "S3_")

def forbidden_keys(names):
    return sorted(name for name in names
                  if name.startswith(prefixes) or name == "JWT_SECRET")

child_forbidden = forbidden_keys(os.environ)
try:
    parent_entries = Path("/proc/1/environ").read_bytes().split(b"\\0")
    parent_names = [entry.split(b"=", 1)[0].decode(errors="replace")
                    for entry in parent_entries if b"=" in entry]
    parent_forbidden = forbidden_keys(parent_names)
    parent_readable = True
except OSError:
    parent_forbidden = []
    parent_readable = False
print(json.dumps({{"sentinel_present": {_SECRET_ENV_NAME!r} in os.environ,
                  "child_forbidden_keys": child_forbidden,
                  "parent_forbidden_keys": parent_forbidden,
                  "parent_readable": parent_readable}}, sort_keys=True))
"""
    network_code = """import json, socket
tcp_blocked = False
dns_blocked = False
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(0.5)
try:
    s.connect(("1.1.1.1", 53))
except OSError:
    tcp_blocked = True
finally:
    s.close()
try:
    socket.getaddrinfo("example.com", 443)
except OSError:
    dns_blocked = True
print(json.dumps({"tcp_blocked": tcp_blocked, "dns_blocked": dns_blocked},
                 sort_keys=True))
"""

    with tempfile.TemporaryDirectory(prefix="smartai-host-sentinel-") as sentinel_dir:
        sentinel = Path(sentinel_dir) / "production-only.txt"
        sentinel.write_text("must stay on host", encoding="utf-8")
        sentinel.chmod(0o600)
        directory_code = f"""import json
from pathlib import Path

def write_allowed(path):
    try:
        Path(path).write_text("probe", encoding="utf-8")
        return True
    except OSError:
        return False

host_visible = Path({str(sentinel)!r}).exists()
root_write = write_allowed("/escape.txt")
input_write = write_allowed("/input/escape.txt")
work_write = write_allowed("/work/probe.txt")
unexpected_writable = [path for path in ("/tmp/spike.txt",
                                         "/var/tmp/spike.txt",
                                         "/run/spike.txt")
                       if write_allowed(path)]
input_entries = sorted(item.name for item in Path("/input").iterdir())
print(json.dumps({{"host_visible": host_visible,
                  "root_write": root_write,
                  "input_write": input_write,
                  "work_write": work_write,
                  "unexpected_writable": unexpected_writable,
                  "input_entries": input_entries}}, sort_keys=True))
"""

        probe_specs = [
            (
                "production_env_cleared",
                env_code,
                lambda detail: detail.get("sentinel_present") is False
                and detail.get("child_forbidden_keys") == []
                and detail.get("parent_forbidden_keys") == []
                and detail.get("parent_readable") is True,
                5.0,
            ),
            (
                "network_disabled",
                network_code,
                lambda detail: detail.get("tcp_blocked") is True
                and detail.get("dns_blocked") is True,
                5.0,
            ),
            (
                "directory_isolated",
                directory_code,
                lambda detail: detail.get("host_visible") is False
                and detail.get("root_write") is False
                and detail.get("input_write") is False
                and detail.get("work_write") is True
                and detail.get("unexpected_writable") == []
                and detail.get("input_entries") == ["request.json"],
                5.0,
            ),
        ]
        for name, code, predicate, timeout in probe_specs:
            try:
                response = run_request(
                    _base_request(f"spike:{name}", code, timeout=timeout),
                    runtime=runtime,
                    image=image,
                    launcher_env=launcher_env,
                )
                checks.append(_probe_result(name, response, predicate))
            except RunnerSpikeError as exc:
                checks.append({"name": name, "passed": False, "detail": str(exc)})

    process_code = f"""import json, subprocess, sys
children = []
spawn_error = False
try:
    for _ in range({DEFAULT_PIDS_LIMIT * 4}):
        try:
            children.append(subprocess.Popen(
                [sys.executable, "-I", "-c", "import time; time.sleep(3)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ))
        except OSError:
            spawn_error = True
            break
finally:
    for child in children:
        child.terminate()
    for child in children:
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
print(json.dumps({{"configured_limit": {DEFAULT_PIDS_LIMIT},
                  "spawned": len(children),
                  "spawn_blocked": spawn_error and len(children) < {DEFAULT_PIDS_LIMIT * 4}}},
                 sort_keys=True))
"""
    try:
        response = run_request(
            _base_request("spike:process_limit", process_code, timeout=8.0),
            runtime=runtime,
            image=image,
            launcher_env=launcher_env,
        )
        checks.append(
            _probe_result(
                "process_limit",
                response,
                lambda detail: detail.get("configured_limit") == DEFAULT_PIDS_LIMIT
                and detail.get("spawn_blocked") is True
                and 0 < detail.get("spawned", DEFAULT_PIDS_LIMIT * 4)
                < DEFAULT_PIDS_LIMIT * 4,
            )
        )
    except RunnerSpikeError as exc:
        checks.append({"name": "process_limit", "passed": False, "detail": str(exc)})

    status = "passed" if len(checks) == 4 and all(c["passed"] for c in checks) else "failed"
    return {
        "schema_version": SCHEMA_VERSION,
        "ticket": SPIKE_TICKET,
        "candidate": "oci_container",
        "runtime": Path(runtime).name,
        "image": image,
        "status": status,
        "checks": checks,
    }


def build_image(*, runtime: str, image: str) -> int:
    completed = subprocess.run(
        [runtime, "build", "--tag", image, str(RUNNER_DIR)],
        stdin=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode


def _print_report(report: dict[str, Any], *, compact: bool) -> None:
    if compact:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="check for a usable OCI CLI")
    doctor.add_argument("--runtime", choices=("docker", "podman"))
    doctor.add_argument("--json", action="store_true")

    build = subparsers.add_parser("build", help="build the isolated worker image")
    build.add_argument("--runtime", choices=("docker", "podman"))
    build.add_argument("--image", default=DEFAULT_IMAGE)

    run = subparsers.add_parser("run", help="run one RunnerRequest JSON document")
    run.add_argument("request", type=Path)
    run.add_argument("--runtime", choices=("docker", "podman"))
    run.add_argument("--image", default=DEFAULT_IMAGE)
    run.add_argument("--json", action="store_true")

    probe = subparsers.add_parser("probe", help="run all Week 1 isolation probes")
    probe.add_argument("--runtime", choices=("docker", "podman"))
    probe.add_argument("--image", default=DEFAULT_IMAGE)
    probe.add_argument("--build", action="store_true")
    probe.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    runtime = find_runtime(args.runtime)
    if runtime is None:
        report = {
            "schema_version": SCHEMA_VERSION,
            "ticket": SPIKE_TICKET,
            "candidate": "oci_container",
            "status": "blocked",
            "reason": "docker/podman CLI not found",
        }
        _print_report(report, compact=getattr(args, "json", False))
        return 2

    if args.command == "doctor":
        completed = subprocess.run(
            [runtime, "version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        report = {
            "schema_version": SCHEMA_VERSION,
            "ticket": SPIKE_TICKET,
            "candidate": "oci_container",
            "runtime": Path(runtime).name,
            "status": "ready" if completed.returncode == 0 else "blocked",
            "reason": "" if completed.returncode == 0 else "runtime service is unavailable",
        }
        _print_report(report, compact=args.json)
        return 0 if completed.returncode == 0 else 2

    if args.command == "build":
        return build_image(runtime=runtime, image=args.image)

    if args.command == "run":
        try:
            request = json.loads(args.request.read_text(encoding="utf-8"))
            response = run_request(request, runtime=runtime, image=args.image)
        except (OSError, json.JSONDecodeError, RunnerSpikeError) as exc:
            _print_report(
                {"status": "failed", "reason": _sanitize_runtime_error(str(exc))},
                compact=args.json,
            )
            return 1
        _print_report(response, compact=args.json)
        return 0

    if args.build and build_image(runtime=runtime, image=args.image) != 0:
        return 1
    report = run_probe(runtime=runtime, image=args.image)
    _print_report(report, compact=args.json)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

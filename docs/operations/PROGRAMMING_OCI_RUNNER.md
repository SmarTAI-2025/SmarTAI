# Programming execution through a rootless OCI runtime

This adapter connects the existing Python grading tool to Docker/Podman. It keeps
stdin/stdout comparison, explicit function-name/argument mapping, per-test results,
and the existing grading/aggregation logic. Each test case runs in its own container.
SymPy uses the same execution transport and reports its actual isolation mode.
It does not implement C/C++ or alter the scoring rubric.

## Configuration

Install Podman with uidmap and a user-session manager (or rootless Docker) on Linux.
The **service user** needs a non-overlapping subuid/subgid range, a writable home,
and a running user manager with CPU, memory and PID controllers on cgroup v2.
No rootful socket or privileged container is needed. Build the image as that user:

```sh
podman build -t localhost/smartai-program-runner:release tools/runner_spike
podman image inspect localhost/smartai-program-runner:release --format '{{.Id}}'
```

Use the returned immutable `sha256:...` ID, not the mutable tag, in the service
configuration. For the current AWS service user UID 999, a systemd drop-in is:

```ini
[Service]
Environment=HOME=/var/lib/smartai
Environment=XDG_RUNTIME_DIR=/run/user/999
Environment=SMARTAI_PROGRAM_RUNNER_MODE=oci
Environment=SMARTAI_PROGRAM_RUNNER_RUNTIME=podman
Environment=SMARTAI_PROGRAM_RUNNER_IMAGE=sha256:REPLACE_WITH_VERIFIED_IMAGE_ID
Environment=SMARTAI_PROGRAM_RUNNER_MAX_CONCURRENT=2
Delegate=yes
```

Build and run the tests on the exact candidate before activating the drop-in.
The application release must contain the adapter. Reloading systemd alone does
not change the environment of an already-running service; activate on its normal
release restart. Keep the previous drop-in and image for rollback. Setting mode
`disabled` is an emergency stop: execution fails explicitly instead of falling
back to the application host. Existing results remain stored; restoring execution
allows **Retry failed items** on the original task without repeating recognition.

## Boundaries and verification

- Production checks rootless runtime, seccomp and cgroup v2. Images must be pinned;
  automatic image pulls are disabled. Never give the application a rootful socket.
- Network is off, root filesystem is read-only, capabilities are dropped, and
  new privileges are forbidden. Only the single request file is mounted read-only.
  No application directory, database, credentials, provider keys or runtime socket
  is mounted. The runtime CLI receives a small environment allowlist.
- Each container has 0.5 CPU, bounded memory/swap, 16 PIDs, a 16 MiB temporary work
  directory, file/output limits, and an inner deadline. Default service-wide
  concurrency is two, enforced with per-user file locks across API workers.
- Return, failure and cancellation all remove the named container before releasing
  capacity. `--rm` and the inner deadline also cover a disconnected launcher;
  after host/runtime crashes inspect containers labelled `smartai.isolated-runner`
  and remove only stale containers from this application. Do not prune unrelated
  containers or images.
- Infrastructure/protocol/runtime availability errors remain unscored and expose
  `programming_execution_unavailable`. Student timeout/runtime/output failures are
  normal per-case results. Raw daemon errors are not exposed to teachers.

Local contract tests:

```sh
python -m pytest backend/tests/test_oci_grading_runner.py backend/tests/test_grading_execution_error_projection.py backend/tests/test_production_host_execution.py -q
```

Linux runtime acceptance (disposable test database; no model calls):

```sh
SMARTAI_OCI_REAL_TEST=1 SMARTAI_PROGRAM_RUNNER_RUNTIME=podman \
SMARTAI_PROGRAM_RUNNER_IMAGE=sha256:VERIFIED_IMAGE_ID \
python -m pytest backend/tests/test_oci_real_runtime.py -q
python tools/runner_spike/harness.py probe --runtime podman --image sha256:VERIFIED_IMAGE_ID --json
podman ps -aq --filter label=smartai.isolated-runner=true
```

The adapter unit suite checks production capability gates separately; the real
runtime pytest suite uses the repository's isolated test environment. AWS candidate
acceptance must additionally run with `runtime_environment=production` under the
actual service user. The PR workflow exercises a real Docker runtime on Linux;
that workflow alone is not evidence of production rootless setup.

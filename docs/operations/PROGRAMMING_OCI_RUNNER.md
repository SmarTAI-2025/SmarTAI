# Programming execution through a rootless OCI runtime

This adapter connects the existing Python grading tool to Docker/Podman. It keeps
stdin/stdout comparison, explicit function-name/argument mapping, per-test results,
and the existing grading/aggregation logic. Each test case runs in its own container.
SymPy uses the same execution transport and reports its actual isolation mode.
Native uploads with an identified single-file main use C11 (gcc) or C++17 (g++).
Every stdin case compiles and links inside its own container, with 12 seconds per
build stage and a separate execution deadline. Only native cases mount an 8 MiB
executable tmpfs at /binary. The /work scratch mount stays noexec. Compiler and
linker diagnostics are classified inside the worker. Recognized source errors
require a successful toolchain control build and run before they can affect scoring.
Questions that specify a different standard for the submitted language require
teacher review. The native acceptance test checks the actual compiler macros
(`__STDC_VERSION__ == 201112L` and `__cplusplus == 201703L`).
Unknown failures leave the submission ungraded. Rebuild and pin the candidate
image before enabling native execution. Teacher scoring uses the existing rubric.

To replay the synthetic C/C++ U1 suite on Linux, fetch fixture commit
`ab36293b5b00ea2dd20803cd89c727398671c835` from PR #277, select the candidate
image with `SMARTAI_PROGRAM_RUNNER_IMAGE`, and run:

```sh
SMARTAI_OCI_REAL_TEST=1 SMARTAI_U1_NATIVE_EVIDENCE=/tmp/u1-native-evidence.json \
  python -m pytest -q backend/tests/test_oci_native.py
```

The output path must be new. The CI run attaches the same JSON as
`u1-native-evidence`. It records safe scoring inputs and observed per-case
stdout and outcomes for synthetic C01–C04 and X01–X04, without model calls.

## Configuration

Install Podman with uidmap and a user-session manager (or rootless Docker) on Linux.
The **service user** needs a non-overlapping subuid/subgid range, a writable home,
and a running user manager with CPU, memory and PID controllers on cgroup v2.
No rootful socket or privileged container is needed. Build the image as that user:

```sh
podman build -t localhost/smartai-program-runner:release tools/runner_spike
raw_id=$(podman image inspect localhost/smartai-program-runner:release --format '{{.Id}}')
export SMARTAI_PROGRAM_RUNNER_IMAGE="sha256:${raw_id#sha256:}"
printf '%s\n' "$SMARTAI_PROGRAM_RUNNER_IMAGE"
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
- The adapter attempts to remove the named container on return, failure and
  cancellation. A failed cleanup retains the name in its slot file. A later
  request has to confirm removal before reusing that slot. On the tested Podman
  version, a cancelled container could outlive the request until the bounded
  program exited. After a host/runtime crash, inspect containers labelled
  `smartai.isolated-runner` and remove only confirmed stale application containers.
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

## Independent task 08 acceptance under a Linux service identity

Use an empty, dedicated rootless Podman account on a disposable Linux machine. Run
the commands as that user from a checkout containing the candidate and the acceptance
tests. Install this checkout's pinned test dependencies in a venv. Resolve the
release image ID as above. For an exact rerun, retain the built image in the user's
image store. A fresh build of the Dockerfile's base tag can resolve different bytes.

The extra suite exercises the application's production-mode OCI checks. Other
application settings and SQLite are test fixtures. It launches bounded synthetic
programs, polls the real runtime, and checks cleanup before another case can reuse
a slot. Its opt-in is independent of the ordinary Docker CI smoke.

```sh
qa_status=0
qa_python=$(python -c 'import sys; print(sys.executable)')
systemd-run --user --wait --pipe --collect -p Delegate=yes -p "WorkingDirectory=$PWD" \
  --setenv=SMARTAI_OCI_ROOTLESS_QA=1 --setenv=SMARTAI_OCI_REAL_TEST=1 \
  --setenv=SMARTAI_PROGRAM_RUNNER_RUNTIME=podman \
  --setenv="SMARTAI_PROGRAM_RUNNER_IMAGE=$SMARTAI_PROGRAM_RUNNER_IMAGE" \
  "$qa_python" -m pytest -p no:cacheprovider -v -o junit_family=legacy \
  --junitxml="$HOME/oci-acceptance.xml" \
  backend/tests/test_oci_real_runtime.py backend/tests/test_oci_rootless_acceptance.py || qa_status=$?
podman ps -a --filter label=smartai.isolated-runner=true
(exit "$qa_status")
```

Preserve pytest's exit status and the JUnit report independently of the final
container listing. For individual failures, append the pytest selector
`::test_cancel_a_live_child_and_reuse_only_slot`,
`::test_08_kernel_oom_is_not_within_limits`, or
`::test_08_pid_exhaustion_has_distinct_result` to the extra suite's filename.

To rerun only the saved teacher edit and callback checks, use the same dedicated
account, pinned image and venv. The mounted tests use disposable SQLite and
fixed scoring responses. They verify both function and stdin modes after the
teacher changes `2、3 → 5` to `3、4 → 7`. The callback test gates delivery of
one real container result to check completion order and report order separately.

```sh
SMARTAI_OCI_ROOTLESS_QA=1 python -m pytest -q backend/tests/test_oci_rootless_acceptance.py \
  -k 'saved_teacher_edit_reaches_real_oci or real_oci_callbacks_follow_completion_order'
```

### 2026-11-05 candidate observations

Runner candidate `edf264c38b4fe5525f153323c7032034a9b860c3` ran on a new
ARM64 VM with an independent ext4 disk, Ubuntu 24.04.4 userspace, kernel
`6.12.114-0-virt`, two vCPUs and 4 GiB RAM. Guest service user `ociqa` (UID
1108) ran a transient delegated systemd 255 user unit. Podman 4.9.3 and crun
1.14.1 reported rootless, seccomp, cgroup v2, and delegated CPU, memory and PID
controllers. The guest had no network adapter. The network test established a
reachable loopback listener on the guest, then verified a distinct container
network namespace, loopback-only interfaces and failure to connect to that listener.

The build used the candidate Dockerfile and worker, the official Python ARM64
base manifest `sha256:df540bfd30bd380e89a349611ce42aa67bcb542d58c047706616eb6ba2f86a1b`,
and an offline wheel directory during the build. The runner image had Python
3.12.16, SymPy 1.14.0 and the following verified identities.

| Identity | SHA-256 |
| --- | --- |
| Local runner image ID supplied to every case | `293e4556f4fb75117b992aa03767617ed4516bdef94058f4acb53f47048b7446` |
| Runner image manifest | `d08096a55e692f08f7214f7432faa9147efe179ee9a277db4687dd6c11c9fcc1` |
| Worker inside the image and in the candidate | `080afd15bb6ec32f515cb900f46e97754d2b88ae76805104580fcadcf1028e17` |

The existing real-runtime suite passed all 4 tests. The additional suite passed
8 of 11. Leave the following three failed assertions visible when qualifying a fix.

| Failed requirement | Reproduced behavior |
| --- | --- |
| Live cancellation removes the container before returning | After the student appeared in `podman top`, cancelling returned `IsolatedRunnerUnavailable`. The container remained in `Stopping` and its name remained in the slot file. In a separate timed trial with a 40-second student, cancellation returned at 5.53 s from launch and an immediate next request failed at 10.65 s. The container disappeared by 41.81 s. A later request succeeded and cleared the slot at 42.29 s. The run did preserve the occupied slot. |
| Memory exhaustion reaches the case result | The 128 MiB control finished. At 64 MiB, `memory.events:oom_kill` increased by one and the student's subprocess returned `-9`. Its parent exited normally. The adapter reported `passed=true, success/within_limits`. A single-process over-allocation separately raised `MemoryError` and yielded `runtime_error/unknown`. |
| PID exhaustion reaches the case result | `pids.max=16` allowed 14 additional simultaneous children, then `fork()` returned `EAGAIN`. An uncaught failure yielded `runtime_error/unknown`; original 08 requires `pid_limit/pid_limit_exceeded`. |

Successful probes covered stdin and multiple function entries, debug stdout separate
from return values, keyword-only invocation at the transport, wrong answers, case
continuation, wall timeout and both output streams. Container evidence showed UID
65534, zero effective capabilities, no-new-privileges, seccomp mode 2, read-only
root and input, a 16 MiB restricted work mount, 0.5 CPU, zero swap and 64 file
descriptors. A synthetic guest file and environment canary were absent. Four requests
peaked at two containers with separate stdin, files and result IDs. Two independent
backend processes shared one slot with disjoint execution intervals. A missing
runtime or pinned image produced an infrastructure error and left the host marker
absent. The final inventory was empty after the bounded programs exited.

The separate legacy `harness.py probe` reported 3/4. Its environment predicate
requires `/proc/1/environ` to be readable, while the candidate supervisor deliberately
disables that access. The child had no forbidden environment keys, the rootless
suite checked the synthetic key against the live container's configured environment,
and the legacy directory, network and PID probes passed. Preserve this discrepancy
when interpreting that command's exit code.

Synthetic C and C++ requests both returned zero cases and an unsupported-language summary.
This evidence covers local Linux Python execution. X2 C/C++ compilation, the
complete versioned 08 result ABI, the integrated grading service and the final AWS
candidate require separate acceptance. AWS verification should establish the
actual service account, unit and delegation, architecture/kernel, LSM and seccomp
profiles, subordinate ID ranges, rootless storage, release image and installed
worker identity. The QA VM had both AppArmor and SELinux disabled. The recorded
local image ID belongs to this build.

To check the candidate using the service user's own environment, run
`python -m backend.tools.programming_readiness`. The command checks stdin and
a keyword-only function through the configured production adapter. Its JSON
reports disabled, unavailable, or ready, the runtime name, the pinned digest,
the contract version and verified Python modes. Run this command in a
disposable acceptance deployment first and record the candidate SHA with
its output.

### Retest against `bb6108cec139b894f5a5bda657d244b6919ca2ff`

On the same disposable Linux disk and service identity, the Podman
`rm --force --time=0` change returned `CancelledError` within 0.16–0.25 s of
three cancellations witnessed after the student process started. These three
controlled trials held the initial image constant. The name and slot were empty
on return, and every immediate follow-up passed. The cancellation test also passed
with the rebuilt intermediate worker and again with the exact candidate below.

The final local image ID was
`sha256:dfc18d58b2ea232327342a1ec7123e60f8203198d9696b998b975aae7f51f412`,
its local manifest was
`sha256:f7773b8da300a17fac5d365470f52d41a06dad074871bbf0dfe10beb314819e2`,
and its worker SHA-256 matched the candidate
(`89f3cd16d6b4634ee73f5b1370c4b83a4fe4ee33401ff39a45c9220c2e8f4b0d`).
All three earlier failures passed. The 64 MiB case's parent exited 0, a child
was killed, and the adapter reported `memory_limit/memory_limit_exceeded` with
one kernel OOM kill. Caught and uncaught PID denials reported
`pid_limit/pid_limit_exceeded` with a positive kernel denial counter and
different process exit codes. An unattributed SIGKILL yielded an infrastructure error.

The pristine candidate's CLI run yielded 15 passes and one failed assertion.
That assertion had required a caught PID denial to produce success. The updated
acceptance test requires a resource failure despite the student's zero exit, a
trusted denial counter and the same bounded diagnostic stdout. With this
correction the original 15 plus the candidate's new SIGKILL/PID test passed.
Nine additional function-diagnosis tests yielded seven passes and two failures
(23/25 overall). The remaining cases both report `runtime_error`.

| Input and requested call | Required case result | Observed boundary |
| --- | --- | --- |
| `from __future__ import annotations`, followed by `def solve(xs: list[int]) -> int: return len(xs)`, call `solve()` | `signature_mismatch` | The static inspector rejects the subscript annotation even though annotation evaluation is deferred. The control call `solve([1,2])` returned 2. |
| `import random`, followed by `def helper(): return 2`, call `solve()` | `target_function_missing` | The inspector exits when it encounters a module outside its standard-library allowlist, before looking up the target. The control call `helper()` returned 2 in the same image. |

The matrix distinguishes a plain missing function, missing keyword-only arguments,
a positional-only argument passed by keyword, a TypeError inside a valid function,
module failure before lookup, malformed source, and a valid variadic call.
All counts above refer to local rootless Linux. Rerun both suites on a fixed
candidate and retain the raw JUnit before qualifying the intended AWS identity.

### Integrated candidate `1897b387ab166b154ee138d97b01c9eef48aacf3`

On 2026-11-05 the same offline ARM64 VM and delegated UID 1108 rebuilt the
exact worker (SHA-256 `35f8c5b329dc9add71096c967b6982434529865661ea0feec4d32bda6cdd1c9f`).
Podman ran image ID `sha256:0be2ceccff9c3855c97241e7cf5c6aae5e48c8e9e916ec9cde9f2d7fce04f924`,
local manifest `sha256:b1c3e6a687e1ac32d0e0569fa8f23fe8807f4c6a6f0da6007308d4d8b7859cb4`.
Both test files matched the frozen suite byte for byte. All 25 passed in 29.77 s
with zero skips. A separate rerun of the existing five real-runtime tests passed
in 9.40 s. The deferred-annotation call now reports `signature_mismatch`, and the
absent entry after `import random` reports `target_function_missing`.

The service-user readiness CLI returned `ready` (exit 0, Python stdin and
function), `unavailable` for a missing image (exit 1), and `disabled` (exit 0).
Eleven output checks verified debug/return separation, NUL escaping, falsey
values, Unicode return boundaries and error precedence. An import-error trace
was 394 characters: at a 256-character cap the result was `output_limit` with
truncated stderr; at 8192 it was `runtime_error`. Final Podman inventory was
empty, including stopped containers, and both slot files were empty.

The separate Week 1 probe still returns exit 1 (3/4). Reproduce as the same
user with `python tools/runner_spike/harness.py probe --runtime podman --image
"$SMARTAI_PROGRAM_RUNNER_IMAGE" --json`. Its environment predicate expects
`parent_readable=true`; the protected supervisor returned `false`. The child
environment and sentinel checks showed no forbidden key. The frozen suite also
verified the live container's configured environment via Podman inspection.

### Input eligibility for AI test evidence

A teacher reference supplies an expected result only after the proposed input has been checked against the frozen question and rubric. The same check applies to supplemental boundary observations. It reads declared argument names, simple scalar and list types, numeric comparisons and intervals, and finite integer/string choices. Function arguments bind to the teacher's signature. Standard input uses the declared scalar or record format. Output descriptions and conditional branches do not establish an input type or precondition.

Unsupported requirements such as graph properties, permutations or implicit relationships require teacher-supplied cases. Extra inputs whose eligibility is unknown or contradicted do not produce a public boundary observation. For a fresh run, teacher-provided examples retain their exact inputs and expected results. If AI proposals are excluded, the private per-question audit records their original positions, inputs, supplied expectations and reasons. A mixed suite continues with the teacher examples and any verified AI inputs; feedback reports the omissions. An incomplete AI-only suite stays ungraded until the teacher edits or adds usable inputs and expectations in the existing test-case editor. Saving changed inputs or expectations establishes teacher authorship for those cases; original AI material remains in the private question source audit. Merely confirming the question, changing labels or visibility, reordering, or resubmitting a source label does not approve AI cases. Restoring an original AI case retains its AI attribution. Previous grade snapshots are preserved.

For fresh input-only proposals with an independent reference, the class can repair a provably invalid input before freezing. The original proposal and all validated positions are retained. For scalar integer inputs the grader first tries distinct neighbors of known legal values and checks each replacement against the same declared domain. Other input shapes may use the single existing format-repair attempt. A failed repair or unverified domain leaves the incomplete AI-only suite ungraded until the teacher edits or adds usable cases. This path cannot rewrite a teacher input, an assigned expectation, or a previously frozen test.

"""Opt-in real Linux OCI smoke; no provider/API key, no production data."""
import os
import pytest
from backend.config import settings
from backend.models import TestCase as Case
from backend.tools.code_interpreter import run_sandbox
from backend.tools.oci_grading_runner import run_python_oci

pytestmark = pytest.mark.skipif(os.getenv("SMARTAI_OCI_REAL_TEST") != "1", reason="Requires explicitly configured Linux OCI runtime")

@pytest.fixture(autouse=True)
def oci(monkeypatch):
    monkeypatch.setattr(settings, "program_runner_mode", "oci")
    monkeypatch.setattr(settings, "program_runner_image", os.environ.get("SMARTAI_PROGRAM_RUNNER_IMAGE", ""))
    monkeypatch.setattr(settings, "program_runner_runtime", os.environ.get("SMARTAI_PROGRAM_RUNNER_RUNTIME", "docker"))

@pytest.mark.asyncio
async def test_real_stdin_and_multiple_functions():
    report = await run_sandbox("print(sum(map(int,input().split())))", [Case(input="1 2", expected_output="3"), Case(input="2 2", expected_output="3")])
    assert [r.passed for r in report.results] == [True, False]
    report = await run_sandbox("def first(x): return 0\ndef second(x): return x*2", [Case(function_name="second", function_args=[3], expected_return="6")])
    assert report.passed_count == 1

@pytest.mark.asyncio
async def test_real_timeout_output_and_student_runtime_error():
    assert (await run_python_oci("while True: pass", timeout=.2)).exit_reason == "timeout"
    assert (await run_python_oci("print('x'*10000)", max_output_chars=256)).exit_reason == "output_limit"
    assert (await run_python_oci("raise ValueError('student error')")).exit_reason == "runtime_error"


@pytest.mark.asyncio
async def test_real_python_literal_return_types_and_wrong_value_controls():
    from backend.tests.test_programming_return_transport import LITERALS
    for value, expected in LITERALS:
        report = await run_sandbox(f"def f(): return {value}", [
            Case(function_name="f", function_args=[], expected_return=expected),
            Case(function_name="f", function_args=[], expected_return="'wrong value'"),
        ])
        assert [r.passed for r in report.results] == [True, False]
        assert [r.exit_reason for r in report.results] == ["success", "wrong_answer"]
    with pytest.raises(ValueError, match="^programming_return_value_unavailable$"):
        await run_sandbox("def f(): return frozenset({1, 2})", [
            Case(function_name="f", function_args=[], expected_return="{1, 2}")])
    assert (await run_python_oci("print('slot reused')")).passed

@pytest.mark.asyncio
async def test_real_no_secrets_or_network():
    result = await run_python_oci("""import os,socket
assert not any(k.startswith(('SMARTAI_', 'AWS_', 'GEMINI_')) for k in os.environ)
s=socket.socket();s.settimeout(.3)
try: s.connect(('1.1.1.1',53))
except OSError: print('isolated')
else: raise AssertionError('network unexpectedly available')
""")
    assert result.passed and result.actual_output.strip() == "isolated"


@pytest.mark.asyncio
async def test_child_cannot_access_supervisor_protocol_descriptor():
    result = await run_python_oci(
        "import os\n"
        "try:\n fd=os.open('/proc/1/fd/1',os.O_WRONLY)\n"
        "except (PermissionError, FileNotFoundError):\n print('protected')\n"
        "else:\n os.close(fd); print('accessible')"
    )
    assert result.passed and result.actual_output.strip() == "protected"


@pytest.mark.asyncio
async def test_real_private_pids_controller_and_unattributed_sigkill():
    # The cap is set by the trusted container command. At least one fork must
    # fail; the code deliberately leaves that exception unhandled.
    pids = await run_python_oci("import subprocess\nchildren=[]\nfor i in range(32):\n children.append(subprocess.Popen(['sleep','2']))",
                                bind_request=True)
    assert pids.exit_reason == "pid_limit" and pids.cgroup_pid_denials > 0
    from backend.tools.oci_grading_runner import IsolatedRunnerUnavailable
    with pytest.raises(IsolatedRunnerUnavailable):
        await run_python_oci("import os,signal; os.kill(os.getpid(),signal.SIGKILL)", bind_request=True)

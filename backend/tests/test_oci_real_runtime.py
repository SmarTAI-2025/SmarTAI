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
async def test_real_no_secrets_or_network():
    result = await run_python_oci("""import os,socket
assert not any(k.startswith(('SMARTAI_', 'AWS_', 'GEMINI_')) for k in os.environ)
s=socket.socket();s.settimeout(.3)
try: s.connect(('1.1.1.1',53))
except OSError: print('isolated')
else: raise AssertionError('network unexpectedly available')
""")
    assert result.passed and result.actual_output.strip() == "isolated"

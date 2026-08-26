from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_pdf_worker_protocol_is_utf8_when_stdio_uses_cp936():
    probe = (
        "from backend.tools._pdf_worker import _write\n"
        "_write({'status': 'ok', "
        "'text': '\\u4e2d\\u6587 \\u2212 \\u2a7e \\uffff'})\n"
    )
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "cp936"

    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        timeout=10,
    )

    assert completed.stderr == b""
    assert json.loads(completed.stdout.decode("utf-8")) == {
        "status": "ok",
        "text": "中文 − ⩾ \uffff",
    }

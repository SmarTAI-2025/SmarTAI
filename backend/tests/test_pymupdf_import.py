"""The PDF tool must not import the unrelated legacy ``fitz`` package."""
import os
import subprocess
import sys
from pathlib import Path


def test_pdf_processing_works_when_legacy_fitz_import_is_unavailable():
    script = '''
import asyncio
import sys
sys.modules["fitz"] = None
import pymupdf
from backend.tools import file_processing
assert file_processing.fitz is pymupdf
with pymupdf.open() as document:
    for number in range(1, 4):
        page = document.new_page(width=300, height=400)
        page.insert_text((20, 40), f"Synthetic PDF page {number}. " + "Selectable prose. " * 5)
    body = document.tobytes()
images = file_processing._render_pdf_pages_for_ocr(body, "local.pdf")
assert [image.label for image in images] == [f"local.pdf page {n}" for n in range(1, 4)]
assert all(image.data.startswith(b"\\x89PNG") for image in images)
text, count = asyncio.run(file_processing._extract_pdf_payload(body))
assert count == 3 and "Synthetic PDF page 3" in text
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "SMARTAI_RUNTIME_ENVIRONMENT": "test"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

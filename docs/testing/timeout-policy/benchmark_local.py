"""Synthetic local file benchmark; no login, network, student data or LLM calls.
Run from repo root: PYTHONPATH=. python docs/testing/timeout-policy/benchmark_local.py
"""
import asyncio
import io
import json
import platform
import random
import time
import zipfile
import pymupdf as fitz
from PIL import Image
from backend.tools.file_processing import extract_text_from_pdf
from backend.tools.media_validation import validate_media_content
from backend.tools.pdf_evidence import (read_pdf_evidence, read_image_evidence,
    PdfIndexRequest, PdfRenderRequest, PdfContactSheetRequest, ImagePrepareRequest)
from backend.knowledge.ingestion import native_units


def fixtures():
    native = fitz.open()
    for _ in range(500):
        page = native.new_page()
        page.insert_text((40, 40), 'Synthetic exercise: x + 2 = 5. Explain your answer.\n' * 10)
    png = io.BytesIO()
    Image.new('RGB', (4000, 4000), 'white').save(png, format='PNG')
    scan = fitz.open()
    for _ in range(200):
        page = scan.new_page()
        page.insert_image(page.rect, stream=png.getvalue())
    vector = fitz.open(); page = vector.new_page()
    shape = page.new_shape()
    for n in range(20000):
        x, y = n % 500, n % 700
        shape.draw_rect(fitz.Rect(x, y, x+2, y+2))
    shape.finish(color=(0, 0, 0)); shape.commit()
    docx = io.BytesIO()
    with zipfile.ZipFile(docx, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('word/document.xml', '<document><body>' + ('<p><t>' + 'x' * 200 + '</t></p>') * 30000 + '</body></document>')
    pptx = io.BytesIO()
    with zipfile.ZipFile(pptx, 'w', zipfile.ZIP_DEFLATED) as z:
        for n in range(1, 1001):
            z.writestr(f'ppt/slides/slide{n}.xml', '<slide><p><t>' + 'Synthetic text. ' * 100 + '</t></p></slide>')
    # Near the product's 16 MP limit, with poorly compressible image detail.
    noise = io.BytesIO()
    Image.frombytes('L', (4000, 4000), random.Random(42).randbytes(16_000_000)).point(lambda v: 240 + v // 32).save(noise, format='JPEG', quality=80)
    dense_scan = fitz.open()
    for n in range(200):
        page = dense_scan.new_page()
        page.insert_image(page.rect, stream=noise.getvalue())
        page.insert_text((40, 40), f'Synthetic scanned page {n + 1}')
    return native.tobytes(), scan.tobytes(), vector.tobytes(), png.getvalue(), docx.getvalue(), pptx.getvalue(), noise.getvalue(), dense_scan.tobytes()


async def main():
    native, scan, vector, png, docx, pptx, noise, dense_scan = fixtures()
    cases = {
        'native_pdf_500pages_inspect': lambda: validate_media_content(native, 'application/pdf'),
        'native_pdf_500pages_text': lambda: extract_text_from_pdf(native, max_pages=500),
        'scanned_pdf_200pages_index': lambda: read_pdf_evidence(scan, PdfIndexRequest(window_pages=500)),
        'scanned_pdf_contact8': lambda: read_pdf_evidence(scan, PdfContactSheetRequest(pages=list(range(1, 9)))),
        'vector_pdf_20000_rectangles_render': lambda: read_pdf_evidence(vector, PdfRenderRequest(page_number=1)),
        'image_16mp_prepare': lambda: read_image_evidence(png, ImagePrepareRequest(content_type='image/png')),
        'docx_6m_chars': lambda: native_units('synthetic.docx', docx),
        'pptx_1000slides': lambda: native_units('synthetic.pptx', pptx),
        'noise_image_16mp_prepare': lambda: read_image_evidence(noise, ImagePrepareRequest(content_type='image/jpeg')),
        'noise_scan_200pages_inspect': lambda: validate_media_content(dense_scan, 'application/pdf'),
        'noise_scan_contact8': lambda: read_pdf_evidence(dense_scan, PdfContactSheetRequest(pages=list(range(1, 9)))),
    }
    output = {'host': platform.platform(), 'python': platform.python_version(), 'samples': {}, 'noise_image_bytes': len(noise), 'noise_scan_bytes': len(dense_scan)}
    for name, invoke in cases.items():
        durations = []
        for _ in range(3):
            start = time.perf_counter()
            await invoke()
            durations.append(round(time.perf_counter() - start, 4))
        output['samples'][name] = {'seconds': durations, 'max_seconds': max(durations)}
    async def timed(invoke):
        start = time.perf_counter(); await invoke(); return round(time.perf_counter()-start, 4)
    output['two_concurrent_image_workers_seconds'] = await asyncio.gather(*[timed(cases['noise_image_16mp_prepare']) for _ in range(2)])
    print(json.dumps(output, indent=2))

if __name__ == '__main__':
    asyncio.run(main())

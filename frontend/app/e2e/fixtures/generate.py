"""Generate synthetic, non-sensitive browser fixtures with the locked PyMuPDF."""
from pathlib import Path
import pymupdf as fitz

root = Path(__file__).parent
for name, count in [('single', 1), ('multiple', 15), ('long', 240), ('mixed', 20)]:
    with fitz.open() as document:
        for i in range(1, count + 1):
            landscape = name == 'mixed' and i % 2 == 0
            w, h = (842, 595) if landscape else (595, 842)
            page = document.new_page(width=w, height=h)
            page.draw_rect(fitz.Rect(24, 24, w-24, h-24), color=(0.28, 0.62, 0.9), width=2)
            page.insert_text((45, 70), f'SYNTHETIC PDF / {name} / PAGE {i} OF {count}', fontsize=19, fontname='hebo')
            page.insert_text((45, 112), '中文预览：连续阅读、题目与公式', fontname='china-s', fontsize=16)
            # HTML embeds fallback fonts; the Chinese line above exercises CMaps.
            page.insert_htmlbox(fitz.Rect(45, 135, w-45, 220), '<p style="font-size:20px">∫₀¹ 2x dx = 1　　α² + β² ≤ 1　　√(x²) = |x|</p>')
            for row in range(8):
                y=250+row*32
                if y > h-70: break
                page.insert_text((45, y), f'{i}.{row+1}  Local preview fixture. Read downward to the next page.', fontsize=12, fontname='tiro')
            page.insert_text((45, h-48), f'END OF PAGE {i} / NEXT {i+1 if i<count else "END"}', fontsize=16)
        document.subset_fonts()
        document.save(root / f'{name}.pdf', garbage=4, deflate=True)
(root / 'broken.pdf').write_bytes(b'%PDF-1.7\nsynthetic invalid local test file\n')

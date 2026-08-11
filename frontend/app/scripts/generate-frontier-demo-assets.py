#!/usr/bin/env python3
"""Generate deterministic, synthetic assets for the public Frontier demo."""

from __future__ import annotations

import json
import hashlib
import math
import os
import random
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter
from reportlab.lib.colors import Color, HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "public" / "frontier-demo"
LIVE_OUTPUT = OUTPUT / "live"
TEX_SOURCE = ROOT / "assets" / "frontier-demo" / "tex"
STIX = Path("/System/Library/Fonts/Supplemental/STIXTwoText.ttf")
STIX_BOLD = Path("/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf")
STIX_ITALIC = Path("/System/Library/Fonts/Supplemental/STIXTwoText-Italic.ttf")
HAND = Path("/System/Library/Fonts/Noteworthy.ttc")
HAND_FALLBACK = Path("/System/Library/Fonts/Supplemental/Bradley Hand Bold.ttf")
MATH = Path("/System/Library/Fonts/Supplemental/STIXGeneralItalic.otf")

INK = HexColor("#17202A")
BLUE = HexColor("#2457D6")
RED = HexColor("#D94A3A")
PALE = HexColor("#F4F6F2")
RULE = HexColor("#D7D9D2")
MUTED = HexColor("#667085")

ZIP_TIMESTAMP = (2026, 8, 12, 0, 0, 0)
SOURCE_DATE_EPOCH = "1786492800"


def pdf_canvas(path: Path) -> canvas.Canvas:
    """Return a byte-for-byte reproducible PDF canvas."""
    return canvas.Canvas(
        str(path),
        pagesize=A4,
        pageCompression=1,
        invariant=1,
    )


def compile_latex_asset(source_name: str, output: Path) -> Path:
    """Compile a real LaTeX fixture without leaving build by-products in public/."""
    source = TEX_SOURCE / source_name
    latexmk = shutil.which("latexmk")
    if latexmk is None:
        mac_latexmk = Path("/Library/TeX/texbin/latexmk")
        latexmk = str(mac_latexmk) if mac_latexmk.exists() else None
    if latexmk is None:
        raise RuntimeError("latexmk is required to regenerate Frontier LaTeX fixtures")
    tmp_root = ROOT / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    environment = {
        **os.environ,
        "SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH,
        "FORCE_SOURCE_DATE": "1",
    }
    with tempfile.TemporaryDirectory(prefix="frontier-latex-", dir=tmp_root) as build_dir:
        subprocess.run(
            [
                latexmk,
                "-pdf",
                "-interaction=nonstopmode",
                "-halt-on-error",
                f"-outdir={build_dir}",
                str(source),
            ],
            cwd=source.parent,
            env=environment,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        compiled = Path(build_dir) / f"{source.stem}.pdf"
        if not compiled.is_file():
            raise RuntimeError(f"LaTeX compilation did not produce {compiled.name}")
        shutil.copyfile(compiled, output)
    return output


def register_fonts() -> None:
    pdfmetrics.registerFont(TTFont("STIXText", str(STIX)))
    pdfmetrics.registerFont(TTFont("STIXBold", str(STIX_BOLD)))
    pdfmetrics.registerFont(TTFont("STIXItalic", str(STIX_ITALIC)))


def wrap_lines(text: str, font: str, size: float, max_width: float) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if pdfmetrics.stringWidth(candidate, font, size) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def paragraph(
    pdf: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    width: float,
    *,
    font: str = "STIXText",
    size: float = 10.5,
    leading: float = 14.5,
    color=INK,
) -> float:
    pdf.setFont(font, size)
    pdf.setFillColor(color)
    for line in wrap_lines(text, font, size, width):
        pdf.drawString(x, y, line)
        y -= leading
    return y


def page_frame(pdf: canvas.Canvas, title: str, subtitle: str, page: int, total: int) -> float:
    width, height = A4
    pdf.setFillColor(PALE)
    pdf.rect(0, 0, width, height, stroke=0, fill=1)
    pdf.setFillColor(INK)
    pdf.setFont("STIXBold", 18)
    pdf.drawString(50, height - 62, title)
    pdf.setFillColor(MUTED)
    pdf.setFont("STIXText", 9.5)
    pdf.drawString(50, height - 82, subtitle)
    pdf.setStrokeColor(RULE)
    pdf.line(50, height - 96, width - 50, height - 96)
    pdf.setFont("Helvetica", 7.5)
    pdf.setFillColor(MUTED)
    pdf.drawString(50, 28, "SYNTHETIC DEMO DOCUMENT - no real student data")
    pdf.drawRightString(width - 50, 28, f"Page {page} of {total}")
    return height - 126


def question_block(pdf: canvas.Canvas, y: float, label: str, title: str, points: int, prompt: str, rubric: list[str]) -> float:
    width, _ = A4
    pdf.setFillColor(BLUE)
    pdf.roundRect(50, y - 22, 38, 22, 5, stroke=0, fill=1)
    pdf.setFillColor(HexColor("#FFFFFF"))
    pdf.setFont("Helvetica-Bold", 8.5)
    pdf.drawCentredString(69, y - 15, label)
    pdf.setFillColor(INK)
    pdf.setFont("STIXBold", 13)
    pdf.drawString(100, y - 15, title)
    pdf.setFont("Helvetica-Bold", 8)
    pdf.setFillColor(MUTED)
    pdf.drawRightString(width - 50, y - 15, f"{points} points")
    y -= 42
    y = paragraph(pdf, prompt, 58, y, width - 116, size=11, leading=16)
    y -= 8
    pdf.setFillColor(HexColor("#E9EEF9"))
    pdf.roundRect(58, y - (len(rubric) * 17 + 16), width - 116, len(rubric) * 17 + 16, 7, stroke=0, fill=1)
    pdf.setFillColor(INK)
    pdf.setFont("Helvetica-Bold", 7.5)
    pdf.drawString(70, y - 13, "RUBRIC SIGNALS")
    pdf.setFont("STIXText", 9)
    for index, item in enumerate(rubric):
        pdf.setFillColor(BLUE)
        pdf.circle(74, y - 31 - index * 17, 2, stroke=0, fill=1)
        pdf.setFillColor(INK)
        pdf.drawString(84, y - 34 - index * 17, item)
    return y - len(rubric) * 17 - 42


def generate_assignment() -> Path:
    path = OUTPUT / "assignment_mixed_stem.pdf"
    pdf = pdf_canvas(path)
    y = page_frame(pdf, "STEM Reasoning Lab", "Mixed Assessment - Questions and rubric signals", 1, 3)
    y = question_block(
        pdf, y, "Q1", "Calculus", 5,
        "Evaluate integral_0^1 x exp(x^2) dx. Show the substitution and the transformed bounds.",
        ["u = x^2", "the 1/2 factor", "bounds and final evaluation"],
    )
    question_block(
        pdf, y, "Q2", "Mechanics", 8,
        "A 2 kg block slides 3 m from rest down a 30 degree incline with mu_k = 0.20. Find its acceleration and final speed. Use g = 9.81 m/s^2.",
        ["force resolution", "friction direction", "acceleration", "speed and units"],
    )
    pdf.showPage()
    y = page_frame(pdf, "STEM Reasoning Lab", "Mixed Assessment - Questions and rubric signals", 2, 3)
    question_block(
        pdf, y, "Q3", "Linear algebra proof", 7,
        "For any real m x n matrix A, prove ker(A) = ker(A^T A), then conclude rank(A) = rank(A^T A). Do not assume A is square or invertible.",
        ["forward inclusion", "norm identity for the reverse inclusion", "rank-nullity"],
    )
    pdf.setFillColor(HexColor("#DCE9DE"))
    pdf.roundRect(58, 320, A4[0] - 116, 105, 7, stroke=0, fill=1)
    pdf.setFillColor(INK)
    pdf.setFont("STIXBold", 10)
    pdf.drawString(72, 401, "REFERENCE IDENTITY")
    pdf.setFont("STIXItalic", 17)
    pdf.drawString(92, 368, "x^T A^T A x = (Ax)^T(Ax) = ||Ax||^2")
    pdf.setFont("STIXText", 9)
    pdf.drawString(72, 341, "The identity is supplied as a grading reference, not as part of the student prompt.")
    pdf.showPage()
    y = page_frame(pdf, "STEM Reasoning Lab", "Mixed Assessment - Questions and rubric signals", 3, 3)
    y = question_block(
        pdf, y, "Q4", "Programming", 10,
        "Implement stable_softmax(xs). Return [] for empty input and avoid overflow for values near 1000. Do not use NumPy.",
        ["empty input", "subtract maximum", "normalization", "extreme-value behavior"],
    )
    pdf.setFillColor(HexColor("#1E293B"))
    pdf.roundRect(58, 270, A4[0] - 116, 154, 7, stroke=0, fill=1)
    pdf.setFillColor(HexColor("#E2E8F0"))
    pdf.setFont("Courier", 8.3)
    tests = [
        "[]                         -> []",
        "[0, 0]                     -> [0.5, 0.5]",
        "[1, 2, 3]                  -> [0.09003, 0.24473, 0.66524]",
        "[1000, 1000]               -> [0.5, 0.5]",
        "[-1000, 0, 1000]           -> approximately [0, 0, 1]",
    ]
    for index, line in enumerate(tests):
        pdf.drawString(75, 392 - index * 23, line)
    pdf.setFillColor(MUTED)
    pdf.setFont("Helvetica", 7.5)
    pdf.drawString(58, 244, "The public demo shows frozen test outcomes; it never executes visitor code.")
    pdf.save()
    return path


def generate_live_assignment() -> Path:
    path = LIVE_OUTPUT / "question_source.pdf"
    pdf = pdf_canvas(path)
    pages = [
        (
            "Q1 - Calculus (5 points)",
            "Evaluate integral_0^1 x exp(x^2) dx. Show the substitution and the transformed bounds.",
            "Q2 - Mechanics (8 points)",
            "A 2 kg block slides 3 m from rest down a 30 degree incline with mu_k = 0.20. Find its acceleration and final speed. Use g = 9.81 m/s^2.",
        ),
        (
            "Q3 - Linear algebra (7 points)",
            "For any real m x n matrix A, prove ker(A) = ker(A^T A), then conclude rank(A) = rank(A^T A). Do not assume A is square or invertible.",
            "",
            "",
        ),
        (
            "Q4 - Programming (10 points)",
            "Implement stable_softmax(xs). Return [] for empty input and avoid overflow for values near 1000. Do not use NumPy.",
            "Required signature",
            "def stable_softmax(xs):",
        ),
    ]
    for index, (first_title, first_copy, second_title, second_copy) in enumerate(pages, start=1):
        y = page_frame(pdf, "STEM Reasoning Lab", "Raw synthetic question source - no answer key", index, len(pages))
        pdf.setFillColor(BLUE)
        pdf.setFont("Helvetica-Bold", 9)
        pdf.drawString(55, y, first_title)
        y = paragraph(pdf, first_copy, 55, y - 34, A4[0] - 110, size=12, leading=19)
        if second_title:
            y -= 55
            pdf.setFillColor(BLUE)
            pdf.setFont("Helvetica-Bold", 9)
            pdf.drawString(55, y, second_title)
            if second_title == "Required signature":
                code_block(pdf, [second_copy], 55, y - 24, A4[0] - 110)
            else:
                paragraph(pdf, second_copy, 55, y - 34, A4[0] - 110, size=12, leading=19)
        pdf.showPage()
    pdf.save()
    return path


def generate_live_rubric() -> Path:
    path = LIVE_OUTPUT / "teacher_rubric_reference.pdf"
    pdf = pdf_canvas(path)
    y = page_frame(pdf, "Teacher Rubric Reference", "Raw synthetic teacher material - separate from student submissions", 1, 2)
    rubrics = [
        ("Q1 - 5 points", ["1: chooses u=x^2", "2: carries the 1/2 factor", "2: transforms bounds and evaluates (e-1)/2"]),
        ("Q2 - 8 points", ["2: resolves gravity and normal force", "2: friction opposes motion", "2: a is approximately 3.20 m/s^2", "2: v is approximately 4.38 m/s with units"]),
    ]
    for title, items in rubrics:
        pdf.setFillColor(BLUE)
        pdf.setFont("Helvetica-Bold", 9)
        pdf.drawString(55, y, title)
        y -= 26
        for item in items:
            y = paragraph(pdf, f"- {item}", 70, y, A4[0] - 140, size=10.5, leading=16)
        y -= 28
    pdf.showPage()
    y = page_frame(pdf, "Teacher Rubric Reference", "Raw synthetic teacher material - separate from student submissions", 2, 2)
    rubrics = [
        ("Q3 - 7 points", ["2: proves ker(A) subset ker(A^T A)", "3: uses x^T A^T A x = ||Ax||^2 for the reverse inclusion", "2: concludes equal rank by rank-nullity"]),
        ("Q4 - 10 points", ["2: returns [] for empty input", "3: subtracts max(xs)", "2: normalizes correctly", "3: passes large-value tests"]),
    ]
    for title, items in rubrics:
        pdf.setFillColor(BLUE)
        pdf.setFont("Helvetica-Bold", 9)
        pdf.drawString(55, y, title)
        y -= 26
        for item in items:
            y = paragraph(pdf, f"- {item}", 70, y, A4[0] - 140, size=10.5, leading=16)
        y -= 28
    pdf.save()
    return path


def answer_heading(pdf: canvas.Canvas, y: float, label: str, score: str) -> float:
    pdf.setFillColor(BLUE)
    pdf.setFont("Helvetica-Bold", 8.5)
    pdf.drawString(55, y, label)
    pdf.setFillColor(MUTED)
    pdf.drawRightString(A4[0] - 55, y, score)
    pdf.setStrokeColor(RULE)
    pdf.line(55, y - 8, A4[0] - 55, y - 8)
    return y - 31


def code_block(pdf: canvas.Canvas, lines: list[str], x: float, y: float, width: float) -> float:
    height = len(lines) * 15 + 22
    pdf.setFillColor(HexColor("#1E293B"))
    pdf.roundRect(x, y - height, width, height, 6, stroke=0, fill=1)
    pdf.setFillColor(HexColor("#E2E8F0"))
    pdf.setFont("Courier", 8.5)
    for index, line in enumerate(lines):
        pdf.drawString(x + 14, y - 20 - index * 15, line)
    return y - height


def generate_typeset_submission() -> Path:
    path = OUTPUT / "DEMO-001_typeset.pdf"
    pdf = pdf_canvas(path)
    y = page_frame(pdf, "Alex Chen - Submission", "Student ID: DEMO-001 | Typeset response - synthetic student", 1, 2)
    y = answer_heading(pdf, y, "Q1 - Calculus", "5 / 5")
    y = paragraph(pdf, "Let u = x^2, so du = 2x dx. Therefore I = (1/2) integral_0^1 exp(u) du = (e - 1)/2.", 60, y, A4[0] - 120, size=12, leading=18)
    y -= 35
    y = answer_heading(pdf, y, "Q2 - Mechanics", "8 / 8")
    y = paragraph(pdf, "N = mg cos 30 deg. Along the slope, ma = mg sin 30 deg - mu_k N, so a = 9.81(sin 30 deg - 0.20 cos 30 deg) = 3.20 m/s^2.", 60, y, A4[0] - 120, size=11, leading=17)
    y -= 12
    paragraph(pdf, "Starting from rest, v^2 = 2as gives v = sqrt(2 x 3.20 x 3) = 4.38 m/s.", 60, y, A4[0] - 120, size=11, leading=17)
    pdf.showPage()
    y = page_frame(pdf, "Alex Chen - Submission", "Student ID: DEMO-001 | Typeset response - synthetic student", 2, 2)
    y = answer_heading(pdf, y, "Q3 - Linear algebra", "7 / 7")
    y = paragraph(pdf, "If Ax=0, then A^T A x=0. Conversely, if A^T A x=0, left-multiplying by x^T gives ||Ax||^2=0, hence Ax=0. The kernels are equal, so rank-nullity gives equal ranks.", 60, y, A4[0] - 120, size=11, leading=17)
    y -= 30
    y = answer_heading(pdf, y, "Q4 - Programming", "6 / 10")
    code_block(pdf, [
        "def stable_softmax(xs):",
        "    if not xs:",
        "        return []",
        "    exps = [math.exp(x) for x in xs]",
        "    total = sum(exps)",
        "    return [value / total for value in exps]",
    ], 60, y, A4[0] - 120)
    pdf.setFillColor(RED)
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(60, 280, "REVIEW SIGNAL - extreme values overflow because max(xs) is not subtracted")
    pdf.save()
    return path


def generate_live_typeset_submission() -> Path:
    path = LIVE_OUTPUT / "DEMO-001_typeset_raw.pdf"
    pdf = pdf_canvas(path)
    y = page_frame(pdf, "Alex Chen - Submission", "Student ID: DEMO-001 | Raw typeset response - synthetic student", 1, 2)
    y = answer_heading(pdf, y, "Q1 - Calculus", "")
    y = paragraph(pdf, "Let u = x^2, so du = 2x dx. Therefore I = (1/2) integral_0^1 exp(u) du = (e - 1)/2.", 60, y, A4[0] - 120, size=12, leading=18)
    y -= 35
    y = answer_heading(pdf, y, "Q2 - Mechanics", "")
    y = paragraph(pdf, "N = mg cos 30 deg. Along the slope, ma = mg sin 30 deg - mu_k N, so a = 9.81(sin 30 deg - 0.20 cos 30 deg) = 3.20 m/s^2.", 60, y, A4[0] - 120, size=11, leading=17)
    y -= 12
    paragraph(pdf, "Starting from rest, v^2 = 2as gives v = sqrt(2 x 3.20 x 3) = 4.38 m/s.", 60, y, A4[0] - 120, size=11, leading=17)
    pdf.showPage()
    y = page_frame(pdf, "Alex Chen - Submission", "Student ID: DEMO-001 | Raw typeset response - synthetic student", 2, 2)
    y = answer_heading(pdf, y, "Q3 - Linear algebra", "")
    y = paragraph(pdf, "If Ax=0, then A^T A x=0. Conversely, if A^T A x=0, left-multiplying by x^T gives ||Ax||^2=0, hence Ax=0. The kernels are equal, so rank-nullity gives equal ranks.", 60, y, A4[0] - 120, size=11, leading=17)
    y -= 30
    y = answer_heading(pdf, y, "Q4 - Programming", "")
    code_block(pdf, [
        "def stable_softmax(xs):",
        "    if not xs:",
        "        return []",
        "    exps = [math.exp(x) for x in xs]",
        "    total = sum(exps)",
        "    return [value / total for value in exps]",
    ], 60, y, A4[0] - 120)
    pdf.save()
    return path


def generate_mixed_submission() -> Path:
    path = OUTPUT / "DEMO-003_mixed.pdf"
    pdf = pdf_canvas(path)
    y = page_frame(pdf, "Jordan Rivera - Submission", "Student ID: DEMO-003 | Mixed response - synthetic student", 1, 2)
    y = answer_heading(pdf, y, "Q1 - Calculus", "5 / 5")
    y = paragraph(pdf, "With u=x^2, I=(e-1)/2.", 60, y, A4[0] - 120, size=12, leading=18)
    y -= 42
    y = answer_heading(pdf, y, "Q2 - Mechanics", "7 / 8")
    y = paragraph(pdf, "a = 9.81(sin 30 deg - 0.2 cos 30 deg) = 3.20; therefore v is approximately 4.4 m/s.", 60, y, A4[0] - 120, size=12, leading=18)
    pdf.setFillColor(RED)
    pdf.setFont("STIXItalic", 11)
    pdf.drawString(92, y - 28, "unit missing here")
    pdf.line(185, y - 24, 260, y - 7)
    pdf.showPage()
    y = page_frame(pdf, "Jordan Rivera - Submission", "Student ID: DEMO-003 | Mixed response - synthetic student", 2, 2)
    y = answer_heading(pdf, y, "Q3 - Linear algebra", "3 / 7")
    y = paragraph(pdf, "A is invertible, so multiplying by A^T cannot change the kernel. Therefore A and A^T A have equal rank.", 60, y, A4[0] - 120, size=11.5, leading=18)
    pdf.setFillColor(RED)
    pdf.setFont("STIXItalic", 11)
    pdf.drawString(82, y - 20, "A may be rectangular - this assumption is not available.")
    y -= 75
    y = answer_heading(pdf, y, "Q4 - Programming", "8 / 10")
    code_block(pdf, [
        "if not xs: return []",
        "m = max(xs)",
        "z = [exp(x - m) for x in xs]",
        "return [value / sum(z) for value in z]",
    ], 60, y, A4[0] - 120)
    pdf.save()
    return path


def generate_live_mixed_submission() -> Path:
    path = LIVE_OUTPUT / "DEMO-003_mixed_raw.pdf"
    pdf = pdf_canvas(path)
    y = page_frame(pdf, "Jordan Rivera - Submission", "Student ID: DEMO-003 | Raw mixed response - synthetic student", 1, 2)
    y = answer_heading(pdf, y, "Q1 - Calculus", "")
    y = paragraph(pdf, "With u=x^2, I=(e-1)/2.", 60, y, A4[0] - 120, size=12, leading=18)
    y -= 42
    y = answer_heading(pdf, y, "Q2 - Mechanics", "")
    paragraph(pdf, "a = 9.81(sin 30 deg - 0.2 cos 30 deg) = 3.20; therefore v is approximately 4.4.", 60, y, A4[0] - 120, size=12, leading=18)
    pdf.showPage()
    y = page_frame(pdf, "Jordan Rivera - Submission", "Student ID: DEMO-003 | Raw mixed response - synthetic student", 2, 2)
    y = answer_heading(pdf, y, "Q3 - Linear algebra", "")
    y = paragraph(pdf, "A is invertible, so multiplying by A^T cannot change the kernel. Therefore A and A^T A have equal rank.", 60, y, A4[0] - 120, size=11.5, leading=18)
    y -= 60
    y = answer_heading(pdf, y, "Q4 - Programming", "")
    code_block(pdf, [
        "if not xs: return []",
        "m = max(xs)",
        "z = [exp(x - m) for x in xs]",
        "return [value / sum(z) for value in z]",
    ], 60, y, A4[0] - 120)
    pdf.save()
    return path


def load_font(path: Path, size: int, index: int = 0) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size=size, index=index)


def paper(seed: int) -> Image.Image:
    rng = random.Random(seed)
    width, height = 1500, 2050
    image = Image.new("RGB", (width, height), (247, 246, 239))
    pixels = image.load()
    for _ in range(65_000):
        x = rng.randrange(width)
        y = rng.randrange(height)
        value = rng.choice((-4, -3, -2, 2, 3))
        base = pixels[x, y]
        pixels[x, y] = tuple(max(0, min(255, channel + value)) for channel in base)
    draw = ImageDraw.Draw(image)
    for y in range(185, height - 110, 82):
        draw.line((120, y, width - 90, y), fill=(204, 214, 216), width=2)
    draw.line((195, 120, 195, height - 90), fill=(221, 138, 130), width=3)
    return image.filter(ImageFilter.GaussianBlur(0.18))


def rotated_text(image: Image.Image, xy: tuple[int, int], text: str, font: ImageFont.FreeTypeFont, fill: tuple[int, int, int], angle: float) -> None:
    box = font.getbbox(text)
    width = max(8, box[2] - box[0] + 30)
    height = max(8, box[3] - box[1] + 30)
    layer = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    ImageDraw.Draw(layer).text((12, 8 - box[1]), text, font=font, fill=(*fill, 255))
    layer = layer.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
    image.paste(layer, xy, layer)


def generate_handwritten_submission() -> tuple[Path, Path, Path, Path]:
    rng = random.Random(20260812)
    image = paper(20260812)
    draw = ImageDraw.Draw(image)
    hand_path = HAND if HAND.exists() else HAND_FALLBACK
    hand = load_font(hand_path, 48)
    hand_small = load_font(hand_path, 40)
    math_font = load_font(MATH, 43)
    utility = load_font(STIX, 25)
    utility_bold = load_font(STIX_BOLD, 31)
    blue = (27, 67, 143)
    red = (177, 53, 42)
    draw.text((235, 48), "Student: Maya Lin", font=utility_bold, fill=(32, 39, 48))
    draw.text((1025, 52), "ID: DEMO-002", font=utility, fill=(32, 39, 48))
    draw.text((235, 108), "mixed STEM responses", font=hand, fill=blue)
    draw.text((1090, 120), "synthetic", font=utility, fill=(100, 108, 113))
    entries = [
        (220, "Q1   u = x^2,  du = 2x dx", hand_small),
        (300, "I = [exp(u)]_0^1 = e - 1", math_font),
        (465, "Q2   μ = 0.20    N = mg cos 30 deg", hand_small),
        (545, "a = g(sin 30 deg - μ cos 30 deg) = 3.20", math_font),
        (625, "v = sqrt(2as) = 4.38 m/s", math_font),
        (800, "Q3   If A^T A x = 0, then Ax = 0", hand_small),
        (880, "because A^T cancels A.", hand_small),
        (1050, "Q4", hand_small),
        (1130, "m = max(xs)", hand_small),
        (1210, "z = [exp(x-m) for x in xs]", hand_small),
        (1290, "return [v / sum(z) for v in z]", hand_small),
    ]
    for y, text, font in entries:
        rotated_text(image, (235 + rng.randint(-8, 8), y), text, font, blue, rng.uniform(-1.1, 1.1))
    raw_image = image.copy()
    raw_draw = ImageDraw.Draw(raw_image)
    raw_draw.text((235, 1935), "SYNTHETIC DEMO DOCUMENT - raw student input", font=utility, fill=(98, 106, 112))
    raw_png_path = LIVE_OUTPUT / "DEMO-002_handwritten_raw.png"
    raw_image.save(raw_png_path, optimize=True)
    raw_pdf_path = LIVE_OUTPUT / "DEMO-002_handwritten_raw.pdf"
    raw_pdf = pdf_canvas(raw_pdf_path)
    raw_pdf.drawImage(ImageReader(raw_image), 0, 0, width=A4[0], height=A4[1])
    raw_pdf.save()
    draw.ellipse((326, 460, 386, 520), outline=red, width=6)
    rotated_text(image, (930, 438), "OCR: μ, not u", hand_small, red, -4)
    draw.line((940, 500, 380, 490), fill=red, width=5)
    rotated_text(image, (900, 775), "justify with ||Ax||^2", hand_small, red, -3)
    draw.line((1010, 850, 790, 872), fill=red, width=5)
    rotated_text(image, (880, 1375), "empty input?", hand_small, red, -5)
    draw.text((235, 1935), "SYNTHETIC DEMO DOCUMENT - generated locally", font=utility, fill=(98, 106, 112))
    png_path = OUTPUT / "DEMO-002_handwritten.png"
    image.save(png_path, optimize=True)
    pdf_path = OUTPUT / "DEMO-002_handwritten.pdf"
    pdf = pdf_canvas(pdf_path)
    pdf.drawImage(ImageReader(image), 0, 0, width=A4[0], height=A4[1])
    pdf.save()
    return png_path, pdf_path, raw_png_path, raw_pdf_path


def generate_scanned_submission() -> tuple[Path, Path, Path, Path]:
    image = paper(20260813)
    draw = ImageDraw.Draw(image)
    hand_path = HAND if HAND.exists() else HAND_FALLBACK
    hand = load_font(hand_path, 43)
    hand_small = load_font(hand_path, 37)
    utility = load_font(STIX, 25)
    utility_bold = load_font(STIX_BOLD, 31)
    graphite = (44, 48, 54)
    draw.text((230, 55), "Student: Taylor Singh", font=utility_bold, fill=graphite)
    draw.text((1030, 58), "ID: DEMO-004", font=utility, fill=graphite)
    rotated_text(image, (230, 235), "Q1  I = (e - 1) / 2", hand, graphite, -0.8)
    rotated_text(image, (230, 470), "Q2  a ~ 3.2    v ~ 4.4", hand, graphite, 0.7)
    draw.rectangle((215, 690, 1340, 980), outline=(176, 178, 176), width=3)
    draw.text((245, 720), "Q3", font=hand, fill=graphite)
    rotated_text(image, (230, 1080), "Q4", hand, graphite, -0.5)
    code = [
        "if not xs: return []",
        "m = max(xs)",
        "y = [exp(x-m) for x in xs]",
        "s = sum(y)",
        "return [v/s for v in y]",
    ]
    for index, line in enumerate(code):
        rotated_text(image, (250, 1160 + index * 83), line, hand_small, graphite, (index - 2) * 0.15)
    draw.text((230, 1935), "SYNTHETIC DEMO DOCUMENT - raw student input", font=utility, fill=(98, 106, 112))
    png_path = OUTPUT / "scan_004.png"
    image.save(png_path, optimize=True)
    pdf_path = OUTPUT / "scan_004.pdf"
    pdf = pdf_canvas(pdf_path)
    pdf.drawImage(ImageReader(image), 0, 0, width=A4[0], height=A4[1])
    pdf.save()
    live_png_path = LIVE_OUTPUT / "scan_004_raw.png"
    image.save(live_png_path, optimize=True)
    live_pdf_path = LIVE_OUTPUT / "scan_004_raw.pdf"
    live_pdf = pdf_canvas(live_pdf_path)
    live_pdf.drawImage(ImageReader(image), 0, 0, width=A4[0], height=A4[1])
    live_pdf.save()
    return png_path, pdf_path, live_png_path, live_pdf_path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_submissions_zip(raw_submission_pdfs: list[tuple[Path, str]]) -> Path:
    """Bundle raw student PDFs with stable order, timestamps and permissions."""
    path = LIVE_OUTPUT / "submissions_raw.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for source, archive_name in sorted(raw_submission_pdfs, key=lambda item: item[1]):
            info = zipfile.ZipInfo(archive_name, date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, source.read_bytes(), compresslevel=9)
    return path


def write_sha256sums(paths: list[Path]) -> Path:
    path = OUTPUT / "SHA256SUMS"
    lines = [
        f"{sha256(asset)}  {asset.relative_to(OUTPUT).as_posix()}"
        for asset in sorted(paths, key=lambda item: item.relative_to(OUTPUT).as_posix())
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_manifest(paths: list[Path]) -> None:
    manifest = {
        "schemaVersion": 1,
        "title": "STEM Reasoning Lab - Mixed Assessment",
        "synthetic": True,
        "visitorCodeExecution": False,
        "walkthrough": {
            "precomputed": True,
            "liveModelCalls": False,
        },
        "liveDemo": {
            "inputMode": "synthetic_raw_fixtures",
            "processingMode": "real_api_ocr_and_grading_required",
        },
        "assets": [
            {
                "path": path.relative_to(OUTPUT).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
            for path in sorted(paths, key=lambda item: item.relative_to(OUTPUT).as_posix())
        ],
    }
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    LIVE_OUTPUT.mkdir(parents=True, exist_ok=True)
    register_fonts()
    question_source = compile_latex_asset("question_source.tex", LIVE_OUTPUT / "question_source.pdf")
    typeset_marked = compile_latex_asset("DEMO-001_typeset.tex", OUTPUT / "DEMO-001_typeset.pdf")
    typeset_raw = compile_latex_asset("DEMO-001_typeset_raw.tex", LIVE_OUTPUT / "DEMO-001_typeset_raw.pdf")
    mixed_raw = generate_live_mixed_submission()
    paths = [
        generate_assignment(),
        typeset_marked,
        generate_mixed_submission(),
        question_source,
        generate_live_rubric(),
        typeset_raw,
        mixed_raw,
    ]
    handwritten_png, handwritten_pdf, handwritten_raw_png, handwritten_raw_pdf = generate_handwritten_submission()
    scan_png, scan_pdf, scan_raw_png, scan_raw_pdf = generate_scanned_submission()
    paths.extend([
        handwritten_png,
        handwritten_pdf,
        handwritten_raw_png,
        handwritten_raw_pdf,
        scan_png,
        scan_pdf,
        scan_raw_png,
        scan_raw_pdf,
    ])
    submissions_zip = write_submissions_zip([
        (typeset_raw, "DEMO-001_Alex-Chen_typeset.pdf"),
        (handwritten_raw_png, "DEMO-002_Maya-Lin_handwritten.png"),
        (mixed_raw, "DEMO-003_Jordan-Rivera_mixed.pdf"),
        (scan_raw_png, "DEMO-004_Taylor-Singh_scan.png"),
    ])
    paths.append(submissions_zip)
    sha256sums = write_sha256sums(paths)
    paths.append(sha256sums)
    write_manifest(paths)
    total = sum(path.stat().st_size for path in paths)
    print(f"generated {len(paths)} synthetic assets ({math.ceil(total / 1024)} KiB) in {OUTPUT}")


if __name__ == "__main__":
    main()

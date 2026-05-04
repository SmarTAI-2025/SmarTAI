"""
File processing tool: extract text files from archives (.zip, .rar, .7z, .tar.*)
or handle a single file.

Migrated from backend/utils.py with no behavior change — just relocated to
the tools/ namespace so it's discoverable as a "predefined tool" per docs §4.2.1.
"""
from __future__ import annotations

import asyncio
import io
import logging
import tarfile
import zipfile
from typing import List, Dict, Optional

try:
    import rarfile
except ImportError:
    rarfile = None

try:
    import py7zr
except ImportError:
    py7zr = None

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

from fastapi import HTTPException

logger = logging.getLogger(__name__)


async def decode_text_bytes(text_bytes: bytes) -> str:
    """Try UTF-8 then GBK; raise 400 if both fail."""
    try:
        return text_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return text_bytes.decode("gbk")
        except UnicodeDecodeError:
            raise HTTPException(
                status_code=400,
                detail="Unable to decode file; please ensure UTF-8 or GBK encoding.",
            )


async def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract text from a PDF file."""
    if fitz is None:
        raise HTTPException(
            status_code=501,
            detail="PDF processing requires 'PyMuPDF'; pip install PyMuPDF"
        )
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text()
        return text
    except Exception as e:
        logger.error(f"Error processing PDF with PyMuPDF: {e}")
        raise HTTPException(
            status_code=400,
            detail=f"Failed to extract text from PDF: {e}",
        )


def _is_valid_file(name: str) -> bool:
    """Filter out OS junk files."""
    return not (name.startswith("__MACOSX") or ".DS_Store" in name)


async def extract_files_from_archive(file_bytes: bytes, filename: str) -> List[Dict[str, str]]:
    """
    Extract all text files from an archive (zip/rar/7z/tar.*) or wrap a single
    file into the same [{"filename": ..., "content": ...}] format.
    """
    files_data: List[Dict[str, str]] = []
    file_in_memory = io.BytesIO(file_bytes)
    lower = filename.lower()

    if lower.endswith(".zip"):
        with zipfile.ZipFile(file_in_memory, "r") as zf:
            valid = [i for i in zf.infolist() if not i.is_dir() and _is_valid_file(i.filename)]

            async def process(info):
                clean = info.filename.split("/")[-1]
                content = await decode_text_bytes(zf.read(info.filename))
                return {"filename": clean, "content": content}

            files_data.extend(await asyncio.gather(*[process(i) for i in valid]))

    elif lower.endswith(".rar"):
        if rarfile is None:
            raise ValueError("Processing .rar files requires 'rarfile'; pip install rarfile")
        try:
            with rarfile.RarFile(file_in_memory, "r") as rf:
                valid = [i for i in rf.infolist() if not i.is_dir() and _is_valid_file(i.filename)]

                async def process(info):
                    clean = info.filename.split("/")[-1]
                    content = await decode_text_bytes(rf.read(info.filename))
                    return {"filename": clean, "content": content}

                files_data.extend(await asyncio.gather(*[process(i) for i in valid]))
        except rarfile.UNRARError as e:
            raise RuntimeError(
                f"RAR extraction failed: {e}. Ensure 'unrar' CLI is installed on the server."
            )

    elif lower.endswith(".7z"):
        if py7zr is None:
            raise ValueError("Processing .7z files requires 'py7zr'; pip install py7zr")
        with py7zr.SevenZipFile(file_in_memory, "r") as szf:
            all_files = szf.readall()
            valid = {n: bio for n, bio in all_files.items() if _is_valid_file(n)}

            async def process(item):
                n, bio = item
                clean = n.split("/")[-1]
                content = await decode_text_bytes(bio.read())
                return {"filename": clean, "content": content}

            files_data.extend(await asyncio.gather(*[process(i) for i in valid.items()]))

    elif lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2")):
        with tarfile.open(fileobj=file_in_memory, mode="r:*") as tf:
            valid = [m for m in tf.getmembers() if m.isfile() and _is_valid_file(m.name)]

            async def process(member):
                clean = member.name.split("/")[-1]
                obj = tf.extractfile(member)
                if obj is None:
                    return None
                content = await decode_text_bytes(obj.read())
                return {"filename": clean, "content": content}

            results = await asyncio.gather(*[process(m) for m in valid])
            files_data.extend([r for r in results if r is not None])

    else:
        if lower.endswith(".txt"):
            files_data.append({
                "filename": filename,
                "content": await decode_text_bytes(file_bytes),
            })
        else:
            logger.warning(f"Ignoring unsupported single-file type: {filename}")

    return files_data

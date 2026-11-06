"""Current offset-preserving chunker contracts, independent of retired storage."""
from backend.rag.chunker import chunk_spans


def test_empty_input():
    assert chunk_spans("") == []
    assert chunk_spans("   \n ") == []


def test_short_input():
    assert chunk_spans("hello world") == [dict(content="hello world", start=0, end=11)]


def test_overlap_preserves_source_offsets():
    text = " ".join(f"w{i}" for i in range(120))
    chunks = chunk_spans(text, chunk_words=50, overlap_words=10)
    assert len(chunks) == 3
    assert chunks[0]["content"].split()[-10:] == chunks[1]["content"].split()[:10]
    assert all(item["content"] == text[item["start"]:item["end"]] for item in chunks)


def test_does_not_truncate_after_500_chunks():
    text = " ".join("w" for _ in range(550 * 50))
    chunks = chunk_spans(text, chunk_words=50, overlap_words=0)
    assert len(chunks) == 550
    assert "".join(item["content"] for item in chunks) == text

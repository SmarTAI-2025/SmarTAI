from concurrent.futures import ThreadPoolExecutor

from backend.knowledge.index import IndexedChunk, KnowledgeIndex
from backend.knowledge.lexical import stem
from backend.knowledge.snapshots import INDEX_VERSION, TERMS_INDEX_VERSION
from backend.knowledge.structure import exercise_hints
from backend.rag.chunker import chunk_spans


def pages(texts, doc="book"):
    return [IndexedChunk(f"{doc}:{page}:{n}", doc, "v", span["content"],
                         dict(page_number=page, start=span["start"], end=span["end"]))
            for page, text in enumerate(texts, 1) for n, span in enumerate(chunk_spans(text))]


def test_sections_continue_only_across_contiguous_printed_pages():
    chunks = pages([
        "EXERCISES\n6. Analyze an example.\nSec. 4.3\nFirst section\n51\n",
        "7. Prove the next property.\n52\nChap. 4\nStructures\n",
        "8. Establish a different property.\n4.4 ANOTHER SECTION\nNew topic.\nSec. 4.4\n53\n",
        "9. This is after a missing page.\n55\nChap. 4\nStructures\n",
    ])
    index = KnowledgeIndex(chunks)
    assert index.search("Exercise 4.3.7", 5)[0][0].metadata["page_number"] == 2
    assert index.search("Exercise 4.3.8", 5)[0][0].metadata["page_number"] == 3
    assert index.search("Exercise 4.4.8", 5) == []
    assert index.search("Exercise 4.4.9", 5) == []
    assert index.search("Exercise 4.3.70", 5) == []
    assert [c.content for c in index.chunks] == [c.content for c in chunks]


def test_no_inheritance_across_documents_versions_or_source_gaps():
    first = pages(["EXERCISES\n6. A question.\nSec. 4.3\n51\n"])
    second = pages(["7. Not authorized to inherit a section.\n52\n"], "other")
    assert KnowledgeIndex(first + second).search("Exercise 4.3.7", 5) == []
    changed = IndexedChunk("next", "book", "another-version", second[0].content,
                           dict(page_number=2, start=0, end=len(second[0].content)))
    assert KnowledgeIndex(first + [changed]).search("Exercise 4.3.7", 5) == []
    partial = IndexedChunk("gap", "book", "v", "7. Partial page.\n52\n", dict(page_number=2, start=10, end=30))
    assert KnowledgeIndex(first + [partial]).search("Exercise 4.3.7", 5) == []


def test_continuation_is_source_linked_and_never_crosses_a_missing_printed_page():
    chunks = pages([
        "EXERCISES\n1. A direct product has these properties:\n(a) first part\nSec. 2.5\n17\n",
        "(b) second part.\n2. Discuss whether it is commutative.\n18\nChap. 2\n",
    ])
    hints, links = exercise_hints(chunks)
    assert links[chunks[0].id] == {chunks[1].id}
    assert "id:2.5.1" in hints[chunks[1].id]
    result = KnowledgeIndex(chunks).search("direct product", 5)
    assert {c.metadata["page_number"] for c, _ in result} == {1, 2}


def test_unlabelled_lists_and_footer_after_a_section_transition_are_not_exercises():
    index = KnowledgeIndex(pages(["1. Enumeration, not an exercise.\n2.2 NEW SECTION\nSec. 2.2\n15\n"]))
    assert index.search("Exercise 2.2.1", 5) == []


def test_v2_freeze_remains_unchanged_and_stemming_is_thread_safe():
    chunks = pages(["Groups contain elements."])
    assert not KnowledgeIndex(chunks, version=TERMS_INDEX_VERSION).search("group element", 1)
    assert KnowledgeIndex(chunks, version=INDEX_VERSION).search("group element", 1)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert set(pool.map(stem, ["groups"] * 20)) == {"group"}

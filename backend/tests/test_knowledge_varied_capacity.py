import random
import statistics
import time

from backend.knowledge.index import IndexedChunk, KnowledgeIndex


def test_varied_2500_page_index_has_bounded_memory_and_hot_queries():
    randomizer = random.Random(41)
    vocabulary = "group ring field module vector matrix normal cyclic subgroup kernel quotient eigenvalue linear basis dimension polynomial degree finite infinite product inverse element order homomorphism isomorphism theorem proof exercise relation generator permutation symmetry rotation mapping identity algebra".split()
    chunks = []
    for page in range(1, 2501):
        sentences = [" ".join(randomizer.sample(vocabulary, 12)) + f" statement{page}case{n}." for n in range(15)]
        text = f"Exercise 1.{page}.7\n" + "\n".join(sentences)
        chunks.append(IndexedChunk(str(page), str((page - 1) // 500), "v", text,
                                  dict(page_number=page, start=0, end=len(text))))
    started = time.perf_counter()
    index = KnowledgeIndex(chunks)
    cold = time.perf_counter() - started
    elapsed = []
    for _ in range(12):
        started = time.perf_counter()
        assert index.search("finite group inverse element order", 5)
        elapsed.append(time.perf_counter() - started)
    print(dict(pages=2500, chars=sum(len(c.content) for c in chunks), cold_seconds=round(cold, 3),
               index_bytes=index.size_bytes, hot_p95_seconds=round(max(elapsed), 4),
               hot_p50_seconds=round(statistics.median(elapsed), 4), provider_calls=0))
    assert index.size_bytes <= 96 * 1024 * 1024
    assert max(elapsed) < 2
    assert index.search("1.2500.7", 1)[0][0].id == "2500"

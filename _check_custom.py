import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")

from rag.retriever import Retriever

r = Retriever()

for q in ["刷钱方法", "火车那关怎么过"]:
    print(f"\n问: {q}")
    for h in r.search(q, top_k=3):
        print(f"  [{h['score']:6.2f}] {h['title']} §{h['section']}")

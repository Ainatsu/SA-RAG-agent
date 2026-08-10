import time

from fastembed import TextEmbedding

t0 = time.time()
model = TextEmbedding("intfloat/multilingual-e5-large")
print(f"加载 {time.time() - t0:.1f}s")

# e5 系列要求前缀：查询用 query:，文档用 passage:
docs = [
    "passage: Wrong Side of the Tracks. Follow the train with Big Smoke on the back of the Sanchez motorbike while he shoots the Vagos on the roof.",
    "passage: Muscle stat can be increased by lifting weights at any gym in San Andreas.",
    "passage: Oysters are collectibles found underwater across the map of San Andreas.",
]
queries = ["query: 火车那关跟不上怎么办", "query: 怎么练肌肉", "query: 牡蛎在哪里找"]

t0 = time.time()
dv = list(model.embed(docs))
print(f"embed 3 文档 {time.time() - t0:.2f}s, dim={len(dv[0])}")

t0 = time.time()
qv = list(model.embed(queries))
print(f"embed 3 查询 {time.time() - t0:.2f}s")

import numpy as np

for q, v in zip(queries, qv):
    sims = [float(np.dot(v, d)) for d in dv]
    best = int(np.argmax(sims))
    print(f"\n{q}")
    for i, s in enumerate(sims):
        mark = " <=" if i == best else ""
        print(f"  {s:.4f}  {docs[i][:60]}{mark}")

"""向量索引：构建、缓存、查询。

BM25 靠字面匹配，专有名词命中很准，但同义口语表达（"打不过"/"卡关了"/
"总失败"）只能靠别名表硬编码。向量检索补的是这一块。

选型说明：
  - fastembed + ONNX Runtime，不引入 torch（省掉约 2.5GB 依赖）
  - multilingual-e5-large：目前 fastembed 里唯一的多语言选项，中文问题
    能直接匹配英文语料，不依赖查询改写
  - 纯 CPU。查询时只 embed 一句话（几十毫秒），远小于后续 LLM 的网络往返；
    建索引是一次性成本，结果缓存到磁盘

e5 系列要求加前缀：查询用 "query: "，文档用 "passage: "。漏掉会明显掉点。
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
VECTORS_FILE = ROOT / "data" / "vectors.npy"
META_FILE = ROOT / "data" / "vectors.meta.json"

MODEL_NAME = "intfloat/multilingual-e5-large"
BATCH_SIZE = 32

log = logging.getLogger("embed")

_model = None


def get_model():
    """惰性加载。模型约 2.2GB，只在真正要用时才载入。"""
    global _model
    if _model is None:
        from fastembed import TextEmbedding

        log.info("加载嵌入模型 %s（首次运行需下载）……", MODEL_NAME)
        _model = TextEmbedding(MODEL_NAME)
    return _model


def embed_texts(texts: list[str], kind: str) -> np.ndarray:
    """把文本编码成 L2 归一化后的向量矩阵。

    kind 为 "query" 或 "passage"，决定 e5 前缀。
    归一化后点积即余弦相似度，检索时省一次除法。
    """
    prefix = "query: " if kind == "query" else "passage: "
    model = get_model()
    vecs = list(model.embed([prefix + t for t in texts], batch_size=BATCH_SIZE))
    arr = np.asarray(vecs, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    return arr / np.maximum(norms, 1e-12)


def chunk_key(chunk: dict) -> str:
    """单块指纹。用于在语料变动时定位哪些块的向量还能复用。

    哈希送去编码的完整文本（含标题与章节），任何改动都会导致 key 变化，
    对应的块就会被重新编码。截断到 16 字符：13400 块规模下碰撞概率可忽略，
    meta 文件体积能省一半。
    """
    h = hashlib.sha256()
    h.update(MODEL_NAME.encode())
    h.update(index_text(chunk).encode())
    return h.hexdigest()[:16]


def index_text(chunk: dict) -> str:
    """构造送去编码的文本。

    标题和章节要带上：向量模型能理解 "Wrong Side of the Tracks — Walkthrough"
    这种上下文，只喂正文会丢失"这段在讲哪个任务"的信息。
    正文截断到 800 字符，e5 的窗口是 512 token，再长会被硬截且拖慢编码。
    """
    return f"{chunk['title']} — {chunk['section']}\n{chunk['text'][:800]}"


def _read_cache() -> tuple[list[str], np.ndarray] | None:
    """读取磁盘缓存，返回 (每块指纹, 向量矩阵)。不可用时返回 None。"""
    if not VECTORS_FILE.exists() or not META_FILE.exists():
        return None
    try:
        meta = json.loads(META_FILE.read_text(encoding="utf-8"))
        vectors = np.load(VECTORS_FILE).astype(np.float32)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        log.warning("向量索引损坏，忽略：%s", e)
        return None

    if meta.get("model") != MODEL_NAME:
        log.warning("向量索引由其他模型生成（%s），需重新编码", meta.get("model"))
        return None

    keys = meta.get("keys")
    if not isinstance(keys, list) or len(keys) != vectors.shape[0]:
        log.warning("向量索引与元数据不一致，需重新编码")
        return None

    return keys, vectors


class VectorIndex:
    """内存里的稠密向量索引。

    1.3 万块 × 1024 维 float32 约 55MB，暴力矩阵乘法在 CPU 上是毫秒级，
    引入 FAISS/hnswlib 只会增加依赖，这个规模不需要。
    """

    def __init__(self, vectors: np.ndarray):
        self.vectors = vectors

    @property
    def size(self) -> int:
        return int(self.vectors.shape[0])

    def search(self, question: str, top_k: int) -> list[tuple[int, float]]:
        """返回 [(块下标, 余弦相似度)]，按相似度降序。"""
        qv = embed_texts([question], kind="query")[0]
        sims = self.vectors @ qv
        k = min(top_k, sims.shape[0])
        # argpartition 只做部分排序，比全量 argsort 快
        idx = np.argpartition(-sims, k - 1)[:k]
        idx = idx[np.argsort(-sims[idx])]
        return [(int(i), float(sims[i])) for i in idx]

    @classmethod
    def load(cls, chunks: list[dict]) -> VectorIndex | None:
        """从磁盘加载。缺文件、模型不符或存在未编码的块则返回 None。"""
        cached = _read_cache()
        if cached is None:
            return None
        keys, vectors = cached

        pos = {k: i for i, k in enumerate(keys)}
        rows = []
        for c in chunks:
            i = pos.get(chunk_key(c))
            if i is None:
                log.warning("语料有 %d 块尚未编码，向量检索关闭。"
                            "请运行：python -m rag.embed",
                            sum(1 for x in chunks if chunk_key(x) not in pos))
                return None
            rows.append(i)

        # 按当前语料顺序重排，保证向量下标与 chunks 下标一一对应
        return cls(vectors[rows])

    @classmethod
    def build(cls, chunks: list[dict]) -> VectorIndex:
        """增量编码：复用磁盘上仍然有效的向量，只编码新增或改动的块。"""
        keys = [chunk_key(c) for c in chunks]

        known: dict[str, np.ndarray] = {}
        cached = _read_cache()
        if cached is not None:
            old_keys, old_vecs = cached
            known = {k: old_vecs[i] for i, k in enumerate(old_keys)}

        todo = [i for i, k in enumerate(keys) if k not in known]
        log.info("共 %d 块，复用 %d，待编码 %d",
                 len(chunks), len(chunks) - len(todo), len(todo))

        if todo:
            step = 500
            for start in range(0, len(todo), step):
                batch = todo[start:start + step]
                vecs = embed_texts([index_text(chunks[i]) for i in batch],
                                   kind="passage")
                for i, v in zip(batch, vecs):
                    known[keys[i]] = v
                log.info("  %d / %d", min(start + step, len(todo)), len(todo))

        vectors = np.vstack([known[k] for k in keys]).astype(np.float32)
        VECTORS_FILE.parent.mkdir(parents=True, exist_ok=True)
        np.save(VECTORS_FILE, vectors)
        META_FILE.write_text(
            json.dumps({
                "model": MODEL_NAME,
                "dim": int(vectors.shape[1]),
                "count": int(vectors.shape[0]),
                "keys": keys,
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info("向量索引已写入 %s（%d × %d）",
                 VECTORS_FILE, vectors.shape[0], vectors.shape[1])
        return cls(vectors)


def main() -> int:
    import sys

    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s  %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    from .retriever import load_chunks

    chunks = load_chunks()
    VectorIndex.build(chunks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

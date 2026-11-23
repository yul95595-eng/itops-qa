# -*- coding: utf-8 -*-
# scripts/eval_rerank_granularity.py
# 消融: 重排粒度对 文件级Recall / MRR / 延迟 的影响
#   父块重排(现状, 1200字) vs 子块重排(400字, 回映射父块) vs 不重排
# 单一变量: 只改"拿什么文本送进 reranker", 其余(k=5, m=2)与生产一致
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.documents import Document
from pymilvus import AnnSearchRequest, WeightedRanker

from data.eval.retrieval_set import RETRIEVAL_CASES
from retrieval.vector_store import VectorStore

K = int(sys.argv[1]) if len(sys.argv) > 1 else 5
M = int(sys.argv[2]) if len(sys.argv) > 2 else 2
FIELDS = ["text", "doc_id", "parent_id", "parent_content",
          "source", "filename", "timestamp"]


def _hybrid(vs, query, k):
    emb = vs.embedding_function([query])
    sparse_vec = {}
    row = emb["sparse"][[0]]
    for idx, val in zip(row.indices, row.data):
        sparse_vec[idx] = val
    d = AnnSearchRequest(data=[emb["dense"][0]], anns_field="dense_vector",
                         param={"metric_type": "IP", "params": {"nprobe": 10}}, limit=k)
    s = AnnSearchRequest(data=[sparse_vec], anns_field="sparse_vector",
                         param={"metric_type": "IP", "params": {}}, limit=k)
    res = vs.client.hybrid_search(
        collection_name=vs.collection_name, reqs=[d, s],
        ranker=WeightedRanker(1.0, 0.7), limit=k, output_fields=FIELDS)[0]
    return [vs._doc_from_hit(h) for h in res]


def _rank_by(vs, query, docs):
    if not docs:
        return []
    scores = vs.reranker.predict([[query, d.page_content] for d in docs])
    return [d for _, d in sorted(zip(scores, docs), key=lambda x: x[0], reverse=True)]


def _retrieve(vs, query, mode):
    chunks = _hybrid(vs, query, K)
    if mode == "parent":
        return _rank_by(vs, query, vs._get_unique_parent_docs(chunks))[:M]
    if mode == "none":
        return vs._get_unique_parent_docs(chunks)[:M]
    best = {}
    if chunks:
        scores = vs.reranker.predict([[query, c.page_content] for c in chunks])
        for sc, ch in zip(scores, chunks):
            pid = ch.metadata.get("parent_id") or ch.metadata.get("parent_content", "")[:32]
            if pid not in best or float(sc) > best[pid][0]:
                best[pid] = (float(sc), ch)
    ranked = sorted(best.values(), key=lambda x: x[0], reverse=True)
    return [Document(page_content=ch.metadata.get("parent_content") or ch.page_content,
                     metadata=ch.metadata) for _, ch in ranked][:M]


def main():
    cases = RETRIEVAL_CASES
    vs = VectorStore()
    modes = [("parent", "父块重排 1200字(现状)"),
             ("child",  "子块重排 400字(改造)"),
             ("none",   "不重排(参照)")]
    rows = []
    print("=" * 78)
    print(f"  重排粒度消融  {len(cases)} 题  k={K} m={M}  单一变量: 送重排的文本粒度")
    print("=" * 78)
    for mode, desc in modes:
        hits, mrr, lat = 0, 0.0, 0.0
        fails = []
        for c in cases:
            t0 = time.time()
            docs = _retrieve(vs, c["query"], mode)
            lat += time.time() - t0
            rank = next((i for i, d in enumerate(docs, 1)
                         if d.metadata.get("filename") == c.get("expect_file")), 0)
            if rank:
                hits += 1
                mrr += 1.0 / rank
            else:
                fails.append(c["id"])
        n = len(cases)
        rows.append((desc, hits / n * 100, mrr / n, lat / n * 1000, fails))
        print(f"  {desc:<22} Recall {hits/n*100:5.1f}%  MRR {mrr/n:.3f}  "
              f"延迟 {lat/n*1000:7.0f}ms  漏检 {fails}")

    print("-" * 78)
    base = rows[0][1]
    for desc, rec, mrr, lat, _ in rows[1:]:
        print(f"  相对现状 {desc}: Recall {rec-base:+.1f} pp, 延迟 {lat-rows[0][3]:+.0f} ms")
    print("-" * 78)

    path = os.path.join("logs", f"rerank_granularity_k{K}_m{M}.md")
    os.makedirs("logs", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# 重排粒度消融\n\n- 题集: retrieval_set.py ({len(cases)} 题)\n")
        f.write(f"- 参数: k={K}, m={M}\n\n| 配置 | Recall@2(文件) | MRR | 延迟 | 漏检题 |\n|---|---|---|---|---|\n")
        for desc, rec, mrr, lat, fails in rows:
            f.write(f"| {desc} | {rec:.1f}% | {mrr:.3f} | {lat:.0f}ms | {','.join(fails) or '-'} |\n")
    print(f"  报告已写入 {path}")


if __name__ == "__main__":
    main()

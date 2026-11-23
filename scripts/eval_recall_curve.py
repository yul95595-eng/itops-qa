# -*- coding: utf-8 -*-
# scripts/eval_recall_curve.py
# 参数扫描: 召回池大小(k) vs 候选数(M) 对文件级 Recall / MRR / 延迟 的影响
#
# 目的: 回答"Recall 怎么提升" —— 用数据找「召回池→精排」的最优平衡点,
#   而不是拍脑袋调参。产出 Recall-延迟 二维对比, 选帕累托前沿。
#
# 运行: python scripts/eval_recall_curve.py
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base.config import config  # noqa: E402
from data.eval.retrieval_set import RETRIEVAL_CASES  # noqa: E402
from retrieval.vector_store import VectorStore  # noqa: E402
from pymilvus import AnnSearchRequest, WeightedRanker  # noqa: E402


def _retrieve(vs, query, k, m):
    """混合检索(稠密+稀疏) + reranker, 返回 top-m 父文档。k=召回子块数, m=候选父文档数"""
    t0 = time.time()
    emb = vs.embedding_function([query])
    dense = emb["dense"][0]
    sparse_vec = {}
    row = emb["sparse"][[0]]
    for idx, val in zip(row.indices, row.data):
        sparse_vec[idx] = val

    dense_req = AnnSearchRequest(
        data=[dense], anns_field="dense_vector",
        param={"metric_type": "IP", "params": {"nprobe": 10}}, limit=k)
    sparse_req = AnnSearchRequest(
        data=[sparse_vec], anns_field="sparse_vector",
        param={"metric_type": "IP", "params": {}}, limit=k)

    res = vs.client.hybrid_search(
        collection_name=vs.collection_name,
        reqs=[dense_req, sparse_req],
        ranker=WeightedRanker(1.0, 0.7),
        limit=k,
        output_fields=["text", "parent_id", "parent_content", "source", "filename", "timestamp"],
    )[0]

    sub_chunks = [vs._doc_from_hit(h) for h in res]
    parents = vs._get_unique_parent_docs(sub_chunks)

    pairs = [[query, d.page_content] for d in parents]
    if pairs:
        scores = vs.reranker.predict(pairs)
        order = sorted(range(len(parents)), key=lambda i: scores[i], reverse=True)
        parents = [parents[i] for i in order]
    return parents[:m], time.time() - t0


def _hit_rank(docs, expect_file):
    for i, d in enumerate(docs, 1):
        if d.metadata.get("filename") == expect_file:
            return i
    return 0


def main():
    cases = RETRIEVAL_CASES
    vs = VectorStore()

    # 扫描组合: (召回子块 k, 候选父文档 m)
    configs = [
        (5, 2),    # 当前生产配置
        (10, 4),
        (20, 8),
        (30, 10),
    ]

    print("=" * 78)
    print(f"  参数扫描: 召回池 vs 候选数 → 文件级 Recall / MRR / 延迟  (共 {len(cases)} 题)")
    print("  说明: 扩大 k 多召回子块、扩大 m 多留父文档, 看 Recall 是否上升、延迟代价多大")
    print("=" * 78)

    results = []
    for k, m in configs:
        hit = 0
        mrr_sum = 0.0
        lat_sum = 0.0
        for case in cases:
            docs, lat = _retrieve(vs, case["query"], k, m)
            rank = _hit_rank(docs, case.get("expect_file"))
            if rank:
                hit += 1
                mrr_sum += 1.0 / rank
            lat_sum += lat
        recall = hit / len(cases) * 100
        mrr = mrr_sum / len(cases)
        lat_ms = lat_sum / len(cases) * 1000
        results.append((k, m, recall, mrr, lat_ms))
        print(f"  k={k:>2} m={m:>2}  →  Recall {recall:5.1f}%   MRR {mrr:.3f}   平均延迟 {lat_ms:7.1f}ms")

    print("\n" + "-" * 78)
    print("  结论解读:")
    # 找 Recall 拐点: 从当前配置(5,2) 到最大, 看每档增量
    base_recall = results[0][2]
    base_lat = results[0][4]
    for k, m, recall, mrr, lat in results[1:]:
        print(f"    ({k},{m}) vs (5,2): Recall {recall - base_recall:+.1f} pp, "
              f"延迟 {lat - base_lat:+.0f} ms (代价)")
    print("-" * 78)


if __name__ == "__main__":
    main()

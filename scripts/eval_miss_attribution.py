# -*- coding: utf-8 -*-
# scripts/eval_miss_attribution.py
# 漏检归因: 在 (20,8) 配置下找出文件级 Recall 未命中的题,
#   并对每道漏检题分别跑「纯稠密 / 纯稀疏 / 混合+rerank」三个通道,
#   定位正确答案文件在哪一环丢失。
#
# 归因逻辑(召回漏斗):
#   查询理解(embedding) → 稠密召回(语义) / 稀疏召回(关键词) → 混合融合 → rerank 精排 → 阈值截断
#   - 稠密也找不到 + 稀疏也找不到  -> 查询理解/切块问题(语义+关键词都够不着)
#   - 稠密找不到, 稀疏能找到       -> 语义召回弱(同义表述), 关键词路更可靠
#   - 稠密/稀疏都能找到, 混合+rerank 后丢了 -> rerank 精排把正确答案挤下去了
#
# 运行: python scripts/eval_miss_attribution.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base.config import config  # noqa: E402
from data.eval.retrieval_set import RETRIEVAL_CASES  # noqa: E402
from retrieval.vector_store import VectorStore  # noqa: E402
from pymilvus import AnnSearchRequest, WeightedRanker  # noqa: E402

K = 20  # 召回子块数(甜点配置)
M = 8   # 候选父文档数(甜点配置)


def _embed(vs, query):
    emb = vs.embedding_function([query])
    dense = emb["dense"][0]
    sparse = {}
    row = emb["sparse"][[0]]
    for idx, val in zip(row.indices, row.data):
        sparse[idx] = val
    return dense, sparse


def _search(vs, query, dense, sparse, mode, k=K):
    """单通道检索, 返回该通道 top-k 命中的 filename 列表(按序, 含重复)"""
    out = ["text", "parent_id", "parent_content", "source", "filename"]
    if mode == "dense":
        res = vs.client.search(
            collection_name=vs.collection_name,
            data=[dense], anns_field="dense_vector",
            search_params={"metric_type": "IP", "params": {"nprobe": 10}},
            limit=k, output_fields=out,
        )[0]
    elif mode == "sparse":
        res = vs.client.search(
            collection_name=vs.collection_name,
            data=[sparse], anns_field="sparse_vector",
            search_params={"metric_type": "IP", "params": {}},
            limit=k, output_fields=out,
        )[0]
    elif mode == "hybrid":
        dense_req = AnnSearchRequest(
            data=[dense], anns_field="dense_vector",
            param={"metric_type": "IP", "params": {"nprobe": 10}}, limit=k)
        sparse_req = AnnSearchRequest(
            data=[sparse], anns_field="sparse_vector",
            param={"metric_type": "IP", "params": {}}, limit=k)
        res = vs.client.hybrid_search(
            collection_name=vs.collection_name,
            reqs=[dense_req, sparse_req],
            ranker=WeightedRanker(1.0, 0.7),
            limit=k, output_fields=out,
        )[0]
    else:
        raise ValueError(mode)
    return [h.get("entity", {}).get("filename", "") for h in res]


def _first_rank(filenames, expect):
    for i, f in enumerate(filenames, 1):
        if f == expect:
            return i
    return 0


def _hybrid_rerank(vs, query, dense, sparse, k=K, m=M):
    """混合 + rerank 后返回 top-m 父文档 filename(按 rerank 序)"""
    dense_req = AnnSearchRequest(
        data=[dense], anns_field="dense_vector",
        param={"metric_type": "IP", "params": {"nprobe": 10}}, limit=k)
    sparse_req = AnnSearchRequest(
        data=[sparse], anns_field="sparse_vector",
        param={"metric_type": "IP", "params": {}}, limit=k)
    res = vs.client.hybrid_search(
        collection_name=vs.collection_name,
        reqs=[dense_req, sparse_req],
        ranker=WeightedRanker(1.0, 0.7),
        limit=k,
        output_fields=["text", "parent_id", "parent_content", "source", "filename"],
    )[0]
    sub_chunks = [vs._doc_from_hit(h) for h in res]
    parents = vs._get_unique_parent_docs(sub_chunks)
    pairs = [[query, d.page_content] for d in parents]
    if pairs:
        scores = vs.reranker.predict(pairs)
        order = sorted(range(len(parents)), key=lambda i: scores[i], reverse=True)
        parents = [parents[i] for i in order]
    return [d.metadata.get("filename", "") for d in parents[:m]]


def main():
    vs = VectorStore()
    cases = RETRIEVAL_CASES

    print("=" * 90)
    print(f"  漏检归因: 在 (k={K}, m={M}) 下, 对文件级 Recall 未命中题做三通道定位")
    print("=" * 90)

    misses = []
    for case in cases:
        query = case["query"]
        expect = case["expect_file"]
        dense, sparse = _embed(vs, query)

        d_rank = _first_rank(_search(vs, query, dense, sparse, "dense"), expect)
        s_rank = _first_rank(_search(vs, query, dense, sparse, "sparse"), expect)
        h_rank = _first_rank(_search(vs, query, dense, sparse, "hybrid"), expect)
        r_rank = _first_rank(_hybrid_rerank(vs, query, dense, sparse), expect)

        if r_rank == 0:  # 混合+rerank 后仍没进 top-8 -> 文件级漏检
            misses.append((case, d_rank, s_rank, h_rank))

    print(f"\n  文件级漏检共 {len(misses)} / {len(cases)} 题\n")

    for case, d_rank, s_rank, h_rank in misses:
        print(f"  [{case['id']}] {case['query']}")
        print(f"       期望文件: {case['expect_file']}")
        print(f"       稠密召回 rank={d_rank} | 稀疏召回 rank={s_rank} | 混合(未rerank) rank={h_rank}")

        if d_rank == 0 and s_rank == 0:
            reason = "① 查询理解/切块问题: 语义+关键词双路都够不着目标文件"
        elif d_rank == 0 and s_rank > 0:
            reason = "② 语义召回弱(口语化同义表述), 关键词路更可靠"
        elif d_rank > 0 and s_rank == 0:
            reason = "③ 关键词稀疏路弱(无词面重合), 依赖语义路"
        elif h_rank == 0:
            reason = "④ 混合融合丢分: 单路都能找到, WeightedRanker 融合后反而没进 top-20"
        else:
            reason = "⑤ rerank 精排误排: 候选池里有, 但 reranker 把正确答案挤出 top-8"
        print(f"       归因 → {reason}\n")

    # 汇总
    from collections import Counter
    counter = Counter()
    for case, d_rank, s_rank, h_rank in misses:
        if d_rank == 0 and s_rank == 0:
            counter["①查询理解/切块"] += 1
        elif d_rank == 0 and s_rank > 0:
            counter["②语义弱"] += 1
        elif d_rank > 0 and s_rank == 0:
            counter["③稀疏弱"] += 1
        elif h_rank == 0:
            counter["④融合丢分"] += 1
        else:
            counter["⑤rerank误排"] += 1

    print("-" * 90)
    print("  归因汇总:")
    for reason, n in counter.most_common():
        print(f"    {reason:<16} {n} 题")
    print("-" * 90)


if __name__ == "__main__":
    main()

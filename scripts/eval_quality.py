# -*- coding: utf-8 -*-
# scripts/eval_quality.py
# 项目质量评估 —— 三件事, 把项目从「我做了个 RAG」提升到「我验证过每个决策」
#
# ============================================================================
# 为什么需要这个脚本(它和 scripts/evaluate.py 的区别)
# ============================================================================
# scripts/evaluate.py 是「题库自测」:
#     错误码题从错误码表反向生成、FAQ 题直接复用原题 —— 题目和训练数据同源,
#     测出 100% 属于「用训练集当测试集」, 拿不出手。
#
# 本脚本补的正是面试官真正会问的三件事:
#   1. --independent  独立测试集(40题): 口语化/错别字/错误码/混合意图/越界拒答
#                     —— 题目措辞完全不照抄知识库, 测的是真实泛化能力
#   2. --ablation     检索消融(32题): 有/无 reranker、混合/纯稠密
#                     —— 回答"你的 reranker 到底提升了几个点"
#   3. --baseline     基线对照: 同一 prompt 去掉上下文, 模型是否开始编造
#                     —— 回答"RAG 到底带来了什么", 用越界题的幻觉率直接量化
#
# 用法:
#   python scripts/eval_quality.py --independent          # 独立测试集
#   python scripts/eval_quality.py --ablation             # 检索消融
#   python scripts/eval_quality.py --baseline             # 基线对照
#   python scripts/eval_quality.py --all                  # 三个都跑
#   python scripts/eval_quality.py --independent --limit 5    # 只跑前5题(调试)
#   python scripts/eval_quality.py --independent --category 越界
#
# 产物(都在 logs/ 下):
#   independent_report.md / ablation_report.md / baseline_report.md
# ============================================================================
import argparse
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base.config import config
from base.logger import logger


# ============================================================================
# 通用工具
# ============================================================================

def _ensure_log_dir():
    os.makedirs(config.LOG_DIR, exist_ok=True)


def _pct(a, b):
    return (a / b * 100) if b else 0.0


# 判定"拒答"的词表: 出现任一即认为模型如实说了"不知道"
_REFUSAL_WORDS = [
    "不知道", "无法回答", "无法确定", "不能回答", "没有找到", "未找到",
    "抱歉", "不清楚", "无法提供", "没有相关", "无相关信息", "无法判断",
    "未提供", "缺少", "没有上下文", "无法基于", "不涉及", "不在",
]


def _is_refusal(text):
    """判断一段回答是否属于「如实拒答」而非「编造」"""
    if not text or not text.strip():
        return True
    t = text.strip()
    if len(t) <= 20:
        return True
    return any(w in t for w in _REFUSAL_WORDS)


def _classify_refusal(layer, answer):
    """
    对「知识库无法回答」的问题, 判定系统的处理方式(三分类):
      arch   架构拦截: 在进 RAG 之前就被某一层挡住了, 完全没调用生成   <- 最佳
             (fallback 兜底 / out_of_domain 域外判定 / chitchat 闲聊引导 / clarify 反问澄清)
      self   模型自觉: 进了 RAG 但诚实说"没有"                        <- 可接受, 但费 LLM 且有编造风险
      halluc 编造:     进了 RAG 且给出了具体内容                       <- 最差

    注意: out_of_domain 和 chitchat 是后来加的层, 它们比 fallback 更靠前
    (连检索都没走), 归为 arch 才是正确口径。漏掉它们会把"架构拦住"
    错记成"模型自觉"(答案里含'不在')或"编造"(引导语里没有拒答词)。

    clarify(槽位自检反问)同理: 它同样在进检索之前就停住了, 没有编造。
    但它对越界题是"问错了方向"而非"正确拒答", 因此下面 _judge_independent
    里对 clarify 单列一条, 不算满分也不算编造。
    """
    if layer in ("fallback", "out_of_domain", "chitchat", "clarify"):
        return "arch"
    return "self" if _is_refusal(answer) else "halluc"


def _print_progress(i, total, query, judgement=""):
    """单行进度, 避免刷屏"""
    q = query if len(query) <= 24 else query[:24] + "…"
    print(f"  [{i:>2}/{total}] {q:<26} {judgement}")


# ============================================================================
# 一、独立测试集评测
# ============================================================================

def _judge_independent(case, result):
    """
    判定单题是否正确。
    三类判定规则:
      越界题 -> 必须诚实拒答(优先架构拦截, 其次模型自觉, 编造为错)
      错误码题 -> 必须提取到正确错误码
      其他题 -> 未兜底(真的答了) 且答案含至少一个领域术语
    :return: (bool 是否正确, str 判定说明)
    """
    layer = result.get("layer")
    answer = (result.get("answer") or "").strip()

    # --- 越界题: 知识库没有, 正确行为是「诚实拒答」 ---
    if case.get("should_refuse"):
        kind = _classify_refusal(layer, answer)
        if kind == "arch":
            if layer == "clarify":
                return False, "❌ 反问澄清(没编造, 但该拒答的题被反问, 属于误拦)"
            return True, "✅ 架构拦截(兜底直接拒答)"
        if kind == "self":
            return True, "✅ 模型自觉拒答(进了RAG, 阈值偏松, 有优化空间)"
        return False, f"❌ 编造(layer={layer})"

    # --- 错误码题: 精确比对错误码 ---
    if case.get("expect_code"):
        got = result.get("code")
        ok = (layer == "error_code" and got == case["expect_code"])
        return ok, (f"✅ 错误码 {got}" if ok else f"❌ 得到 {got} (layer={layer})")

    # --- 其他题: 先看有没有被误拒, 再看是否答出领域术语 ---
    # clarify 也是"该答未答": 正题被反问等于多花用户一轮, 计失败但原因写清楚,
    # 否则会被误读成"答了但没含关键词"。
    if layer == "clarify":
        return False, "❌ 误拦为反问澄清(该答未答)"
    if layer == "fallback":
        return False, "❌ 误拒(该答未答)"

    kws = case.get("expect_kw") or []
    hit = [k for k in kws if k in answer]
    if hit:
        return True, f"✅ 命中 {hit} (layer={layer})"
    return False, f"❌ 已作答但未含 {kws} (layer={layer})"


def run_independent(limit=None, category=None):
    """跑独立测试集, 返回统计结果 dict"""
    from data.eval.independent_set import get_cases
    from service.pipeline import QAPipeline

    cases = get_cases([category] if category else None)
    if limit:
        cases = cases[:limit]

    print("\n" + "=" * 74)
    print(f"  独立测试集评测  (共 {len(cases)} 题)")
    print("  说明: 题目措辞不照抄知识库, 测真实泛化能力; 越界题测反幻觉拒答")
    print("=" * 74)

    pipeline = QAPipeline()
    rows = []
    t0 = time.time()

    for i, case in enumerate(cases, 1):
        try:
            result = pipeline.answer(case["query"])
        except Exception as e:
            logger.error(f"评测异常 [{case['id']}]: {e}")
            result = {"layer": "error", "answer": f"[评测异常] {e}"}

        ok, judge = _judge_independent(case, result)
        rows.append(dict(case=case, result=result, ok=ok, judge=judge))
        _print_progress(i, len(cases), case["query"], judge)

    elapsed = time.time() - t0

    # ---------- 统计 ----------
    total = len(rows)
    correct = sum(1 for r in rows if r["ok"])

    by_cat = defaultdict(lambda: [0, 0])
    for r in rows:
        c = r["case"]["category"]
        by_cat[c][1] += 1
        if r["ok"]:
            by_cat[c][0] += 1

    # 层分布(比"层命中率"更有信息量: 体现每层实际承接了多少题、降级是否生效)
    layer_dist = Counter(r["result"].get("layer") for r in rows)

    # 拒答相关
    refuse_cases = [r for r in rows if r["case"].get("should_refuse")]
    answerable = [r for r in rows if not r["case"].get("should_refuse")]
    refuse_kinds = Counter(
        _classify_refusal(r["result"].get("layer"), r["result"].get("answer") or "")
        for r in refuse_cases
    )
    refuse_ok = refuse_kinds["arch"] + refuse_kinds["self"]
    false_refuse = sum(1 for r in answerable if r["result"].get("layer") == "fallback")

    stats = dict(
        total=total, correct=correct, acc=_pct(correct, total), elapsed=elapsed,
        by_cat=dict(by_cat), layer_dist=dict(layer_dist),
        refuse_total=len(refuse_cases), refuse_ok=refuse_ok,
        refuse_acc=_pct(refuse_ok, len(refuse_cases)),
        refuse_arch=refuse_kinds["arch"], refuse_self=refuse_kinds["self"],
        refuse_halluc=refuse_kinds["halluc"],
        answerable_total=len(answerable), false_refuse=false_refuse,
        false_refuse_rate=_pct(false_refuse, len(answerable)),
        rows=rows,
    )

    # ---------- 控制台汇总 ----------
    print("\n" + "-" * 74)
    print(f"  总体准确率: {correct}/{total} = {stats['acc']:.1f}%   (耗时 {elapsed:.0f}s)")
    print("-" * 74)
    print(f"  {'题型':<12}{'正确':>6}{'总数':>6}{'准确率':>10}")
    for cat, (c, t) in sorted(by_cat.items(), key=lambda x: -x[1][1]):
        print(f"  {cat:<12}{c:>6}{t:>6}{_pct(c, t):>9.1f}%")
    print("-" * 74)
    print(f"  拒答准确率(越界题): {refuse_ok}/{len(refuse_cases)} = {stats['refuse_acc']:.1f}%"
          f"  (架构拦截 {refuse_kinds['arch']} / 模型自觉 {refuse_kinds['self']}"
          f" / 编造 {refuse_kinds['halluc']})")
    print(f"  误拒率(该答未答):   {false_refuse}/{len(answerable)} = {stats['false_refuse_rate']:.1f}%")
    print(f"  层分布:             " + " / ".join(f"{k}:{v}" for k, v in layer_dist.most_common()))
    print("=" * 74)

    # ---------- 写报告 ----------
    _ensure_log_dir()
    path = os.path.join(config.LOG_DIR, "independent_report.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 独立测试集评测报告\n\n")
        f.write(f"- 评测时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"- 题目来源: `data/eval/independent_set.py`（措辞不照抄知识库，非反向生成）\n")
        f.write(f"- 样本量: {total} 题\n")
        f.write(f"- 总耗时: {elapsed:.0f}s\n\n")

        f.write("## 总体结果\n\n")
        f.write("| 指标 | 值 |\n|---|---|\n")
        f.write(f"| **答案准确率** | **{correct}/{total} = {stats['acc']:.1f}%** |\n")
        f.write(f"| 拒答准确率（越界题） | {refuse_ok}/{len(refuse_cases)} = {stats['refuse_acc']:.1f}% |\n")
        f.write(f"| 误拒率（该答未答） | {false_refuse}/{len(answerable)} = {stats['false_refuse_rate']:.1f}% |\n\n")

        f.write("## 越界题：处理方式三分类\n\n")
        f.write("知识库里没有的题（天气、菜谱、公司年会…），正确行为是诚实拒答。"
                "但「怎么拒答」有质量差别：\n\n")
        f.write("| 处理方式 | 题数 | 含义 |\n|---|---|---|\n")
        f.write(f"| **架构拦截** | {refuse_kinds['arch']} | 兜底层直接拒答，**完全没调用 LLM**，零编造风险、零成本 |\n")
        f.write(f"| 模型自觉拒答 | {refuse_kinds['self']} | 进了 RAG 层但 LLM 诚实说没有。能兜住，但**依赖模型自觉** |\n")
        f.write(f"| 编造 | {refuse_kinds['halluc']} | 进了 RAG 层且给出了具体内容，属于幻觉 |\n\n")
        f.write(f"→ 架构拦截占 {_pct(refuse_kinds['arch'], len(refuse_cases)):.0f}%，"
                f"模型自觉占 {_pct(refuse_kinds['self'], len(refuse_cases)):.0f}%。"
                "后者占比偏高说明**相关性阈值偏松**，不相关文档也能过阈值进入 LLM，是可优化项。\n\n")

        f.write("## 层分布（体现分层降级是否生效）\n\n")
        f.write("同一道题走 FAQ 还是 RAG 都算对——分层设计允许「上层漏了、下层接住」，\n")
        f.write("所以这里看分布而不是「层命中率」。\n\n")
        f.write("| 实际承接层 | 题数 | 占比 |\n|---|---|---|\n")
        for layer, n in layer_dist.most_common():
            f.write(f"| {layer} | {n} | {_pct(n, total):.1f}% |\n")
        f.write("\n")

        f.write("## 分题型结果\n\n")
        f.write("| 题型 | 正确 | 总数 | 准确率 |\n|---|---|---|---|\n")
        for cat, (c, t) in sorted(by_cat.items(), key=lambda x: -x[1][1]):
            f.write(f"| {cat} | {c} | {t} | {_pct(c, t):.1f}% |\n")
        f.write("\n")

        f.write("## 逐题明细\n\n")
        f.write("| 题号 | 题型 | 问题 | 判定 | 实际层 | 考察点 |\n|---|---|---|---|---|---|\n")
        for r in rows:
            c = r["case"]
            f.write(f"| {c['id']} | {c['category']} | {c['query']} | {r['judge']} | "
                    f"{r['result'].get('layer')} | {c.get('note','')} |\n")
        f.write("\n")

        # 失败题单独列出, 方便针对性优化
        fails = [r for r in rows if not r["ok"]]
        f.write(f"## 未通过清单（{len(fails)} 题）\n\n")
        if not fails:
            f.write("无。\n")
        else:
            for r in fails:
                c = r["case"]
                ans = (r["result"].get("answer") or "").replace("\n", " ")[:160]
                f.write(f"- **{c['id']}** `{c['query']}` → {r['judge']}\n")
                f.write(f"  - 实际回答（截断）: {ans}\n")

    print(f"\n报告已保存: {path}")
    return stats


# ============================================================================
# 二、检索消融实验
# ============================================================================

def _retrieve(vs, query, mode, k=5):
    """
    按指定配置执行检索, 统一不做阈值过滤(保证 Recall 对比公平)。
    :param mode: hybrid_rerank | hybrid_only | dense_rerank
    :return: (父文档列表, 耗时秒)
    """
    from pymilvus import AnnSearchRequest, WeightedRanker

    t0 = time.time()
    emb = vs.embedding_function([query])
    dense = emb["dense"][0]

    if mode == "dense_rerank":
        # 只用稠密向量
        # 注意: pymilvus 2.5+ 的 MilvusClient.search 用 search_params(不是 param)
        res = vs.client.search(
            collection_name=vs.collection_name,
            data=[dense],
            anns_field="dense_vector",
            search_params={"metric_type": "IP", "params": {"nprobe": 10}},
            limit=k,
            output_fields=["text", "parent_id", "parent_content", "source", "filename", "timestamp"],
        )[0]
    else:
        # 稠密 + 稀疏混合
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

    # hybrid_only: 直接用 RRF 融合后的顺序, 不重排
    if mode == "hybrid_only" or len(parents) < 2:
        docs = parents[:config.CANDIDATE_M]
        return docs, time.time() - t0

    # 其余两种模式都过 reranker
    pairs = [[query, d.page_content] for d in parents]
    scores = vs.reranker.predict(pairs)
    order = sorted(range(len(parents)), key=lambda i: scores[i], reverse=True)
    docs = [parents[i] for i in order][:config.CANDIDATE_M]
    return docs, time.time() - t0


def _first_hit_rank(docs, expect_source, expect_file=None):
    """返回第一个命中期望的排位(1-based); 未命中返回 0。
    expect_file 给定时按「文件级」精判(返回父块是否来自该文件);
    否则退回「域级」粗判(metadata.source == expect_source)。
    """
    for i, d in enumerate(docs, 1):
        if expect_file:
            if d.metadata.get("filename") == expect_file:
                return i
        else:
            if d.metadata.get("source") == expect_source:
                return i
    return 0


def _ndcg(docs, expect_source, expect_file=None, k=None):
    """计算单条查询的 nDCG@k(文件级/域级二分类相关性, 命中=1 否则 0)。
    返回 0~1; 完全没命中返回 0。
    """
    k = k or len(docs)
    ideal = 1.0  # 单相关文档的理想 DCG = 1 / log2(2) = 1
    for i, d in enumerate(docs[:k], 1):
        if expect_file:
            hit = d.metadata.get("filename") == expect_file
        else:
            hit = d.metadata.get("source") == expect_source
        if hit:
            return (1.0 / _log2(i + 1)) / ideal
    return 0.0


def _log2(x):
    import math
    return math.log2(x)


def run_ablation(limit=None):
    """跑三组检索配置的消融对比"""
    from data.eval.retrieval_set import RETRIEVAL_CASES
    from retrieval.vector_store import VectorStore

    cases = RETRIEVAL_CASES[:limit] if limit else RETRIEVAL_CASES

    modes = [
        ("hybrid_rerank", "稠密+稀疏 + reranker（当前生产配置）"),
        ("hybrid_only", "稠密+稀疏，不重排（去掉 reranker）"),
        ("dense_rerank", "只用稠密向量 + reranker（去掉稀疏）"),
    ]

    print("\n" + "=" * 74)
    print(f"  检索消融实验  (共 {len(cases)} 题, {len(modes)} 组配置)")
    print("  指标: Recall@M(文件级) / MRR / nDCG —— 判返回父块是否来自正确答案所在文件")
    print("  说明: 三组统一不做阈值过滤, 保证对比公平")
    print("=" * 74)

    vs = VectorStore()
    results = {}

    for mode, desc in modes:
        print(f"\n[{mode}] {desc}")
        hit_file = 0
        hit_domain = 0
        mrr_sum = 0.0
        ndcg_sum = 0.0
        lat_sum = 0.0
        detail = []

        for i, case in enumerate(cases, 1):
            try:
                docs, lat = _retrieve(vs, case["query"], mode)
            except Exception as e:
                logger.error(f"检索异常 [{case['id']}] {mode}: {e}")
                docs, lat = [], 0.0

            expect_file = case.get("expect_file")
            rank_file = _first_hit_rank(docs, case["expect_source"], expect_file)
            rank_domain = _first_hit_rank(docs, case["expect_source"], None)
            if rank_file:
                hit_file += 1
                mrr_sum += 1.0 / rank_file
            if rank_domain:
                hit_domain += 1
            ndcg_sum += _ndcg(docs, case["expect_source"], expect_file)
            lat_sum += lat
            detail.append((case, rank_file, rank_domain))
            _print_progress(i, len(cases), case["query"],
                            f"{'✅' if rank_file else '❌'} 文件排位={rank_file or '-'}")

        results[mode] = dict(
            desc=desc,
            recall=_pct(hit_file, len(cases)),       # 文件级 Recall
            recall_domain=_pct(hit_domain, len(cases)),  # 域级 Recall(旧口径, 偏松)
            hit=hit_file,
            total=len(cases),
            mrr=(mrr_sum / len(cases)) if cases else 0.0,
            ndcg=(ndcg_sum / len(cases)) if cases else 0.0,
            latency_ms=(lat_sum / len(cases) * 1000) if cases else 0.0,
            detail=detail,
        )

    # ---------- 控制台对比表 ----------
    print("\n" + "-" * 74)
    print(f"  {'配置':<32}{'Recall@M':>10}{'MRR':>8}{'nDCG':>8}{'域级Recall':>11}{'平均耗时':>11}")
    print("-" * 74)
    for mode, _ in modes:
        r = results[mode]
        print(f"  {mode:<32}{r['recall']:>9.1f}%{r['mrr']:>8.3f}{r['ndcg']:>8.3f}"
              f"{r['recall_domain']:>10.1f}%{r['latency_ms']:>10.1f}ms")
    print("-" * 74)

    base = results["hybrid_rerank"]["recall"]
    for mode in ["hybrid_only", "dense_rerank"]:
        delta = results[mode]["recall"] - base
        print(f"  {mode} 相对生产配置(文件级 Recall): {delta:+.1f} pp")
    print("=" * 74)

    # ---------- 写报告 ----------
    _ensure_log_dir()
    path = os.path.join(config.LOG_DIR, "ablation_report.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 检索消融实验报告\n\n")
        f.write(f"- 实验时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"- 题目来源: `data/eval/retrieval_set.py`（{len(cases)} 题，措辞不照抄文档标题）\n")
        f.write(f"- 判定口径: Recall@M(文件级) —— 返回的 {config.CANDIDATE_M} 个父文档中是否含「正确答案所在文件」\n")
        f.write(f"- 附域级 Recall(旧口径, 每个域 35 篇, 偏松, 仅作参照)\n")
        f.write(f"- 注意: 三组均**不启用**相关性阈值过滤，以保证对比公平\n\n")

        f.write("## 结果对比\n\n")
        f.write("| 配置 | 说明 | Recall@M(文件) | MRR | nDCG | 域级Recall | 平均耗时 |\n|---|---|---|---|---|---|---|\n")
        for mode, desc in modes:
            r = results[mode]
            f.write(f"| `{mode}` | {desc} | {r['recall']:.1f}% | {r['mrr']:.3f} | {r['ndcg']:.3f} | "
                    f"{r['recall_domain']:.1f}% | {r['latency_ms']:.1f}ms |\n")
        f.write("\n")

        f.write("## 增量分析（相对生产配置 hybrid_rerank）\n\n")
        for mode in ["hybrid_only", "dense_rerank"]:
            d = results[mode]["recall"] - base
            dm = results[mode]["mrr"] - results["hybrid_rerank"]["mrr"]
            dn = results[mode]["ndcg"] - results["hybrid_rerank"]["ndcg"]
            f.write(f"- **`{mode}`**: Recall {d:+.1f} pp, MRR {dm:+.3f}, nDCG {dn:+.3f}")
            if d < -0.5:
                f.write(" → 该组件有正贡献\n")
            elif d > 0.5:
                f.write(" → 该组件反而有害，值得复查\n")
            else:
                f.write(" → 差异在噪声范围内，该组件可考虑移除（省成本/降延迟）\n")
        f.write("\n")

        f.write("## 逐题明细\n\n")
        f.write("| 题号 | 问题 | 期望文件 | " + " | ".join(m for m, _ in modes) + " |\n")
        f.write("|---|---|---|" + "---|" * len(modes) + "\n")
        for idx, case in enumerate(cases):
            cells = []
            for mode, _ in modes:
                rank = results[mode]["detail"][idx][1]
                cells.append(str(rank) if rank else "✗")
            expect_file = case.get("expect_file") or case["expect_source"]
            f.write(f"| {case['id']} | {case['query']} | {expect_file} | " + " | ".join(cells) + " |\n")

    print(f"\n报告已保存: {path}")
    return results


# ============================================================================
# 三、基线对照（去掉上下文，看模型是否编造）
# ============================================================================

# 裸模型用的 system prompt: 刻意不带「只能基于上下文」的约束,
# 这样才能测出模型「凭自身参数知识会怎么答」。
# 若沿用生产的 system prompt(其中强制"上下文不足就说没有"), 裸模型也会100%拒答,
# 对照就失效了 —— 这是一个必须避开的实验设计陷阱。
_BARE_SYSTEM = (
    "你是一个乐于助人的 AI 助手。请直接、简洁地回答用户的问题。"
    "如果你确实不知道答案, 可以如实说明。"
)


def run_baseline(limit=None):
    """
    基线对照: 同一套 system prompt, 一组给上下文(RAG), 一组不给(裸模型)。
    重点看越界题上裸模型会不会编造 —— 这是 RAG 反幻觉价值最直接的量化。
    """
    from data.eval.independent_set import get_cases
    from service.pipeline import QAPipeline
    from service.llm_client import LLMClient

    # 越界题(该拒答) + 少量可答题(看能否答出知识库内容)
    refuse_cases = get_cases(["越界"])
    answerable = [c for c in get_cases() if not c.get("should_refuse")][:5]
    cases = refuse_cases + answerable
    if limit:
        cases = cases[:limit]

    print("\n" + "=" * 74)
    print(f"  基线对照实验  (共 {len(cases)} 题: {len(refuse_cases)} 道越界题 + 5 道可答题)")
    print("  A 组 = RAG 完整链路（给检索上下文）")
    print("  B 组 = 裸模型（同一 prompt，但上下文为空）")
    print("  核心指标: 越界题上「如实拒答率」—— 裸模型编造 = 幻觉")
    print("=" * 74)

    pipeline = QAPipeline()
    llm = LLMClient()
    rows = []
    t0 = time.time()

    for i, case in enumerate(cases, 1):
        q = case["query"]

        # A 组: 完整链路(带检索上下文)
        try:
            ra = pipeline.answer(q)
        except Exception as e:
            ra = {"layer": "error", "answer": f"[异常] {e}"}
        a_kind = _classify_refusal(ra.get("layer"), ra.get("answer") or "")
        a_refuse = a_kind in ("arch", "self")

        # B 组: 裸模型 —— 不给上下文, 且换成不带「只能基于上下文」约束的 prompt
        try:
            ans_b = llm.generate(_BARE_SYSTEM, q)
        except Exception as e:
            ans_b = f"[异常] {e}"
        b_refuse = _is_refusal(ans_b)

        rows.append(dict(case=case, a=ra, a_kind=a_kind, a_refuse=a_refuse,
                         b=ans_b, b_refuse=b_refuse))
        # 越界题看三分类; 可答题只看"有没有作答"
        if case.get("should_refuse"):
            a_label = {"arch": "架构拦截", "self": "模型自觉拒答", "halluc": "编造"}[a_kind]
        else:
            a_label = "作答" if ra.get("layer") != "fallback" else "拒答"
        _print_progress(i, len(cases), q,
                        f"A={a_label} / B={'拒答' if b_refuse else '作答'}")

    elapsed = time.time() - t0

    # ---------- 统计 ----------
    ref_rows = [r for r in rows if r["case"].get("should_refuse")]
    ans_rows = [r for r in rows if not r["case"].get("should_refuse")]

    a_refuse_ok = sum(1 for r in ref_rows if r["a_refuse"])
    b_refuse_ok = sum(1 for r in ref_rows if r["b_refuse"])
    b_halluc = len(ref_rows) - b_refuse_ok
    a_halluc = len(ref_rows) - a_refuse_ok
    a_arch = sum(1 for r in ref_rows if r["a_kind"] == "arch")
    a_self = sum(1 for r in ref_rows if r["a_kind"] == "self")

    print("\n" + "-" * 74)
    print(f"  【越界题 {len(ref_rows)} 道】越界处理方式")
    print(f"    A 组 RAG 链路 : 诚实拒答 {a_refuse_ok}/{len(ref_rows)} = "
          f"{_pct(a_refuse_ok, len(ref_rows)):.1f}%   (编造 {a_halluc} 题)")
    print(f"      ├─ 架构拦截(未调LLM) : {a_arch} 题")
    print(f"      └─ 模型自觉拒答      : {a_self} 题  ← 阈值偏松, 可优化点")
    print(f"    B 组 裸模型   : 拒答 {b_refuse_ok}/{len(ref_rows)} = "
          f"{_pct(b_refuse_ok, len(ref_rows)):.1f}%   (编造 {b_halluc} 题)")
    delta = _pct(a_refuse_ok, len(ref_rows)) - _pct(b_refuse_ok, len(ref_rows))
    print(f"    → 反幻觉增益 : {delta:+.1f} pp")
    print("-" * 74)
    print(f"  【可答题 {len(ans_rows)} 道】作答率（不该拒答）")
    # 可答题只看「有没有真的作答」= 没走兜底; 不能用 a_refuse(它含"答案里有抱歉"的语义)
    a_answered = sum(1 for r in ans_rows if r["a"].get("layer") != "fallback")
    b_answered = sum(1 for r in ans_rows if not r["b_refuse"])
    print(f"    A 组 : {a_answered}/{len(ans_rows)} 作答")
    print(f"    B 组 : {b_answered}/{len(ans_rows)} 作答")
    print(f"  耗时: {elapsed:.0f}s")
    print("=" * 74)

    # ---------- 写报告 ----------
    _ensure_log_dir()
    path = os.path.join(config.LOG_DIR, "baseline_report.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 基线对照实验报告（RAG vs 裸模型）\n\n")
        f.write(f"- 实验时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("- 对照设计: A 组 = 完整 RAG 链路（带检索上下文）；"
                "B 组 = 裸模型（不给上下文，且**换用不带「只能基于上下文」约束**的 prompt）\n")
        f.write("- 为什么 B 组要换 prompt：若沿用生产 system prompt（其中强制“上下文不足就说没找到”），"
                "裸模型也会 100% 拒答，对照会完全失效——这是本实验踩过的坑\n")
        f.write("- 核心逻辑: 越界题（知识库无答案）上，裸模型若给出具体内容 = 编造 = 幻觉\n\n")

        f.write("## 越界题：处理方式对比\n\n")
        f.write("| 组别 | 诚实拒答 | 总数 | 拒答率 | 编造（幻觉） |\n|---|---|---|---|---|\n")
        f.write(f"| A 组 RAG 链路 | {a_refuse_ok} | {len(ref_rows)} | "
                f"{_pct(a_refuse_ok, len(ref_rows)):.1f}% | {a_halluc} |\n")
        f.write(f"| B 组 裸模型 | {b_refuse_ok} | {len(ref_rows)} | "
                f"{_pct(b_refuse_ok, len(ref_rows)):.1f}% | {b_halluc} |\n")
        f.write(f"\n**反幻觉增益：{delta:+.1f} pp**\n\n")

        f.write("### A 组的分层细节（架构拦截 vs 模型自觉）\n\n")
        f.write("| 处理方式 | 题数 | 含义 |\n|---|---|---|\n")
        f.write(f"| 架构拦截 | {a_arch} | 兜底层直接拒答，**完全没调用 LLM**，零编造风险、零成本 |\n")
        f.write(f"| 模型自觉拒答 | {a_self} | 进了 RAG 层但 LLM 诚实说没有——能兜住，但**依赖模型自觉**，建议收紧相关性阈值 |\n")
        f.write("\n> 架构拦截的比例越高越好。若「模型自觉」占比偏高，说明相关性阈值偏松，"
                "不相关文档也能过阈值进入 LLM，属于可优化项。\n\n")

        f.write("## 逐题对照\n\n")
        f.write("| 题号 | 问题 | 应否拒答 | A组(RAG) | B组(裸模型) | B组回答摘录 |\n")
        f.write("|---|---|---|---|---|---|\n")
        for r in rows:
            c = r["case"]
            should = "是" if c.get("should_refuse") else "否"
            if c.get("should_refuse"):
                a_s = {"arch": "架构拦截", "self": "模型自觉拒答", "halluc": "编造"}.get(r["a_kind"], "?")
            else:
                a_s = "作答" if r["a"].get("layer") != "fallback" else "拒答"
            b_s = "拒答" if r["b_refuse"] else "作答"
            b_ex = (r["b"] or "").replace("\n", " ")[:80]
            f.write(f"| {c['id']} | {c['query']} | {should} | {a_s} | {b_s} | {b_ex} |\n")

    print(f"\n报告已保存: {path}")
    return dict(a_refuse=a_refuse_ok, b_refuse=b_refuse_ok,
                total=len(ref_rows), delta=delta)


# ============================================================================
# 入口
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="项目质量评估: 独立测试集 / 检索消融 / 基线对照")
    parser.add_argument("--independent", action="store_true", help="跑独立测试集(40题)")
    parser.add_argument("--ablation", action="store_true", help="跑检索消融(32题)")
    parser.add_argument("--baseline", action="store_true", help="跑基线对照(RAG vs 裸模型)")
    parser.add_argument("--all", action="store_true", help="三个全跑")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 题(调试用)")
    parser.add_argument("--category", default=None,
                        help="独立测试集只跑某类: 口语化/错别字/错误码/混合意图/越界")
    args = parser.parse_args()

    if not (args.independent or args.ablation or args.baseline or args.all):
        parser.print_help()
        print("\n提示: 至少指定 --independent / --ablation / --baseline / --all 之一")
        return

    if args.independent or args.all:
        run_independent(limit=args.limit, category=args.category)
    if args.ablation or args.all:
        run_ablation(limit=args.limit)
    if args.baseline or args.all:
        run_baseline(limit=args.limit)


if __name__ == "__main__":
    main()

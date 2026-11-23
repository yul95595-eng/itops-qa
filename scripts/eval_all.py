# -*- coding: utf-8 -*-
# scripts/eval_all.py
# 一键评测 —— 跑一套测试集, 产出一份「可对比」的结构化结果
#
# ============================================================================
# 它解决什么问题
# ============================================================================
# 之前: 7 个脚本各自为战, 结果写成 md 散文。
#   改一个参数之后, 你不知道:
#     - 现在是变好了还是变坏了
#     - 坏在哪几道题上
#     - 上一次的 40 题和这一次的 40 题是不是同一批
#
# 现在: 一条命令 -> 一个 json
#   logs/eval_runs/20261118_2015_quality_baseline.json
#   里面有: 指标 + 逐题明细 + 数据集 sha256 + 参数快照 + 耗时
#   然后用 eval_compare.py 对比任意两次。
#
# 用法:
#   python scripts/eval_all.py --suite quality --tag baseline
#   python scripts/eval_all.py --suite quality --tag rerank_off --note "关掉 reranker"
#   python scripts/eval_all.py --suite both  --tag baseline
#   python scripts/eval_all.py --list                 # 看历史 run
#   python scripts/eval_all.py --suite quality --limit 5   # 冒烟(只跑前 5 题)
#
# 产物: logs/eval_runs/<run_id>.json
# ============================================================================
import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base.config import config

DATASET_DIR = os.path.join(config.PROJECT_ROOT, "data", "eval", "datasets")
RUN_DIR = os.path.join(config.LOG_DIR, "eval_runs")
MANIFEST = os.path.join(DATASET_DIR, "manifest.json")

QUALITY_FILE = "{v}.jsonl"
RETRIEVAL_FILE = "{v}.retrieval.jsonl"


# ============================================================================
# 通用
# ============================================================================

def _pct(a, b):
    return round(a / b * 100, 2) if b else 0.0


def _percentile(values, p):
    """不引第三方, 手写百分位"""
    if not values:
        return 0
    vs = sorted(values)
    if len(vs) == 1:
        return vs[0]
    idx = (len(vs) - 1) * p
    lo, hi = int(idx), min(int(idx) + 1, len(vs) - 1)
    return vs[lo] + (vs[hi] - vs[lo]) * (idx - lo)


def _latency_stats(ms_list):
    if not ms_list:
        return {}
    return dict(
        mean=round(statistics.mean(ms_list), 1),
        p50=round(_percentile(ms_list, 0.50), 1),
        p95=round(_percentile(ms_list, 0.95), 1),
        max=round(max(ms_list), 1),
    )


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git_commit():
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=config.PROJECT_ROOT, capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _config_snapshot():
    """参数快照: 对比两次 run 时, 一眼看出改了什么"""
    keys = [
        "RETRIEVAL_K", "CANDIDATE_M", "RERANK_SCORE_THRESHOLD",
        "LLM_MODEL", "EMBEDDING_MODEL", "PARENT_CHUNK_SIZE", "CHILD_CHUNK_SIZE",
    ]
    snap = {}
    for k in keys:
        v = getattr(config, k, None)
        if v is not None:
            snap[k] = v
    return snap


def _load_manifest():
    if not os.path.exists(MANIFEST):
        raise SystemExit(
            "找不到 data/eval/datasets/manifest.json\n"
            "先跑: python scripts/export_dataset.py"
        )
    with open(MANIFEST, encoding="utf-8") as f:
        return json.load(f)


def _load_cases(version, suite):
    fname = (QUALITY_FILE if suite == "quality" else RETRIEVAL_FILE).format(v=version)
    path = os.path.join(DATASET_DIR, fname)
    if not os.path.exists(path):
        raise SystemExit(f"找不到测试集 {path}\n先跑: python scripts/export_dataset.py")
    raw = open(path, encoding="utf-8").read()
    cases = [json.loads(line) for line in raw.splitlines() if line.strip()]
    return cases, _sha256(raw)


# ============================================================================
# suite 1: quality —— 端到端答对率
# ============================================================================

def run_quality(cases, limit=None):
    """
    走完整 pipeline.answer(), 判定逻辑直接复用 eval_quality.py 的 _judge_independent,
    保证「新框架的数和旧报告的数」是同一个口径, 不出现两套判定。
    """
    from scripts.eval_quality import _judge_independent, _classify_refusal
    from service.pipeline import QAPipeline

    if limit:
        cases = cases[:limit]

    pipeline = QAPipeline()
    rows, latencies, errors = [], [], 0
    t_all = time.time()

    print(f"\n[quality] 共 {len(cases)} 题, 走完整链路 (耗时较长, 请耐心)")
    print("-" * 74)

    for i, case in enumerate(cases, 1):
        t0 = time.time()
        try:
            result = pipeline.answer(case["query"])
            crashed = False
        except Exception as e:
            result = {"layer": "error", "answer": f"[评测异常] {e}"}
            crashed = True
            errors += 1
        cost_ms = (time.time() - t0) * 1000
        latencies.append(cost_ms)

        ok, judge = _judge_independent(case, result)
        if crashed:
            ok, judge = False, f"❌ 异常: {result['answer'][:60]}"

        answer = (result.get("answer") or "").strip()
        rows.append(dict(
            id=case["id"],
            query=case["query"],
            category=case.get("category", ""),
            should_refuse=bool(case.get("should_refuse")),
            layer=result.get("layer"),
            ok=ok,
            judge=judge,
            latency_ms=round(cost_ms, 1),
            answer_len=len(answer),
            answer_head=answer[:200],
        ))
        q = case["query"] if len(case["query"]) <= 22 else case["query"][:22] + "…"
        print(f"  [{i:>2}/{len(cases)}] {q:<24} {judge}")

    # ---------- 聚合 ----------
    total = len(rows)
    correct = sum(1 for r in rows if r["ok"])

    by_cat = {}
    for r in rows:
        c = by_cat.setdefault(r["category"], dict(correct=0, total=0))
        c["total"] += 1
        if r["ok"]:
            c["correct"] += 1
    for c in by_cat.values():
        c["accuracy"] = _pct(c["correct"], c["total"])

    refuse = [r for r in rows if r["should_refuse"]]
    answerable = [r for r in rows if not r["should_refuse"]]
    kinds = {"arch": 0, "self": 0, "halluc": 0}
    for r in refuse:
        kinds[_classify_refusal(r["layer"], r["answer_head"])] += 1
    refuse_ok = kinds["arch"] + kinds["self"]
    false_refuse = sum(1 for r in answerable if r["layer"] == "fallback")

    layer_dist = {}
    for r in rows:
        layer_dist[r["layer"]] = layer_dist.get(r["layer"], 0) + 1

    metrics = dict(
        total=total,
        correct=correct,
        accuracy=_pct(correct, total),
        by_category=by_cat,
        refuse=dict(
            total=len(refuse), ok=refuse_ok,
            accuracy=_pct(refuse_ok, len(refuse)),
            arch=kinds["arch"], self=kinds["self"], halluc=kinds["halluc"],
        ),
        false_refuse=dict(
            count=false_refuse, total=len(answerable),
            rate=_pct(false_refuse, len(answerable)),
        ),
        layer_dist=layer_dist,
        latency_ms=_latency_stats(latencies),
        error_count=errors,
    )

    print("-" * 74)
    print(f"  答对率        {correct}/{total} = {metrics['accuracy']}%")
    print(f"  拒答准确率    {refuse_ok}/{len(refuse)} = {metrics['refuse']['accuracy']}%"
          f"  (架构 {kinds['arch']} / 自觉 {kinds['self']} / 编造 {kinds['halluc']})")
    print(f"  误拒率        {false_refuse}/{len(answerable)} = {metrics['false_refuse']['rate']}%")
    print(f"  延迟          P50 {metrics['latency_ms'].get('p50', 0)}ms  "
          f"P95 {metrics['latency_ms'].get('p95', 0)}ms")
    if errors:
        print(f"  ⚠️  异常 {errors} 题 —— 基础设施可能没起, 这份结果不能当基线")
    print(f"  总耗时        {time.time() - t_all:.0f}s")
    return metrics, rows


# ============================================================================
# suite 2: retrieval —— 检索层召回 (文件级)
# ============================================================================

def run_retrieval(cases, limit=None):
    """
    只测检索层, 不调 LLM 生成 —— 消融实验用。
    判定口径: 文件级 (返回的父块是否来自 expect_file 那一篇), 比域级严格。
    """
    from retrieval.vector_store import VectorStore

    if limit:
        cases = cases[:limit]

    vs = VectorStore()
    rows, latencies, errors = [], [], 0
    t_all = time.time()

    print(f"\n[retrieval] 共 {len(cases)} 题, 只测检索+重排, 不生成")
    print("-" * 74)

    for i, case in enumerate(cases, 1):
        t0 = time.time()
        rank = None
        try:
            docs = vs.hybrid_search_with_rerank(case["query"], k=config.RETRIEVAL_K)
            for pos, d in enumerate(docs, 1):
                if (d.metadata or {}).get("filename") == case["expect_file"]:
                    rank = pos
                    break
            crashed = False
        except Exception as e:
            docs, rank, crashed = [], None, True
            errors += 1
            print(f"  [{i:>2}/{len(cases)}] 异常: {e}")

        cost_ms = (time.time() - t0) * 1000
        latencies.append(cost_ms)
        hit = rank is not None

        rows.append(dict(
            id=case["id"],
            query=case["query"],
            expect_source=case.get("expect_source"),
            expect_file=case.get("expect_file"),
            hit=hit,
            rank=rank,
            top1_file=((docs[0].metadata or {}).get("filename") if docs else None),
            latency_ms=round(cost_ms, 1),
        ))
        if not crashed:
            q = case["query"] if len(case["query"]) <= 22 else case["query"][:22] + "…"
            print(f"  [{i:>2}/{len(cases)}] {q:<24} "
                  f"{'✅ rank=' + str(rank) if hit else '❌ 未命中'}  {cost_ms:.0f}ms")

    total = len(rows)
    hit_n = sum(1 for r in rows if r["hit"])
    mrr = sum(1 / r["rank"] for r in rows if r["rank"]) / total if total else 0

    metrics = dict(
        total=total,
        hit=hit_n,
        recall_at_m=_pct(hit_n, total),
        mrr=round(mrr, 4),
        top_m=config.CANDIDATE_M,
        latency_ms=_latency_stats(latencies),
        error_count=errors,
    )

    print("-" * 74)
    print(f"  Recall@{config.CANDIDATE_M} (文件级)  {hit_n}/{total} = {metrics['recall_at_m']}%")
    print(f"  MRR                  {mrr:.4f}")
    print(f"  延迟                 P50 {metrics['latency_ms'].get('p50', 0)}ms  "
          f"P95 {metrics['latency_ms'].get('p95', 0)}ms")
    if errors:
        print(f"  ⚠️  异常 {errors} 题 —— 检查 Milvus 是否就绪")
    print(f"  总耗时               {time.time() - t_all:.0f}s")
    return metrics, rows


# ============================================================================
# 落盘
# ============================================================================

def _new_run_id(suite, tag):
    base = f"{datetime.now().strftime('%Y%m%d_%H%M')}_{suite}_{tag}"
    path = os.path.join(RUN_DIR, base + ".json")
    n = 1
    while os.path.exists(path):
        path = os.path.join(RUN_DIR, f"{base}-{n}.json")
        n += 1
    return os.path.splitext(os.path.basename(path))[0]


def _save(run):
    os.makedirs(RUN_DIR, exist_ok=True)
    path = os.path.join(RUN_DIR, run["run_id"] + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(run, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {path}")
    return path


def cmd_list():
    if not os.path.isdir(RUN_DIR):
        print("还没有任何评测结果")
        return
    files = sorted(f for f in os.listdir(RUN_DIR) if f.endswith(".json"))
    if not files:
        print("还没有任何评测结果")
        return
    print(f"{'run_id':<46}{'数据集':<8}{'指标':<26}{'耗时':>7}")
    print("-" * 92)
    for fn in files:
        try:
            r = json.load(open(os.path.join(RUN_DIR, fn), encoding="utf-8"))
        except Exception:
            continue
        m = r.get("metrics", {})
        if r.get("suite") == "quality":
            main = f"答对 {m.get('accuracy')}%  误拒 {m.get('false_refuse', {}).get('rate')}%"
        else:
            main = f"R@{m.get('top_m')} {m.get('recall_at_m')}%  MRR {m.get('mrr')}"
        print(f"{r.get('run_id', fn):<46}{r.get('dataset_version', '?'):<8}"
              f"{main:<26}{r.get('duration_sec', 0):>6.0f}s")


def main():
    ap = argparse.ArgumentParser(description="一键评测(结构化输出)")
    ap.add_argument("--suite", choices=["quality", "retrieval", "both"], default="quality")
    ap.add_argument("--dataset", default=None, help="数据集版本, 默认取 manifest.current")
    ap.add_argument("--tag", default="run", help="人类可读标签, 如 baseline / rerank_off")
    ap.add_argument("--note", default="", help="这次改了什么(会写进结果)")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 题(冒烟用)")
    ap.add_argument("--list", action="store_true", help="列出历史结果")
    args = ap.parse_args()

    if args.list:
        cmd_list()
        return

    manifest = _load_manifest()
    version = args.dataset or manifest.get("current")
    if not version:
        raise SystemExit("manifest 没有 current 版本, 先跑 export_dataset.py")

    suites = ["quality", "retrieval"] if args.suite == "both" else [args.suite]
    started = datetime.now()

    runs = []
    for suite in suites:
        cases, ds_hash = _load_cases(version, suite)
        recorded = manifest["versions"].get(version, {}).get(suite, {}).get("sha256")
        if recorded and recorded != ds_hash:
            print(f"⚠️  数据集 {version}/{suite} 的 sha256 与 manifest 不符 "
                  f"(文件被手工改过?), 结果仍会记录实际 hash")

        t0 = time.time()
        metrics, rows = (run_quality if suite == "quality" else run_retrieval)(cases, args.limit)
        duration = time.time() - t0

        run = dict(
            schema_version=1,
            run_id=_new_run_id(suite, args.tag),
            suite=suite,
            dataset_version=version,
            dataset_sha256=ds_hash,
            dataset_cases=len(cases),
            limit=args.limit,
            tag=args.tag,
            note=args.note,
            git_commit=_git_commit(),
            started_at=started.strftime("%Y-%m-%d %H:%M:%S"),
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            duration_sec=round(duration, 1),
            config=_config_snapshot(),
            metrics=metrics,
            cases=rows,
        )
        _save(run)
        runs.append(run)

    if len(runs) > 1:
        print("\n对比两次以上结果: python scripts/eval_compare.py --base <旧.json> --cand <新.json>")


if __name__ == "__main__":
    main()

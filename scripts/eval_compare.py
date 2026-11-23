# -*- coding: utf-8 -*-
# scripts/eval_compare.py
# 对比两次评测 —— 这次比上次好还是坏, 坏在哪几道题上
#
# ============================================================================
# 为什么需要它
# ============================================================================
# 只有"跑一次出个分数"不叫评测闭环。真正的闭环是:
#   改一个参数 -> 跑一次 -> 和上一次比 -> 看是哪几道题退化 -> 决定留不留
#
# 这一条命令回答三个问题:
#   1. 总体指标变了多少(涨了几个点)
#   2. 哪些题从对变错、哪些从错变对(可以逐题点开看)
#   3. 两次用的测试集是不是同一批(sha256 不一致 -> 对比无意义)
#
# 用法:
#   python scripts/eval_compare.py --base logs/eval_runs/a.json --cand logs/eval_runs/b.json
#   python scripts/eval_compare.py --base baseline --cand latest       # 按 tag 找最近两次
#   python scripts/eval_compare.py --base baseline --cand latest --max-drop 2   # 掉超 2pp 退出码 1
#   python scripts/eval_compare.py ... --md logs/compare_report.md     # 顺带写一份可贴的报告
# ============================================================================
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base.config import config

RUN_DIR = os.path.join(config.LOG_DIR, "eval_runs")


# ============================================================================
# 载入
# ============================================================================

def _all_runs():
    if not os.path.isdir(RUN_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(RUN_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            out.append(json.load(open(os.path.join(RUN_DIR, fn), encoding="utf-8")))
        except Exception:
            continue
    return sorted(out, key=lambda r: r.get("finished_at", ""))


def _resolve(spec):
    """spec 可以是文件路径 / run_id / tag / latest / latest~1"""
    if os.path.isfile(spec):
        return json.load(open(spec, encoding="utf-8"))

    runs = _all_runs()
    if not runs:
        raise SystemExit(f"没有找到任何评测结果（目录: {RUN_DIR}）")

    if spec == "latest":
        return runs[-1]
    if spec.startswith("latest~"):
        n = int(spec.split("~")[1])
        if n >= len(runs):
            raise SystemExit(f"只有 {len(runs)} 份结果，取不到 latest~{n}")
        return runs[-1 - n]

    by_id = [r for r in runs if r.get("run_id") == spec]
    if by_id:
        return by_id[-1]

    by_tag = [r for r in runs if r.get("tag") == spec]
    if len(by_tag) == 1:
        return by_tag[0]
    if len(by_tag) > 1:
        # 静默取最后一个会让"基准选错"变成一件无声的事 —— 宁可直接报错
        raise SystemExit(
            f"tag '{spec}' 匹配到 {len(by_tag)} 份结果，无法确定用哪份。请改用 run_id：\n"
            + "\n".join(
                f"   {r['run_id']:<46} suite={r.get('suite'):<10} {r.get('finished_at')}"
                for r in by_tag
            )
        )

    raise SystemExit(f"找不到 '{spec}'。可用: python scripts/eval_all.py --list")


# ============================================================================
# 对比
# ============================================================================

def _delta(base, cand, unit="pp", reverse=False):
    """返回 (变化量, 是否变差)"""
    if base is None or cand is None:
        return None, False
    d = cand - base
    worse = (d < 0) if not reverse else (d > 0)
    return d, worse


def _fmt_delta(d, unit="pp"):
    if d is None:
        return "—"
    arrow = "↑" if d > 0 else ("↓" if d < 0 else "=")
    return f"{arrow}{abs(d):.2f}{unit}"


def compare(base, cand, max_drop=None, md_path=None):
    # ---------- 前置校验 ----------
    # suite 不同就没有任何可比性, 直接拒绝, 不要算出一堆 "—" 让人误以为能看
    if base.get("suite") != cand.get("suite"):
        raise SystemExit(
            f"suite 不同（{base.get('suite')} vs {cand.get('suite')}），无法对比。\n"
            f"  基线: {base['run_id']}\n  候选: {cand['run_id']}\n"
            "用 --base / --cand 指定同一 suite 的结果, 或先跑 --list 看有哪些。"
        )

    problems = []
    if base.get("dataset_version") != cand.get("dataset_version"):
        problems.append(f"数据集版本不同: {base.get('dataset_version')} vs {cand.get('dataset_version')}")
    if base.get("dataset_sha256") != cand.get("dataset_sha256"):
        problems.append("数据集内容不同(sha256 不一致) —— 下面的差异不全是代码改动带来的")
    if base.get("limit") != cand.get("limit"):
        problems.append(f"跑了不同题数: limit={base.get('limit')} vs {cand.get('limit')}")

    suite = base.get("suite")
    bm, cm = base["metrics"], cand["metrics"]

    print("=" * 78)
    print(f"  评测对比 · {suite}")
    print(f"  基线: {base['run_id']}   tag={base.get('tag')}   {base.get('finished_at')}")
    print(f"  候选: {cand['run_id']}   tag={cand.get('tag')}   {cand.get('finished_at')}")
    print(f"  数据集: {base.get('dataset_version')}  "
          f"sha256 {str(base.get('dataset_sha256'))[:12]} vs {str(cand.get('dataset_sha256'))[:12]}")
    print("=" * 78)

    if problems:
        print("\n⚠️  可比较性告警：")
        for p in problems:
            print(f"   - {p}")
        print("   （指标仍然算，但结论要打折看）")

    # ---------- 参数差异 ----------
    bc, cc = base.get("config", {}), cand.get("config", {})
    changed = {k: (bc.get(k), cc.get(k)) for k in set(bc) | set(cc) if bc.get(k) != cc.get(k)}
    if changed:
        print("\n参数变化：")
        for k, (b, c) in sorted(changed.items()):
            print(f"   {k:<26} {b}  →  {c}")

    # ---------- 指标表 ----------
    rows = []
    if suite == "quality":
        rows = [
            ("答对率", bm.get("accuracy"), cm.get("accuracy"), "pp", False),
            ("拒答准确率", bm.get("refuse", {}).get("accuracy"),
             cm.get("refuse", {}).get("accuracy"), "pp", False),
            ("编造数(越界题)", bm.get("refuse", {}).get("halluc"),
             cm.get("refuse", {}).get("halluc"), "", True),
            ("误拒率", bm.get("false_refuse", {}).get("rate"),
             cm.get("false_refuse", {}).get("rate"), "pp", True),
            ("延迟 P50", bm.get("latency_ms", {}).get("p50"),
             cm.get("latency_ms", {}).get("p50"), "ms", True),
            ("延迟 P95", bm.get("latency_ms", {}).get("p95"),
             cm.get("latency_ms", {}).get("p95"), "ms", True),
            ("异常数", bm.get("error_count"), cm.get("error_count"), "", True),
        ]
    else:
        rows = [
            (f"Recall@{bm.get('top_m')}", bm.get("recall_at_m"), cm.get("recall_at_m"), "pp", False),
            ("MRR", bm.get("mrr"), cm.get("mrr"), "", False),
            ("延迟 P50", bm.get("latency_ms", {}).get("p50"),
             cm.get("latency_ms", {}).get("p50"), "ms", True),
            ("延迟 P95", bm.get("latency_ms", {}).get("p95"),
             cm.get("latency_ms", {}).get("p95"), "ms", True),
            ("异常数", bm.get("error_count"), cm.get("error_count"), "", True),
        ]

    print(f"\n{'指标':<20}{'基线':>12}{'候选':>12}{'变化':>14}")
    print("-" * 78)
    md_rows = []
    for name, b, c, unit, reverse in rows:
        d, worse = _delta(b, c, unit, reverse)
        flag = "" if d is None else ("  ⚠️" if worse else ("  ✅" if (d != 0 and not worse) else ""))
        print(f"{name:<20}{_show(b, unit):>12}{_show(c, unit):>12}{_fmt_delta(d, unit):>14}{flag}")
        md_rows.append((name, _show(b, unit), _show(c, unit), _fmt_delta(d, unit) + flag))

    # ---------- 逐题差异 ----------
    b_cases = {c["id"]: c for c in base.get("cases", [])}
    c_cases = {c["id"]: c for c in cand.get("cases", [])}
    common = sorted(set(b_cases) & set(c_cases))
    gone = sorted(set(b_cases) - set(c_cases))
    new = sorted(set(c_cases) - set(b_cases))

    if suite == "quality":
        regress = [i for i in common if b_cases[i]["ok"] and not c_cases[i]["ok"]]
        improve = [i for i in common if not b_cases[i]["ok"] and c_cases[i]["ok"]]
    else:
        regress = [i for i in common if b_cases[i]["hit"] and not c_cases[i]["hit"]]
        improve = [i for i in common if not b_cases[i]["hit"] and c_cases[i]["hit"]]

    print(f"\n逐题差异（共比对 {len(common)} 题）")
    print("-" * 78)
    if not regress and not improve:
        print("  没有任何一题翻转 —— 指标变化只来自延迟/分布，不来自对错。")

    if regress:
        print(f"  退化 {len(regress)} 题：")
        for i in regress:
            b, c = b_cases[i], c_cases[i]
            if suite == "quality":
                print(f"    ❌ {i:<5}{_clip(b['query']):<26} layer {b['layer']} → {c['layer']}")
                print(f"       基线: {b['judge']}")
                print(f"       候选: {c['judge']}")
            else:
                print(f"    ❌ {i:<5}{_clip(b['query']):<26} rank {b['rank']} → 未命中")

    if improve:
        print(f"  改善 {len(improve)} 题：" + "、".join(improve))

    if gone:
        print(f"  只在基线里（{len(gone)} 题）: " + "、".join(gone))
    if new:
        print(f"  只在候选里（{len(new)} 题）: " + "、".join(new))

    # ---------- 门槛 ----------
    exit_code = 0
    if max_drop is not None:
        main_name, main_b, main_c = (("答对率", bm.get("accuracy"), cm.get("accuracy"))
                                     if suite == "quality"
                                     else (f"Recall@{bm.get('top_m')}",
                                           bm.get("recall_at_m"), cm.get("recall_at_m")))
        if main_b is not None and main_c is not None:
            drop = main_b - main_c
            print("-" * 78)
            if drop > max_drop:
                print(f"❌ 回归门槛未通过: {main_name} 下降 {drop:.2f}pp > 允许的 {max_drop}pp")
                exit_code = 1
            else:
                print(f"✅ 回归门槛通过: {main_name} 变化 {main_c - main_b:+.2f}pp "
                      f"(允许下降 {max_drop}pp)")

    print("=" * 78)

    if md_path:
        _write_md(md_path, base, cand, md_rows, regress, improve, suite, problems)
        print(f"Markdown 报告: {md_path}")

    return exit_code


def _show(v, unit):
    if v is None:
        return "—"
    if unit == "pp":
        return f"{v:.2f}%"
    if unit == "ms":
        return f"{v:.0f}ms"
    return f"{v}"


def _clip(s, n=24):
    s = str(s)
    return s if len(s) <= n else s[:n] + "…"


def _write_md(path, base, cand, md_rows, regress, improve, suite, problems):
    lines = [
        f"# 评测对比 · {suite}",
        "",
        f"| | 基线 | 候选 |",
        f"|---|---|---|",
        f"| run | `{base['run_id']}` | `{cand['run_id']}` |",
        f"| tag | {base.get('tag')} | {cand.get('tag')} |",
        f"| 数据集 | {base.get('dataset_version')} / `{str(base.get('dataset_sha256'))[:12]}` "
        f"| {cand.get('dataset_version')} / `{str(cand.get('dataset_sha256'))[:12]}` |",
        f"| 完成时间 | {base.get('finished_at')} | {cand.get('finished_at')} |",
        "",
    ]
    if problems:
        lines += ["## ⚠️ 可比较性告警", ""] + [f"- {p}" for p in problems] + [""]

    lines += ["## 指标", "", "| 指标 | 基线 | 候选 | 变化 |", "|---|---|---|---|"]
    for name, b, c, d in md_rows:
        lines.append(f"| {name} | {b} | {c} | {d} |")

    lines += ["", "## 逐题差异", ""]
    if not regress and not improve:
        lines.append("没有题目翻转。")
    else:
        if regress:
            lines.append(f"**退化 {len(regress)} 题**：" + "、".join(f"`{i}`" for i in regress))
            lines.append("")
        if improve:
            lines.append(f"**改善 {len(improve)} 题**：" + "、".join(f"`{i}`" for i in improve))
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description="对比两次评测结果")
    ap.add_argument("--base", required=True, help="基线: 文件路径 / run_id / tag / latest~1")
    ap.add_argument("--cand", default="latest", help="候选: 同上, 默认 latest")
    ap.add_argument("--max-drop", type=float, default=None,
                    help="主指标允许的最大下降(pp), 超出则退出码 1")
    ap.add_argument("--md", default=None, help="同时写一份 markdown 报告到该路径")
    args = ap.parse_args()

    base, cand = _resolve(args.base), _resolve(args.cand)
    if base.get("run_id") == cand.get("run_id"):
        raise SystemExit("基线和候选是同一份结果")

    sys.exit(compare(base, cand, args.max_drop, args.md))


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
# scripts/evaluate.py
# 效果评测脚本 —— 三层检索路由的量化评测
#
# 用法:
#   python scripts/evaluate.py            # 全量评测(FAQ全量 + 错误码抽样 + RAG抽样)
#   python scripts/evaluate.py --quick    # 快速评测(每层抽样, 适合快速验证)
#
# 评测维度:
#   第1层 错误码精确匹配: 准确率(抽取+匹配是否正确)
#   第2层 FAQ/BM25:      Top1 命中率(召回的问题是否=标准问题)
#   第3层 RAG 向量检索:  相关性(检索结果 rerank 分数是否达标) + 是否检索到对应域
#
# 输出:
#   控制台打印结果表格 + 保存评测报告到 logs/eval_report.md
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base.config import config
from base.logger import logger


# ==================== 评测集构造 ====================

def build_error_code_cases():
    """从种子数据构造错误码评测集: (输入语句, 期望错误码)

    只选取「正则设计能识别的格式化错误码」:
    - 纯数字(3~5位): 1045/691/404/500
    - ORA-xxxx / 0xXXXXXXXX / ERR_XXX / WRONGPASS/OOM 等关键字
    排除「整句文本型错误消息」(如 "No space left on device"),
    这类属于长文本, 本应由 RAG 层处理, 不是精确匹配层的职责。
    """
    import re
    from data.seed.error_codes_large import LARGE_ERROR_CODES
    from data.seed.error_code_seed import ERROR_CODES

    # 判断错误码是否可被 extract_error_code 正则识别
    def is_extractable(code):
        c = code.strip()
        if re.fullmatch(r'\d{3,5}', c):            # 纯数字
            return True
        if re.fullmatch(r'0x[0-9A-Fa-f]{8}', c):   # Windows hex
            return True
        if re.fullmatch(r'ORA-\d{4,5}', c):        # Oracle
            return True
        if re.fullmatch(r'ERR_[A-Z_]+', c):        # 浏览器/网络
            return True
        if re.fullmatch(r'(WRONGPASS|LOADING|READONLY|NOAUTH|OOM|MISCONF|BUSYKEY)', c):  # Redis 关键字
            return True
        return False

    cases = []
    for code, comp, meaning, cause, sol in LARGE_ERROR_CODES:
        if is_extractable(code):
            cases.append((f"数据库报错 error {code} 怎么处理", code))
    for code, comp, meaning, cause, sol in ERROR_CODES:
        if is_extractable(code):
            cases.append((f"系统提示 {code} 错误", code))
    return cases


def build_faq_cases():
    """从 FAQ 种子构造评测集: (问题, 标准答案)"""
    from data.seed.faq_seed import FAQ_SEED
    # 直接复用标准问题作为评测(测试"标准问法是否命中")
    cases = [(q, a) for q, a, _ in FAQ_SEED]
    return cases


def build_faq_rewrite_cases():
    """构造 FAQ「改写问法」评测集: 模拟真实用户口语化/同义改写, 测试鲁棒性

    每条: (改写后的问题, 期望命中的标准问题关键词, 期望答案关键词)
    """
    return [
        ("我密码忘了，咋整", "密码", "自助服务"),
        ("账号被锁了打不开", "锁定", "30 分钟"),
        ("想改一下登录密码", "修改", "Ctrl+Alt+Del"),
        ("新来的同事账号怎么弄", "新员工", "HR"),
        ("邮箱收不到信了", "收不到邮件", "垃圾邮件"),
        ("附件太大发不出去", "附件", "25MB"),
        ("VPN 一直连不上", "VPN", "客户端"),
        ("无线网连上了但是没网", "WiFi", "MAC"),
        ("内网怎么上外网", "外网", "代理"),
        ("怎么设置休假自动回复", "自动回复", "Outlook"),
        ("打印机老是卡纸", "卡纸", "电源"),
        ("电脑蓝屏了", "蓝屏", "安全模式"),
        ("登录提示异地了", "异地登录", "冻结"),
        ("邮箱满了怎么腾地方", "容量", "归档"),
        ("怎么退出别的设备登录", "退出", "会话"),
    ]


def eval_faq_rewrite(bm25, cases):
    """FAQ 改写问法评测: 测口语化/同义改写下的命中情况(更接近真实场景)"""
    correct = 0
    total = len(cases)
    detail = []
    for query, expect_kw, answer_kw in cases:
        answer, need_rag = bm25.search(query)
        if not need_rag and answer and expect_kw in answer:
            correct += 1
            detail.append((query, "✅ 命中"))
        else:
            detail.append((query, "❌ 未命中/答非所问"))
    acc = correct / total if total else 0
    return {"name": "FAQ 改写问法(鲁棒性)", "correct": correct, "total": total, "acc": acc, "detail": detail}


def build_rag_cases():
    """构造 RAG 评测集: (长尾问题, 期望知识域)"""
    # 这些问题不在 FAQ 里, 应走 RAG, 且检索结果应来自对应域
    return [
        ("堡垒机登录远程运维通道应该怎么使用", "network"),
        ("无线AP漫游切换异常怎么排查", "network"),
        ("打印队列卡住了怎么清理", "printer"),
        ("复印机故障代码怎么对照处理", "printer"),
        ("账号异地登录告警怎么处理", "account"),
        ("单点登录SSO配置常见问题", "account"),
        ("办公软件打开报错怎么处理", "software"),
        ("杀毒软件拦截程序怎么办", "software"),
    ]


# ==================== 各层评测 ====================

def eval_error_code(matcher, cases):
    """错误码层: 测抽取+匹配准确率"""
    correct = 0
    total = len(cases)
    detail = []
    for query, expect_code in cases:
        hit = matcher.match(query)
        if hit and hit["code"] == expect_code:
            correct += 1
            detail.append((query, expect_code, "✅"))
        else:
            got = hit["code"] if hit else "None"
            detail.append((query, expect_code, f"❌(得到{got})"))
    acc = correct / total if total else 0
    return {"name": "错误码精确匹配", "correct": correct, "total": total, "acc": acc, "detail": detail}


def eval_faq(bm25, cases):
    """FAQ/BM25 层: 测 Top1 命中率(召回的问题是否正确)"""
    correct = 0
    total = len(cases)
    detail = []
    for query, _answer in cases:
        # 命中则返回(答案, False); 未命中返回(None, True)
        answer, need_rag = bm25.search(query)
        if not need_rag and answer:
            correct += 1
        detail.append((query, "命中" if not need_rag else "未命中"))
    acc = correct / total if total else 0
    return {"name": "FAQ/BM25 高频问答", "correct": correct, "total": total, "acc": acc, "detail": detail}


def eval_rag(vector_store, cases, threshold):
    """RAG 层: 测检索相关性(最高 rerank 分是否达标) + 域是否正确"""
    from pymilvus import AnnSearchRequest, WeightedRanker
    correct = 0
    total = len(cases)
    detail = []
    for query, expect_source in cases:
        emb = vector_store.embedding_function([query])
        qd = emb["dense"][0]
        sq = {}
        row = emb["sparse"][[0]]
        for idx, val in zip(row.indices, row.data):
            sq[idx] = val
        dr = AnnSearchRequest(data=[qd], anns_field="dense_vector",
                              param={"metric_type": "IP", "params": {"nprobe": 10}}, limit=5)
        sr = AnnSearchRequest(data=[sq], anns_field="sparse_vector",
                              param={"metric_type": "IP", "params": {}}, limit=5)
        res = vector_store.client.hybrid_search(
            collection_name=vector_store.collection_name, reqs=[dr, sr],
            ranker=WeightedRanker(1.0, 0.7), limit=5,
            output_fields=["text", "parent_content", "source"])[0]
        sub = [vector_store._doc_from_hit(h) for h in res]
        parents = vector_store._get_unique_parent_docs(sub)
        if not parents:
            detail.append((query, "无检索结果"))
            continue
        pairs = [[query, d.page_content] for d in parents]
        scores = vector_store.reranker.predict(pairs)
        top_score = float(max(scores))
        # 相关性达标: 最高分 >= threshold
        hit = top_score >= threshold
        # 域是否匹配(取分数最高的文档的 source)
        best = parents[int(scores.argmax())]
        src = best.metadata.get("source", "unknown")
        src_ok = (src == expect_source)
        if hit and src_ok:
            correct += 1
        detail.append((query, f"score={top_score:.2f} src={src} {'✅' if hit and src_ok else '⚠️'}"))
    acc = correct / total if total else 0
    return {"name": "RAG 向量检索", "correct": correct, "total": total, "acc": acc, "detail": detail}


# ==================== 主流程 ====================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="快速评测(抽样)")
    args = parser.parse_args()

    print("=" * 70)
    print("  三层检索路由效果评测")
    print("=" * 70)
    t0 = time.time()

    results = []

    # ---- 第1层: 错误码 ----
    print("\n[1/3] 评测错误码精确匹配层...")
    from service.error_code_matcher import ErrorCodeMatcher
    from store.error_code_store import ErrorCodeStore
    ec_cases = build_error_code_cases()
    if args.quick:
        ec_cases = ec_cases[:10]
    matcher = ErrorCodeMatcher(store=ErrorCodeStore())
    results.append(eval_error_code(matcher, ec_cases))

    # ---- 第2层: FAQ/BM25 ----
    print("[2/3] 评测 FAQ/BM25 层...")
    from service.bm25_search import BM25Search
    faq_cases = build_faq_cases()
    if args.quick:
        faq_cases = faq_cases[:30]
    bm25 = BM25Search()
    results.append(eval_faq(bm25, faq_cases))
    # 额外: 改写问法鲁棒性评测(全部跑, 数量少)
    rewrite_cases = build_faq_rewrite_cases()
    results.append(eval_faq_rewrite(bm25, rewrite_cases))

    # ---- 第3层: RAG ----
    print("[3/3] 评测 RAG 向量检索层...")
    from retrieval.vector_store import VectorStore
    rag_cases = build_rag_cases()
    if args.quick:
        rag_cases = rag_cases[:4]
    vs = VectorStore()
    results.append(eval_rag(vs, rag_cases, config.RERANK_SCORE_THRESHOLD))

    # ---- 汇总输出 ----
    elapsed = time.time() - t0
    print("\n" + "=" * 70)
    print("  评测结果汇总")
    print("=" * 70)
    total_correct = 0
    total_all = 0
    for r in results:
        total_correct += r["correct"]
        total_all += r["total"]
        print(f"  {r['name']:<16} {r['correct']:>3}/{r['total']:<3}  准确率 {r['acc']*100:5.1f}%")
    overall = total_correct / total_all if total_all else 0
    print("-" * 70)
    print(f"  {'总体':<16} {total_correct:>3}/{total_all:<3}  准确率 {overall*100:5.1f}%")
    print(f"  耗时: {elapsed:.1f}s")
    print("=" * 70)

    # 打印各层失败明细
    for r in results:
        fails = [d for d in r["detail"] if "❌" in str(d[-1]) or "⚠️" in str(d[-1]) or "未命中" in str(d[-1])]
        if fails:
            print(f"\n【{r['name']}】未达标样例:")
            for f in fails[:10]:
                print(f"   - {f[0]}  → {f[-1]}")

    # 保存报告
    report_path = os.path.join(config.LOG_DIR, "eval_report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("# 三层检索路由效果评测报告\n\n")
        f.write(f"- 评测时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"- 评测模式: {'快速抽样' if args.quick else '全量'}\n\n")
        f.write("## 汇总\n\n")
        f.write("| 层级 | 命中 | 总数 | 准确率 |\n")
        f.write("|------|------|------|--------|\n")
        for r in results:
            f.write(f"| {r['name']} | {r['correct']} | {r['total']} | {r['acc']*100:.1f}% |\n")
        f.write(f"| **总体** | {total_correct} | {total_all} | {overall*100:.1f}% |\n\n")
        f.write(f"## 详细结果\n\n")
        for r in results:
            f.write(f"### {r['name']}\n\n")
            for d in r["detail"]:
                f.write(f"- {d[0]} → {d[-1]}\n")
            f.write("\n")
    print(f"\n评测报告已保存: {report_path}")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
# scripts/ragas_evaluate.py
# RAGAS 标准评估 —— 用行业标准指标评估 RAG 层答案质量
#
# 用法:
#   python scripts/ragas_evaluate.py            # 全量评估(12个RAG样例)
#   python scripts/ragas_evaluate.py --quick    # 快速评估(4个样例)
#
# 评估指标(RAGAS 三大核心指标):
#   Context Precision   上下文精确度: 检索到的上下文中有多少是真正有用的(检索质量)
#   Answer Relevancy    答案相关性: 生成的答案与问题的相关程度(生成质量)
#   Faithfulness        忠实度: 答案是否完全基于检索上下文、有无编造(幻觉程度)
#
# 与 scripts/evaluate.py 的区别:
#   evaluate.py 是「命中率」评测(各层是否命中), 偏工程指标
#   本脚本是「答案质量」评测(答案好不好), 对标 RAGAS 行业标准, 面试更有说服力
#
# 实现说明:
#   - LLM: 用通义千问(qwen-plus)的 OpenAI 兼容接口适配 ragas 评分器
#   - Embeddings: 复用本地 BGE-M3(通过 milvus_model), 供 answer_relevancy 用
#   - 评测集: 每个样例含 (问题, 标准答案, 检索上下文)
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base.config import config
from base.logger import logger


# ==================== RAGAS 评测集构造 ====================

def build_ragas_samples():
    """
    构造 RAGAS 评测集: 每样例 (question, reference 标准答案)
    评测时会走真实检索链路拿到 contexts, 再用 LLM 生成 answer。
    """
    return [
        ("堡垒机登录远程运维通道应该怎么使用",
         "通过堡垒机申请运维账号，审批通过后跳转访问目标服务器进行运维操作"),
        ("无线AP漫游切换异常怎么排查",
         "检查AP信号覆盖与信道配置，确认终端漫游阈值设置，排查相邻AP干扰"),
        ("打印队列卡住了怎么清理",
         "打开打印队列清空所有文档，重启打印服务或打印机电源"),
        ("账号异地登录告警怎么处理",
         "立即修改密码并检查登录会话，必要时冻结账号并联系IT服务台"),
        ("单点登录SSO配置常见问题",
         "检查SSO回调地址与证书配置，确认账号映射关系正确"),
        ("办公软件打开报错怎么处理",
         "修复或重装办公软件，清理配置文件与注册表残留"),
        ("公司新员工入职需要开通哪些账号和权限",
         "开通域账号、企业邮箱、VPN权限，按岗位分配系统访问权限"),
        ("如何设置邮件自动回复",
         "在Outlook中通过自动回复规则设置内外部的自动回复内容"),
    ]


# ==================== RAGAS LLM/Embeddings 适配 ====================

def build_ragas_llm():
    """用通义千问(OpenAI兼容)适配 ragas 的 LangchainLLM"""
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=config.LLM_MODEL,
        api_key=config.DASHSCOPE_API_KEY,
        base_url=config.DASHSCOPE_BASE_URL,
        temperature=0.0,
    )


def build_ragas_embeddings():
    """用本地 BGE-M3 适配 ragas 的 embeddings(供 answer_relevancy 用)"""
    from langchain_core.embeddings import Embeddings
    from retrieval.vector_store import _resolve_model_path
    from milvus_model.hybrid import BGEM3EmbeddingFunction
    import torch

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    m3 = BGEM3EmbeddingFunction(
        model_name_or_path=_resolve_model_path("bge-m3"),
        use_fp16=(device.type == "cuda"),
        device=device,
    )

    class BGE3Embeddings(Embeddings):
        """把 BGEM3EmbeddingFunction 包装成 LangChain Embeddings 接口"""

        def _to_list(self, dense):
            # 兼容 numpy 数组 / list(元素可能是 numpy.ndarray)
            if hasattr(dense, "tolist"):
                return dense.tolist()
            if isinstance(dense, list):
                return [self._to_list(x) for x in dense]
            return list(dense)

        def embed_documents(self, texts):
            return self._to_list(m3(texts)["dense"])

        def embed_query(self, text):
            return self._to_list(m3([text])["dense"])[0]

    return BGE3Embeddings()


# ==================== 主流程 ====================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="快速评估(抽样4条)")
    args = parser.parse_args()

    print("=" * 70)
    print("  RAGAS 标准评估 —— RAG 层答案质量")
    print("=" * 70)

    if not config.DASHSCOPE_API_KEY:
        print("❌ 未配置 DASHSCOPE_API_KEY, 无法使用 LLM 评分")
        return

    samples = build_ragas_samples()
    if args.quick:
        samples = samples[:4]

    # 1. 初始化检索链路
    print("\n[1/3] 初始化检索链路(VectorStore + LLM)...")
    t0 = time.time()
    from retrieval.vector_store import VectorStore
    from service.llm_client import LLMClient
    vs = VectorStore()
    llm = LLMClient()

    # 2. 对每个问题跑真实检索 + 生成, 组装 RAGAS 输入
    print("[2/3] 跑真实检索 + 生成答案...")
    from datasets import Dataset
    records = []
    for question, reference in samples:
        docs = vs.hybrid_search_with_rerank(question)
        if not docs:
            print(f"  ⚠️ 检索为空(跳过): {question}")
            continue
        contexts = [d.page_content for d in docs]
        answer = llm.generate_answer(question, contexts)
        records.append({
            "user_input": question,
            "reference": reference,
            "response": answer,
            "retrieved_contexts": contexts,
        })
        print(f"  ✅ {question[:20]}... (检索到 {len(contexts)} 个上下文)")

    if not records:
        print("❌ 无有效评测样例")
        return

    dataset = Dataset.from_list(records)

    # 3. RAGAS 评估
    print("\n[3/3] RAGAS 评分(通义千问做评判)...")
    from ragas import evaluate
    from ragas.metrics import faithfulness, answer_relevancy, context_precision

    ragas_llm = build_ragas_llm()
    ragas_embeddings = build_ragas_embeddings()

    result = evaluate(
        dataset,
        metrics=[context_precision, answer_relevancy, faithfulness],
        llm=ragas_llm,
        embeddings=ragas_embeddings,
        show_progress=False,
    )

    elapsed = time.time() - t0
    df = result.to_pandas()

    # 输出结果
    print("\n" + "=" * 70)
    print("  RAGAS 评估结果")
    print("=" * 70)
    score_map = {
        "context_precision": "上下文精确度 Context Precision",
        "answer_relevancy": "答案相关性 Answer Relevancy",
        "faithfulness": "忠实度 Faithfulness",
    }
    summary = {}
    for col, label in score_map.items():
        if col in df.columns:
            avg = df[col].mean()
            summary[label] = round(float(avg), 4)
            print(f"  {label:<32} {avg:.4f}")
    print("-" * 70)
    print(f"  评测样例数: {len(df)}")
    print(f"  耗时: {elapsed:.1f}s")
    print("=" * 70)

    # 逐条明细
    print("\n逐条明细:")
    for i, row in df.iterrows():
        print(f"\n  [{i+1}] {row['user_input'][:30]}")
        for col, label in score_map.items():
            if col in df.columns:
                print(f"      {label}: {row[col]:.4f}")

    # 保存报告
    report_path = os.path.join(config.LOG_DIR, "ragas_report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("# RAGAS 标准评估报告\n\n")
        f.write(f"- 评估时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"- 评测样例数: {len(df)}\n")
        f.write(f"- 评分 LLM: 通义千问 {config.LLM_MODEL}\n\n")
        f.write("## 汇总\n\n")
        f.write("| 指标 | 分数 |\n")
        f.write("|------|------|\n")
        for label, score in summary.items():
            f.write(f"| {label} | {score:.4f} |\n")
        f.write("\n## 逐条明细\n\n")
        for i, row in df.iterrows():
            f.write(f"### {i+1}. {row['user_input']}\n\n")
            for col, label in score_map.items():
                if col in df.columns:
                    f.write(f"- {label}: {row[col]:.4f}\n")
            f.write("\n")
    print(f"\n评估报告已保存: {report_path}")


if __name__ == "__main__":
    main()

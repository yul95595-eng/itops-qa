# -*- coding: utf-8 -*-
# scripts/openjudge_demo.py
# 用阿里 OpenJudge 的 HallucinationGrader / RelevanceGrader 判 RAG 答案质量
#
# 用途: 演示"LLM-as-Judge"评测 —— 用大模型当裁判, 对答案做幻觉/相关性打分。
#   这是"生成层评测"区别于"检索层评测(Recall/MRR)"的关键:
#   检索层能客观自动判定, 生成层必须靠 LLM-as-judge(人工太贵)。
#
# 依赖: pip install py-openjudge
# 运行: python scripts/openjudge_demo.py
#
# 说明: 这里用手写样本演示(不依赖向量库/LLM生成链路), 方便独立跑通;
#   接入真实项目时, 把 query/context 换成检索返回的父块、response 换成 qwen 生成答案即可。
import os
import sys
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base.config import config  # noqa: E402
from openjudge.models import OpenAIChatModel  # noqa: E402
from openjudge.graders.common import HallucinationGrader, RelevanceGrader  # noqa: E402


def _load_dashscope_key():
    """从 .env 读 DASHSCOPE_API_KEY"""
    key = os.environ.get("DASHSCOPE_API_KEY")
    if key:
        return key
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8"):
            line = line.strip()
            if line.startswith("DASHSCOPE_API_KEY="):
                return line.split("=", 1)[1].strip()
    return None


# 手写样本: 模拟 RAG 问答的 query/context(检索到的知识)/response(模型生成)
SAMPLES = [
    {
        "query": "VPN 客户端安装后一直报错打不开怎么办？",
        "context": "VPN 客户端安装与常见报错：安装后报错通常是驱动冲突或杀毒软件拦截，"
                   "可先关闭杀毒软件重装，或更新网卡驱动。",
        "response": "建议先卸载杀毒软件后重新安装 VPN 客户端，并更新网卡驱动。",  # 忠实于 context
    },
    {
        "query": "VPN 客户端安装后一直报错打不开怎么办？",
        "context": "VPN 客户端安装与常见报错：安装后报错通常是驱动冲突或杀毒软件拦截。",
        "response": "这是公司网络策略导致的，请联系行政部申请带宽扩容。",  # 编造了 context 没有的内容
    },
    {
        "query": "邮箱满了收不到新邮件怎么处理？",
        "context": "邮箱容量清理与归档指南：可将旧邮件归档到本地 PST 文件，或删除大附件。",
        "response": "可以清理旧邮件、归档大附件来释放邮箱空间。",  # 相关且忠实
    },
]


async def main():
    api_key = _load_dashscope_key()
    if not api_key:
        print("❌ 未找到 DASHSCOPE_API_KEY, 请在 .env 配置")
        return

    # 用 dashscope 兼容接口的 qwen 模型当"裁判"
    model = OpenAIChatModel(
        model="qwen-plus",
        api_key=api_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )

    halluc_grader = HallucinationGrader(model=model, language="zh")
    relevance_grader = RelevanceGrader(model=model, language="zh")

    print("=" * 70)
    print("  OpenJudge LLM-as-Judge 评测演示 (裁判模型: qwen-plus)")
    print("=" * 70)

    for i, s in enumerate(SAMPLES, 1):
        print(f"\n【样本 {i}】query: {s['query']}")
        print(f"  context: {s['context'][:40]}...")
        print(f"  response: {s['response'][:40]}...")

        h = await halluc_grader.aevaluate(
            query=s["query"], response=s["response"], context=s["context"])
        r = await relevance_grader.aevaluate(query=s["query"], response=s["response"])

        # GraderScore: score 语义 ——
        #   HallucinationGrader: 5=无幻觉, 1=严重幻觉 (越低越差, <3 判为有幻觉)
        #   RelevanceGrader:     5=完全相关, 1=不相关 (越高越好)
        h_score = getattr(h, "score", None)
        h_reason = getattr(h, "reason", "")
        r_score = getattr(r, "score", None)
        r_reason = getattr(r, "reason", "")

        print(f"  幻觉分(1~5, 5=无幻觉): {h_score}  — {h_reason[:50]}")
        print(f"  相关性(1~5, 5=完全相关): {r_score}  — {r_reason[:50]}")

    print("\n" + "=" * 70)
    print("  解读 (注意: 幻觉分越高越好, 5=无幻觉):")
    print("   - 样本1(忠实) 幻觉分应偏高(无幻觉), 相关性应偏高")
    print("   - 样本2(编造) 幻觉分应偏低(<3, 有明显幻觉), 相关性应偏低")
    print("   - 样本3(相关) 幻觉分=5(无幻觉), 相关性=5")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())

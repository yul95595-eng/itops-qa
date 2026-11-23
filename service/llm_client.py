# -*- coding: utf-8 -*-
# service/llm_client.py
# LLM 调用客户端 —— RAG 向量检索层的答案生成（通义千问 / 百炼 DashScope）
#
# 用 OpenAI 兼容接口(compatible-mode) 调通义千问, 复用 config 里的 DASHSCOPE_API_KEY。
# 提供 generate_answer() 用于"基于检索到的上下文生成答案", 支持流式与非流式两种。
import os
import sys
import json
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base.config import config
from base.logger import logger


class LLMClient:
    """通义千问 LLM 客户端, 负责把检索上下文 + 用户问题组织成 prompt 生成答案"""

    def __init__(self, model=None):
        self.model = model or config.LLM_MODEL

    def _build_payload(self, system, user, temperature=0.3, stream=False):
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "stream": stream,
        }

    def generate(self, system, user, temperature=0.3):
        """非流式调用, 返回完整文本"""
        if not config.DASHSCOPE_API_KEY:
            raise RuntimeError("未配置 DASHSCOPE_API_KEY, 请在 it_ops_qa/.env 中设置")
        payload = self._build_payload(system, user, temperature=temperature, stream=False)
        req = urllib.request.Request(
            config.DASHSCOPE_BASE_URL + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + config.DASHSCOPE_API_KEY,
            },
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]

    def _build_messages_payload(self, system, messages, temperature=0.3, stream=False):
        """构建多轮 messages 的请求体(用于携带对话历史)"""
        return {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + messages,
            "temperature": temperature,
            "stream": stream,
        }

    def generate_messages(self, system, messages, temperature=0.3):
        """非流式多轮调用, messages 为 [{"role","content"}, ...]"""
        if not config.DASHSCOPE_API_KEY:
            raise RuntimeError("未配置 DASHSCOPE_API_KEY, 请在 it_ops_qa/.env 中设置")
        payload = self._build_messages_payload(system, messages, temperature=temperature, stream=False)
        req = urllib.request.Request(
            config.DASHSCOPE_BASE_URL + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + config.DASHSCOPE_API_KEY,
            },
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]

    def _build_system(self):
        return (
            "你是企业内部 IT 运维知识库助手, 只依赖给定的知识库上下文回答问题。\n"
            "【必须遵守的规则】\n"
            "1. 只能根据下方【参考上下文】中的内容回答, 严禁联网搜索、严禁使用任何外部信息、严禁编造或臆测上下文以外的内容。\n"
            "2. 如果上下文不足以回答用户问题, 必须如实回答“抱歉, 知识库中没有找到相关信息”, 并建议联系 IT 服务台, 绝不能凭空编造答案。\n"
            "3. 回答要准确、简洁、分点清晰, 直接给出可操作的解决步骤。\n"
            "4. 若用户提问中包含指代(如“那这个呢”“密码忘了呢”), 请结合【对话历史】理解其真实所指。"
        )

    def generate_hypothesis(self, query):
        """
        HyDE(Hypothetical Document Embeddings)假设文档生成:
        针对口语化/过短的问题, 先让 LLM 扩写成一段"假设的标准答案",
        再用这段假设答案去向量检索, 提升短问的语义召回。
        :param query: 原始口语化短问
        :return: 假设答案文本
        """
        if not config.DASHSCOPE_API_KEY:
            raise RuntimeError("未配置 DASHSCOPE_API_KEY, 请在 it_ops_qa/.env 中设置")
        # 关键约束: 不得引入原问题没有的实体。
        # 早期版本写的是"包含可能的场景、涉及的系统或设备", 等于让模型替用户猜设备 ——
        # 用户说"插件坏了"(没说是哪个软件的插件), 模型会扩写成某个具体软件的插件故障,
        # 检索于是被引向那篇文档, 答得"很像对"但答的是另一个东西。
        # 扩写只该补"词"(术语归一/同义表述), 不该补"事实"(设备名/错误码/场景)。
        system = (
            "你是 IT 运维知识库的检索增强助手。用户会用简短、口语化的方式描述 IT 故障, "
            "请把他的话改写成规范的运维术语表述, 便于向量检索。"
            "硬性要求: 不得引入原问题里没有出现的软件名、设备名、系统名或错误码; "
            "不得猜测使用场景; 只做术语归一和表达规范化。只输出改写后的文本, 不要加任何前缀或解释。"
        )
        user = f"用户简短描述: {query}\n\n请改写为规范的故障描述:"
        return self.generate(system, user, temperature=0.3)

    def generate_answer(self, query, context_docs, history=None):
        """
        基于检索到的上下文文档生成答案, 支持多轮对话历史。
        :param query: 用户问题
        :param context_docs: 检索到的上下文(字符串列表)
        :param history: 历史消息列表 [{"role": "user"/"assistant", "content": "..."}], 可选
        :return: 答案文本
        """
        system = self._build_system()
        context = "\n\n---\n\n".join(context_docs)
        user = f"【用户问题】\n{query}\n\n【参考上下文】\n{context}\n\n请严格基于以上上下文回答。"

        # 组织消息列表: 有历史则把历史作为多轮 messages 传入
        messages = []
        if history:
            for msg in history:
                role = "assistant" if msg.get("role") == "assistant" else "user"
                messages.append({"role": role, "content": msg.get("content", "")})
        messages.append({"role": "user", "content": user})
        return self.generate_messages(system, messages)

    def generate_answer_stream(self, query, context_docs, history=None):
        """
        流式生成答案, 逐段 yield 文本。支持多轮对话历史。
        :param history: 历史消息列表 [{"role", "content"}, ...], 可选
        :return: generator, 逐段产出增量文本
        """
        if not config.DASHSCOPE_API_KEY:
            raise RuntimeError("未配置 DASHSCOPE_API_KEY, 请在 it_ops_qa/.env 中设置")
        system = self._build_system()
        context = "\n\n---\n\n".join(context_docs)
        user = f"【用户问题】\n{query}\n\n【参考上下文】\n{context}\n\n请严格基于以上上下文回答。"

        messages = []
        if history:
            for msg in history:
                role = "assistant" if msg.get("role") == "assistant" else "user"
                messages.append({"role": role, "content": msg.get("content", "")})
        messages.append({"role": "user", "content": user})

        payload = self._build_messages_payload(system, messages, temperature=0.3, stream=True)

        req = urllib.request.Request(
            config.DASHSCOPE_BASE_URL + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + config.DASHSCOPE_API_KEY,
            },
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            for line in resp:
                line = line.decode("utf-8").strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = chunk["choices"][0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        yield content
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue


if __name__ == '__main__':
    llm = LLMClient()
    ans = llm.generate(
        "你是测试助手", "用一句话介绍你自己", temperature=0.3
    )
    print("非流式:", ans)

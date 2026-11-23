# -*- coding: utf-8 -*-
# scripts/smoke_test.py
# 冒烟测试 —— 验证整条链路"从服务启动到给出答案"是否真的通
#
# 与 eval_quality.py 的区别:
#   eval_quality.py 测的是"检索质量"(准确率/消融/基线), 需要先有服务、有数据;
#   smoke_test.py   测的是"链路通没通"(服务活着、每层能给响应、写接口鉴权生效)。
#   上线前先跑这个, 全绿了再谈质量。
#
# 用法(需先启动服务):
#   python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
#   python scripts/smoke_test.py
#
# 退出码: 0 = 全绿, 1 = 有失败项(供 start.sh / CI 判断)
import json
import os
import sys
import time
import urllib.error
import urllib.request

# 项目根加入 path, 复用代码里的配置(避免脚本里硬编码口令)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from base.config import config  # noqa: E402

BASE = "http://127.0.0.1:8000"
ADMIN_TOKEN = config.ADMIN_TOKEN  # 来自 .env / config.local.ini, 不在脚本里写死

_results = []  # (name, ok, detail)


def _check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    mark = "✅" if ok else "❌"
    print(f"  {mark} {name}" + (f"  -> {detail}" if detail else ""))


def _get(path, headers=None):
    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _post_json(path, payload, headers=None):
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _ask(question, history=None):
    """走 SSE 流式问答, 返回 (layers, 拼接后的答案文本)"""
    status, raw = _post_json("/chat", {"question": question, "history": history or []})
    if status != 200:
        return [], ""
    layers, text = [], ""
    for line in raw.split("\n"):
        if line.startswith("data:"):
            data = line[5:].strip()
            if data == "[DONE]":
                continue
            try:
                evt = json.loads(data)
            except Exception:
                continue
            if evt.get("layer") == "rag_meta":
                continue
            layers.append(evt.get("layer"))
            text += evt.get("content", "")
    return layers, text


def main():
    print("=" * 60)
    print("IT 运维知识库助手 —— 冒烟测试")
    print("=" * 60)

    # 1. 服务存活
    status, body = _get("/health")
    _check("健康检查 /health", status == 200, f"HTTP {status}")

    # 2. 首页可访问
    status, body = _get("/")
    _check("首页 / 可访问", status == 200 and "知识库" in body, f"HTTP {status}, 长度 {len(body)}")

    # 3. 错误码速查(零 LLM)
    status, body = _get("/api/error_code?code=1045")
    ok = status == 200 and json.loads(body).get("found") is True
    _check("错误码速查 1045", ok, f"HTTP {status}")

    # 4. 三层路由: 错误码层
    layers, text = _ask("数据库报错 error 1045 怎么办")
    _check("问答命中「错误码层」", "error_code" in layers, f"layers={layers}")

    # 5. 三层路由: FAQ 层
    layers, text = _ask("密码忘了怎么重置")
    _check("问答命中「FAQ 层」", "faq" in layers, f"layers={layers}")

    # 6. 三层路由: RAG 层(长尾问题, 走混合检索+LLM)
    layers, text = _ask("公司内网访问不了共享盘怎么排查")
    _check("问答命中「RAG 层」", "rag" in layers, f"layers={set(layers) if layers else '空'}")

    # 7. 域守卫: 闲聊/寒暄应在前置层短路, 不进检索、不落盲区表
    layers, text = _ask("你好")
    _check("闲聊短路(chitchat)", "chitchat" in layers, f"layers={layers}")

    # 7b. 域外: 与 IT 无关、也不是闲聊的问题, 应在进 RAG 前被域判定拦住
    #     期望走 out_of_domain(不是 fallback): 不烧重排算力, 也不落 knowledge_gaps
    layers, text = _ask("珠穆朗玛峰有多高")
    _check("域外问题前置拒答(out_of_domain)", "out_of_domain" in layers, f"layers={layers}")

    # 7c. 兜底: 域内但知识库里确实没有的内容, 应诚实拒答(fallback), 并落盲区表
    layers, text = _ask("公司服务器机柜的承重标准是多少")
    _check("域内冷门问题诚实兜底(fallback)", "fallback" in layers, f"layers={layers}")

    # 7d. 槽位自检: 只说了类别词(没说清哪个打印机), 应在进 RAG 前反问并给候选。
    #     顺带回归 A04: 这条曾经命中"U 盘插上没反应"(共享"没反应"三个字), 答案串台到 U 盘。
    layers, text = _ask("打印机没反应")
    _check("模糊问题先反问(clarify)", "clarify" in layers and "1." in text, f"layers={layers}")
    _check("FAQ 错配不再串台(A04)", "U 盘" not in text, f"文本={text[:30]}")

    # 7e. 澄清轮: 用户回"1"本身检索不了任何东西, 必须还原成上一轮的候选文档标题
    hist = [{"role": "user", "content": "打印机没反应"},
            {"role": "assistant", "content": text}]
    layers2, text2 = _ask("1", history=hist)
    _check("澄清轮序号还原", bool(layers2) and "clarify" not in layers2 and len(text2) > 10,
           f"layers={layers2}")

    # 8. 鉴权: 无口令访问知识盲区应 401
    status, body = _get("/api/gaps")
    _check("知识盲区无口令拦截(401)", status == 401, f"HTTP {status}")

    # 9. 鉴权: 正确口令应 200
    status, body = _get("/api/gaps", {"X-Admin-Token": ADMIN_TOKEN})
    _check("知识盲区正确口令可访问(200)", status == 200, f"HTTP {status}")

    # 10. 文档列表接口可用(需口令? 列表接口当前未加鉴权, 只验证可达)
    status, body = _get("/api/docs")
    _check("文档列表 /api/docs 可达", status == 200, f"HTTP {status}")

    # 汇总
    print("=" * 60)
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print(f"结果: {passed}/{total} 项通过")
    for name, ok, detail in _results:
        if not ok:
            print(f"  失败项: {name} {detail}")
    print("=" * 60)
    if passed == total:
        print("🎉 冒烟测试全绿, 链路已打通")
        return 0
    print("⚠️ 存在失败项, 请检查上述失败环节")
    return 1


if __name__ == "__main__":
    sys.exit(main())

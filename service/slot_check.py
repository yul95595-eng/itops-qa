# -*- coding: utf-8 -*-
"""回答前的槽位自检 —— 判断"用户说的话够不够拿来检索"。

要解决的问题:
  用户说"插件坏了""打印机不行", 只说了类别, 没说清具体是哪个东西。
  直接丢进 RAG, 稠密检索会把注意力摊到所有同类文档上, top1 分数不低但很可能是错的
  (实测 A04: 问"打印机没反应"命中"U 盘插上没反应", raw=7.53 / margin=0.36 双判据都过, 答案错了)。
  所以进 RAG 之前先自检一句: 说得够不够? 不够就先问, 不要猜。

设计原则:
  1. 纯词表 + 分词 + 标题匹配: 0 次模型调用、0 次向量检索, 判定成本 < 1ms。
  2. 宁可漏问不可误拦: 命中强实体、或候选唯一时一律放行(RAG 层还有兜底)。
  3. 候选只从"文档标题"里取, 不走向量库。好处是反问的选项就是真实存在的文档,
     用户选完即命中; 代价是不认识同义词, 所以只在类别词这条路线上用它。

刻意不做的事:
  - 不做 LLM 槽位抽取: 慢、不稳, 而且抽出来的值还要归一化。
  - 不做多轮状态机: 前端已经持有完整会话(index.html 的 localStorage 会话),
    后端靠 CLARIFY_PREFIX 认出澄清轮即可, 不需要新增会话存储。
  - 不落 knowledge_gaps: 盲区表的判据是"能对应到一个补文档的动作",
    模糊提问对应不到具体文档, 记进去只会污染运营的判断。
"""
import os
import re

import jieba

WIKI_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "wiki"
)

# 澄清回复的固定开头。下一轮靠它认出"上一轮是澄清轮", 这样前端不需要改消息结构。
CLARIFY_PREFIX = "您的问题可能对应几个方向"

# 故障词分两档。这个区分是"问不问"的分水岭:
#   万能故障词: 只说"不好使了", 不指向任何具体原因 —— 只有这类词才可能需要反问
#   具体故障词: 现象本身就能定位(卡纸/蓝屏/装不上), 出现即放行
# 早期版本只有一档, 导致"打印机卡纸""软件装不上"也被反问, 实测 25 条常见问题误拦 7 条。
VAGUE_FAULT_WORDS = (
    "坏了", "坏啦", "坏掉", "没反应", "没响应", "无响应", "不行", "不能用",
    "用不了", "有问题", "出问题", "异常", "故障", "不工作", "挂掉",
)
SPECIFIC_FAULT_WORDS = (
    "报错", "打不开", "连不上", "连接不上", "失败", "闪退", "崩溃", "蓝屏",
    "黑屏", "死机", "乱码", "收不到", "发不出", "装不上", "登录不了", "登不上",
    "看不到", "没声音", "没图像", "打不出", "开不了", "卡纸", "很慢", "太慢",
    "被锁", "失灵", "不亮", "不一致", "不兼容", "闪一下",
)
FAULT_WORDS = VAGUE_FAULT_WORDS + SPECIFIC_FAULT_WORDS    # 判"是不是在报障"用全集

# 排障类标题特征词: 候选同分时优先给"排障文档", 而不是"手册/规范/设置说明"
TROUBLE_MARKERS = (
    "无法", "不能", "故障", "排查", "失败", "异常", "问题", "报错",
    "冲突", "卡住", "丢失", "修复", "处理",
)

# 强实体: 一说出来就能把范围缩到少数几篇, 命中即放行, 绝不反问。
# 覆盖两类: 常见办公软件/系统名(来自 wiki 140 篇标题) + 错误码表的组件枚举。
STRONG_ENTITIES = (
    "outlook", "office", "excel", "word", "ppt", "vpn", "dhcp", "dns", "sso",
    "mfa", "802.1x", "vlan", "pac", "堡垒机", "远程桌面", "域账号", "单点登录",
    "复印机", "扫描", "投影", "会议平板", "输入法", "浏览器", "chrome", "edge",
    "云盘", "网盘", "ftp", "杀毒", "防火墙", "交换机", "无线ap", "门禁卡",
    "蓝屏", "环境变量", "应用商店", "剪贴板", "磁盘", "服务器", "共享文件夹",
    "mysql", "redis", "nginx", "docker", "kubernetes", "k8s", "linux", "windows",
    "oracle", "postgresql", "sql server", "sqlserver",
)

# 过泛的词: 标题匹配时丢掉。不丢的话"电脑""系统"能匹配上半个知识库, 候选数永远爆表。
TOO_GENERIC = {"电脑", "设备", "问题", "情况", "东西", "所有", "一个", "这个", "那个", "一下"}

_titles = None


def _load_titles():
    """懒加载全部文档标题(140 篇), 进程内只读一次。"""
    global _titles
    if _titles is None:
        titles = []
        for root, _, files in os.walk(WIKI_DIR):
            for f in files:
                if f.endswith(".md"):
                    titles.append(f[:-3])
        _titles = titles
    return _titles


def _useful_words(query):
    """切词, 丢掉故障词和过泛词 —— 剩下的才是能定位文档的线索。"""
    words = []
    for w in jieba.lcut(query):
        w = w.strip().lower()
        if len(w) < 2 or w in TOO_GENERIC:
            continue
        # 注意方向: 判断的是"这个词是不是某个故障词的一部分", 而不是反过来。
        # jieba 会把"没反应"切成 ['没','反应'], "反应"本身不在故障词表里,
        # 如果只做精确匹配, "反应"就会被当成有效线索 —— 而"U 盘插上没反应"正好含"反应",
        # 实体一致性于是误判为"一致", A04 那种错配就漏过去了。
        if any(w in fw for fw in FAULT_WORDS):
            continue
        words.append(w)
    return words


def candidates(query):
    """用线索词匹配文档标题, 排序: 命中词数降序 -> 像排障文档优先 -> 短标题优先。"""
    words = _useful_words(query)
    if not words:
        return []
    scored = []
    for t in _load_titles():
        tl = t.lower()
        n = sum(1 for w in words if w in tl)
        if n:
            trouble = sum(1 for m in TROUBLE_MARKERS if m in t)
            scored.append((n, trouble, len(t), t))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    return [t for *_, t in scored]


def entity_consistent(query, matched_text):
    """
    实体一致性检查 —— 用于判断 FAQ 命中的那条"标准问题"是不是同一个主体。

    为什么不用 margin 判: 实测 margin 高低跟对错不成正比。
      "密码忘了"      margin=0.07, 命中的就是正确答案(FAQ 里全是密码类问题, 天然分不开)
      "电脑蓝屏"      margin=0.00, 命中也是对的
      "打印机没反应"  margin=0.45, 命中"U 盘插上没反应", 是错的
    所以真正能判错配的是"主体对不对", 不是"分数领先多少"。

    规则: 从 query 里切出实词(已过滤故障词和过泛词), 至少有一个出现在命中的标准问题里。
      "打印机没反应" vs "U 盘插上没反应"  -> 打印机不在 -> 不一致
      "打印机卡纸"   vs "打印机卡纸了怎么办" -> 在 -> 一致
    切不出实词时一律判一致(宁可放行), 因为无法判断也不该因此拦住用户。
    """
    if not matched_text:
        return True
    words = _useful_words(query)
    if not words:
        return True
    mt = matched_text.lower()
    return any(w in mt for w in words)


def check(query):
    """
    自检: 这句话够不够用来检索?

    :return: dict
        need_clarify: True 表示"在报障但说不清对象", 应该先反问
        reason:       判定依据(写日志用, 也是面试时自证口径的依据)
        candidates:   候选文档标题(按相关度排序, 最多 4 条)
        assumed:      放行时的唯一候选, 可用于在回答里声明"我按 X 理解"
    """
    empty = {"need_clarify": False, "reason": "", "candidates": [], "assumed": None}
    q = (query or "").strip()
    if not q:
        return empty

    # 1. 不是报障 -> 放行(问流程、问申请的问题本来就没有对象缺失)
    if not any(fw in q for fw in FAULT_WORDS):
        return {**empty, "reason": "不含故障词, 不像报障"}

    # 2. 有强实体 -> 放行。这一步是"宁可漏问"的兜底: 说了 VPN 就别反问 VPN
    if any(e in q.lower() for e in STRONG_ENTITIES):
        return {**empty, "reason": "命中强实体, 信息足够"}

    # 3. 现象本身够具体(卡纸/蓝屏/装不上/登录不了) -> 放行。
    #    只有"没反应/坏了/不行"这种万能词, 才真的什么都不指向, 值得问一句。
    if any(fw in q for fw in SPECIFIC_FAULT_WORDS):
        return {**empty, "reason": "含具体现象词, 足以定位"}

    # 3. 只有类别词 -> 看候选数量决定要不要问
    cands = candidates(q)
    if not cands:
        return {**empty, "reason": "无候选标题, 交给正常链路兜底"}
    if len(cands) == 1:
        return {**empty, "reason": "候选唯一, 放行", "assumed": cands[0]}

    return {
        **empty,
        "need_clarify": True,
        "reason": f"只说了类别词, 有 {len(cands)} 个可能方向",
        "candidates": cands[:4],
    }


def resolve_reply(query, history):
    """
    澄清轮的下一轮: 把用户的一句话还原成"能拿去检索的 query"。

    为什么必须做: 检索入口只认单轮 query。
    用户回"1"或"outlook 的", 直接拿去检索是检索不到东西的(切词后只剩一个数字/一个词)。

    三种情况:
      - 用户回序号("1"/"第2个") -> 换成上一轮列出的那个候选文档标题
      - 用户补充描述("outlook 的") -> 拼上上一轮的原始问题
      - 上一轮不是澄清轮 -> 原样返回(不影响正常多轮)
    """
    q = (query or "").strip()
    if not history:
        return q

    last = (history[-1].get("content") or "").strip()
    if not last.startswith(CLARIFY_PREFIX):
        return q

    # 上一轮用户问的是什么
    prev = ""
    for msg in reversed(history[:-1]):
        if msg.get("role") == "user":
            prev = (msg.get("content") or "").strip()
            break

    # 情况一: 回的是序号 -> 换成候选标题
    m = re.match(r"^第?\s*([1-9])\s*[个条项]?[.、)]?$", q)
    if m:
        idx = int(m.group(1)) - 1
        opts = re.findall(r"^\s*\d+\.\s*(.+)$", last, flags=re.M)
        if 0 <= idx < len(opts):
            return opts[idx].strip()

    # 情况二: 补充了描述 -> 拼回原问题(检索用拼接串, 生成仍用原文)
    return f"{prev} {q}".strip() if prev else q


if __name__ == "__main__":
    tests = [
        "插件坏了",          # 候选唯一 -> 放行
        "打印机没反应",      # 候选多 -> 反问
        "电脑很卡",          # 候选多 -> 反问
        "VPN 连不上",        # 强实体 -> 放行
        "Outlook 插件启动报错",  # 强实体 -> 放行
        "怎么申请 VPN 账号",  # 不是报障 -> 放行
        "邮箱收不到邮件",    # 类别词过泛 -> 看候选
    ]
    for q in tests:
        r = check(q)
        flag = "反问" if r["need_clarify"] else "放行"
        print(f"[{flag}] {q}  <- {r['reason']}")
        for i, c in enumerate(r["candidates"], 1):
            print(f"      {i}. {c}")
        if r["assumed"]:
            print(f"      假定: {r['assumed']}")

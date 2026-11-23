# -*- coding: utf-8 -*-
# data/eval/independent_set.py
# 独立测试集 —— 用来替代「从知识库反向生成题目」的自测方式
#
# ============================================================================
# 为什么需要这套题
# ============================================================================
# scripts/evaluate.py 里的错误码题(172条)、FAQ 题(120条)都是从数据表
# /种子文件反向生成的 —— 题目措辞和数据几乎一模一样, 测出 100% 属于
# 「用训练集当测试集」, 在面试中被一句"这题是不是你自己生成的"就问穿。
#
# 本测试集的原则:
#   1. 题目措辞 **完全不照抄** data/wiki 的文档标题, 也不照抄 FAQ 原题
#   2. 模拟真实用户输入: 口语化 / 错别字 / 混合意图 / 越界问题
#   3. 判定只用「领域术语」(密码/蓝屏/VPN), 不用答案特有措辞, 避免对着答案改题
#   4. 单独统计「该拒答时是否正确拒答」, 这是反幻觉能力的直接证据
#
# 用法:
#   python scripts/eval_quality.py --independent
#
# ============================================================================
# 字段说明
# ============================================================================
#   id           题号, 对应报告里的逐条明细
#   query        用户输入(刻意做得像真人打字, 不像考试题)
#   category     题型: 口语化 / 错别字 / 错误码 / 混合意图 / 越界(应拒答)
#   expect_layer 理想情况下应命中的层(参考项, 用于统计"层命中率")
#                error_code / faq / rag / fallback
#   expect_kw    答案中应出现的领域术语(命中任一即算答对);
#                只放"任何正确回答都必然提到的词", 不放答案特有措辞
#   expect_code  仅错误码题使用: 期望提取到的错误码
#   should_refuse True = 知识库里没有这类信息, 正确行为是拒答转人工
#   note         考察意图(给人看的, 方便你理解每道题在测什么)
# ============================================================================

INDEPENDENT_CASES = [

    # ---------------- A. 口语化改写 (12 题) ----------------
    # 用户不会照文档标题提问。测的是"能不能理解大白话"。
    dict(id="A01", query="密码想不起来了，怎么弄回来", category="口语化",
         expect_layer="faq", expect_kw=["密码", "重置"],
         note="对应账号域-密码自助重置, 但不含'重置'字样, 靠语义命中"),

    dict(id="A02", query="账号被锁死了，进不去系统", category="口语化",
         expect_layer="faq", expect_kw=["锁定", "解锁", "30"],
         note="把'域账号被锁定'说成'被锁死', 测同义替换"),

    dict(id="A03", query="vpn 拨号一直转圈，连不上", category="口语化",
         expect_layer="faq", expect_kw=["VPN", "客户端"],
         note="'拨号一直转圈'是真实用户描述, 不是标准问法"),

    dict(id="A04", query="打印机按了没反应，纸也不动", category="口语化",
         expect_layer="faq", expect_kw=["打印", "电源", "卡纸"],
         note="描述现象而非报错, 测能不能定位到'无法打印'"),

    dict(id="A05", query="outlook 老弹密码框说密码不对", category="口语化",
         expect_layer="faq", expect_kw=["邮箱", "密码", "客户端"],
         note="Outlook 小写 + 口语描述, 测英文实体识别"),

    dict(id="A06", query="电脑老弹广告，烦死了", category="口语化",
         expect_layer="rag", expect_kw=["广告", "恶意", "杀毒"],
         note="FAQ 无此题, 只能走 RAG; 对应 Wiki'电脑弹出广告与恶意软件处理'"),

    dict(id="A07", query="U 盘插上电脑认不出来", category="口语化",
         expect_layer="faq", expect_kw=["U 盘", "USB", "接口"],
         note="'认不出来'=无法识别, 测口语到术语的映射"),

    dict(id="A08", query="无线网有信号但网页打不开", category="口语化",
         expect_layer="faq", expect_kw=["WiFi", "DNS", "IP"],
         note="FAQ 有'WiFi 连上但上不了网', 但措辞完全不同"),

    dict(id="A09", query="会议室投影仪连不上我的电脑", category="口语化",
         expect_layer="faq", expect_kw=["投影", "信号", "HDMI"],
         note="涉及会议室设备, 测跨子域的定位"),

    dict(id="A10", query="邮件发出去了对方说没收到", category="口语化",
         expect_layer="faq", expect_kw=["邮件", "垃圾", "拦截"],
         note="与'邮箱收不到邮件'镜像, 但描述视角是发件人"),

    dict(id="A11", query="电脑开机要等三分钟", category="口语化",
         expect_layer="rag", expect_kw=["开机", "启动", "自启动"],
         note="带具体数字'三分钟', 对应 Wiki'电脑开机慢与优化建议'"),

    dict(id="A12", query="office 老提示要激活", category="口语化",
         expect_layer="faq", expect_kw=["激活", "Office"],
         note="英文软件名小写 + 口语, 测实体识别"),

    # ---------------- B. 错别字 / 不规范输入 (8 题) ----------------
    # 真人打字会错。BN25 词面对错别字会直接崩, 这一组专门暴露它。
    dict(id="B01", query="打应机驱动怎么装", category="错别字",
         expect_layer="faq", expect_kw=["打印", "驱动"],
         note="打印机→打应机, 同音错字"),

    dict(id="B02", query="蓝平了怎么办", category="错别字",
         expect_layer="faq", expect_kw=["蓝屏"],
         note="蓝屏→蓝平, 最典型的输入法错字"),

    dict(id="B03", query="网线插上了但是没网洛", category="错别字",
         expect_layer="faq", expect_kw=["网络", "IP", "端口"],
         note="网络→网洛, 测容错"),

    dict(id="B04", query="邮箱发不出付件", category="错别字",
         expect_layer="faq", expect_kw=["附件", "大小"],
         note="附件→付件"),

    dict(id="B05", query="共享文当打不开", category="错别字",
         expect_layer="faq", expect_kw=["共享", "权限"],
         note="文档→文当"),

    dict(id="B06", query="vpn 怎么连节", category="错别字",
         expect_layer="faq", expect_kw=["VPN", "连接"],
         note="连接→连节"),

    dict(id="B07", query="杀度软件把程序删了", category="错别字",
         expect_layer="faq", expect_kw=["杀毒", "白名单", "恢复"],
         note="杀毒→杀度"),

    dict(id="B08", query="电脑开极很慢", category="错别字",
         expect_layer="rag", expect_kw=["开机", "启动"],
         note="开机→开极"),

    # ---------------- C. 错误码 (8 题) ----------------
    # 这一组是第 1 层的"真考": 错误码藏在口语里, 不是"数据库报错 error 1045"这种模板句。
    dict(id="C01", query="数据库连不上，报 1045", category="错误码",
         expect_layer="error_code", expect_code="1045", expect_kw=["1045"],
         note="错误码在句尾, 无'error'前缀"),

    dict(id="C02", query="启动报 ORA-12541 急死了", category="错误码",
         expect_layer="error_code", expect_code="ORA-12541", expect_kw=["ORA-12541"],
         note="Oracle 错误码 + 情绪词干扰"),

    dict(id="C03", query="蓝屏代码 0x80072EE7", category="错误码",
         expect_layer="error_code", expect_code="0x80072EE7", expect_kw=["0x80072EE7"],
         note="十六进制码, 测 0x 前缀是否被正确保留"),

    dict(id="C04", query="redis 报 WRONGPASS 认证失败", category="错误码",
         expect_layer="error_code", expect_code="WRONGPASS", expect_kw=["WRONGPASS"],
         note="Redis 关键字型错误码"),

    dict(id="C05", query="网页打不开显示 404", category="错误码",
         expect_layer="error_code", expect_code="404", expect_kw=["404"],
         note="HTTP 状态码, 只有 3 位"),

    dict(id="C06", query="系统提示 error 1064", category="错误码",
         expect_layer="error_code", expect_code="1064", expect_kw=["1064"],
         note="带 error 前缀的标准写法"),

    dict(id="C07", query="MySQL 报 1049 找不到库", category="错误码",
         expect_layer="error_code", expect_code="1049", expect_kw=["1049"],
         note="错误码 + 中文解释混排"),

    dict(id="C08", query="浏览器 ERR_CONNECTION_RESET", category="错误码",
         expect_layer="error_code", expect_code="ERR_CONNECTION_RESET", expect_kw=["ERR_CONNECTION_RESET"],
         note="长下划线错误码"),

    # ---------------- D. 混合意图 (4 题) ----------------
    # 一句话里两个诉求。测的是"会不会只答一半"。
    # 判定放宽: 只要答到其中一个诉求即算对(真实系统做法是分点回答或追问)。
    dict(id="D01", query="vpn 连不上，而且邮箱也收不到信", category="混合意图",
         expect_layer="faq", expect_kw=["VPN", "邮件"],
         note="两个域叠加, 理想行为是分点回答或追问先解决哪个"),

    dict(id="D02", query="新员工入职要开哪些账号，电脑什么时候能领", category="混合意图",
         expect_layer="faq", expect_kw=["账号", "电脑"],
         note="账号域 + 资产申领, 跨子域"),

    dict(id="D03", query="打印机卡纸了，打出来还是乱码", category="混合意图",
         expect_layer="faq", expect_kw=["卡纸", "乱码"],
         note="同一设备两个故障, 测能否覆盖"),

    dict(id="D04", query="密码忘了，顺便问下密码策略多长", category="混合意图",
         expect_layer="faq", expect_kw=["密码", "复杂", "策略"],
         note="操作诉求 + 政策咨询"),

    # ---------------- E. 越界题 (8 题) ----------------
    # 知识库里根本没有这些信息。正确行为 = 拒答 + 转人工, 绝不能编。
    dict(id="E01", query="今天天气怎么样", category="越界",
         expect_layer="fallback", expect_kw=[], should_refuse=True,
         note="完全无关, 测是否会被硬答"),

    dict(id="E02", query="帮我写一首关于春天的诗", category="越界",
         expect_layer="fallback", expect_kw=[], should_refuse=True,
         note="创作类请求, 不是知识问答"),

    dict(id="E03", query="北京到上海高铁要多久", category="越界",
         expect_layer="fallback", expect_kw=[], should_refuse=True,
         note="生活常识, 知识库无"),

    dict(id="E04", query="股票怎么开户", category="越界",
         expect_layer="fallback", expect_kw=[], should_refuse=True,
         note="金融领域, 知识库无"),

    dict(id="E05", query="公司年会在哪里办", category="越界",
         expect_layer="fallback", expect_kw=[], should_refuse=True,
         note="企业内部信息, IT 知识库不覆盖 —— 最容易误答的一类"),

    dict(id="E06", query="红烧肉怎么做", category="越界",
         expect_layer="fallback", expect_kw=[], should_refuse=True,
         note="烹饪, 与 IT 完全无关"),

    dict(id="E07", query="推荐一部好看的电影", category="越界",
         expect_layer="fallback", expect_kw=[], should_refuse=True,
         note="推荐类请求"),

    dict(id="E08", query="人工智能会取代人类吗", category="越界",
         expect_layer="fallback", expect_kw=[], should_refuse=True,
         note="开放性观点题 —— 知识库无标准答案, 应拒答"),
]


def get_cases(categories=None):
    """
    取测试集。
    :param categories: 只取指定题型, 如 ["越界"]; None 表示全部
    :return: list[dict]
    """
    if not categories:
        return list(INDEPENDENT_CASES)
    return [c for c in INDEPENDENT_CASES if c["category"] in categories]


def get_refuse_cases():
    """只取应拒答的题(用于单独统计拒答准确率)"""
    return [c for c in INDEPENDENT_CASES if c.get("should_refuse")]


def get_answerable_cases():
    """只取应该答出来的题(用于统计误拒率)"""
    return [c for c in INDEPENDENT_CASES if not c.get("should_refuse")]


if __name__ == "__main__":
    from collections import Counter

    total = len(INDEPENDENT_CASES)
    print(f"独立测试集共 {total} 题")
    for cat, n in Counter(c["category"] for c in INDEPENDENT_CASES).items():
        print(f"  {cat:<8} {n:>2} 题")
    print(f"\n应拒答 {len(get_refuse_cases())} 题 / 应答出 {len(get_answerable_cases())} 题")

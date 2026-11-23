# -*- coding: utf-8 -*-
# data/eval/retrieval_set.py
# 检索层评测集 —— 用于消融实验(有/无 reranker、混合/纯向量)
#
# ============================================================================
# 为什么单独做一套检索题
# ============================================================================
# 生成质量的评测需要人工判分或 LLM-as-judge, 成本高;
# 而检索层的"召回是否正确"可以客观自动判定。
#
# 判定口径(两档, 可切换):
#   expect_source: 域级粗判(返回父块是否属于正确知识域) —— 旧口径, 偏松
#   expect_file  : 文件级精判(返回父块是否来自"正确答案所在的那一个文件") —— 新口径, 硬
#   因为每个域有 35 篇, "属于正确域"几乎必中, 虚高; 只有精确到具体文件,
#   才能测出"语义检索能力"而不是"标题匹配能力"。
#
# 关键原则: **问题措辞不照抄 data/wiki 的文档标题**。
#   若直接用标题(如"VPN客户端安装与常见报错"), BM25 会靠标题词命中, 召回率虚高,
#   所以这里全部改写为真人会问的口语化句子。
#
# 用法:
#   python scripts/eval_quality.py --ablation
# ============================================================================

RETRIEVAL_CASES = [
    # ---------------- network (8) ----------------
    dict(id="R01", query="装了 vpn 客户端之后一直报错打不开",
         expect_source="network", expect_file="VPN客户端安装与常见报错",
         note="对应 VPN 客户端安装与报错, 但不含'安装'字样"),
    dict(id="R02", query="打开网站提示找不到服务器",
         expect_source="network", expect_file="DNS解析异常排查手册",
         note="DNS 解析异常的典型口述"),
    dict(id="R03", query="网速很慢，打开什么都卡",
         expect_source="network", expect_file="办公网带宽占用过高排查指南",
         note="带宽占用 / 延迟丢包"),
    dict(id="R04", query="插上网线还是上不了网",
         expect_source="network", expect_file="办公网有线接入与IP地址申请规范",
         note="有线接入 + 内网外网排查"),
    dict(id="R05", query="公司 wifi 连上就掉线，反复重连",
         expect_source="network", expect_file="无线AP漫游切换异常排查",
         note="无线 AP 漫游 / 设备绑定"),
    dict(id="R06", query="访问内网系统提示证书不安全",
         expect_source="network", expect_file="SSL证书更新与内网证书信任导入",
         note="SSL 证书信任导入"),
    dict(id="R07", query="两个网卡都有网，流量走哪个",
         expect_source="network", expect_file="双网卡路由冲突与优先级设置",
         note="双网卡路由优先级"),
    dict(id="R08", query="在家连不上公司电脑的桌面",
         expect_source="network", expect_file="远程桌面无法连接内网主机排查",
         note="远程桌面 / 堡垒机通道"),

    # ---------------- account (8) ----------------
    dict(id="R09", query="离职了邮箱还能用吗",
         expect_source="account", expect_file="离职员工账号回收规范",
         note="离职账号回收规范"),
    dict(id="R10", query="邮箱满了收不到新邮件",
         expect_source="account", expect_file="邮箱容量清理与归档指南",
         note="容量清理与归档"),
    dict(id="R11", query="收到一封说是财务部让改密码的邮件",
         expect_source="account", expect_file="邮件钓鱼与反欺诈识别指南",
         note="邮件钓鱼识别 —— 关键词完全不重合"),
    dict(id="R12", query="换了手机，验证码收不到",
         expect_source="account", expect_file="多因素认证(MFA)强制开启说明",
         note="MFA 多因素认证绑定"),
    dict(id="R13", query="新同事第一天来，账号什么时候能给",
         expect_source="account", expect_file="新员工入职IT账号开通时间表",
         note="入职账号开通时间表"),
    dict(id="R14", query="邮箱签名想统一成公司模板",
         expect_source="account", expect_file="邮箱签名与自动回复设置规范",
         note="签名与自动回复规范"),
    dict(id="R15", query="同一个账号在别的城市登录了",
         expect_source="account", expect_file="账号异地登录告警与处理流程",
         note="异地登录告警处置"),
    dict(id="R16", query="外包人员的账号怎么管",
         expect_source="account", expect_file="外包人员账号管理规范",
         note="外包人员账号管理规范"),

    # ---------------- printer (8) ----------------
    dict(id="R17", query="打印出来一半是空白",
         expect_source="printer", expect_file="PDF打印空白与编码问题排查",
         note="PDF 打印空白 / 缺粉"),
    dict(id="R18", query="打印机显示脱机",
         expect_source="printer", expect_file="网络打印机无法打印排查指南",
         note="离线 / 端口异常"),
    dict(id="R19", query="换了新电脑连不上打印机",
         expect_source="printer", expect_file="打印机驱动安装手册",
         note="驱动安装 / 共享连接"),
    dict(id="R20", query="扫描件发不到邮箱",
         expect_source="printer", expect_file="扫描到邮箱功能配置说明",
         note="扫描到邮箱配置"),
    dict(id="R21", query="彩打申请怎么弄",
         expect_source="printer", expect_file="彩色打印权限申请与配额管理",
         note="彩色打印权限配额"),
    dict(id="R22", query="打印出来字是乱码",
         expect_source="printer", expect_file="打印乱码与字体缺失排查",
         note="字体缺失 / 驱动不匹配"),
    dict(id="R23", query="键盘打字没反应",
         expect_source="printer", expect_file="键盘鼠标失灵与更换申请",
         note="外设接入(归在 printer 子域)"),
    dict(id="R24", query="会议室投屏没画面",
         expect_source="printer", expect_file="会议平板与投屏设备连接说明",
         note="投影 / 会议平板"),

    # ---------------- software (8) ----------------
    dict(id="R25", query="office 提示盗版",
         expect_source="software", expect_file="Office激活失败与修复指南",
         note="激活失败与修复"),
    dict(id="R26", query="电脑开机风扇狂转",
         expect_source="software", expect_file="系统性能监控与资源占用分析",
         note="资源占用 / 性能分析"),
    dict(id="R27", query="装的软件被安全软件拦了",
         expect_source="software", expect_file="杀毒软件拦截程序的放行方法",
         note="杀毒白名单放行"),
    dict(id="R28", query="电脑时间老是变",
         expect_source="software", expect_file="系统时间不同步与时钟服务",
         note="时钟服务 / 时间同步"),
    dict(id="R29", query="系统更新卡住不动",
         expect_source="software", expect_file="系统更新失败与回滚处理",
         note="更新失败与回滚"),
    dict(id="R30", query="桌面上图标都没了",
         expect_source="software", expect_file="桌面图标丢失与开始菜单异常",
         note="桌面图标 / 开始菜单异常"),
    dict(id="R31", query="装了两个版本的 python 冲突",
         expect_source="software", expect_file="软件多版本共存与冲突解决",
         note="多版本共存"),
    dict(id="R32", query="程序一打开就崩溃",
         expect_source="software", expect_file="软件崩溃与日志收集方法",
         note="崩溃日志收集"),
]


def get_cases(sources=None):
    """取检索评测题; sources 可限定知识域, 如 ["network"]"""
    if not sources:
        return list(RETRIEVAL_CASES)
    return [c for c in RETRIEVAL_CASES if c["expect_source"] in sources]


if __name__ == "__main__":
    from collections import Counter

    print(f"检索评测集共 {len(RETRIEVAL_CASES)} 题")
    for src, n in Counter(c["expect_source"] for c in RETRIEVAL_CASES).items():
        print(f"  {src:<10} {n} 题")

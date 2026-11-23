# -*- coding: utf-8 -*-
# scripts/gen_wiki_docs.py
# 用千问(百炼 DashScope) 批量合成"仿真企业运维 Wiki 文档"
#
# 为什么用 LLM 合成而非找真实数据:
#   真实企业的 Confluence/飞书 Wiki 数据涉密、拿不到。用 LLM 生成"形态对标真实 Wiki"
#   的文档(多级标题 + 编号步骤 + 表格 + 注意事项), 既能验证完整 RAG 流水线,
#   又体现了"理解企业数据保密"的常识。面试时这是加分话术, 不是减分。
#
# 用法: python scripts/gen_wiki_docs.py
# 输出: data/wiki/<source>/<文档名>.md  每个 source 一个子目录
import json
import os
import sys
import time
import urllib.request

# 将项目根目录加入模块搜索路径, 保证无论从哪个目录运行都能导入 base/core 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base.config import config
from base.logger import logger

# 要生成的知识域及其子目录名
DOMAINS = {
    "network": "网络与VPN",
    "account": "账号与邮箱",
    "printer": "打印机与硬件",
    "software": "软件与报错",
}

# 每个知识域要生成的文档主题（模拟真实企业 Wiki 的标题）
TOPICS = {
    "network": [
        "VPN客户端安装与常见报错",
        "内网无法访问外网的排查流程",
        "公司WiFi连接与设备绑定说明",
        "远程办公网络接入规范",
        "防火墙策略申请与变更流程",
        "办公网有线接入与IP地址申请规范",
        "访客网络接入与临时账号管理办法",
        "DNS解析异常排查手册",
        "代理服务器配置与PAC脚本说明",
        "VPN多因素认证(MFA)绑定指引",
        "网络延迟与丢包排查流程",
        "DHCP地址池耗尽与续租故障处理",
        "双网卡路由冲突与优先级设置",
        "无线AP漫游切换异常排查",
        "SSL证书更新与内网证书信任导入",
        "堡垒机登录与远程运维通道说明",
        "跨部门网络权限申请流程",
        "视频会议网络质量优化指南",
        "公司邮箱收发信网络依赖排查",
        "移动端VPN接入配置(安卓/iOS)",
        "网络准入认证(802.1X)配置说明",
        "链路冗余与主备切换演练方案",
        "工位网络端口故障报修与处理",
        "Ping不通但能上网的常见原因",
        "网关地址冲突定位与解决",
        "内网IP地址段划分与规划说明",
        "网络设备日志查看与告警响应",
        "VPN连接后无法访问特定系统排查",
        "办公网带宽占用过高排查指南",
        "远程桌面无法连接内网主机排查",
        "新办公楼网络部署与验收标准",
        "网络交换机端口VLAN配置规范",
        "物联网设备网络隔离与接入规范",
        "网络环路广播风暴定位与处理",
        "公司VPN账号申请与配额说明",
    ],
    "account": [
        "域账号解锁与密码重置SOP",
        "企业邮箱配置与常见问题",
        "新员工账号开通清单",
        "账号权限申请与审批流程",
        "离职员工账号回收规范",
        "域账号密码复杂度与过期策略",
        "AD域账号锁定策略与自解锁说明",
        "单点登录(SSO)配置与常见问题",
        "邮箱容量清理与归档指南",
        "邮件群组与通讯组管理规范",
        "共享邮箱与委托权限设置说明",
        "账号异地登录告警与处理流程",
        "移动设备邮箱配置(Outlook/手机)",
        "邮箱签名与自动回复设置规范",
        "域账号密码自助重置门户使用说明",
        "第三方系统账号同步与去重",
        "实习生账号开通与权限范围",
        "外包人员账号管理规范",
        "邮箱附件大小限制与发送失败处理",
        "账号被盗后的紧急处置流程",
        "邮件钓鱼与反欺诈识别指南",
        "多因素认证(MFA)强制开启说明",
        "域账号登录失败次数与锁定关系",
        "公共账号与业务账号使用规范",
        "邮箱别名与主邮箱切换流程",
        "假期自动回复与外出设置",
        "账号审计与权限定期复核规范",
        "新员工入职IT账号开通时间表",
        "邮箱客户端无法收发信排查",
        "域账号与邮箱账号的对应关系说明",
        "供应商外部账号开通与回收流程",
        "密码管理器使用与推广说明",
        "账号禁用与重新启用流程",
        "邮件大附件传输方案(FTP/网盘)",
        "账号生命周期管理与自动化",
    ],
    "printer": [
        "打印机驱动安装手册",
        "网络打印机无法打印排查指南",
        "会议室设备使用与故障报修",
        "工位电脑外设接入规范",
        "打印机耗材更换与报修流程",
        "打印队列卡住与清理方法",
        "打印乱码与字体缺失排查",
        "双面打印与默认设置调整",
        "共享打印机连接与权限设置",
        "扫描到邮箱功能配置说明",
        "复印机故障代码对照与处理",
        "打印任务失败与驱动重装指南",
        "USB打印线连接与识别问题",
        "打印机IP地址冲突与修改",
        "彩色打印权限申请与配额管理",
        "打印计费系统使用与查询",
        "无线打印配置(手机/平板)",
        "大型复印机保养与定期维护",
        "打印尺寸与纸张类型设置",
        "PDF打印空白与编码问题排查",
        "会议室投影与音视频系统使用",
        "门禁卡权限申请与补办流程",
        "监控摄像头故障报修流程",
        "工位显示器与外接屏设置",
        "键盘鼠标失灵与更换申请",
        "USB接口供电不足与扩展坞使用",
        "会议平板与投屏设备连接说明",
        "打印机端口占用与后台服务异常",
        "打印服务器与集中打印部署说明",
        "特殊纸张与装订功能使用指南",
        "设备固定资产标签与盘点规范",
        "办公设备报废与回收处置流程",
        "工位电源与网口布局说明",
        "会议室预订系统与设备联动",
        "打印机固件升级与安全补丁",
    ],
    "software": [
        "办公软件常见报错处理",
        "杀毒软件拦截程序的放行方法",
        "公司标准软件安装清单",
        "系统更新与补丁推送说明",
        "Office激活失败与修复指南",
        "浏览器插件冲突与故障排查",
        "软件白名单申请与安装流程",
        "系统蓝屏代码对照与处理",
        "磁盘空间不足清理指南",
        "电脑开机慢与优化建议",
        "软件崩溃与日志收集方法",
        "邮箱客户端(Outlook)配置排错",
        "即时通讯软件登录异常排查",
        "远程会议软件音频视频问题",
        "浏览器无法打开网页排查流程",
        "压缩文件损坏与解压失败处理",
        "PDF阅读器与编辑软件问题",
        "杀毒软件更新失败与病毒库异常",
        "输入法异常与语言栏设置",
        "系统时间不同步与时钟服务",
        "软件卸载残留与清理方法",
        "Windows系统还原与恢复点使用",
        "办公软件文档格式兼容问题",
        "系统更新失败与回滚处理",
        "应用商店下载失败排查",
        "文件共享权限与访问异常",
        "云盘客户端同步失败排查",
        "邮件客户端与杀软冲突处理",
        "专业软件许可证申请与激活",
        "电脑弹出广告与恶意软件处理",
        "系统环境变量配置与问题",
        "软件多版本共存与冲突解决",
        "桌面图标丢失与开始菜单异常",
        "剪贴板异常与复制粘贴失效",
        "系统性能监控与资源占用分析",
    ],
}

# 生成文档的 prompt 模板
SYSTEM_PROMPT = (
    "你是一名企业 IT 运维文档撰写专家。请生成一篇结构规范、内容专业的中文运维 Wiki 文档。"
    "要求：1) 使用 Markdown 格式；2) 必须包含多级标题(## 和 ###)；3) 至少包含一个 Markdown 表格"
    "和一个有序步骤列表；4) 内容具体可操作，包含真实的技术细节和常见错误码；5) 不要出现虚构的公司名"
    "和个人信息，统一用「公司」指代；6) 直接输出 Markdown 正文，不要任何解释性前言。"
)


def call_llm(system: str, user: str, model=None, temperature=0.7) -> str:
    """调用百炼 DashScope 兼容接口，返回文本内容"""
    model = model or config.LLM_FLASH_MODEL
    if not config.DASHSCOPE_API_KEY:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY，请在 it_ops_qa/.env 中设置")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
    }
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


def gen_all_docs():
    """生成所有知识域的仿真 Wiki 文档"""
    total = 0
    for source, label in DOMAINS.items():
        out_dir = os.path.join(config.WIKI_DIR, source)
        os.makedirs(out_dir, exist_ok=True)
        for topic in TOPICS[source]:
            fname = topic.replace("/", "_") + ".md"
            fpath = os.path.join(out_dir, fname)
            if os.path.exists(fpath) and os.path.getsize(fpath) > 100:
                logger.info(f"已存在, 跳过: {fpath}")
                continue
            user_prompt = f"主题：{topic}\n所属知识域：{label}"
            logger.info(f"生成中: {label} / {topic} ...")
            try:
                content = call_llm(SYSTEM_PROMPT, user_prompt)
                with open(fpath, "w", encoding="utf-8") as f:
                    f.write(content)
                total += 1
                logger.info(f"已生成: {fpath} ({len(content)} 字)")
            except Exception as e:
                logger.error(f"生成失败 {topic}: {e}")
            time.sleep(1)  # 限速, 避免触发 API 频率限制
    logger.info(f"全部完成, 共生成 {total} 篇文档")


if __name__ == "__main__":
    gen_all_docs()

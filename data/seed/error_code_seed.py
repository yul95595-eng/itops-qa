# -*- coding: utf-8 -*-
# data/seed/error_code_seed.py
# 内置的常见 IT 运维错误码种子数据（MySQL / Redis / Windows / 网络）
# 说明: 这是"精确匹配层"的初始数据, 用于让系统零依赖先跑通;
#       之后可替换为从官方文档爬取/整理的完整错误码表。
#
# 每条格式: (错误码, 所属组件, 含义, 常见原因, 解决步骤)

ERROR_CODES = [
    # ============ MySQL ============
    ("1045", "MySQL", "Access denied for user", "用户名或密码错误、主机未授权",
     "1. 核对用户名/密码; 2. 检查用户是否允许从该主机登录(grant ... @'%'); 3. 检查 my.cnf 的 skip-grant-tables 是否误开"),
    ("1049", "MySQL", "Unknown database", "连接的库不存在或拼写错误",
     "1. SHOW DATABASES 确认库名; 2. 检查连接串里的库名大小写/拼写; 3. 若确实不存在则 CREATE DATABASE"),
    ("1064", "MySQL", "SQL syntax error", "SQL 语句语法错误",
     "1. 定位报错提示的 \"near '...'\" 附近; 2. 检查关键字/引号/逗号; 3. 复杂语句分段执行定位"),
    ("1062", "MySQL", "Duplicate entry for key", "主键或唯一索引重复插入",
     "1. 确认是否重复提交; 2. 检查业务逻辑是否应使用 INSERT ... ON DUPLICATE KEY UPDATE; 3. 清理重复数据"),
    ("1146", "MySQL", "Table doesn't exist", "表不存在或所在库不对",
     "1. SHOW TABLES 确认表名; 2. 检查是否连错库(USE 切换); 3. 大小写敏感时核对表名"),
    ("2003", "MySQL", "Can't connect to MySQL server", "服务未启动 / 端口不通 / 防火墙拦截",
     "1. systemctl status mysqld 确认服务; 2. 确认监听端口 3306; 3. 检查防火墙放行; 4. 检查 bind-address 是否限制"),
    ("2006", "MySQL", "Server has gone away", "连接超时被回收 / 包过大 / 服务重启",
     "1. 增大 wait_timeout / max_allowed_packet; 2. 确认连接池有 ping 检活; 3. 检查服务是否重启"),
    ("1366", "MySQL", "Incorrect string value", "字符编码不匹配(中文乱码)",
     "1. 统一库/表/连接为 utf8mb4; 2. 连接串加 charset=utf8mb4; 3. 检查数据源编码"),

    # ============ Redis ============
    ("ERR AUTH", "Redis", "NOAUTH Authentication required", "Redis 设置了密码但客户端未认证",
     "1. 连接串携带密码(-a 或 AUTH 命令); 2. 检查 requirepass 配置; 3. 密码含特殊字符需转义"),
    ("WRONGPASS", "Redis", "invalid username-password pair", "Redis 密码错误",
     "1. 核对密码; 2. 确认 ACL 用户名; 3. 密码含 @ 等字符时注意 URL 编码"),
    ("LOADING", "Redis", "Redis is loading the dataset in memory", "Redis 正在从磁盘加载数据",
     "1. 这是启动时的正常状态; 2. 等待加载完成(大数据量可能几分钟); 3. 关注 rdb 文件是否过大"),
    ("OOM", "Redis", "command not allowed when used memory > maxmemory", "内存超限, 触发 maxmemory-policy",
     "1. INFO memory 查看用量; 2. 检查 maxmemory 配置; 3. 设置淘汰策略(如 allkeys-lru); 4. 清理无 TTL 的 key"),
    ("READONLY", "Redis", "You can't write against a read only replica", "向从节点执行写操作",
     "1. 确认当前连的是主节点; 2. 从节点只读; 3. 检查客户端是否连错端口"),

    # ============ Windows ============
    ("0x80072EE7", "Windows", "无法解析服务器名称或地址", "网络 DNS 解析失败 / 代理配置错误",
     "1. nslookup 测试 DNS; 2. 检查 hosts 文件; 3. 检查系统代理设置; 4. 换用公共 DNS(114.114.114.114)"),
    ("0x80070005", "Windows", "拒绝访问 (Access Denied)", "权限不足 / 防火墙拦截",
     "1. 以管理员身份运行; 2. 检查文件/目录权限; 3. 临时关闭防火墙测试"),
    ("0xC0000022", "Windows", "STATUS_ACCESS_DENIED", "文件或程序权限问题",
     "1. 右键属性→安全 赋予权限; 2. 以管理员运行; 3. 检查杀软是否拦截"),
    ("0x8024402C", "Windows", "Windows Update 连接失败", "更新服务未启动 / 代理拦截",
     "1. 重启 Windows Update 服务(wuauserv); 2. 检查代理; 3. 运行 Windows Update 疑难解答"),
    ("0x80070422", "Windows", "服务未启动", "依赖的 Windows 服务被禁用",
     "1. services.msc 找到对应服务; 2. 设置启动类型为自动; 3. 手动启动"),

    # ============ 网络 / VPN ============
    ("691", "网络", "宽带/VPN 拨号 691 错误", "账号密码错误 / 账号被占用 / 欠费",
     "1. 核对账号密码; 2. 确认账号未被其他终端占用; 3. 检查是否欠费; 4. 重启拨号客户端"),
    ("720", "网络", "VPN 720 错误", "拨号网络设置问题 / 协议不匹配",
     "1. 重建拨号连接; 2. 检查 VPN 协议(L2TP/PPTP); 3. 更新网卡驱动"),
    ("800", "网络", "VPN 800 错误", "VPN 服务器不可达(路由/防火墙)",
     "1. ping VPN 服务器地址; 2. 检查防火墙放行 VPN 端口; 3. 检查本地路由"),
    ("ERR_CONNECTION_RESET", "网络", "连接被重置", "网络中断 / 防火墙阻断 / 代理异常",
     "1. 刷新 DNS(ipconfig /flushdns); 2. 检查代理是否生效; 3. 换网络环境测试"),
    ("ERR_CERT_AUTHORITY_INVALID", "网络", "证书颁发机构无效", "SSL 证书不受信任 / 系统时间错误",
     "1. 校准系统时间; 2. 确认证书链完整; 3. 内网自签证书需导入信任"),
    ("ERR_NAME_NOT_RESOLVED", "网络", "无法解析域名", "DNS 服务器异常",
     "1. ipconfig /flushdns; 2. 更换 DNS(114.114.114.114); 3. 检查 hosts 是否被篡改"),

    # ============ Oracle ============
    ("ORA-12541", "Oracle", "TNS: 无监听程序", "监听器未启动 / 端口不通 / 服务名错误",
     "1. lsnrctl start 启动监听; 2. 确认监听端口(默认1521); 3. 检查 tnsnames.ora 的 host/port; 4. 防火墙放行 1521"),
    ("ORA-12154", "Oracle", "TNS: 无法解析指定的连接标识符", "tnsnames.ora 配置错误或缺失",
     "1. 检查 tnsnames.ora 中的连接名; 2. 确认 ORACLE_HOME 环境变量; 3. 连接名与配置一致"),
    ("ORA-00001", "Oracle", "唯一约束违反", "主键/唯一索引重复",
     "1. 检查是否重复提交; 2. 查询已有数据; 3. 使用 MERGE 或先删后插"),
]

# 组件标签（用于检索过滤）
COMPONENTS = ["MySQL", "Redis", "Windows", "网络"]


def get_error_codes():
    """返回错误码列表（内置种子 + 大规模扩展表）"""
    return ERROR_CODES


# 合并大规模错误码表(500 条规模), 供入库使用
def get_all_error_codes():
    """
    返回完整错误码列表 = 内置种子(27条) + 大规模扩展表(约500条)。
    去重依据: (code, component) 组合。
    """
    try:
        from data.seed.error_codes_large import LARGE_ERROR_CODES
    except ImportError:
        from error_codes_large import LARGE_ERROR_CODES

    merged = []
    seen = set()
    for rec in list(ERROR_CODES) + list(LARGE_ERROR_CODES):
        key = (rec[0].upper(), rec[1])
        if key in seen:
            continue
        seen.add(key)
        merged.append(rec)
    return merged


if __name__ == '__main__':
    print(f"内置错误码共 {len(ERROR_CODES)} 条")
    from collections import Counter
    c = Counter(code[1] for code in ERROR_CODES)
    for comp, n in c.items():
        print(f"  {comp}: {n} 条")

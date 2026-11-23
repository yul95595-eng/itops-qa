# DNS解析异常排查手册  
**所属知识域：网络与VPN**  
*最后更新时间：2024年6月*  

---

## 一、概述  
DNS解析异常是指终端设备无法将域名（如 `www.company.com`）正确解析为IP地址，导致业务访问失败、网页打不开、API调用超时、内部服务连通性中断等现象。本手册面向公司IT运维人员及一线技术支持工程师，提供标准化、可复现的排查流程与关键验证点。

---

## 二、常见现象与初步判断  

| 现象类型 | 典型表现 | 可能原因方向 |
|----------|----------|--------------|
| **全网解析失败** | 所有域名均无法访问（含 `www.baidu.com`），`ping` 域名失败，`nslookup` 报 `server can't find` 或 `connection timed out` | 上游DNS服务器不可达、本地DNS配置错误、防火墙阻断53端口、系统级DNS缓存污染 |
| **部分域名解析失败** | 仅公司内网域名（如 `intranet.company.local`）或特定SaaS服务（如 `api.snowflake.com`）失败，公网通用域名正常 | DNS策略路由异常、条件转发（Conditional Forwarding）配置错误、私有DNS区域未同步、Split DNS配置失效 |
| **间歇性解析失败** | `nslookup` 偶尔成功/失败，`dig +short` 返回空或超时，应用日志出现 `NXDOMAIN` / `SERVFAIL` 随机交替 | DNS服务器负载过高、UDP包被截断（TC=1）、EDNS0协商失败、上游递归DNS不稳定 |
| **解析结果错误** | `nslookup company.com` 返回非预期IP（如测试环境IP、旧A记录、0.0.0.0） | DNS缓存中毒、TTL未生效、负载均衡DNS轮询异常、CDN配置错误、Hosts文件劫持 |

> ⚠️ 注意：在排查前，请确认问题是否与VPN状态强相关（如仅在接入公司SSL VPN后出现），此类场景需优先检查VPN客户端的DNS分流策略（如 `split-dns` 或 `dns-server` 推送配置）。

---

## 三、标准化排查步骤  

### 步骤1：确认基础网络连通性  
1. 执行 `ping 8.8.8.8` —— 验证IP层可达性；  
2. 执行 `telnet 8.8.8.8 53` 或 `nc -zv 8.8.8.8 53` —— 验证DNS端口（TCP/UDP 53）未被防火墙拦截；  
3. 若失败，检查本地防火墙、终端安全软件、公司出口防火墙策略（尤其关注 `UDP 53` 出向规则）。

### 步骤2：定位当前DNS服务器配置  
- **Windows**：`ipconfig /all \| findstr "DNS Servers"`  
- **Linux/macOS**：`cat /etc/resolv.conf` 或 `systemd-resolve --status \| grep "DNS Servers"`（若启用systemd-resolved）  
- **VPN连接中**：检查OpenConnect / AnyConnect日志，确认是否推送了 `DNS Server: 10.10.20.10` 类内网DNS地址，并验证该地址是否可达（`nslookup company.com 10.10.20.10`）

### 步骤3：分层执行DNS查询验证  
按以下顺序逐级执行，记录返回结果与响应时间：

| 查询方式 | 命令示例 | 目的 | 关键观察项 |
|----------|-----------|------|-------------|
| ① 本地缓存查询 | `nslookup company.com`（无指定server） | 检查默认DNS行为 | 是否超时？返回`*** Can't find...`？ |
| ② 绕过缓存直连权威DNS | `nslookup company.com 8.8.8.8` | 排除本地DNS服务故障 | 是否返回正确A记录？是否有`Non-authoritative answer`？ |
| ③ 查询权威NS服务器 | `nslookup -type=ns company.com 8.8.8.8` → 获取NS列表 → `nslookup company.com ns1.company-dns.com` | 验证权威链完整性 | NS是否响应？SOA记录是否有效？ |
| ④ 检查DNSSEC状态（如启用） | `dig company.com +dnssec +short` | 排查签名验证失败（`SERVFAIL`常见原因） | 是否含`ad`（authentic data）标志？是否存在`bad signature`？ |

### 步骤4：检查本地缓存与Hosts干扰  
- 清除本地DNS缓存：  
  - Windows：`ipconfig /flushdns`  
  - macOS：`sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder`  
  - Linux（systemd-resolved）：`sudo systemd-resolve --flush-caches`  
- 检查Hosts文件：  
  - Windows：`C:\Windows\System32\drivers\etc\hosts`  
  - Linux/macOS：`/etc/hosts`  
  → 查找是否存在 `127.0.0.1 company.com` 或 `0.0.0.0 api.company.com` 等强制映射条目。

### 步骤5：抓包分析（进阶）  
当上述步骤无法定位时，在客户端执行：  
```bash
# Linux/macOS（捕获DNS流量）
sudo tcpdump -i any port 53 -w dns-trace.pcap

# Windows（使用Wireshark过滤器）
udp.port == 53 || tcp.port == 53
```
重点关注：  
- 请求是否发出？源/目的IP与端口是否符合预期；  
- 是否收到响应？响应码（RCODE）是否为 `NOERROR`（0）、`NXDOMAIN`（3）、`SERVFAIL`（2）；  
- UDP响应是否被截断（TC=1）且未自动降级为TCP重试；  
- EDNS0选项是否存在且版本兼容（`UDP packet size >= 1220`）。

---

## 四、典型错误码速查表  

| RCODE | 十进制值 | 含义 | 常见原因 | 应对建议 |
|--------|-----------|------|------------|------------|
| NOERROR | 0 | 查询成功 | — | ✅ 正常响应 |
| NXDOMAIN | 3 | 域名不存在 | 域名拼写错误、DNS区域未部署、子域未授权 | 检查`dig -t soa parent.com`确认父域授权；核对DNS管理平台配置 |
| SERVFAIL | 2 | 服务器失败 | 权威DNS宕机、递归DNS配置错误、DNSSEC验证失败、后端数据库不可用 | 切换至其他递归DNS（如`1.1.1.1`）测试；检查`dig +trace domain.com`递归路径 |
| REFUSED | 5 | 服务器拒绝查询 | DNS服务器ACL限制（如只允许内网IP）、BIND `allow-query`配置错误 | 检查DNS服务端访问控制策略；确认客户端IP是否在白名单内 |
| TIMEOUT | — | 超时无响应 | 网络丢包、防火墙拦截、DNS服务进程僵死 | `mtr -z -p 53 <dns-server>` 测试路径；`systemctl status named` 查看服务状态 |

---

## 五、附录：快速验证脚本（Linux/macOS）  
```bash
#!/bin/bash
DOMAIN=${1:-"company.com"}
echo "=== DNS Health Check for $DOMAIN ==="
echo "1. Local resolv.conf:"
cat /etc/resolv.conf 2>/dev/null | grep nameserver
echo -e "\n2. Default resolution:"
nslookup "$DOMAIN" 2>&1 | head -10
echo -e "\n3. Public resolver (8.8.8.8):"
nslookup "$DOMAIN" 8.8.8.8 2>&1 | head -10
echo -e "\n4. DNSSEC status:"
dig "$DOMAIN" +short +dnssec 2>/dev/null | tail -5
echo -e "\n5. TTL check:"
dig "$DOMAIN" +short +noall +answer 2>/dev/null
```

> ✅ 将脚本保存为 `dns-check.sh`，运行 `chmod +x dns-check.sh && ./dns-check.sh intranet.company.local` 快速输出核心诊断信息。

---  
**文档维护团队：公司基础设施运维中心**  
**版本控制：v2.3 — 新增DNSSEC与EDNS0排查指引**
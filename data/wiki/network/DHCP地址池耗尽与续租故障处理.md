# DHCP地址池耗尽与续租故障处理

> **适用对象**：网络运维工程师、系统管理员  
> **更新日期**：2024-06-15  
> **文档版本**：v2.3  

---

## 一、问题概述

DHCP地址池耗尽（IP Exhaustion）和客户端续租失败（Renew Failure）是企业局域网中高频发生的网络连通性问题，常导致新设备无法入网、已有终端断网或获取到 `169.254.x.x`（APIPA）链路本地地址，进而引发业务中断。

常见现象包括：
- 新终端获取 IP 超时（`DHCPDISCOVER` 无响应）；
- 已在线终端在 T1/T2 定时器超时后释放 IP 并进入 `INIT` 状态；
- 客户端日志出现错误码 `0x80070005`（访问拒绝）、`0x8007007E`（模块未找到）或 `0x80070426`（服务未启动）；
- DHCP 服务器日志显示 `No free leases in pool` 或 `Pool exhausted`。

---

## 二、根因分析矩阵

| 故障现象 | 常见根因 | 检查路径 | 关键指标 |
|----------|-----------|------------|-------------|
| 所有新终端无法获取IP | 地址池完全耗尽 / 作用域未启用 / 作用域范围过小 | `Get-DhcpServerv4Scope`（Windows）<br>`dhcpd -t`（Linux ISC） | `FreeAddressCount == 0`<br>`State == InActive` |
| 部分终端续租失败（T1超时后广播RENEW无响应） | 客户端与DHCP服务器间路由/ACL阻断UDP 67/68<br>防火墙策略误拦截DHCP流量 | `tcpdump -i eth0 port 67 or port 68`<br>`netsh interface ipv4 show interfaces` | `RENEW request未到达服务器`<br>`DHCPACK未返回客户端` |
| 续租成功但IP变更（非预期重分配） | 地址池中存在大量 `Expired` 或 `Abandoned` 状态租约未回收 | `Get-DhcpServerv4Lease -ScopeId x.x.x.x \| Where-Object {$_.IpAddress -ne $null}` | `AbandonedLeaseCount > 5% of total pool` |
| Windows客户端显示“受限连接” | DHCP Client服务异常 / 注册表 `DhcpConnForceBroadcastFlag` 强制广播失效 | `sc query dhcp`<br>`reg query "HKLM\SYSTEM\CurrentControlSet\Services\Dhcp\Parameters"` | `SERVICE_STATUS == STOPPED`<br>`DhcpConnForceBroadcastFlag = 0x1` |

---

## 三、标准处置流程（按优先级执行）

### 1. 快速验证与临时缓解
1. 登录DHCP服务器（Windows Server / Linux ISC DHCPd），执行基础状态检查：
   ```powershell
   # Windows PowerShell（以管理员身份）
   Get-DhcpServerv4Scope | Select ScopeId, Name, State, SubnetMask, StartRange, EndRange, FreeAddressCount
   Get-DhcpServerv4Lease -ScopeId 192.168.10.0 | Measure-Object | Select Count
   ```
   ```bash
   # Linux（ISC DHCPd）
   sudo dhcpd -t -cf /etc/dhcp/dhcpd.conf  # 配置语法校验
   sudo systemctl status isc-dhcp-server
   tail -n 20 /var/log/syslog | grep dhcpd
   ```

2. 若 `FreeAddressCount == 0`，立即执行**租约清理**（仅限测试/非生产环境快速恢复）：
   - Windows：运行 `Invoke-DhcpServerv4FailoverReplication -ScopeId 192.168.10.0 -Force`（如启用Failover）；或手动清除过期租约：
     ```powershell
     Get-DhcpServerv4Lease -ScopeId 192.168.10.0 | Where-Object {$_.AddressState -eq "Expired"} | Remove-DhcpServerv4Lease
     ```
   - Linux：重启服务并清空 `dhcpd.leases`（**慎用！需先备份**）：
     ```bash
     sudo cp /var/lib/dhcp/dhcpd.leases /var/lib/dhcp/dhcpd.leases.bak.$(date +%s)
     sudo truncate -s 0 /var/lib/dhcp/dhcpd.leases
     sudo systemctl restart isc-dhcp-server
     ```

### 2. 地址池扩容（推荐长期方案）
- **评估当前使用率**：确保 `Used / Total ≥ 85%` 时触发扩容；
- **新增作用域（Windows）**：
  ```powershell
  Add-DhcpServerv4Scope -Name "LAN-Extended" -ScopeId 192.168.11.0 -StartRange 192.168.11.100 -EndRange 192.168.11.254 -SubnetMask 255.255.255.0 -Description "Expansion for IoT devices"
  Set-DhcpServerv4OptionValue -ScopeId 192.168.11.0 -OptionId 3 -Value 192.168.11.1  # Default Gateway
  ```
- **调整现有作用域范围（Linux）**：
  编辑 `/etc/dhcp/dhcpd.conf`，修改对应 `subnet` 块的 `range` 行，例如：
  ```conf
  subnet 192.168.10.0 netmask 255.255.255.0 {
    range 192.168.10.50 192.168.10.249;  # 原为 .100–.200 → 扩容至 .50–.249
    option routers 192.168.10.1;
  }
  ```
  保存后执行 `sudo dhcpd -t && sudo systemctl reload isc-dhcp-server`

### 3. 续租路径诊断（客户端侧）
对单台故障终端执行以下步骤：
1. 释放并重新获取 IP：
   ```cmd
   ipconfig /release && ipconfig /renew
   ```
2. 抓包确认 DHCP 流程完整性（Wireshark 过滤：`bootp`）：
   - ✅ 正常流程：`DHCPDISCOVER → DHCPOFFER → DHCPREQUEST → DHCPACK`
   - ❌ 异常中断点定位：
     - 无 `DHCPOFFER` → 服务器未响应或网络不通；
     - 有 `DHCPREQUEST` 无 `DHCPACK` → 服务器拒绝请求（如MAC白名单限制、策略冲突）；
3. 检查客户端 DHCP Client 服务状态及依赖项（Windows）：
   ```cmd
   sc query dhcp
   sc query tcpip
   sc query nsi
   ```

---

## 四、预防性建议

| 措施类型 | 具体操作 | 执行周期 | 备注 |
|----------|-----------|-------------|------|
| **容量监控** | 部署Zabbix/Prometheus采集 `FreeAddressCount`、`LeaseUtilizationRate` 指标，阈值设为 90% | 实时告警 | 可结合PowerShell脚本自动推送企业微信/邮件 |
| **租约生命周期优化** | 将默认租期从 8h 调整为 24h（办公网）或 72h（稳定终端区）；避免 `infinite` 租期 | 初始部署+季度复核 | 过短租期加剧T1/T2风暴，过长阻碍IP回收 |
| **地址规划治理** | 按部门/设备类型划分子网+独立作用域（如：`192.168.10.0/24` 办公PC，`192.168.20.0/24` IoT），启用基于策略的地址分配（Policy-based Assignment） | 年度网络架构评审 | 避免“大池共用”导致单点耗尽影响全局 |
| **高可用加固** | 启用DHCP Failover（Windows）或双机热备（Linux + keepalived + shared storage） | 新建环境强制启用 | 单点DHCP服务器宕机将导致全网续租中断 |

---

## 五、附录：常用命令速查表

| 场景 | Windows PowerShell | Linux (ISC DHCPd) |
|------|---------------------|---------------------|
| 查看所有作用域 | `Get-DhcpServerv4Scope` | `grep "subnet" /etc/dhcp/dhcpd.conf` |
| 查看某作用域租约详情 | `Get-DhcpServerv4Lease -ScopeId 192.168.10.0 \| Sort-Object IPAddress \| Format-Table IPAddress,ClientId,AddressState,LeaseExpiryTime` | `awk '/lease/{print $2}' /var/lib/dhcp/dhcpd.leases \| sort \| uniq -c` |
| 清理过期租约 | `Get-DhcpServerv4Lease -ScopeId 192.168.10.0 \| Where AddressState -eq 'Expired' \| Remove-DhcpServerv4Lease` | `sed -i '/lease.*{/!d;:a;N;/}/!ba;s/[^}]*//g' /var/lib/dhcp/dhcpd.leases && systemctl restart isc-dhcp-server`（高级操作，需测试） |
| 强制同步Failover | `Invoke-DhcpServerv4FailoverReplication -ScopeId 192.168.10.0 -Force` | 不适用（需自建脚本+rsync+trigger） |

> ⚠️ **重要提醒**：所有生产环境变更前，必须完成配置备份、变更窗口审批及回滚预案验证。禁止在业务高峰期执行作用域删除、租约清空等高风险操作。

---  
✅ **本文档由公司IT基础设施部网络组维护**  
🔗 相关链接：[DHCP最佳实践白皮书 v3.1](https://wiki.company.internal/network/dhcp-best-practices) | [自动化巡检脚本仓库](https://gitlab.company.internal/netops/dhcp-healthcheck)
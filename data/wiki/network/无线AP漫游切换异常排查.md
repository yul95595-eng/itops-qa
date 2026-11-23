# 无线AP漫游切换异常排查指南

> **适用对象**：公司IT运维工程师、网络管理员  
> **更新日期**：2024-06-15  
> **文档版本**：v2.3  

---

## 一、现象描述

用户在移动过程中（如办公区走廊、楼层间穿行），出现以下一种或多种现象：
- Wi-Fi连接中断超过3秒，需手动重连；
- 终端IP地址变更（DHCP租约重建）；
- 网络延迟突增（>200ms）或丢包率骤升（>15%）；
- 终端始终“粘滞”在弱信号AP（RSSI < -75dBm），未触发切换；
- iOS/Android设备显示“正在连接…”但无法完成漫游。

---

## 二、关键排查维度与验证步骤

### 2.1 基础配置一致性检查

确保所有参与漫游的AP属于同一SSID、安全策略（WPA2/WPA3）、VLAN及802.11r/k/v启用状态。常见不一致项如下：

| 配置项 | 正确要求 | 常见错误示例 | 检查命令/路径 |
|--------|----------|----------------|----------------|
| SSID广播 | 必须完全一致（含大小写、空格） | `Corp-WiFi` vs `corp-wifi` | `show ap ssid <ap-name>`（Aruba/Cisco） |
| 加密模式 | 同一SSID下所有AP必须相同 | AP1: WPA2-AES；AP2: WPA2-TKIP | `show run | include security` |
| 802.11r（Fast BSS Transition） | **必须启用**（尤其对iOS/macOS） | 仅在部分AP开启，或未配置Mobility Domain | `show wireless mobility`（Cisco WLC） |
| 802.11k（Neighbor Report） | 推荐启用，提升候选AP发现效率 | 未启用 → 终端盲目扫描耗时增加 | `show ap dot11 5ghz neighbor-report` |
| 802.11v（BSS Transition Management） | 推荐启用，支持AP引导切换 | 关闭 → 依赖终端自主决策，可靠性低 | `show ap dot11 24ghz bss-transition` |

> ⚠️ **注意**：若使用第三方AC（如UniFi、TP-Link Omada），需确认控制器固件版本 ≥ 推荐最低版本（如Omada v5.12+ 支持完整802.11r/k/v协同）。

---

### 2.2 射频环境与覆盖规划核查

#### ✅ 执行步骤（按序操作）：

1. **确认重叠覆盖区（Overlap Zone）设计合规**  
   - 2.4GHz频段：相邻AP信号交叠区RSSI应 ≥ -67dBm（理想-65dBm）  
   - 5GHz频段：交叠区RSSI应 ≥ -65dBm（因穿透衰减大，需更紧密部署）  
   - 使用热图工具（Ekahau Site Survey / NetSpot）验证，**禁止存在“覆盖空洞”或“孤岛AP”**

2. **检查同频干扰与信道复用**  
   - 2.4GHz：仅使用信道1/6/11（非重叠），禁用自动信道（Auto Channel）——易导致相邻AP同信道冲突  
   - 5GHz：启用DFS信道需谨慎（雷达检测导致AP闪退），建议固定使用UNII-1（36–48）或UNII-3（149–165）信道组  
   - 运行频谱分析：`show ap spectrum`（Aruba）或 `monitor ap channel-utilization`（Cisco）

3. **验证AP功率均衡性**  
   - 同区域AP发射功率差异 ≤ 3dBm（如AP1: 15dBm，AP2: 18dBm → 可能引发不对称漫游）  
   - 命令示例（Cisco）：  
     ```bash
     show ap config general <ap-name> | include "Tx Power"
     ```

---

### 2.3 终端侧兼容性验证

| 终端类型 | 关键兼容要求 | 排查方法 |
|----------|----------------|------------|
| **iOS（15+）** | 必须开启「Wi-Fi助理」→ 实际为802.11k/v/r协商开关 | 设置 > Wi-Fi > 右侧ⓘ > 查看「已启用快速漫游」状态 |
| **Android（12+）** | 需厂商支持（Samsung/Google Pixel默认OK；部分国产ROM阉割802.11r） | `adb shell dumpsys wifi \| grep -i "ft\|bss\|neighbor"` |
| **Windows 10/11** | 驱动需支持WPA3-Enterprise & 802.11r（Intel AX200/AX210驱动 ≥ v22.120） | 设备管理器 > 网络适配器 > 属性 > 高级 > 查看「Fast Roaming」选项 |
| **macOS（Ventura+）** | 依赖AirPort固件更新，需系统升级至最新补丁 | `system_profiler SPNetworkDataType \| grep -A5 "Wi-Fi"` |

> 🔍 **快速终端诊断命令**（Linux/macOS）：  
> ```bash
> # 查看当前关联AP及漫游事件日志
> iw dev wlan0 link  
> journalctl -u wpa_supplicant -n 50 --no-pager \| grep -i "roam\|bss\|ft"
> ```

---

## 三、典型错误码与处置建议

| 错误现象 | 系统日志关键词 | 根本原因 | 解决方案 |
|----------|----------------|------------|-------------|
| `FT Authentication failed` | `wpa_supplicant: FT: Authentication with xx:xx:xx failed` | AC未配置统一Mobility Domain或密钥不匹配 | 在AC全局配置中执行 `wireless mobility domain <domain-id> key <shared-key>` |
| `No suitable AP found in neighbor report` | `802.11k: No neighbors reported` | AP未启用802.11k，或终端不支持/未请求Neighbor Report | 检查AP射频模板中 `neighbor-report enable`，并确认终端Wi-Fi高级设置中开启「网络优化」 |
| `DHCP timeout after roam` | `dhcpcd[xxx]: timed out` | 新AP所在VLAN未正确透传DHCP Relay，或DHCP服务器响应延迟 | 在交换机侧验证 `show ip dhcp relay statistics`，检查Option 82插入是否被过滤 |
| `Roam triggered but stuck on old AP` | `wpa_supplicant: CTRL-EVENT-CONNECTED`（无后续EVENT-ROAMED） | 终端RSSI阈值过于保守（如-70dBm才触发），而当前AP仍维持-72dBm弱连接 | 调整终端注册表/配置文件（Android需Root，iOS不可调，依赖厂商策略） |

---

## 四、附录：推荐工具链

| 工具 | 用途 | 获取方式 |
|------|------|-----------|
| **Wireshark + AirPcap/NIC** | 抓取802.11管理帧（Beacon/Probe Request/Response/Reassociation） | https://www.wireshark.org |
| **Acrylic WiFi Home（Windows）** | 可视化扫描周边AP信道、RSSI、BSSID及802.11r/k/v支持标识 | https://www.acrylicwifi.com |
| **Apple Wireless Diagnostics（macOS）** | 原生诊断漫游延迟、BSSID切换时间戳 | `⌥+点击菜单栏Wi-Fi图标 → Open Wireless Diagnostics` |
| **Cisco Prime Infrastructure / Aruba Central** | 集中查看漫游轨迹、失败次数、AP负载热力图 | 企业级网管平台内置模块 |

---  
✅ **完成排查后，请同步更新《公司无线网络漫游基线配置模板》并归档本次案例至ITSM知识库（KB-ID: NET-WIFI-ROAM-202406）**
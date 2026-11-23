# 网络交换机端口VLAN配置规范

> **适用范围**：公司所有接入层及汇聚层可网管交换机（含 Cisco、H3C、华为、锐捷主流型号）  
> **生效日期**：2024年10月1日  
> **版本号**：V2.3  
> **责任部门**：IT基础设施运维部  

---

## 一、基本原则

### 1.1 VLAN规划总则
- 全公司采用**三层VLAN编号体系**：`VLAN XXX`，其中：
  - `100–199`：用户接入VLAN（按办公区域/部门划分）
  - `200–299`：服务器/核心业务VLAN（含数据库、应用服务、监控等）
  - `300–399`：管理VLAN（含带外管理、SNMP、Syslog）
  - `400–499`：语音/VoIP VLAN（启用QoS标记）
  - `900–999`：保留VLAN（测试、隔离、临时用途）
- 所有VLAN必须在核心交换机上**全局创建并命名**，禁止仅在接入交换机本地定义。
- VLAN名称格式统一为：`<用途>_<归属部门/区域>_<功能描述>`，例如：`USER_HR_BJ_OA`、`MGT_CORE_SWITCH_MGMT`。

### 1.2 端口模式分类与使用场景

| 端口模式 | 描述 | 典型应用场景 | 是否允许Trunk协商（DTP） |
|----------|------|----------------|---------------------------|
| `access` | 单VLAN接入，PVID固定，不打Tag | PC、打印机、IP电话（数据口） | ❌ 禁用（静态配置） |
| `trunk` | 多VLAN透传，Native VLAN不打Tag | 交换机级联、交换机-防火墙、交换机-服务器（支持802.1Q） | ❌ 禁用（静态Trunk） |
| `hybrid`（华为/H3C） | 灵活控制Tag/Untag，支持多Native VLAN | 特殊设备对接（如部分NAS、IoT网关） | ❌ 禁用（显式配置） |
| `dot1q-tunnel`（QinQ） | 仅用于专线接入边缘，外层VLAN由运营商分配 | 分支机构MPLS/以太专线接入 | ⚠️ 仅限广域网接入模块启用 |

> ✅ **强制要求**：所有`trunk`端口必须显式配置`allowed vlan`白名单，禁止使用`all`或`except`模糊匹配。

---

## 二、标准配置流程（以Cisco IOS为例）

### 2.1 配置前检查清单
1. 确认目标交换机已同步NTP时间（误差 ≤ 5s），确保日志可追溯；
2. 核对全局VLAN数据库是否已存在目标VLAN（`show vlan brief`）；
3. 检查端口当前状态与已有配置（`show running-config interface Gi1/0/X`）；
4. 验证STP角色（避免误配导致环路），建议优先启用`rapid-pvst`；
5. 记录原始配置并备份至GitOps仓库（路径：`/network/configs/switches/<hostname>/pre-vlan-change.cfg`）。

### 2.2 标准配置步骤（Access端口）

```bash
# 步骤1：进入接口配置模式
Switch(config)# interface GigabitEthernet1/0/5

# 步骤2：关闭端口（避免配置过程中产生异常流量）
Switch(config-if)# shutdown

# 步骤3：设置为access模式并指定VLAN
Switch(config-if)# switchport mode access
Switch(config-if)# switchport access vlan 121

# 步骤4：启用PortFast（仅限接入端口，加速生成树收敛）
Switch(config-if)# spanning-tree portfast

# 步骤5：启用BPDU Guard（防非法交换机接入）
Switch(config-if)# spanning-tree bpduguard enable

# 步骤6：开启端口安全（可选，按安全策略启用）
Switch(config-if)# switchport port-security
Switch(config-if)# switchport port-security maximum 2
Switch(config-if)# switchport port-security violation restrict

# 步骤7：启用描述（强制填写，格式见1.1）
Switch(config-if)# description USER_IT_SH_ServerRoom_WinPC_01

# 步骤8：启用端口
Switch(config-if)# no shutdown
```

### 2.3 Trunk端口配置示例（交换机互联）

```bash
Switch(config)# interface TenGigabitEthernet1/1/1
Switch(config-if)# shutdown
Switch(config-if)# switchport mode trunk
Switch(config-if)# switchport trunk native vlan 301   # 管理VLAN，不打Tag
Switch(config-if)# switchport trunk allowed vlan 121,122,205,301,410  # 显式白名单
Switch(config-if)# switchport trunk encapsulation dot1q
Switch(config-if)# description TRUNK_TO_CORE_SW02_UPLINK_P1
Switch(config-if)# no shutdown
```

---

## 三、验证与故障排查

### 3.1 必检验证命令

| 检查项 | 命令 | 预期输出特征 |
|--------|------|----------------|
| VLAN是否存在且激活 | `show vlan id 121` | `Status: active`, `Ports:` 包含目标接口 |
| 接口VLAN归属 | `show interfaces Gi1/0/5 switchport` | `Access Mode VLAN: 121`, `Operational Mode: access` |
| Trunk允许VLAN | `show interfaces Te1/1/1 trunk` | `Allowed VLANs: 121,122,205,301,410` |
| MAC地址学习 | `show mac address-table interface Gi1/0/5` | 存在客户端MAC条目（非0） |
| STP端口角色 | `show spanning-tree interface Gi1/0/5` | `Role: Designated`, `State: forwarding` |

### 3.2 常见错误码与处理

| 错误现象 | 可能原因 | 解决方案 |
|----------|-----------|------------|
| `interface shows 'notconnect' despite cable OK` | PVID不匹配 / 对端未UP / Native VLAN不一致（Trunk） | 检查对端`switchport trunk native vlan`是否一致；确认物理层（光模块/线缆） |
| `VLAN not found in show vlan` | VLAN未在本设备创建，或`vlan.dat`未同步 | 执行`vlan 121` → `name USER_IT_SH_ServerRoom_WinPC_01`；跨堆叠需`copy running-config startup-config` |
| `port stuck in 'blocking' state` | STP检测到环路 / BPDU Guard触发shutdown | `show spanning-tree blockedports`；若为误报，检查物理拓扑；恢复端口：`errdisable recovery cause bpduguard` |
| `client gets APIPA (169.254.x.x)` | DHCP未到达 / VLAN未路由 / ACL拦截DHCP | 检查DHCP relay配置（`ip helper-address`）、三层接口子网配置、ACL中是否放行UDP 67/68 |

---

## 四、附录：合规性审计要点（季度巡检）

- [ ] 所有`access`端口均启用`portfast` + `bpduguard`  
- [ ] 所有`trunk`端口`allowed vlan`列表长度 ≤ 20，且无`all`/`except`关键字  
- [ ] `native vlan`在全网Trunk链路上保持一致（推荐统一为VLAN 301）  
- [ ] 无`switchport voice vlan`未配合QoS策略部署（VoIP VLAN必须配置`mls qos trust dscp`）  
- [ ] 所有配置变更已提交至CMDB，并关联变更单（Change ID格式：CHG-YYYYMMDD-XXXXX）  

> 📌 **注**：本规范每半年复审一次，重大网络架构调整后须立即更新。最新版文档托管于内部Confluence：[Network/VLAN-Standard-Guide](https://confluence.company.net/display/NET/VLAN+Configuration+Standard)
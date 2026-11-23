# 公司 WiFi 连接与设备绑定说明

> **适用对象**：全体员工（含外包及临时访客）  
> **生效日期**：2024年7月1日  
> **文档版本**：v2.3  
> **维护团队**：IT基础设施运维组  

---

## 一、连接要求与准入机制

公司WiFi采用 **802.1X EAP-TLS 认证 + MAC地址白名单双重准入控制**，所有终端设备需完成以下任一绑定方式后方可接入内网SSID：

- ✅ **员工设备**：绑定公司域账号（Active Directory）+ 注册设备MAC地址  
- ✅ **访客设备**：通过「访客自助注册平台」申请临时凭证（有效期≤24h，限1台设备）  
- ❌ 未绑定设备将被隔离至 `GUEST-VLAN-100`，仅允许访问互联网（不可访问OA、ERP、文件服务器等内部资源）

---

## 二、设备绑定操作流程（员工）

### 步骤 1：确认设备网络信息  
在终端执行以下命令获取物理MAC地址（注意：取**无线网卡**的MAC，非蓝牙或虚拟网卡）：

| 系统类型 | 命令示例 | 输出关键字段 |
|----------|-----------|----------------|
| Windows 10/11 | `getmac /v \| findstr "Wi-Fi"` | `Physical Address` 列值（格式如 `AA-BB-CC-DD-EE-FF`） |
| macOS Monterey+ | `networksetup -listallhardwareports \| grep -A2 "Wi-Fi"`<br>`ifconfig en0 \| grep ether` | `ether xx:xx:xx:xx:xx:xx` |
| Android（需ADB） | `adb shell cat /sys/class/net/wlan0/address` | 十六进制MAC（小写，冒号分隔） |
| iOS（受限） | 设置 → 通用 → 关于本机 → Wi-Fi 地址（仅显示，不可复制）→ 建议用Mac/PC辅助读取 |

> ⚠️ 注意：部分Android设备（如华为EMUI、小米MIUI）默认隐藏MAC地址，需开启「开发者选项」→「网络」→「启用MAC地址随机化」设为「关闭」后重试。

### 步骤 2：登录设备绑定平台  
访问：`https://bind.internal.company.net`（需先通过公司VPN或内网环境访问）  
使用AD账号登录 → 进入【设备管理】→ 【新增设备】：

1. 输入设备名称（建议格式：`张三-ThinkPad-X1-2024`）  
2. 粘贴上一步获取的MAC地址（自动校验格式与唯一性）  
3. 选择设备类型：`笔记本电脑` / `台式机` / `手机` / `平板`  
4. 点击【提交审核】→ 系统自动触发ITSM工单（SLA：2个工作小时内完成审批）

### 步骤 3：连接公司WiFi  
SSID名称：`COMPANY-CORP-SECURE`（隐藏广播，需手动添加）  
配置参数如下：

| 参数项 | 值 |
|--------|---|
| 安全类型 | WPA3-Enterprise（兼容WPA2-Enterprise） |
| EAP方法 | `TLS` |
| 阶段2认证 | `None` |
| CA证书 | 内置系统根证书（无需手动安装） |
| 用户证书 | 由公司PKI系统自动推送（首次连接时触发MDM策略下发） |
| 身份标识（Identity） | `user@company.local`（即AD登录名@域名） |

> 🔐 若提示证书错误，请检查系统时间是否准确（误差＞3分钟将导致TLS握手失败）；若提示“身份验证失败”，请确认AD密码未过期且账户未被禁用。

---

## 三、常见问题与错误码速查

| 错误现象 | 可能原因 | 解决方案 |
|----------|-----------|------------|
| 连接后立即断开，日志显示 `EAP-TLS handshake failed (code 4001)` | 设备未安装用户证书或证书已过期 | 登录 `https://pki.company.net` 下载最新证书并导入；或联系IT重发证书 |
| 显示“已连接，无互联网访问” | 设备MAC未通过白名单审核 | 查看绑定平台中设备状态是否为 `Approved`；若为 `Pending`，等待审批完成 |
| 无法打开绑定平台（`ERR_CONNECTION_TIMED_OUT`） | 当前网络不在公司内网或未接入VPN | 启动公司标准VPN客户端（Cisco AnyConnect v4.10+），连接至 `vpn.company.net` 后重试 |
| 绑定成功但WiFi仍提示“拒绝访问” | 无线驱动不支持WPA3或TLS协商异常 | Windows：更新Intel/WiFi6网卡驱动至最新版；macOS：重置网络设置（系统设置→网络→右下角…→还原网络设置） |

---

## 四、安全合规提醒

- 📵 手机/平板绑定后，**禁止开启热点共享公司WiFi**（违反《网络安全管理办法》第5.2条）  
- 💾 笔记本离司前须提交【设备解绑申请】，由IT清除证书与MAC白名单记录  
- 🛑 发现他人设备异常接入（如未知MAC频繁尝试认证），请立即上报 `security@company.net`

---  
✅ **附：快速验证绑定状态**  
打开浏览器访问 `https://status.internal.company.net/wifi-check` → 输入MAC地址 → 实时返回：`Bound & Active` / `Not Found` / `Expired`  

*本文档依据 ISO/IEC 27001:2022 Annex A.8.2 及公司《无线网络安全管理规范V3.1》编制。*
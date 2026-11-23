# VPN客户端安装与常见报错

> 本文档适用于公司统一部署的 **FortiClient SSL VPN 客户端（v7.4.x）**，适用于 Windows 10/11 及 macOS 12+ 系统。其他版本或第三方客户端（如 OpenConnect、Cisco AnyConnect）请参考对应厂商文档。

---

## 一、客户端安装流程

### 1. 下载安装包

- **Windows 用户**：访问内部软件分发平台 `https://software.company.internal/forticlient-win`，下载 `FortiClient_7.4.x_Win64.exe`  
- **macOS 用户**：访问 `https://software.company.internal/forticlient-mac`，下载 `FortiClient_7.4.x_Mac.pkg`  
- ✅ 验证文件完整性（SHA256 值见平台页面底部，安装前建议校验）

### 2. 安装步骤（Windows 示例）

1. 双击 `FortiClient_7.4.x_Win64.exe`，以**管理员身份运行**
2. 在安装向导中：
   - 勾选 **SSL VPN** 模块（必选）
   - 可选勾选 **Endpoint Protection**（仅需防病毒功能时启用）
   - 取消勾选 **FortiTray**（避免系统托盘冗余图标）
3. 完成安装后，**不要立即启动**，先执行下一步配置

### 3. 首次配置（关键步骤）

- 启动 FortiClient → 点击左下角 **“Configure VPN”**  
- 点击 **“+” 添加新连接**：
  - Connection Name：`Company-SSL-VPN`（建议统一命名）
  - Remote Gateway：`vpn.company.internal:443`（生产环境地址）
  - Authentication Method：`Certificate + Username/Password`
- 点击 **“Import Certificate”** → 选择已预置的 `company-vpn-root-ca.crt`（由IT服务台统一推送至 `C:\ProgramData\Fortinet\FortiClient\sslvpn\certs\`）
- 保存配置，关闭向导

> ⚠️ 注意：证书未导入将导致后续连接时出现 `ERR_CERT_AUTHORITY_INVALID` 类错误

---

## 二、常见报错及解决方案

| 错误现象 | 错误码 / 日志关键词 | 原因分析 | 解决方案 |
|----------|---------------------|----------|-----------|
| 连接后立即断开，状态栏显示“Connecting…”后消失 | `SSL handshake failed`, `ERR_SSL_VERSION_OR_CIPHER_MISMATCH` | 客户端与网关 TLS 协议版本不兼容（如网关已禁用 TLS 1.1） | 升级至 v7.4.3+；或在客户端高级设置中强制启用 TLS 1.2/1.3（路径：Settings → Advanced → SSL/TLS → Enable TLS 1.2 & TLS 1.3） |
| 提示“Authentication failed”且用户名/密码确认无误 | `Invalid certificate chain`, `CERT_HAS_EXPIRED` | 本地证书过期或根证书未正确信任 | ① 检查系统时间是否准确（±3分钟内）<br>② 删除旧证书：`C:\ProgramData\Fortinet\FortiClient\sslvpn\certs\*`，重新导入最新 CA 证书<br>③ 在 Windows 证书管理器中将 `company-vpn-root-ca.crt` 导入 **“受信任的根证书颁发机构”** |
| 连接成功但无法访问内网资源（如 `10.20.0.0/16`） | `Split tunneling disabled`, `No routes pushed` | 分支隧道（Split Tunnel）策略未下发或被客户端拦截 | ① 登录 VPN 后，在客户端界面点击右上角 ⚙️ → **Network Options** → 确认 **"Use default gateway on remote network"** 为 *Disabled*（即启用 Split Tunnel）<br>② 若仍无内网路由，执行命令行检查：<br>　　`route print \| findstr "10.20"`（Win）或 `netstat -nr \| grep 10.20`（macOS）→ 若无对应条目，联系网络组核查策略推送状态 |
| macOS 上安装 `.pkg` 后提示“已损坏，无法打开” | `“FortiClient” is damaged and can’t be opened.` | macOS Gatekeeper 阻止未签名/非 App Store 应用 | 打开「系统设置 → 隐私与安全性」→ 在“安全性”区域点击 **“仍要打开”**；或临时执行：<br>`sudo xattr -rd com.apple.quarantine /Applications/FortiClient.app` |

---

## 三、附：快速诊断命令

### Windows（管理员 PowerShell）
```powershell
# 1. 查看当前 VPN 路由
route print | findstr "10\.20\|172\.16\."

# 2. 检查证书信任状态
certutil -verify -urlfetch "C:\ProgramData\Fortinet\FortiClient\sslvpn\certs\company-vpn-root-ca.crt"

# 3. 清除 FortiClient 缓存（谨慎使用）
& "$env:ProgramFiles\Fortinet\FortiClient\FCConfigTool.exe" /reset
```

### macOS（终端）
```bash
# 查看活跃 SSLVPN 接口
ifconfig | grep -A 5 "utun[0-9]"

# 检查 DNS 是否被劫持（应指向 10.20.1.10）
scutil --dns | grep "nameserver"

# 重启网络服务（无需重启系统）
sudo killall -HUP mDNSResponder
```

---

📌 **技术支持入口**  
- 内部工单系统：`https://helpdesk.company.internal` → 选择「网络服务 > SSL VPN 支持」  
- 紧急问题：拨打 IT 服务热线 `Ext. 8080`（7×12 小时）  

>Last updated: 2024-06-18 | Version: 2.3 | Owner: Network Operations Team
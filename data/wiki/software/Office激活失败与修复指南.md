# Office 激活失败与修复指南

> 本文档适用于公司内部 Windows 环境下 Microsoft Office（2019 / 2021 / Microsoft 365 Apps）的激活异常排查与修复，涵盖常见错误码、诊断步骤及标准化修复流程。

---

## 一、常见激活失败错误码与含义

| 错误码 | 含义说明 | 典型触发场景 |
|--------|----------|--------------|
| `0xC004F074` | KMS 激活服务不可达或未响应 | KMS 服务器地址配置错误、网络不通、KMS 服务未启动 |
| `0x80070005` | 访问被拒绝（权限不足） | 以普通用户身份运行激活命令、UAC 阻止、组策略限制 |
| `0x8007232B` | DNS 查找失败（无法解析 KMS 主机名） | DNS 配置异常、KMS 域名拼写错误、内网 DNS 未同步 |
| `0xC004F017` | 产品密钥不匹配或版本不兼容 | 使用了零售版密钥激活批量授权版（VLSC）Office，或密钥已过期/耗尽 |
| `0x8007007B` | 路径非法（含中文/特殊字符） | Office 安装路径含非 ASCII 字符（如「程序文件（x86）」中的括号或全角符号） |

> ✅ **提示**：所有错误码均可通过 `cscript ospp.vbs /dstatus` 获取当前许可证状态及详细错误上下文。

---

## 二、标准诊断流程（管理员权限执行）

### 步骤 1：确认 Office 版本与安装类型
```powershell
# 在 PowerShell（管理员）中执行：
Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Office\ClickToRun\Configuration" -Name ProductReleaseId -ErrorAction SilentlyContinue | Select-Object ProductReleaseId
# 或检查安装源：
(Get-Item "C:\Program Files\Microsoft Office\root\Office16\ospp.vbs").VersionInfo.ProductVersion
```

### 步骤 2：检查当前许可证状态
```cmd
cd /d "%ProgramFiles%\Microsoft Office\Office16"
cscript ospp.vbs /dstatus
```
重点关注输出中的：
- `LICENSE NAME`（是否为 `Office 16, VOLUME_KMS_CLIENT channel`）
- `LICENSE STATUS`（应为 `LICENSED`）
- `REMAINING GRACE DAYS`（≤30 天需及时处理）

### 步骤 3：验证 KMS 连接性（若使用 KMS 激活）
```cmd
# 测试端口连通性（默认 KMS 端口 1688）
telnet kms.company.local 1688
# 若失败，检查：
#   - 本地防火墙是否放行 outbound 1688
#   - 是否启用代理（需在 WinHTTP 中排除 kms.company.local）
```

---

## 三、标准化修复方案

### 方案 A：重置并强制重激活（推荐首选）
> ✅ 适用场景：`0xC004F074`、`0x8007232B`、许可证状态异常

```cmd
# 1. 以管理员身份打开 CMD
cd /d "%ProgramFiles%\Microsoft Office\Office16"

# 2. 卸载当前密钥（清除残留）
cscript ospp.vbs /unpkey:XXXXX

# 3. 设置 KMS 服务器（按公司规范填写）
cscript ospp.vbs /sethst:kms.company.local
cscript ospp.vbs /setprt:1688

# 4. 安装 VLSC 提供的 KMS 客户端密钥（例：Office 2021 Pro Plus）
cscript ospp.vbs /inpkey:FXYTK-NJJ8C-GB6DW-3DYQT-6F7TH

# 5. 强制激活
cscript ospp.vbs /act
```

### 方案 B：清除缓存并重建许可证存储
> ✅ 适用场景：`0x80070005`、`0x8007007B`、反复激活失败

```powershell
# 执行前关闭所有 Office 应用（Win+R → taskkill /f /im winword.exe /im excel.exe /im powerpnt.exe）

# 清理许可证缓存目录
Remove-Item "$env:LOCALAPPDATA\Microsoft\Office\16.0\Registration" -Recurse -Force -ErrorAction Ignore
Remove-Item "$env:PROGRAMDATA\Microsoft\OfficeSoftwareProtectionPlatform\tokens.dat" -Force -ErrorAction Ignore

# 重启 Software Protection 服务
Restart-Service sppsvc -Force

# 重新运行 ospp.vbs /act
```

### 方案 C：切换至 AAD 登录激活（仅限 Microsoft 365 Apps）
> ✅ 适用场景：员工已加入公司 Azure AD，且分配了 E3/E5 订阅许可

```powershell
# 确保已登录公司账号（设置 → 账户 → 工作或学校账户）
# 后台自动激活触发条件：
#   - 注册表项存在：HKEY_CURRENT_USER\Software\Microsoft\Office\16.0\Common\Identity\EnableADAL = 1
#   - 组策略启用：Computer Config → Admin Templates → Microsoft Office 2016 → Account → "Enable identity for Office applications"
```

---

## 四、预防性建议

- ✅ **部署阶段**：统一使用 Office Deployment Tool (ODT) + 配置 XML 部署，避免手动安装导致通道（Channel）错配；
- ✅ **密钥管理**：KMS 客户端密钥严禁硬编码在脚本中，应通过 Intune/SCCM 变量注入；
- ✅ **DNS 保障**：内网 DNS 必须权威解析 `kms.company.local` 至 KMS 服务器 VIP，TTL ≤ 300 秒；
- ❌ **禁止操作**：禁用 `sppsvc` 服务、修改 `tokens.dat` 文件、使用第三方 KMS 激活工具（违反公司软件合规政策）。

---

📌 **附：快速验证命令**
```cmd
cscript ospp.vbs /dstatus | findstr -i "LICENSE STATUS\STATUS\REMAINING"
# 输出示例：LICENSE STATUS: LICENSED （✅ 成功）  
```

> 文档更新日期：2024-06-12｜维护团队：IT 基础设施部·终端服务组
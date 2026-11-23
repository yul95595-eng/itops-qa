# 新员工入职 IT 账号开通时间表  

> **适用对象**：IT 运维团队、HR 入职专员、部门管理员  
> **更新日期**：2024年10月15日  
> **版本号**：v2.3  

---

## 一、总体原则  

- 所有 IT 账号（含域账号、企业邮箱、OA系统、VPN、代码仓库等）须在**员工正式入职日（D-Day）前完成开通**，确保首日可立即办公。  
- 开通流程以 HR 提交的《入职信息确认单》为唯一触发依据，IT 部门不接受口头或非标准渠道申请。  
- 账号开通时效按「优先级分级」执行，核心系统保障 SLA，非核心系统允许弹性延迟（见下表）。  

---

## 二、各系统账号开通时效要求（自收到有效入职单起计时）

| 系统类型       | 账号名称             | 开通目标时效 | SLA 容忍上限 | 依赖条件                     | 常见失败原因与错误码示例         |
|----------------|----------------------|--------------|--------------|------------------------------|----------------------------------|
| **核心系统**   | Active Directory 域账号 | ≤ 2 小时     | 4 小时       | HR 已同步组织架构与职级数据   | `ERR_AD003`（OU路径不存在）、`ERR_AD007`（重复SamAccountName） |
|                | 企业邮箱（Exchange Online） | ≤ 3 小时     | 6 小时       | 域账号已创建且启用            | `ERR_EXO112`（邮箱策略未绑定）、`ERR_EXO205`（别名冲突）      |
|                | 单点登录主账号（SSO）    | ≤ 4 小时     | 8 小时       | 邮箱已激活 + SSO IDP配置就绪  | `ERR_SSO409`（IDP中用户已存在但状态异常）                    |
| **业务系统**   | OA 办公平台           | ≤ 1 个工作日 | 2 个工作日   | 部门审批流已配置              | `ERR_OA5001`（岗位编码映射缺失）                              |
|                | 代码仓库（GitLab/Bitbucket） | ≤ 1 个工作日 | 2 个工作日   | 所属项目组权限模板已预设      | `ERR_GIT403`（组权限模板未启用）                              |
| **辅助系统**   | VPN（FortiGate / Cisco AnyConnect） | ≤ 1 个工作日 | 3 个工作日   | 安全组策略已下发至AD          | `ERR_VPN882`（证书签发失败）、`ERR_VPN901`（IPSec隧道未授权） |
|                | 云盘（OneDrive / 腾讯微云企业版） | 自动同步（≤5分钟） | —            | 邮箱开通即触发同步策略        | 无需人工干预；若超时检查 `SyncEngine` 日志中的 `Error 0x80070005` |

> ✅ **注**：所有时效均指**工作日（周一至周五 09:00–18:00）内**，不含法定节假日及系统维护窗口期（每月第1个周三 02:00–04:00）。

---

## 三、标准开通流程（IT 运维执行步骤）

### 1. 接收与校验（T+0 分钟）
- 检查 HR 提交的《入职信息确认单》是否含必填字段：  
  `员工姓名、工号、部门/二级部门、岗位、入职日期、直属上级AD账号、邮箱前缀偏好（如需）`  
- 校验工号唯一性（对接HRIS系统实时查询）；若冲突，立即退回HR并标注 `ERR_HRIS001`。

### 2. 创建基础身份（T+0 ~ T+2 小时）
- 在 AD 中创建用户对象，设置：  
  - `sAMAccountName = 工号`（强制小写）  
  - `UserPrincipalName = 邮箱前缀@company.com`  
  - `memberOf`：自动加入 `CN=All-Employees,OU=Groups,DC=company,DC=com` 及对应部门安全组  
- 启用账户并设置初始密码（符合公司密码策略：长度≥10，含大小写字母+数字+符号，不包含姓名/工号）。

### 3. 邮箱与协同服务开通（T+2 ~ T+3 小时）
- 执行 Exchange Online PowerShell 命令：  
  ```powershell
  Enable-Mailbox -Identity "工号" -Alias "邮箱前缀" -PrimarySmtpAddress "邮箱前缀@company.com"
  Set-Mailbox -Identity "工号" -Type Regular -ProhibitSendQuota 2GB -IssueWarningQuota 1.8GB
  ```
- 绑定默认邮件策略（`Company-Default-Retention-Policy`）与防病毒/反垃圾策略。

### 4. SSO 与业务系统对接（T+3 ~ T+4 小时）
- 调用 Identity Provider（Okta/Azure AD）API 创建用户，并分配预定义应用组（如 `App-OA-Readers`, `App-GitLab-Developers`）。  
- 触发 SCIM 同步至下游系统（OA/GitLab），验证返回 HTTP 201 或 200。

### 5. 通知与闭环（T+4 小时内）
- 自动向新员工发送欢迎邮件（含账号信息、首次登录指引、密码重置链接、IT支持入口）；  
- 同步抄送直属上级及IT服务台（ServiceNow Incident #INC-XXXXX）；  
- 在 ITSM 系统中标记该工单为「已完成」，关闭流程。

---

## 四、例外处理机制

- **加急需求（入职日 < 24 小时）**：启动绿色通道，由值班工程师直连 AD/Exchange 管理员手动执行，需在 ServiceNow 中备注 `PRIORITY-URGENT` 并附HR书面确认截图。  
- **信息不全被退回**：HR 补单后重新计时，原工单作废；累计3次无效提交，暂停该HR专员自助提单权限1周。  
- **系统级故障导致超时**：立即启动应急预案（启用备用AD森林/本地邮箱代理），并在15分钟内通报IT经理与HRBP。

---

## 五、附录：常用工具与链接

- 📌 [AD 用户批量创建模板（Excel）](https://wiki.company.com/it/ad-template)  
- 📌 [Exchange Online 权限速查表](https://wiki.company.com/it/exo-perm)  
- 📌 [SSO 应用组映射关系图谱](https://wiki.company.com/it/sso-mapping)  
- 📌 IT 服务台紧急通道：`helpdesk@company.com` / 分机 8080（7×12 支持）  

---  
✅ **本流程已通过 ISO/IEC 27001 信息安全管理审核（证书编号：ISMS-2024-CN-0887）**
# 单点登录（SSO）配置与常见问题

> **适用对象**：IT 运维人员、系统管理员、应用集成工程师  
> **更新时间**：2024年6月  
> **知识域**：账号与邮箱  

---

## 一、SSO 基本概念

单点登录（Single Sign-On, SSO）是一种身份验证机制，允许用户使用一组凭据（如公司统一账号）登录多个相互信任的应用系统，无需重复认证。公司当前主要采用 **SAML 2.0** 协议实现企业级 SSO，部分新应用支持 **OIDC（OpenID Connect）**。

---

## 二、典型 SSO 配置流程（以 SAML 为例）

### 1. 前置条件
- 已开通公司统一身份认证平台（IdP）管理权限（角色：`IdP Admin`）
- 目标应用（SP）支持 SAML 2.0，并提供元数据文件（`metadata.xml`）或手动配置参数（ACS URL、Entity ID、X.509 证书等）
- 网络策略已放行 IdP 与 SP 间的 HTTPS 通信（端口 443）

### 2. 配置步骤（IdP 侧 — 公司统一认证平台）

1. 登录 [公司统一身份认证管理后台](https://idp.company.internal)  
2. 进入 **「应用集成」→「新建 SAML 应用」**  
3. 填写基础信息：
   - 应用名称：`Jira Cloud（生产环境）`  
   - Entity ID：`https://company.atlassian.net`  
   - ACS URL：`https://company.atlassian.net/saml/authenticate`  
4. 上传 SP 提供的 `metadata.xml`（或手动输入证书与断言消费者服务地址）  
5. 配置属性映射（关键字段必须匹配）：

| SAML 属性名         | 公司 IdP 对应字段 | 说明                     |
|---------------------|-------------------|--------------------------|
| `email`             | `mail`            | 必填，用于账号绑定与通知 |
| `userName`          | `sAMAccountName`  | 用于显示名/登录别名      |
| `firstName`         | `givenName`       | 可选，用于用户资料同步   |
| `lastName`          | `sn`              | 可选                     |

6. 启用应用并下载 IdP 元数据（`idp-metadata.xml`），提供给 SP 方完成反向配置  
7. 在「用户组授权」中为对应业务组（如 `IT-Dev-Team`）分配该应用访问权限  

---

## 三、常见问题与排查指南

### ❗ 问题1：登录跳转后提示 “Invalid SAML Response” 或错误码 `ERR_SAML_VALIDATION_FAILED`

| 可能原因 | 排查方法 | 解决方案 |
|----------|-----------|-----------|
| SP 签名证书未在 IdP 中正确配置 | 检查 IdP 应用配置页中「SP 签名证书」是否粘贴完整（含 `-----BEGIN CERTIFICATE-----` 和 `-----END CERTIFICATE-----`） | 重新上传 PEM 格式证书，避免换行丢失或空格污染 |
| 时间不同步（Skew）超过 3 分钟 | 使用 `curl -v https://idp.company.internal` 查看响应头 `Date`，对比 SP 服务器 `date` 输出 | 同步 IdP 与 SP 服务器 NTP 时间（建议指向 `ntp.company.internal`） |
| SAML 断言中的 `Audience` 不匹配 | 抓取浏览器 SAML Response（通过开发者工具 → Network → Filter `sso` → 查看 POST payload），检查 `<Audience>` 值 | 在 IdP 配置中将「期望 Audience」设为 SP 的 Entity ID（如 `https://company.atlassian.net`） |

### ❗ 问题2：用户登录后显示 “User not found” 或重定向至注册页

- ✅ **确认项**：SP 是否启用「Just-in-Time (JIT) Provisioning」？  
- ✅ **根因**：IdP 返回的 `email` 属性为空、格式非法（如含多余空格），或 SP 数据库中已存在同邮箱但非 SSO 创建的账号  
- ✅ **操作**：  
  1. 在 IdP 日志中搜索对应用户 `login_id`，确认 `mail` 属性是否成功解析；  
  2. 检查 SP 管理后台 → 用户目录 → 查找该邮箱是否存在冲突账号；  
  3. 如需强制绑定，可在 SP 手动将本地账号关联至 SSO 邮箱（需 `System Admin` 权限）。

### ❗ 问题3：移动端 App 无法 SSO（如 Outlook iOS / Teams）

- ⚠️ 注意：部分移动客户端**不支持重定向型 SAML Web Flow**，仅支持 OIDC 或 Brokered Auth（如 Microsoft Authenticator）  
- ✅ 解决路径：  
  1. 优先为该 App 配置 OIDC 接入（参考《OIDC 应用接入规范 v2.1》）；  
  2. 若必须用 SAML，需在设备端安装公司 MDM 客户端并启用「SSO Extension」策略；  
  3. iOS 17+ 用户需在「设置 → 密码与账户 → 添加账户 → 公司 SSO」中手动配置凭证提供商。

---

## 四、附录：快速验证命令

```bash
# 1. 验证 IdP 元数据可访问性（需内网环境）
curl -I https://idp.company.internal/FederationMetadata/2007-06/FederationMetadata.xml

# 2. 检查 SAML 签名证书有效期（从元数据中提取 Base64 证书后解码）
echo "MIIF..." | base64 -d | openssl x509 -noout -dates

# 3. 测试 SP ACS 端点响应（模拟空 POST，预期返回 400 或 401）
curl -X POST https://company.atlassian.net/saml/authenticate -H "Content-Type: application/x-www-form-urlencoded"
```

---  
✅ **文档维护团队**：公司 Identity & Access Management (IAM) Team  
🔍 **相关链接**：  
- [SSO 配置检查清单（PDF）](https://wiki.company.internal/docs/sso-checklist)  
- [IdP 管理后台操作视频教程](https://video.company.internal/play?id=sso-setup-2024)  
- 紧急支持通道：`#iam-support`（Slack）｜ `iam-help@company.com`
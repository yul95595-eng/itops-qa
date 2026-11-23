# 代理服务器配置与 PAC 脚本说明

> **适用范围**：公司内部网络环境、远程办公终端（Windows/macOS/Linux）、企业级浏览器及系统级代理设置  
> **生效范围**：仅限公司内网访问策略、SaaS 应用分流、境外资源合规代理  
> **维护责任**：IT 基础设施组（Network & Security Team）

---

## 一、代理服务器基础配置

公司统一提供两类代理服务：

| 类型 | 地址与端口 | 认证方式 | 适用场景 | 备注 |
|------|-------------|-----------|------------|------|
| HTTP/HTTPS 代理 | `proxy.corp.local:8080` | NTLM / Kerberos（域账号自动认证） | 浏览器、Java 应用、Docker build 阶段 | 不支持 SOCKS 协议 |
| SOCKS5 代理 | `socks.corp.local:1080` | 用户名/密码（LDAP 统一凭证） | Git CLI、VS Code Remote-SSH、Telegram Desktop | 需显式配置，不参与 PAC 自动判断 |

⚠️ **注意**：
- 所有代理服务均启用 TLS 1.2+ 加密通道，禁用 SSLv3/TLS 1.0；
- 代理连接超时默认为 `30s`，失败后自动重试 2 次（间隔 2s）；
- 若返回 HTTP 状态码 `407 Proxy Authentication Required`，请确认域登录状态或重置 Kerberos ticket（`klist -l` / `kinit`）。

---

## 二、PAC（Proxy Auto-Config）脚本详解

### 2.1 脚本部署地址
- 主分发地址（HTTPS）：`https://policies.corp.local/proxy.pac`  
- 备用地址（HTTP，仅内网 DNS 可解析）：`http://wpad.corp.local/wpad.dat`  
- 本地缓存路径（推荐）：`file:///etc/proxy.pac`（Linux/macOS）、`file://C:\Windows\proxy.pac`（Windows）

### 2.2 核心逻辑说明（简化版 JS 片段）
```javascript
function FindProxyForURL(url, host) {
    // 1. 本地地址直连（含 localhost、127.0.0.1、RFC1918 私有网段）
    if (isPlainHostName(host) || 
        isInNet(host, "127.0.0.0", "255.0.0.0") ||
        isInNet(host, "10.0.0.0", "255.0.0.0") ||
        isInNet(host, "172.16.0.0", "255.240.0.0") ||
        isInNet(host, "192.168.0.0", "255.255.0.0")) {
        return "DIRECT";
    }

    // 2. 公司核心域名直连（如 OA、HR、ERP 等内部系统）
    if (shExpMatch(host, "*.corp.local") ||
        shExpMatch(host, "gitlab.internal") ||
        shExpMatch(host, "confluence.*.corp")) {
        return "DIRECT";
    }

    // 3. 特定 SaaS 服务走代理（含证书校验绕过白名单）
    if (shExpMatch(host, "*.salesforce.com") ||
        shExpMatch(host, "*.zoom.us") ||
        shExpMatch(host, "microsoft.*.com")) {
        return "PROXY proxy.corp.local:8080";
    }

    // 4. 默认策略：境外域名（非 CN 顶级域 + 非阿里云/腾讯云 CDN IP 段）走代理
    if (!isInNet(dnsResolve(host), "100.64.0.0", "255.192.0.0") &&  // CGNAT 排除
        !/\.cn$/.test(host) &&
        !/\.gov\.cn$/.test(host)) {
        return "PROXY proxy.corp.local:8080; SOCKS5 socks.corp.local:1080";
    }

    // 5. 其余流量直连
    return "DIRECT";
}
```

### 2.3 PAC 脚本验证方法

1. 将脚本保存为本地文件（如 `proxy.pac`）；
2. 在浏览器中打开 `about:config`（Firefox）或 `chrome://net-internals/#proxy`（Chrome），手动指定 PAC URL；
3. 使用在线工具验证语法：[https://pacparser.github.io](https://pacparser.github.io)（上传 `.pac` 文件）；
4. 终端调试命令（需安装 `pacparser`）：
   ```bash
   # Ubuntu/Debian
   sudo apt install pacparser
   pacparser --url "https://policies.corp.local/proxy.pac" --host "google.com" --url "https://google.com"
   # 输出示例：PROXY proxy.corp.local:8080
   ```

---

## 三、常见问题排查

| 现象 | 可能原因 | 解决方案 |
|------|-----------|------------|
| 浏览器提示“代理服务器拒绝连接” | PAC 脚本加载失败或 DNS 解析异常 | 检查 `curl -I https://policies.corp.local/proxy.pac` 是否返回 `200 OK`；确认本地 DNS 可解析 `policies.corp.local` |
| 内部系统（如 `oa.corp.local`）被错误代理 | PAC 中域名匹配规则未覆盖子域名 | 检查 `shExpMatch(host, "*.corp.local")` 是否存在；避免使用 `host == "corp.local"` 这类精确匹配 |
| Git clone 超时或 403 错误 | Git 未读取系统代理或忽略 PAC | 手动配置 Git 代理：<br>`git config --global http.proxy http://proxy.corp.local:8080`<br>`git config --global https.proxy https://proxy.corp.local:8080` |
| macOS 系统偏好设置中“自动代理配置”灰色不可选 | 网络接口被 MDM（如 Jamf）锁定 | 联系 IT 支持解除配置描述文件限制，或改用「手动代理」+ 指定 `proxy.corp.local:8080` |

---

## 四、附录：强制刷新与清除缓存

- **Windows**：  
  `netsh winhttp reset proxy`（重置 WinHTTP 全局代理）  
  `ipconfig /flushdns && net stop winmgmt && net start winmgmt`（清 DNS + WMI 缓存）

- **macOS**：  
  `sudo networksetup -setautoproxystate Wi-Fi off && sudo networksetup -setautoproxystate Wi-Fi on`

- **Chrome / Edge**：  
  地址栏输入 `chrome://settings/system` → 关闭「使用系统代理设置」→ 再开启，触发 PAC 重载

- **Firefox**：  
  `about:preferences#general` → 「网络设置」→ 点击「设置…」→ 切换任意选项后点「确定」强制重载 PAC

---  
✅ **最后更新时间**：2024-06-12  
📄 **版本号**：PAC-v3.2.1（基于 RFC 2138 / 2139 实现）
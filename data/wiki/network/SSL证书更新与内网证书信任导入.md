# SSL证书更新与内网证书信任导入

## 一、概述  
本指南适用于公司IT运维人员及系统管理员，用于规范**SSL证书到期前更新流程**及**内网自签名/私有CA证书在终端设备上的信任导入操作**。涵盖Web服务（Nginx/Apache）、负载均衡器（F5/Nginx Ingress）、以及Windows/macOS/Linux终端的信任配置。

> ⚠️ 注意：生产环境证书更新须在维护窗口期执行，并提前72小时完成灰度验证；内网证书未正确导入将导致HTTPS访问失败、API调用中断、VPN连接拒绝等故障。

---

## 二、SSL证书更新标准流程

### 2.1 证书到期监控与预警
- 使用Prometheus + Blackbox Exporter监控证书剩余有效期（阈值：≤30天触发P2告警，≤7天升级为P1）
- 自动化脚本每日扫描关键域名（`curl -v https://<domain> 2>&1 | grep "expire date"`）并推送至企业微信/钉钉运维群

### 2.2 更新操作步骤（以Nginx为例）

1. **获取新证书文件**  
   - 确认已从CA（如DigiCert、Let’s Encrypt或公司内部CA）获取以下文件：
     - `fullchain.pem`（证书链，含服务器证书+中间CA）
     - `privkey.pem`（私钥，权限必须为`600`，属主为`root`）

2. **备份旧证书**  
   ```bash
   sudo cp /etc/nginx/ssl/example.com.crt /etc/nginx/ssl/example.com.crt.bak.$(date +%Y%m%d)
   sudo cp /etc/nginx/ssl/example.com.key /etc/nginx/ssl/example.com.key.bak.$(date +%Y%m%d)
   ```

3. **替换证书并校验格式**  
   ```bash
   sudo cp fullchain.pem /etc/nginx/ssl/example.com.crt
   sudo cp privkey.pem /etc/nginx/ssl/example.com.key
   sudo chmod 600 /etc/nginx/ssl/example.com.key
   sudo nginx -t  # 必须返回 "syntax is ok" 且 "test is successful"
   ```

4. **热重载配置（不中断服务）**  
   ```bash
   sudo systemctl reload nginx
   # 或
   sudo nginx -s reload
   ```

5. **验证生效**  
   - 浏览器访问 `https://example.com` → 查看地址栏锁形图标 → 点击查看证书 → 检查“有效期至”字段  
   - 命令行验证：  
     ```bash
     openssl s_client -connect example.com:443 -servername example.com 2>/dev/null | openssl x509 -noout -dates
     ```

---

## 三、内网证书信任导入指南

公司内网服务（如GitLab、Jenkins、内部API网关）常使用**公司私有CA签发的SSL证书**。若终端未信任该CA根证书，将出现如下错误：

| 平台       | 典型错误现象                          | 错误码/提示示例                     |
|------------|-----------------------------------------|----------------------------------------|
| Windows    | IE/Edge访问白屏、Chrome显示NET::ERR_CERT_AUTHORITY_INVALID | `ERR_SSL_VERSION_OR_CIPHER_MISMATCH` |
| macOS      | Safari提示“此网站使用的证书不受信任”     | `CSSMERR_TP_NOT_TRUSTED`              |
| Linux CLI  | `curl` 报错                            | `SSL certificate problem: unable to get local issuer certificate` |
| Android/iOS  | App无法连接内网服务、VPN连接失败         | `Trust anchor for certification path not found` |

### 3.1 根证书获取路径
- 内部CA根证书统一存放于：`https://ca.internal.company/root-ca.crt`（需内网DNS解析）  
- 或通过IT服务门户下载（路径：`IT Portal > 安全 > 证书管理 > 下载公司根证书`）

### 3.2 各平台信任导入方法

#### ✅ Windows（域环境推荐组策略分发）
| 步骤 | 操作说明 |
|------|----------|
| 1 | 下载 `root-ca.crt` 文件（PEM格式） |
| 2 | 双击打开 → 选择“安装证书” → “本地计算机” → “受信任的根证书颁发机构” → 完成 |
| 3 | **域控批量部署（推荐）**：<br>– 将证书导入GPO：`计算机配置 → 策略 → Windows设置 → 安全设置 → 公钥策略 → 证书信任列表`<br>– 链接至对应OU，强制刷新：`gpupdate /force` |

#### ✅ macOS（12.0+）
```bash
# 终端执行（需管理员密码）
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain root-ca.crt

# 验证是否成功
security find-certificate -p /Library/Keychains/System.keychain | grep -A 1 "Company Internal CA"
```

#### ✅ Linux（Ubuntu/Debian/CentOS）
| 发行版 | 操作命令 |
|--------|----------|
| **Ubuntu/Debian** | ```sudo cp root-ca.crt /usr/local/share/ca-certificates/company-root-ca.crt && sudo update-ca-certificates``` |
| **CentOS/RHEL 8+** | ```sudo cp root-ca.crt /etc/pki/ca-trust/source/anchors/ && sudo update-ca-trust``` |
| **所有发行版（CLI工具临时信任）** | ```curl --cacert root-ca.crt https://gitlab.internal``` |

#### ✅ Chrome / Edge（基于Chromium）
- 依赖系统级证书存储，**无需单独配置**（Windows/macOS/Linux均复用系统根证书库）  
- 若仍报错，请检查：  
  - 是否启用了`chrome://flags/#unsafely-treat-insecure-origin-as-secure`（仅测试环境允许）  
  - 是否被企业策略禁用自定义根证书（检查`chrome://policy`）

---

## 四、常见问题排查

| 现象 | 可能原因 | 解决方案 |
|------|----------|----------|
| `nginx -t` 提示 `SSL_CTX_use_PrivateKey_file failed` | 私钥格式错误（如PKCS#8未转PKCS#1）或权限过高 | `openssl rsa -in privkey.pem -out privkey_pkcs1.pem && chmod 600 privkey_pkcs1.pem` |
| 浏览器仍显示旧证书 | Nginx缓存了SSL会话（session cache） | 在`http{}`块中添加 `ssl_session_cache off;` 后重载 |
| 内网服务HTTPS可访问，但curl失败 | Linux未更新CA证书库，或应用使用独立证书存储（如Java） | Java需执行：`sudo $JAVA_HOME/bin/keytool -import -trustcacerts -keystore $JAVA_HOME/jre/lib/security/cacerts -storepass changeit -alias company-ca -file root-ca.crt` |

---

## 五、附录：证书生命周期管理规范

| 阶段 | 责任人 | 关键动作 | SLA |
|------|--------|----------|-----|
| 申请 | 开发/运维 | 提交CSR至ITSM工单，注明用途、域名、有效期（≤1年） | ≤1工作日响应 |
| 签发 | PKI团队 | 生成证书，邮件通知申请人，同步至证书管理平台 | ≤2工作日 |
| 部署 | 运维 | 执行更新流程，记录变更时间、操作人、验证结果 | 维护窗口内完成 |
| 监控 | SRE | 接入证书监控大盘，自动告警+生成续期待办 | 提前30/7/1天三级告警 |

> 📌 **合规要求**：所有内网证书必须由公司授权CA签发，禁止使用自签名证书（`openssl req -x509`直出）上线生产环境。

---  
*文档版本：v2.3 | 最后更新：2024-06-15 | 维护团队：Infra-SRE*
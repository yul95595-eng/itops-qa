# PDF打印空白与编码问题排查指南

## 一、问题现象概述

在公司日常办公中，用户常遇到以下两类典型问题：
- **PDF文档打印后输出为纯空白页**（无文字、无图像、无边框）；
- **PDF中中文/特殊符号显示异常**（如方块、问号、乱码、字体缺失提示），或打印后出现字符错位、截断。

此类问题通常**非打印机硬件故障**，而是由 PDF 渲染路径中的编码处理、字体嵌入、驱动兼容性或应用层设置引发。

---

## 二、核心排查路径与优先级

### 1. 确认PDF文件本身完整性（一级排查）

| 检查项 | 方法 | 预期结果 | 常见异常 |
|--------|------|-----------|-----------|
| 是否可正常预览 | 使用 Adobe Acrobat Reader DC 或 SumatraPDF 打开并滚动查看 | 文字/图像清晰可读，无渲染错误提示 | 出现「缺少字体」「无法提取文本」警告；缩略图为空白 |
| 是否含嵌入字体 | Acrobat → 文件 → 属性 → 字体标签页 | 中文内容对应字体（如`SimSun`, `Noto Sans CJK SC`, `Adobe Heiti Std`）状态为「已嵌入」或「已子集化」 | 显示「未嵌入」或「仅轮廓」，且字体名含`#`前缀（如`#STSong`）→ 高风险 |
| 是否受保护/加密 | Acrobat → 文件 → 属性 → 安全标签页 | 安全方法为「无安全性」或「密码权限已解除」 | 显示「使用密码限制打印」或「禁止复制/打印」→ 需先解密 |

> ✅ **快速验证**：将PDF拖入 Chrome 浏览器地址栏打开 → 右键「打印」→ 选择「另存为 PDF」生成新副本。若新PDF可正常打印，则原文件存在渲染兼容性缺陷。

---

### 2. 打印驱动与渲染引擎（二级排查）

#### 常见驱动类型对比

| 驱动类型 | 适用场景 | 对PDF中文支持 | 典型问题 | 推荐操作 |
|----------|-----------|----------------|-------------|------------|
| **PCL6 / PCL XL** | 高速激光打印机（HP、Lexmark主流型号） | ⚠️ 差（依赖系统字体映射） | 中文转义失败、空白页、符号替换为`□` | 切换为 **PostScript** 或 **Microsoft Print to PDF** 中转 |
| **PostScript (PS)** | 支持PS的高端设备（Ricoh、Xerox、部分Canon） | ✅ 优（原生支持Type 1/TrueType嵌入） | PS解释器版本过旧（如Level 2不支持CID字体） | 升级打印机固件；在驱动属性中启用「TrueType字体下载为 outlines」 |
| **Microsoft XPS / Print to PDF** | 虚拟打印机（Windows内置） | ✅ 稳定（绕过设备驱动） | 生成PDF体积增大、部分矢量图转为位图 | 用作诊断中间件：`原PDF → Win10打印到XPS → 再打印XPS` |

#### 关键设置检查（以Windows为例）：
1. 控制面板 → 设备和打印机 → 右键目标打印机 → **打印首选项**  
2. 切换至「高级」或「图形」选项卡：
   - ✅ 勾选「打印 TrueType 字体为图形」（规避字体缺失）
   - ✅ 设置「图形渲染模式」为「高分辨率」或「TrueType 替换」
   - ❌ 禁用「后台打印」（Spool data format 若设为 `RAW` 可能跳过编码处理）

---

### 3. 应用程序层干扰（三级排查）

| 应用程序 | 高危行为 | 解决方案 |
|----------|-----------|------------|
| **Chrome / Edge** | 默认使用内置PDFium渲染器，对非标准CID字体支持弱 | 地址栏输入 `chrome://flags/#pdf-unseasonal-fonts` → 启用 → 重启；或导出为「另存为PDF」再打印 |
| **WPS Office** | 默认导出PDF时「不嵌入中文字体」且勾选「优化兼容性（降低文件大小）」 | WPS → 文件 → 另存为PDF → 点击「选项」→ ✅ 勾选「嵌入所有字体」+「保留原文档格式」 |
| **微信/QQ内置浏览器** | 直接调用系统WebView打印 → 字体沙箱隔离导致缺失 | 必须先下载PDF到本地，用Acrobat或Edge打开后打印 |

> 🔧 **注册表应急修复（仅限Windows管理员）**  
> 若多台设备复现「所有PDF打印空白」，检查：  
> `HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Print\Printers\[PrinterName]\CopyFiles`  
> 确保值为 `1`（启用字体缓存复制），否则手动添加 `DWORD` 值并设为 `1`。

---

## 三、标准化处理流程（按序执行）

1. **验证源文件**  
   - 用 Acrobat 打开 → `Ctrl+D` 查看字体嵌入状态 → 若关键中文字体未嵌入，返回源头重新导出（如Word中勾选「嵌入字体」）。

2. **切换渲染路径**  
   - 尝试通过「Microsoft Print to PDF」生成中间PDF → 再用该PDF打印 → 若成功，确认为原驱动兼容性问题。

3. **更新/重装驱动**  
   - 卸载当前驱动 → 从打印机官网下载**最新WHQL认证驱动**（非Windows Update自动推送版）→ 安装时选择「完整功能安装」。

4. **强制字体下载策略**  
   - 打印机管理界面（Web UI）或驱动高级设置中：  
     - 启用「Always download TrueType fonts」  
     - 设置「Font substitution table」将 `SimSun` 映射至打印机内置 `Courier`（临时兜底）

5. **终极验证命令（管理员CMD）**  
   ```bat
   print /d:"\\server\printer" "C:\test\chinese_ok.pdf"
   ```
   - 若命令行打印成功但GUI失败 → 定位为应用程序打印对话框编码层Bug（如旧版Foxit Reader）。

---

## 四、附录：常见错误代码速查

| 错误现象 | 打印机面板/日志代码 | 根本原因 | 应对措施 |
|----------|------------------------|------------|-------------|
| 空白页 + 控制面板显示 `Processing...` 后停顿 | `E030`（Ricoh） / `0x80070005`（Windows事件日志） | PDF含加密流或损坏的交叉引用表（xref） | 用 `qpdf --decrypt --optimize-images input.pdf output.pdf` 修复 |
| 中文显示为`■■■`但英文正常 | `Font Not Found: STSong`（PostScript日志） | 打印机未安装对应CID字体或映射表缺失 | 将TTF字体上传至打印机闪存（需管理员权限），或改用已预装字体（如`Arial Unicode MS`） |
| 打印几页后卡死，任务堆积 | `0x80070070`（磁盘空间不足） | Windows spooler 缓存PDF解压临时文件超限 | 清理 `C:\Windows\System32\spool\PRINTERS\` 并重启 `Print Spooler` 服务 |

---  
✅ **本文档持续更新于公司IT Wiki知识库（ID: HW-PRN-PDF-TROUBLESHOOTING）**  
>Last updated: 2024-06-12 | Version: 2.3
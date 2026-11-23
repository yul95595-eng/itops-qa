# 企业内部 IT 运维知识库助手（it_ops_qa）

## 项目简介
基于 RAG 的企业 IT 运维知识库问答助手，员工通过聊天入口提问（VPN 连不上、邮箱被锁、软件报错等），系统自动从内部知识资产中检索答案，未命中时转人工。

## 核心架构（三层检索）
1. **错误码精确匹配层**（核心亮点）：用户输入含固定格式错误码（如 ORA-12541、error 1045、0x80072EE7）时，通过正则提取 + 结构化表精确查询，秒回标准处置，零 LLM 调用、零幻觉。
2. **FAQ / BM25 层（三级缓存）**：高频问题走「Redis 热点缓存 → MySQL FAQ 精确匹配 → BM25 关键词匹配」三级递进（jieba 分词 + 双判据阈值：绝对分 `raw>=5.0` + 相对优势 `margin>=0.3`），命中直接返回，并内置缓存命中率统计。
3. **RAG 向量检索层（多策略路由）**：长尾问题先经意图识别（Direct / HyDE / Keyword），再走 Milvus 混合检索（BGE-M3 稠密 + 稀疏）+ bge-reranker 重排，最终由千问（qwen-plus）生成答案。

### 关键设计
- **多轮对话 / 上下文记忆**：前端维护最近 3 轮对话历史随请求发送，后端透传给 LLM，支持「那密码忘了呢」这类指代追问；切换知识域自动清空历史。
- **三级缓存架构**：Redis（热点答案，归一化 key 提升命中）→ MySQL FAQ（精确匹配）→ BM25（关键词匹配），内置 `CacheStats` 统计缓存命中率，量化「缓存降本」效果。
- **意图识别 + 多策略路由**：`intent_router.py` 用纯规则识别意图，口语化短问走 HyDE（假设文档扩写提升召回）、故障名词走 Keyword（稀疏检索强化）、标准陈述走 Direct。
- **相关性阈值兜底（严格离线）**：RAG 检索后按 bge-reranker 分数（0~1 概率）判断相关性，低于阈值（默认 `0.1`）直接走兜底，如实告知「知识库未收录、不联网搜索」，绝不编造。
- **RAGAS 标准评估**：`scripts/ragas_evaluate.py` 用 Context Precision / Answer Relevancy / Faithfulness 三个行业标准指标评估 RAG 答案质量。
- **提示词强约束**：LLM 只能基于检索上下文回答，严禁联网搜索、严禁使用外部信息、严禁臆测。

## 运行环境
- **容器模式（推荐）**：Docker Desktop + docker compose，一条命令起全栈，宿主机无需装 Python
- **开发模式**：conda 环境 `itOpsQA`（Python 3.10），`pip install -r requirements.txt`
- 模型文件：bge-m3 / bge-reranker-large 放到 `retrieval/models/`（约 3GB，不打进镜像，由 volume 挂载）
- 配置：API Key 与容器口令在 `.env`（不入库）；结构参数在 `config.ini`（入库模板）；本地真实口令在 `config.local.ini`（不入库，覆盖 config.ini）

## 目录结构
```
it_ops_qa/
├── Dockerfile         # ★ 应用镜像（依赖层/代码层分离, torch 装 CPU 版）
├── docker-compose.yml # ★ 全栈编排（应用 + MySQL + Redis + Milvus + etcd + MinIO）
├── 启动.bat           # ★ Windows 双击启动（环境自检 → 构建 → 起服务 → 开浏览器）
├── start.sh           # 开发模式启动（只把依赖放容器, 应用跑宿主机）
├── config.ini         # 配置模板（口令为占位符, 可公开）
├── config.local.ini   # 本地真实口令（不入库, 覆盖 config.ini）
├── base/              # 配置(config.py) + 日志(logger.py)
├── service/           # 服务层：三层路由与检索逻辑
│   ├── pipeline.py              # 三层路由总管线(错误码→FAQ→RAG, 含意图路由)
│   ├── error_code_matcher.py    # 错误码精确匹配器（核心亮点, 接MySQL）
│   ├── bm25_search.py           # FAQ 三级缓存 + BM25 匹配(含命中率统计)
│   ├── intent_router.py         # 意图识别器(Direct/HyDE/Keyword 多策略路由)
│   ├── llm_client.py            # 通义千问 LLM 客户端(生成答案/流式/HyDE)
│   ├── knowledge_ingest.py      # ★ 异步入库服务(单线程串行队列, 保护 CPU/内存)
│   └── excel_import.py          # ★ FAQ/错误码 Excel 模板下载/预览/导入
├── store/             # 存储层：数据库访问
│   ├── error_code_store.py      # MySQL 错误码入库+查询
│   ├── faq_store.py             # MySQL FAQ 入库+查询
│   ├── redis_client.py          # Redis 缓存客户端(支持归一化 key)
│   ├── doc_store.py             # ★ 上传知识文档登记表(状态机: pending→parsing→embedding→done/failed)
│   └── gap_store.py             # ★ 知识缺口登记表(答不上/不满意的提问聚合, 幂等累加)
├── data/              # 数据层
│   ├── seed/                    # 种子数据
│   │   ├── error_code_seed.py   # 内置错误码种子
│   │   ├── error_codes_large.py # 大规模真实错误码表(231条)
│   │   └── faq_seed.py          # 高频 FAQ 种子(120条)
│   ├── wiki/                    # 仿真企业 Wiki 文档（按知识域分目录）
│   ├── error_codes/             # 错误码表
│   └── uploads/                 # 企业上传文档落盘目录(供删除/重建)
├── retrieval/         # 检索层：向量检索
│   ├── document_processor.py    # Wiki 加载 + 父子块切分(docx/pdf/md/txt)
│   ├── vector_store.py          # Milvus 混合检索 + 重排序
│   ├── milvus_admin.py          # 按 doc_id 删/数向量(不加载大模型)
│   ├── edu_text_spliter/        # 中文递归切块器
│   └── models/                  # 本地模型(bge-m3 / bge-reranker-large, 项目自包含)
├── scripts/           # 脚本
│   ├── gen_wiki_docs.py         # 千问批量合成仿真 Wiki 文档
│   ├── ingest_all.py            # 一键入库(错误码+FAQ→MySQL, Wiki→Milvus)
│   ├── smoke_test.py            # ★ 冒烟测试(10 项验证整条链路通没通)
│   ├── demo.py                  # 现场演示脚本(7个案例展示三层路由)
│   ├── evaluate.py              # 题库自测(题目反向生成, 仅供参考)
│   ├── eval_quality.py          # ★ 独立测试集 + 检索消融 + 基线对照(可对外讲)
│   └── ragas_evaluate.py        # RAGAS 标准评估(CP/AR/Faithfulness)
├── data/eval/         # 评测集(独立测试集 40 题 + 检索评测集 32 题)
├── tests/             # 单元测试
│   └── test_pipeline_layers.py  # 三层路由 + 兜底共22个单测
├── app/               # 应用层(FastAPI 接口 + 前端)
│   ├── main.py                  # FastAPI 入口(/health + /chat 流式 + /api/error_code + 知识库文档管理 + / 页面)
│   └── static/index.html        # 多页面产品级前端(主页/分域问答/错误码速查/知识库管理)
├── docker-compose.yml  # ★ 依赖编排(Milvus+etcd+minio + MySQL + Redis)
├── start.sh            # ★ 一键启动(拉依赖→灌数据→起服务→冒烟)
└── logs/              # 运行时产物(评测报告/日志, 已 gitignore)
```

## 知识库自助上传（企业自有运维文档入库）

不同企业的故障场景不同，系统支持把企业自己整理的运维文档灌入知识库，无需改代码：

```bash
# 上传(md/txt/docx/pdf, 单文件 ≤ 20MB), 返回 doc_id, 后台异步解析+向量化
curl -F "file=@故障手册.docx" -F "category=network" http://127.0.0.1:8000/api/docs/upload

# 轮询入库进度(前端进度条数据源): pending → parsing → embedding → done/failed
curl http://127.0.0.1:8000/api/docs/{doc_id}

# 文档列表 / 重新入库(失败重试) / 删除(向量+登记+原文件三处一起清)
curl http://127.0.0.1:8000/api/docs
curl -X POST http://127.0.0.1:8000/api/docs/{doc_id}/reindex
curl -X DELETE http://127.0.0.1:8000/api/docs/{doc_id}
```

工程设计要点：

| 设计 | 原因 |
|---|---|
| 内容寻址 doc_id（文件 md5 前 16 位） | 同一文件重复上传得到同一 ID，天然幂等；查重直接拒绝，不浪费 CPU 重算向量 |
| 异步 + 单线程串行队列 | CPU 上 BGE-M3 向量化是分钟级，同步必超时；并发跑两份模型会打爆内存 |
| 状态机落库（每步写回 MySQL） | 进程重启不丢账，前端可轮询画进度条，失败可一键重试 |
| 删除三处一起清（Milvus + MySQL + 原文件） | 少清一处会出现"删了还能搜到"的幽灵数据 |

## FAQ / 错误码 Excel 批量导入

高频 FAQ 和错误码也支持企业用 Excel 批量导入（走 BM25 / 精确查表，不占向量算力）：

```bash
# 下载模板(FAQ 三列 / 错误码五列)
curl -o faq.xlsx "http://127.0.0.1:8000/api/import/template?kind=faq"
curl -o code.xlsx "http://127.0.0.1:8000/api/import/template?kind=error_code"

# 校验预览(只解析不落库, 返回逐行校验结果)
curl -F "file=@faq.xlsx" "http://127.0.0.1:8000/api/import/preview?kind=faq"

# 正式导入(落库 + 失效 BM25/Redis 缓存, 新数据立即可检索)
curl -F "file=@faq.xlsx" "http://127.0.0.1:8000/api/import/commit?kind=faq"
```

导入数据标记 `source='custom'`，与内置 `builtin` 数据区分。FAQ 导入后会自动失效 BM25 索引和 Redis 答案缓存，新问题立即生效。

前端已提供「知识库管理」页（导航栏「知识库管理」入口），支持拖拽上传、进度轮询、文档列表/删除、试问验证、Excel 模板下载与导入预览。

## 管理鉴权 + 答案反馈 + 知识盲区（自我进化闭环）

管理端写操作（上传/删除/重建/批量导入）与知识盲区查看需验证管理口令，防止误操作清空知识库：

```bash
# 所有写接口需在 Header 携带管理口令:  X-Admin-Token: <你的口令>
# 口令在 .env 的 ADMIN_TOKEN（容器模式）或 config.local.ini 的 admin_token（本地模式）配置
curl -X DELETE -H "X-Admin-Token: $ADMIN_TOKEN" http://127.0.0.1:8000/api/docs/{doc_id}
```

答案反馈 + 知识盲区，让知识库具备自我进化能力——员工问了但答不上的问题，就是下一批该补的知识清单：

```bash
# 用户对回答点"没用" -> 记为一条知识缺口
curl -X POST -H "Content-Type: application/json" \
  -d '{"question":"VPN 一直连接超时","useful":false,"layer":"rag"}' \
  http://127.0.0.1:8000/api/feedback

# 查看知识盲区(按被问次数倒序, 需口令)
curl -H "X-Admin-Token: $ADMIN_TOKEN" http://127.0.0.1:8000/api/gaps

# 补了对应知识后, 关闭一条缺口
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" http://127.0.0.1:8000/api/gaps/{query_hash}/resolve
```

三类缺口自动记录（触发点都在问答管线内，无需人工上报）：

| 缺口类型 | 触发时机 | 含义 |
|---|---|---|
| `fallback` | 三层路由全未命中，或 RAG 检索为空 | 知识里确实没有 |
| `rag_low` | RAG 检索到内容但 rerank 最高分低于阈值 | 有疑似内容但不相关 |
| `feedback` | 用户对答案点"没用" | 答案存在但用户不满意（答非所问/过时/错误） |

关键设计：缺口以 `query_hash`（问题归一化 md5）作幂等主键，同一问题被反复问只记一条、`hit_count` 累加——运营按被问次数排序，一眼看出最该优先补的知识。

## 快速开始

### 前置条件

| 依赖 | 用途 | 说明 |
|---|---|---|
| Docker Desktop | 跑全部服务 | Windows/macOS 装完需启动并等待 Engine running |
| 模型权重 | BGE-M3 + bge-reranker | 约 3GB，**不打进镜像**，需放到 `retrieval/models/` |
| `DASHSCOPE_API_KEY` | 调用通义千问 | 阿里云百炼控制台申请 |

**模型下载（只做一次，二选一）**

```bash
# 方式 A: ModelScope（国内快）
pip install modelscope
modelscope download --model BAAI/bge-m3 --local_dir retrieval/models/bge-m3
modelscope download --model BAAI/bge-reranker-large --local_dir retrieval/models/bge-reranker-large

# 方式 B: HuggingFace 镜像
HF_ENDPOINT=https://hf-mirror.com huggingface-cli download BAAI/bge-m3 --local-dir retrieval/models/bge-m3
HF_ENDPOINT=https://hf-mirror.com huggingface-cli download BAAI/bge-reranker-large --local-dir retrieval/models/bge-reranker-large
```

### 方式一：双击启动（Windows，非开发同学用）

1. 安装并启动 Docker Desktop
2. 把模型权重放到 `retrieval/models/`（见上）
3. 首次运行会生成 `.env`，填入 `DASHSCOPE_API_KEY` 后保存
4. **双击 `启动.bat`** —— 自动检查环境、构建镜像、拉起服务、打开浏览器

脚本会依次检查 Docker 是否安装/运行、模型是否就位、配置是否存在，任何一步不满足都会给出中文提示，不会一闪而过。

### 方式二：命令行启动（全栈容器化）

```bash
cp .env.example .env          # 填入 DASHSCOPE_API_KEY, 按需改口令
docker compose up -d --build  # 构建应用镜像 + 拉起 MySQL/Redis/Milvus/etcd/MinIO
# 浏览器打开 http://127.0.0.1:8000

# 首次需要灌入内置知识库（错误码 + FAQ + Wiki 向量化）
docker compose exec app python scripts/ingest_all.py

docker compose logs -f app    # 看应用日志
docker compose down           # 停止（数据卷保留）
docker compose down -v        # 停止并清空数据
```

> **构建耗时说明**：首次构建需下载约 1GB 依赖（其中 torch 单包 539MB）。
> 实测 450KB/s 带宽下约需 40 分钟，且大包容易断流；建议在网络良好时构建，
> 或在一台已构建好的机器上 `docker save itops-qa:latest | gzip > app.tar.gz` 再 `docker load` 导入。

### 方式三：开发模式（宿主机直跑，改代码即时生效）

```bash
# 前置: 已激活 conda 环境(itOpsQA, 已 pip install -r requirements.txt)
# 只把依赖服务放进容器，应用跑宿主机，改代码不用重建镜像

./start.sh                # 拉基础设施 → 灌数据 → 起服务 → 冒烟测试
./start.sh --skip-ingest  # 数据已灌过, 只起服务 + 冒烟
./start.sh --no-smoke     # 只起服务, 不跑冒烟
```

`start.sh` 内部做的事，拆开看就是下面「方式四」的四步。

### 方式四：手动分步（便于排查问题）

```bash
# 1. 拉起基础设施(Milvus + MySQL + Redis), 一个 compose 全起
docker compose up -d

# 2. 生成仿真 Wiki 文档（需先在 .env 配置 DASHSCOPE_API_KEY）
python scripts/gen_wiki_docs.py

# 3. 一键入库：错误码→MySQL + FAQ→MySQL + Wiki 切块→Milvus
python scripts/ingest_all.py

# 4. 启动 Web 服务（FastAPI 接口 + 前端聊天界面）
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# 浏览器访问 http://127.0.0.1:8000/ 即可对话
```

**冒烟测试（验证整条链路通没通）**

```bash
# 服务起来后跑一次, 10 项全绿说明"从提问到答案"整条链路已打通
python scripts/smoke_test.py
```

**其他常用命令**

```bash
# 现场演示（一条命令展示三层路由效果）
python scripts/demo.py

# 效果评测（三套口径，详见下方「效果评测」章节）
python scripts/eval_quality.py --all       # ★ 独立测试集+消融+基线（可对外讲）
python scripts/evaluate.py                 # 题库自测（仅供参考，题目反向生成）

# 单元测试
python -m unittest tests.test_pipeline_layers -v
```

## 效果评测

项目有三套评测口径，**别混用**：

| 脚本 | 口径 | 能否对外讲 |
|---|---|---|
| `evaluate.py` | 题库自测（题目从数据表反向生成） | ❌ 会被问穿 |
| `ragas_evaluate.py` | RAGAS 三指标（4 样例） | ⚠️ 样本太小 |
| **`eval_quality.py`** | **独立测试集 + 消融 + 基线对照** | ✅ **主打这个** |

### 独立测试集（`eval_quality.py --independent`）

40 题，措辞完全不照抄知识库，覆盖口语化/错别字/混合意图/越界拒答：

| 题型 | 结果 |
|---|---|
| 口语化 | 11/12（91.7%） |
| 错别字 | 7/8（87.5%） |
| 错误码 | 8/8（100%） |
| 混合意图 | 4/4（100%） |
| 越界（应拒答） | 8/8（100%，编造 0） |
| **总体** | **38/40 = 95%** |

### 检索消融（`eval_quality.py --ablation`）

| 配置 | Recall@2 | MRR | 延迟 |
|---|---|---|---|
| 生产（混合+reranker） | 100% | 0.953 | 3989 ms |
| 去掉 reranker | 96.9% | 0.938 | 179 ms |
| 去掉稀疏向量 | 100% | 0.953 | 4081 ms |

### 基线对照（`eval_quality.py --baseline`）

| 组别 | 越界题拒答 | 编造 |
|---|---|---|
| RAG 完整链路 | 8/8（100%） | 0 |
| 裸模型（无上下文） | 1/8（12.5%） | 7 |

**反幻觉增益 +87.5pp**。复现命令：`python scripts/eval_quality.py --baseline`。

**关键洞察**：口语化改写问法（如「密码忘了咋整」）FAQ 层关键词匹配会漏，但会落到 RAG 层由语义检索兜底——这正是三层路由设计的价值所在。

## RAGAS 标准评估
`scripts/ragas_evaluate.py` 用 RAGAS 三大标准指标评估 RAG 层答案质量（评分 LLM 用通义千问），结果输出到 `logs/ragas_report.md`：

| 指标 | 含义 | 分数 |
|---|---|---|
| Context Precision | 上下文精确度（检索质量） | 1.0000 |
| Answer Relevancy | 答案相关性（生成质量） | 0.8793 |
| Faithfulness | 忠实度（反幻觉） | 0.7725 |

## 单元测试
`tests/test_pipeline_layers.py` 用标准 `unittest` 覆盖三层路由核心逻辑（共 22 个用例，不依赖 pytest）：

- 错误码提取正则（数字码 / ORA 前缀 / 十六进制 0x 前缀）
- 错误码标准化与匹配
- BM25 分词 + 双判据阈值逻辑
- RAG 相关性阈值判断（含边界）
- 兜底话术

## 入库数据现状
- MySQL：`it_ops_qa.error_codes` 表 **231 条**错误码（MySQL/PostgreSQL/Oracle/SQL Server/Redis/Nginx/HTTP/Docker/K8s/Linux/Windows，联合唯一键 code+component）
- Milvus：`it_ops.it_ops_wiki` 集合 **1405 个子块**（来自 140 篇 Wiki，父子块结构，BGE-M3 稠密+稀疏向量）

## 数据规模扩展
- **错误码**：`data/seed/error_codes_large.py` 存放大规模真实错误码表（231 条），`error_code_seed.py` 的 `get_all_error_codes()` 合并内置种子 + 扩展表
- **Wiki**：`scripts/gen_wiki_docs.py` 的 TOPICS 扩充到每域 35 篇（4 域共 140 篇），用千问批量生成
- 重新入库命令：`python scripts/ingest_all.py`

## 知识域
- network：网络与 VPN
- account：账号与邮箱
- printer：打印机与硬件
- software：软件与报错

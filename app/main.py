# -*- coding: utf-8 -*-
# app/main.py
# FastAPI 接口层 —— 企业内部 IT 运维知识库助手
#
# 接口:
#   GET  /health   健康检查
#   POST /chat     问答接口(SSE 流式输出)
#   GET  /         前端聊天页面(静态文件)
import hashlib
import json
import os
import shutil
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request, UploadFile, File, Form, Header, HTTPException, Depends
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from base.config import config
from base.logger import logger
from service.pipeline import QAPipeline

app = FastAPI(
    title="企业内部 IT 运维知识库助手",
    description="三层检索路由: 错误码精确匹配 → FAQ/BM25 → RAG向量检索+LLM; 支持企业自助上传知识文档(md/txt/docx/pdf)异步入库",
    version="1.1.0",
)

# 全局管线单例(懒加载各层, 首次调用时初始化)
pipeline = QAPipeline()


class ChatRequest(BaseModel):
    question: str
    history: list = []  # 历史消息 [{"role": "user"/"assistant", "content": "..."}]


class FeedbackRequest(BaseModel):
    question: str
    useful: bool  # True=有用, False=没用
    layer: str = ""  # 答案所属层(可选)
    request_id: str = ""  # 问答请求id(可选), 用于关联审计日志还原当时答案


def _require_admin(x_admin_token: str = Header(default="")):
    """
    最简鉴权: 管理端写操作需在 Header 携带 X-Admin-Token 匹配口令。
    用常量时间比较(secrets.compare_digest)防时序侧信道。
    """
    import secrets
    if not secrets.compare_digest(x_admin_token, config.ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="管理口令缺失或错误")


@app.get("/health")
def health():
    """健康检查"""
    return JSONResponse({
        "status": "ok",
        "service": "it_ops_qa",
        "llm_model": config.LLM_MODEL,
    })


@app.post("/chat")
async def chat(req: ChatRequest):
    """问答接口, SSE 流式返回。事件字段: layer + content"""
    question = req.question.strip()
    if not question:
        return JSONResponse({"error": "question 不能为空"}, status_code=400)

    # 本次问答的追踪 id: 随响应回传, 用户反馈问题时凭此 id 回查审计日志
    request_id = uuid.uuid4().hex[:16]

    def event_stream():
        # 审计埋点(统一在路由层做, 不侵入管线的各条 return 分支):
        # 边转发边收集本次问答的关键信息, 流结束后统一落一条审计日志。
        layer = ""
        strategy = ""
        top_score = None
        top_filenames = None
        answer_parts = []
        try:
            for evt_layer, content in pipeline.answer_stream(
                    question, history=req.history, request_id=request_id):
                if evt_layer == "rag_meta":
                    # 元信息事件: 只解析用于审计, 解析失败也不影响答案输出
                    layer = layer or "rag"
                    try:
                        meta = json.loads(content)
                        strategy = meta.get("strategy") or ""
                        top_score = meta.get("top_score")
                        top_filenames = meta.get("top_filenames")
                    except Exception as e:
                        logger.warning(f"解析 rag_meta 失败(不影响回答): {e}")
                else:
                    layer = evt_layer
                    answer_parts.append(content or "")
                yield _sse(evt_layer, content, request_id=request_id)
        except Exception as e:
            logger.error(f"流式问答异常: {e}")
            layer = layer or "fallback"
            yield _sse("fallback", "系统异常, 请稍后重试或联系 IT 服务台。", request_id=request_id)
        finally:
            # 命中文档的版本号: 追责时用于确认"当时依据的是哪一版"
            doc_version = None
            if top_filenames:
                try:
                    rec = _get_doc_store().find_latest_by_filename(
                        top_filenames[0] if isinstance(top_filenames, list) else top_filenames)
                    if rec:
                        doc_version = rec.get("version")
                except Exception as e:
                    logger.warning(f"查询命中文档版本失败(不影响问答): {e}")
            # 写审计日志: log_qa 内部已 try/except, 失败只 warning, 绝不影响问答
            from store.qa_log import log_qa
            log_qa(
                request_id=request_id,
                query=question,
                layer=layer or "unknown",
                strategy=strategy or None,
                top_score=top_score,
                top_filenames=top_filenames,
                answer="".join(answer_parts),
                doc_version=doc_version,
            )
            yield "event: done\ndata: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Request-Id": request_id,
        },
    )


def _sse(layer: str, content: str, request_id: str = None) -> str:
    """构造一条 SSE 事件: 用 data 字段携带 JSON(layer + content), 并带上 request_id 供前端追溯"""
    payload = {"layer": layer, "content": content}
    if request_id:
        payload["request_id"] = request_id
    return f"event: message\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


# 错误码速查: 懒加载 MySQL store 单例
_error_code_store = None


def _get_error_code_store():
    global _error_code_store
    if _error_code_store is None:
        from store.error_code_store import ErrorCodeStore
        _error_code_store = ErrorCodeStore()
    return _error_code_store


@app.get("/api/error_code")
def query_error_code(code: str):
    """错误码速查接口: 直接查 MySQL 结构化表, 零 LLM"""
    code = code.strip()
    if not code:
        return JSONResponse({"error": "code 不能为空"}, status_code=400)
    try:
        store = _get_error_code_store()
        hit = store.query(code)
        if hit:
            return JSONResponse({"found": True, "data": hit})
        return JSONResponse({"found": False})
    except Exception as e:
        logger.error(f"错误码速查异常: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ==================== 知识库文档管理(企业自助上传) ====================
# 单例(懒加载): 文档登记表 + 异步入库服务
_doc_store = None


def _get_doc_store():
    global _doc_store
    if _doc_store is None:
        from store.doc_store import KnowledgeDocStore
        _doc_store = KnowledgeDocStore()
        _doc_store.create_table()
    return _doc_store


def _get_ingest_service():
    from service.knowledge_ingest import get_ingest_service
    return get_ingest_service()


def _safe_filename(filename):
    """只保留文件名本身(去掉路径), 并防御性剔除危险字符"""
    name = os.path.basename(filename or "")
    return "".join(c for c in name if c not in r'<>:"/\|?*').strip() or "unnamed"


@app.post("/api/docs/upload", dependencies=[Depends(_require_admin)])
async def upload_doc(
    file: UploadFile = File(...),
    category: str = Form(config.DEFAULT_DOC_CATEGORY),
):
    """
    上传知识文档(md/txt/docx/pdf), 登记后异步入库。
    立即返回 doc_id 与 pending 状态; 前端轮询 GET /api/docs/{doc_id} 看进度。
    """
    filename = _safe_filename(file.filename)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in config.ALLOWED_DOC_EXTS:
        return JSONResponse(
            {"error": f"不支持的文件类型 {ext}, 仅支持 {', '.join(config.ALLOWED_DOC_EXTS)}"},
            status_code=400,
        )

    try:
        # 读文件(带大小上限保护, 超限即中止, 不把 20MB 以上内容读进内存)
        content = bytearray()
        while True:
            block = await file.read(1 << 20)  # 1MB
            if not block:
                break
            content.extend(block)
            if len(content) > config.MAX_UPLOAD_SIZE:
                return JSONResponse(
                    {"error": f"文件超过大小上限 {config.MAX_UPLOAD_SIZE // (1024 * 1024)}MB"},
                    status_code=413,
                )
    finally:
        await file.close()

    if not content:
        return JSONResponse({"error": "文件内容为空"}, status_code=400)

    file_hash = hashlib.md5(bytes(content)).hexdigest()
    doc_id = file_hash[:16]  # 内容寻址: 同一文件重复上传得到同一 doc_id

    # 查重: 同内容文件已登记过 -> 拒绝(省 CPU 不重算向量)
    store = _get_doc_store()
    existed = store.find_by_hash(file_hash)
    if existed:
        return JSONResponse(
            {
                "error": "相同内容的文件已上传过",
                "existing_doc_id": existed["doc_id"],
                "existing_status": existed["status"],
                "existing_filename": existed["filename"],
            },
            status_code=409,
        )

    # 落盘: uploads/ 目录留存原文件(供重新入库/审计)
    os.makedirs(config.UPLOAD_DIR, exist_ok=True)
    save_path = os.path.join(config.UPLOAD_DIR, f"{doc_id}{ext}")
    with open(save_path, "wb") as f:
        f.write(bytes(content))

    # 登记(状态 pending) + 提交异步入库任务
    category = (category or "").strip()[:32] or config.DEFAULT_DOC_CATEGORY
    # 同名文档版本处理: 旧版本下架(删旧向量 + 标记 is_latest=0), 本次作为新版本入库。
    # 不做这一步会有"幽灵旧版": 内容变 -> doc_id 变 -> 旧向量仍在库里, 与新版一起被检索到。
    version = 1
    superseded = None
    previous = store.find_latest_by_filename(filename)
    if previous and previous.get("doc_id") and previous["doc_id"] != doc_id:
        version = int(previous.get("version") or 1) + 1
        try:
            from retrieval.milvus_admin import delete_doc_vectors
            delete_doc_vectors(previous["doc_id"])
        except Exception as e:
            logger.warning(f"下架旧版本向量失败 {previous['doc_id']}: {e}")
        if store.mark_superseded(previous["doc_id"]):
            superseded = {"doc_id": previous["doc_id"], "version": previous.get("version")}

    try:
        store.register(doc_id, filename, save_path, category, file_hash, version=version)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=409)

    ok, message = _get_ingest_service().submit(doc_id)
    if not ok:
        return JSONResponse({"error": message}, status_code=409)

    return JSONResponse(
        {
            "doc_id": doc_id,
            "filename": filename,
            "category": category,
            "status": "pending",
            "version": version,
            "superseded": superseded,
            "message": (
                f"上传成功, 旧版本 v{superseded['version']} 已下架, 正在后台解析入库"
                if superseded else "上传成功, 正在后台解析入库, 请稍后查询状态"
            ),
        },
        status_code=202,
    )


@app.get("/api/docs")
def list_docs(category: str = None):
    """知识文档列表(含状态/块数/错误信息), 前端管理页数据源"""
    docs = _get_doc_store().list_docs(category=category)
    return JSONResponse({"total": len(docs), "docs": docs})


@app.get("/api/docs/{doc_id}")
def get_doc(doc_id: str):
    """单篇文档详情(上传后轮询此接口看入库进度)"""
    doc = _get_doc_store().get(doc_id)
    if not doc:
        return JSONResponse({"error": "文档不存在"}, status_code=404)
    # 顺带查一下向量库里的实际块数, 和登记表互相对账
    from retrieval.milvus_admin import count_doc_vectors
    doc["vector_count"] = count_doc_vectors(doc_id)
    return JSONResponse(doc)


@app.delete("/api/docs/{doc_id}", dependencies=[Depends(_require_admin)])
def delete_doc(doc_id: str):
    """
    删除一篇上传文档: 三处一起清, 少一处都会出现"删了还能搜到"的幽灵数据。
      1. Milvus 按 doc_id 删向量块
      2. MySQL 删登记记录
      3. uploads/ 删原文件
    """
    store = _get_doc_store()
    doc = store.get(doc_id)
    if not doc:
        return JSONResponse({"error": "文档不存在"}, status_code=404)

    from retrieval.milvus_admin import delete_doc_vectors, count_doc_vectors
    deleted_vectors = delete_doc_vectors(doc_id)
    # 删除后对账: Strong 一致性计数, 带短暂重试(删除操作落盘有延迟, 立刻数可能读到旧值)
    remaining = count_doc_vectors(doc_id)
    for _ in range(5):
        if remaining == 0:
            break
        time.sleep(1)
        remaining = count_doc_vectors(doc_id)
    deleted_record = store.delete(doc_id)

    # 原文件删除(失败不影响主流程, 只记日志)
    file_removed = False
    try:
        if os.path.exists(doc["file_path"]):
            os.remove(doc["file_path"])
        file_removed = True
    except Exception as e:
        logger.warning(f"删除上传原文件失败 {doc['file_path']}: {e}")

    ok = deleted_vectors and deleted_record and remaining == 0
    return JSONResponse({
        "deleted": ok,
        "vector_removed": remaining == 0,
        "record_removed": deleted_record,
        "file_removed": file_removed,
        "message": "文档及全部向量块已删除" if ok else "部分清理失败, 请检查服务日志",
    })


@app.post("/api/docs/{doc_id}/reindex", dependencies=[Depends(_require_admin)])
def reindex_doc(doc_id: str):
    """重新入库(失败重试 / 更新配置后重跑), 状态置回 pending 排队"""
    store = _get_doc_store()
    doc = store.get(doc_id)
    if not doc:
        return JSONResponse({"error": "文档不存在"}, status_code=404)
    if not os.path.exists(doc["file_path"]):
        return JSONResponse({"error": "原文件已不存在, 无法重建"}, status_code=400)

    ok, message = _get_ingest_service().submit(doc_id)
    if not ok:
        return JSONResponse({"error": message}, status_code=409)
    return JSONResponse({"doc_id": doc_id, "status": "pending", "message": "重建任务已提交"})


# ==================== FAQ / 错误码 Excel 批量导入(B层) ====================

@app.get("/api/import/template")
def download_template(kind: str = "faq"):
    """下载导入模板 Excel。kind: faq / error_code"""
    from service.excel_import import build_template
    if kind not in ("faq", "error_code"):
        return JSONResponse({"error": "kind 只能是 faq 或 error_code"}, status_code=400)
    data = build_template(kind)
    # 中文文件名需 URL 编码(RFC 5987), 否则 latin-1 编码会报错
    from urllib.parse import quote
    filename = "FAQ导入模板.xlsx" if kind == "faq" else "错误码导入模板.xlsx"
    encoded = quote(filename)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
        },
    )


@app.post("/api/import/preview")
async def preview_import(kind: str = "faq", file: UploadFile = File(...)):
    """校验预览: 只解析不落库, 返回逐行校验结果, 供前端展示后让用户确认"""
    from service.excel_import import parse_faq, parse_error_codes
    if kind not in ("faq", "error_code"):
        return JSONResponse({"error": "kind 只能是 faq 或 error_code"}, status_code=400)
    content = await file.read()
    await file.close()
    try:
        if kind == "faq":
            records, errors = parse_faq(content)
        else:
            records, errors = parse_error_codes(content)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.error(f"解析 Excel 失败: {e}")
        return JSONResponse({"error": f"Excel 解析失败: {e}"}, status_code=400)

    return JSONResponse({
        "kind": kind,
        "valid_count": len(records),
        "error_count": len(errors),
        "records": records[:50],  # 预览前 50 条, 避免超大响应
        "errors": errors[:50],
    })


@app.post("/api/import/commit", dependencies=[Depends(_require_admin)])
async def commit_import(kind: str = "faq", file: UploadFile = File(...)):
    """正式导入: 校验 + 落库 + 失效缓存"""
    from service.excel_import import import_faq, import_error_codes
    if kind not in ("faq", "error_code"):
        return JSONResponse({"error": "kind 只能是 faq 或 error_code"}, status_code=400)
    content = await file.read()
    await file.close()
    try:
        if kind == "faq":
            n, errors = import_faq(content)
        else:
            n, errors = import_error_codes(content)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.error(f"导入失败: {e}")
        return JSONResponse({"error": f"导入失败: {e}"}, status_code=500)

    return JSONResponse({
        "kind": kind,
        "imported": n,
        "error_count": len(errors),
        "errors": errors[:50],
    })


# ==================== 答案反馈 & 知识缺口(自我进化闭环) ====================

@app.post("/api/feedback")
def submit_feedback(req: FeedbackRequest):
    """
    答案反馈: 用户对回答点"有用/没用"。
    点"没用"会记为一条知识缺口(reason=feedback), 供运营定位"答案存在但用户不满意"的问题。
    """
    question = (req.question or "").strip()
    if not question:
        return JSONResponse({"error": "question 不能为空"}, status_code=400)
    try:
        from store.gap_store import get_gap_store
        store = get_gap_store()
        if not req.useful:
            store.record(question, reason="feedback", layer=req.layer or "",
                         request_id=req.request_id or None)
        return JSONResponse({"recorded": True, "useful": req.useful})
    except Exception as e:
        logger.error(f"记录反馈失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/gaps", dependencies=[Depends(_require_admin)])
def list_gaps(reason: str = None):
    """知识盲区列表: 按被问次数倒序, 运营据此决定下一批补什么知识"""
    from store.gap_store import get_gap_store
    store = get_gap_store()
    gaps = store.list_gaps(reason=reason)
    return JSONResponse({
        "total": len(gaps),
        "gaps": gaps,
        "open_count": store.count(),
    })


@app.post("/api/gaps/{query_hash}/resolve", dependencies=[Depends(_require_admin)])
def resolve_gap(query_hash: str):
    """关闭一条缺口: 补了对应知识后标记 resolved"""
    from store.gap_store import get_gap_store
    store = get_gap_store()
    ok = store.resolve(query_hash)
    if not ok:
        return JSONResponse({"error": "关闭缺口失败"}, status_code=500)
    return JSONResponse({"resolved": True})


# 静态文件: 前端页面(挂载在 /, 放在最后避免覆盖 API 路由)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(STATIC_DIR):
    @app.get("/")
    def index():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)

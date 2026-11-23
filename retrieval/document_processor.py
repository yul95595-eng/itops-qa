# -*- coding: utf-8 -*-
# retrieval/document_processor.py
# Wiki 文档加载与父子块切分 —— RAG 向量检索层的数据预处理
#
# 父子块策略:
#   父块(较大, 如 1200 字)承载完整上下文, 用于最终喂给 LLM;
#   子块(较小, 如 400 字)用于向量检索, 召回更精准;
#   检索命中子块后, 通过 parent_id 回溯父块, 把完整内容交给 LLM。
#
# 本项目的知识库是 Markdown 格式的 IT 运维 Wiki(分 network/account/printer/software 四类),
# 所以这里主要用 UnstructuredMarkdownLoader 加载, 用 MarkdownTextSplitter 切分。
import hashlib
import os
import sys
from datetime import datetime

# 将项目根目录加入模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_community.document_loaders import TextLoader
from langchain_community.document_loaders.markdown import UnstructuredMarkdownLoader
from langchain_text_splitters import MarkdownTextSplitter
from langchain_core.documents import Document

from base.config import config
from base.logger import logger
from retrieval.edu_text_spliter import ChineseRecursiveTextSplitter


# ---------- 扩展格式加载器(docx / pdf) ----------
def _load_docx(file_path):
    """Word 文档: python-docx 逐段提取, 表格转竖线分隔行(与 Markdown 表格风格一致)"""
    import docx as docx_lib
    d = docx_lib.Document(file_path)
    parts = []
    for para in d.paragraphs:
        text = para.text.strip()
        if text:
            parts.append(text)
    for table in d.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    if not parts:
        raise ValueError("Word 文档内容为空(或为纯图片/扫描件)")
    return [Document(page_content="\n\n".join(parts), metadata={"file_path": file_path})]


def _load_pdf(file_path):
    """PDF 文档: pypdf 逐页提取文本(扫描件无文本层会得到空内容, 直接报错提示)"""
    from pypdf import PdfReader
    reader = PdfReader(file_path)
    texts = []
    for page in reader.pages:
        t = (page.extract_text() or "").strip()
        if t:
            texts.append(t)
    if not texts:
        raise ValueError("PDF 无可提取文本(可能为扫描件, 需先 OCR)")
    return [Document(page_content="\n\n".join(texts), metadata={"file_path": file_path})]


# 支持的文件类型 -> 加载器
document_loaders = {
    ".txt": TextLoader,
    ".md": UnstructuredMarkdownLoader,
    ".docx": _load_docx,
    ".pdf": _load_pdf,
}


def compute_doc_id(file_path):
    """
    内容寻址的文档 ID: md5(文件字节) 前 16 位。
    - 同一份文件重复入库 -> doc_id 相同 -> 子块主键相同 -> upsert 原地覆盖(幂等)
    - 文件内容变化 -> doc_id 变化 -> 需按旧 doc_id 删除(见 milvus_admin.delete_doc_vectors)
    """
    hasher = hashlib.md5()
    with open(file_path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()[:16]


def _load_single_file(file_path):
    """按扩展名加载单个文件, 返回 Document 列表(不打 source, 由调用方决定)"""
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in document_loaders:
        raise ValueError(f"不支持的文件类型: {ext}(支持 md/txt/docx/pdf)")
    loader = document_loaders[ext]
    if ext == ".txt":
        return loader(file_path, encoding="utf-8").load()
    if ext in (".docx", ".pdf"):
        return loader(file_path)
    return loader(file_path).load()


def _split_parent_child(doc, doc_id, is_markdown):
    """
    对单个 Document 做父子块切分, 返回子块列表。
    parent_id 采用「doc_id + 段序号」内容寻址:
      doc_id 由文件内容 hash 决定, 同一文件重复入库时 parent_id 稳定,
      子块主键也稳定 -> upsert 原地覆盖, 不会产生重复数据。
    (旧实现用遍历序号 i, 多批入库时会互相撞车, 已废弃)
    """
    parent_splitter = (
        MarkdownTextSplitter(chunk_size=config.PARENT_CHUNK_SIZE, chunk_overlap=config.CHUNK_OVERLAP)
        if is_markdown else
        ChineseRecursiveTextSplitter(chunk_size=config.PARENT_CHUNK_SIZE, chunk_overlap=config.CHUNK_OVERLAP)
    )
    child_splitter = (
        MarkdownTextSplitter(chunk_size=config.CHILD_CHUNK_SIZE, chunk_overlap=config.CHUNK_OVERLAP)
        if is_markdown else
        ChineseRecursiveTextSplitter(chunk_size=config.CHILD_CHUNK_SIZE, chunk_overlap=config.CHUNK_OVERLAP)
    )

    parent_docs = parent_splitter.split_documents([doc])
    child_chunks = []
    for j, parent_doc in enumerate(parent_docs):
        parent_id = f"{doc_id}_p{j}"
        parent_doc.metadata["parent_id"] = parent_id
        parent_doc.metadata["parent_content"] = parent_doc.page_content

        sub_chunks = child_splitter.split_documents([parent_doc])
        for k, sub_chunk in enumerate(sub_chunks):
            sub_chunk.metadata["id"] = f"{parent_id}_c{k}"
            sub_chunk.metadata["parent_id"] = parent_id
            sub_chunk.metadata["parent_content"] = parent_doc.page_content
            sub_chunk.metadata["doc_id"] = doc_id
            sub_chunk.metadata["source"] = doc.metadata.get("source", "unknown")
            # 文件名(块级 ground truth 判定用): 从 file_path 取 basename, 去掉扩展名
            fp = doc.metadata.get("file_path", "")
            sub_chunk.metadata["filename"] = os.path.splitext(os.path.basename(fp))[0] if fp else ""
            child_chunks.append(sub_chunk)
    return child_chunks


def load_document_from_directory(directory_path):
    """
    从目录(含子目录)递归加载所有支持的文档, 并打上 source 元数据。
    子目录名即分类标签(network/account/printer/software)。
    :param directory_path: 目标目录(本项目为 data/wiki)
    :return: LangChain Document 列表
    """
    documents = []
    supported_extensions = document_loaders.keys()

    for root, _, files in os.walk(directory_path):
        # 用相对子目录名作为 source 分类
        rel_dir = os.path.relpath(root, directory_path)
        source = os.path.basename(root) if rel_dir == "." else rel_dir.split(os.sep)[0]

        for file in files:
            file_path = os.path.join(root, file)
            ext = os.path.splitext(file)[1].lower()
            if ext not in supported_extensions:
                logger.warning(f"不支持的文件类型, 跳过: {file_path}")
                continue
            try:
                loader_class = document_loaders[ext]
                if ext == ".txt":
                    loader = loader_class(file_path, encoding="utf-8")
                else:
                    loader = loader_class(file_path)
                loaded_docs = loader.load()
                for doc in loaded_docs:
                    doc.metadata["source"] = source
                    doc.metadata["file_path"] = file_path
                    doc.metadata["timestamp"] = datetime.now().isoformat()
                documents.extend(loaded_docs)
                logger.info(f"成功加载文件: {file_path} (source={source})")
            except Exception as e:
                logger.error(f"加载文件失败 {file_path}: {e}")

    return documents


def process_documents(
        directory_path,
        parent_chunk_size=config.PARENT_CHUNK_SIZE,
        child_chunk_size=config.CHILD_CHUNK_SIZE,
        chunk_overlap=config.CHUNK_OVERLAP):
    """
    对目录下文档做父子块切分, 返回带父块关联信息的子块列表。
    :param directory_path: 文档目录
    :param parent_chunk_size: 父块大小
    :param child_chunk_size: 子块大小
    :param chunk_overlap: 重叠大小
    :return: 子块列表(每个子块 metadata 含 doc_id / parent_id / parent_content)
    """
    documents = load_document_from_directory(directory_path)
    logger.info(f"加载文档总数: {len(documents)}")

    child_chunks = []
    for doc in documents:
        file_path = doc.metadata.get("file_path", "")
        doc_id = compute_doc_id(file_path)
        doc.metadata["doc_id"] = doc_id
        is_markdown = os.path.splitext(file_path)[1].lower() == ".md"
        child_chunks.extend(_split_parent_child(doc, doc_id, is_markdown))

    logger.info(f"总子块数: {len(child_chunks)}")
    return child_chunks


def process_single_file(file_path, doc_id=None, source="custom"):
    """
    对单个上传文件做父子块切分(供知识库上传入库使用)。
    :param file_path: 文件绝对路径
    :param doc_id: 文档 ID(为空时按文件内容自动计算)
    :param source: 知识分类(上传时由用户指定, 不再局限于内置四类)
    :return: 子块列表
    """
    if doc_id is None:
        doc_id = compute_doc_id(file_path)

    loaded = _load_single_file(file_path)
    child_chunks = []
    for doc in loaded:
        doc.metadata["source"] = source
        doc.metadata["file_path"] = file_path
        doc.metadata["timestamp"] = datetime.now().isoformat()
        doc.metadata["doc_id"] = doc_id
        is_markdown = os.path.splitext(file_path)[1].lower() == ".md"
        child_chunks.extend(_split_parent_child(doc, doc_id, is_markdown))

    logger.info(f"文件切分完成: {os.path.basename(file_path)} -> {len(child_chunks)} 个子块 (doc_id={doc_id})")
    if not child_chunks:
        raise ValueError("切分后无有效内容(文件可能为空)")
    return child_chunks


if __name__ == "__main__":
    chunks = process_documents(config.WIKI_DIR)
    print(f"切分完成, 共 {len(chunks)} 个子块")
    if chunks:
        print("示例子块:")
        print("  content:", chunks[0].page_content[:80])
        print("  parent_id:", chunks[0].metadata.get("parent_id"))
        print("  source:", chunks[0].metadata.get("source"))

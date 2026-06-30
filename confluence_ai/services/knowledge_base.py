from __future__ import annotations

import re
from collections import Counter
from typing import Any

import frappe

from confluence_ai.services.embeddings import embed_text, embedding_input_hash, get_embedding_config
from confluence_ai.services.utils import as_json, create_error, now, parse_json_list


TOKEN_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_+\-.]{2,}")
STOPWORDS = {
    "about",
    "after",
    "also",
    "and",
    "are",
    "but",
    "can",
    "for",
    "from",
    "have",
    "how",
    "into",
    "our",
    "that",
    "the",
    "their",
    "then",
    "this",
    "what",
    "when",
    "with",
    "your",
}


def tokenize(text: str | None) -> list[str]:
    if not text:
        return []
    tokens = [t.lower() for t in TOKEN_RE.findall(str(text))]
    return [t for t in tokens if t not in STOPWORDS]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    size = min(len(a), len(b))
    return sum(float(a[i]) * float(b[i]) for i in range(size))


def extract_keywords(text: str | None, limit: int = 20) -> str:
    counts = Counter(tokenize(text))
    return ", ".join([token for token, _count in counts.most_common(limit)])


def chunk_text(text: str, chunk_size: int = 1200, chunk_overlap: int = 150) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []

    chunk_size = max(int(chunk_size or 1200), 300)
    chunk_overlap = max(min(int(chunk_overlap or 0), chunk_size // 3), 0)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= chunk_size:
            current = candidate
            continue

        if current:
            chunks.append(current)
        if len(paragraph) <= chunk_size:
            current = paragraph
            continue

        start = 0
        while start < len(paragraph):
            end = start + chunk_size
            chunks.append(paragraph[start:end].strip())
            if end >= len(paragraph):
                break
            start = max(end - chunk_overlap, start + 1)
        current = ""

    if current:
        chunks.append(current)

    if chunk_overlap and len(chunks) > 1:
        stitched: list[str] = []
        previous_tail = ""
        for chunk in chunks:
            merged = f"{previous_tail}\n{chunk}".strip() if previous_tail else chunk
            stitched.append(merged[: chunk_size + chunk_overlap])
            previous_tail = chunk[-chunk_overlap:]
        return stitched

    return chunks


def rebuild_document_chunks(document_name: str) -> int:
    doc = frappe.get_doc("AI Knowledge Document", document_name)

    if not doc.enabled or doc.status != "Published" or not doc.content:
        frappe.db.delete("AI Knowledge Chunk", {"document": doc.name})
        doc.db_set("last_indexed_at", now(), update_modified=False)
        return 0

    prepared_chunks = []
    try:
        chunks = chunk_text(doc.content, doc.chunk_size, doc.chunk_overlap)
        for idx, chunk in enumerate(chunks, start=1):
            embedding = embed_text(chunk)
            prepared_chunks.append(
                {
                    "enabled": 1,
                    "title": f"{doc.title} #{idx}",
                    "document": doc.name,
                    "category": doc.category,
                    "chunk_index": idx,
                    "content": chunk,
                    "keywords": extract_keywords(chunk),
                    "embedding_json": as_json(embedding.vector),
                    "embedding_provider": embedding.provider,
                    "embedding_model": embedding.model,
                    "embedding_dimension": embedding.dimension,
                    "embedding_hash": embedding_input_hash(chunk, embedding.provider, embedding.model),
                    "index_status": "Indexed",
                    "index_error": "",
                    "indexed_at": now(),
                }
            )
    except Exception as exc:
        create_error(
            "Knowledge Indexing",
            str(exc),
            source="knowledge_base",
            payload={"document": document_name},
            exc=exc,
        )
        frappe.db.set_value(
            "AI Knowledge Chunk",
            {"document": doc.name},
            {
                "index_status": "Failed",
                "index_error": str(exc)[:1000],
            },
            update_modified=False,
        )
        doc.db_set("last_indexed_at", now(), update_modified=False)
        return 0

    frappe.db.delete("AI Knowledge Chunk", {"document": doc.name})
    for chunk_payload in prepared_chunks:
        chunk_doc = frappe.new_doc("AI Knowledge Chunk")
        chunk_doc.update(chunk_payload)
        chunk_doc.insert(ignore_permissions=True)

    doc.db_set("last_indexed_at", now(), update_modified=False)
    return len(prepared_chunks)


def rebuild_all_published_documents() -> dict:
    docs = frappe.get_all(
        "AI Knowledge Document",
        filters={"enabled": 1, "status": "Published"},
        pluck="name",
    )
    total_chunks = 0
    for document_name in docs:
        total_chunks += rebuild_document_chunks(document_name)
    return {"documents": len(docs), "chunks": total_chunks}


def _agent_can_see_document(document_name: str, agent: str | None) -> bool:
    if not agent:
        return True
    rows = frappe.get_all(
        "AI Knowledge Document Agent",
        filters={"parent": document_name, "parenttype": "AI Knowledge Document"},
        fields=["agent"],
        limit=500,
    )
    if not rows:
        return True
    return any(row.agent == agent for row in rows)


def retrieve_knowledge(
    query: str,
    *,
    agent: str | None = None,
    categories: list[str] | None = None,
    limit: int = 6,
) -> list[dict[str, Any]]:
    active_config = get_embedding_config()
    query_tokens = set(tokenize(query))
    query_embedding = embed_text(query).vector
    filters: dict[str, Any] = {"enabled": 1}
    if categories:
        filters["category"] = ["in", categories]

    rows = frappe.get_all(
        "AI Knowledge Chunk",
        filters=filters,
        fields=[
            "name",
            "title",
            "document",
            "category",
            "content",
            "keywords",
            "embedding_json",
            "embedding_provider",
            "embedding_model",
            "index_status",
        ],
        limit=1000,
        order_by="modified desc",
    )

    scored: list[dict[str, Any]] = []
    for row in rows:
        doc_status = frappe.db.get_value("AI Knowledge Document", row.document, ["enabled", "status"], as_dict=True)
        if not doc_status or not doc_status.enabled or doc_status.status != "Published":
            continue
        if not _agent_can_see_document(row.document, agent):
            continue
        if row.index_status != "Indexed":
            continue
        if row.embedding_provider != active_config.provider or row.embedding_model != active_config.model:
            continue

        try:
            embedding = parse_json_list(row.embedding_json, "Embedding JSON")
        except Exception:
            continue

        chunk_tokens = set(tokenize(f"{row.title} {row.keywords} {row.content}"))
        overlap = len(query_tokens.intersection(chunk_tokens))
        semantic = cosine_similarity(query_embedding, embedding)
        score = semantic + min(overlap * 0.02, 0.2)
        if score <= 0 and query_tokens:
            continue

        scored.append(
            {
                "score": round(score, 4),
                "chunk": row.name,
                "title": row.title,
                "document": row.document,
                "category": row.category,
                "content": row.content,
            }
        )

    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[: max(int(limit or 6), 1)]


def format_knowledge_snippets(snippets: list[dict[str, Any]], *, max_chars: int = 3500) -> str:
    parts: list[str] = []
    used = 0
    for idx, item in enumerate(snippets, start=1):
        source = item.get("title") or item.get("document") or item.get("chunk")
        content = (item.get("content") or "").strip()
        if not content:
            continue
        block = f"{idx}. {source}\n{content}"
        if used + len(block) > max_chars:
            remaining = max_chars - used
            if remaining <= 150:
                break
            block = block[:remaining].rstrip()
        parts.append(block)
        used += len(block)
        if used >= max_chars:
            break
    return "\n\n".join(parts)


@frappe.whitelist()
def rebuild_document(document_name: str) -> dict:
    return {"document": document_name, "chunks": rebuild_document_chunks(document_name)}


@frappe.whitelist()
def search(query: str, agent: str | None = None, limit: int = 6) -> list[dict[str, Any]]:
    try:
        return retrieve_knowledge(query, agent=agent, limit=int(limit or 6))
    except Exception as exc:
        create_error("Knowledge Search", str(exc), source="knowledge_base", exc=exc)
        raise


@frappe.whitelist()
def rebuild_all() -> dict:
    return rebuild_all_published_documents()

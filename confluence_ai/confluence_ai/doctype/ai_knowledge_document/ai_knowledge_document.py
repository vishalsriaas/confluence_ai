from __future__ import annotations

import frappe
from frappe.model.document import Document


class AIKnowledgeDocument(Document):
    def validate(self) -> None:
        if self.source_type == "URL":
            self._load_url_content_if_needed()
        if not self.content and self.source_file:
            self.content = _read_plain_text_file(self.source_file)
        if not self.version:
            self.version = 1
        if not self.chunk_size or int(self.chunk_size) <= 0:
            self.chunk_size = 1200
        if self.chunk_overlap is None or int(self.chunk_overlap) < 0:
            self.chunk_overlap = 150

    def on_update(self) -> None:
        if self.status != "Published" or not self.enabled:
            return
        if (
            self.flags.in_insert
            or self.has_value_changed("content")
            or self.has_value_changed("status")
            or self.has_value_changed("enabled")
            or self.has_value_changed("chunk_size")
            or self.has_value_changed("chunk_overlap")
            or self.has_value_changed("source_url")
            or self.has_value_changed("crawl_max_pages")
        ):
            from confluence_ai.services.knowledge_base import rebuild_document_chunks

            rebuild_document_chunks(self.name)

    def on_trash(self) -> None:
        frappe.db.delete("AI Knowledge Chunk", {"document": self.name})

    def _load_url_content_if_needed(self) -> None:
        if not self.source_url:
            return
        if not self.crawl_url_on_save:
            return

        should_fetch = (
            self.flags.in_insert
            or not self.content
            or self.has_value_changed("source_url")
            or self.has_value_changed("crawl_max_pages")
            or self.has_value_changed("crawl_same_domain_only")
        )
        if not should_fetch:
            return

        from confluence_ai.services.url_ingest import fetch_url_knowledge
        from confluence_ai.services.utils import now

        result = fetch_url_knowledge(
            self.source_url,
            max_pages=int(self.crawl_max_pages or 10),
            same_domain_only=bool(self.crawl_same_domain_only),
        )
        self.content = result["content"]
        self.source_name = self.source_name or result["source_url"]
        self.last_url_fetched_at = now()
        self.last_url_fetch_summary = (
            f"Fetched {result['pages_fetched']} page(s): " + ", ".join(result["urls"][:5])
        )[:500]


def _read_plain_text_file(file_url: str) -> str:
    allowed_extensions = (".txt", ".md", ".csv", ".json")
    if not file_url.lower().split("?", 1)[0].endswith(allowed_extensions):
        return ""

    file_name = frappe.db.get_value("File", {"file_url": file_url}, "name")
    if not file_name:
        return ""

    file_doc = frappe.get_doc("File", file_name)
    try:
        content = file_doc.get_content()
    except Exception:
        return ""
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="ignore")
    return str(content or "")

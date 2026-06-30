from __future__ import annotations

from frappe.model.document import Document

from confluence_ai.services.utils import parse_json_list


class AIKnowledgeChunk(Document):
    def validate(self) -> None:
        parse_json_list(self.embedding_json, "Embedding JSON")

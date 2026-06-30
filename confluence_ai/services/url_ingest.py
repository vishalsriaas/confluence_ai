from __future__ import annotations

import re
from collections import deque
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse

import frappe
import requests


SKIP_EXTENSIONS = (
    ".7z",
    ".avi",
    ".css",
    ".doc",
    ".docx",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".rar",
    ".svg",
    ".webp",
    ".xls",
    ".xlsx",
    ".zip",
)


class ReadableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "canvas"}:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag == "a":
            for key, value in attrs:
                if key and key.lower() == "href" and value:
                    self.links.append(value)
        if tag in {"p", "br", "div", "section", "article", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "canvas"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
        if tag in {"p", "div", "section", "article", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        clean = " ".join((data or "").split())
        if not clean:
            return
        if self._in_title:
            self.title_parts.append(clean)
        self.text_parts.append(clean)
        self.text_parts.append(" ")

    @property
    def title(self) -> str:
        return " ".join(self.title_parts).strip()

    @property
    def text(self) -> str:
        raw = "".join(self.text_parts)
        lines = []
        for line in raw.splitlines():
            clean = re.sub(r"\s+", " ", line).strip()
            if clean:
                lines.append(clean)
        return "\n".join(lines)


def fetch_url_knowledge(
    start_url: str,
    *,
    max_pages: int = 10,
    same_domain_only: bool = True,
    timeout: int = 15,
) -> dict:
    normalized_start = normalize_url(start_url)
    if not normalized_start:
        frappe.throw("Valid Source URL is required.")

    max_pages = max(1, min(int(max_pages or 10), 100))
    timeout = max(5, min(int(timeout or 15), 60))
    base_host = urlparse(normalized_start).netloc.lower()

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "ConfluenceAIKnowledgeBot/1.0 "
                "(+https://sriaasai.m.frappe.cloud; knowledge-base ingestion)"
            )
        }
    )

    visited: set[str] = set()
    queue: deque[str] = deque([normalized_start])
    pages: list[dict] = []

    while queue and len(pages) < max_pages:
        url = queue.popleft()
        if url in visited or should_skip_url(url):
            continue
        if same_domain_only and urlparse(url).netloc.lower() != base_host:
            continue
        visited.add(url)

        page = fetch_single_page(session, url, timeout=timeout)
        if not page:
            continue

        pages.append(page)
        for link in page["links"]:
            next_url = normalize_url(urljoin(url, link))
            if not next_url or next_url in visited or should_skip_url(next_url):
                continue
            if same_domain_only and urlparse(next_url).netloc.lower() != base_host:
                continue
            queue.append(next_url)

    if not pages:
        frappe.throw(f"No readable HTML content found at URL: {normalized_start}")

    content_parts = []
    for page in pages:
        title = page.get("title") or page["url"]
        content_parts.append(f"# {title}\nSource: {page['url']}\n\n{page['text']}")

    return {
        "source_url": normalized_start,
        "pages_fetched": len(pages),
        "urls": [page["url"] for page in pages],
        "content": "\n\n---\n\n".join(content_parts),
    }


def fetch_single_page(session: requests.Session, url: str, *, timeout: int) -> dict | None:
    try:
        response = session.get(url, timeout=timeout, allow_redirects=True)
    except requests.RequestException:
        return None

    content_type = response.headers.get("Content-Type", "")
    if not response.ok or "text/html" not in content_type.lower():
        return None

    parser = ReadableHTMLParser()
    parser.feed(response.text)
    text = parser.text.strip()
    if len(text) < 80:
        return None

    final_url = normalize_url(response.url) or url
    return {
        "url": final_url,
        "title": parser.title,
        "text": text,
        "links": parser.links,
    }


def normalize_url(url: str | None) -> str:
    if not url:
        return ""
    raw = str(url).strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = f"https:{raw}"
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"

    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""

    clean, _fragment = urldefrag(raw)
    return clean.rstrip("/")


def should_skip_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(SKIP_EXTENSIONS)


@frappe.whitelist()
def preview_url(url: str, max_pages: int = 3) -> dict:
    result = fetch_url_knowledge(url, max_pages=int(max_pages or 3))
    return {
        "source_url": result["source_url"],
        "pages_fetched": result["pages_fetched"],
        "urls": result["urls"],
        "content_preview": result["content"][:2000],
    }


@frappe.whitelist()
def fetch_document_url(document_name: str) -> dict:
    from confluence_ai.services.knowledge_base import rebuild_document_chunks
    from confluence_ai.services.utils import now

    doc = frappe.get_doc("AI Knowledge Document", document_name)
    if doc.source_type != "URL" or not doc.source_url:
        frappe.throw("AI Knowledge Document must have Source Type = URL and Source URL set.")

    result = fetch_url_knowledge(
        doc.source_url,
        max_pages=int(doc.crawl_max_pages or 10),
        same_domain_only=bool(doc.crawl_same_domain_only),
    )
    doc.content = result["content"]
    doc.source_name = doc.source_name or result["source_url"]
    doc.last_url_fetched_at = now()
    doc.last_url_fetch_summary = (
        f"Fetched {result['pages_fetched']} page(s): " + ", ".join(result["urls"][:5])
    )[:500]
    doc.save(ignore_permissions=True)

    chunks = 0
    if doc.status == "Published" and doc.enabled:
        chunks = rebuild_document_chunks(doc.name)

    frappe.db.commit()
    return {
        "document": doc.name,
        "pages_fetched": result["pages_fetched"],
        "urls": result["urls"],
        "chunks": chunks,
    }

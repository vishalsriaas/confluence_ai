from __future__ import annotations

import hashlib
from dataclasses import dataclass
from urllib.parse import urlencode

import frappe
import requests


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str
    model: str
    api_key: str
    base_url: str
    path: str
    timeout: int


@dataclass(frozen=True)
class EmbeddingResult:
    vector: list[float]
    provider: str
    model: str
    dimension: int


def get_embedding_config() -> EmbeddingConfig:
    settings = frappe.get_single("Confluence AI Settings")
    provider = (settings.get("embedding_provider") or "OpenAI").strip()
    model = (settings.get("embedding_model") or "").strip()
    base_url = (settings.get("embedding_base_url") or "").strip()
    path = (settings.get("embedding_path") or "").strip()
    timeout = int(settings.get("embedding_timeout_seconds") or 30)
    api_key = settings.get_password("embedding_api_key", raise_exception=False) or ""

    if provider == "OpenAI":
        model = model or "text-embedding-3-small"
        base_url = base_url or "https://api.openai.com/v1"
        path = path or "/embeddings"
    elif provider == "Gemini":
        model = model or "text-embedding-004"
        base_url = base_url or "https://generativelanguage.googleapis.com/v1beta"
    elif provider == "OpenAI Compatible":
        if not base_url:
            frappe.throw("Embedding Base URL is required for OpenAI Compatible provider.")
        if not path:
            path = "/embeddings"
    else:
        frappe.throw(f"Unsupported embedding provider: {provider}")

    if not api_key:
        frappe.throw("Embedding API Key is required in Confluence AI Settings before indexing/searching KB.")
    if not model:
        frappe.throw("Embedding Model is required in Confluence AI Settings.")

    return EmbeddingConfig(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        path=path,
        timeout=max(timeout, 5),
    )


def embed_text(text: str) -> EmbeddingResult:
    config = get_embedding_config()
    content = (text or "").strip()
    if not content:
        frappe.throw("Cannot create embedding for empty text.")

    if config.provider in {"OpenAI", "OpenAI Compatible"}:
        vector = _embed_openai_compatible(content, config)
    elif config.provider == "Gemini":
        vector = _embed_gemini(content, config)
    else:
        frappe.throw(f"Unsupported embedding provider: {config.provider}")

    if not vector:
        frappe.throw("Embedding provider returned an empty vector.")

    normalized = normalize_vector(vector)
    return EmbeddingResult(
        vector=normalized,
        provider=config.provider,
        model=config.model,
        dimension=len(normalized),
    )


def embedding_input_hash(text: str, provider: str, model: str) -> str:
    raw = f"{provider}\n{model}\n{text or ''}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def normalize_vector(vector: list[float]) -> list[float]:
    values = [float(v) for v in vector]
    norm = sum(v * v for v in values) ** 0.5
    if not norm:
        return values
    return [round(v / norm, 8) for v in values]


def _embed_openai_compatible(text: str, config: EmbeddingConfig) -> list[float]:
    url = f"{config.base_url}/{config.path.lstrip('/')}"
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        json={"model": config.model, "input": text},
        timeout=config.timeout,
    )
    if not response.ok:
        frappe.throw(f"Embedding provider failed with HTTP {response.status_code}: {response.text[:500]}")

    data = response.json()
    try:
        return data["data"][0]["embedding"]
    except Exception as exc:
        raise frappe.ValidationError(f"Unexpected OpenAI-compatible embedding response: {data}") from exc


def _embed_gemini(text: str, config: EmbeddingConfig) -> list[float]:
    model_path = config.model if config.model.startswith("models/") else f"models/{config.model}"
    query = urlencode({"key": config.api_key})
    url = f"{config.base_url}/{model_path}:embedContent?{query}"
    response = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json={"content": {"parts": [{"text": text}]}},
        timeout=config.timeout,
    )
    if not response.ok:
        frappe.throw(f"Embedding provider failed with HTTP {response.status_code}: {response.text[:500]}")

    data = response.json()
    try:
        return data["embedding"]["values"]
    except Exception as exc:
        raise frappe.ValidationError(f"Unexpected Gemini embedding response: {data}") from exc


@frappe.whitelist()
def test_embedding_config() -> dict:
    result = embed_text("Confluence AI production embedding configuration test.")
    return {
        "provider": result.provider,
        "model": result.model,
        "dimension": result.dimension,
        "ok": True,
    }

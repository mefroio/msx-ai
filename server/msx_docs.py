"""Small, deterministic and auditable documentation corpus for MCP clients."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib import resources
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable


DOC_URI_PREFIX = "msx-ai://docs/"
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+_.-]*", re.IGNORECASE)


@dataclass(frozen=True)
class Document:
    id: str
    path: str
    title: str
    summary: str
    tags: tuple[str, ...]
    audience: tuple[str, ...]
    backends: tuple[str, ...]
    text: str
    sha256: str

    @property
    def uri(self) -> str:
        return DOC_URI_PREFIX + self.id


def _corpus_root():
    """Return a Traversable for installed and source-checkout execution."""
    try:
        return resources.files("msx_ai.resources.docs")
    except (ModuleNotFoundError, TypeError):
        return Path(__file__).resolve().parent / "resources" / "docs"


def _safe_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or ".." in path.parts or
            len(path.parts) != 1 or path.suffix.lower() != ".md"):
        raise ValueError(f"unsafe documentation path: {value!r}")
    return value


def load_manifest() -> dict[str, Any]:
    root = _corpus_root()
    raw = root.joinpath("manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(raw)
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported documentation manifest schema")
    if (manifest.get("corpus_license", manifest.get("license")) !=
            "GPL-3.0-or-later"):
        raise ValueError(
            "documentation corpus must declare GPL-3.0-or-later")
    if not isinstance(manifest.get("documents"), list):
        raise ValueError("documentation manifest has no document list")
    return manifest


def load_documents() -> tuple[Document, ...]:
    root = _corpus_root()
    manifest = load_manifest()
    documents: list[Document] = []
    seen: set[str] = set()
    for item in manifest["documents"]:
        identifier = str(item.get("id", ""))
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", identifier):
            raise ValueError(f"invalid documentation id: {identifier!r}")
        if identifier in seen:
            raise ValueError(f"duplicate documentation id: {identifier}")
        seen.add(identifier)
        path = _safe_relative_path(str(item.get("path", "")))
        text = root.joinpath(path).read_text(encoding="utf-8")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        declared = str(item.get("sha256", "")).lower()
        if digest != declared:
            raise ValueError(f"documentation digest mismatch: {path}")
        if (item.get("origin") != "project-authored" or
                item.get("license") != "GPL-3.0-or-later"):
            raise ValueError(f"invalid provenance for documentation: {path}")
        documents.append(Document(
            id=identifier,
            path=path,
            title=str(item.get("title", identifier)),
            summary=str(item.get("summary", "")),
            tags=tuple(str(value) for value in item.get("tags", ())),
            audience=tuple(str(value) for value in item.get("audience", ())),
            backends=tuple(str(value) for value in item.get("backends", ())),
            text=text,
            sha256=digest,
        ))
    return tuple(documents)


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(token.lower() for token in _TOKEN_RE.findall(value))


def _audience_key(value: str) -> str:
    normalized = value.strip().lower()
    return normalized[:-1] if normalized.endswith("s") else normalized


def _snippet(text: str, terms: Iterable[str], *, length: int = 240) -> str:
    compact = " ".join(line.strip() for line in text.splitlines()
                       if line.strip() and not line.startswith("#"))
    lowered = compact.lower()
    positions = [lowered.find(term) for term in terms]
    positions = [position for position in positions if position >= 0]
    start = max(0, (min(positions) if positions else 0) - length // 4)
    end = min(len(compact), start + length)
    excerpt = compact[start:end].strip()
    if start:
        excerpt = "…" + excerpt
    if end < len(compact):
        excerpt += "…"
    return excerpt


def search(query: str, *, backend: str | None = None,
           audience: str | None = None, limit: int = 5) -> dict[str, Any]:
    """Search the bundled corpus with deterministic weighted lexical scoring."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ValueError("limit must be an integer in range 1..20")
    terms = _tokens(query)
    if not terms:
        raise ValueError("query must contain a searchable word")
    phrase = " ".join(terms)
    matches: list[tuple[float, Document]] = []
    for document in load_documents():
        if backend is not None and backend not in document.backends:
            continue
        if (audience is not None and
                _audience_key(audience) not in {
                    _audience_key(value) for value in document.audience}):
            continue
        title = document.title.lower()
        tags = " ".join(document.tags).lower()
        summary = document.summary.lower()
        headings = " ".join(line.lstrip("# ").lower()
                            for line in document.text.splitlines()
                            if line.startswith("#"))
        body = document.text.lower()
        score = 0.0
        for term in terms:
            score += 8.0 * title.count(term)
            score += 5.0 * tags.count(term)
            score += 3.0 * summary.count(term)
            score += 2.0 * headings.count(term)
            score += min(8, body.count(term))
        if phrase in title:
            score += 12.0
        elif phrase in summary:
            score += 6.0
        elif phrase in body:
            score += 2.0
        if score > 0:
            matches.append((score, document))
    matches.sort(key=lambda item: (-item[0], item[1].id))
    results = [{
        "id": document.id,
        "title": document.title,
        "uri": document.uri,
        "score": round(score, 3),
        "snippet": _snippet(document.text, terms),
        "tags": list(document.tags),
    } for score, document in matches[:limit]]
    return {"query": query, "count": len(results), "results": results}


def resource_catalog() -> tuple[dict[str, Any], ...]:
    documents = load_documents()
    catalog = [{
        "name": "msx-ai-docs-index",
        "title": "MSX-AI documentation index",
        "uri": DOC_URI_PREFIX + "index",
        "description": "Catalog of the bundled, project-authored MSX-AI documentation.",
        "mimeType": "application/json",
    }]
    catalog.extend({
        "name": f"msx-ai-doc-{document.id}",
        "title": document.title,
        "uri": document.uri,
        "description": document.summary,
        "mimeType": "text/markdown",
    } for document in documents)
    catalog.append({
        "name": "msx-ai-docs-provenance",
        "title": "MSX-AI documentation provenance",
        "uri": DOC_URI_PREFIX + "manifest",
        "description": "Machine-readable origin, evidence, license and digests.",
        "mimeType": "application/json",
    })
    return tuple(catalog)


def read_resource(uri: str) -> tuple[str, str]:
    """Return ``(mime_type, text)`` for one exact documentation URI."""
    if not isinstance(uri, str) or not uri.startswith(DOC_URI_PREFIX):
        raise KeyError(f"unknown MSX-AI resource URI: {uri}")
    identifier = uri[len(DOC_URI_PREFIX):]
    if identifier == "manifest":
        return ("application/json",
                json.dumps(load_manifest(), indent=2, sort_keys=True) + "\n")
    documents = load_documents()
    if identifier == "index":
        payload = {
            "documents": [{
                "id": document.id,
                "title": document.title,
                "summary": document.summary,
                "uri": document.uri,
                "tags": list(document.tags),
                "audience": list(document.audience),
                "backends": list(document.backends),
            } for document in documents]
        }
        return "application/json", json.dumps(payload, indent=2) + "\n"
    for document in documents:
        if document.id == identifier:
            return "text/markdown", document.text
    raise KeyError(f"unknown MSX-AI resource URI: {uri}")

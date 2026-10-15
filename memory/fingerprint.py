"""Stable page fingerprint for the plan-memory cache key.

Dynamic text, prices, and timestamps are not part of the skeleton.
"""
import hashlib
import re
from urllib.parse import urlparse

_LANDMARK = re.compile(r"<(header|nav|main|footer|form|aside)\b([^>]*)>", re.I)
_CONTROL = re.compile(r"<(input|select|textarea|button)\b([^>]*)>", re.I)
_HEADING = re.compile(r"<(h1|h2)\b([^>]*)>", re.I)
_ATTR = re.compile(r"""([:\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_UUID = re.compile(r"/[0-9a-fA-F]{8,}(?:-[0-9a-fA-F]+)+")
_NUMERIC_SEG = re.compile(r"/\d+")


def cache_key(normalized_step: str, page_fingerprint: str) -> str:
    raw = f"{normalized_step}\n{page_fingerprint}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def page_fingerprint(url: str, html: str) -> str:
    path = normalize_path(url)
    digest = hashlib.sha256(dom_skeleton(html).encode("utf-8")).hexdigest()[:16]
    return f"{path}|{digest}"


def normalize_path(url: str) -> str:
    if not url or url.startswith("about:"):
        return "/"
    path = urlparse(url).path or "/"
    path = _UUID.sub("", path)
    path = _NUMERIC_SEG.sub("", path)
    path = re.sub(r"/+", "/", path).lower().rstrip("/")
    return path or "/"


def dom_skeleton(html: str) -> str:
    """Landmark tags, form control ids, and heading levels. No text nodes."""
    cleaned = re.sub(r"<(script|style|svg|noscript)[\s\S]*?</\1>", "", html or "", flags=re.I)
    parts: list[str] = []
    for match in _LANDMARK.finditer(cleaned):
        tag = match.group(1).lower()
        attrs = match.group(2)
        name = _attr(attrs, "name") or _attr(attrs, "id") or _attr(attrs, "role") or ""
        action = normalize_path(_attr(attrs, "action") or "")
        parts.append(f"{tag}:{name}:{action}")
    for match in _CONTROL.finditer(cleaned):
        tag = match.group(1).lower()
        attrs = match.group(2)
        ident = _attr(attrs, "id") or _attr(attrs, "name") or ""
        kind = _attr(attrs, "type") or ""
        parts.append(f"{tag}:{ident}:{kind}")
    for match in _HEADING.finditer(cleaned):
        parts.append(match.group(1).lower())
    return "\n".join(parts)


def _attr(attrs: str, name: str) -> str:
    for match in _ATTR.finditer(attrs or ""):
        if match.group(1).lower() == name.lower():
            return match.group(2) if match.group(2) is not None else (match.group(3) or "")
    return ""

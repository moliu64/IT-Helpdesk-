"""Small, optional public-web search adapter used only when the user enables it."""
from __future__ import annotations

import json
from urllib.parse import quote
from urllib.request import Request, urlopen


def search_web(query: str, limit: int = 5) -> list[dict[str, str]]:
    """Fetch public DuckDuckGo instant-answer results without an API key.

    Network failures intentionally return an empty list so chat remains usable offline.
    """
    query = query.strip()
    if not query:
        return []
    url = f"https://api.duckduckgo.com/?q={quote(query)}&format=json&no_html=1&skip_disambig=1"
    try:
        request = Request(url, headers={"User-Agent": "HelpdeskAgent/1.0"})
        with urlopen(request, timeout=8) as response:  # noqa: S310 - fixed public endpoint
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return []
    results: list[dict[str, str]] = []
    if payload.get("AbstractText"):
        results.append({"title": payload.get("Heading", query), "snippet": payload["AbstractText"], "url": payload.get("AbstractURL", "")})
    for topic in payload.get("RelatedTopics", []):
        if len(results) >= limit:
            break
        if topic.get("Text"):
            results.append({"title": topic.get("Text", "")[:100], "snippet": topic.get("Text", ""), "url": topic.get("FirstURL", "")})
    return results

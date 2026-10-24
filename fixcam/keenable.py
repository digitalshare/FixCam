"""Keenable web search + page fetch. Falls back to the keyless public endpoint."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import httpx

from . import config

APP_TITLE = "FixCam"


_key_rejected = False


def _request(method: str, path: str, **kwargs) -> dict:
    """Use the keyed API when a valid key is set; otherwise the keyless public endpoint."""
    global _key_rejected
    if config.KEENABLE_API_KEY and not _key_rejected:
        r = httpx.request(method, f"{config.KEENABLE_BASE_URL}{path}",
                          headers={"X-API-Key": config.KEENABLE_API_KEY}, timeout=60, **kwargs)
        if r.status_code not in (401, 403):
            r.raise_for_status()
            return r.json()
        print("[keenable] API key rejected; falling back to the keyless public endpoint")
        _key_rejected = True
    r = httpx.request(method, f"{config.KEENABLE_BASE_URL}{path}/public",
                      headers={"X-Keenable-Title": APP_TITLE}, timeout=60, **kwargs)
    r.raise_for_status()
    return r.json()


def search(query: str, max_results: int = 5) -> list[dict]:
    try:
        data = _request("POST", "/v1/search", json={"query": query, "mode": "realtime", "max_results": max_results})
    except httpx.HTTPError as e:
        print(f"[keenable] search failed for {query!r}: {e}")
        return []
    return [
        {"title": h.get("title", ""), "url": h.get("url", ""),
         "snippet": (h.get("snippet") or h.get("description") or "")[:1500]}
        for h in data.get("results", [])
    ]


def fetch(url: str, prompt: str | None = None, max_chars: int = 6000) -> str:
    params = {"url": url, "max_chars": max_chars}
    if prompt:
        params["prompt"] = prompt
    try:
        data = _request("GET", "/v1/fetch", params=params)
    except httpx.HTTPError as e:
        print(f"[keenable] fetch failed for {url}: {e}")
        return ""
    return (data.get("content") or "")[:max_chars]


EXTRACT_PROMPT = ("Extract the exact troubleshooting / repair steps, the meaning of any error code, "
                  "required tools or replacement part names and part numbers, and safety warnings.")


def research(queries: list[str], fetch_top: int = 3) -> list[dict]:
    """Run queries in parallel, de-dupe URLs, and fetch the top pages' contents."""
    with ThreadPoolExecutor(max_workers=6) as pool:
        result_lists = list(pool.map(search, queries[:4]))
    # Round-robin across queries so each one contributes to the top (fetched) results.
    seen, hits = set(), []
    for rank in range(max((len(lst) for lst in result_lists), default=0)):
        for lst in result_lists:
            if rank < len(lst) and lst[rank]["url"] and lst[rank]["url"] not in seen:
                seen.add(lst[rank]["url"])
                hits.append(lst[rank])
    top = hits[:fetch_top]
    with ThreadPoolExecutor(max_workers=fetch_top or 1) as pool:
        urls = [h["url"] for h in top]
        contents = dict(zip(urls, pool.map(lambda u: fetch(u, prompt=EXTRACT_PROMPT), urls)))
    for h in hits:
        h["content"] = contents.get(h["url"], "")
    return hits[:10]

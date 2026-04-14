"""FinHouse — Web Search Tool (SearXNG)."""

import httpx
from config import get_settings

settings = get_settings()


async def web_search(query: str, num_results: int = 5) -> list[dict]:
    """
    Search the web via SearXNG and return top results.
    Returns list of {title, url, snippet}.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{settings.SEARXNG_HOST}/search",
                params={
                    "q": query,
                    "format": "json",
                    "categories": "general",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        results = []
        for item in data.get("results", [])[:num_results]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
            })
        return results

    except Exception as e:
        return [{"title": "Search Error", "url": "", "snippet": str(e)}]


# Tool schema for Ollama function calling
WEB_SEARCH_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the internet for current information. Use this when the user "
            "asks about recent events, news, or anything that requires up-to-date "
            "information beyond your training data."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query to look up on the internet.",
                },
            },
            "required": ["query"],
        },
    },
}

"""HTTP integrations for Minecraft Wiki and Dynmap."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import aiohttp

from .core import MapMarker, markers_from_dynmap


@dataclass(slots=True)
class WikiPage:
    page_id: int
    title: str
    extract: str
    url: str


class HttpServices:
    def __init__(self, timeout: float = 10.0):
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    def _session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers={"User-Agent": "AstrBot-CHTNEMC/1.0 (Minecraft server helper)"},
            )
        return self.session

    async def search_wiki(self, api_url: str, keyword: str, limit: int = 6) -> list[WikiPage]:
        params = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "generator": "search",
            "gsrsearch": keyword,
            "gsrnamespace": "0",
            "gsrlimit": str(limit),
            "prop": "extracts|info",
            "exintro": "1",
            "explaintext": "1",
            "exchars": "700",
            "inprop": "url",
            "redirects": "1",
            "origin": "*",
        }
        async with self._session().get(api_url, params=params) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
        pages: list[WikiPage] = []
        for page in (data.get("query") or {}).get("pages", []):
            pages.append(
                WikiPage(
                    page_id=int(page.get("pageid", 0)),
                    title=str(page.get("title", "")),
                    extract=str(page.get("extract", "")).strip(),
                    url=str(page.get("fullurl", "")),
                )
            )
        return pages

    async def fetch_dynmap_markers(
        self, base_url: str, world: str, url_template: str = ""
    ) -> list[MapMarker]:
        if url_template:
            url = url_template.replace("{world}", quote(world, safe=""))
        else:
            url = f"{base_url.rstrip('/')}/tiles/_markers_/marker_{quote(world, safe='')}.json"
        async with self._session().get(url) as response:
            response.raise_for_status()
            payload: dict[str, Any] = await response.json(content_type=None)
        return markers_from_dynmap(payload)

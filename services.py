"""HTTP integrations for Minecraft Wiki and Dynmap."""

from __future__ import annotations

import asyncio
import os
import tempfile
from io import BytesIO
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin

import aiohttp
from PIL import Image, ImageDraw

from .core import (
    MapMarker,
    dynmap_hd_tile_name,
    dynmap_project_to_pixels,
    markers_from_dynmap,
)


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
        self.temp_files: set[str] = set()

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        for path in self.temp_files:
            try:
                os.unlink(path)
            except OSError:
                pass
        self.temp_files.clear()

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

    async def render_dynmap(
        self,
        base_url: str,
        world_name: str,
        map_name: str,
        center_x: float | None,
        center_z: float | None,
        zoom_out: int,
        width: int = 960,
        height: int = 600,
        config_url: str = "",
        tiles_url_template: str = "",
    ) -> str:
        """Download Dynmap HD tiles and compose them locally into a PNG."""
        root_url = f"{base_url.rstrip('/')}/"
        configuration_url = urljoin(
            root_url, config_url or "up/configuration"
        )
        async with self._session().get(configuration_url) as response:
            response.raise_for_status()
            configuration: dict[str, Any] = await response.json(content_type=None)

        world = next(
            (
                item
                for item in configuration.get("worlds", [])
                if str(item.get("name")) == world_name
            ),
            None,
        )
        if not world:
            raise RuntimeError(f"Dynmap 配置中没有 world：{world_name}")
        map_config = next(
            (
                item
                for item in world.get("maps", [])
                if str(item.get("name")) == map_name
                or str(item.get("prefix")) == map_name
            ),
            None,
        )
        if not map_config:
            raise RuntimeError(f"Dynmap world {world_name} 中没有地图：{map_name}")

        world_to_map = [float(value) for value in map_config.get("worldtomap", [])]
        tile_size = 128 << max(0, int(map_config.get("tilescale", 0)))
        maximum_zoom = max(0, int(map_config.get("mapzoomout", 0)))
        zoom_out = min(max(0, zoom_out), maximum_zoom)
        center = world.get("center") or {}
        x = float(center_x if center_x is not None else center.get("x", 0))
        y = float(center.get("y", 64))
        z = float(center_z if center_z is not None else center.get("z", 0))
        pixel_x, pixel_y = dynmap_project_to_pixels(
            world_to_map, x, y, z, tile_size, zoom_out
        )

        left = pixel_x - width / 2
        top = pixel_y - height / 2
        first_x = int(left // tile_size)
        last_x = int((pixel_x + width / 2) // tile_size)
        first_y = int(top // tile_size)
        last_y = int((pixel_y + height / 2) // tile_size)
        prefix = str(map_config.get("prefix", map_name))
        image_format = str(map_config.get("image-format", "png"))
        day = bool(map_config.get("nightandday") and configuration.get("serverday"))

        tiles: list[tuple[int, int, str]] = []
        for tile_y in range(first_y, last_y + 1):
            for tile_x in range(first_x, last_x + 1):
                tile_name = dynmap_hd_tile_name(
                    prefix, tile_x, tile_y, zoom_out, image_format, day
                )
                tiles.append(
                    (
                        tile_x,
                        tile_y,
                        self._dynmap_tile_url(
                            base_url,
                            configuration,
                            world_name,
                            tile_name,
                            tiles_url_template,
                        ),
                    )
                )

        semaphore = asyncio.Semaphore(8)

        async def download_limited(url: str) -> Image.Image | None:
            async with semaphore:
                return await self._download_image(url)

        downloaded = await asyncio.gather(
            *(download_limited(url) for _, _, url in tiles)
        )
        canvas = Image.new("RGB", (width, height), (24, 28, 32))
        loaded = 0
        for (tile_x, tile_y, _), tile in zip(tiles, downloaded):
            if tile is None:
                continue
            loaded += 1
            if tile.size != (tile_size, tile_size):
                tile = tile.resize((tile_size, tile_size), Image.Resampling.LANCZOS)
            paste_x = round(tile_x * tile_size - left)
            paste_y = round(tile_y * tile_size - top)
            canvas.paste(tile.convert("RGB"), (paste_x, paste_y))
        if loaded == 0:
            sample = tiles[len(tiles) // 2][2] if tiles else "无"
            raise RuntimeError(f"Dynmap 瓦片全部加载失败；示例瓦片 URL：{sample}")

        draw = ImageDraw.Draw(canvas)
        center_px, center_py = width // 2, height // 2
        draw.ellipse(
            (center_px - 7, center_py - 7, center_px + 7, center_py + 7),
            fill=(255, 74, 74),
            outline=(255, 255, 255),
            width=2,
        )
        file = tempfile.NamedTemporaryFile(
            prefix="chtnemc_dynmap_", suffix=".png", delete=False
        )
        file.close()
        canvas.save(file.name, "PNG")
        self.temp_files.add(file.name)
        return file.name

    async def _download_image(self, url: str) -> Image.Image | None:
        try:
            async with self._session().get(url) as response:
                if response.status != 200:
                    return None
                data = await response.read()
            image = Image.open(BytesIO(data))
            image.load()
            return image
        except (aiohttp.ClientError, OSError, ValueError):
            return None

    @staticmethod
    def _dynmap_tile_url(
        base_url: str,
        configuration: dict[str, Any],
        world: str,
        tile_name: str,
        template: str,
    ) -> str:
        encoded_world = quote(world, safe="")
        encoded_tile = quote(tile_name, safe="/_-.")
        if template:
            rendered = template.replace("{world}", encoded_world).replace(
                "{tile}", encoded_tile
            )
            return urljoin(f"{base_url.rstrip('/')}/", rendered)
        configured = str((configuration.get("url") or {}).get("tiles", "tiles/"))
        tiles_base = urljoin(f"{base_url.rstrip('/')}/", configured)
        if "?" in tiles_base and tiles_base.rstrip().endswith("="):
            return f"{tiles_base}{encoded_world}/{encoded_tile}"
        return f"{tiles_base.rstrip('/')}/{encoded_world}/{encoded_tile}"

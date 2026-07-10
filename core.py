"""Pure helpers kept independent from AstrBot for easy testing."""

from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable


USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
COMMAND_RE = re.compile(r"^[a-z0-9_:-]{1,64}$")
NUMBER_RE = re.compile(
    r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?[dDfF]?"
)


@dataclass(slots=True)
class OnlineList:
    online: int
    maximum: int | None
    players: list[str]


@dataclass(slots=True)
class MapMarker:
    label: str
    x: float
    z: float
    radius_x: float = 0.0
    radius_z: float = 0.0
    polygon_x: list[float] | None = None
    polygon_z: list[float] | None = None


def valid_username(value: str) -> bool:
    return bool(USERNAME_RE.fullmatch(value))


def normalize_command(value: str) -> str | None:
    value = value.strip().lstrip("/").lower()
    return value if COMMAND_RE.fullmatch(value) else None


def parse_online_list(response: str) -> OnlineList:
    numbers = re.search(r"(\d+)\D+(\d+)", response)
    online = int(numbers.group(1)) if numbers else 0
    maximum = int(numbers.group(2)) if numbers else None
    tail = response.rsplit(":", 1)[-1] if ":" in response else ""
    players = [part.strip() for part in tail.split(",") if valid_username(part.strip())]
    if not numbers:
        online = len(players)
    return OnlineList(online=online, maximum=maximum, players=players)


def parse_position(response: str) -> tuple[float, float, float] | None:
    match = re.search(r"\[([^\]]+)\]", response)
    if not match:
        return None
    values = NUMBER_RE.findall(match.group(1))
    if len(values) < 3:
        return None
    try:
        return tuple(float(v.rstrip("dDfF")) for v in values[:3])  # type: ignore[return-value]
    except ValueError:
        return None


def parse_dimension(response: str) -> str | None:
    match = re.search(r'"((?:[a-z0-9_.-]+:)?[a-z0-9_./-]+)"', response, re.I)
    if match:
        return match.group(1)
    match = re.search(r"((?:minecraft:)?(?:overworld|the_nether|the_end))", response, re.I)
    return match.group(1) if match else None


def parse_daytime(response: str) -> int | None:
    values = re.findall(r"-?\d+", response)
    return int(values[-1]) % 24000 if values else None


def describe_daytime(ticks: int) -> tuple[str, str]:
    ticks %= 24000
    minutes = ((ticks + 6000) % 24000) * 1440 // 24000
    clock = f"{minutes // 60:02d}:{minutes % 60:02d}"
    if ticks < 4000 or ticks >= 23000:
        state = "早晨"
    elif ticks < 7000:
        state = "中午"
    elif ticks < 10000:
        state = "下午"
    elif ticks < 13000:
        state = "傍晚"
    else:
        state = "晚上"
    return clock, state


def clean_label(value: Any) -> str:
    text = re.sub(r"<[^>]+>", "", str(value or ""))
    return html.unescape(text).strip()


def markers_from_dynmap(payload: dict[str, Any]) -> list[MapMarker]:
    result: list[MapMarker] = []
    for marker_set in (payload.get("sets") or {}).values():
        for marker in (marker_set.get("markers") or {}).values():
            try:
                result.append(
                    MapMarker(
                        clean_label(marker.get("label")),
                        float(marker["x"]),
                        float(marker["z"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        for circle in (marker_set.get("circles") or {}).values():
            try:
                result.append(
                    MapMarker(
                        clean_label(circle.get("label")),
                        float(circle["x"]),
                        float(circle["z"]),
                        abs(float(circle.get("xr", 0))),
                        abs(float(circle.get("zr", 0))),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        for area in (marker_set.get("areas") or {}).values():
            try:
                xs = [float(v) for v in area["x"]]
                zs = [float(v) for v in area["z"]]
                if len(xs) >= 3 and len(xs) == len(zs):
                    result.append(
                        MapMarker(
                            clean_label(area.get("label")),
                            xs[0],
                            zs[0],
                            polygon_x=xs,
                            polygon_z=zs,
                        )
                    )
            except (KeyError, TypeError, ValueError):
                continue
    return [marker for marker in result if marker.label]


def _inside_polygon(x: float, z: float, xs: list[float], zs: list[float]) -> bool:
    inside = False
    j = len(xs) - 1
    for i in range(len(xs)):
        if (zs[i] > z) != (zs[j] > z):
            cross_x = (xs[j] - xs[i]) * (z - zs[i]) / (zs[j] - zs[i]) + xs[i]
            if x < cross_x:
                inside = not inside
        j = i
    return inside


def marker_distance(marker: MapMarker, x: float, z: float) -> float:
    if marker.polygon_x and marker.polygon_z:
        return 0.0 if _inside_polygon(x, z, marker.polygon_x, marker.polygon_z) else math.inf
    if marker.radius_x > 0 and marker.radius_z > 0:
        normalized = (
            ((x - marker.x) / marker.radius_x) ** 2
            + ((z - marker.z) / marker.radius_z) ** 2
        )
        return 0.0 if normalized <= 1 else math.hypot(x - marker.x, z - marker.z)
    return math.hypot(x - marker.x, z - marker.z)


def nearest_marker(
    markers: Iterable[MapMarker], x: float, z: float, maximum_distance: float
) -> str | None:
    candidates = ((marker_distance(marker, x, z), marker.label) for marker in markers)
    distance, label = min(candidates, default=(math.inf, None), key=lambda item: item[0])
    return label if label and distance <= maximum_distance else None

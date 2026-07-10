from __future__ import annotations

import asyncio
import html
import math
import re
import shlex
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core import (
    describe_daytime,
    nearest_marker,
    normalize_command,
    parse_daytime,
    parse_dimension,
    parse_online_list,
    parse_position,
    valid_username,
)
from .rcon import RconClient, RconError
from .services import HttpServices, WikiPage


DEFAULT_REMOTE_COMMANDS = {"tp", "tell", "msg", "seed", "ping"}
PLUGIN_COMMANDS = {
    "mchelp",
    "j",
    "mctime",
    "mcbond",
    "mcwiki",
    "mcmap",
    "tpa",
    "tpahere",
    "tpaccept",
    "tpdeny",
    "mccmdadd",
    "mccmddel",
}
MENTION_RE = re.compile(r"__ASTR_AT_(.+?)__")
DIMENSION_CONFIG_KEYS = {
    "minecraft:overworld": "overworld",
    "minecraft:the_nether": "nether",
    "minecraft:the_end": "end",
}
MAP_VIEW_LABELS = {
    "overworld": "主世界",
    "overworld_3d": "伪3D",
    "nether": "下界",
    "end": "末地",
}


class CommandUsageError(ValueError):
    pass


@dataclass(slots=True)
class Reply:
    text: str = ""
    image: str = ""
    mention_uid: str = ""


@dataclass(slots=True)
class PlayerLocation:
    name: str
    dimension: str | None
    position: tuple[float, float, float] | None


@dataclass(slots=True)
class TeleportRequest:
    kind: str
    requester_key: str
    requester_uid: str
    requester_name: str
    source_player: str
    approver_key: str
    target_player: str
    session: str
    created_at: float


class CHTNEMCPlugin(Star):
    """Use RCON, Dynmap and Minecraft Wiki to help manage a Minecraft server."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.http = HttpServices(timeout=float(config.get("http_timeout", 10)))
        self._state_lock = asyncio.Lock()
        self._state_loaded = False
        self.bindings: dict[str, str] = {}
        self.extra_commands: dict[str, str] = {}
        self.wiki_choices: dict[str, tuple[float, list[WikiPage]]] = {}
        self.teleport_requests: dict[str, TeleportRequest] = {}
        self._rcon_semaphore = asyncio.Semaphore(
            max(1, int(config.get("max_parallel_rcon", 8)))
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def dispatch_mc_command(self, event: AstrMessageEvent):
        """Dispatch built-in and administrator-added slash commands."""
        raw = self._message_with_mentions(event).strip()
        if not raw.startswith("/"):
            return

        await self._ensure_state()
        first, _, args = raw[1:].partition(" ")
        command = normalize_command(first)
        if not command or not self._manages(command):
            return

        event.stop_event()
        scope_error = self._scope_error(event)
        if scope_error:
            yield event.plain_result(scope_error)
            return

        try:
            reply = await self._dispatch(event, command, args.strip())
        except CommandUsageError as exc:
            reply = Reply(text=str(exc))
        except RconError as exc:
            logger.warning("CHTNEMC RCON error: %s", exc)
            reply = Reply(text=f"RCON 操作失败：{exc}")
        except Exception as exc:
            logger.exception("CHTNEMC command /%s failed", command)
            reply = Reply(text=f"执行 /{command} 时发生错误：{exc}")

        if reply.image:
            yield event.image_result(reply.image)
        if reply.text and reply.mention_uid:
            yield event.chain_result(
                [Comp.At(qq=reply.mention_uid), Comp.Plain(f" {reply.text}")]
            )
        elif reply.text:
            yield event.plain_result(reply.text)

    async def _dispatch(
        self, event: AstrMessageEvent, command: str, args: str
    ) -> Reply:
        handlers = {
            "mchelp": self._handle_help,
            "j": self._handle_players,
            "mctime": self._handle_time,
            "mcbond": self._handle_bind,
            "mcwiki": self._handle_wiki,
            "mcmap": self._handle_map,
            "tpa": self._handle_tpa,
            "tpahere": self._handle_tpahere,
            "tpaccept": self._handle_tpaccept,
            "tpdeny": self._handle_tpdeny,
            "mccmdadd": self._handle_command_add,
            "mccmddel": self._handle_command_del,
        }
        if command in handlers:
            return await handlers[command](event, args)
        return await self._handle_remote_command(event, command, args)

    def _manages(self, command: str) -> bool:
        return command in PLUGIN_COMMANDS | DEFAULT_REMOTE_COMMANDS | set(self.extra_commands)

    def _scope_error(self, event: AstrMessageEvent) -> str | None:
        group_id = event.get_group_id()
        if not group_id:
            if bool(self.config.get("allow_private", True)):
                return None
            return "本插件未在私聊中启用。"
        groups = {str(value) for value in self.config.get("enabled_groups", [])}
        if groups and str(group_id) not in groups:
            return "本群未启用 Minecraft 管理插件。"
        return None

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        configured = {str(value) for value in self.config.get("admin_uids", [])}
        try:
            astrbot_admin = bool(event.is_admin())
        except (AttributeError, TypeError):
            astrbot_admin = getattr(event, "role", "member") == "admin"
        return astrbot_admin or str(event.get_sender_id()) in configured

    def _binding_key(self, event: AstrMessageEvent, uid: str | None = None) -> str:
        return f"{event.get_platform_name()}:{uid or event.get_sender_id()}"

    async def _ensure_state(self) -> None:
        if self._state_loaded:
            return
        async with self._state_lock:
            if self._state_loaded:
                return
            bindings = await self.get_kv_data("bindings", {})
            commands = await self.get_kv_data("extra_commands", {})
            self.bindings = dict(bindings) if isinstance(bindings, dict) else {}
            self.extra_commands = dict(commands) if isinstance(commands, dict) else {}
            self._state_loaded = True

    async def _save_bindings(self) -> None:
        await self.put_kv_data("bindings", self.bindings)

    async def _save_commands(self) -> None:
        await self.put_kv_data("extra_commands", self.extra_commands)

    def _rcon(self) -> RconClient:
        return RconClient(
            host=str(self.config.get("rcon_host", "127.0.0.1")),
            port=int(self.config.get("rcon_port", 25575)),
            password=str(self.config.get("rcon_password", "")),
            timeout=float(self.config.get("rcon_timeout", 5)),
        )

    async def _execute(self, command: str) -> str:
        if "\n" in command or "\r" in command:
            raise CommandUsageError("指令中不能包含换行符。")
        maximum = int(self.config.get("max_command_length", 500))
        if len(command) > maximum:
            raise CommandUsageError(f"指令过长（最多 {maximum} 个字符）。")
        async with self._rcon_semaphore:
            return await self._rcon().execute(command)

    def _message_with_mentions(self, event: AstrMessageEvent) -> str:
        parts: list[str] = []
        for component in event.get_messages():
            if isinstance(component, Comp.Plain):
                parts.append(str(getattr(component, "text", "")))
            elif isinstance(component, Comp.At):
                uid = self._mention_uid(component)
                if uid and uid != str(event.get_self_id()):
                    parts.append(f" __ASTR_AT_{uid}__ ")
        return "".join(parts) or event.message_str

    @staticmethod
    def _mention_uid(component: Any) -> str:
        for field in ("qq", "user_id", "id", "target"):
            value = getattr(component, field, None)
            if value is not None:
                return str(value)
        return ""

    def _resolve_mentions(self, event: AstrMessageEvent, value: str) -> str:
        def replace(match: re.Match[str]) -> str:
            uid = match.group(1)
            player = self.bindings.get(self._binding_key(event, uid))
            if not player:
                raise CommandUsageError(f"被艾特的成员（UID {uid}）尚未绑定 MC 玩家名。")
            return player

        return MENTION_RE.sub(replace, value)

    async def _handle_help(self, event: AstrMessageEvent, args: str) -> Reply:
        topic = normalize_command(args.split()[0]) if args else None
        details = {
            "j": "/j — 查看在线玩家、维度、坐标及附近 Dynmap 标记。",
            "mctime": "/mctime — 查看主世界时间和早晨/中午/下午/傍晚/晚上状态。",
            "mcbond": "/mcbond <玩家名> — 将当前聊天平台 UID 绑定到 MC 玩家。玩家需在线（可在配置中关闭校验）。",
            "mcwiki": "/mcwiki <关键词> — 模糊搜索中文 Minecraft Wiki；有多个结果时再发送 /mcwiki <序号>。",
            "mcmap": (
                "/mcmap [视图] [x] [z] [缩放] — 渲染 Dynmap。"
                "视图名由 dynmap_views 配置；/mcmap list 可查看。"
            ),
            "tp": "/tp <目标> 或 /tp <玩家> <目标> — 转发传送指令；单目标语法需要先绑定。目标可用群聊艾特。",
            "tpa": "/tpa <玩家> — 以绑定玩家为发起者请求传送；目标可用群聊艾特。",
            "tpahere": "/tpahere <玩家> — 请求对方传送到自己；目标可用群聊艾特。",
            "tpaccept": "/tpaccept — 接受当前会话中发给自己的传送请求，随后执行原版 /tp。",
            "tpdeny": "/tpdeny — 拒绝当前会话中发给自己的传送请求。",
            "tell": "/tell <玩家> <消息>（或 /msg）— 向 MC 玩家发送私信；玩家可用群聊艾特。",
            "seed": "/seed — 查看世界种子（服务端权限仍由 RCON 控制）。",
            "ping": "/ping [玩家] — 查询绑定玩家或指定玩家的延迟；未绑定且无参数时显示 RCON 往返延迟。",
            "mccmdadd": "/mccmdadd <指令> [admin] — 管理员添加可转发指令；admin 表示仅管理员可用。",
            "mccmddel": "/mccmddel <指令> — 管理员删除额外指令。",
        }
        if topic:
            text = details.get(topic)
            if not text:
                return Reply(text=f"没有找到 /{topic} 的帮助。发送 /mchelp 查看全部指令。")
            return Reply(text=text)
        lines = [
            "CHTNEMC · Minecraft 服务器助手",
            "查询：/j /mctime /mcwiki /mcmap",
            "绑定：/mcbond",
            "传送请求：/tpa /tpahere /tpaccept /tpdeny",
            "服务器指令：/tp /tell /msg /seed /ping",
            "管理：/mccmdadd /mccmddel",
            "发送 /mchelp <指令名> 查看具体用法。",
        ]
        if self.extra_commands:
            lines.append("额外指令：" + " ".join(f"/{name}" for name in sorted(self.extra_commands)))
        return Reply(text="\n".join(lines))

    async def _handle_players(self, event: AstrMessageEvent, args: str) -> Reply:
        response = await self._execute("list")
        online = parse_online_list(response)
        maximum = f"/{online.maximum}" if online.maximum is not None else ""
        if not online.players:
            return Reply(text=f"当前在线 {online.online}{maximum} 人。")

        locations = await asyncio.gather(
            *(self._player_location(name) for name in online.players)
        )
        marker_tasks: dict[str, asyncio.Task] = {}
        for location in locations:
            world = self._dynmap_world(location.dimension)
            if world and world not in marker_tasks and self.config.get("dynmap_url"):
                marker_tasks[world] = asyncio.create_task(self._markers(world))

        lines = [f"当前在线 {online.online}{maximum} 人："]
        for location in locations:
            dimension_name = self._dimension_config_value(
                "dimension_names",
                location.dimension,
                location.dimension or "未知维度",
            )
            if not location.position:
                lines.append(f"- {location.name} {dimension_name}：坐标未知")
                continue
            x, y, z = location.position
            suffix = ""
            world = self._dynmap_world(location.dimension)
            if world in marker_tasks:
                try:
                    markers = await marker_tasks[world]
                    label = nearest_marker(
                        markers,
                        x,
                        z,
                        float(self.config.get("marker_radius", 100)),
                    )
                    if label:
                        suffix = f"（{label}）"
                except Exception as exc:
                    logger.warning("Unable to read Dynmap markers for %s: %s", world, exc)
            coordinates = f"{math.floor(x)} {math.floor(y)} {math.floor(z)}"
            lines.append(f"- {location.name} {dimension_name}：{coordinates}{suffix}")
        return Reply(text="\n".join(lines))

    async def _player_location(self, player: str) -> PlayerLocation:
        pos_result, dim_result = await asyncio.gather(
            self._execute(f"data get entity {player} Pos"),
            self._execute(f"data get entity {player} Dimension"),
        )
        return PlayerLocation(player, parse_dimension(dim_result), parse_position(pos_result))

    def _dynmap_world(self, dimension: str | None) -> str | None:
        value = self._dimension_config_value("dynmap_worlds", dimension, "")
        return str(value) or None

    def _dimension_config_value(
        self, field: str, dimension: str | None, fallback: str
    ) -> str:
        values = dict(self.config.get(field, {}))
        # Accept both v1.0.0's full dimension IDs and v1.0.1's object keys.
        if dimension in values:
            return str(values[dimension])
        alias = DIMENSION_CONFIG_KEYS.get(dimension or "")
        return str(values.get(alias, fallback))

    def _configured_map_views(self) -> dict[str, str]:
        values = dict(self.config.get("dynmap_views", {}))
        # Stable ASCII schema keys avoid punctuation/non-ASCII field-ID issues in WebUI,
        # while commands continue to expose friendly Chinese view names.
        return {
            MAP_VIEW_LABELS.get(str(key), str(key)): str(value)
            for key, value in values.items()
            if str(value).strip()
        }

    async def _markers(self, world: str):
        return await self.http.fetch_dynmap_markers(
            str(self.config.get("dynmap_url", "")),
            world,
            str(self.config.get("dynmap_markers_url", "")),
        )

    async def _handle_time(self, event: AstrMessageEvent, args: str) -> Reply:
        response = await self._execute("time query daytime")
        ticks = parse_daytime(response)
        if ticks is None:
            raise RconError(f"无法解析服务端时间响应：{response or '空响应'}")
        clock, state = describe_daytime(ticks)
        return Reply(text=f"主世界时间：{clock}（{state}，{ticks} ticks）")

    async def _handle_bind(self, event: AstrMessageEvent, args: str) -> Reply:
        player = args.strip()
        if not valid_username(player):
            raise CommandUsageError("用法：/mcbond <玩家名>（1–16 位字母、数字或下划线）")
        if bool(self.config.get("verify_player_on_bind", True)):
            response = await self._execute(f"data get entity {player} UUID")
            failed = re.search(
                r"no entity|not found|unknown|找不到|未找到|没有找到|不存在",
                response,
                re.I,
            )
            if failed or not response:
                raise CommandUsageError(
                    f"未在在线玩家中找到 {player}。请让玩家上线后重试，或关闭绑定在线校验。"
                )
        if bool(self.config.get("unique_binding", True)):
            owner = next(
                (
                    key
                    for key, value in self.bindings.items()
                    if value.lower() == player.lower() and key != self._binding_key(event)
                ),
                None,
            )
            if owner:
                raise CommandUsageError("该 MC 玩家名已被其他成员绑定。")
        self.bindings[self._binding_key(event)] = player
        await self._save_bindings()
        return Reply(text=f"绑定成功：{event.get_sender_name() or event.get_sender_id()} → {player}")

    async def _handle_wiki(self, event: AstrMessageEvent, args: str) -> Reply:
        query = args.strip()
        if not query:
            raise CommandUsageError("用法：/mcwiki <关键词>；出现候选后用 /mcwiki <序号> 选择。")
        key = self._binding_key(event)
        if query.isdigit() and key in self.wiki_choices:
            expires, choices = self.wiki_choices[key]
            index = int(query) - 1
            if time.monotonic() > expires:
                self.wiki_choices.pop(key, None)
                raise CommandUsageError("上一次 Wiki 候选已过期，请重新搜索。")
            if not 0 <= index < len(choices):
                raise CommandUsageError(f"请选择 1–{len(choices)} 之间的序号。")
            return Reply(text=self._format_wiki_page(choices[index]))

        pages = await self.http.search_wiki(
            str(self.config.get("wiki_api_url", "https://zh.minecraft.wiki/api.php")),
            query,
            int(self.config.get("wiki_result_limit", 6)),
        )
        if not pages:
            return Reply(text=f"中文 Minecraft Wiki 中没有找到“{query}”。")
        exact = next((page for page in pages if page.title.casefold() == query.casefold()), None)
        if exact:
            return Reply(text=self._format_wiki_page(exact))
        ttl = max(30, int(self.config.get("wiki_choice_ttl", 180)))
        self.wiki_choices[key] = (time.monotonic() + ttl, pages)
        lines = [f"“{query}”的模糊匹配结果："]
        lines.extend(f"{index}. {page.title}" for index, page in enumerate(pages, 1))
        lines.append("发送 /mcwiki <序号> 查看简介与原文链接。")
        return Reply(text="\n".join(lines))

    @staticmethod
    def _format_wiki_page(page: WikiPage) -> str:
        extract = page.extract or "该条目暂无简介。"
        return f"【{page.title}】\n{extract}\n原文：{page.url}"

    async def _handle_map(self, event: AstrMessageEvent, args: str) -> Reply:
        base_url = str(self.config.get("dynmap_url", "")).strip()
        if not base_url:
            raise CommandUsageError("尚未配置 dynmap_url。")
        views = self._configured_map_views()
        if not views:
            raise CommandUsageError("尚未配置 dynmap_views。")
        try:
            parts = shlex.split(args)
        except ValueError as exc:
            raise CommandUsageError(f"地图参数格式错误：{exc}") from exc
        if parts and parts[0].lower() in {"list", "列表"}:
            return Reply(text="可用 Dynmap 视图：" + "、".join(views))
        view_names = {str(name).casefold(): str(name) for name in views}
        requested = view_names.get(parts[0].casefold()) if parts else None
        if requested:
            parts.pop(0)
        default_requested = str(
            self.config.get("default_map_view", next(iter(views)))
        )
        view_name = requested or view_names.get(default_requested.casefold()) or next(iter(views))
        view_value = str(views[view_name])
        world, _, map_name = view_value.partition("|")
        query: dict[str, str] = {"worldname": world}
        if map_name:
            query["mapname"] = map_name
        labels = ("x", "z", "zoom")
        for label, value in zip(labels, parts):
            try:
                float(value)
            except ValueError as exc:
                raise CommandUsageError(f"{label} 必须是数字。") from exc
            query[label] = value
        url = self._with_query(base_url, query)
        iframe = f"""
        <div style="width:960px;height:600px;overflow:hidden;background:#15191d;position:relative">
          <iframe src="{html.escape(url, quote=True)}" title="Dynmap"
            style="width:960px;height:600px;border:0" loading="eager"></iframe>
          <div style="position:absolute;left:14px;top:14px;padding:7px 12px;color:white;
            background:rgba(0,0,0,.65);border-radius:6px;font:16px sans-serif">
            {html.escape(view_name)}
          </div>
        </div>
        """
        try:
            image_url = await self.html_render(
                iframe,
                {},
                options={"type": "png", "full_page": True, "animations": "disabled"},
            )
            return Reply(image=image_url)
        except Exception as exc:
            logger.warning("Dynmap screenshot failed: %s", exc)
            return Reply(text=f"Dynmap 渲染失败，可直接打开：{url}\n错误：{exc}")

    @staticmethod
    def _with_query(url: str, values: dict[str, str]) -> str:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.update(values)
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )

    async def _handle_command_add(self, event: AstrMessageEvent, args: str) -> Reply:
        if not self._is_admin(event):
            raise CommandUsageError("只有插件管理员可以添加 RCON 指令。")
        parts = args.split()
        command = normalize_command(parts[0]) if parts else None
        if not command:
            raise CommandUsageError("用法：/mccmdadd <指令名> [admin]")
        if command in PLUGIN_COMMANDS:
            raise CommandUsageError("不能覆盖插件内置指令。")
        mode = "admin" if len(parts) > 1 and parts[1].lower() == "admin" else "member"
        self.extra_commands[command] = mode
        await self._save_commands()
        audience = "仅管理员" if mode == "admin" else "群成员"
        return Reply(text=f"已允许转发 /{command}（{audience}可用）。")

    async def _handle_command_del(self, event: AstrMessageEvent, args: str) -> Reply:
        if not self._is_admin(event):
            raise CommandUsageError("只有插件管理员可以删除 RCON 指令。")
        command = normalize_command(args.split()[0]) if args else None
        if not command:
            raise CommandUsageError("用法：/mccmddel <指令名>")
        if command not in self.extra_commands:
            raise CommandUsageError(f"/{command} 不在额外指令列表中。")
        self.extra_commands.pop(command, None)
        await self._save_commands()
        return Reply(text=f"已删除额外指令 /{command}。")

    def _teleport_request_key(self, event: AstrMessageEvent, uid: str) -> str:
        return f"{event.unified_msg_origin}|{self._binding_key(event, uid)}"

    def _find_bound_target(
        self, event: AstrMessageEvent, argument: str
    ) -> tuple[str, str]:
        argument = argument.strip()
        if not argument or len(argument.split()) != 1:
            raise CommandUsageError("目标必须是一个已绑定的 MC 玩家名或群聊艾特。")

        mention = MENTION_RE.fullmatch(argument)
        if mention:
            uid = mention.group(1)
            player = self.bindings.get(self._binding_key(event, uid))
            if not player:
                raise CommandUsageError("被艾特的成员尚未绑定 MC 玩家名。")
            return uid, player

        if not valid_username(argument):
            raise CommandUsageError("目标必须是一个已绑定的 MC 玩家名或群聊艾特。")
        platform_prefix = f"{event.get_platform_name()}:"
        matches = [
            (key[len(platform_prefix) :], player)
            for key, player in self.bindings.items()
            if key.startswith(platform_prefix) and player.casefold() == argument.casefold()
        ]
        if not matches:
            raise CommandUsageError(f"玩家 {argument} 尚未绑定到当前平台的成员。")
        if len(matches) > 1:
            raise CommandUsageError(f"玩家 {argument} 对应多个成员，请直接艾特目标成员。")
        return matches[0]

    async def _create_teleport_request(
        self, event: AstrMessageEvent, args: str, kind: str
    ) -> Reply:
        source_player = self.bindings.get(self._binding_key(event))
        if not source_player:
            raise CommandUsageError(f"/{kind} 需要先用 /mcbond 绑定 MC 玩家。")
        target_uid, target_player = self._find_bound_target(event, args)
        if target_uid == str(event.get_sender_id()):
            raise CommandUsageError("不能向自己发起传送请求。")
        if target_player.casefold() == source_player.casefold():
            raise CommandUsageError("发起者和目标不能绑定到同一个 MC 玩家。")

        ttl = max(10, int(self.config.get("teleport_request_ttl", 120)))
        now = time.monotonic()
        self.teleport_requests = {
            key: pending
            for key, pending in self.teleport_requests.items()
            if now - pending.created_at <= ttl
        }
        request = TeleportRequest(
            kind=kind,
            requester_key=self._binding_key(event),
            requester_uid=str(event.get_sender_id()),
            requester_name=event.get_sender_name() or str(event.get_sender_id()),
            source_player=source_player,
            approver_key=self._binding_key(event, target_uid),
            target_player=target_player,
            session=event.unified_msg_origin,
            created_at=now,
        )
        self.teleport_requests[self._teleport_request_key(event, target_uid)] = request
        if kind == "tpa":
            action = f"请求传送到你（{source_player} → {target_player}）"
        else:
            action = f"请求你传送过去（{target_player} → {source_player}）"
        return Reply(
            text=(
                f"{request.requester_name} {action}。请在 {ttl} 秒内发送 "
                "/tpaccept 接受或 /tpdeny 拒绝。"
            ),
            mention_uid=target_uid,
        )

    async def _handle_tpa(self, event: AstrMessageEvent, args: str) -> Reply:
        return await self._create_teleport_request(event, args, "tpa")

    async def _handle_tpahere(self, event: AstrMessageEvent, args: str) -> Reply:
        return await self._create_teleport_request(event, args, "tpahere")

    def _take_teleport_request(self, event: AstrMessageEvent) -> TeleportRequest:
        key = self._teleport_request_key(event, str(event.get_sender_id()))
        request = self.teleport_requests.pop(key, None)
        if not request:
            raise CommandUsageError("当前会话中没有等待你审批的传送请求。")
        ttl = max(10, int(self.config.get("teleport_request_ttl", 120)))
        if time.monotonic() - request.created_at > ttl:
            raise CommandUsageError("传送请求已经过期，请让对方重新发起。")
        current_target = self.bindings.get(request.approver_key)
        current_source = self.bindings.get(request.requester_key)
        if current_target != request.target_player or current_source != request.source_player:
            raise CommandUsageError("请求双方的玩家绑定已经改变，请重新发起传送请求。")
        return request

    async def _handle_tpaccept(self, event: AstrMessageEvent, args: str) -> Reply:
        if args:
            raise CommandUsageError("用法：/tpaccept")
        request = self._take_teleport_request(event)
        if request.kind == "tpa":
            moving, destination = request.source_player, request.target_player
        else:
            moving, destination = request.target_player, request.source_player
        output = await self._execute(f"tp {moving} {destination}")
        result = output or "服务端未返回文本。"
        return Reply(
            text=f"已接受传送请求：{moving} → {destination}\n{result}",
            mention_uid=request.requester_uid,
        )

    async def _handle_tpdeny(self, event: AstrMessageEvent, args: str) -> Reply:
        if args:
            raise CommandUsageError("用法：/tpdeny")
        request = self._take_teleport_request(event)
        return Reply(
            text=f"已拒绝 {request.requester_name} 的 /{request.kind} 请求。",
            mention_uid=request.requester_uid,
        )

    async def _handle_remote_command(
        self, event: AstrMessageEvent, command: str, args: str
    ) -> Reply:
        required_role = self.extra_commands.get(command, "member")
        if required_role == "admin" and not self._is_admin(event):
            raise CommandUsageError(f"/{command} 仅插件管理员可用。")
        resolved = self._resolve_mentions(event, args).strip()
        sender = self.bindings.get(self._binding_key(event))
        tokens = resolved.split()

        if command == "tp":
            if len(tokens) == 1 or len(tokens) in {3, 5}:
                if not sender:
                    raise CommandUsageError("这种 /tp 玩家语法需要先用 /mcbond 绑定 MC 玩家。")
                resolved = f"{sender} {resolved}"
        elif command == "ping" and not tokens:
            if sender:
                template = str(self.config.get("ping_template", "ping {source}"))
                rendered = template.format(source=sender)
                output = await self._execute(rendered)
                return Reply(text=output or "指令已发送，服务端未返回文本。")
            started = time.perf_counter()
            await self._execute("list")
            elapsed = (time.perf_counter() - started) * 1000
            return Reply(text=f"RCON 往返延迟：{elapsed:.1f} ms（绑定玩家后可查询游戏内延迟）")

        output = await self._execute(f"{command}{(' ' + resolved) if resolved else ''}")
        return Reply(text=output or "指令已发送，服务端未返回文本。")

    async def terminate(self):
        await self.http.close()

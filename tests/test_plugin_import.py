from __future__ import annotations

import asyncio
import logging
import sys
import types
import unittest
from pathlib import Path


class _Plain:
    pass


class _At:
    pass


class _EventMessageType:
    ALL = "all"


class _Filter:
    EventMessageType = _EventMessageType

    @staticmethod
    def event_message_type(*_args, **_kwargs):
        return lambda function: function


class _Star:
    def __init__(self, context):
        self.context = context


class _Event:
    def __init__(self, uid: str, name: str, session: str = "qq:GroupMessage:42"):
        self.uid = uid
        self.name = name
        self.unified_msg_origin = session

    def get_platform_name(self):
        return "qq"

    def get_sender_id(self):
        return self.uid

    def get_sender_name(self):
        return self.name


def install_astrbot_stubs():
    astrbot = types.ModuleType("astrbot")
    aiohttp = types.ModuleType("aiohttp")
    pillow = types.ModuleType("PIL")
    pillow.Image = types.SimpleNamespace(Image=object)
    pillow.ImageDraw = types.SimpleNamespace()
    api = types.ModuleType("astrbot.api")
    components = types.ModuleType("astrbot.api.message_components")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")

    components.Plain = _Plain
    components.At = _At
    api.AstrBotConfig = dict
    api.logger = logging.getLogger("test")
    event.AstrMessageEvent = object
    event.filter = _Filter
    star.Context = object
    star.Star = _Star

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.message_components": components,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
            "aiohttp": aiohttp,
            "PIL": pillow,
        }
    )


class PluginImportTests(unittest.TestCase):
    def test_plugin_module_imports_with_public_api_shape(self):
        install_astrbot_stubs()
        sys.path.insert(0, str(Path.cwd().parent))
        try:
            from astrbot_plugin_CHTNEMC.main import CHTNEMCPlugin, PLUGIN_COMMANDS

            self.assertTrue(issubclass(CHTNEMCPlugin, _Star))
            self.assertIn("mchelp", PLUGIN_COMMANDS)
            plugin = object.__new__(CHTNEMCPlugin)
            plugin.config = {
                "dimension_names": {"overworld": "主世界"},
                "dynmap_worlds": {"overworld": "survival"},
                "dynmap_views": {
                    "overworld": "survival|flat",
                    "overworld_3d": "survival|surface",
                },
            }
            self.assertEqual(
                plugin._dimension_config_value(
                    "dimension_names", "minecraft:overworld", "未知"
                ),
                "主世界",
            )
            self.assertEqual(plugin._dynmap_world("minecraft:overworld"), "survival")
            self.assertEqual(
                plugin._configured_map_views(),
                {"主世界": "survival|flat", "伪3D": "survival|surface"},
            )
        finally:
            sys.path.pop(0)

    def test_teleport_requests_execute_vanilla_tp_only_after_acceptance(self):
        install_astrbot_stubs()
        sys.path.insert(0, str(Path.cwd().parent))
        try:
            from astrbot_plugin_CHTNEMC.main import CHTNEMCPlugin

            plugin = object.__new__(CHTNEMCPlugin)
            plugin.config = {"teleport_request_ttl": 120}
            plugin.bindings = {"qq:100": "Alice", "qq:200": "Bob"}
            plugin.teleport_requests = {}
            executed = []

            async def execute(command):
                executed.append(command)
                return "Teleported"

            plugin._execute = execute
            requester = _Event("100", "Requester")
            approver = _Event("200", "Approver")

            async def scenario():
                reply = await plugin._handle_tpa(
                    requester, "__ASTR_AT_200__"
                )
                self.assertEqual(reply.mention_uid, "200")
                self.assertEqual(executed, [])
                await plugin._handle_tpaccept(approver, "")
                self.assertEqual(executed, ["tp Alice Bob"])

                await plugin._handle_tpahere(requester, "Bob")
                self.assertEqual(executed, ["tp Alice Bob"])
                await plugin._handle_tpaccept(approver, "")
                self.assertEqual(executed[-1], "tp Bob Alice")

            asyncio.run(scenario())
        finally:
            sys.path.pop(0)


if __name__ == "__main__":
    unittest.main()

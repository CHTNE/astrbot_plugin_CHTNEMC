from __future__ import annotations

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


def install_astrbot_stubs():
    astrbot = types.ModuleType("astrbot")
    aiohttp = types.ModuleType("aiohttp")
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


if __name__ == "__main__":
    unittest.main()

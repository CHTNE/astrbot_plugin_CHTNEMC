from __future__ import annotations

import json
import unittest
from pathlib import Path


SUPPORTED_TYPES = {
    "int",
    "float",
    "bool",
    "string",
    "text",
    "list",
    "file",
    "object",
    "template_list",
}


class ConfigSchemaTests(unittest.TestCase):
    def test_only_astrbot_426_supported_types_are_used(self):
        schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))

        def check(node, path="root"):
            if not isinstance(node, dict):
                return
            if "type" in node:
                self.assertIn(node["type"], SUPPORTED_TYPES, path)
                if node["type"] == "object":
                    self.assertTrue(node.get("items"), f"{path} object must define items")
            for key, value in node.items():
                check(value, f"{path}.{key}")

        check(schema)


if __name__ == "__main__":
    unittest.main()

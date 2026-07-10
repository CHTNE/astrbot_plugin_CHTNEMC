from __future__ import annotations

import asyncio
import struct
import unittest

from rcon import RconAuthError, RconClient


async def read_packet(reader: asyncio.StreamReader):
    (length,) = struct.unpack("<i", await reader.readexactly(4))
    body = await reader.readexactly(length)
    request_id, packet_type = struct.unpack("<ii", body[:8])
    return request_id, packet_type, body[8:-2].decode()


async def write_packet(writer, request_id: int, packet_type: int, value: str):
    body = struct.pack("<ii", request_id, packet_type) + value.encode() + b"\0\0"
    writer.write(struct.pack("<i", len(body)) + body)
    await writer.drain()


class RconTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers = []

    async def asyncTearDown(self):
        for server in self.servers:
            server.close()
            await server.wait_closed()

    async def start_server(self, handler):
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def test_execute(self):
        async def handler(reader, writer):
            auth_id, auth_type, password = await read_packet(reader)
            self.assertEqual((auth_id, auth_type, password), (1, 3, "secret"))
            await write_packet(writer, 1, 2, "")
            command_id, command_type, command = await read_packet(reader)
            self.assertEqual((command_id, command_type, command), (2, 2, "list"))
            await write_packet(writer, 2, 0, "There are 0 players online")
            writer.close()

        port = await self.start_server(handler)
        result = await RconClient("127.0.0.1", port, "secret").execute("list")
        self.assertEqual(result, "There are 0 players online")

    async def test_bad_password(self):
        async def handler(reader, writer):
            await read_packet(reader)
            await write_packet(writer, -1, 2, "")
            writer.close()

        port = await self.start_server(handler)
        with self.assertRaises(RconAuthError):
            await RconClient("127.0.0.1", port, "wrong").execute("list")


if __name__ == "__main__":
    unittest.main()

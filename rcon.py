"""Small asynchronous implementation of the Source/Minecraft RCON protocol."""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass


class RconError(RuntimeError):
    """Base class for user-facing RCON failures."""


class RconAuthError(RconError):
    """Raised when the server rejects the configured password."""


@dataclass(slots=True)
class RconClient:
    host: str
    port: int
    password: str
    timeout: float = 5.0
    max_packet_size: int = 4 * 1024 * 1024

    async def execute(self, command: str) -> str:
        """Authenticate, run one command and close the connection."""
        if not self.password:
            raise RconAuthError("尚未配置 RCON 密码")

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise RconError(f"无法连接 RCON {self.host}:{self.port}: {exc}") from exc

        try:
            try:
                await self._send(writer, 1, 3, self.password)
                await self._wait_for_auth(reader)
                await self._send(writer, 2, 2, command)
                return await self._read_response(reader, request_id=2)
            except RconError:
                raise
            except asyncio.TimeoutError:
                raise RconError("RCON 连接或认证超时") from None
            except (OSError, asyncio.IncompleteReadError) as exc:
                raise RconError(f"RCON 连接意外中断：{exc}") from exc
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, RuntimeError):
                pass

    async def _wait_for_auth(self, reader: asyncio.StreamReader) -> None:
        # Some servers send an empty RESPONSE_VALUE before AUTH_RESPONSE.
        for _ in range(3):
            request_id, packet_type, _ = await asyncio.wait_for(
                self._read_packet(reader), self.timeout
            )
            if request_id == -1:
                raise RconAuthError("RCON 认证失败，请检查密码")
            if request_id == 1 and packet_type == 2:
                return
        raise RconAuthError("RCON 服务器未返回有效的认证响应")

    async def _read_response(
        self, reader: asyncio.StreamReader, request_id: int
    ) -> str:
        chunks: list[str] = []
        while True:
            try:
                packet_id, _, payload = await asyncio.wait_for(
                    self._read_packet(reader),
                    self.timeout if not chunks else min(self.timeout, 0.08),
                )
            except asyncio.TimeoutError:
                if chunks:
                    break
                raise RconError("RCON 命令响应超时") from None
            except asyncio.IncompleteReadError:
                if chunks:
                    break
                raise RconError("RCON 在返回命令结果前关闭了连接") from None
            if packet_id == request_id:
                chunks.append(payload)
            if reader.at_eof():
                break
        return "".join(chunks).strip()

    async def _send(
        self,
        writer: asyncio.StreamWriter,
        request_id: int,
        packet_type: int,
        payload: str,
    ) -> None:
        encoded = payload.encode("utf-8")
        body = struct.pack("<ii", request_id, packet_type) + encoded + b"\x00\x00"
        writer.write(struct.pack("<i", len(body)) + body)
        await asyncio.wait_for(writer.drain(), self.timeout)

    async def _read_packet(
        self, reader: asyncio.StreamReader
    ) -> tuple[int, int, str]:
        raw_length = await reader.readexactly(4)
        (length,) = struct.unpack("<i", raw_length)
        if length < 10 or length > self.max_packet_size:
            raise RconError(f"RCON 返回了异常的数据包长度：{length}")
        body = await reader.readexactly(length)
        request_id, packet_type = struct.unpack("<ii", body[:8])
        payload = body[8:-2].decode("utf-8", errors="replace")
        return request_id, packet_type, payload

"""A minimal RFC 6455 WebSocket client — just enough for the Discord gateway.

The standard library has no WebSocket client, and pulling in `websockets`/`discord.py` would break the
"stdlib only" promise of this runtime. So: one handshake, masked client frames, ping/pong, and
fragmented text. Nothing else (no extensions, no compression, no subprotocols).
"""

from __future__ import annotations

import base64
import os
import socket
import ssl
import struct
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(RuntimeError):
    pass


def build_frame(payload: bytes, opcode: int = OP_TEXT, masked: bool = True) -> bytes:
    """Encode one client frame. Clients must mask; the mask key is random per frame."""
    header = bytes([0x80 | opcode])
    length = len(payload)
    mask_bit = 0x80 if masked else 0
    if length < 126:
        header += bytes([mask_bit | length])
    elif length < 1 << 16:
        header += bytes([mask_bit | 126]) + struct.pack("!H", length)
    else:
        header += bytes([mask_bit | 127]) + struct.pack("!Q", length)
    if not masked:
        return header + payload
    key = os.urandom(4)
    masked_payload = bytes(byte ^ key[i % 4] for i, byte in enumerate(payload))
    return header + key + masked_payload


def parse_frame(data: bytes) -> tuple[bool, int, bytes, int]:
    """Decode one frame from `data`: (fin, opcode, payload, bytes_consumed). Raises if incomplete."""
    if len(data) < 2:
        raise WebSocketError("frame truncated")
    fin = bool(data[0] & 0x80)
    opcode = data[0] & 0x0F
    masked = bool(data[1] & 0x80)
    length = data[1] & 0x7F
    offset = 2
    if length == 126:
        if len(data) < offset + 2:
            raise WebSocketError("frame truncated")
        length = struct.unpack("!H", data[offset:offset + 2])[0]
        offset += 2
    elif length == 127:
        if len(data) < offset + 8:
            raise WebSocketError("frame truncated")
        length = struct.unpack("!Q", data[offset:offset + 8])[0]
        offset += 8
    key = b""
    if masked:
        if len(data) < offset + 4:
            raise WebSocketError("frame truncated")
        key = data[offset:offset + 4]
        offset += 4
    if len(data) < offset + length:
        raise WebSocketError("frame truncated")
    payload = data[offset:offset + length]
    if masked:
        payload = bytes(byte ^ key[i % 4] for i, byte in enumerate(payload))
    return fin, opcode, payload, offset + length


class WebSocket:
    """Blocking text WebSocket. `recv()` returns a whole message, or None when the peer closed."""

    def __init__(self, url: str, timeout: float = 40.0):
        parts = urlsplit(url)
        if parts.scheme not in {"ws", "wss"}:
            raise WebSocketError(f"not a websocket url: {url}")
        self.host = parts.hostname or ""
        self.port = parts.port or (443 if parts.scheme == "wss" else 80)
        self.secure = parts.scheme == "wss"
        self.path = parts.path or "/"
        if parts.query:
            self.path += "?" + parts.query
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self._buffer = b""

    def connect(self) -> "WebSocket":
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.secure:
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        raw.settimeout(self.timeout)
        self.sock = raw
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        raw.sendall(handshake.encode())
        while b"\r\n\r\n" not in self._buffer:
            chunk = raw.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            self._buffer += chunk
        head, _, self._buffer = self._buffer.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n")[0].decode(errors="replace")
        if "101" not in status_line:
            raise WebSocketError(f"handshake refused: {status_line}")
        accept = base64.b64encode(
            __import__("hashlib").sha1((key + GUID).encode()).digest()).decode()
        if accept not in head.decode(errors="replace"):
            raise WebSocketError("bad Sec-WebSocket-Accept")
        return self

    def _send_frame(self, payload: bytes, opcode: int) -> None:
        if not self.sock:
            raise WebSocketError("not connected")
        self.sock.sendall(build_frame(payload, opcode))

    def send(self, text: str) -> None:
        self._send_frame(text.encode("utf-8"), OP_TEXT)

    def _read_frame(self) -> tuple[bool, int, bytes]:
        while True:
            try:
                fin, opcode, payload, used = parse_frame(self._buffer)
                self._buffer = self._buffer[used:]
                return fin, opcode, payload
            except WebSocketError:
                if not self.sock:
                    raise
            chunk = self.sock.recv(8192)
            if not chunk:
                raise WebSocketError("connection closed by peer")
            self._buffer += chunk

    def recv(self) -> str | None:
        pieces: list[bytes] = []
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == OP_CLOSE:
                return None
            if opcode == OP_PING:
                self._send_frame(payload, OP_PONG)
                continue
            if opcode == OP_PONG:
                continue
            if opcode in (OP_TEXT, OP_BINARY, OP_CONTINUATION):
                pieces.append(payload)
                if fin:
                    return b"".join(pieces).decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            if self.sock:
                self._send_frame(b"", OP_CLOSE)
        except Exception:
            pass
        finally:
            if self.sock:
                try:
                    self.sock.close()
                finally:
                    self.sock = None

"""Minimal Source RCON client (the protocol Factorio's --rcon-port speaks). Stdlib only."""

import socket
import struct
import threading

AUTH = 3
EXEC = 2
AUTH_RESPONSE = 2
RESPONSE = 0


class RconError(Exception):
    pass


class Rcon:
    def __init__(self, host: str, port: int, password: str, timeout: float = 15.0):
        self.host, self.port, self.password, self.timeout = host, port, password, timeout
        self.sock = None
        self.next_id = 1
        self.lock = threading.Lock()

    def connect(self):
        self.close()
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        rid = self._send(AUTH, self.password)
        while True:
            pid, ptype, _ = self._recv()
            if ptype == AUTH_RESPONSE:
                if pid == -1 or pid != rid:
                    self.close()
                    raise RconError("RCON authentication failed (wrong password?)")
                return

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def command(self, text: str) -> str:
        with self.lock:
            if not self.sock:
                self.connect()
            try:
                return self._exec(text)
            except (OSError, RconError):
                # one reconnect attempt (server restarted, socket dropped)
                self.connect()
                return self._exec(text)

    def _exec(self, text: str) -> str:
        rid = self._send(EXEC, text)
        while True:
            pid, ptype, body = self._recv()
            if pid == rid and ptype == RESPONSE:
                return body

    def _send(self, ptype: int, body: str) -> int:
        rid = self.next_id
        self.next_id = self.next_id % 0x7FFFFFFF + 1
        data = body.encode("utf-8")
        packet = struct.pack("<ii", rid, ptype) + data + b"\x00\x00"
        self.sock.sendall(struct.pack("<i", len(packet)) + packet)
        return rid

    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RconError("RCON connection closed")
            buf += chunk
        return bytes(buf)

    def _recv(self):
        (size,) = struct.unpack("<i", self._recv_exact(4))
        payload = self._recv_exact(size)
        pid, ptype = struct.unpack("<ii", payload[:8])
        body = payload[8:-2].decode("utf-8", errors="replace")
        return pid, ptype, body

"""Minimal RouterOS API client (stdlib only, plain API on port 8728).

Only what Phase 1 needs: log in and run a read-only print with a query.
Works with RouterOS v6.43+ and v7 (plain-text login over the API port, so
restrict the API service to the app's IP on the router).
"""
import socket


class RouterOSError(Exception):
    pass


def _encode_len(n):
    if n < 0x80:
        return bytes([n])
    if n < 0x4000:
        return (n | 0x8000).to_bytes(2, "big")
    if n < 0x200000:
        return (n | 0xC00000).to_bytes(3, "big")
    if n < 0x10000000:
        return (n | 0xE0000000).to_bytes(4, "big")
    return b"\xf0" + n.to_bytes(4, "big")


class Api:
    def __init__(self, host, port=8728, timeout=5):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RouterOSError("connection closed by router")
            buf += chunk
        return buf

    def _read_len(self):
        b = self._read(1)[0]
        if b < 0x80:
            return b
        if b < 0xC0:
            return ((b & 0x3F) << 8) | self._read(1)[0]
        if b < 0xE0:
            r = self._read(2)
            return ((b & 0x1F) << 16) | (r[0] << 8) | r[1]
        if b < 0xF0:
            r = self._read(3)
            return ((b & 0x0F) << 24) | (r[0] << 16) | (r[1] << 8) | r[2]
        return int.from_bytes(self._read(4), "big")

    def _write_sentence(self, words):
        out = b""
        for w in words:
            wb = w.encode("utf-8")
            out += _encode_len(len(wb)) + wb
        self.sock.sendall(out + b"\x00")

    def _read_sentence(self):
        words = []
        while True:
            n = self._read_len()
            if n == 0:
                return words
            words.append(self._read(n).decode("utf-8", "replace"))

    def talk(self, words):
        self._write_sentence(words)
        rows, err = [], None
        while True:
            s = self._read_sentence()
            if not s:
                continue
            kind = s[0]
            attrs = {}
            for w in s[1:]:
                if w.startswith("="):
                    k, _, v = w[1:].partition("=")
                    attrs[k] = v
            if kind == "!re":
                rows.append(attrs)
            elif kind == "!trap":
                err = attrs.get("message", "router error")
            elif kind == "!done":
                if err:
                    raise RouterOSError(err)
                return rows
            elif kind == "!fatal":
                raise RouterOSError("fatal: " + " ".join(s[1:]))

    def login(self, user, password):
        self.talk(["/login", "=name=" + user, "=password=" + password])


def query(host, port, user, password, command, where=None, timeout=5):
    """Run a read-only print: query(..., '/ppp/active/print', {'address': '1.2.3.4'})"""
    api = Api(host, port, timeout)
    try:
        api.login(user, password)
        words = [command] + ["?%s=%s" % (k, v) for k, v in (where or {}).items()]
        return api.talk(words)
    finally:
        api.close()

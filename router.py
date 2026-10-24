"""Looks up which PPPoE username is using a given IP."""
import json
import threading
import time

import routeros


class MockRouter:
    """Reads {"10.69.1.254": "username"} from a JSON file. For testing."""

    def __init__(self, path):
        self.path = path

    def username_for_ip(self, ip):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f).get(ip)
        except (OSError, ValueError):
            return None


class ApiRouter:
    def __init__(self, host, port, user, password, ttl=10):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.ttl = ttl
        self._cache = {}
        self._lock = threading.Lock()

    def username_for_ip(self, ip):
        now = time.time()
        with self._lock:
            hit = self._cache.get(ip)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
        rows = routeros.query(self.host, self.port, self.user, self.password,
                              "/ppp/active/print", {"address": ip})
        name = rows[0].get("name") if rows else None
        with self._lock:
            self._cache[ip] = (now, name)
            if len(self._cache) > 2000:
                self._cache.clear()
        return name


def make_router(cfg):
    r = cfg["router"]
    if r.get("mode", "mock") == "api":
        return ApiRouter(r["host"], int(r.get("port", 8728)), r["user"], r["password"])
    return MockRouter(r.get("mock_file", "mock_router.json"))

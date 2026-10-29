#!/usr/bin/env python3
"""
Сервер ЧимаТВ в России (ставится на отдельный сервер, см. install.sh).

Телевизоры ЧимаТВ подключаются к серверу медиатеки через него. Это HTTP CONNECT-прокси: телевизор
просит «соедини с kino.cheema.tv:443», сервер открывает соединение и дальше только пересылает байты.
Шифрование — от телевизора до сервера медиатеки (TLS): содержимое здесь не видно, ключей нет.

Пускает ТОЛЬКО к адресам из allow (по умолчанию kino.cheema.tv:443) — это не открытый прокси.
GET /health — «ok chima-relay» (для проверки ботом и сторожем). Настройки — /etc/chima-relay.json:
{"port": 443, "allow": ["kino.cheema.tv:443"], "max_per_ip": 128, "rate_per_min": 900}
"""
import json
import logging
import os
import socket
import threading
import time

CONFIG = os.environ.get("CHIMA_RELAY_CONFIG", "/etc/chima-relay.json")
DEFAULTS = {"listen": "0.0.0.0", "port": 443, "allow": ["kino.cheema.tv:443"],
            "max_per_ip": 128, "rate_per_min": 900, "max_total": 4000}
VERSION = "2"
IDLE = 600          # тишина в соединении дольше — закрываем, с
BUF = 256 * 1024

log = logging.getLogger("chima-relay")


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    cfg["allow"] = {str(a).lower() for a in cfg.get("allow") or []}
    return cfg


class Limits:
    """Сколько соединений у одного адреса сейчас и сколько новых за минуту (защита от злоупотреблений)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.active = {}
        self.recent = {}
        self.total = 0
        self.stats = {"tunnels": 0, "refused": 0, "up": 0, "down": 0}

    def enter(self, ip):
        now = time.time()
        with self.lock:
            if self.total >= self.cfg["max_total"] or self.active.get(ip, 0) >= self.cfg["max_per_ip"]:
                return False
            xs = [t for t in self.recent.get(ip, []) if t > now - 60]
            if len(xs) >= self.cfg["rate_per_min"]:
                self.recent[ip] = xs
                return False
            xs.append(now)
            self.recent[ip] = xs
            self.active[ip] = self.active.get(ip, 0) + 1
            self.total += 1
            if len(self.recent) > 5000:
                self.recent = {k: v for k, v in self.recent.items() if v and v[-1] > now - 60}
            return True

    def leave(self, ip):
        with self.lock:
            self.total -= 1
            n = self.active.get(ip, 1) - 1
            if n <= 0:
                self.active.pop(ip, None)
            else:
                self.active[ip] = n

    def add(self, key, n=1):
        with self.lock:
            self.stats[key] += n


def reply(sock, code, text, body=b""):
    try:
        sock.sendall(("HTTP/1.1 %d %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" %
                      (code, text, len(body))).encode() + body)
    except OSError:
        pass


def pipe(src, dst, limits, key):
    """Пересылать байты src → dst, пока одна из сторон не закроется."""
    try:
        while True:
            data = src.recv(BUF)
            if not data:
                break
            dst.sendall(data)
            limits.add(key, len(data))
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def tune(s):
    try:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        pass


def handle(client, addr, cfg, limits):
    ip = addr[0]
    if not limits.enter(ip):
        limits.add("refused")
        reply(client, 429, "Too Many Requests")
        client.close()
        return
    upstream = None
    try:
        client.settimeout(20)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = client.recv(4096)
            if not chunk:
                return
            data += chunk
            if len(data) > 8192:
                reply(client, 431, "Request Header Fields Too Large")
                return
        head, rest = data.split(b"\r\n\r\n", 1)
        parts = head.split(b"\r\n", 1)[0].decode("latin-1").split()
        if len(parts) != 3:
            reply(client, 400, "Bad Request")
            return
        method, target = parts[0].upper(), parts[1]
        if method == "GET" and target in ("/health", "/"):
            reply(client, 200, "OK", ("ok chima-relay %s\n" % VERSION).encode())
            return
        if method != "CONNECT":
            reply(client, 405, "Method Not Allowed")
            return
        if target.lower() not in cfg["allow"]:
            limits.add("refused")
            reply(client, 403, "Forbidden")
            return
        host, _, port = target.rpartition(":")
        try:
            upstream = socket.create_connection((host, int(port)), timeout=15)
        except (OSError, ValueError) as e:
            log.warning("нет связи с %s: %s", target, e)
            reply(client, 502, "Bad Gateway")
            return
        tune(client)
        tune(upstream)
        client.settimeout(IDLE)
        upstream.settimeout(IDLE)
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        if rest:
            upstream.sendall(rest)
        limits.add("tunnels")
        t = threading.Thread(target=pipe, args=(upstream, client, limits, "down"), daemon=True)
        t.start()
        pipe(client, upstream, limits, "up")
        t.join(IDLE)
    except OSError:
        pass
    finally:
        for s in (client, upstream):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        limits.leave(ip)


def report(limits):
    """Раз в 10 минут — сводка в журнал (без адресов)."""
    while True:
        time.sleep(600)
        with limits.lock:
            s = dict(limits.stats)
            active = limits.total
        log.info("соединений сейчас %d · туннелей всего %d · отказов %d · к серверу %.1f МБ · к телевизорам %.1f МБ",
                 active, s["tunnels"], s["refused"], s["up"] / 1e6, s["down"] / 1e6)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = load_config()
    limits = Limits(cfg)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((cfg["listen"], int(cfg["port"])))
    srv.listen(512)
    log.info("Сервер ЧимаТВ (chima-relay) слушает %s:%s, пускает к: %s", cfg["listen"], cfg["port"], ", ".join(sorted(cfg["allow"])))
    threading.Thread(target=report, args=(limits,), daemon=True).start()
    while True:
        try:
            client, addr = srv.accept()
        except OSError:
            time.sleep(0.1)
            continue
        threading.Thread(target=handle, args=(client, addr, cfg, limits), daemon=True).start()


if __name__ == "__main__":
    main()

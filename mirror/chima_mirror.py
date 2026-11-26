#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Зеркало ЧимаТВ в России (chima-mirror): домашний сервер с белым IP, который хранит часть облака
и отдаёт фильмы телевизорам напрямую — без VPN. Сервер NL из России недоступен, а зеркало
не звонит на NL вообще: NL сам шлёт ему команды и забирает состояние.

Что делает:
  • принимает команды NL (подпись HMAC общим секретом): /m/sync, /m/add, /m/remove, /m/status;
  • качает торренты через Transmission (127.0.0.1:9091) в media;
  • сам проверяет пропуск телевизора (X-Chima-Token) по списку, который прислал NL (/m/sync);
  • отдаёт /api/library.json (только фильмы этого пользователя), /api/auth (для Caddy forward_auth:
    видео и обложки отдаёт сам Caddy), /api/img (постеры), /api/ping;
  • удаляет фильмы через 3 дня после последнего просмотра и при нехватке места.

Индексатор (chima_indexer.py, отдельная служба chima-mirror-indexer) строит library.json в data/.
Конфиг /etc/chima-mirror.json: {name, secret, port, media, data, rpc, reserve_gb, ttl_days}.
Только стандартная библиотека.
"""
import base64
import hashlib
import hmac
import json
import re
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse, parse_qs

VERSION = 9
CONF_PATH = os.environ.get("CHIMA_MIRROR_CONF", "/etc/chima-mirror.json")


def load_conf():
    with open(CONF_PATH, encoding="utf-8") as f:
        c = json.load(f)
    c.setdefault("name", "mirror")
    c.setdefault("port", 8095)
    c.setdefault("media", "/srv/media")
    c.setdefault("data", "/var/lib/chima-mirror")
    c.setdefault("rpc", "http://127.0.0.1:9091/transmission/rpc")
    c.setdefault("reserve_gb", 20)
    c.setdefault("ttl_days", 3)
    return c


CONF = {}
LOCK = threading.RLock()


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


# ── файлы состояния ──

def _path(name):
    return os.path.join(CONF["data"], name)


def read_json(name, default):
    try:
        with open(_path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(name, obj):
    os.makedirs(CONF["data"], exist_ok=True)
    tmp = _path(name) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, _path(name))


# ── Transmission ──

class Transmission:
    def __init__(self, url):
        self.url = url
        self.sid = ""

    def call(self, method, **args):
        body = json.dumps({"method": method, "arguments": args}).encode()
        for _ in range(2):
            req = urllib.request.Request(self.url, data=body, headers={"X-Transmission-Session-Id": self.sid})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    res = json.load(r)
                if res.get("result") != "success":
                    raise RuntimeError(res.get("result"))
                return res.get("arguments", {})
            except urllib.error.HTTPError as e:
                if e.code == 409:
                    self.sid = e.headers.get("X-Transmission-Session-Id", "")
                    continue
                raise
        raise RuntimeError("Transmission не принял сессию")

    def torrents(self):
        return self.call("torrent-get", fields=[
            "id", "hashString", "name", "percentDone", "status", "totalSize", "sizeWhenDone", "rateDownload",
            "peersConnected", "error", "errorString", "doneDate", "downloadDir"]).get("torrents", [])


TR = None


def torrents_cached(max_age=5):
    with LOCK:
        c = getattr(torrents_cached, "c", None)
        if c and time.time() - c[0] < max_age:
            return c[1]
    try:
        t = TR.torrents()
    except Exception as e:
        log("transmission:", e)
        t = c[1] if c else []
    with LOCK:
        torrents_cached.c = (time.time(), t)
    return t


def drop_cache():
    with LOCK:
        torrents_cached.c = None
    AUTH.cache = {}


def name_hash():
    return {t["name"]: t["hashString"].lower() for t in torrents_cached(15)}


# ── доступ ──

class Auth:
    """Кто пришёл (по X-Chima-Token) и какие фильмы ему видны. Данные присылает NL (/m/sync)."""

    def __init__(self):
        self.mtime = None
        self.tokens = {}
        self.admin = ""
        self.cache = {}

    def load(self):
        try:
            mt = os.path.getmtime(_path("state.json"))
        except OSError:
            mt = None
        if mt != self.mtime:
            st = read_json("state.json", {})
            self.tokens = {d["token"]: (str(d.get("uid", "")), str(d.get("did", "")))
                           for d in st.get("devices", []) if d.get("token")}
            self.admin = str(st.get("admin", ""))
            self.mtime = mt
            self.cache = {}

    def who(self, headers):
        """→ (uid, did) или None."""
        self.load()
        tok = (headers.get("X-Chima-Token") or "").strip()
        return self.tokens.get(tok) if tok else None

    def visible_keys(self, uid, did):
        """Ключи фильмов (имена в media), которые можно видеть этому пользователю на этом телевизоре."""
        ck = (uid, did)
        with LOCK:
            c = self.cache.get(ck)
            if c and time.time() - c[0] < 15:
                return c[1]
        items = read_json("items.json", {})
        nh = name_hash()
        keys = set()
        for name, h in nh.items():
            it = items.get(h)
            if not it:
                if uid == self.admin:
                    keys.add(name)
                continue
            if uid != self.admin and str(it.get("uid")) != uid and uid not in [str(x) for x in it.get("also") or []]:
                continue
            tvs = it.get("tvs")
            if tvs is not None and did and did not in tvs:
                continue
            keys.add(name)
        with LOCK:
            self.cache[ck] = (time.time(), keys)
        return keys


AUTH = Auth()


class RateLimiter:
    """≥40 запросов без доступа за 60 с с одного адреса — блок на 5 минут (перебор пропусков)."""

    def __init__(self):
        self.fails = {}
        self.until = {}

    @staticmethod
    def ip(h, peer):
        xf = (h.get("X-Forwarded-For") or "").split(",")[0].strip()
        return xf or peer

    def blocked(self, ip):
        return self.until.get(ip, 0) > time.time()

    def fail(self, ip):
        now = time.time()
        with LOCK:
            lst = [t for t in self.fails.get(ip, []) if now - t < 60] + [now]
            self.fails[ip] = lst
            if len(lst) >= 40:
                self.until[ip] = now + 300
                self.fails[ip] = []


RATE = RateLimiter()


# ── управление от NL ──

def sig(secret, ts, method, path, body):
    mac = hmac.new(secret.encode(), digestmod=hashlib.sha256)
    mac.update(("%s\n%s\n%s\n" % (ts, method, path)).encode())
    mac.update(body)
    return mac.hexdigest()


def check_sig(handler, body):
    ts = handler.headers.get("X-Chima-Ts", "")
    got = handler.headers.get("X-Chima-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 300:
            return False
    except ValueError:
        return False
    want = sig(CONF["secret"], ts, handler.command, urlparse(handler.path).path, body)
    return hmac.compare_digest(want, got)


def _link_sig(path, exp):
    """Подпись временной прямой ссылки на файл. Ключ — производный от секрета зеркала (его знает и NL)."""
    key = hashlib.sha256((CONF["secret"] + "|link").encode()).digest()
    return hmac.new(key, ("%s|%d" % (path, exp)).encode(), hashlib.sha256).hexdigest()


def link_ok(path, query):
    try:
        q = parse_qs(query)
        exp = int(q["e"][0])
        sg = q["s"][0]
    except (KeyError, IndexError, ValueError):
        return False
    return exp > time.time() and path.startswith("/media/") and hmac.compare_digest(sg, _link_sig(path, exp))


def cmd_files(req):
    """Файлы фильма на зеркале (для прямых ссылок): [{path, size}] относительно папки media."""
    h = str(req.get("hash", "")).lower()
    t = next((x for x in torrents_cached(2) if x["hashString"].lower() == h), None)
    if not t:
        return {"ok": False, "error": "на зеркале нет такого фильма"}
    root = os.path.realpath(CONF["media"])
    base = os.path.realpath(os.path.join(root, t["name"]))
    if not base.startswith(root + os.sep):
        return {"ok": False, "error": "странный путь"}
    out = []
    if os.path.isfile(base):
        out.append({"path": t["name"], "size": os.path.getsize(base)})
    else:
        for d, _, fs in os.walk(base):
            for f in sorted(fs):
                fp = os.path.join(d, f)
                out.append({"path": os.path.relpath(fp, root), "size": os.path.getsize(fp)})
                if len(out) >= 300:
                    break
    out.sort(key=lambda x: -x["size"])
    return {"ok": True, "files": out}


def _natkey(s):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", s)]


def _torrent_path(h, rel):
    """(торрент, абсолютный путь, путь внутри раздачи) для rel (от папки media) — только внутри раздачи."""
    t = next((x for x in torrents_cached(2) if x["hashString"].lower() == str(h).lower()), None)
    if not t:
        return None, None, None, "на зеркале нет такого фильма"
    rel = (rel or t["name"]).strip("/")
    if rel != t["name"] and not rel.startswith(t["name"] + "/"):
        return t, None, None, "путь вне фильма"
    if any(p in ("", ".", "..") for p in rel.split("/")):
        return t, None, None, "странный путь"
    root = os.path.realpath(CONF["media"])
    full = os.path.realpath(os.path.join(root, rel))
    if full != os.path.join(root, t["name"]) and not full.startswith(os.path.join(root, t["name"]) + os.sep) and full != os.path.realpath(os.path.join(root, t["name"])):
        return t, None, None, "странный путь"
    return t, full, rel, None


def cmd_ls(req):
    """Содержимое папки фильма: {entries:[{name, path, dir, size, files}]} — папки сверху, серии по номерам."""
    t, full, rel, err = _torrent_path(req.get("hash"), req.get("dir"))
    if err:
        return {"ok": False, "error": err}
    root = os.path.realpath(CONF["media"])
    if os.path.isfile(full):
        return {"ok": True, "root": t["name"], "dir": os.path.dirname(rel) or t["name"],
                "entries": [{"name": os.path.basename(full), "path": rel, "dir": False, "size": os.path.getsize(full), "files": 1}]}
    if not os.path.isdir(full):
        return {"ok": True, "root": t["name"], "dir": rel, "entries": []}
    out = []
    for name in os.listdir(full):
        fp = os.path.join(full, name)
        if name.startswith(".") or name.endswith(".part"):
            continue
        if os.path.isdir(fp):
            n = size = 0
            for d, _, fs in os.walk(fp):
                for f in fs:
                    try:
                        size += os.path.getsize(os.path.join(d, f))
                        n += 1
                    except OSError:
                        pass
            out.append({"name": name, "path": rel + "/" + name, "dir": True, "size": size, "files": n})
        else:
            try:
                out.append({"name": name, "path": rel + "/" + name, "dir": False, "size": os.path.getsize(fp), "files": 1})
            except OSError:
                pass
    out.sort(key=lambda e: (not e["dir"], _natkey(e["name"])))
    return {"ok": True, "root": t["name"], "dir": rel, "entries": out[:500]}


def cmd_rename(req):
    t, full, rel, err = _torrent_path(req.get("hash"), req.get("path"))
    if err:
        return {"ok": False, "error": err}
    new = str(req.get("name", "")).strip()
    if not new or "/" in new or new in (".", "..") or new.startswith("."):
        return {"ok": False, "error": "недопустимое имя"}
    try:    # путь — как в Transmission: от имени раздачи («Сериал/S01»)
        TR.call("torrent-rename-path", ids=[t["id"]], path=rel, name=new)
    except Exception as e:
        return {"ok": False, "error": "Transmission: %s" % e}
    drop_cache()
    touch_reindex()
    return {"ok": True}


def cmd_delete(req):
    """Удалить файл или папку внутри фильма (весь фильм — /m/remove)."""
    t, full, rel, err = _torrent_path(req.get("hash"), req.get("path"))
    if err:
        return {"ok": False, "error": err}
    if rel == t["name"]:
        return {"ok": False, "error": "это весь фильм — удалите его целиком"}
    try:
        files = TR.call("torrent-get", ids=[t["id"]], fields=["files"]).get("torrents", [{}])[0].get("files", [])
        idx = [i for i, f in enumerate(files) if f["name"] == rel or f["name"].startswith(rel + "/")]
        if idx:     # чтобы Transmission не докачал удалённое
            TR.call("torrent-set", ids=[t["id"]], **{"files-unwanted": idx})
    except Exception as e:
        log("delete: transmission", e)
    try:
        if os.path.isdir(full):
            shutil.rmtree(full)
        elif os.path.exists(full):
            os.remove(full)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    drop_cache()
    touch_reindex()
    return {"ok": True}


def cmd_short(req):
    """Короткая ссылка: код → длинная подписанная (код выдумывает NL, зеркало только хранит до срока)."""
    code = str(req.get("code", ""))
    url = str(req.get("url", ""))
    exp = int(req.get("exp") or 0)
    if not code or len(code) > 20 or not url.startswith("https://" + (CONF.get("domain") or "")) or exp < time.time():
        return {"ok": False, "error": "неверные данные ссылки"}
    with LOCK:
        t = read_json("short.json", {})
        now = time.time()
        t = {k: v for k, v in t.items() if v.get("exp", 0) > now}
        if code in t:
            return {"ok": False, "error": "код занят"}
        t[code] = {"url": url, "exp": exp}
        write_json("short.json", t)
    return {"ok": True}


def cmd_update(_req):
    """Обновить зеркало: кладём флаг, root-служба systemd (chima-mirror-update.path) заново запускает установщик."""
    if not os.path.isfile("/etc/systemd/system/chima-mirror-update.path"):
        return {"ok": False, "error": "на зеркале ещё нет самообновления — один раз повторите команду установки"}
    open(_path("update_request"), "w").close()
    return {"ok": True, "from": VERSION}


def touch_reindex():
    try:
        open(_path("reindex"), "w").close()
    except OSError:
        pass


def cmd_sync(req):
    devices = []
    for d in (req.get("devices") or [])[:5000]:
        if isinstance(d, dict) and d.get("token"):
            devices.append({"token": str(d["token"])[:200], "uid": str(d.get("uid", ""))[:40],
                            "did": str(d.get("did", ""))[:80]})
    write_json("state.json", {"admin": str(req.get("admin", "")), "devices": devices, "synced": int(time.time())})
    return {"ok": True, "devices": len(devices)}


def cmd_add(req):
    h = str(req.get("hash", "")).lower()
    if len(h) != 40 or any(c not in "0123456789abcdef" for c in h):
        return {"ok": False, "error": "нет hash раздачи"}
    free, _ = disk()
    size = int(req.get("size") or 0)
    if req.get("adopt"):      # файлы уже переданы (переправка с другого сервера) — кладём на место, место уже занято ими
        try:
            _adopt(h)
        except OSError as e:
            return {"ok": False, "error": "не удалось разложить файлы: %s" % e}
    elif free - size < CONF["reserve_gb"] * 2 ** 30:
        return {"ok": False, "error": "нет места", "free": free}
    args = {"download-dir": CONF["media"], "paused": False}
    if req.get("torrent_b64"):
        args["metainfo"] = req["torrent_b64"]
    elif req.get("magnet"):
        args["filename"] = req["magnet"]
    else:
        return {"ok": False, "error": "нет magnet и .torrent"}
    try:
        res = TR.call("torrent-add", **args)
    except Exception as e:
        return {"ok": False, "error": "Transmission: %s" % e}
    t = res.get("torrent-added") or res.get("torrent-duplicate") or {}
    # Новая раздача того же сериала (трекер обновил): если папка с файлами та же — Transmission сверит
    # уже скачанное и докачает только недостающее; старую раздачу убираем БЕЗ удаления файлов.
    reused = False
    rep = str(req.get("replaces", "")).lower()
    carry = {}
    if len(rep) == 40 and rep != h:
        try:
            o = TR.call("torrent-get", ids=[rep], fields=["name"]).get("torrents") or []
            if o and o[0].get("name") and o[0]["name"] == t.get("name"):
                TR.call("torrent-remove", ids=[rep], **{"delete-local-data": False})
                reused = True
        except Exception as e:
            log("replaces:", e)
    with LOCK:
        items = read_json("items.json", {})
        if reused:
            carry = items.pop(rep, {})
        old = items.get(h) or carry
        items[h] = {
            "uid": str(req.get("uid", old.get("uid", ""))), "name": str(req.get("name") or old.get("name") or ""),
            "poster": str(req.get("poster") or old.get("poster") or ""),
            "tvs": req.get("tvs", old.get("tvs")), "keep": bool(req.get("keep", old.get("keep", False))),
            "added": old.get("added") or int(time.time()), "viewed": old.get("viewed", 0), "done": old.get("done", 0),
            "also": old.get("also", []),
        }
        write_json("items.json", items)
    if req.get("poster") and req.get("poster_b64"):
        try:
            os.makedirs(_path("img"), exist_ok=True)
            with open(_path("img/" + hashlib.sha1(req["poster"].encode()).hexdigest() + ".jpg"), "wb") as f:
                f.write(base64.b64decode(req["poster_b64"]))
        except Exception as e:
            log("poster:", e)
    drop_cache()
    touch_reindex()
    return {"ok": True, "name": t.get("name"), "duplicate": "torrent-duplicate" in res, "reused": reused}


# ── переправка фильма между серверами ──
# Файлы приходят в <data>/incoming/<hash>/<путь от media> (сюда пишут /m/upload от NL и /m/pull с другого зеркала),
# потом /m/add с adopt=true кладёт их в media, и Transmission сверяет их с торрентом (недостающее докачает сам).

def _incoming(h):
    return _path("incoming/" + h)


def _safe_rel(rel):
    rel = str(rel or "").strip("/")
    if not rel or "\\" in rel or any(p in ("", ".", "..") for p in rel.split("/")):
        raise ValueError("странный путь")
    return rel


def _adopt(h):
    src = _incoming(h)
    if not os.path.isdir(src):
        raise OSError("нет принятых файлов")
    root = os.path.realpath(CONF["media"])
    for d, _, fs in os.walk(src):
        for f in fs:
            fp = os.path.join(d, f)
            rel = os.path.relpath(fp, src)
            dest = os.path.realpath(os.path.join(root, rel))
            if not dest.startswith(root + os.sep):
                raise OSError("странный путь")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(fp, dest)
    shutil.rmtree(src, ignore_errors=True)


def cmd_meta(req):
    """Торрент и файлы фильма — чтобы переправить его на другой сервер."""
    h = str(req.get("hash", "")).lower()
    try:
        t = next((x for x in TR.call("torrent-get", ids=[h], fields=[
            "hashString", "name", "percentDone", "totalSize", "magnetLink", "torrentFile"]).get("torrents") or []), None)
    except Exception as e:
        return {"ok": False, "error": "Transmission: %s" % e}
    if not t:
        return {"ok": False, "error": "на зеркале нет такого фильма"}
    out = {"ok": True, "name": t["name"], "progress": t.get("percentDone", 0), "size": t.get("totalSize", 0),
           "magnet": t.get("magnetLink", "")}
    try:
        with open(t["torrentFile"], "rb") as f:
            out["torrent_b64"] = base64.b64encode(f.read(8 * 1024 * 1024)).decode()
    except (OSError, KeyError, TypeError):
        pass
    out["files"] = cmd_files({"hash": h}).get("files", [])
    return out


def cmd_place(req):
    """Принятые файлы без торрента (обычная папка/файл с NL) → в media. {hash: ключ, files: [{rel, size}]}."""
    h = str(req.get("hash", "")).lower()
    base = _incoming(h)
    for f in req.get("files") or []:
        try:
            p = os.path.join(base, _safe_rel(f["rel"]))
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if not os.path.isfile(p) or os.path.getsize(p) != int(f["size"]):
            return {"ok": False, "error": "файл принят не полностью: %s" % f["rel"]}
    try:
        _adopt(h)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    touch_reindex()
    return {"ok": True}


def cmd_upload(req):
    """Кусок файла от NL: {hash, rel, off, data(b64)}. off=-1 — спросить, сколько уже принято."""
    h = str(req.get("hash", "")).lower()
    if len(h) != 40 or any(c not in "0123456789abcdef" for c in h):
        return {"ok": False, "error": "нет hash"}
    try:
        rel = _safe_rel(req.get("rel"))
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    path = os.path.join(_incoming(h), rel)
    size = os.path.getsize(path) if os.path.isfile(path) else 0
    off = int(req.get("off", 0))
    if off < 0:
        return {"ok": True, "size": size}
    if off != size:
        return {"ok": False, "error": "смещение", "size": size}
    data = base64.b64decode(req.get("data") or "")
    if data:
        free, _ = disk()
        if free - len(data) < 2 * 2 ** 30:
            return {"ok": False, "error": "нет места", "size": size}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "ab") as f:
            f.write(data)
        size += len(data)
    return {"ok": True, "size": size}


XFER = {}   # hash → {state: run|done|error|cancel, done, total, error}


def _fetch_file(url, path, size, st):
    """Докачка файла по HTTP (Range) в path до size байт."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fails = 0
    while True:
        have = os.path.getsize(path) if os.path.isfile(path) else 0
        if have >= size:
            return
        if st.get("state") == "cancel":
            raise RuntimeError("отменено")
        try:
            rq = urllib.request.Request(url, headers={"Range": "bytes=%d-" % have, "User-Agent": "ChimaMirror"})
            with urllib.request.urlopen(rq, timeout=30) as r:
                if r.status == 200 and have:      # сервер не умеет Range — начинаем файл заново
                    open(path, "wb").close()
                    have = 0
                with open(path, "ab") as f:
                    while True:
                        b = r.read(1 << 20)
                        if not b:
                            break
                        f.write(b)
                        st["done"] += len(b)
                        if st.get("state") == "cancel":
                            raise RuntimeError("отменено")
            fails = 0
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404, 416):
                raise RuntimeError("источник отказал: HTTP %s" % e.code)
            fails += 1
        except RuntimeError:
            raise
        except Exception:
            fails += 1
        if fails > 8:
            raise RuntimeError("связь с источником пропала")
        if fails:
            time.sleep(min(30, 3 * fails))


def _pull(req, st):
    h = str(req["hash"]).lower()
    try:
        base = _incoming(h)
        for fl in req["files"]:
            rel = _safe_rel(fl["rel"])
            _fetch_file(fl["url"], os.path.join(base, rel), int(fl["size"]), st)
            if os.path.getsize(os.path.join(base, rel)) != int(fl["size"]):
                raise RuntimeError("размер не совпал: %s" % rel)
        add = dict(req)
        add["adopt"] = True
        add.pop("files", None)
        r = cmd_add(add)
        if not r.get("ok"):
            raise RuntimeError(r.get("error") or "не удалось добавить")
        st["state"] = "done"
    except Exception as e:
        if st.get("state") != "cancel":
            st["state"] = "error"
        st["error"] = str(e)[:200]
        log("pull", h, e)


def cmd_pull(req):
    """Забрать фильм с другого зеркала по подписанным ссылкам: {hash, files:[{rel,size,url}], + как /m/add}."""
    h = str(req.get("hash", "")).lower()
    if len(h) != 40 or not req.get("files"):
        return {"ok": False, "error": "нет данных"}
    total = sum(int(f.get("size", 0)) for f in req["files"])
    have = 0
    for f in req["files"]:
        p = os.path.join(_incoming(h), _safe_rel(f["rel"]))
        if os.path.isfile(p):
            have += os.path.getsize(p)
    free, _ = disk()
    if free - (total - have) < CONF["reserve_gb"] * 2 ** 30:
        return {"ok": False, "error": "нет места", "free": free}
    old = XFER.get(h)
    if old and old.get("state") == "run":
        return {"ok": True, "already": True}
    st = {"state": "run", "done": have, "total": total, "error": ""}
    XFER[h] = st
    threading.Thread(target=_pull, args=(req, st), daemon=True).start()
    return {"ok": True}


def cmd_xfer(req):
    h = str(req.get("hash", "")).lower()
    st = XFER.get(h)
    if not st:
        return {"ok": True, "state": "none"}
    if req.get("cancel") and st.get("state") == "run":
        st["state"] = "cancel"
    if req.get("clear") and st.get("state") != "run":
        XFER.pop(h, None)
        shutil.rmtree(_incoming(h), ignore_errors=True)
        return {"ok": True, "state": "none"}
    return {"ok": True, "state": st["state"], "done": st["done"], "total": st["total"], "error": st["error"]}


def cmd_share(req):
    """Общая раздача: ещё один пользователь видит уже скачанный фильм (add=False — убрать его)."""
    h = str(req.get("hash", "")).lower()
    uid = str(req.get("uid", ""))
    with LOCK:
        items = read_json("items.json", {})
        it = items.get(h)
        if not it or (not uid and "tvs" not in req):
            return {"ok": False, "error": "на зеркале нет такого фильма"}
        also = [str(x) for x in it.get("also") or []]
        if req.get("add", True):
            if uid not in also and uid != str(it.get("uid")):
                also.append(uid)
        else:
            also = [x for x in also if x != uid]
        it["also"] = also
        if req.get("owner"):
            it["uid"] = str(req["owner"])
        if "tvs" in req:
            it["tvs"] = req["tvs"]
        write_json("items.json", items)
    drop_cache()
    return {"ok": True}


def cmd_keep(req):
    """Хранить фильм всегда (не удалять по сроку) или снять отметку."""
    h = str(req.get("hash", "")).lower()
    with LOCK:
        items = read_json("items.json", {})
        if h not in items:
            return {"ok": False, "error": "на зеркале нет такого фильма"}
        items[h]["keep"] = bool(req.get("keep"))
        write_json("items.json", items)
    return {"ok": True}


def cmd_remove(req):
    h = str(req.get("hash", "")).lower()
    try:
        TR.call("torrent-remove", ids=[h], **{"delete-local-data": True})
    except Exception as e:
        return {"ok": False, "error": "Transmission: %s" % e}
    with LOCK:
        items = read_json("items.json", {})
        items.pop(h, None)
        write_json("items.json", items)
    drop_cache()
    touch_reindex()
    return {"ok": True}


def cmd_status(_req):
    free, total = disk()
    items = read_json("items.json", {})
    out = []
    for t in torrents_cached(2):
        h = t["hashString"].lower()
        it = items.get(h, {})
        out.append({"hash": h, "name": t["name"], "progress": round(t["percentDone"], 3),
                    "size": t.get("sizeWhenDone") or t.get("totalSize") or 0, "rate": t.get("rateDownload", 0),
                    "peers": t.get("peersConnected", 0), "error": t.get("errorString") or "",
                    "uid": it.get("uid", ""), "added": it.get("added", 0), "viewed": it.get("viewed", 0),
                    "keep": bool(it.get("keep"))})
    return {"ok": True, "name": CONF["name"], "version": VERSION, "free": free, "total": total, "items": out,
            "synced": read_json("state.json", {}).get("synced", 0)}


COMMANDS = {"/m/sync": cmd_sync, "/m/add": cmd_add, "/m/remove": cmd_remove, "/m/share": cmd_share, "/m/keep": cmd_keep, "/m/files": cmd_files, "/m/ls": cmd_ls, "/m/rename": cmd_rename, "/m/delete": cmd_delete, "/m/update": cmd_update, "/m/short": cmd_short, "/m/status": cmd_status,
            "/m/meta": cmd_meta, "/m/upload": cmd_upload, "/m/pull": cmd_pull, "/m/xfer": cmd_xfer, "/m/place": cmd_place}


def disk():
    try:
        u = shutil.disk_usage(CONF["media"])
        return u.free, u.total
    except OSError:
        return 0, 0


# ── библиотека для телевизора ──

def library_for(uid, did):
    lib = read_json("library.json", None)
    if lib is None:   # индексатор пишет рядом с items.json в data/
        lib = {"version": 1, "titles": []}
    keys = AUTH.visible_keys(uid, did)
    nh = name_hash()
    items = read_json("items.json", {})
    out = []
    for t in lib.get("titles", []):
        if t["key"] not in keys or not os.path.exists(os.path.join(CONF["media"], t["key"])):
            continue
        h = nh.get(t["key"])
        if h:
            t["hash"] = h
            it = items.get(h) or {}
            if it.get("name"):
                t["name"] = it["name"]
            if it.get("poster"):
                t["poster"] = it["poster"]
        out.append(t)
    return {"version": 1, "updated": lib.get("updated", 0), "mirror": CONF["name"], "titles": out}


def mark_viewed(key):
    h = name_hash().get(key)
    if not h:
        return
    with LOCK:
        last = getattr(mark_viewed, "last", {})
        mark_viewed.last = last
        if time.time() - last.get(h, 0) < 60:
            return
        last[h] = time.time()
        items = read_json("items.json", {})
        if h in items:
            items[h]["viewed"] = int(time.time())
            write_json("items.json", items)


# ── уборка ──

def janitor():
    """Раз в 10 минут: отметить готовые (переиндексация) и убрать старое / освободить место."""
    items = read_json("items.json", {})
    changed = False
    for t in torrents_cached(0):
        h = t["hashString"].lower()
        it = items.get(h)
        if it is not None and t["percentDone"] >= 1 and not it.get("done"):
            it["done"] = int(time.time())
            changed = True
            touch_reindex()
    now = time.time()
    ttl = CONF["ttl_days"] * 86400
    drop = []
    for h, it in items.items():
        if it.get("keep"):
            continue
        if it.get("done") and now - max(it.get("viewed", 0), it.get("done", 0)) > ttl:
            drop.append(h)
    try:      # недопереданные файлы старше 3 суток (переправка не доведена) — убираем
        inc = _path("incoming")
        for n in os.listdir(inc):
            d = os.path.join(inc, n)
            if (XFER.get(n) or {}).get("state") != "run" and now - os.path.getmtime(d) > 3 * 86400:
                shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass
    free, total = disk()
    if free < CONF["reserve_gb"] * 2 ** 30:
        rest = sorted((h for h, it in items.items() if h not in drop and it.get("done") and not it.get("keep")),
                      key=lambda h: max(items[h].get("viewed", 0), items[h].get("done", 0)))
        if rest:
            drop.append(rest[0])
    for h in drop:
        try:
            TR.call("torrent-remove", ids=[h], **{"delete-local-data": True})
        except Exception as e:
            log("уборка", h, e)
            continue
        items.pop(h, None)
        changed = True
        log("убрано (срок/место):", h)
    if changed:
        write_json("items.json", items)
        AUTH.cache = {}
        touch_reindex()


def janitor_loop():
    while True:
        try:
            janitor()
        except Exception as e:
            log("janitor:", e)
        time.sleep(600)


# ── HTTP ──

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, code=200, extra=None):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def deny(self, code=403, text="доступ закрыт"):
        RATE.fail(RATE.ip(self.headers, self.client_address[0]))
        self.send_json({"error": text}, code)

    def throttled(self):
        if RATE.blocked(RATE.ip(self.headers, self.client_address[0])):
            self.send_json({"error": "слишком много неудачных попыток — подождите 5 минут"}, 429,
                           {"Retry-After": "300"})
            return True
        return False

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 * 1024 * 1024:
            self.send_json({"error": "слишком большой запрос"}, 413)
            self.close_connection = True
            return
        body = self.rfile.read(n) if n else b""
        path = urlparse(self.path).path
        fn = COMMANDS.get(path)
        if not fn:
            return self.send_json({"error": "нет такого метода"}, 404)
        if self.throttled():
            return
        if not check_sig(self, body):
            return self.deny(403, "подпись не подошла")
        try:
            req = json.loads(body or b"{}")
        except ValueError:
            return self.send_json({"ok": False, "error": "не JSON"}, 400)
        try:
            self.send_json(fn(req))
        except Exception as e:
            log("команда", path, e)
            self.send_json({"ok": False, "error": str(e)[:200]}, 500)

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        code = None
        _m = re.match(r"^/([a-hj-km-np-z2-9]{3})$", p) or re.match(r"^/d/([A-Za-z0-9_-]{1,20})/?$", p)
        if _m:
            code = _m.group(1).lower()
        if code:
            v = read_json("short.json", {}).get(code)
            if v and v.get("exp", 0) > time.time():
                self.send_response(302)
                self.send_header("Location", v["url"])
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            return self.deny(404, "Ссылка устарела или скопирована не полностью — попросите новую.")
        if p == "/api/ping":
            return self.send_json({"ok": True, "mirror": CONF["name"], "version": VERSION})
        if self.throttled():
            return
        if p == "/m/status":
            if not check_sig(self, b""):
                return self.deny(403, "подпись не подошла")
            return self.send_json(cmd_status({}))
        who = AUTH.who(self.headers)
        if who is None and p == "/api/auth":
            # временная прямая ссылка на один файл (выдаёт бот админу)
            _, _, q1 = (self.headers.get("X-Forwarded-Uri") or "").partition("?")
            _, _, q2 = (self.headers.get("X-Chima-Orig") or "").partition("?")
            raw = unquote(urlparse(self.headers.get("X-Forwarded-Uri") or "").path)
            if (".." not in raw.split("/") and "." not in raw.split("/") and "\\" not in raw
                    and (link_ok(raw, q1) or link_ok(raw, q2))):
                log("скачивание по ссылке", raw[:150])
                return self.send_json({"ok": True})
            return self.deny(403, "ссылка устарела или повреждена — попросите новую")
        if who is None:
            return self.deny(403, "телевизор не привязан к боту — ⚙ → Telegram-бот")
        uid, did = who
        if p == "/api/library.json":
            return self.send_json(library_for(uid, did))
        if p == "/api/auth":
            # Caddy forward_auth: пропускать ли запрос к /media/… или /thumbs/… (адрес — в X-Forwarded-Uri)
            raw = unquote(urlparse(self.headers.get("X-Forwarded-Uri") or "").path)
            parts = [x for x in raw.split("/") if x not in ("",)]
            if ".." in parts or "." in parts or "\\" in raw:
                return self.deny()
            if len(parts) >= 2 and parts[0] == "media":
                if parts[1] in AUTH.visible_keys(uid, did):
                    mark_viewed(parts[1])
                    return self.send_json({"ok": True})
                return self.deny()
            if len(parts) >= 2 and parts[0] == "thumbs":
                return self.send_json({"ok": True})
            return self.deny()
        if p == "/api/img":
            ref = (parse_qs(u.query).get("u") or [""])[0]
            f = _path("img/" + hashlib.sha1(ref.encode()).hexdigest() + ".jpg")
            if not os.path.isfile(f):
                return self.send_json({"error": "нет картинки"}, 404)
            with open(f, "rb") as fh:
                data = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "public, max-age=604800")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            return
        self.send_json({"error": "нет такого метода"}, 404)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def main():
    global CONF, TR
    CONF = load_conf()
    os.makedirs(CONF["data"], exist_ok=True)
    TR = Transmission(CONF["rpc"])
    threading.Thread(target=janitor_loop, daemon=True).start()
    srv = Server(("127.0.0.1", int(CONF["port"])), Handler)
    log("зеркало «%s» на 127.0.0.1:%s" % (CONF["name"], CONF["port"]))
    srv.serve_forever()


if __name__ == "__main__":
    main()

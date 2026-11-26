#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Индексатор ЧимаТВ: сканирует папку с видео и пишет library.json + обложки.

Раскладка как дома:
  media/film1/film1.mkv               → фильм «film1»
  media/film2.mkv                     → фильм «film2»
  media/serial1/s01/serials01e01.mkv  → сериал «serial1», сезон 1
  media/serial2/e01.mkv, e02.mkv      → сериал без сезонов

Режимы:
  chima_indexer.py --once   — один проход
  chima_indexer.py          — работать постоянно: пересканировать каждые 10 минут
                              или сразу, когда бот создаст файл-флаг /var/lib/chima/reindex
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import chima_tmdb
except Exception:  # модуль постеров необязателен
    chima_tmdb = None

MEDIA = os.environ.get("CHIMA_MEDIA", "/srv/media")
DATA = os.environ.get("CHIMA_DATA", "/var/lib/chima")
THUMBS = os.path.join(DATA, "thumbs")
LIBRARY = os.path.join(DATA, "library.json")
FLAG = os.path.join(DATA, "reindex")
PERIOD = 600

VIDEO_EXT = {"mkv", "mp4", "m4v", "avi", "mov", "ts", "m2ts", "mts", "wmv",
             "mpg", "mpeg", "webm", "flv", "3gp", "vob"}
SEASON_ONLY = re.compile(r"^\s*(?:s|season|сезон)?\s*[._-]?\s*0*(\d{1,2})\s*$", re.I)
SEASON_ANY = re.compile(r"(?:^|[\s._-])(?:s|season|сезон)\s*[._-]?\s*0*(\d{1,2})(?:$|[\s._-])", re.I)
EPISODE = re.compile(r"s\d{1,2}\s*e\d{1,3}|\b\d{1,2}x\d{2}\b|(?:серия|episode|ep)\s*\d+", re.I)
CHUNK = re.compile(r"\d+|\D+")


def natural(s):
    return [(0, int(c), "") if c.isdigit() else (1, 0, c) for c in CHUNK.findall(s.lower())]


def is_video(name):
    return not name.startswith(".") and name.rsplit(".", 1)[-1].lower() in VIDEO_EXT and "." in name


def clean(name):
    base = name
    if "." in base and base.rsplit(".", 1)[-1].lower() in VIDEO_EXT:
        base = base.rsplit(".", 1)[0]
    t = base.replace("_", " ")
    if " " not in t:
        t = t.replace(".", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t or name


def looks_like_season(name):
    return bool(SEASON_ONLY.match(name) or SEASON_ANY.search(name))


def listdir(path):
    try:
        names = [n for n in os.listdir(path) if not n.startswith(".")]
    except OSError:
        return [], []
    dirs = sorted([n for n in names if os.path.isdir(os.path.join(path, n))], key=natural)
    vids = sorted([n for n in names if is_video(n) and os.path.isfile(os.path.join(path, n))], key=natural)
    return dirs, vids


def collect(path, rel, depth):
    """Все видео в папке и подпапках (до depth уровней)."""
    dirs, vids = listdir(path)
    out = [f"{rel}/{v}" for v in vids]
    if depth > 0:
        for d in dirs:
            out += collect(os.path.join(path, d), f"{rel}/{d}", depth - 1)
    return out


# ── длительность и обложки ──

_probe_cache = {}


def probe_duration(full):
    st = os.stat(full)
    key = (full, st.st_size, int(st.st_mtime))
    if key in _probe_cache:
        return _probe_cache[key]
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", full],
            capture_output=True, text=True, timeout=60).stdout.strip()
        ms = int(float(out) * 1000)
    except Exception:
        ms = 0
    _probe_cache[key] = ms
    return ms


def thumb_name(rel):
    return hashlib.sha1(rel.encode("utf-8")).hexdigest() + ".jpg"


def make_thumb(full, rel, dur_ms):
    name = thumb_name(rel)
    dst = os.path.join(THUMBS, name)
    if os.path.exists(dst):
        return name
    fail = dst + ".fail"
    if os.path.exists(fail):
        return None
    at = min(dur_ms // 10, 300_000) / 1000 if dur_ms > 0 else 60
    tmp = dst + ".tmp.jpg"
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{at:.1f}", "-i", full,
             "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", tmp],
            capture_output=True, timeout=120)
        if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, dst)
            return name
    except Exception:
        pass
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
        open(fail, "w").close()
    except OSError:
        pass
    return None


def video(title_key, season, rel):
    full = os.path.join(MEDIA, rel.lstrip("/"))
    dur = probe_duration(full)
    return {
        "key": f"{title_key}/{season}/{os.path.basename(rel)}",
        "title": os.path.basename(rel),
        "path": rel.lstrip("/"),
        "durationMs": dur,
        "thumb": make_thumb(full, rel, dur),
    }


def movie(key, name, rel):
    return {"key": key, "name": clean(name), "isSeries": False,
            "seasons": [{"name": "", "episodes": [video(key, "", rel)]}]}


def build_title(dirname):
    path = os.path.join(MEDIA, dirname)
    dirs, vids = listdir(path)
    key = dirname
    if not dirs:
        if not vids:
            return None
        if len(vids) == 1:
            return movie(key, dirname, f"{dirname}/{vids[0]}")
        return {"key": key, "name": clean(dirname), "isSeries": True,
                "seasons": [{"name": "", "episodes": [video(key, "", f"{dirname}/{v}") for v in vids]}]}

    season_like = any(looks_like_season(d) for d in dirs)
    if len(vids) == 1 and not season_like and not EPISODE.search(vids[0]):
        return movie(key, dirname, f"{dirname}/{vids[0]}")  # фильм с папками Extras/Subs

    seasons = []
    if vids:
        seasons.append({"name": "", "episodes": [video(key, "", f"{dirname}/{v}") for v in vids]})
    for d in dirs:
        eps = collect(os.path.join(path, d), f"{dirname}/{d}", 2)
        if eps:
            seasons.append({"name": d, "episodes": [video(key, d, e) for e in eps]})
    if not seasons:
        return None
    if len(seasons) == 1 and len(seasons[0]["episodes"]) == 1:
        ep = seasons[0]["episodes"][0]
        return {"key": key, "name": clean(dirname), "isSeries": False,
                "seasons": [{"name": "", "episodes": [ep]}]}
    return {"key": key, "name": clean(dirname), "isSeries": True, "seasons": seasons}


def scan():
    os.makedirs(THUMBS, exist_ok=True)
    dirs, vids = listdir(MEDIA)
    titles = []
    for name in sorted(dirs + vids, key=natural):
        try:
            if name in dirs:
                t = build_title(name)
            else:
                t = movie(name, name, name)
            if t:
                titles.append(t)
        except Exception as e:
            print(f"ошибка в «{name}»: {e}", file=sys.stderr)
    # Постеры с TMDB (если задан токен); результаты кешируются
    if chima_tmdb and chima_tmdb.token():
        cache = chima_tmdb.load_cache()
        try:
            import chima_ytdl
            clips = chima_ytdl.media_names()  # видео по ссылкам — не фильмы, постер из кинобазы им не нужен
        except Exception:
            clips = set()
        for t in titles:
            if t["key"] in clips:
                continue
            try:
                info = chima_tmdb.lookup(t["key"], cache)
            except Exception:
                info = None
            if info and info.get("poster"):
                t["poster"] = info["poster"]
            # Русское название с TMDB (ключ и путь к файлам не меняются — позиции просмотра сохраняются)
            if info and info.get("title"):
                t["name"] = info["title"] + (" (%s)" % info["year"] if info.get("year") else "")
        chima_tmdb.save_cache(cache)
    data = {"version": 1, "updated": int(time.time()), "titles": titles}
    tmp = LIBRARY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, LIBRARY)
    # удаляем обложки от удалённых файлов
    used = {e["thumb"] for t in titles for s in t["seasons"] for e in s["episodes"] if e["thumb"]}
    for n in os.listdir(THUMBS):
        base = n[:-5] if n.endswith(".fail") else n
        if base not in used and not n.endswith(".tmp.jpg"):
            path = os.path.join(THUMBS, base)
            # .fail удаляем раз в сутки, чтобы попробовать сделать обложку ещё раз
            if n.endswith(".fail"):
                try:
                    if time.time() - os.path.getmtime(os.path.join(THUMBS, n)) > 86400:
                        os.remove(os.path.join(THUMBS, n))
                except OSError:
                    pass
                continue
            try:
                os.remove(path)
            except OSError:
                pass
    return len(titles)


def main():
    if "--once" in sys.argv:
        print(f"найдено: {scan()}")
        return
    last = 0.0
    while True:
        flagged = os.path.exists(FLAG)
        if flagged or time.time() - last > PERIOD:
            if flagged:
                try:
                    os.remove(FLAG)
                except OSError:
                    pass
            try:
                n = scan()
                print(f"{time.strftime('%H:%M:%S')} библиотека: {n}", flush=True)
            except Exception as e:
                print(f"ошибка сканирования: {e}", file=sys.stderr, flush=True)
            last = time.time()
        time.sleep(15)


if __name__ == "__main__":
    main()

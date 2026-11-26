#!/bin/bash
# Зеркало ЧимаТВ в России (chima-mirror). Команду с вашими значениями присылает бот: /mirror add
#   curl -fsSL https://cheema.tv/mirror/install.sh | sudo MIRROR_DOMAIN=m1.cheema.tv MIRROR_NAME=Москва MIRROR_SECRET=… bash
# Повторный запуск безопасен: обновит программу, настройки и фильмы останутся.
# Чужое на сервере не трогаем: свои сайты/боты, чужой Caddy/nginx и чужой Transmission остаются как были
# (свой Transmission — отдельная служба chima-transmission, свой сайт — отдельный файл для Caddy).
set -e
[ "$(id -u)" = 0 ] || { echo "Запустите от root (sudo)."; exit 1; }
: "${MIRROR_DOMAIN:?не задан MIRROR_DOMAIN (адрес зеркала, например m1.cheema.tv)}"
: "${MIRROR_SECRET:?не задан MIRROR_SECRET (его даёт бот в команде /mirror add)}"
MIRROR_NAME=${MIRROR_NAME:-$MIRROR_DOMAIN}
SRC=${CHIMA_MIRROR_SRC:-https://cheema.tv/mirror}
MEDIA=${MIRROR_MEDIA:-/srv/media}
DATA=/var/lib/chima-mirror
APP=/opt/chima-mirror
TUSER=debian-transmission
export DEBIAN_FRONTEND=noninteractive

echo "▶ Зеркало ЧимаТВ «$MIRROR_NAME» ($MIRROR_DOMAIN)"
echo "== 1/6 Пакеты"
apt-get update -q
dpkg -s transmission-daemon >/dev/null 2>&1 && HAD_TR=1 || HAD_TR=0
dpkg -s caddy >/dev/null 2>&1 && HAD_CADDY=1 || HAD_CADDY=0
apt-get install -y -q transmission-daemon ffmpeg python3 curl ufw openssl
if [ "$HAD_TR" = 0 ]; then   # пакет поставили мы — стандартную службу не запускаем, у зеркала своя
    systemctl disable --now transmission-daemon >/dev/null 2>&1 || true
fi
if ! command -v caddy >/dev/null; then
    apt-get install -y -q caddy || {
        apt-get install -y -q debian-keyring debian-archive-keyring apt-transport-https gnupg
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
        apt-get update -q
        apt-get install -y -q caddy
    }
fi
id "$TUSER" >/dev/null 2>&1 || useradd --system --home /var/lib/transmission-daemon --shell /usr/sbin/nologin "$TUSER"

echo "== 2/6 Папки и программа"
mkdir -p "$MEDIA" /srv/incomplete "$DATA/thumbs" "$DATA/img" "$APP"
for f in chima_mirror.py chima_indexer.py; do
    curl -fsSL "$SRC/$f" -o "$APP/$f.new"
    python3 -m py_compile "$APP/$f.new"
    mv "$APP/$f.new" "$APP/$f"
done
chown -R "$TUSER:$TUSER" "$MEDIA" /srv/incomplete "$DATA"
chmod 750 "$DATA"
usermod -aG "$TUSER" caddy 2>/dev/null || true
# Какой Transmission использует зеркало: у уже установленных зеркал — тот, что был (не переезжаем, чтобы не потерять закачки),
# у новых — свой отдельный (chima-transmission, порт 9092), общий не трогаем.
RPC_PORT=9092; PEER_PORT=51423; TMODE=own
if [ -f /etc/chima-mirror.json ]; then
    OLD_RPC=$(python3 -c 'import json;print(json.load(open("/etc/chima-mirror.json")).get("rpc",""))' 2>/dev/null || true)
    case "$OLD_RPC" in
        *:9092/*) ;;
        *:*) TMODE=stock; RPC_PORT=9091; PEER_PORT=51413 ;;
    esac
fi
TDIR=$DATA/transmission
cat > /etc/chima-mirror.json.new <<JSON
{"name": "$MIRROR_NAME", "domain": "$MIRROR_DOMAIN", "secret": "$MIRROR_SECRET", "port": 8095, "media": "$MEDIA", "data": "$DATA",
 "rpc": "http://127.0.0.1:$RPC_PORT/transmission/rpc", "reserve_gb": 20, "ttl_days": 3}
JSON
chown root:"$TUSER" /etc/chima-mirror.json.new
chmod 640 /etc/chima-mirror.json.new
mv /etc/chima-mirror.json.new /etc/chima-mirror.json

echo "== 3/6 Transmission"
# AppArmor (Ubuntu): штатный профиль Transmission не пускает в /srv — разрешаем свои папки
# (иначе «Permission denied», хотя права папок верные). Имя файла профиля ищем, оно бывает разным.
AAF=$(grep -rl "profile transmission-daemon" /etc/apparmor.d/ 2>/dev/null | head -1 || true)
if [ -n "$AAF" ]; then
    mkdir -p /etc/apparmor.d/local
    cat > /etc/apparmor.d/local/transmission-daemon <<AA
$MEDIA/ r,
$MEDIA/** rwk,
/srv/incomplete/ r,
/srv/incomplete/** rwk,
$TDIR/ r,
$TDIR/** rwk,
AA
    apparmor_parser -r "$AAF" 2>/dev/null || true
fi
if [ "$TMODE" = own ]; then
    TSVC=chima-transmission
    mkdir -p "$TDIR"; chown "$TUSER:$TUSER" "$TDIR"
    cat > /etc/systemd/system/chima-transmission.service <<UNIT
[Unit]
Description=ЧимаТВ: Transmission зеркала (отдельный от системного)
After=network-online.target

[Service]
User=$TUSER
UMask=0022
ExecStart=/usr/bin/transmission-daemon -f --config-dir $TDIR
Restart=always
RestartSec=5
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
UNIT
    TSET="$TDIR/settings.json"
else
    TSVC=transmission-daemon
    mkdir -p /etc/systemd/system/transmission-daemon.service.d
    printf '[Service]\nNoNewPrivileges=yes\n' > /etc/systemd/system/transmission-daemon.service.d/chima-hardening.conf
    TSET=/etc/transmission-daemon/settings.json
fi
systemctl daemon-reload
systemctl stop "$TSVC" 2>/dev/null || true
python3 - "$MEDIA" "$TSET" "$RPC_PORT" "$PEER_PORT" <<'PY'
import json, os, sys
media, p, rpc, peer = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
try:
    s = json.load(open(p))
except (OSError, ValueError):
    s = {}
s.update({
    "download-dir": media, "incomplete-dir": "/srv/incomplete", "incomplete-dir-enabled": True,
    "rpc-enabled": True, "rpc-bind-address": "127.0.0.1", "rpc-port": rpc,
    "rpc-authentication-required": False, "rpc-whitelist-enabled": True, "rpc-whitelist": "127.0.0.1,::1",
    "rpc-host-whitelist-enabled": False, "umask": 18, "peer-port": peer, "port-forwarding-enabled": False,
    "rename-partial-files": True, "speed-limit-up": 10240, "speed-limit-up-enabled": True,
    "ratio-limit": 1.0, "ratio-limit-enabled": True, "peer-limit-global": 300, "peer-limit-per-torrent": 60,
})
json.dump(s, open(p, "w"), indent=4, ensure_ascii=False)
PY
chown "$TUSER:$TUSER" "$TSET" 2>/dev/null || true
systemctl enable --now "$TSVC"

echo "== 4/6 Службы"
cat > /etc/systemd/system/chima-mirror.service <<UNIT
[Unit]
Description=ЧимаТВ: зеркало (команды от NL, отдача фильмов)
After=network-online.target $TSVC.service

[Service]
User=$TUSER
UMask=0027
ExecStart=/usr/bin/python3 $APP/chima_mirror.py
Restart=always
RestartSec=3
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/chima-mirror-indexer.service <<UNIT
[Unit]
Description=ЧимаТВ: индексатор зеркала
After=network-online.target

[Service]
User=$TUSER
UMask=0027
Environment=CHIMA_MEDIA=$MEDIA
Environment=CHIMA_DATA=$DATA
ExecStart=/usr/bin/python3 $APP/chima_indexer.py
Restart=always
RestartSec=10
Nice=10
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
UNIT
# Самообновление по кнопке в боте (/mirror → «Обновить зеркала»): сервис зеркала (без прав root) кладёт файл-флаг,
# systemd-path от root замечает его и заново запускает этот же установщик (настройки берёт из /etc/chima-mirror.json).
cat > "$APP/update.sh" <<'UPD'
#!/bin/bash
LOG=/var/lib/chima-mirror/update.log
{
  echo "=== $(date '+%F %T') обновление зеркала ==="
  eval "$(python3 - <<'PY'
import json, re, shlex
c = json.load(open("/etc/chima-mirror.json"))
d = c.get("domain") or ""
if not d:
    m = re.match(r"\s*([A-Za-z0-9.-]+)\s*\{", [l for l in open("/etc/caddy/Caddyfile") if l.strip() and not l.startswith("#")][0])
    d = m.group(1) if m else ""
print("export MIRROR_DOMAIN=%s MIRROR_NAME=%s MIRROR_SECRET=%s MIRROR_MEDIA=%s" % tuple(
    shlex.quote(x) for x in (d, c.get("name", ""), c.get("secret", ""), c.get("media", "/srv/media"))))
PY
)"
  curl -fsSL "${CHIMA_MIRROR_SRC:-https://cheema.tv/mirror}/install.sh" -o /tmp/chima-mirror-install.sh && bash /tmp/chima-mirror-install.sh
  echo "=== конец (код $?) ==="
} >> "$LOG" 2>&1
UPD
chmod 755 "$APP/update.sh"
cat > /etc/systemd/system/chima-mirror-update.service <<UNIT
[Unit]
Description=ЧимаТВ: обновление зеркала по запросу от NL

[Service]
Type=oneshot
ExecStartPre=/bin/rm -f $DATA/update_request
ExecStart=$APP/update.sh
UNIT
cat > /etc/systemd/system/chima-mirror-update.path <<UNIT
[Unit]
Description=ЧимаТВ: ждём запрос на обновление зеркала

[Path]
PathExists=$DATA/update_request

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now chima-mirror-update.path >/dev/null 2>&1 || true
systemctl enable chima-mirror chima-mirror-indexer >/dev/null
systemctl restart chima-mirror chima-mirror-indexer

echo "== 5/6 HTTPS (Caddy)"
mkdir -p /var/log/caddy
SITE=/etc/caddy/chima-mirror.caddy
IMPORT="import $SITE"
# Порты 80/443 заняты чужой программой (nginx, apache, docker…)? Тогда Caddy не трогаем и чужое не останавливаем.
FOREIGN=$(ss -ltnpH 2>/dev/null | awk '$4 ~ /:(80|443)$/' | grep -v '"caddy"' || true)
if [ -n "$FOREIGN" ]; then
    echo "   ⚠ порты 80/443 уже заняты другой программой:"; echo "$FOREIGN" | sed 's/^/     /'
    echo "   Чужое не останавливаю. Зеркало (служба 8095) работает, но HTTPS-адрес $MIRROR_DOMAIN не настроен."
    echo "   Нужно добавить в вашу программу (nginx и т.п.) адрес $MIRROR_DOMAIN → 127.0.0.1:8095 (api, m, d) и отдачу файлов — напишите в чат, помогу."
    SKIP_CADDY=1
fi
if [ -z "$SKIP_CADDY" ]; then
cat > $SITE.new <<CADDY
# Зеркало ЧимаТВ: пропуск телевизора проверяет chima-mirror (forward_auth), файлы отдаёт Caddy.
# Это отдельный файл — остальные сайты в /etc/caddy/Caddyfile не затрагиваются.
$MIRROR_DOMAIN {
	handle /api/* {
		reverse_proxy 127.0.0.1:8095
	}
	handle /m/* {
		reverse_proxy 127.0.0.1:8095
	}
	handle /d/* {
		reverse_proxy 127.0.0.1:8095
	}
	@short path_regexp short ^/[a-hj-km-np-z2-9]{3}$
	handle @short {
		reverse_proxy 127.0.0.1:8095
	}
	handle {
		forward_auth 127.0.0.1:8095 {
			uri /api/auth
			header_up X-Chima-Orig {http.request.orig_uri}
		}
		handle_path /thumbs/* {
			header Cache-Control "public, max-age=604800"
			root * $DATA/thumbs
			file_server
		}
		handle_path /media/* {
			root * $MEDIA
			file_server
		}
		respond "ЧимаТВ" 403
	}
}
CADDY
if [ -f /etc/caddy/Caddyfile ] && head -n1 /etc/caddy/Caddyfile | grep -q "Зеркало ЧимаТВ"; then
    # Caddyfile целиком записан старой версией установщика (только наш сайт) — заменяем одной строкой подключения
    mv /etc/caddy/Caddyfile /etc/caddy/Caddyfile.chima-old
    echo "$IMPORT" > /etc/caddy/Caddyfile
elif [ ! -f /etc/caddy/Caddyfile ]; then
    echo "$IMPORT" > /etc/caddy/Caddyfile
elif ! grep -qF "$SITE" /etc/caddy/Caddyfile; then
    [ -f /etc/caddy/Caddyfile.chima-backup ] || cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.chima-backup
    printf '\n%s\n' "$IMPORT" >> /etc/caddy/Caddyfile
fi
[ -f $SITE ] && cp $SITE $SITE.prev
mv $SITE.new $SITE
if ! caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
    echo "!! Caddy не принял настройки (чужие сайты не тронуты):"; caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1 | tail -n 6
    if [ -f $SITE.prev ]; then mv $SITE.prev $SITE; else rm -f $SITE; fi
    [ -f /etc/caddy/Caddyfile.chima-backup ] && cp /etc/caddy/Caddyfile.chima-backup /etc/caddy/Caddyfile
    exit 1
fi
rm -f $SITE.prev
systemctl enable caddy >/dev/null
if systemctl is-active --quiet caddy; then systemctl reload caddy || systemctl restart caddy; else systemctl start caddy; fi
fi
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
    ufw allow $PEER_PORT/tcp >/dev/null; ufw allow $PEER_PORT/udp >/dev/null
fi

# быстрее и ровнее отдача видео на длинных маршрутах с потерями: очередь fq + BBR
cat > /etc/sysctl.d/99-chima-bbr.conf <<'SYS'
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
SYS
sysctl --system >/dev/null 2>&1 || true

echo "== 6/6 Проверка"
sleep 3
for s in "$TSVC" chima-mirror chima-mirror-indexer caddy; do
    systemctl is-active --quiet "$s" && echo "  ✓ $s работает" || { echo "  ✗ $s не запустился:"; journalctl -u "$s" -n 8 --no-pager; }
done
curl -s -m 5 http://127.0.0.1:8095/api/ping | sed 's/^/  ответ зеркала: /'; echo
MYIP=$(curl -s -m 5 https://api.ipify.org || true)
DNSIP=$(getent hosts "$MIRROR_DOMAIN" | awk '{print $1; exit}')
echo "  внешний IP этого сервера: ${MYIP:-не определён}; адрес $MIRROR_DOMAIN указывает на: ${DNSIP:-не найден}"
[ -n "$MYIP" ] && [ "$MYIP" = "$DNSIP" ] || echo "  ⚠ DNS не совпадает с IP — добавьте запись A для $MIRROR_DOMAIN → ${MYIP:-IP сервера} (без прокси), иначе сертификат не выпустится"
curl -s -m 15 "https://$MIRROR_DOMAIN/api/ping" | sed 's/^/  по HTTPS: /' || echo "  ✗ HTTPS пока не отвечает (сертификат выпускается до минуты; порты 80 и 443 должны быть открыты на роутере)"
echo
echo "Готово. Теперь в боте: /mirror — проверить зеркало."

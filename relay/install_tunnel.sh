#!/bin/bash
# Сервер в России → сервер медиатеки через VPN (Xray VLESS Reality).
# Нужен, когда с этого сервера до kino.cheema.tv напрямую данные не идут (адрес заблокирован в РФ).
# Мост (chima-relay) принимает телевизоры у себя и пересылает их трафик через Xray в Нидерланды.
#
#   1) положите ссылки vless:// (по одной в строке) в файл /root/links.txt
#   2) sudo bash install_tunnel.sh
# Повторный запуск безопасен.
set -e
[ "$(id -u)" = 0 ] || { echo "Запустите от root"; exit 1; }
LINKS=${1:-/root/links.txt}
[ -s "$LINKS" ] || { echo "Нет файла со ссылками: $LINKS (по одной vless:// в строке)"; exit 1; }
[ -f /opt/chima-relay/chima_relay.py ] || { echo "Сначала поставьте мост: curl -fsSL https://cheema.tv/relay/install.sh | sudo bash"; exit 1; }
command -v python3 >/dev/null && command -v curl >/dev/null || { apt-get update -qq; apt-get install -y -qq python3 curl unzip >/dev/null; }

echo "== 1/4 Xray"
if ! command -v xray >/dev/null; then
    bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install >/dev/null
fi
xray version | head -n 1

echo "== 2/4 Настройка туннеля"
python3 - "$LINKS" <<'PY'
import json, sys, urllib.parse as u
outs = []
for i, line in enumerate(l.strip() for l in open(sys.argv[1], encoding="utf-8")):
    if not line.startswith("vless://"):
        continue
    x = u.urlparse(line)
    q = {k: v[0] for k, v in u.parse_qs(x.query).items()}
    user = {"id": x.username, "encryption": q.get("encryption", "none")}
    if q.get("flow"):
        user["flow"] = q["flow"]
    net = q.get("type", "tcp")
    st = {"network": net, "security": q.get("security", "none")}
    if st["security"] == "reality":
        st["realitySettings"] = {"serverName": q.get("sni", ""), "publicKey": q.get("pbk", ""),
                                 "shortId": q.get("sid", ""), "fingerprint": q.get("fp", "chrome"),
                                 "spiderX": q.get("spx", "/")}
    elif st["security"] == "tls":
        st["tlsSettings"] = {"serverName": q.get("sni", x.hostname), "fingerprint": q.get("fp", "chrome")}
    if net == "grpc":
        st["grpcSettings"] = {"serviceName": q.get("serviceName", ""), "multiMode": q.get("mode") == "multi"}
    outs.append({"tag": "p%d" % len(outs), "protocol": "vless",
                 "settings": {"vnext": [{"address": x.hostname, "port": x.port or 443, "users": [user]}]},
                 "streamSettings": st})
if not outs:
    sys.exit("в файле нет ссылок vless://")
cfg = {
    "log": {"loglevel": "warning"},
    "inbounds": [{"tag": "in", "listen": "127.0.0.1", "port": 10443, "protocol": "dokodemo-door",
                  "settings": {"address": "kino.cheema.tv", "port": 443, "network": "tcp"}}],
    "outbounds": outs + [{"tag": "direct", "protocol": "freedom"}],
    "observatory": {"subjectSelector": ["p"], "probeUrl": "https://www.gstatic.com/generate_204", "probeInterval": "1m"},
    "routing": {"balancers": [{"tag": "b", "selector": ["p"], "strategy": {"type": "leastPing"}}],
                "rules": [{"type": "field", "inboundTag": ["in"], "balancerTag": "b"}]},
}
json.dump(cfg, open("/usr/local/etc/xray/config.json", "w"), indent=1)
print("профилей в туннеле:", len(outs))
PY
xray run -test -config /usr/local/etc/xray/config.json >/dev/null
systemctl enable -q xray
systemctl restart xray
sleep 2
systemctl is-active --quiet xray && echo "   Xray работает" || { journalctl -u xray -n 20 --no-pager; exit 1; }

echo "== 3/4 Мост: включаю путь через туннель"
python3 - <<'PY'
import json
p = "/etc/chima-relay.json"
try:
    c = json.load(open(p))
except Exception:
    c = {"port": 443, "allow": ["kino.cheema.tv:443"]}
c["via"] = "127.0.0.1:10443"
json.dump(c, open(p, "w"))
PY
# если мост старой версии (до «via») — докладываем поддержку на месте
if ! grep -q 'cfg.get("via")' /opt/chima-relay/chima_relay.py; then
python3 - <<'PY'
p = "/opt/chima-relay/chima_relay.py"
s = open(p, encoding="utf-8").read()
old = "            upstream = socket.create_connection((host, int(port)), timeout=15)"
new = ('            via = cfg.get("via")\n            if via:\n                vh, _, vp = str(via).rpartition(":")\n'
       '                upstream = socket.create_connection((vh, int(vp)), timeout=15)\n            else:\n'
       '                upstream = socket.create_connection((host, int(port)), timeout=15)')
assert old in s, "не нашёл место для правки"
open(p, "w", encoding="utf-8").write(s.replace(old, new, 1))
PY
fi
systemctl restart chima-relay
sleep 2

echo "== 4/4 Проверка"
for i in 1 2 3; do
  curl -s -m 25 -o /dev/null -x http://127.0.0.1:443 https://kino.cheema.tv/app/version.json \
    -w "   через мост и туннель: код %{http_code} за %{time_total} с\n" || echo "   ✗ не прошло (попытка $i)"
done
curl -s -m 60 -o /dev/null -x http://127.0.0.1:443 https://kino.cheema.tv/app/ChimaTV.apk \
  -w "   скорость скачивания: %{speed_download} байт/с\n" || true

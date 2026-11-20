#!/bin/bash
# Сервер ЧимаТВ в России (chima-relay). Ставится (и обновляется) одной командой:
#   curl -fsSL https://cheema.tv/relay/install.sh | sudo bash
# Повторный запуск безопасен: обновит программу и перезапустит службу, настройки останутся.
set -e
[ "$(id -u)" = 0 ] || { echo "Запустите от root: curl -fsSL https://cheema.tv/relay/install.sh | sudo bash"; exit 1; }
SRC=${CHIMA_RELAY_SRC:-https://cheema.tv/relay}
echo "▶ Ставлю сервер ЧимаТВ (chima-relay)…"
command -v python3 >/dev/null && command -v curl >/dev/null || { apt-get update -qq; apt-get install -y -qq python3 curl >/dev/null; }

mkdir -p /opt/chima-relay
curl -fsSL "$SRC/chima_relay.py" -o /opt/chima-relay/chima_relay.py.new
python3 -m py_compile /opt/chima-relay/chima_relay.py.new
mv /opt/chima-relay/chima_relay.py.new /opt/chima-relay/chima_relay.py
id chima-relay >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin chima-relay
[ -f /etc/chima-relay.json ] || echo '{"port": 443, "allow": ["kino.cheema.tv:443"]}' > /etc/chima-relay.json

cat > /etc/systemd/system/chima-relay.service <<'UNIT'
[Unit]
Description=ЧимаТВ: сервер для телевизоров в России
After=network-online.target
Wants=network-online.target

[Service]
User=chima-relay
ExecStart=/usr/bin/python3 /opt/chima-relay/chima_relay.py
Restart=always
RestartSec=3
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
UNIT

# быстрее и ровнее на домашних сетях с потерями: очередь fq + BBR
cat > /etc/sysctl.d/90-chima-relay.conf <<'SYS'
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.core.somaxconn = 4096
SYS
sysctl -q --system >/dev/null 2>&1 || true

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow 443/tcp >/dev/null; fi

systemctl daemon-reload
systemctl enable -q chima-relay
systemctl restart chima-relay
sleep 2

echo "▶ Проверка:"
systemctl is-active --quiet chima-relay && echo "  служба работает" || { echo "  ✗ служба не запустилась:"; journalctl -u chima-relay -n 20 --no-pager; exit 1; }
curl -s -m 5 http://127.0.0.1:443/health | sed 's/^/  ответ: /'
curl -s -m 20 -o /dev/null -x http://127.0.0.1:443 https://kino.cheema.tv/app/ChimaTV.apk \
  -w "  до сервера медиатеки: код %{http_code} · %{size_download} байт за %{time_total} с\n" || echo "  ✗ сервер медиатеки отсюда недоступен"
IP=$(curl -s -m 8 https://ipinfo.io/ip || curl -s -m 8 ifconfig.me)
echo
echo "✅ Готово. Теперь отправьте боту ЧимаТВ (из аккаунта администратора) команду:"
echo "   /relay $IP Москва"

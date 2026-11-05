#!/usr/bin/env bash
# RouterCat 安装为 systemd 服务（开机自启）
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${1:-8848}"
UNIT="/etc/systemd/system/routercat.service"
RUN_USER="${SUDO_USER:-$(id -un)}"

if [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
else
  SUDO=""
fi

echo "安装目录 : $DIR"
echo "运行用户 : $RUN_USER"
echo "监听端口 : $PORT"

$SUDO tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=RouterCat 路由器状态监控
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$DIR
ExecStart=$(command -v python3) -u $DIR/server.py --port $PORT
Restart=always
RestartSec=5
# ICMP 探测依赖 ping 命令与 cap_net_raw
AmbientCapabilities=CAP_NET_RAW
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable --now routercat.service
sleep 2
$SUDO systemctl --no-pager --lines=8 status routercat.service || true

echo
echo "完成。浏览器访问 http://127.0.0.1:$PORT"
echo "查看日志：journalctl -u routercat -f"
echo "停止服务：sudo systemctl stop routercat"

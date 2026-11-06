#!/usr/bin/env bash
# Instala los servicios y timers de systemd (de usuario) con la ruta de esta carpeta.
#   ./instalar.sh            instala y activa todo
#   ./instalar.sh --sin-panel  sin el servidor del panel (si no vas a usar Tailscale)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(command -v python3)"
DESTINO="${DESTINO:-$HOME/.config/systemd/user}"

mkdir -p "$DESTINO"
for f in "$DIR"/systemd/*; do
  sed -e "s#__DIR__#$DIR#g" -e "s#__PYTHON__#$PYTHON#g" "$f" > "$DESTINO/$(basename "$f")"
done
echo "Unidades copiadas a $DESTINO"

[ "${SIN_ACTIVAR:-}" = 1 ] && exit 0
systemctl --user daemon-reload
systemctl --user enable --now tec-agenda.timer tec-resumen.timer tec-cierre.timer
if [ "${1:-}" != "--sin-panel" ]; then
  systemctl --user enable --now tec-panel.service
  echo "Panel en http://127.0.0.1:8765  (para el celular: tailscale serve --bg 8765)"
fi
systemctl --user list-timers --no-pager | grep -E "NEXT|tec-"

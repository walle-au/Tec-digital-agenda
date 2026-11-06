#!/usr/bin/env python3
"""Servidor mínimo que entrega SOLO panel.html, y solo dentro de esta compu (127.0.0.1).
Tailscale lo pasa a tus dispositivos con:  tailscale serve --bg 8765
Cualquier otra ruta da 404, así que .env y agenda.db nunca se pueden pedir."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PANEL = Path(__file__).resolve().parent / "panel.html"
PUERTO = 8765


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] not in ("/", "/panel.html"):
            self.send_error(404)
            return
        try:
            datos = PANEL.read_bytes()
        except OSError:
            self.send_error(503, "El panel todavía no existe")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")  # siempre la versión más nueva
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PUERTO), Handler).serve_forever()

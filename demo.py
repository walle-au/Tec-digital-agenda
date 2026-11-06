#!/usr/bin/env python3
"""Genera docs/panel-demo.html con datos inventados (para capturas y para probar el panel sin cuenta).

Uso:  python3 demo.py
"""
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import tec_agenda as t

AHORA = datetime.now(t.TZ).replace(second=0, microsecond=0)


def en(dias=0, horas=0, hora=None):
    d = AHORA + timedelta(days=dias, hours=horas)
    return (d.replace(hour=hora, minute=0) if hora is not None else d).isoformat()


TAREAS = [
    # (curso, categoría, nombre, valor, vence, entregada)
    ("Arquitectura de computadoras I GR 20", "Paper", "Abstract y referencias", "3.00", (AHORA + timedelta(hours=5)).replace(minute=0).isoformat(), False),
    ("Señales y sistemas GR 20 · Tutoría", "Tareas", "Tarea 10", "0.00", en(2, hora=12), False),
    ("Control automático GR 20", "Laboratorio", "Informe de laboratorio 3", "10.00", en(3, hora=23), True),
    ("Arquitectura de computadoras I GR 20", "Proyectos", "Proyecto 4", "10.00", en(9, hora=18), False),
    ("Normalización técnica GR 20", "Proyectos", "Informe proyecto 4", "15.00", en(12, hora=23), False),
    ("Control automático GR 20", "Evaluación Unidad 4", "Diseño de controladores", "30.00", en(17, hora=23), False),
    ("Arquitectura de computadoras I GR 20", "Proyectos", "Proyecto 5", "10.00", None, False),
]
EXAMENES = [
    ("Señales y sistemas GR 20", "Examen final", en(20), False, "programa del curso", "40.00"),
    ("Arquitectura de computadoras I GR 20", "Examen 2", en(21, hora=18), True, "fechas.txt", ""),
]


def main():
    with tempfile.TemporaryDirectory() as d:
        t.DB_PATH = Path(d) / "demo.db"
        db = t.abrir_db()
        for i, (curso, cat, nombre, valor, vence, ent) in enumerate(TAREAS):
            db.execute("INSERT INTO tareas(id,curso,categoria,nombre,valor,vence,entregada,nota,url) VALUES(?,?,?,?,?,?,?,?,?)",
                       (str(i), curso, cat, nombre, valor, vence, int(ent), "", ""))
        for i, (curso, nombre, fecha, hora, fuente, valor) in enumerate(EXAMENES):
            db.execute("INSERT INTO examenes VALUES(?,?,?,?,?,?,?,?)",
                       (f"ex{i}", curso, nombre, fecha, int(hora), fuente, valor, ""))
        db.commit()
        t.PANEL_PATH = Path(__file__).resolve().parent / "docs" / "panel-demo.html"
        t.PANEL_PATH.parent.mkdir(exist_ok=True)
        t.generar_panel(db)
        asunto, texto, html = t.resumen_correo(db)
        (t.PANEL_PATH.parent / "correo-demo.html").write_text(html, encoding="utf-8")
        db.close()
    print(f"Listo: {t.PANEL_PATH}\nAsunto del correo de ejemplo: {asunto}")


if __name__ == "__main__":
    main()

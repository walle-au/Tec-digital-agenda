"""Pruebas de tec_agenda con páginas de ejemplo (sin conexión y sin datos reales).

Correr:  python3 -m unittest -v        (desde la carpeta del proyecto)
"""
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import servir_panel  # noqa: E402
import tec_agenda as t  # noqa: E402


# ---------------------------------------------------------------- páginas de ejemplo
def asignacion(assign_id, nombre, nota, fecha=None, archivo=False, valor="10.00", nota_obtenida="-- / 100"):
    """Una asignación con la misma estructura que usa la pestaña Evaluaciones."""
    campo = lambda titulo, valor: (f'<div class="gaap_content row"><p class="title_subsection">{titulo} :</p>'
                                   f'<div class="body_style"> {valor} </div></div>')
    return (f'<div assign_id="{assign_id}" class="container_accor"><div class="head_subaccord">'
            f'<span class="assignNameText">{nombre}</span>'
            f'<span class="text_right vt_grade_student"> {nota}</span></div>'
            + campo("Valor de la Asignación", valor)
            + campo("Nota obtenida", nota_obtenida)
            + (campo("Fecha de entrega", fecha) if fecha else "")
            + ('<span class="displayNameFile">entrega.pdf</span>' if archivo else "")
            + "</div>")


def categoria(nombre, *asignaciones):
    return (f'<div class="asign_student title_acor_grade"><span class="clase">{nombre} </span></div>'
            f'<div class="container_accor_detail">{"".join(asignaciones)}</div>')


EVALUACIONES = "<html><body>" + categoria(
    "Proyectos",
    asignacion("1", "Proyecto 4", "-- / 10.00", fecha="18/11/2026 18:00"),
    asignacion("2", "Proyecto 3", "-- / 10.00", fecha="30/10/2026 18:00", archivo=True),
) + categoria(
    "Exámenes",
    asignacion("3", "Examen 1", "14.07 / 15.00", valor="15.00", nota_obtenida="93.8 / 100"),
    asignacion("4", "Examen Final", "-- / 40.00", valor="40.00"),
) + "</body></html>"

# Igual que el portal real: el bloque "current" de cursos no se cierra bien y se "traga" los
# cursos de semestres anteriores; además hay un bloque "current" de comunidades antes.
PORTAL = """<html><body><ul>
<li name="current"><a href="/dotlrn/escuela/escuela.E/">Escuela</a></li>
<li name="current">SEMESTRE 2 2026<ul>
  <li><a href="/dotlrn/classes/E/EL4314/S-2-2026.AL.EL4314.20/">Arquitectura de computadoras I GR 20 <span>beenhere</span></a>
  <li><a href="/dotlrn/classes/E/EL4703/S-2-2026.AL.EL4703.20/">Señales y sistemas GR 20</a>
  <li><a href="/dotlrn/classes/E/EL4703/S-2-2026.AL.EL4703.20/tutoriagr20/">Tutoria-GR20</a>
  <li><a href="/dotlrn/classes/E/EL4703/S-2-2026.AL.EL4703.20/file-storage/view/programa.pdf">Programa</a>
<li>SEMESTRE 1 2026<ul>
  <li><a href="/dotlrn/classes/E/EL3310/S-1-2026.AL.EL3310.20/">Diseño de sistemas digitales GR 20</a>
</ul></body></html>"""

PROGRAMA = """
                    Actividad               Contenido        Porcentaje              Fecha
                    Primer examen parcial          Unidades 1 y 2        25 %         Sábado 12/09/2026
                    Examen final                   Unidades 1–5          40 %         Miércoles 02/12/2026 13:00
                   pero superior o igual a 57,5 %, tienen derecho a realizar un examen de reposición el 09/12/2026,
"""


# ---------------------------------------------------------------- lectores
class TestLectores(unittest.TestCase):
    def test_leer_evaluaciones(self):
        tareas = {x["nombre"]: x for x in t.leer_evaluaciones(EVALUACIONES, "Arqui", "https://x/ev")}
        self.assertEqual(len(tareas), 4)
        p4 = tareas["Proyecto 4"]
        self.assertEqual((p4["categoria"], p4["entregada"], p4["valor"]), ("Proyectos", False, "10.00"))
        self.assertEqual(p4["vence"], "2026-11-18T18:00:00-06:00")
        self.assertTrue(tareas["Proyecto 3"]["entregada"], "con archivo subido cuenta como entregada")
        self.assertTrue(tareas["Examen 1"]["entregada"], "con nota cuenta como entregada")
        self.assertFalse(tareas["Examen Final"]["entregada"])
        self.assertIsNone(tareas["Examen Final"]["vence"])

    def test_cursos_actuales_filtra_semestre_y_agrega_subgrupos(self):
        cursos = t.cursos_actuales(PORTAL)
        self.assertEqual([c["nombre"] for c in cursos], [
            "Arquitectura de computadoras I GR 20",
            "Señales y sistemas GR 20",
            "Señales y sistemas GR 20 · Tutoria-GR20",
        ])
        self.assertTrue(all(c["url"].startswith("https://tecdigital.tec.ac.cr/dotlrn/classes/") for c in cursos))

    def test_tiene_nota(self):
        self.assertTrue(t.tiene_nota("14.07 / 15.00"))
        self.assertTrue(t.tiene_nota("Ponderado85.0"))
        self.assertFalse(t.tiene_nota("-- / 10.00"))
        self.assertFalse(t.tiene_nota(""))


# ---------------------------------------------------------------- exámenes
class TestExamenes(unittest.TestCase):
    def test_clave_examen_reconoce_nombres_equivalentes(self):
        self.assertEqual(t.clave_examen("Primer examen parcial"), t.clave_examen("Examen Parcial 1"))
        self.assertEqual(t.clave_examen("Segundo examen parcial"), "parcial2")
        self.assertEqual(t.clave_examen("Examen final"), t.clave_examen("Examen Final"))
        self.assertEqual(t.clave_examen("Examen de reposición"), "reposicion")

    def test_examenes_de_texto_lee_la_tabla_e_ignora_parrafos(self):
        ex = t.examenes_de_texto(PROGRAMA)
        self.assertEqual([e["nombre"] for e in ex], ["Primer examen parcial", "Examen final"])
        self.assertEqual((ex[0]["fecha"][:10], ex[0]["hora"]), ("2026-09-12", False))
        self.assertEqual((ex[1]["fecha"], ex[1]["hora"]), ("2026-12-02T13:00:00-06:00", True))

    def test_fechas_manuales(self):
        with tempfile.TemporaryDirectory() as d:
            t.FECHAS_PATH = Path(d) / "fechas.txt"
            t.FECHAS_PATH.write_text("# comentario\nArqui | Examen 2 | 03/12/2026 18:00\n"
                                     "senales | Examen final | 2/12/2026\nesta línea está mal\n", encoding="utf-8")
            ex = t.leer_fechas_manuales(["Arquitectura de computadoras I GR 20", "Señales y sistemas GR 20"])
        self.assertEqual([(e["curso"], e["hora"]) for e in ex],
                         [("Arquitectura de computadoras I GR 20", True), ("Señales y sistemas GR 20", False)])

    def test_armar_examenes_prioridad(self):
        tareas = t.leer_evaluaciones(EVALUACIONES, "SyS", "https://x/ev")
        programa = [{"curso": "SyS", "nombre": "Examen final", "fecha": "2026-12-02T00:00:00-06:00", "hora": False}]
        manual = [{"curso": "SyS", "nombre": "Final", "fecha": "2026-12-03T08:00:00-06:00", "hora": True}]
        ex = t.armar_examenes(tareas, programa, manual, {})
        self.assertEqual(len(ex), 1, "los tres son el mismo examen final")
        self.assertEqual(ex[0]["nombre"], "Examen Final", "se queda el nombre de Evaluaciones")
        self.assertEqual(ex[0]["fecha"], "2026-12-03T08:00:00-06:00", "fechas.txt gana")
        self.assertEqual(ex[0]["valor"], "40.00")


# ---------------------------------------------------------------- avisos
def tarea(i, curso="Arqui", horas=None, entregada=False, nota="-- / 10.00"):
    vence = (datetime.now(t.TZ) + timedelta(hours=horas)).replace(microsecond=0).isoformat() if horas is not None else None
    return {"id": i, "curso": curso, "categoria": "Proyectos", "nombre": f"Tarea {i}", "valor": "10.00",
            "vence": vence, "entregada": entregada, "nota": nota, "url": ""}


class TestAvisos(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        t.DB_PATH = Path(self.dir.name) / "agenda.db"
        self.db = t.abrir_db()

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()

    def tipos(self, avisos):
        return sorted((a[0]["id"], a[1].split("-")[0]) for a in avisos)

    def test_primera_vez_no_avisa_de_nuevas(self):
        avisos, primera = t.actualizar(self.db, [tarea("a", horas=100)])
        self.assertTrue(primera)
        self.assertEqual(avisos, [])

    def test_nueva_solo_en_cursos_conocidos(self):
        t.actualizar(self.db, [tarea("a", horas=100)])
        avisos, _ = t.actualizar(self.db, [tarea("a", horas=100), tarea("b", horas=100),
                                           tarea("c", curso="Tutoría nueva", horas=100)])
        self.assertEqual(self.tipos(avisos), [("b", "nueva")])

    def test_recordatorios_y_correo_solo_si_no_se_entrego(self):
        avisos, _ = t.actualizar(self.db, [tarea("lejos", horas=30), tarea("cerca", horas=1.5),
                                           tarea("media", horas=2.5), tarea("lista", horas=1.5, entregada=True)])
        # 30 h → recordatorio de 48 h; 2.5 h → de 3 h; 1.5 h → de 3 h + correo (≤ 2 h); entregada → nada
        self.assertEqual(self.tipos(avisos), [("cerca", "correo"), ("cerca", "rec3"),
                                              ("lejos", "rec48"), ("media", "rec3")])

    def test_no_repite_avisos_enviados(self):
        t.actualizar(self.db, [tarea("a", horas=100)])
        avisos, _ = t.actualizar(self.db, [tarea("a", horas=2.5)])
        for x, tipo, _ in avisos:
            t.marcar_enviado(self.db, x, tipo)
        avisos, _ = t.actualizar(self.db, [tarea("a", horas=2.5)])
        self.assertEqual(avisos, [])

    def test_cambio_de_fecha(self):
        t.actualizar(self.db, [tarea("a", horas=100)])
        avisos, _ = t.actualizar(self.db, [tarea("a", horas=200)])
        self.assertEqual(self.tipos(avisos), [("a", "cambio")])

    def test_nota_nueva_queda_registrada(self):
        t.actualizar(self.db, [tarea("a", horas=-5, entregada=True)])
        t.actualizar(self.db, [tarea("a", horas=-5, entregada=True, nota="9.5 / 10.00")])
        cambio = self.db.execute("SELECT nota_cambio FROM tareas WHERE id='a'").fetchone()[0]
        self.assertIsNotNone(cambio)

    def test_secciones_muestra_entregadas_hasta_que_cierran(self):
        t.actualizar(self.db, [tarea("futura", horas=10, entregada=True), tarea("pasada", horas=-10, entregada=True),
                               tarea("pendiente", horas=20)])
        pend = t.secciones(self.db)["pend"]
        self.assertEqual([(x["nombre"], x["ent"]) for x in pend], [("Tarea futura", True), ("Tarea pendiente", False)])


# ---------------------------------------------------------------- servidor del panel
class TestServidorPanel(unittest.TestCase):
    def test_solo_entrega_el_panel(self):
        with tempfile.TemporaryDirectory() as d:
            servir_panel.PANEL = Path(d) / "panel.html"
            servir_panel.PANEL.write_text("<h1>panel</h1>", encoding="utf-8")
            (Path(d) / ".env").write_text("TEC_CLAVE=secreto")
            srv = ThreadingHTTPServer(("127.0.0.1", 0), servir_panel.Handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            try:
                self.assertIn(b"panel", urllib.request.urlopen(base + "/").read())
                for ruta in ("/.env", "/agenda.db", "/../.env", "/%2e%2e/.env", "/panel.html/../.env"):
                    with self.assertRaises(urllib.error.HTTPError, msg=ruta) as e:
                        urllib.request.urlopen(base + ruta)
                    self.assertEqual(e.exception.code, 404, ruta)
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()

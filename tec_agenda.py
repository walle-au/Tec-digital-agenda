#!/usr/bin/env python3
"""TEC Digital agenda: revisa las evaluaciones de tus cursos, avisa de tareas
nuevas y de cierres próximos, y genera un panel HTML con lo pendiente.

Uso:
  python3 tec_agenda.py                  # revisión normal (inicia sesión)
  python3 tec_agenda.py --resumen        # revisión + correo con lo que viene en la semana (lunes)
  python3 tec_agenda.py --cierre-semana  # revisión + correo de cierre de semana (viernes)
  python3 tec_agenda.py --probar ARCHIVO # prueba el lector con una página guardada
"""
import argparse
import html as htmllib
import json
import os
import re
import shutil
import smtplib
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE = "https://tecdigital.tec.ac.cr"
PORTAL = BASE + "/dotlrn/"
TZ = ZoneInfo("America/Costa_Rica")
DIR = Path(__file__).resolve().parent
DB_PATH = DIR / "agenda.db"
PANEL_PATH = DIR / "panel.html"
FECHAS_PATH = DIR / "fechas.txt"          # exámenes y fechas anotadas a mano
CACHE_PROGRAMAS = DIR / ".programas.json"  # exámenes leídos de los programas (se relee 1 vez al día)
# Horas antes del cierre en las que se manda recordatorio
RECORDATORIOS_H = [48, 24, 3]
# Días antes de un examen en los que se manda recordatorio
RECORDATORIOS_EXAMEN_D = [3, 1]
# Si falta esto o menos para un cierre (y no has entregado), también llega un correo
CORREO_ANTES_H = 2


# ---------------------------------------------------------------- config
def cargar_env():
    env = {}
    f = DIR / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k.startswith(("TEC_", "NTFY_", "SMTP_", "CORREO_"))})
    return env


# ---------------------------------------------------------------- sesión
class LoginError(Exception):
    pass


def iniciar_sesion(usuario, clave):
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) tec-agenda/1.0"
    r = s.get(PORTAL, timeout=30)
    if "register/logout" in r.text:
        return s  # ya había sesión

    # El login actual es una app Angular que envía un JSON a /api/login/new-form/
    base = "{0.scheme}://{0.netloc}".format(urlparse(r.url))
    try:
        rj = s.post(base + "/api/login/new-form/",
                    json={"email": usuario.lower(), "password": clave, "returnUrl": "/dotlrn/"},
                    headers={"X-Content-Type-Options": "nosniff", "Referer": r.url},
                    timeout=30)
        estado = rj.json().get("status")
    except (requests.RequestException, ValueError):
        estado = None
    if estado == "ok":
        r2 = s.get(PORTAL, timeout=30)
        if "register/logout" in r2.text or "/dotlrn/classes/" in r2.text:
            return s
        raise LoginError("TEC Digital aceptó la clave, pero no pude abrir Mi portal.")
    if estado is not None:
        mensajes = {
            "bad_password": "usuario o contraseña incorrectos",
            "muchos_intentos": "demasiados intentos; espera un rato antes de probar otra vez",
            "invalid_captcha": "TEC Digital pidió un captcha",
        }
        raise LoginError("El inicio de sesión no funcionó: " + mensajes.get(estado, estado))

    # Respaldo: formulario HTML clásico (versión vieja del login)
    soup = BeautifulSoup(r.text, "html.parser")
    form = next((f for f in soup.find_all("form") if f.find("input", {"type": "password"})), None)
    if form is None:
        if "register/logout" in r.text:
            return s  # ya había sesión
        raise LoginError(f"No encontré el formulario de inicio de sesión en {r.url}")

    datos = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        tipo = (inp.get("type") or "text").lower()
        if tipo == "password":
            datos[name] = clave
        elif tipo in ("text", "email") and not inp.get("value"):
            datos[name] = usuario
        elif tipo in ("checkbox", "radio"):
            if inp.has_attr("checked"):
                datos[name] = inp.get("value", "on")
        elif tipo != "submit":
            datos[name] = inp.get("value", "")
    accion = urljoin(r.url, form.get("action") or r.url)
    r2 = s.post(accion, data=datos, timeout=30)
    if "register/logout" not in r2.text:
        r2 = s.get(PORTAL, timeout=30)
    if "register/logout" not in r2.text:
        raise LoginError("El inicio de sesión no funcionó (¿usuario o contraseña incorrectos?)")
    return s


# ---------------------------------------------------------------- lectores
def cursos_actuales(html):
    """Cursos del periodo actual (bloque marcado como 'current' en Mi portal)."""
    soup = BeautifulSoup(html, "html.parser")
    # Hay un bloque 'current' para comunidades y otro para cursos; tomar el de cursos
    bloque = next((li for li in soup.find_all("li", attrs={"name": "current"})
                   if li.find("a", href=re.compile(r"/dotlrn/classes/"))), None)
    enlaces = bloque.find_all("a", href=True) if bloque else soup.find_all("a", href=re.compile(r"/dotlrn/classes/"))
    # Portales de curso y sus subgrupos (p. ej. .../S-2-2026.AL.EL4703.20/tutoriagr20/)
    patron = re.compile(r"/dotlrn/classes/[^/]+/[^/]+/(([A-Z]+-\d+-\d{4})\.[^/]+)/([^/?#]+/)?$")
    enlaces = [a for a in enlaces if patron.search(a["href"])]
    # El HTML viene mal cerrado y el bloque 'current' a veces incluye semestres viejos:
    # quedarse solo con el periodo del primer curso (p. ej. "S-2-2026")
    periodo = patron.search(enlaces[0]["href"]).group(2) if (bloque and enlaces) else None
    cursos, subgrupos, vistos = [], [], set()
    for a in enlaces:
        href = a["href"]
        m = patron.search(href)
        if href in vistos or (periodo and m.group(2) != periodo):
            continue
        vistos.add(href)
        nombre = a.get_text(" ", strip=True).replace("beenhere", "").strip()
        (subgrupos if m.group(3) else cursos).append({"nombre": nombre, "url": urljoin(BASE, href), "clave": m.group(1)})
    # Los subgrupos se nombran con su curso: "Señales y sistemas GR 20 · Tutoria-GR20"
    for sg in subgrupos:
        padre = next((c for c in cursos if c["clave"] == patron.search(sg["url"]).group(1)), None)
        if padre:
            cursos.append({"nombre": f"{padre['nombre']} · {sg['nombre']}", "url": sg["url"], "clave": sg["clave"]})
    return [{"nombre": c["nombre"], "url": c["url"]} for c in cursos]


def url_evaluaciones(html_curso, url_curso):
    soup = BeautifulSoup(html_curso, "html.parser")
    a = soup.find("a", title=re.compile(r"Ir a Evaluaciones$"))
    return urljoin(url_curso, a["href"]) if a else None


def _fecha(txt):
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})\s+(\d{1,2}):(\d{2})", txt or "")
    if not m:
        return None
    d, mo, y, h, mi = map(int, m.groups())
    return datetime(y, mo, d, h, mi, tzinfo=TZ)


def leer_evaluaciones(html, curso, url=""):
    soup = BeautifulSoup(html, "html.parser")
    tareas = []
    for cont in soup.select("div.container_accor[assign_id]"):
        nombre_el = cont.select_one(".assignNameText")
        if not nombre_el:
            continue
        campos = {}
        for p in cont.select("p.title_subsection"):
            val = p.find_next_sibling("div", class_="body_style")
            if val is None:
                continue
            clave = re.sub(r"[\s:]+$", "", p.get_text(" ", strip=True)).lower()
            campos.setdefault(clave, val.get_text(" ", strip=True))
        categoria = ""
        grupo = cont.find_parent("div", class_="container_accor_detail")
        if grupo:
            cab = grupo.find_previous_sibling("div", class_="title_acor_grade")
            if cab and cab.select_one(".clase"):
                categoria = cab.select_one(".clase").get_text(strip=True)
        nota_el = cont.select_one(".vt_grade_student")
        fecha_txt = next((v for k, v in campos.items() if k.startswith("fecha de entrega")), "")
        entregada = bool(
            cont.select_one(".displayNameFile")
            or "comprobante del intento" in campos
            or re.search(r"\d", campos.get("nota obtenida", "").split("/")[0])
        )
        vence = _fecha(fecha_txt)
        tareas.append({
            "id": cont["assign_id"],
            "curso": curso,
            "categoria": categoria,
            "nombre": nombre_el.get_text(strip=True),
            "valor": campos.get("valor de la asignación", ""),
            "vence": vence.isoformat() if vence else None,
            "entregada": entregada,
            "nota": nota_el.get_text(strip=True) if nota_el else "",
            "url": url,
        })
    return tareas


# ---------------------------------------------------------------- exámenes
def _sin_tildes(txt):
    return "".join(c for c in unicodedata.normalize("NFKD", txt) if not unicodedata.combining(c)).lower()


def es_examen(categoria, nombre=""):
    return "examen" in _sin_tildes(f"{categoria} {nombre}")


def clave_examen(nombre):
    """'Primer examen parcial' y 'Examen Parcial 1' dan la misma clave: 'parcial1'."""
    n = _sin_tildes(nombre)
    for palabra, num in (("primer", "1"), ("segund", "2"), ("tercer", "3"), ("cuart", "4"),
                         ("1er", "1"), ("2do", "2"), ("3er", "3")):
        n = n.replace(palabra, f" {num} ")
    tipo = ("reposicion" if "reposici" in n else "final" if "final" in n
            else "parcial" if "parcial" in n else "examen")
    num = re.search(r"\d+", n)
    return tipo + (num.group() if num and tipo != "final" else "")


def url_programa(html_curso, url_curso):
    soup = BeautifulSoup(html_curso, "html.parser")
    for a in soup.find_all("a", href=True):
        if "programa del curso" in a.get_text(" ", strip=True).lower() and "file-storage" in a["href"]:
            return urljoin(url_curso, a["href"])
    return None


def examenes_de_texto(texto):
    """Busca líneas como 'Primer examen parcial   Unidades 1 y 2   25 %   Sábado 12/09/2026'."""
    encontrados = []
    for linea in texto.splitlines():
        if not re.search(r"examen|parcial", _sin_tildes(linea)):
            continue
        m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", linea)
        if not m:
            continue
        nombre = re.split(r"\s{2,}", linea.strip())[0]
        if m.group() in nombre or len(nombre) > 60:
            continue  # es un párrafo, no una fila de la tabla de evaluación
        d, mo, y = map(int, m.groups())
        hora = re.search(r"\b(\d{1,2}):(\d{2})\b", linea[m.end():])
        h, mi = (int(hora.group(1)), int(hora.group(2))) if hora else (0, 0)
        try:
            fecha = datetime(y, mo, d, h, mi, tzinfo=TZ)
        except ValueError:
            continue
        encontrados.append({"nombre": nombre, "fecha": fecha.isoformat(), "hora": bool(hora)})
    return encontrados


def examenes_del_programa(s, url):
    """Descarga el PDF del programa y saca las fechas de examen (usa pdftotext)."""
    if not shutil.which("pdftotext"):
        return []
    r = s.get(url, timeout=60)
    if r.status_code != 200 or not r.content.startswith(b"%PDF"):
        return []
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(r.content)
        f.flush()
        out = subprocess.run(["pdftotext", "-layout", f.name, "-"], capture_output=True, text=True)
    return examenes_de_texto(out.stdout)


def leer_fechas_manuales(cursos):
    """fechas.txt: una línea por examen →  curso | nombre | dd/mm/aaaa [hh:mm]"""
    if not FECHAS_PATH.exists():
        return []
    res = []
    for n, linea in enumerate(FECHAS_PATH.read_text(encoding="utf-8").splitlines(), 1):
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        partes = [p.strip() for p in linea.split("|")]
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+(\d{1,2}):(\d{2}))?", partes[-1]) if len(partes) == 3 else None
        if not m:
            print(f"  fechas.txt línea {n}: no la entendí (formato: curso | nombre | dd/mm/aaaa hh:mm)")
            continue
        d, mo, y = int(m[1]), int(m[2]), int(m[3])
        h, mi = (int(m[4]), int(m[5])) if m[4] else (0, 0)
        curso = next((c for c in cursos if _sin_tildes(partes[0]) in _sin_tildes(c)), partes[0])
        res.append({"curso": curso, "nombre": partes[1],
                    "fecha": datetime(y, mo, d, h, mi, tzinfo=TZ).isoformat(), "hora": bool(m[4])})
    return res


def armar_examenes(tareas, del_programa, manuales, urls_curso):
    """Junta exámenes de Evaluaciones (sin fecha), del programa y de fechas.txt.
    Si coinciden curso + tipo de examen, gana: fechas.txt > programa > Evaluaciones."""
    ex = {}
    for t in tareas:
        if es_examen(t["categoria"], t["nombre"]) and not t["vence"] and not t["entregada"]:
            ex[(t["curso"], clave_examen(t["nombre"]))] = {
                "curso": t["curso"], "nombre": t["nombre"], "fecha": None, "hora": False,
                "fuente": "Evaluaciones (sin fecha)", "valor": t["valor"], "url": t["url"]}
    for fuente, lista in (("programa del curso", del_programa), ("fechas.txt", manuales)):
        for e in lista:
            k = (e["curso"], clave_examen(e["nombre"]))
            previo = ex.get(k, {})
            ex[k] = {"curso": e["curso"], "nombre": previo.get("nombre", e["nombre"]),
                     "fecha": e["fecha"], "hora": e["hora"], "fuente": fuente,
                     "valor": previo.get("valor", ""), "url": previo.get("url") or urls_curso.get(e["curso"], "")}
    for (curso, clave), e in ex.items():
        e["id"] = f"examen:{curso}:{clave}"
    return list(ex.values())


# ---------------------------------------------------------------- base de datos
def abrir_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""CREATE TABLE IF NOT EXISTS tareas(
        id TEXT PRIMARY KEY, curso TEXT, categoria TEXT, nombre TEXT, valor TEXT,
        vence TEXT, entregada INTEGER, nota TEXT, url TEXT, visto TEXT)""")
    if "nota_cambio" not in [r[1] for r in db.execute("PRAGMA table_info(tareas)")]:
        db.execute("ALTER TABLE tareas ADD COLUMN nota_cambio TEXT")  # cuándo apareció/cambió la nota
    db.execute("CREATE TABLE IF NOT EXISTS avisos(id TEXT, tipo TEXT, PRIMARY KEY(id, tipo))")
    db.execute("""CREATE TABLE IF NOT EXISTS examenes(
        id TEXT PRIMARY KEY, curso TEXT, nombre TEXT, fecha TEXT, hora INTEGER,
        fuente TEXT, valor TEXT, url TEXT)""")
    return db


def guardar_examenes(db, examenes):
    """Reemplaza la lista de exámenes y devuelve los recordatorios pendientes."""
    db.execute("DELETE FROM examenes")
    hoy = datetime.now(TZ).date()
    avisos = []
    for e in examenes:
        db.execute("INSERT INTO examenes VALUES(?,?,?,?,?,?,?,?)",
                   (e["id"], e["curso"], e["nombre"], e["fecha"], int(e["hora"]), e["fuente"], e["valor"], e["url"]))
        if not e["fecha"]:
            continue
        dias = (datetime.fromisoformat(e["fecha"]).date() - hoy).days
        for d in sorted(RECORDATORIOS_EXAMEN_D):
            if 0 <= dias <= d:
                cuando = "hoy" if dias == 0 else "mañana" if dias == 1 else f"en {dias} días"
                tipo = f"exa{d}-{e['fecha']}"
                if not db.execute("SELECT 1 FROM avisos WHERE id=? AND tipo=?", (e["id"], tipo)).fetchone():
                    avisos.append((e, tipo, f"📝 Examen {cuando}: {e['nombre']} ({e['curso']}) — {fmt_examen(e)}"))
                break
    db.commit()
    return avisos


def actualizar(db, tareas):
    """Guarda las tareas y devuelve la lista de avisos a enviar."""
    primera_vez = db.execute("SELECT COUNT(*) FROM tareas").fetchone()[0] == 0
    # Un curso que aparece por primera vez (p. ej. una tutoría recién agregada) no avisa de "nuevas"
    cursos_conocidos = {r[0] for r in db.execute("SELECT DISTINCT curso FROM tareas")}
    ahora = datetime.now(TZ)
    avisos = []
    for t in tareas:
        prev = db.execute("SELECT vence, nota, nota_cambio FROM tareas WHERE id=?", (t["id"],)).fetchone()
        nota_cambio = prev[2] if prev else None
        if prev is not None and prev[1] != t["nota"] and tiene_nota(t["nota"]):
            nota_cambio = ahora.isoformat()
        if prev is None and not primera_vez and t["curso"] in cursos_conocidos:
            avisos.append((t, "nueva", f"Nueva asignación en {t['curso']}: {t['nombre']}" + (f" (cierra {fmt(t['vence'])})" if t["vence"] else "")))
        elif prev is not None and prev[0] != t["vence"] and t["vence"]:
            avisos.append((t, f"cambio-{t['vence']}", f"Cambió la fecha de {t['nombre']} ({t['curso']}): ahora cierra {fmt(t['vence'])}"))
        db.execute("""INSERT INTO tareas(id,curso,categoria,nombre,valor,vence,entregada,nota,url,visto,nota_cambio)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET curso=excluded.curso, categoria=excluded.categoria,
            nombre=excluded.nombre, valor=excluded.valor, vence=excluded.vence,
            entregada=excluded.entregada, nota=excluded.nota, url=excluded.url,
            nota_cambio=excluded.nota_cambio""",
            (t["id"], t["curso"], t["categoria"], t["nombre"], t["valor"], t["vence"],
             int(t["entregada"]), t["nota"], t["url"], ahora.isoformat(), nota_cambio))

        if t["vence"] and not t["entregada"]:
            faltan = datetime.fromisoformat(t["vence"]) - ahora
            for h in sorted(RECORDATORIOS_H):  # la ventana más cercana primero
                if timedelta(0) < faltan <= timedelta(hours=h):
                    avisos.append((t, f"rec{h}-{t['vence']}", f"⏰ {t['nombre']} ({t['curso']}) cierra en {humano(faltan)} — {fmt(t['vence'])}"))
                    break
            if timedelta(0) < faltan <= timedelta(hours=CORREO_ANTES_H):
                avisos.append((t, f"correo-{t['vence']}", None))  # None = va por correo, no como notificación
    # Filtrar avisos ya enviados
    nuevos = []
    for t, tipo, msg in avisos:
        if db.execute("SELECT 1 FROM avisos WHERE id=? AND tipo=?", (t["id"], tipo)).fetchone():
            continue
        nuevos.append((t, tipo, msg))
    db.commit()
    return nuevos, primera_vez


def tiene_nota(nota):
    """'14.07 / 15.00' o 'Ponderado85.0' sí; '-- / 10.00' no."""
    return bool(re.search(r"\d", (nota or "").split("/")[0]))


def marcar_enviado(db, t, tipo):
    db.execute("INSERT OR IGNORE INTO avisos VALUES(?,?)", (t["id"], tipo))
    db.commit()


# ---------------------------------------------------------------- avisos
DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def fmt(iso):
    if not iso:
        return "sin fecha"
    d = datetime.fromisoformat(iso)
    return f"{DIAS[d.weekday()]} {d:%d/%m %H:%M}"


def fmt_examen(e):
    if not e["fecha"]:
        return "fecha por confirmar"
    d = datetime.fromisoformat(e["fecha"])
    return f"{DIAS[d.weekday()]} {d:%d/%m}" + (f" {d:%H:%M}" if e["hora"] else "")


def humano(td):
    h = int(td.total_seconds() // 3600)
    if h >= 24:
        return f"{h // 24} d {h % 24} h"
    return f"{h} h {int(td.total_seconds() % 3600 // 60)} min"


def notificar(env, mensaje, url=""):
    ok = False
    topic = env.get("NTFY_TOPIC")
    if topic:
        try:
            headers = {"Title": "TEC Digital"}
            if url:
                headers["Click"] = url
            requests.post(f"https://ntfy.sh/{topic}", data=mensaje.encode("utf-8"), headers=headers, timeout=15).raise_for_status()
            ok = True
        except requests.RequestException as e:
            print(f"  (no se pudo enviar a ntfy: {e})", file=sys.stderr)
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "TEC Digital", "TEC Digital", mensaje], check=False)
        ok = True
    print("  AVISO:", mensaje)
    return ok


# ---------------------------------------------------------------- panel
def secciones(db):
    """Lo que se muestra en el panel y en el correo, ya ordenado."""
    ahora = datetime.now(TZ)
    pend, sin_fecha, vencidas = [], [], []
    for (curso, cat, nombre, valor, vence, ent, url) in db.execute(
            "SELECT curso,categoria,nombre,valor,vence,entregada,url FROM tareas"):
        t = dict(curso=curso, cat=cat, nombre=nombre, valor=valor, vence=vence, url=url, ent=bool(ent))
        if ent and not (vence and datetime.fromisoformat(vence) >= ahora):
            continue  # entregada y ya cerró (o sin fecha): no hace falta mostrarla
        if not vence:
            if not es_examen(cat, nombre):  # los exámenes van en su propia sección
                sin_fecha.append(t)
        elif datetime.fromisoformat(vence) >= ahora:
            pend.append(t)
        elif ahora - datetime.fromisoformat(vence) < timedelta(days=7):
            vencidas.append(t)
    pend.sort(key=lambda t: t["vence"])
    examenes = []
    for (curso, nombre, fecha, hora, fuente, valor, url) in db.execute(
            "SELECT curso,nombre,fecha,hora,fuente,valor,url FROM examenes"):
        if fecha and datetime.fromisoformat(fecha).date() < ahora.date():
            continue  # ya pasó
        examenes.append(dict(curso=curso, nombre=nombre, fecha=fecha, hora=bool(hora),
                             fuente=fuente, valor=valor, url=url))
    examenes.sort(key=lambda e: (e["fecha"] is None, e["fecha"] or ""))
    return {"ahora": ahora, "pend": pend, "examenes": examenes, "sin_fecha": sin_fecha, "vencidas": vencidas}


def _falta(iso, ahora, solo_dias=False):
    td = datetime.fromisoformat(iso) - ahora
    if solo_dias:
        dias = (datetime.fromisoformat(iso).date() - ahora.date()).days
        return "hoy" if dias == 0 else "mañana" if dias == 1 else f"{dias} días"
    return humano(td)


def generar_panel(db):
    sec = secciones(db)
    ahora = sec["ahora"]
    e = htmllib.escape

    def celda_nombre(t, sub, estado=""):
        nombre = f'<a href="{e(t["url"])}" target="_blank">{e(t["nombre"])}</a>' if t["url"] else e(t["nombre"])
        # En el celular la columna Estado se oculta y el estado sale debajo del nombre
        movil = f'<div class="solo-movil">{estado}</div>' if estado else ""
        return f'<td>{nombre}<div class="sub">{e(sub)}</div>{movil}</td>'

    def clase_por(td):
        return "urgente" if td < timedelta(hours=24) else "pronto" if td < timedelta(days=3) else ""

    def fila(t, mostrar_falta=True):
        falta, clase = "", ""
        if t["vence"] and mostrar_falta:
            falta = _falta(t["vence"], ahora)
            clase = "" if t["ent"] else clase_por(datetime.fromisoformat(t["vence"]) - ahora)
        if t["ent"]:
            clase = "hecha"
        sub = t["cat"] + (" · vale " + t["valor"] if t["valor"] else "")
        estado = ('<span class="est ok">✔ Entregada</span>' if t["ent"]
                  else '<span class="est no">Sin entregar</span>')
        return (f'<tr class="{clase}">{celda_nombre(t, sub, estado)}'
                f'<td>{e(t["curso"])}</td><td class="num">{fmt(t["vence"])}</td><td class="num">{falta}</td><td>{estado}</td></tr>')

    def fila_examen(x):
        falta, clase = "", ""
        if x["fecha"]:
            falta = _falta(x["fecha"], ahora, solo_dias=True)
            dias = (datetime.fromisoformat(x["fecha"]).date() - ahora.date()).days
            clase = "urgente" if dias <= 1 else "pronto" if dias <= 3 else ""
        sub = "fuente: " + x["fuente"] + (" · vale " + x["valor"] if x["valor"] else "")
        return (f'<tr class="{clase}">{celda_nombre(x, sub)}'
                f'<td>{e(x["curso"])}</td><td class="num">{fmt_examen(x)}</td><td class="num">{falta}</td><td></td></tr>')

    def tabla(titulo, lista, filas, col="Cierra", n=None):
        if not lista:
            return f"<h2>{titulo}</h2><p class='vacio'>Nada por aquí 🎉</p>"
        n = len(lista) if n is None else n
        return (f"<h2>{titulo} <span class='n'>{n}</span></h2><table><thead><tr><th>Asignación</th><th>Curso</th>"
                f"<th>{col}</th><th>Falta</th><th>Estado</th></tr></thead><tbody>{''.join(filas)}</tbody></table>")

    html = f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Agenda TEC Digital</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--muted:#6b7280;--line:#e5e7eb;--red:#c0392b;--redbg:#fdecea;--amber:#9a6700;--amberbg:#fff6dd;--link:#1f5fbf;--ok:#1e7d3a;--okbg:#e3f4e8}}
@media (prefers-color-scheme:dark){{:root{{--bg:#14161b;--card:#1d2027;--fg:#e8eaef;--muted:#9aa1ad;--line:#2c3039;--red:#ff8a80;--redbg:#3a1f1e;--amber:#ffd27a;--amberbg:#3a3120;--link:#8ab4ff;--ok:#7fd99a;--okbg:#1f3326}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}}
main{{max-width:960px;margin:0 auto;padding:24px 16px}}
h1{{margin:0 0 4px;font-size:24px}} .upd{{color:var(--muted);margin-bottom:20px}}
h2{{font-size:17px;margin:28px 0 8px}} .n{{background:var(--line);border-radius:10px;padding:1px 8px;font-size:13px}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}}
th,td{{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{font-size:12px;text-transform:uppercase;color:var(--muted);font-weight:600}}
.num{{white-space:nowrap;font-variant-numeric:tabular-nums}} .sub{{color:var(--muted);font-size:13px}}
tr.urgente td{{background:var(--redbg)}} tr.urgente .num{{color:var(--red);font-weight:600}}
tr.pronto td{{background:var(--amberbg)}} tr.pronto .num{{color:var(--amber);font-weight:600}}
a{{color:var(--link);text-decoration:none}} .vacio{{color:var(--muted)}}
.est{{font-size:12px;font-weight:600;padding:2px 8px;border-radius:10px;white-space:nowrap}}
.est.ok{{background:var(--okbg);color:var(--ok)}} .est.no{{background:var(--line);color:var(--muted)}}
tr.urgente .est.no{{background:var(--red);color:var(--card)}}
tr.hecha td{{color:var(--muted)}} tr.hecha a{{color:var(--muted)}}
.solo-movil{{display:none;margin-top:4px}}
@media (max-width:600px){{
  th:nth-child(2),td:nth-child(2),th:nth-child(5),td:nth-child(5){{display:none}}
  .solo-movil{{display:block}} th,td{{padding:8px 8px}} main{{padding:20px 12px}}
}}
</style></head><body><main>
<h1>Agenda TEC Digital</h1><div class="upd">Actualizado {fmt(ahora.isoformat())}</div>
{tabla("Próximos cierres", sec["pend"], [fila(t) for t in sec["pend"]], n=f"{sum(not t['ent'] for t in sec['pend'])} sin entregar")}
{tabla("Exámenes", sec["examenes"], [fila_examen(x) for x in sec["examenes"]], col="Fecha")}
{tabla("Sin fecha definida", sec["sin_fecha"], [fila(t, False) for t in sec["sin_fecha"]])}
{tabla("Cerraron sin entrega (últimos 7 días)", sec["vencidas"], [fila(t, False) for t in sec["vencidas"]])}
</main></body></html>"""
    PANEL_PATH.write_text(html, encoding="utf-8")


# ---------------------------------------------------------------- correos
def _corto(curso):
    """'Señales y sistemas GR 20' → 'Señales y sistemas' (para que quepa en el celular)."""
    return re.sub(r" GR \d+\b", "", curso)


def _armar_correo(titulo, subtitulo, bloques):
    """bloques: [(título, color, [(nombre, url, detalle, cuándo, nota_al_lado, línea_de_texto)])].
    Estilos en línea para que Gmail y Outlook los respeten. Devuelve (texto, html)."""
    e = htmllib.escape
    html_b, txt_b = [], []
    for tit, color, filas in bloques:
        if not filas:
            continue
        trs = []
        for nombre, url, sub, cuando, lado, _ in filas:
            n = f'<a href="{e(url)}" style="color:#1f5fbf;text-decoration:none">{e(nombre)}</a>' if url else e(nombre)
            trs.append(f'<tr><td style="padding:8px 6px;border-bottom:1px solid #e5e7eb">{n}'
                       f'<div style="color:#6b7280;font-size:12px">{e(sub)}</div></td>'
                       f'<td style="padding:8px 6px;border-bottom:1px solid #e5e7eb;white-space:nowrap;text-align:right">{e(cuando)}'
                       f'<div style="color:#6b7280;font-size:12px">{e(lado)}</div></td></tr>')
        html_b.append(f'<h2 style="font-size:16px;margin:24px 0 8px;color:{color}">{e(tit)}</h2>'
                      f'<table style="width:100%;border-collapse:collapse;font-size:14px">{"".join(trs)}</table>')
        txt_b.append(f"\n{tit.upper()}\n" + "\n".join(f[5] for f in filas) + "\n")
    cuerpo = "".join(html_b) or "<p>No hay nada por aquí 🎉</p>"
    html = (f'<!doctype html><html><body style="margin:0;background:#f6f7f9">'
            f'<div style="max-width:640px;margin:0 auto;padding:20px 16px;font-family:system-ui,Arial,sans-serif;color:#1d2330;background:#fff">'
            f'<h1 style="font-size:20px;margin:0 0 4px">{e(titulo)}</h1>'
            f'<div style="color:#6b7280;font-size:13px">{e(subtitulo)}</div>{cuerpo}</div></body></html>')
    return f"{titulo} ({subtitulo})\n" + "".join(txt_b), html


def _fila_tarea(t, ahora, lado=None):
    sub = _corto(t["curso"]) + (" · vale " + t["valor"] if t.get("valor") else "")
    if lado is None:
        if t.get("ent"):
            lado = "✔ entregada"
        else:
            lado = ("sin entregar · faltan " + _falta(t["vence"], ahora)) if t["vence"] and t["vence"] > ahora.isoformat() else ""
    return (t["nombre"], t["url"], sub, fmt(t["vence"]), lado, f"- {t['nombre']} ({t['curso']}): {fmt(t['vence'])} {lado}".rstrip())


def _fila_examen(x, ahora):
    lado = ("faltan " + _falta(x["fecha"], ahora, solo_dias=True)) if x["fecha"] else ""
    return (x["nombre"], x["url"], _corto(x["curso"]), fmt_examen(x), lado,
            f"- {x['nombre']} ({x['curso']}): {fmt_examen(x)}")


def _plural(n, palabra, pl=None):
    return f"{n} {palabra if n == 1 else (pl or palabra + 's')}"


def resumen_correo(db):
    """Correo del lunes: lo que viene esta semana."""
    sec = secciones(db)
    ahora = sec["ahora"]
    limite = ahora + timedelta(days=7)
    semana = [t for t in sec["pend"] if t["vence"] <= limite.isoformat()]
    despues = [t for t in sec["pend"] if t["vence"] > limite.isoformat()]
    ex_semana = [x for x in sec["examenes"] if x["fecha"] and x["fecha"] <= limite.isoformat()]
    asunto = f"TEC Digital: {_plural(sum(not t['ent'] for t in semana), 'entrega')} esta semana"
    if ex_semana:
        asunto += f" y {_plural(len(ex_semana), 'examen', 'exámenes')}"
    texto, html = _armar_correo("Tu semana en TEC Digital", f"Semana del {ahora:%d/%m} al {limite:%d/%m}", [
        ("Esta semana", "#c0392b", [_fila_tarea(t, ahora) for t in semana]),
        ("Exámenes", "#1d2330", [_fila_examen(x, ahora) for x in sec["examenes"]]),
        ("Más adelante", "#1d2330", [_fila_tarea(t, ahora) for t in despues]),
        ("Sin fecha definida", "#6b7280", [_fila_tarea(t, ahora, "") for t in sec["sin_fecha"]]),
    ])
    return asunto, texto, html


def cierre_semana_correo(db):
    """Correo del viernes: qué entregaste, qué se pasó, notas nuevas y lo que viene."""
    sec = secciones(db)
    ahora = sec["ahora"]
    inicio = ahora - timedelta(days=7)
    entregadas, se_paso, notas = [], [], []
    for (curso, cat, nombre, valor, vence, ent, nota, url, nota_cambio) in db.execute(
            "SELECT curso,categoria,nombre,valor,vence,entregada,nota,url,nota_cambio FROM tareas"):
        t = dict(curso=curso, nombre=nombre, valor=valor, vence=vence, url=url, nota=nota)
        if vence and inicio.isoformat() <= vence <= ahora.isoformat():
            (entregadas if ent else se_paso).append(t)
        if nota_cambio and nota_cambio >= inicio.isoformat():
            notas.append(t)
    for lista in (entregadas, se_paso):
        lista.sort(key=lambda t: t["vence"])
    prox_limite = ahora + timedelta(days=9)  # hasta el domingo de la otra semana
    proxima = [t for t in sec["pend"] if t["vence"] <= prox_limite.isoformat()]
    examenes = [x for x in sec["examenes"] if x["fecha"] and x["fecha"] <= (ahora + timedelta(days=21)).isoformat()]

    partes = [_plural(len(entregadas), "entregada")]
    if se_paso:
        partes.append(f"{len(se_paso)} sin entregar")
    if notas:
        partes.append(_plural(len(notas), "nota nueva", "notas nuevas"))
    asunto = "TEC Digital: cierre de semana — " + ", ".join(partes)
    texto, html = _armar_correo("Cierre de semana", f"Del {inicio:%d/%m} al {ahora:%d/%m}", [
        ("Se te pasaron (sin entregar)", "#c0392b", [_fila_tarea(t, ahora, "sin entregar") for t in se_paso]),
        ("Notas nuevas", "#1d2330", [(t["nombre"], t["url"], _corto(t["curso"]), t["nota"].replace("Ponderado", ""), "",
                                      f"- {t['nombre']} ({t['curso']}): {t['nota']}") for t in notas]),
        ("Entregaste esta semana ✔", "#2e7d32", [_fila_tarea(t, ahora, "entregada") for t in entregadas]),
        ("La próxima semana", "#1d2330", [_fila_tarea(t, ahora) for t in proxima]),
        ("Exámenes en las próximas 3 semanas", "#1d2330", [_fila_examen(x, ahora) for x in examenes]),
    ])
    return asunto, texto, html


def recordatorio_correo(tareas_ya, ahora):
    """Correo de 'cierra en menos de 2 h' (puede juntar varias asignaciones)."""
    filas = [_fila_tarea(t, ahora) for t in tareas_ya]
    primera = tareas_ya[0]
    asunto = (f"⏰ Cierra en {_falta(primera['vence'], ahora)}: {primera['nombre']} ({_corto(primera['curso'])})"
              if len(tareas_ya) == 1 else f"⏰ {len(tareas_ya)} asignaciones cierran en menos de {CORREO_ANTES_H} h")
    texto, html = _armar_correo("Todavía no has entregado", f"Cierra en menos de {CORREO_ANTES_H} horas",
                                [("Por cerrar", "#c0392b", filas)])
    return asunto, texto, html


def enviar_correo(env, asunto, texto, html):
    usuario, clave = env.get("SMTP_USUARIO"), env.get("SMTP_CLAVE", "").replace(" ", "")  # Google la muestra con espacios
    if not usuario or not clave:
        print("  (correo no configurado: faltan SMTP_USUARIO y SMTP_CLAVE en .env; ver README, sección 5)")
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = asunto, usuario, env.get("CORREO_PARA") or usuario
    msg.set_content(texto)
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP_SSL(env.get("SMTP_SERVIDOR", "smtp.gmail.com"), int(env.get("SMTP_PUERTO", "465")), timeout=30) as smtp:
        smtp.login(usuario, clave)
        smtp.send_message(msg)
    print(f"Correo enviado a {msg['To']}: {asunto}")
    return True


# ---------------------------------------------------------------- main
def cargar_cache_programas():
    try:
        return json.loads(CACHE_PROGRAMAS.read_text())
    except (OSError, ValueError):
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probar", metavar="ARCHIVO", help="lee una página de Evaluaciones guardada y muestra lo que encuentra")
    ap.add_argument("--resumen", action="store_true", help="además de revisar, manda el correo con lo que viene en la semana")
    ap.add_argument("--cierre-semana", action="store_true", help="además de revisar, manda el correo de cierre de semana")
    args = ap.parse_args()

    if args.probar:
        tareas = leer_evaluaciones(Path(args.probar).read_text(encoding="utf-8", errors="ignore"), "prueba")
        for t in tareas:
            estado = "entregada" if t["entregada"] else "PENDIENTE"
            print(f"- [{t['categoria']}] {t['nombre']}: cierra {fmt(t['vence'])} · {estado} · {t['nota']}")
        return

    env = cargar_env()
    if not env.get("TEC_USUARIO") or not env.get("TEC_CLAVE"):
        sys.exit("Falta TEC_USUARIO o TEC_CLAVE en el archivo .env (ver .env.example)")

    s = iniciar_sesion(env["TEC_USUARIO"], env["TEC_CLAVE"])
    cursos = cursos_actuales(s.get(PORTAL, timeout=30).text)
    print(f"Cursos encontrados: {len(cursos)}")
    cache = cargar_cache_programas()
    ahora = datetime.now(TZ)
    tareas, del_programa = [], []
    for c in cursos:
        html_curso = s.get(c["url"], timeout=30).text
        # Exámenes del programa del curso (el PDF se relee como mucho una vez al día)
        url_prog = url_programa(html_curso, c["url"])
        if url_prog:
            guardado = cache.get(url_prog)
            if not guardado or ahora - datetime.fromisoformat(guardado["leido"]) > timedelta(days=1):
                try:
                    guardado = {"leido": ahora.isoformat(), "examenes": examenes_del_programa(s, url_prog)}
                    cache[url_prog] = guardado
                except requests.RequestException as e:
                    print(f"  {c['nombre']}: no pude bajar el programa ({e})")
            if guardado:
                del_programa += [dict(x, curso=c["nombre"]) for x in guardado["examenes"]]
        url_ev = url_evaluaciones(html_curso, c["url"])
        if not url_ev:
            print(f"  {c['nombre']}: sin pestaña de Evaluaciones")
            continue
        encontradas = leer_evaluaciones(s.get(url_ev, timeout=30).text, c["nombre"], url_ev)
        n_prog = len(cache.get(url_prog, {}).get("examenes", [])) if url_prog else 0
        print(f"  {c['nombre']}: {len(encontradas)} asignaciones" + (f", {n_prog} fechas en el programa" if n_prog else ""))
        tareas += encontradas
    CACHE_PROGRAMAS.write_text(json.dumps(cache, ensure_ascii=False, indent=1))

    db = abrir_db()
    avisos, primera_vez = actualizar(db, tareas)
    if primera_vez:
        print("Primera ejecución: guardé todo sin avisar de 'nuevas'.")
    urls_curso = {}
    for t in tareas:
        urls_curso.setdefault(t["curso"], t["url"])
    examenes = armar_examenes(tareas, del_programa, leer_fechas_manuales([c["nombre"] for c in cursos]), urls_curso)
    avisos += guardar_examenes(db, examenes)
    por_correo = [(t, tipo) for t, tipo, msg in avisos if msg is None]
    for t, tipo, msg in avisos:
        if msg is not None and notificar(env, msg, t["url"]):
            marcar_enviado(db, t, tipo)
    if por_correo:
        try:
            if enviar_correo(env, *recordatorio_correo([t for t, _ in por_correo], datetime.now(TZ))):
                for t, tipo in por_correo:
                    marcar_enviado(db, t, tipo)
        except (smtplib.SMTPException, OSError) as e:
            print(f"  (no se pudo mandar el correo de recordatorio: {e})", file=sys.stderr)
    generar_panel(db)
    print(f"Panel: {PANEL_PATH}")
    if args.resumen:
        enviar_correo(env, *resumen_correo(db))
    if args.cierre_semana:
        enviar_correo(env, *cierre_semana_correo(db))


if __name__ == "__main__":
    try:
        main()
    except (LoginError, requests.RequestException, smtplib.SMTPException) as e:
        sys.exit(f"Error: {e}")

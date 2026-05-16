"""
app.py — Backend Flask para SmartKardex Web (corregido)
========================================================
Endpoints reales que el frontend espera:

  POST   /api/kardex/cargar                → sube PDF de kárdex (campo 'pdf')
  GET    /api/alumnos                      → lista alumnos (devuelve array directo)
  GET    /api/alumnos/<codigo>/analizar    → análisis académico completo
  POST   /api/alumnos/<codigo>/perfil      → guarda orientación/SS/PP del alumno
  POST   /api/alumnos/<codigo>/horario-pdf → extrae horario desde PDF (campo 'pdf')
  GET    /api/alumnos/<codigo>/horario     → devuelve horario guardado
  POST   /api/alumnos/<codigo>/horario     → guarda lista de materias del horario
  DELETE /api/alumnos/<codigo>             → elimina alumno de la BD
  GET    /api/alumnos/<codigo>/sugerir     → busca materia por clave o nombre
  POST   /api/plan/importar               → importa CSV del plan de estudios
  GET    /health                           → healthcheck para Render
"""

import os
import tempfile
import sqlite3 as _sqlite3
from pathlib import Path

from flask import Flask, request, jsonify
from flask_cors import CORS

from database import init_db, normalizar, log
from extractor import KardexExtractor
from plan import importar_plan_estudios
from motor_web import MotorInferencia

# ─────────────────────────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────────────────────────
DB_PATH    = os.environ.get("KARDEX_DB_PATH", "kardex_udg.db")
PLAN_CSV   = os.environ.get("PLAN_CSV_PATH", "Plan de Estudios IELC - Hoja 6.csv")
SECRET_KEY = os.environ.get("SECRET_KEY", os.urandom(24).hex())
MAX_MB     = 20

app = Flask(__name__)
app.config["SECRET_KEY"]         = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024
CORS(app)

# Inicializar BD al arrancar
init_db(DB_PATH)

# Agregar columnas de perfil si no existen (migración segura)
def _migrar_db():
    conn = _sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    columnas = [r[1] for r in cur.execute("PRAGMA table_info(alumnos)").fetchall()]
    if "orientacion_elegida" not in columnas:
        cur.execute("ALTER TABLE alumnos ADD COLUMN orientacion_elegida TEXT DEFAULT ''")
    if "servicio_social" not in columnas:
        cur.execute("ALTER TABLE alumnos ADD COLUMN servicio_social INTEGER DEFAULT 0")
    if "practicas_profesionales" not in columnas:
        cur.execute("ALTER TABLE alumnos ADD COLUMN practicas_profesionales INTEGER DEFAULT 0")
    conn.commit()

    # Tabla para el horario del ciclo actual
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS horario_ciclo (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            alumno_id  INTEGER NOT NULL REFERENCES alumnos(id) ON DELETE CASCADE,
            clave      TEXT NOT NULL,
            nombre     TEXT NOT NULL,
            creditos   INTEGER DEFAULT 0,
            calendario TEXT DEFAULT '',
            UNIQUE(alumno_id, clave)
        );
    """)
    conn.commit()
    conn.close()

_migrar_db()

# Auto-importar plan si la tabla está vacía
def _plan_vacio() -> bool:
    try:
        conn = _sqlite3.connect(DB_PATH)
        n = conn.execute("SELECT COUNT(*) FROM plan_estudios").fetchone()[0]
        conn.close()
        return n == 0
    except Exception:
        return True

if _plan_vacio() and Path(PLAN_CSV).exists():
    log.info("Auto-importando plan desde %s", PLAN_CSV)
    importar_plan_estudios(db_path=DB_PATH, csv_path=PLAN_CSV)


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────
def _err(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": msg}), code

def _ok(data: dict):
    return jsonify({"ok": True, **data})

def _get_alumno_id(codigo: str):
    """Devuelve (alumno_id, conn) o None si no existe."""
    conn = _sqlite3.connect(DB_PATH)
    conn.row_factory = _sqlite3.Row
    row = conn.execute("SELECT id FROM alumnos WHERE codigo=?", (codigo,)).fetchone()
    if not row:
        conn.close()
        return None, None
    return row["id"], conn

def _guardar_tmp(file_storage, suffix=".pdf") -> str:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        file_storage.save(tmp.name)
        return tmp.name


# ─────────────────────────────────────────────────────────────────
# GET /health
# ─────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return jsonify({"status": "ok"})


# ─────────────────────────────────────────────────────────────────
# POST /api/kardex/cargar
# ─────────────────────────────────────────────────────────────────
@app.post("/api/kardex/cargar")
def cargar_kardex():
    """
    El frontend envía el PDF en el campo 'pdf' (no 'file').
    Devuelve: {"ok": true, "alumno": {...}}
    """
    campo = "pdf" if "pdf" in request.files else "file"
    if campo not in request.files:
        return _err("Se requiere el PDF del kárdex (campo 'pdf').")

    archivo = request.files[campo]
    if not archivo.filename.lower().endswith(".pdf"):
        return _err("Solo se aceptan archivos PDF.")

    tmp_path = _guardar_tmp(archivo)
    try:
        extractor = KardexExtractor(db_path=DB_PATH)
        resultado = extractor.cargar_pdf(tmp_path)
        if not resultado:
            return _err("No se pudo extraer información del PDF. "
                        "Verifica que sea un kárdex UDG válido.")
        # El frontend espera la clave "alumno" con los datos del alumno
        alumno = resultado.get("alumno", resultado)
        return _ok({"alumno": alumno})
    except Exception as e:
        log.exception("Error al cargar kárdex")
        return _err(f"Error interno: {str(e)}", 500)
    finally:
        os.unlink(tmp_path)


# ─────────────────────────────────────────────────────────────────
# GET /api/alumnos
# ─────────────────────────────────────────────────────────────────
@app.get("/api/alumnos")
def listar_alumnos():
    """
    El frontend espera un array directo (no un objeto con clave 'alumnos').
    """
    try:
        conn = _sqlite3.connect(DB_PATH)
        conn.row_factory = _sqlite3.Row
        rows = conn.execute("""
            SELECT codigo, nombre, carrera, ultimo_ciclo,
                   creditos_adquiridos, creditos_requeridos, promedio
            FROM alumnos ORDER BY nombre
        """).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify([])  # array vacío en caso de error, no rompe el init


# ─────────────────────────────────────────────────────────────────
# GET /api/alumnos/<codigo>/analizar
# ─────────────────────────────────────────────────────────────────
@app.get("/api/alumnos/<codigo>/analizar")
def analizar(codigo: str):
    """
    El frontend hace GET (no POST) con el código en la URL.
    Lee el perfil del alumno desde la BD para pasar SS/PP/orientación.
    """
    try:
        conn = _sqlite3.connect(DB_PATH)
        conn.row_factory = _sqlite3.Row
        al_row = conn.execute(
            "SELECT id, orientacion_elegida, servicio_social, practicas_profesionales "
            "FROM alumnos WHERE codigo=?", (codigo,)
        ).fetchone()
        conn.close()

        if not al_row:
            return _err(f"El alumno {codigo} no está en la base de datos.", 404)

        # Leer horario guardado para excluirlo del análisis
        conn2 = _sqlite3.connect(DB_PATH)
        conn2.row_factory = _sqlite3.Row
        hrows = conn2.execute(
            "SELECT clave, nombre FROM horario_ciclo WHERE alumno_id=?",
            (al_row["id"],)
        ).fetchall()
        conn2.close()

        horario_claves  = {r["clave"].upper() for r in hrows}
        horario_nombres = {normalizar(r["nombre"]) for r in hrows}

        motor = MotorInferencia(db_path=DB_PATH)
        resultado = motor.analizar(
            codigo_alumno   = codigo,
            horario_claves  = horario_claves,
            horario_nombres = horario_nombres,
            servicio_social = bool(al_row["servicio_social"]),
            practicas_prof  = bool(al_row["practicas_profesionales"]),
            orientacion     = (al_row["orientacion_elegida"] or "").strip().upper(),
        )
        if resultado is None or "error" in resultado:
            return _err((resultado or {}).get("error", "Error desconocido."))

        return jsonify(resultado)
    except Exception as e:
        log.exception("Error en análisis")
        return _err(f"Error interno: {str(e)}", 500)


# ─────────────────────────────────────────────────────────────────
# POST /api/alumnos/<codigo>/perfil
# ─────────────────────────────────────────────────────────────────
@app.post("/api/alumnos/<codigo>/perfil")
def guardar_perfil(codigo: str):
    """
    Guarda orientación elegida, SS y PP en la tabla alumnos.
    Body JSON: {orientacion, servicio_social, practicas}
    """
    data = request.get_json(silent=True) or {}
    try:
        conn = _sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT id FROM alumnos WHERE codigo=?", (codigo,)).fetchone()
        if not row:
            conn.close()
            return _err(f"Alumno {codigo} no encontrado.", 404)

        conn.execute("""
            UPDATE alumnos
               SET orientacion_elegida     = ?,
                   servicio_social         = ?,
                   practicas_profesionales = ?
             WHERE codigo = ?
        """, (
            (data.get("orientacion") or "").strip().upper(),
            int(bool(data.get("servicio_social", False))),
            int(bool(data.get("practicas", False))),
            codigo,
        ))
        conn.commit()
        conn.close()
        return _ok({"mensaje": "Perfil actualizado."})
    except Exception as e:
        log.exception("Error al guardar perfil")
        return _err(f"Error interno: {str(e)}", 500)


# ─────────────────────────────────────────────────────────────────
# POST /api/alumnos/<codigo>/horario-pdf
# ─────────────────────────────────────────────────────────────────
@app.post("/api/alumnos/<codigo>/horario-pdf")
def cargar_horario_pdf(codigo: str):
    """
    Extrae materias del PDF de horario y las guarda en horario_ciclo.
    El frontend envía el PDF en el campo 'pdf'.
    """
    campo = "pdf" if "pdf" in request.files else "file"
    if campo not in request.files:
        return _err("Se requiere el PDF del horario (campo 'pdf').")

    archivo = request.files[campo]
    if not archivo.filename.lower().endswith(".pdf"):
        return _err("Solo se aceptan archivos PDF.")

    alumno_id, conn = _get_alumno_id(codigo)
    if alumno_id is None:
        return _err(f"Alumno {codigo} no encontrado.", 404)
    conn.close()

    tmp_path = _guardar_tmp(archivo)
    try:
        from horario_extractor import extraer_horario
        datos = extraer_horario(tmp_path)

        if not datos or not datos.get("materias"):
            return _err("No se pudieron extraer materias del horario PDF.")

        materias = datos["materias"]
        conn2 = _sqlite3.connect(DB_PATH)
        conn2.execute("DELETE FROM horario_ciclo WHERE alumno_id=?", (alumno_id,))
        for m in materias:
            conn2.execute("""
                INSERT OR IGNORE INTO horario_ciclo (alumno_id, clave, nombre, creditos, calendario)
                VALUES (?, ?, ?, ?, ?)
            """, (
                alumno_id,
                m["clave"].upper(),
                m["nombre"],
                m.get("creditos", 0),
                datos.get("ciclo", ""),
            ))
        conn2.commit()
        conn2.close()

        return _ok({
            "materias_en_horario": len(materias),
            "materias": materias,
            "ciclo": datos.get("ciclo", ""),
        })
    except Exception as e:
        log.exception("Error al procesar horario PDF")
        return _err(f"Error interno: {str(e)}", 500)
    finally:
        os.unlink(tmp_path)


# ─────────────────────────────────────────────────────────────────
# GET /api/alumnos/<codigo>/horario
# ─────────────────────────────────────────────────────────────────
@app.get("/api/alumnos/<codigo>/horario")
def get_horario(codigo: str):
    """
    Devuelve el horario guardado del alumno.
    El frontend espera un array directo (no un objeto).
    """
    alumno_id, conn = _get_alumno_id(codigo)
    if alumno_id is None:
        return jsonify([])
    conn.close()

    try:
        conn2 = _sqlite3.connect(DB_PATH)
        conn2.row_factory = _sqlite3.Row
        rows = conn2.execute(
            "SELECT clave, nombre, creditos, calendario FROM horario_ciclo WHERE alumno_id=?",
            (alumno_id,)
        ).fetchall()
        conn2.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify([])


# ─────────────────────────────────────────────────────────────────
# POST /api/alumnos/<codigo>/horario
# ─────────────────────────────────────────────────────────────────
@app.post("/api/alumnos/<codigo>/horario")
def guardar_horario(codigo: str):
    """
    Guarda la lista de materias del horario ingresada manualmente.
    Body JSON: {calendario: "2026A", materias: [{clave, nombre, creditos}, ...]}
    """
    data = request.get_json(silent=True) or {}
    materias   = data.get("materias", [])
    calendario = (data.get("calendario") or "").strip()

    alumno_id, conn = _get_alumno_id(codigo)
    if alumno_id is None:
        return _err(f"Alumno {codigo} no encontrado.", 404)
    conn.close()

    if not materias:
        return _err("No se enviaron materias.")

    try:
        conn2 = _sqlite3.connect(DB_PATH)
        conn2.execute("DELETE FROM horario_ciclo WHERE alumno_id=?", (alumno_id,))
        guardadas = 0
        for m in materias:
            clave = str(m.get("clave", "")).strip().upper()
            nombre = str(m.get("nombre", "")).strip()
            if not clave or not nombre:
                continue
            conn2.execute("""
                INSERT OR REPLACE INTO horario_ciclo (alumno_id, clave, nombre, creditos, calendario)
                VALUES (?, ?, ?, ?, ?)
            """, (alumno_id, clave, nombre, int(m.get("creditos", 0)), calendario))
            guardadas += 1
        conn2.commit()
        conn2.close()
        return _ok({"guardadas": guardadas})
    except Exception as e:
        log.exception("Error al guardar horario")
        return _err(f"Error interno: {str(e)}", 500)


# ─────────────────────────────────────────────────────────────────
# DELETE /api/alumnos/<codigo>
# ─────────────────────────────────────────────────────────────────
@app.delete("/api/alumnos/<codigo>")
def eliminar_alumno(codigo: str):
    """Elimina al alumno y todos sus datos (CASCADE en la BD)."""
    alumno_id, conn = _get_alumno_id(codigo)
    if alumno_id is None:
        return _err(f"Alumno {codigo} no encontrado.", 404)
    conn.close()

    try:
        conn2 = _sqlite3.connect(DB_PATH)
        conn2.execute("PRAGMA foreign_keys = ON")
        conn2.execute("DELETE FROM alumnos WHERE codigo=?", (codigo,))
        conn2.commit()
        conn2.close()
        return _ok({"mensaje": f"Alumno {codigo} eliminado."})
    except Exception as e:
        log.exception("Error al eliminar alumno")
        return _err(f"Error interno: {str(e)}", 500)


# ─────────────────────────────────────────────────────────────────
# GET /api/alumnos/<codigo>/sugerir?q=<query>&area=<area>
# ─────────────────────────────────────────────────────────────────
@app.get("/api/alumnos/<codigo>/sugerir")
def sugerir_materia(codigo: str):
    """
    Busca una materia en el plan por clave exacta o similitud de nombre.
    Query params: q (texto), area (opcional, filtra por área).
    """
    q    = (request.args.get("q") or "").strip()
    area = (request.args.get("area") or "").strip()

    if not q:
        return jsonify({"encontrado": False, "filtro": "ninguno", "mensaje": "Ingresa una clave o nombre."})

    try:
        from difflib import SequenceMatcher
        conn = _sqlite3.connect(DB_PATH)
        conn.row_factory = _sqlite3.Row
        plan = [dict(r) for r in conn.execute(
            "SELECT clave, materia, area_cod, area, orientacion, creditos FROM plan_estudios"
        ).fetchall()]
        conn.close()

        q_upper = q.upper()
        q_norm  = normalizar(q)

        # 1. Búsqueda por clave exacta
        for m in plan:
            if m["clave"].upper() == q_upper:
                return jsonify({"encontrado": True, "filtro": "clave", "materia": m})

        # 2. Búsqueda por similitud de nombre
        def sim(a, b):
            return SequenceMatcher(None, a, b).ratio()

        mejor = max(plan, key=lambda m: sim(q_norm, normalizar(m["materia"])))
        score = sim(q_norm, normalizar(mejor["materia"]))
        if score >= 0.55:
            return jsonify({"encontrado": True, "filtro": "nombre", "materia": mejor, "similitud": score})

        # 3. Sin coincidencia — devolver áreas disponibles para selector
        areas = sorted({m["area"] for m in plan if m["area"]})
        resp  = {"encontrado": False, "filtro": "ninguno",
                 "mensaje": f"No se encontró '{q}' en el plan de estudios.",
                 "areas": areas}

        # Si se pasó área, devolver materias de esa área
        if area:
            lista = [m for m in plan if m["area"] == area or m["area_cod"] == area]
            resp["filtro"]     = "area"
            resp["lista_area"] = lista
            resp["mensaje"]    = f"{len(lista)} materia(s) en el área '{area}'."

        return jsonify(resp)
    except Exception as e:
        log.exception("Error en sugerir")
        return _err(f"Error interno: {str(e)}", 500)


# ─────────────────────────────────────────────────────────────────
# POST /api/plan/importar
# ─────────────────────────────────────────────────────────────────
@app.post("/api/plan/importar")
def importar_plan():
    """
    Importa el plan desde CSV subido (campo 'file') o desde disco.
    """
    tmp_path = None
    try:
        if "file" in request.files:
            archivo = request.files["file"]
            tmp_path = _guardar_tmp(archivo, suffix=".csv")
            csv_to_use = tmp_path
        elif Path(PLAN_CSV).exists():
            csv_to_use = PLAN_CSV
        else:
            return _err(f"No se encontró el CSV del plan ('{PLAN_CSV}'). Súbelo manualmente.")

        importar_plan_estudios(db_path=DB_PATH, csv_path=csv_to_use)

        conn = _sqlite3.connect(DB_PATH)
        total = conn.execute("SELECT COUNT(*) FROM plan_estudios").fetchone()[0]
        conn.close()
        return _ok({"mensaje": f"Plan importado. {total} materias en BD."})
    except Exception as e:
        log.exception("Error al importar plan")
        return _err(f"Error interno: {str(e)}", 500)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────
# Mantener compatibilidad con endpoints anteriores (por si acaso)
# ─────────────────────────────────────────────────────────────────
@app.post("/api/cargar-kardex")
def cargar_kardex_legacy():
    return cargar_kardex()

@app.post("/api/cargar-horario")
def cargar_horario_legacy():
    """Versión legacy: no tiene código de alumno, solo extrae y devuelve."""
    campo = "pdf" if "pdf" in request.files else "file"
    if campo not in request.files:
        return _err("Se requiere el PDF del horario.")
    archivo = request.files[campo]
    tmp_path = _guardar_tmp(archivo)
    try:
        from horario_extractor import extraer_horario
        datos = extraer_horario(tmp_path)
        if not datos:
            return _err("No se pudieron extraer materias del horario.")
        return _ok({"horario": datos})
    except Exception as e:
        return _err(f"Error interno: {str(e)}", 500)
    finally:
        os.unlink(tmp_path)


# ─────────────────────────────────────────────────────────────────
# Manejo de errores globales
# ─────────────────────────────────────────────────────────────────
@app.errorhandler(413)
def too_large(e):
    return _err(f"El archivo excede el límite de {MAX_MB} MB.", 413)

@app.errorhandler(404)
def not_found(e):
    return _err("Endpoint no encontrado.", 404)

@app.errorhandler(500)
def server_error(e):
    return _err("Error interno del servidor.", 500)


# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

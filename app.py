"""
app.py — Smartkardex Web API v2
"""

import os
import io
import sys
import json
import tempfile
import traceback
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import init_db
from extractor import KardexExtractor
from motor import MotorInferencia
from plan import importar_plan_estudios

BASE_DIR = Path(os.path.abspath(__file__)).parent

# ── Rutas de archivos estáticos y datos ─────────────────────────
# index.html vive directamente en BASE_DIR (junto a app.py).
# Si existe una carpeta "static/" se usa como raíz alternativa;
# si no, se sirve desde BASE_DIR.
STATIC_DIR = BASE_DIR / "static"
if not STATIC_DIR.exists():
    STATIC_DIR = BASE_DIR          # fallback: index.html junto a app.py

DB_FILE  = str(BASE_DIR / "kardex_udg.db")
CSV_FILE = str(BASE_DIR / "Plan de Estudios IELC - Hoja 6.csv")

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
CORS(app)


# ── Captura de stdout para evitar que print() de los módulos
#    ensucien la respuesta JSON ─────────────────────────────────
class CaptureOutput:
    def __enter__(self):
        self._old = sys.stdout
        self._buf = io.StringIO()
        sys.stdout = self._buf
        return self._buf

    def __exit__(self, *a):
        sys.stdout = self._old


def clean(obj):
    """Convierte sets a listas para serialización JSON."""
    if isinstance(obj, set):   return list(obj)
    if isinstance(obj, dict):  return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, list):  return [clean(i) for i in obj]
    return obj


# ── Inicialización al arranque ───────────────────────────────────
import sqlite3 as _sqlite3
import logging as _logging
_log = _logging.getLogger(__name__)

try:
    init_db(DB_FILE)

    # Auto-importar plan de estudios si la tabla está vacía
    _conn_init = _sqlite3.connect(DB_FILE)
    _plan_count = _conn_init.execute("SELECT COUNT(*) FROM plan_estudios").fetchone()[0]
    _conn_init.close()

    if _plan_count == 0 and Path(CSV_FILE).exists():
        _log.info("Importando plan de estudios al arranque...")
        with CaptureOutput():
            importar_plan_estudios(db_path=DB_FILE, csv_path=CSV_FILE)
        _log.info("Plan de estudios importado correctamente.")
    else:
        _log.info("BD lista. Plan: %d materias. Static: %s", _plan_count, STATIC_DIR)

except Exception as _e:
    _log.error("ERROR inicializando BD: %s", _e, exc_info=True)


# ══════════════════════════════════════════════
# HELPERS INTERNOS
# ══════════════════════════════════════════════

def _get_alumno_id(conn, codigo: str):
    """Devuelve alumno_id o None si no existe."""
    row = conn.execute("SELECT id FROM alumnos WHERE codigo=?", (codigo,)).fetchone()
    return row[0] if row else None


# ══════════════════════════════════════════════
# RUTAS DE API
# ══════════════════════════════════════════════

@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "version": "2.1",
        "static_dir": str(STATIC_DIR),
        "static_exists": STATIC_DIR.exists(),
        "index_exists": (STATIC_DIR / "index.html").exists(),
    })


@app.route("/api/plan/status")
def plan_status():
    conn = _sqlite3.connect(DB_FILE)
    count = conn.execute("SELECT COUNT(*) FROM plan_estudios").fetchone()[0]
    conn.close()
    return jsonify({"cargado": count > 0, "total_materias": count})


@app.route("/api/plan/importar", methods=["POST"])
def importar_plan():
    if not Path(CSV_FILE).exists():
        return jsonify({"ok": False, "error": f"CSV no encontrado: {CSV_FILE}"}), 404
    try:
        with CaptureOutput() as buf:
            importar_plan_estudios(db_path=DB_FILE, csv_path=CSV_FILE)
        return jsonify({"ok": True, "mensaje": buf.getvalue()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/alumnos")
def listar_alumnos():
    conn = _sqlite3.connect(DB_FILE)
    conn.row_factory = _sqlite3.Row
    rows = conn.execute("""
        SELECT codigo, nombre, carrera, creditos_adquiridos,
               creditos_requeridos, promedio, ultimo_ciclo, situacion,
               orientacion_elegida, servicio_social, practicas_profesionales
        FROM alumnos ORDER BY nombre
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/alumnos/<codigo>", methods=["GET"])
def consultar_alumno(codigo):
    conn = _sqlite3.connect(DB_FILE)
    conn.row_factory = _sqlite3.Row
    alumno = conn.execute("SELECT * FROM alumnos WHERE codigo=?", (codigo,)).fetchone()
    if not alumno:
        conn.close()
        return jsonify({"error": f"Alumno {codigo} no encontrado"}), 404

    alumno_dict = dict(alumno)
    alumno_id   = alumno_dict["id"]

    materias = conn.execute("""
        SELECT clave, nombre, calificacion, estatus, creditos,
               tipo, calendario, fecha_eval
        FROM materias WHERE alumno_id=? ORDER BY calendario, nombre
    """, (alumno_id,)).fetchall()

    creditos_area = conn.execute("""
        SELECT area, requeridos, adquiridos, faltantes
        FROM creditos_por_area WHERE alumno_id=? ORDER BY area
    """, (alumno_id,)).fetchall()

    horario = conn.execute("""
        SELECT clave, nombre, creditos, calendario
        FROM horario WHERE alumno_id=?
    """, (alumno_id,)).fetchall()

    conn.close()
    return jsonify({
        "alumno":       alumno_dict,
        "materias":     [dict(m) for m in materias],
        "creditos_area": [dict(c) for c in creditos_area],
        "horario":      [dict(h) for h in horario],
    })


@app.route("/api/alumnos/<codigo>", methods=["DELETE"])
def eliminar_alumno(codigo):
    conn = _sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()
    cur.execute("DELETE FROM alumnos WHERE codigo=?", (codigo,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    if deleted == 0:
        return jsonify({"error": "Alumno no encontrado"}), 404
    return jsonify({"ok": True})


@app.route("/api/alumnos/<codigo>/analizar", methods=["GET", "POST"])
def analizar_alumno(codigo):
    try:
        # Verificar que el plan de estudios esté cargado
        conn_check = _sqlite3.connect(DB_FILE)
        plan_count = conn_check.execute("SELECT COUNT(*) FROM plan_estudios").fetchone()[0]
        conn_check.close()
        if plan_count == 0:
            # Intentar importar automáticamente
            if Path(CSV_FILE).exists():
                with CaptureOutput():
                    importar_plan_estudios(db_path=DB_FILE, csv_path=CSV_FILE)
            else:
                return jsonify({
                    "error": "Plan de estudios no cargado. Sube el CSV del plan."
                }), 400

        motor = MotorInferencia(db_path=DB_FILE)
        orientacion     = None
        servicio_social = False
        practicas       = False

        if request.method == "POST":
            data            = request.get_json(silent=True) or {}
            orientacion     = data.get("orientacion")
            servicio_social = bool(data.get("servicio_social", False))
            practicas       = bool(data.get("practicas", False))
        else:
            orientacion     = request.args.get("orientacion")
            servicio_social = request.args.get("servicio_social", "").lower() == "true"
            practicas       = request.args.get("practicas", "").lower() == "true"

        with CaptureOutput():
            resultado = motor.analizar(
                codigo,
                orientacion=orientacion,
                servicio_social=servicio_social,
                practicas=practicas,
            )

        if resultado is None:
            return jsonify({"error": f"Alumno {codigo} no encontrado"}), 404

        return jsonify(clean(resultado))

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/alumnos/<codigo>/horario", methods=["POST"])
def guardar_horario(codigo):
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"ok": False, "error": "Body JSON requerido"}), 400

        calendario = data.get("calendario", "")
        materias   = data.get("materias", [])

        if not isinstance(materias, list):
            return jsonify({"ok": False, "error": "'materias' debe ser una lista"}), 400

        motor     = MotorInferencia(db_path=DB_FILE)
        resultado = motor.guardar_horario(codigo, materias, calendario)
        return jsonify(resultado)

    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/alumnos/<codigo>/horario", methods=["GET"])
def obtener_horario(codigo):
    conn = _sqlite3.connect(DB_FILE)
    conn.row_factory = _sqlite3.Row
    alumno = conn.execute("SELECT id FROM alumnos WHERE codigo=?", (codigo,)).fetchone()
    if not alumno:
        conn.close()
        return jsonify({"error": "Alumno no encontrado"}), 404

    horario = conn.execute("""
        SELECT clave, nombre, creditos, calendario
        FROM horario WHERE alumno_id=? ORDER BY calendario, nombre
    """, (alumno["id"],)).fetchall()
    conn.close()
    return jsonify([dict(h) for h in horario])


@app.route("/api/alumnos/<codigo>/sugerir", methods=["GET", "POST"])
def sugerir_materia(codigo):
    try:
        if request.method == "POST":
            data       = request.get_json(silent=True) or {}
            query      = data.get("q", "")
            area_manual = data.get("area", None)
        else:
            query      = request.args.get("q", "")
            area_manual = request.args.get("area", None)

        if not query:
            return jsonify({"error": "Parámetro 'q' requerido"}), 400

        motor     = MotorInferencia(db_path=DB_FILE)
        resultado = motor.sugerir_materia(codigo, query, area_manual)
        return jsonify(clean(resultado))

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/alumnos/<codigo>/perfil", methods=["POST"])
def actualizar_perfil(codigo):
    try:
        data = request.get_json(silent=True) or {}
        conn = _sqlite3.connect(DB_FILE)
        alumno = conn.execute("SELECT id FROM alumnos WHERE codigo=?", (codigo,)).fetchone()
        if not alumno:
            conn.close()
            return jsonify({"error": "Alumno no encontrado"}), 404

        alumno_id = alumno[0]
        fields, vals = [], []

        if "orientacion" in data:
            fields.append("orientacion_elegida=?")
            vals.append(data["orientacion"])
        if "servicio_social" in data:
            fields.append("servicio_social=?")
            vals.append(1 if data["servicio_social"] else 0)
        if "practicas" in data:
            fields.append("practicas_profesionales=?")
            vals.append(1 if data["practicas"] else 0)

        if fields:
            conn.execute(
                f"UPDATE alumnos SET {', '.join(fields)} WHERE id=?",
                vals + [alumno_id]
            )
            conn.commit()

        conn.close()
        return jsonify({"ok": True})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/kardex/cargar", methods=["POST"])
def cargar_kardex():
    if "pdf" not in request.files:
        return jsonify({"error": "No se envió ningún archivo PDF"}), 400

    file = request.files["pdf"]
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Solo se aceptan archivos PDF"}), 400

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    try:
        extractor = KardexExtractor(db_path=DB_FILE)
        with CaptureOutput() as buf:
            resultado = extractor.cargar_pdf(tmp_path)

        if not resultado:
            return jsonify({
                "ok": False,
                "error": "No se pudo extraer el kárdex. Verifica que el PDF sea válido."
            }), 400

        # Devolver datos del alumno recién cargado
        conn = _sqlite3.connect(DB_FILE)
        conn.row_factory = _sqlite3.Row
        alumno = conn.execute("""
            SELECT codigo, nombre, carrera, creditos_adquiridos,
                   creditos_requeridos, promedio, ultimo_ciclo,
                   orientacion_elegida, servicio_social, practicas_profesionales
            FROM alumnos ORDER BY fecha_carga DESC, id DESC LIMIT 1
        """).fetchone()
        conn.close()

        return jsonify({
            "ok":     True,
            "log":    buf.getvalue(),
            "accion": resultado.get("accion", ""),
            "alumno": dict(alumno) if alumno else None,
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500

    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ══════════════════════════════════════════════
# FRONTEND — SIEMPRE AL FINAL
# ══════════════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory(str(STATIC_DIR), "index.html")


@app.route("/<path:path>")
def catch_all(path):
    # No interceptar rutas de API
    if path.startswith("api/"):
        return jsonify({"error": "Not found"}), 404

    target = STATIC_DIR / path
    if target.exists() and target.is_file():
        return send_from_directory(str(STATIC_DIR), path)

    # SPA fallback
    return send_from_directory(str(STATIC_DIR), "index.html")


# ── Arranque ─────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Smartkardex v2 corriendo en http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)

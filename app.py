"""
app.py — Smartkardex Web API
Flask backend que expone el sistema de Kárdex UDG como REST API.
En producción (Render) también sirve el frontend estático.
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

sys.path.insert(0, os.path.dirname(__file__))

from database import init_db
from extractor import KardexExtractor
from motor import MotorInferencia
from plan import importar_plan_estudios

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

BASE_DIR = Path(__file__).parent
DB_FILE  = str(BASE_DIR / "kardex_udg.db")
CSV_FILE = str(BASE_DIR / "Plan de Estudios IELC - Hoja 6.csv")


class CaptureOutput:
    def __enter__(self):
        self._old = sys.stdout
        self._buf = io.StringIO()
        sys.stdout = self._buf
        return self._buf
    def __exit__(self, *a):
        sys.stdout = self._old


# ── Servir frontend ──────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


# ── Health ───────────────────────────────────────────────────
@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "version": "1.0"})


# ── Plan de estudios ─────────────────────────────────────────
@app.route("/api/plan/status")
def plan_status():
    import sqlite3
    conn = sqlite3.connect(DB_FILE)
    count = conn.execute("SELECT COUNT(*) FROM plan_estudios").fetchone()[0]
    conn.close()
    return jsonify({"cargado": count > 0, "total_materias": count})


@app.route("/api/plan/importar", methods=["POST"])
def importar_plan():
    try:
        with CaptureOutput() as buf:
            importar_plan_estudios(db_path=DB_FILE, csv_path=CSV_FILE)
        return jsonify({"ok": True, "mensaje": buf.getvalue()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Alumnos ──────────────────────────────────────────────────
@app.route("/api/alumnos")
def listar_alumnos():
    import sqlite3
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT codigo, nombre, carrera, creditos_adquiridos,
               creditos_requeridos, promedio, ultimo_ciclo, situacion
        FROM alumnos ORDER BY nombre
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/alumnos/<codigo>")
def consultar_alumno(codigo):
    import sqlite3
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
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
    conn.close()
    return jsonify({
        "alumno":        alumno_dict,
        "materias":      [dict(m) for m in materias],
        "creditos_area": [dict(c) for c in creditos_area],
    })


@app.route("/api/alumnos/<codigo>/analizar")
def analizar_alumno(codigo):
    try:
        motor = MotorInferencia(db_path=DB_FILE)
        with CaptureOutput():
            resultado = motor.analizar(codigo)
        if resultado is None:
            return jsonify({"error": f"Alumno {codigo} no encontrado"}), 404
        def clean(obj):
            if isinstance(obj, set):  return list(obj)
            if isinstance(obj, dict): return {k: clean(v) for k,v in obj.items()}
            if isinstance(obj, list): return [clean(i) for i in obj]
            return obj
        return jsonify(clean(resultado))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


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
            extractor.cargar_pdf(tmp_path)
        import sqlite3
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        alumno = conn.execute("""
            SELECT codigo, nombre, carrera, creditos_adquiridos,
                   creditos_requeridos, promedio, ultimo_ciclo
            FROM alumnos ORDER BY fecha_carga DESC, id DESC LIMIT 1
        """).fetchone()
        conn.close()
        return jsonify({"ok": True, "log": buf.getvalue(),
                        "alumno": dict(alumno) if alumno else None})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        os.unlink(tmp_path)


@app.route("/api/alumnos/<codigo>", methods=["DELETE"])
def eliminar_alumno(codigo):
    import sqlite3
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()
    cur.execute("DELETE FROM alumnos WHERE codigo=?", (codigo,))
    deleted = cur.rowcount
    conn.commit(); conn.close()
    if deleted == 0:
        return jsonify({"error": "Alumno no encontrado"}), 404
    return jsonify({"ok": True})


# ── Arranque ──────────────────────────────────────────────────
if __name__ == "__main__":
    init_db(DB_FILE)
    import sqlite3
    conn = sqlite3.connect(DB_FILE)
    plan_count = conn.execute("SELECT COUNT(*) FROM plan_estudios").fetchone()[0]
    conn.close()
    if plan_count == 0 and Path(CSV_FILE).exists():
        print("📚 Importando plan de estudios...")
        with CaptureOutput():
            importar_plan_estudios(db_path=DB_FILE, csv_path=CSV_FILE)
        print("✅ Plan importado.")
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Smartkardex corriendo en http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)

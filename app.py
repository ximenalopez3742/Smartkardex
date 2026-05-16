"""
app.py — Smartkardex Web API
Flask backend que expone el sistema de Kárdex UDG como REST API.
En producción (Render) también sirve el frontend estático.

VERSIÓN CORREGIDA: agrega rutas de horarios que faltaban:
  POST /api/horario/cargar          → cargar PDF de horario
  POST /api/horario/extraer         → extraer materias de PDF de horario
  GET  /api/alumnos/<codigo>/horario → obtener horario guardado
  POST /api/alumnos/<codigo>/horario → guardar horario manual
  DELETE /api/alumnos/<codigo>/horario → borrar horario
  GET  /api/alumnos/<codigo>/perfil  → perfil completo del alumno
"""

import os
import io
import re
import sys
import json
import tempfile
import traceback
import sqlite3
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

sys.path.insert(0, os.path.dirname(__file__))

from database import init_db
from extractor import KardexExtractor
from motor import MotorInferencia
from plan import importar_plan_estudios
from horario_extractor import extraer_horario_siiau

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

BASE_DIR = Path(__file__).parent
DB_FILE  = str(BASE_DIR / "kardex_udg.db")
CSV_FILE = str(BASE_DIR / "Plan de Estudios IELC - Hoja 6.csv")


# ─────────────────────────────────────────────────────────────────
# Helper: asegurar tabla horarios en BD
# ─────────────────────────────────────────────────────────────────

def _ensure_horario_table(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS horarios (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            alumno_id   INTEGER NOT NULL REFERENCES alumnos(id) ON DELETE CASCADE,
            nrc         TEXT,
            clave       TEXT,
            nombre      TEXT NOT NULL,
            seccion     TEXT,
            tipo        TEXT,
            creditos    INTEGER DEFAULT 0,
            dias        TEXT,
            hora_inicio TEXT,
            hora_fin    TEXT,
            edificio    TEXT,
            aula        TEXT,
            maestro     TEXT,
            ciclo       TEXT,
            UNIQUE(alumno_id, nrc, clave, dias, hora_inicio)
        );
    """)
    conn.commit()


def _get_alumno_id(conn, codigo):
    row = conn.execute(
        "SELECT id FROM alumnos WHERE codigo = ?", (codigo,)
    ).fetchone()
    return row["id"] if row else None


class CaptureOutput:
    def __enter__(self):
        self._old = sys.stdout
        self._buf = io.StringIO()
        sys.stdout = self._buf
        return self._buf

    def __exit__(self, *a):
        sys.stdout = self._old


# ─────────────────────────────────────────────────────────────────
# Utilidad: limpiar objetos para JSON (sets → lists)
# ─────────────────────────────────────────────────────────────────

def clean(obj):
    if isinstance(obj, set):   return list(obj)
    if isinstance(obj, dict):  return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, list):  return [clean(i) for i in obj]
    return obj


# ─────────────────────────────────────────────────────────────────
# Extractor de PDF de horario SIIAU UDG (a medida del formato real)
# ─────────────────────────────────────────────────────────────────

def _extraer_materias_horario_pdf(pdf_path: str) -> tuple[dict, list[dict]]:
    """
    Extrae alumno y materias del PDF de horario SIIAU UDG.
    Retorna (alumno_dict, lista_materias).
    """
    return extraer_horario_siiau(pdf_path)


# ─────────────────────────────────────────────────────────────────
# Frontend
# ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


# ─────────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "version": "1.1"})


# ─────────────────────────────────────────────────────────────────
# Plan de estudios
# ─────────────────────────────────────────────────────────────────

@app.route("/api/plan/status")
def plan_status():
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


# ─────────────────────────────────────────────────────────────────
# Alumnos
# ─────────────────────────────────────────────────────────────────

@app.route("/api/alumnos")
def listar_alumnos():
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
        "alumno":       alumno_dict,
        "materias":     [dict(m) for m in materias],
        "creditos_area":[dict(c) for c in creditos_area],
    })


# ─────────────────────────────────────────────────────────────────
# Perfil completo  GET /api/alumnos/<codigo>/perfil
# ─────────────────────────────────────────────────────────────────

@app.route("/api/alumnos/<codigo>/perfil")
def perfil_alumno(codigo):
    """
    Retorna datos completos del alumno: info, materias, créditos por área,
    horario actual y análisis del motor de inferencia.
    """
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    _ensure_horario_table(conn)

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
        SELECT nrc, clave, nombre, seccion, tipo, creditos,
               dias, hora_inicio, hora_fin, edificio, aula, maestro, ciclo
        FROM horarios WHERE alumno_id=?
        ORDER BY dias, hora_inicio
    """, (alumno_id,)).fetchall()

    conn.close()

    # Motor de inferencia (silencioso)
    try:
        motor = MotorInferencia(db_path=DB_FILE)
        with CaptureOutput():
            analisis = motor.analizar(codigo)
        analisis = clean(analisis) if analisis else {}
    except Exception:
        analisis = {}

    return jsonify({
        "alumno":        alumno_dict,
        "materias":      [dict(m) for m in materias],
        "creditos_area": [dict(c) for c in creditos_area],
        "horario":       [dict(h) for h in horario],
        "analisis":      analisis,
    })


# ─────────────────────────────────────────────────────────────────
# Análisis con motor IA
# ─────────────────────────────────────────────────────────────────

@app.route("/api/alumnos/<codigo>/analizar")
def analizar_alumno(codigo):
    try:
        motor = MotorInferencia(db_path=DB_FILE)
        with CaptureOutput():
            resultado = motor.analizar(codigo)
        if resultado is None:
            return jsonify({"error": f"Alumno {codigo} no encontrado"}), 404
        return jsonify(clean(resultado))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────
# Cargar kárdex PDF
# ─────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════
#  HORARIOS — rutas que faltaban y causaban los errores 404 / 405
# ══════════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────────

# ── 1. Extraer materias de un PDF de horario (SIN guardar en BD)
#       POST /api/horario/extraer
@app.route("/api/horario/extraer", methods=["POST"])
def extraer_horario_pdf():
    """
    Recibe un PDF de horario SIIAU y devuelve las materias detectadas
    sin guardarlas todavía. El frontend puede mostrarlas para que el
    usuario las confirme antes de guardar.
    """
    if "pdf" not in request.files:
        return jsonify({"error": "No se envió ningún archivo PDF"}), 400

    file = request.files["pdf"]
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Solo se aceptan archivos PDF"}), 400

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    try:
        alumno_pdf, materias = _extraer_materias_horario_pdf(tmp_path)
        return jsonify({"ok": True, "materias": materias, "total": len(materias), "alumno": alumno_pdf})
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        os.unlink(tmp_path)


# ── 2. Cargar PDF de horario y guardar automáticamente
#       POST /api/horario/cargar
@app.route("/api/horario/cargar", methods=["POST"])
def cargar_horario_pdf():
    """
    Recibe un PDF de horario y el código del alumno.
    Extrae las materias y las guarda en la tabla horarios.
    Body (multipart): pdf=<archivo>, codigo=<codigo_alumno>, ciclo=<opcional>
    """
    if "pdf" not in request.files:
        return jsonify({"error": "No se envió ningún archivo PDF"}), 400

    file    = request.files["pdf"]
    codigo  = request.form.get("codigo", "").strip()
    ciclo   = request.form.get("ciclo", "").strip()

    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Solo se aceptan archivos PDF"}), 400
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    try:
        alumno_pdf, materias = _extraer_materias_horario_pdf(tmp_path)
        # Usar ciclo del PDF si no se envió en el formulario
        if not ciclo and alumno_pdf.get("ciclo"):
            ciclo = alumno_pdf["ciclo"]
        # Si no se envió código, intentar usar el del PDF
        if not codigo and alumno_pdf.get("codigo"):
            codigo = alumno_pdf["codigo"]
    except RuntimeError as e:
        os.unlink(tmp_path)
        return jsonify({"ok": False, "error": str(e)}), 500
    except Exception as e:
        traceback.print_exc()
        os.unlink(tmp_path)
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    # Guardar en BD
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    _ensure_horario_table(conn)

    alumno_id = _get_alumno_id(conn, codigo)
    if alumno_id is None:
        conn.close()
        return jsonify({"error": f"Alumno {codigo} no encontrado en la BD"}), 404

    insertados = 0
    for m in materias:
        try:
            conn.execute("""
                INSERT OR REPLACE INTO horarios
                    (alumno_id, nrc, clave, nombre, seccion, tipo, creditos,
                     dias, hora_inicio, hora_fin, edificio, aula, maestro, ciclo)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                alumno_id,
                m.get("nrc"),
                m.get("clave"),
                m.get("nombre", "Sin nombre"),
                m.get("seccion"),
                m.get("tipo"),
                m.get("creditos", 0),
                m.get("dias_str") or "".join(m.get("dias", [])),
                m.get("hora_inicio"),
                m.get("hora_fin"),
                m.get("edificio"),
                m.get("aula"),
                m.get("maestro") or m.get("profesor"),
                ciclo or m.get("ciclo"),
            ))
            insertados += 1
        except sqlite3.IntegrityError:
            pass

    conn.commit()
    conn.close()

    return jsonify({
        "ok": True,
        "materias_detectadas": len(materias),
        "materias_guardadas":  insertados,
        "materias":            materias,
        "alumno_pdf":          alumno_pdf,
    })


# ── 3. Obtener horario de un alumno
#       GET /api/alumnos/<codigo>/horario
@app.route("/api/alumnos/<codigo>/horario", methods=["GET"])
def obtener_horario(codigo):
    """Devuelve todas las materias del horario guardado del alumno."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    _ensure_horario_table(conn)

    alumno_id = _get_alumno_id(conn, codigo)
    if alumno_id is None:
        conn.close()
        return jsonify({"error": f"Alumno {codigo} no encontrado"}), 404

    rows = conn.execute("""
        SELECT id, nrc, clave, nombre, seccion, tipo, creditos,
               dias, hora_inicio, hora_fin, edificio, aula, maestro, ciclo
        FROM horarios
        WHERE alumno_id = ?
        ORDER BY dias, hora_inicio
    """, (alumno_id,)).fetchall()
    conn.close()

    return jsonify({"ok": True, "horario": [dict(r) for r in rows]})


# ── 4. Guardar / reemplazar horario manual
#       POST /api/alumnos/<codigo>/horario
@app.route("/api/alumnos/<codigo>/horario", methods=["POST"])
def guardar_horario(codigo):
    """
    Guarda o actualiza el horario del alumno de forma manual.
    Body JSON: { "materias": [...], "ciclo": "...", "reemplazar": true/false }
    Cada materia puede tener: nrc, clave, nombre, dias, hora_inicio,
    hora_fin, aula, edificio, maestro, tipo, creditos, seccion.
    """
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "Body JSON inválido o vacío"}), 400

    materias  = data.get("materias", [])
    ciclo     = data.get("ciclo", "")
    reemplazar = data.get("reemplazar", False)

    if not isinstance(materias, list):
        return jsonify({"error": "'materias' debe ser una lista"}), 400

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    _ensure_horario_table(conn)

    alumno_id = _get_alumno_id(conn, codigo)
    if alumno_id is None:
        conn.close()
        return jsonify({"error": f"Alumno {codigo} no encontrado"}), 404

    if reemplazar:
        conn.execute("DELETE FROM horarios WHERE alumno_id = ?", (alumno_id,))

    insertados = 0
    errores    = []
    for i, m in enumerate(materias):
        nombre = (m.get("nombre") or "").strip()
        if not nombre:
            errores.append(f"Materia #{i+1}: falta el campo 'nombre'")
            continue
        try:
            conn.execute("""
                INSERT OR REPLACE INTO horarios
                    (alumno_id, nrc, clave, nombre, seccion, tipo, creditos,
                     dias, hora_inicio, hora_fin, edificio, aula, maestro, ciclo)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                alumno_id,
                m.get("nrc"),
                m.get("clave"),
                nombre,
                m.get("seccion"),
                m.get("tipo"),
                m.get("creditos", 0),
                m.get("dias"),
                m.get("hora_inicio"),
                m.get("hora_fin"),
                m.get("edificio"),
                m.get("aula"),
                m.get("maestro"),
                ciclo or m.get("ciclo"),
            ))
            insertados += 1
        except sqlite3.IntegrityError as e:
            errores.append(f"Materia '{nombre}': {e}")

    conn.commit()
    conn.close()

    return jsonify({
        "ok":         True,
        "insertados": insertados,
        "errores":    errores,
    })


# ── 5. Eliminar horario completo de un alumno
#       DELETE /api/alumnos/<codigo>/horario
@app.route("/api/alumnos/<codigo>/horario", methods=["DELETE"])
def eliminar_horario(codigo):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    _ensure_horario_table(conn)

    alumno_id = _get_alumno_id(conn, codigo)
    if alumno_id is None:
        conn.close()
        return jsonify({"error": f"Alumno {codigo} no encontrado"}), 404

    conn.execute("DELETE FROM horarios WHERE alumno_id = ?", (alumno_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "mensaje": "Horario eliminado"})


# ── 6. Eliminar una materia específica del horario
#       DELETE /api/alumnos/<codigo>/horario/<int:horario_id>
@app.route("/api/alumnos/<codigo>/horario/<int:horario_id>", methods=["DELETE"])
def eliminar_materia_horario(codigo, horario_id):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    _ensure_horario_table(conn)

    alumno_id = _get_alumno_id(conn, codigo)
    if alumno_id is None:
        conn.close()
        return jsonify({"error": f"Alumno {codigo} no encontrado"}), 404

    cur = conn.execute(
        "DELETE FROM horarios WHERE id = ? AND alumno_id = ?",
        (horario_id, alumno_id)
    )
    deleted = cur.rowcount
    conn.commit()
    conn.close()

    if deleted == 0:
        return jsonify({"error": "Materia no encontrada en el horario"}), 404
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────────
# Eliminar alumno
# ─────────────────────────────────────────────────────────────────

@app.route("/api/alumnos/<codigo>", methods=["DELETE"])
def eliminar_alumno(codigo):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()
    cur.execute("DELETE FROM alumnos WHERE codigo=?", (codigo,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    if deleted == 0:
        return jsonify({"error": "Alumno no encontrado"}), 404
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────────
# Arranque
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db(DB_FILE)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    _ensure_horario_table(conn)
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

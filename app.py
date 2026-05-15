"""
app.py — Smartkardex Web API v2
"""
import os, io, sys, tempfile, traceback
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
CSV_NAMES = [
    "Plan de Estudios IELC - Hoja 6.csv",
    "Plan_de_Estudios_IELC_-_Hoja_6.csv",
]


class CaptureOutput:
    def __enter__(self):
        self._old = sys.stdout
        self._buf = io.StringIO()
        sys.stdout = self._buf
        return self._buf
    def __exit__(self, *a):
        sys.stdout = self._old


def clean(obj):
    if isinstance(obj, set): return list(obj)
    if isinstance(obj, dict): return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, list): return [clean(i) for i in obj]
    return obj


def _ensure_plan():
    """Importa el plan si la tabla está vacía. Se ejecuta al importar el módulo (gunicorn + python)."""
    import sqlite3
    init_db(DB_FILE)
    conn = sqlite3.connect(DB_FILE)
    count = conn.execute("SELECT COUNT(*) FROM plan_estudios").fetchone()[0]
    conn.close()
    if count == 0:
        csv_path = next((str(BASE_DIR / n) for n in CSV_NAMES if (BASE_DIR / n).exists()), None)
        if csv_path:
            print(f"Importando plan desde {csv_path}...")
            with CaptureOutput():
                importar_plan_estudios(db_path=DB_FILE, csv_path=csv_path)
            print("Plan importado OK.")
        else:
            print(f"AVISO: CSV del plan no encontrado en {BASE_DIR}")

# Se ejecuta SIEMPRE al importar (funciona con gunicorn, no solo con __main__)
_ensure_plan()


# ── Frontend ──────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/health")
def health():
    import sqlite3
    conn = sqlite3.connect(DB_FILE)
    plan = conn.execute("SELECT COUNT(*) FROM plan_estudios").fetchone()[0]
    alumnos = conn.execute("SELECT COUNT(*) FROM alumnos").fetchone()[0]
    conn.close()
    return jsonify({"status": "ok", "version": "2.0", "plan_materias": plan, "alumnos": alumnos})

# ── Plan ──────────────────────────────────────────────────
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
        csv_path = next((str(BASE_DIR / n) for n in CSV_NAMES if (BASE_DIR / n).exists()), None)
        if not csv_path:
            return jsonify({"ok": False, "error": f"CSV no encontrado en {BASE_DIR}"}), 404
        with CaptureOutput() as buf:
            importar_plan_estudios(db_path=DB_FILE, csv_path=csv_path)
        return jsonify({"ok": True, "mensaje": buf.getvalue()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── Alumnos ───────────────────────────────────────────────
@app.route("/api/alumnos")
def listar_alumnos():
    import sqlite3
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT codigo, nombre, carrera, creditos_adquiridos,
               creditos_requeridos, promedio, ultimo_ciclo, situacion,
               orientacion_elegida, servicio_social, practicas_profesionales
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
    alumno_id = alumno_dict["id"]
    materias = conn.execute("""
        SELECT clave, nombre, calificacion, estatus, creditos,
               tipo, calendario, fecha_eval
        FROM materias WHERE alumno_id=? ORDER BY calendario, nombre
    """, (alumno_id,)).fetchall()
    conn.close()
    return jsonify({"alumno": alumno_dict, "materias": [dict(m) for m in materias]})

@app.route("/api/alumnos/<codigo>/analizar", methods=["GET", "POST"])
def analizar_alumno(codigo):
    try:
        import sqlite3
        conn = sqlite3.connect(DB_FILE)
        plan_count = conn.execute("SELECT COUNT(*) FROM plan_estudios").fetchone()[0]
        conn.close()
        if plan_count == 0:
            _ensure_plan()

        orientacion = None
        servicio_social = False
        practicas = False
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            orientacion     = data.get("orientacion")
            servicio_social = bool(data.get("servicio_social", False))
            practicas       = bool(data.get("practicas", False))
        else:
            orientacion     = request.args.get("orientacion")
            servicio_social = request.args.get("servicio_social", "").lower() == "true"
            practicas       = request.args.get("practicas", "").lower() == "true"

        motor = MotorInferencia(db_path=DB_FILE)
        with CaptureOutput():
            resultado = motor.analizar(codigo,
                orientacion=orientacion,
                servicio_social=servicio_social,
                practicas=practicas)

        if resultado is None:
            return jsonify({"error": f"Alumno {codigo} no encontrado o plan vacío"}), 404
        return jsonify(clean(resultado))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ── Horario JSON ──────────────────────────────────────────
@app.route("/api/alumnos/<codigo>/horario", methods=["GET"])
def obtener_horario(codigo):
    import sqlite3
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    alumno = conn.execute("SELECT id FROM alumnos WHERE codigo=?", (codigo,)).fetchone()
    if not alumno:
        conn.close()
        return jsonify({"error": "Alumno no encontrado"}), 404
    horario = conn.execute("""
        SELECT clave, nombre, creditos, calendario FROM horario
        WHERE alumno_id=? ORDER BY nombre
    """, (alumno["id"],)).fetchall()
    conn.close()
    return jsonify([dict(h) for h in horario])

@app.route("/api/alumnos/<codigo>/horario", methods=["POST"])
def guardar_horario(codigo):
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"ok": False, "error": "Body JSON requerido"}), 400
        calendario = data.get("calendario", "")
        materias   = data.get("materias", [])
        motor = MotorInferencia(db_path=DB_FILE)
        resultado = motor.guardar_horario(codigo, materias, calendario)
        return jsonify(resultado)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500

# ── Horario PDF ───────────────────────────────────────────
@app.route("/api/alumnos/<codigo>/horario-pdf", methods=["POST"])
def cargar_horario_pdf(codigo):
    """Extrae materias de un PDF de horario de Leo UDG y las guarda."""
    if "pdf" not in request.files:
        return jsonify({"ok": False, "error": "No se envió archivo PDF"}), 400
    file = request.files["pdf"]
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"ok": False, "error": "Solo se aceptan archivos PDF"}), 400

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name
    try:
        materias, calendario = _extraer_horario_pdf(tmp_path)
        if not materias:
            return jsonify({"ok": False, "error": "No se encontraron materias en el PDF"}), 400
        motor = MotorInferencia(db_path=DB_FILE)
        resultado = motor.guardar_horario(codigo, materias, calendario)
        return jsonify({**resultado, "materias": materias, "calendario": calendario})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        os.unlink(tmp_path)


def _extraer_horario_pdf(pdf_path: str):
    """Extrae materias de un PDF de horario Leo UDG."""
    import re
    import pdfplumber

    materias = []
    calendario = ""
    clave_re = re.compile(r'\b([A-Z]{1,3}\d{3,5})\b')

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""

            if not calendario:
                m = re.search(r'(\d{4}[-]\s*[AB])', text, re.IGNORECASE)
                if m:
                    calendario = re.sub(r'\s+', '', m.group(1)).upper()

            # Intentar extraer de tablas
            for tabla in (page.extract_tables() or []):
                for fila in tabla:
                    celdas = [str(c or "").strip() for c in fila]
                    clave = None
                    for celda in celdas:
                        m = clave_re.match(celda)
                        if m:
                            clave = m.group(1)
                            break
                    if not clave:
                        continue
                    nombre = max(
                        (c for c in celdas if c != clave and not c.isdigit() and len(c) > 3),
                        key=len, default=""
                    )
                    creditos = next(
                        (int(c) for c in celdas if c.isdigit() and 1 <= int(c) <= 12), 0
                    )
                    if nombre and not any(x["clave"] == clave for x in materias):
                        materias.append({"clave": clave, "nombre": nombre.upper(), "creditos": creditos})

            # Fallback: extraer del texto línea por línea
            if not materias:
                for linea in text.split("\n"):
                    m = clave_re.search(linea.strip())
                    if not m:
                        continue
                    clave = m.group(1)
                    resto = linea[m.end():].strip()
                    nombre_part = re.sub(r'\b\d+\b', '', resto).strip(" -|:")
                    if len(nombre_part) > 3 and not any(x["clave"] == clave for x in materias):
                        creditos_m = re.search(r'\b(\d{1,2})\b', resto)
                        creditos = int(creditos_m.group(1)) if creditos_m else 0
                        materias.append({"clave": clave, "nombre": nombre_part.upper(), "creditos": creditos})

    return materias, calendario or "2025-B"


# ── Perfil alumno ─────────────────────────────────────────
@app.route("/api/alumnos/<codigo>/perfil", methods=["POST"])
def actualizar_perfil(codigo):
    try:
        import sqlite3
        data = request.get_json(silent=True) or {}
        conn = sqlite3.connect(DB_FILE)
        alumno = conn.execute("SELECT id FROM alumnos WHERE codigo=?", (codigo,)).fetchone()
        if not alumno:
            conn.close()
            return jsonify({"error": "Alumno no encontrado"}), 404
        alumno_id = alumno[0]
        fields, vals = [], []
        if "orientacion" in data:
            fields.append("orientacion_elegida=?"); vals.append(data["orientacion"])
        if "servicio_social" in data:
            fields.append("servicio_social=?"); vals.append(1 if data["servicio_social"] else 0)
        if "practicas" in data:
            fields.append("practicas_profesionales=?"); vals.append(1 if data["practicas"] else 0)
        if fields:
            conn.execute(f"UPDATE alumnos SET {', '.join(fields)} WHERE id=?", vals + [alumno_id])
            conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── Sugerir materia ───────────────────────────────────────
@app.route("/api/alumnos/<codigo>/sugerir", methods=["GET", "POST"])
def sugerir_materia(codigo):
    try:
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            query = data.get("q", "")
            area_manual = data.get("area", None)
        else:
            query = request.args.get("q", "")
            area_manual = request.args.get("area", None)
        if not query:
            return jsonify({"error": "Parámetro 'q' requerido"}), 400
        motor = MotorInferencia(db_path=DB_FILE)
        return jsonify(clean(motor.sugerir_materia(codigo, query, area_manual)))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ── Cargar kárdex PDF ─────────────────────────────────────
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
                   creditos_requeridos, promedio, ultimo_ciclo,
                   orientacion_elegida, servicio_social, practicas_profesionales
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

# ── Eliminar alumno ───────────────────────────────────────
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Smartkardex v2 en http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)

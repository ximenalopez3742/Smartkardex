"""
app.py — Smartkardex Web API  (versión completa)
=================================================
Endpoints:
  GET  /api/health
  GET  /api/plan/status
  POST /api/plan/importar
  GET  /api/plan/areas
  POST /api/plan/equivalencia
  GET  /api/alumnos
  GET  /api/alumnos/<codigo>
  DELETE /api/alumnos/<codigo>
  POST /api/alumnos/<codigo>/perfil
  GET|POST /api/alumnos/<codigo>/analizar
  GET  /api/alumnos/<codigo>/horario
  POST /api/alumnos/<codigo>/horario
  GET  /api/alumnos/<codigo>/sugerir
  POST /api/kardex/cargar
  POST /api/horario/cargar
"""

import os, io, sys, sqlite3, tempfile, traceback
from pathlib import Path
from difflib import SequenceMatcher

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

sys.path.insert(0, os.path.dirname(__file__))

from database import init_db, normalizar
from extractor import KardexExtractor
from motor import MotorInferencia
from plan import importar_plan_estudios

try:
    from extractor_horario import extraer_horario_pdf
    _HORARIO_OK = True
except ImportError:
    _HORARIO_OK = False

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

def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def _similitud(a, b):
    return SequenceMatcher(None, a, b).ratio()

def _migrate_db():
    """Agrega columnas y tablas que pueden no existir en instalaciones antiguas."""
    conn = get_conn()
    cur = conn.cursor()
    cols = {r[1] for r in cur.execute("PRAGMA table_info(alumnos)")}
    for col, dfn in [
        ("orientacion_elegida",     "TEXT DEFAULT ''"),
        ("servicio_social",         "INTEGER DEFAULT 0"),
        ("practicas_profesionales", "INTEGER DEFAULT 0"),
        ("fecha_carga",             "TEXT DEFAULT ''"),
    ]:
        if col not in cols:
            cur.execute(f"ALTER TABLE alumnos ADD COLUMN {col} {dfn}")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS horario (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            alumno_id  INTEGER NOT NULL REFERENCES alumnos(id) ON DELETE CASCADE,
            clave      TEXT NOT NULL,
            nombre     TEXT NOT NULL,
            creditos   INTEGER DEFAULT 0,
            calendario TEXT DEFAULT '',
            UNIQUE(alumno_id, clave)
        )
    """)
    conn.commit()
    conn.close()


# ── Frontend ─────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "version": "1.2"})

# ── Plan ─────────────────────────────────────────────────────
@app.route("/api/plan/status")
def plan_status():
    conn = get_conn()
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

@app.route("/api/plan/areas")
def listar_areas():
    conn = get_conn()
    rows = conn.execute("SELECT DISTINCT area_cod, area FROM plan_estudios ORDER BY area_cod").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/plan/equivalencia", methods=["POST"])
def registrar_equivalencia():
    data = request.get_json(silent=True) or {}
    required = ["clave_plan", "nombre_plan", "creditos_plan", "area_cod", "area"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"ok": False, "error": f"Campos faltantes: {', '.join(missing)}"}), 400
    try:
        motor = MotorInferencia(db_path=DB_FILE)
        ok = motor.registrar_equivalencia(
            clave_plan=data["clave_plan"], nombre_plan=data["nombre_plan"],
            creditos_plan=int(data["creditos_plan"]), area_cod=data["area_cod"],
            area=data["area"], prerrequisito=data.get("prerrequisito", ""),
            orientacion_cod=data.get("orientacion_cod", ""),
            orientacion=data.get("orientacion", ""),
        )
        if ok:
            return jsonify({"ok": True, "mensaje": f"Materia '{data['nombre_plan']}' agregada."})
        return jsonify({"ok": False, "error": "La clave ya existe en el plan."}), 409
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500

# ── Alumnos ──────────────────────────────────────────────────
@app.route("/api/alumnos")
def listar_alumnos():
    conn = get_conn()
    rows = conn.execute("""
        SELECT codigo, nombre, carrera, creditos_adquiridos,
               creditos_requeridos, promedio, ultimo_ciclo, situacion
        FROM alumnos ORDER BY nombre
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/alumnos/<codigo>")
def consultar_alumno(codigo):
    conn = get_conn()
    alumno = conn.execute("SELECT * FROM alumnos WHERE codigo=?", (codigo,)).fetchone()
    if not alumno:
        conn.close()
        return jsonify({"error": f"Alumno {codigo} no encontrado"}), 404
    alumno_id = dict(alumno)["id"]
    materias = conn.execute("""
        SELECT clave, nombre, calificacion, estatus, creditos, tipo, calendario, fecha_eval
        FROM materias WHERE alumno_id=? ORDER BY calendario, nombre
    """, (alumno_id,)).fetchall()
    creditos_area = conn.execute("""
        SELECT area, requeridos, adquiridos, faltantes
        FROM creditos_por_area WHERE alumno_id=? ORDER BY area
    """, (alumno_id,)).fetchall()
    conn.close()
    return jsonify({"alumno": dict(alumno), "materias": [dict(m) for m in materias],
                    "creditos_area": [dict(c) for c in creditos_area]})

@app.route("/api/alumnos/<codigo>", methods=["DELETE"])
def eliminar_alumno(codigo):
    conn = get_conn()
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()
    cur.execute("DELETE FROM alumnos WHERE codigo=?", (codigo,))
    deleted = cur.rowcount
    conn.commit(); conn.close()
    if deleted == 0:
        return jsonify({"error": "Alumno no encontrado"}), 404
    return jsonify({"ok": True})

# ── Perfil ───────────────────────────────────────────────────
@app.route("/api/alumnos/<codigo>/perfil", methods=["POST"])
def guardar_perfil(codigo):
    data = request.get_json(silent=True) or {}
    conn = get_conn()
    alumno = conn.execute("SELECT id FROM alumnos WHERE codigo=?", (codigo,)).fetchone()
    if not alumno:
        conn.close()
        return jsonify({"error": "SESSION_EXPIRED"}), 404
    conn.execute("""
        UPDATE alumnos
        SET orientacion_elegida=?, servicio_social=?, practicas_profesionales=?
        WHERE codigo=?
    """, (
        data.get("orientacion", ""),
        1 if data.get("servicio_social") else 0,
        1 if data.get("practicas") else 0,
        codigo,
    ))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

# ── Analizar ─────────────────────────────────────────────────
@app.route("/api/alumnos/<codigo>/analizar", methods=["GET", "POST"])
def analizar_alumno(codigo):
    """
    GET  → carga el horario guardado en BD y lo excluye de las sugerencias.
    POST → acepta {"materias_en_curso": [{clave, nombre}]} en el body.
    """
    materias_en_curso = []

    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        materias_en_curso = data.get("materias_en_curso", [])
    else:
        try:
            conn = get_conn()
            row = conn.execute("SELECT id FROM alumnos WHERE codigo=?", (codigo,)).fetchone()
            if row:
                hor = conn.execute(
                    "SELECT clave, nombre FROM horario WHERE alumno_id=?", (row["id"],)
                ).fetchall()
                materias_en_curso = [{"clave": r["clave"], "nombre": r["nombre"]} for r in hor]
            conn.close()
        except Exception:
            pass

    try:
        motor = MotorInferencia(db_path=DB_FILE)
        with CaptureOutput():
            resultado = motor.analizar(codigo, materias_en_curso=materias_en_curso)
        if resultado is None:
            return jsonify({"error": "SESSION_EXPIRED"}), 404

        def clean(obj):
            if isinstance(obj, set):  return list(obj)
            if isinstance(obj, dict): return {k: clean(v) for k, v in obj.items()}
            if isinstance(obj, list): return [clean(i) for i in obj]
            return obj

        return jsonify(clean(resultado))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ── Horario (BD) ─────────────────────────────────────────────
@app.route("/api/alumnos/<codigo>/horario", methods=["GET"])
def get_horario(codigo):
    conn = get_conn()
    alumno = conn.execute("SELECT id FROM alumnos WHERE codigo=?", (codigo,)).fetchone()
    if not alumno:
        conn.close()
        return jsonify([])
    rows = conn.execute(
        "SELECT clave, nombre, creditos, calendario FROM horario WHERE alumno_id=? ORDER BY clave",
        (alumno["id"],)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/alumnos/<codigo>/horario", methods=["POST"])
def save_horario(codigo):
    data = request.get_json(silent=True) or {}
    materias   = data.get("materias", [])
    calendario = data.get("calendario", "")
    conn = get_conn()
    alumno = conn.execute("SELECT id FROM alumnos WHERE codigo=?", (codigo,)).fetchone()
    if not alumno:
        conn.close()
        return jsonify({"error": "SESSION_EXPIRED"}), 404
    alumno_id = alumno["id"]
    conn.execute("DELETE FROM horario WHERE alumno_id=?", (alumno_id,))
    guardadas = 0
    for m in materias:
        clave  = str(m.get("clave", "")).strip().upper()
        nombre = str(m.get("nombre", "")).strip()
        creditos = int(m.get("creditos", 0))
        if not clave:
            continue
        try:
            conn.execute("""
                INSERT OR REPLACE INTO horario (alumno_id, clave, nombre, creditos, calendario)
                VALUES (?, ?, ?, ?, ?)
            """, (alumno_id, clave, nombre, creditos, calendario))
            guardadas += 1
        except Exception:
            pass
    conn.commit(); conn.close()
    return jsonify({"ok": True, "guardadas": guardadas})

# ── Sugerir / Equivalencia ───────────────────────────────────
@app.route("/api/alumnos/<codigo>/sugerir")
def sugerir_materia(codigo):
    q    = (request.args.get("q", "")).strip()
    area = (request.args.get("area", "")).strip()
    conn = get_conn()

    def plan_dict(row):
        return {"clave": row["clave"], "materia": row["materia"], "area_cod": row["area_cod"],
                "area": row["area"], "orientacion": row["orientacion"] or "", "creditos": row["creditos"]}

    if area:
        rows = conn.execute("""
            SELECT clave, materia, area_cod, area, orientacion, creditos
            FROM plan_estudios WHERE area=? ORDER BY materia
        """, (area,)).fetchall()
        conn.close()
        return jsonify({"filtro": "area", "lista_area": [plan_dict(r) for r in rows],
                        "mensaje": f"Materias en el área '{area}'"})

    if not q:
        conn.close()
        return jsonify({"encontrado": False, "filtro": "ninguno", "mensaje": "Sin búsqueda"})

    # Clave exacta
    row = conn.execute("""
        SELECT clave, materia, area_cod, area, orientacion, creditos
        FROM plan_estudios WHERE UPPER(TRIM(clave))=?
    """, (q.upper(),)).fetchone()
    if row:
        conn.close()
        return jsonify({"encontrado": True, "filtro": "clave", "materia": plan_dict(row)})

    # Similitud de nombre
    q_norm = normalizar(q)
    rows = conn.execute("SELECT clave, materia, area_cod, area, orientacion, creditos FROM plan_estudios").fetchall()
    mejor, mejor_sim = None, 0.0
    for r in rows:
        sim = _similitud(q_norm, normalizar(r["materia"]))
        if sim > mejor_sim:
            mejor_sim, mejor = sim, r

    if mejor and mejor_sim >= 0.55:
        conn.close()
        return jsonify({"encontrado": True, "filtro": "nombre",
                        "similitud": mejor_sim, "materia": plan_dict(mejor)})

    # No encontrado → devolver áreas
    areas = [r["area"] for r in conn.execute("SELECT DISTINCT area FROM plan_estudios ORDER BY area").fetchall()]
    conn.close()
    return jsonify({
        "encontrado": False, "filtro": "ninguno", "areas": areas,
        "mensaje": (f"No se encontró '{q}' en el plan de estudios. "
                    "Selecciona el área a la que pertenece para ver las materias disponibles."),
    })

# ── Cargar Kárdex PDF ────────────────────────────────────────
@app.route("/api/kardex/cargar", methods=["POST"])
def cargar_kardex():
    if "pdf" not in request.files:
        return jsonify({"error": "No se envió ningún archivo PDF"}), 400
    file = request.files["pdf"]
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Solo se aceptan archivos PDF"}), 400
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        file.save(tmp.name); tmp_path = tmp.name
    try:
        extractor = KardexExtractor(db_path=DB_FILE)
        with CaptureOutput() as buf:
            extractor.cargar_pdf(tmp_path)
        conn = get_conn()
        alumno = conn.execute("""
            SELECT codigo, nombre, carrera, creditos_adquiridos, creditos_requeridos,
                   promedio, ultimo_ciclo, orientacion_elegida, servicio_social, practicas_profesionales
            FROM alumnos ORDER BY fecha_carga DESC, id DESC LIMIT 1
        """).fetchone()
        conn.close()
        return jsonify({"ok": True, "log": buf.getvalue(), "alumno": dict(alumno) if alumno else None})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        os.unlink(tmp_path)

# ── Cargar Horario PDF ───────────────────────────────────────
@app.route("/api/horario/cargar", methods=["POST"])
def cargar_horario_pdf():
    if not _HORARIO_OK:
        return jsonify({"ok": False, "error": "extractor_horario.py no disponible."}), 500
    if "pdf" not in request.files:
        return jsonify({"ok": False, "error": "No se envió ningún archivo PDF"}), 400
    file = request.files["pdf"]
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"ok": False, "error": "Solo se aceptan archivos PDF"}), 400
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        file.save(tmp.name); tmp_path = tmp.name
    try:
        resultado  = extraer_horario_pdf(tmp_path)
        materias   = resultado.get("materias", [])
        calendario = resultado.get("calendario", "")
        aviso = ("No se encontraron materias en el PDF. "
                 "Verifica que sea el horario de SIIAUESCOLAR.") if not materias else None
        return jsonify({"ok": True, "materias": materias, "calendario": calendario,
                        "codigo": resultado.get("codigo", ""),
                        "nombre": resultado.get("nombre", ""), "aviso": aviso})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        os.unlink(tmp_path)

# ── Arranque ──────────────────────────────────────────────────
if __name__ == "__main__":
    init_db(DB_FILE)
    _migrate_db()
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

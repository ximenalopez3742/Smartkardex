"""
app.py — Smartkardex Web API
Flask backend que expone el sistema de Kárdex UDG como REST API.
En producción (Render) también sirve el frontend estático.

Endpoints implementados:
  GET  /api/health
  GET  /api/plan/status
  POST /api/plan/importar
  GET  /api/alumnos
  GET  /api/alumnos/<codigo>
  DELETE /api/alumnos/<codigo>
  POST /api/alumnos/<codigo>/perfil          ← nuevo
  GET  /api/alumnos/<codigo>/analizar
  POST /api/alumnos/<codigo>/analizar        ← acepta horario en body
  GET  /api/alumnos/<codigo>/horario         ← nuevo
  POST /api/alumnos/<codigo>/horario         ← nuevo
  GET  /api/alumnos/<codigo>/sugerir         ← nuevo
  POST /api/kardex/cargar
  POST /api/horario/cargar                   ← nuevo (PDF horario)
"""

import os
import io
import sys
import json
import tempfile
import traceback
import re
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

sys.path.insert(0, os.path.dirname(__file__))

from database import init_db, normalizar
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


def get_conn():
    import sqlite3
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ── Servir frontend ──────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


# ── Health ───────────────────────────────────────────────────
@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "version": "1.2"})


# ── Plan de estudios ─────────────────────────────────────────
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


# ── Listar alumnos ───────────────────────────────────────────
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


# ── Consultar alumno ─────────────────────────────────────────
@app.route("/api/alumnos/<codigo>")
def consultar_alumno(codigo):
    conn = get_conn()
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

    creditos_area = conn.execute("""
        SELECT area, requeridos, adquiridos, faltantes
        FROM creditos_por_area WHERE alumno_id=? ORDER BY area
    """, (alumno_id,)).fetchall()

    conn.close()
    return jsonify({
        "alumno": alumno_dict,
        "materias": [dict(m) for m in materias],
        "creditos_area": [dict(c) for c in creditos_area],
    })


# ── Guardar perfil (orientación, servicio, prácticas) ────────
@app.route("/api/alumnos/<codigo>/perfil", methods=["POST"])
def guardar_perfil(codigo):
    """
    Guarda preferencias del alumno: orientación elegida,
    si ya realizó servicio social y/o prácticas profesionales.
    """
    try:
        conn = get_conn()
        alumno = conn.execute(
            "SELECT id FROM alumnos WHERE codigo=?", (codigo,)
        ).fetchone()

        if not alumno:
            conn.close()
            # Si el alumno no está aún (sesión perdida), devolver SESSION_EXPIRED
            return jsonify({"error": "SESSION_EXPIRED"}), 404

        body = request.get_json(silent=True) or {}
        orientacion = body.get("orientacion", "") or ""
        servicio_social = bool(body.get("servicio_social", False))
        practicas = bool(body.get("practicas", False))

        # Guardar en columnas extras — las añadimos si no existen
        # (ALTER TABLE es idempotente con el try/except)
        for col, typ in [
            ("orientacion_elegida", "TEXT"),
            ("servicio_social", "INTEGER"),
            ("practicas_profesionales", "INTEGER"),
        ]:
            try:
                conn.execute(f"ALTER TABLE alumnos ADD COLUMN {col} {typ}")
                conn.commit()
            except Exception:
                pass  # columna ya existe

        conn.execute("""
            UPDATE alumnos
            SET orientacion_elegida=?, servicio_social=?, practicas_profesionales=?
            WHERE codigo=?
        """, (orientacion or None, int(servicio_social), int(practicas), codigo))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ── Analizar alumno ──────────────────────────────────────────
@app.route("/api/alumnos/<codigo>/analizar", methods=["GET", "POST"])
def analizar_alumno(codigo):
    """
    Analiza el kárdex y devuelve materias disponibles, bloqueadas, alertas y avance.

    El horario activo (materias ya inscritas) puede enviarse para:
      1. Excluirlas de las sugerencias (ya están inscritas).
      2. No sumar sus créditos al avance real por área.
      3. Comparar por CLAVE y por NOMBRE (normalizado).

    Formas de enviar el horario:
      POST body JSON: { "horario": [{"clave":"I5886","nombre":"CALCULO DIFERENCIAL"}, ...] }
      GET query:      ?horario=I5886,I5887  (solo claves)
    """
    try:
        horario: list[dict] = []

        # Intentar cargar el horario guardado en BD primero
        conn = get_conn()
        alumno_row = conn.execute(
            "SELECT id FROM alumnos WHERE codigo=?", (codigo,)
        ).fetchone()

        if not alumno_row:
            conn.close()
            return jsonify({"error": "SESSION_EXPIRED"}), 404

        alumno_id = alumno_row["id"]

        # Horario persistido en BD
        try:
            saved = conn.execute("""
                SELECT clave, nombre, creditos, calendario
                FROM horario_materias WHERE alumno_id=?
                ORDER BY id
            """, (alumno_id,)).fetchall()
            horario = [dict(r) for r in saved]
        except Exception:
            horario = []
        conn.close()

        # Si llega horario explícito en la petición, lo usa en su lugar
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            req_horario = body.get("horario", [])
            if req_horario:
                horario = [
                    h if isinstance(h, dict) else {"clave": str(h)}
                    for h in req_horario
                ]
        else:
            claves_raw = request.args.get("horario", "")
            if claves_raw:
                horario = [{"clave": c.strip()} for c in claves_raw.split(",") if c.strip()]

        motor = MotorInferencia(db_path=DB_FILE)
        with CaptureOutput():
            resultado = motor.analizar(codigo, horario=horario)

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


# ── Horario del alumno (GET: leer, POST: guardar) ────────────
@app.route("/api/alumnos/<codigo>/horario", methods=["GET", "POST"])
def horario_alumno(codigo):
    """
    GET  → devuelve la lista de materias del horario activo guardado.
    POST → guarda el horario (calendario + materias) en la BD.
           Body: { "calendario": "2025-B", "materias": [{clave, nombre, creditos}, ...] }
    """
    try:
        conn = get_conn()
        alumno = conn.execute(
            "SELECT id FROM alumnos WHERE codigo=?", (codigo,)
        ).fetchone()
        if not alumno:
            conn.close()
            return jsonify({"error": "SESSION_EXPIRED"}), 404
        alumno_id = alumno["id"]

        # Asegurar que la tabla existe
        conn.execute("""
            CREATE TABLE IF NOT EXISTS horario_materias (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                alumno_id INTEGER NOT NULL REFERENCES alumnos(id) ON DELETE CASCADE,
                calendario TEXT,
                clave     TEXT NOT NULL,
                nombre    TEXT,
                creditos  INTEGER DEFAULT 0
            )
        """)
        conn.commit()

        if request.method == "GET":
            rows = conn.execute("""
                SELECT clave, nombre, creditos, calendario
                FROM horario_materias WHERE alumno_id=? ORDER BY id
            """, (alumno_id,)).fetchall()
            conn.close()
            return jsonify([dict(r) for r in rows])

        # POST — reemplazar horario completo
        body = request.get_json(silent=True) or {}
        calendario = (body.get("calendario") or "").strip()
        materias   = body.get("materias", [])

        conn.execute("DELETE FROM horario_materias WHERE alumno_id=?", (alumno_id,))
        guardadas = 0
        for m in materias:
            clave   = str(m.get("clave", "")).strip().upper()
            nombre  = str(m.get("nombre", "")).strip().upper()
            creditos = int(m.get("creditos") or 0)
            if not clave:
                continue
            conn.execute("""
                INSERT INTO horario_materias (alumno_id, calendario, clave, nombre, creditos)
                VALUES (?,?,?,?,?)
            """, (alumno_id, calendario, clave, nombre, creditos))
            guardadas += 1

        conn.commit()
        conn.close()
        return jsonify({"ok": True, "guardadas": guardadas, "calendario": calendario})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ── Buscar materia / equivalencia ────────────────────────────
@app.route("/api/alumnos/<codigo>/sugerir")
def sugerir_materia(codigo):
    """
    Busca una materia en el plan de estudios por clave o nombre.
    Query param: ?q=<clave o nombre>  &area=<nombre de área> (opcional)

    Respuesta posible:
      { encontrado: true,  filtro: 'clave'|'nombre', similitud, materia: {...} }
      { encontrado: false, filtro: 'ninguno', mensaje, areas: [...] }
      { encontrado: false, filtro: 'area', mensaje, lista_area: [...] }
    """
    try:
        q   = request.args.get("q", "").strip()
        area_sel = request.args.get("area", "").strip()
        conn = get_conn()

        if area_sel:
            rows = conn.execute("""
                SELECT clave, materia, area, orientacion, creditos
                FROM plan_estudios WHERE area=? ORDER BY materia
            """, (area_sel,)).fetchall()
            conn.close()
            return jsonify({
                "encontrado": False,
                "filtro": "area",
                "mensaje": f"Materias en el área '{area_sel}'",
                "lista_area": [dict(r) for r in rows],
            })

        if not q:
            conn.close()
            return jsonify({"encontrado": False, "filtro": "ninguno", "mensaje": "Sin consulta"}), 400

        # 1. Buscar por clave exacta
        q_upper = q.upper().strip()
        row = conn.execute(
            "SELECT clave, materia, area, orientacion, creditos FROM plan_estudios WHERE UPPER(TRIM(clave))=?",
            (q_upper,)
        ).fetchone()
        if row:
            conn.close()
            return jsonify({"encontrado": True, "filtro": "clave", "similitud": 1.0, "materia": dict(row)})

        # 2. Buscar por nombre normalizado (similitud)
        q_norm = normalizar(q)
        plan_rows = conn.execute(
            "SELECT clave, materia, area, orientacion, creditos FROM plan_estudios"
        ).fetchall()

        mejor = None
        mejor_sim = 0.0
        for pr in plan_rows:
            n_norm = normalizar(pr["materia"])
            # Similitud simple: proporción de palabras del query que aparecen en el nombre
            palabras_q = set(q_norm.split())
            palabras_n = set(n_norm.split())
            if not palabras_q:
                continue
            comunes = palabras_q & palabras_n
            sim = len(comunes) / len(palabras_q)
            if sim > mejor_sim:
                mejor_sim = sim
                mejor = pr

        if mejor and mejor_sim >= 0.5:
            conn.close()
            return jsonify({
                "encontrado": True,
                "filtro": "nombre",
                "similitud": mejor_sim,
                "materia": dict(mejor),
            })

        # 3. No encontrada — devolver lista de áreas disponibles
        areas = [r[0] for r in conn.execute(
            "SELECT DISTINCT area FROM plan_estudios ORDER BY area"
        ).fetchall()]
        conn.close()
        return jsonify({
            "encontrado": False,
            "filtro": "ninguno",
            "mensaje": f"No se encontró '{q}' en el plan de estudios.",
            "areas": areas,
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ── Cargar horario desde PDF ─────────────────────────────────
@app.route("/api/horario/cargar", methods=["POST"])
def cargar_horario_pdf():
    """
    Recibe un PDF de horario escolar y extrae las materias inscritas.
    Devuelve: { ok, materias: [{clave, nombre, creditos}], calendario, aviso }

    La extracción usa heurísticas sobre el formato de horario UDG:
      - Detecta claves con patrón letra(s)+dígitos (ej: I5886, CC101)
      - Extrae el nombre de la materia de la celda adyacente
      - Detecta el calendario del encabezado
    """
    if "pdf" not in request.files:
        return jsonify({"ok": False, "error": "No se envió ningún archivo PDF"}), 400

    file = request.files["pdf"]
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"ok": False, "error": "Solo se aceptan archivos PDF"}), 400

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    try:
        import pdfplumber

        materias_dict = {}   # clave → {clave, nombre, creditos}
        calendario = ""
        aviso = ""

        CAL_RE    = re.compile(r"\b(\d{4}[-–][AB])\b")
        CLAVE_RE  = re.compile(r"^[A-Z]{1,3}\d{3,5}$", re.IGNORECASE)
        CRED_RE   = re.compile(r"\b(\d{1,2})\s*cr", re.IGNORECASE)

        with pdfplumber.open(tmp_path) as pdf:
            full_text = ""
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                full_text += page_text + "\n"

                # Detectar calendario en el texto de la página
                if not calendario:
                    m = CAL_RE.search(page_text)
                    if m:
                        calendario = m.group(1).replace("–", "-")

                # Intentar extraer materias de tablas
                for tabla in page.extract_tables():
                    for fila in tabla:
                        celdas = [str(c or "").strip() for c in fila]
                        for i, celda in enumerate(celdas):
                            celda_clean = celda.replace(" ", "").upper()
                            if CLAVE_RE.match(celda_clean):
                                clave = celda_clean
                                # Nombre: celda siguiente no vacía
                                nombre = ""
                                for j in range(i + 1, min(i + 4, len(celdas))):
                                    if celdas[j] and not CLAVE_RE.match(celdas[j].replace(" ", "")):
                                        nombre = celdas[j].upper()
                                        break
                                # Créditos: buscar número de créditos en la misma fila
                                creditos = 0
                                for c2 in celdas:
                                    cm = CRED_RE.search(c2)
                                    if cm:
                                        creditos = int(cm.group(1))
                                        break
                                if nombre and clave not in materias_dict:
                                    materias_dict[clave] = {
                                        "clave": clave,
                                        "nombre": nombre,
                                        "creditos": creditos,
                                    }

            # Fallback: extraer claves del texto completo si no se encontró nada en tablas
            if not materias_dict:
                lineas = full_text.splitlines()
                for linea in lineas:
                    tokens = linea.split()
                    for tok in tokens:
                        tok_clean = tok.replace(" ", "").upper()
                        if CLAVE_RE.match(tok_clean) and tok_clean not in materias_dict:
                            materias_dict[tok_clean] = {
                                "clave": tok_clean,
                                "nombre": tok_clean,
                                "creditos": 0,
                            }
                if materias_dict:
                    aviso = "Se extrajeron solo claves del PDF; los nombres no pudieron detectarse automáticamente."

        materias = list(materias_dict.values())

        if not materias:
            return jsonify({
                "ok": False,
                "error": "No se encontraron materias en el PDF. Verifica que sea un horario UDG.",
            }), 422

        return jsonify({
            "ok": True,
            "materias": materias,
            "calendario": calendario,
            "aviso": aviso,
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        os.unlink(tmp_path)


# ── Cargar kárdex PDF ────────────────────────────────────────
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

        conn = get_conn()
        alumno = conn.execute("""
            SELECT codigo, nombre, carrera, creditos_adquiridos,
                   creditos_requeridos, promedio, ultimo_ciclo,
                   orientacion_elegida, servicio_social, practicas_profesionales
            FROM alumnos ORDER BY fecha_carga DESC, id DESC LIMIT 1
        """).fetchone()
        conn.close()

        return jsonify({
            "ok": True,
            "log": buf.getvalue(),
            "alumno": dict(alumno) if alumno else None,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        os.unlink(tmp_path)


# ── Eliminar alumno ───────────────────────────────────────────
@app.route("/api/alumnos/<codigo>", methods=["DELETE"])
def eliminar_alumno(codigo):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM alumnos WHERE codigo=?", (codigo,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    if deleted == 0:
        return jsonify({"error": "Alumno no encontrado"}), 404
    return jsonify({"ok": True})


# ── Arranque ──────────────────────────────────────────────────
if __name__ == "__main__":
    init_db(DB_FILE)

    # Asegurar columnas de perfil en la tabla alumnos
    conn = get_conn()
    for col, typ in [
        ("orientacion_elegida",    "TEXT"),
        ("servicio_social",        "INTEGER DEFAULT 0"),
        ("practicas_profesionales","INTEGER DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE alumnos ADD COLUMN {col} {typ}")
            conn.commit()
        except Exception:
            pass

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

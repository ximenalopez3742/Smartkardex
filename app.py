"""
app.py — Backend Flask para SmartKardex Web
============================================
Endpoints:
  POST /api/cargar-kardex      → sube y extrae un PDF de kárdex
  POST /api/importar-plan      → importa el CSV del plan de estudios
  POST /api/analizar           → análisis académico (motor de inferencia)
  POST /api/cargar-horario     → extrae claves del PDF de horario
  GET  /api/alumno/<codigo>    → datos del alumno desde la BD
  GET  /api/alumnos            → lista todos los alumnos
  GET  /api/plan               → lista el plan de estudios cargado
  GET  /health                 → healthcheck para Render

Despliegue en Render:
  - Runtime: Python 3.11
  - Build command:  pip install -r requirements.txt
  - Start command:  gunicorn app:app --timeout 120

Variables de entorno (opcionales):
  KARDEX_DB_PATH   → ruta a la BD SQLite (default: kardex_udg.db)
  PLAN_CSV_PATH    → ruta al CSV del plan (default: Plan de Estudios IELC - Hoja 6.csv)
  SECRET_KEY       → clave secreta Flask (genera una aleatoria si no se pone)
"""

import os
import tempfile
import traceback
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
DB_PATH      = os.environ.get("KARDEX_DB_PATH", "kardex_udg.db")
PLAN_CSV     = os.environ.get("PLAN_CSV_PATH", "Plan de Estudios IELC - Hoja 6.csv")
SECRET_KEY   = os.environ.get("SECRET_KEY", os.urandom(24).hex())
MAX_MB       = 20  # tamaño máximo de PDF en MB

app = Flask(__name__)
app.config["SECRET_KEY"]      = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024

# CORS abierto — en producción restringe a tu dominio:
#   CORS(app, origins=["https://smartkardex.onrender.com"])
CORS(app)

# Inicializar BD al arrancar
init_db(DB_PATH)

# Importar el plan automáticamente si existe el CSV y la tabla está vacía
import sqlite3 as _sqlite3
def _plan_vacio() -> bool:
    try:
        conn = _sqlite3.connect(DB_PATH)
        n = conn.execute("SELECT COUNT(*) FROM plan_estudios").fetchone()[0]
        conn.close()
        return n == 0
    except Exception:
        return True

if _plan_vacio() and Path(PLAN_CSV).exists():
    log.info("Auto-importando plan de estudios desde %s", PLAN_CSV)
    importar_plan_estudios(db_path=DB_PATH, csv_path=PLAN_CSV)


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def _err(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": msg}), code

def _ok(data: dict):
    return jsonify({"ok": True, **data})


# ─────────────────────────────────────────────────────────────────
# Healthcheck
# ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return jsonify({"status": "ok"})


# ─────────────────────────────────────────────────────────────────
# POST /api/cargar-kardex
# ─────────────────────────────────────────────────────────────────

@app.post("/api/cargar-kardex")
def cargar_kardex():
    """
    Recibe un PDF de kárdex (multipart/form-data, campo 'file').
    Extrae los datos y los guarda en la BD.
    Retorna el resumen del alumno.
    """
    if "file" not in request.files:
        return _err("Se requiere el campo 'file' con el PDF del kárdex.")
    
    archivo = request.files["file"]
    if not archivo.filename.lower().endswith(".pdf"):
        return _err("Solo se aceptan archivos PDF.")

    # Guardar en archivo temporal
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp_path = tmp.name
        archivo.save(tmp_path)

    try:
        extractor = KardexExtractor(db_path=DB_PATH)
        resultado = extractor.cargar_pdf(tmp_path)
        if not resultado:
            return _err("No se pudo extraer información del PDF. "
                        "Verifica que sea un kárdex UDG válido.")
        return _ok({"resumen": resultado})
    except Exception as e:
        log.exception("Error al cargar kárdex")
        return _err(f"Error interno: {str(e)}", 500)
    finally:
        os.unlink(tmp_path)


# ─────────────────────────────────────────────────────────────────
# POST /api/cargar-horario
# ─────────────────────────────────────────────────────────────────

@app.post("/api/cargar-horario")
def cargar_horario():
    """
    Recibe un PDF de horario Leo UDG (campo 'file').
    Retorna las materias inscritas en el ciclo actual.
    """
    if "file" not in request.files:
        return _err("Se requiere el campo 'file' con el PDF del horario.")

    archivo = request.files["file"]
    if not archivo.filename.lower().endswith(".pdf"):
        return _err("Solo se aceptan archivos PDF.")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp_path = tmp.name
        archivo.save(tmp_path)

    try:
        from horario_extractor import extraer_horario
        datos = extraer_horario(tmp_path)
        if not datos:
            return _err("No se pudieron extraer materias del horario. "
                        "Verifica que sea un horario Leo UDG válido.")
        return _ok({"horario": datos})
    except Exception as e:
        log.exception("Error al cargar horario")
        return _err(f"Error interno: {str(e)}", 500)
    finally:
        os.unlink(tmp_path)


# ─────────────────────────────────────────────────────────────────
# POST /api/analizar
# ─────────────────────────────────────────────────────────────────

@app.post("/api/analizar")
def analizar():
    """
    Analiza el avance académico de un alumno.

    JSON body:
    {
      "codigo":            "222937383",
      "servicio_social":   false,        // ¿ya acreditó el SS?
      "practicas_prof":    false,        // ¿ya acreditó las PP?
      "orientacion":       "OT",         // orientación elegida (opcional)
      "horario_claves":    ["IE123", "IE456"],  // claves del horario actual
      "horario_nombres":   []            // nombres normalizados (opcional)
    }
    """
    data = request.get_json(silent=True) or {}
    codigo = (data.get("codigo") or "").strip()
    if not codigo:
        return _err("Se requiere el campo 'codigo' del alumno.")

    horario_claves  = set(str(c).upper() for c in data.get("horario_claves", []))
    horario_nombres = set(normalizar(str(n)) for n in data.get("horario_nombres", []))

    try:
        motor    = MotorInferencia(db_path=DB_PATH)
        resultado = motor.analizar(
            codigo_alumno   = codigo,
            horario_claves  = horario_claves,
            horario_nombres = horario_nombres,
            servicio_social = bool(data.get("servicio_social", False)),
            practicas_prof  = bool(data.get("practicas_prof",  False)),
            orientacion     = (data.get("orientacion") or "").strip().upper(),
        )
        if resultado is None or "error" in resultado:
            return _err(resultado.get("error", "Error desconocido."))
        return _ok({"analisis": resultado})
    except Exception as e:
        log.exception("Error en análisis")
        return _err(f"Error interno: {str(e)}", 500)


# ─────────────────────────────────────────────────────────────────
# POST /api/importar-plan
# ─────────────────────────────────────────────────────────────────

@app.post("/api/importar-plan")
def importar_plan():
    """
    Importa el plan de estudios desde el CSV.
    Acepta CSV como archivo (campo 'file') o usa el CSV en disco.
    """
    tmp_path = None
    try:
        if "file" in request.files:
            archivo = request.files["file"]
            with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
                tmp_path = tmp.name
                archivo.save(tmp_path)
            csv_to_use = tmp_path
        elif Path(PLAN_CSV).exists():
            csv_to_use = PLAN_CSV
        else:
            return _err(f"No se encontró el CSV del plan. "
                        f"Sube el archivo o colócalo como '{PLAN_CSV}'.")

        importar_plan_estudios(db_path=DB_PATH, csv_path=csv_to_use)
        
        conn = _sqlite3.connect(DB_PATH)
        total = conn.execute("SELECT COUNT(*) FROM plan_estudios").fetchone()[0]
        conn.close()
        return _ok({"mensaje": f"Plan importado correctamente. {total} materias en BD."})
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
# GET /api/alumno/<codigo>
# ─────────────────────────────────────────────────────────────────

@app.get("/api/alumno/<codigo>")
def get_alumno(codigo: str):
    """Retorna los datos completos del alumno (incluyendo materias y áreas)."""
    try:
        extractor = KardexExtractor(db_path=DB_PATH)
        conn = extractor.conn
        fila = conn.execute(
            "SELECT id FROM alumnos WHERE codigo=?", (codigo,)
        ).fetchone()
        if not fila:
            return _err(f"Alumno {codigo} no encontrado.", 404)
        res = extractor._resumen(fila["id"])
        return _ok({"alumno_data": res})
    except Exception as e:
        log.exception("Error al consultar alumno")
        return _err(f"Error interno: {str(e)}", 500)


# ─────────────────────────────────────────────────────────────────
# GET /api/alumnos
# ─────────────────────────────────────────────────────────────────

@app.get("/api/alumnos")
def listar_alumnos():
    """Lista todos los alumnos registrados en la BD."""
    try:
        conn = init_db(DB_PATH)
        rows = conn.execute("""
            SELECT codigo, nombre, carrera, ultimo_ciclo,
                   creditos_adquiridos, creditos_requeridos, promedio
            FROM alumnos ORDER BY nombre
        """).fetchall()
        alumnos = [dict(r) for r in rows]
        return _ok({"alumnos": alumnos, "total": len(alumnos)})
    except Exception as e:
        return _err(f"Error: {str(e)}", 500)


# ─────────────────────────────────────────────────────────────────
# GET /api/plan
# ─────────────────────────────────────────────────────────────────

@app.get("/api/plan")
def get_plan():
    """Retorna el plan de estudios cargado en la BD."""
    try:
        conn = init_db(DB_PATH)
        rows = conn.execute("""
            SELECT clave, materia, area_cod, area, orientacion_cod, orientacion,
                   tipo, creditos, prerrequisito
            FROM plan_estudios ORDER BY area_cod, materia
        """).fetchall()
        return _ok({"plan": [dict(r) for r in rows], "total": len(rows)})
    except Exception as e:
        return _err(f"Error: {str(e)}", 500)


# ─────────────────────────────────────────────────────────────────
# Manejo de errores globales
# ─────────────────────────────────────────────────────────────────

@app.errorhandler(413)
def too_large(e):
    return _err(f"El archivo excede el tamaño máximo de {MAX_MB} MB.", 413)

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

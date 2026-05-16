"""
database.py — Inicialización de BD y constantes compartidas
============================================================
Módulo base que todos los demás importan.
Centraliza la conexión SQLite y las constantes institucionales UDG.
"""

import sqlite3
import logging
from pathlib import Path

# ─────────────────────────────────────────────────────────────────
# Configuración de logging
# ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# Constantes institucionales UDG / IELC
# ─────────────────────────────────────────────────────────────────
DB_PATH                = "kardex_udg.db"

CALIFICACION_MINIMA    = 60      # mínimo aprobatorio UDG
MAX_REPROBACIONES      = 2       # periodos distintos → riesgo Art. 33
PCT_SERVICIO_SOCIAL    = 0.60    # 60 % de créditos totales
PCT_PRACTICAS_PROF     = 0.70    # 70 % de créditos totales
TOP_SUGERENCIAS        = 10      # máximo de materias sugeridas
CREDITOS_TOTALES_IELC  = 423     # créditos mínimos para egresar (fallback)

# Créditos requeridos por código de carrera
CREDITOS_POR_CARRERA = {
    "IELC": 423,
    "ICOM": 423,
}

# Palabras clave en nombre de carrera → créditos (fallback)
CARRERA_KEYWORDS = {
    "ELECTRONICA Y COMPUTACION": 423,
    "ELECTRÓNICA Y COMPUTACIÓN": 423,
}


# ─────────────────────────────────────────────────────────────────
# Conexión
# ─────────────────────────────────────────────────────────────────

def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Abre la conexión con row_factory y foreign keys activados."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    """
    Crea todas las tablas si no existen.
    Retorna la conexión abierta lista para usar.
    """
    conn = get_connection(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS alumnos (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo              TEXT UNIQUE NOT NULL,
            nombre              TEXT,
            carrera             TEXT,
            codigo_carrera      TEXT,
            nivel               TEXT,
            centro              TEXT,
            sede                TEXT,
            ciclo_admision      TEXT,
            ultimo_ciclo        TEXT,
            situacion           TEXT,
            creditos_adquiridos INTEGER DEFAULT 0,
            creditos_requeridos INTEGER DEFAULT 0,
            creditos_faltantes  INTEGER DEFAULT 0,
            promedio            REAL    DEFAULT 0.0,
            pdf_hash            TEXT,
            fecha_kardex        TEXT,
            fecha_carga         TEXT,
            fecha_actualizacion TEXT
        );

        CREATE TABLE IF NOT EXISTS materias (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            alumno_id       INTEGER NOT NULL REFERENCES alumnos(id) ON DELETE CASCADE,
            nrc             TEXT,
            clave           TEXT NOT NULL,
            nombre          TEXT NOT NULL,
            calificacion    INTEGER,
            tipo            TEXT,
            creditos        INTEGER DEFAULT 0,
            horas           INTEGER DEFAULT 0,
            calendario      TEXT,
            fecha_eval      TEXT,
            estatus         TEXT,
            UNIQUE(alumno_id, clave, calendario, tipo)
        );

        CREATE TABLE IF NOT EXISTS creditos_por_area (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            alumno_id   INTEGER NOT NULL REFERENCES alumnos(id) ON DELETE CASCADE,
            area        TEXT,
            requeridos  INTEGER DEFAULT 0,
            adquiridos  INTEGER DEFAULT 0,
            faltantes   INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS alertas (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            alumno_id   INTEGER NOT NULL REFERENCES alumnos(id) ON DELETE CASCADE,
            tipo        TEXT,
            descripcion TEXT,
            activa      INTEGER DEFAULT 1,
            fecha       TEXT
        );

        CREATE TABLE IF NOT EXISTS plan_estudios (
            clave           TEXT PRIMARY KEY,
            materia         TEXT NOT NULL,
            area_cod        TEXT,
            area            TEXT,
            orientacion_cod TEXT,
            orientacion     TEXT,
            tipo            TEXT,
            teoria          INTEGER DEFAULT 0,
            practica        INTEGER DEFAULT 0,
            totales         INTEGER DEFAULT 0,
            creditos        INTEGER DEFAULT 0,
            prerrequisito   TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS log_cargas (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            alumno_id INTEGER REFERENCES alumnos(id),
            archivo   TEXT,
            accion    TEXT,
            fecha     TEXT
        );
    """)
    conn.commit()
    log.info("BD lista: %s", db_path)
    return conn


# ─────────────────────────────────────────────────────────────────
# Utilidades compartidas
# ─────────────────────────────────────────────────────────────────

def normalizar(texto: str) -> str:
    """
    Normaliza texto para comparación: mayúsculas, sin tildes, sin espacios dobles.
    Evita falsos negativos entre el PDF del kárdex y el CSV del plan de estudios.
    """
    import re
    if not texto:
        return ""
    t = str(texto).upper().strip()
    for c, s in {"Á":"A","É":"E","Í":"I","Ó":"O","Ú":"U",
                  "À":"A","È":"E","Ì":"I","Ò":"O","Ù":"U",
                  "Ü":"U","Ñ":"N"}.items():
        t = t.replace(c, s)
    return re.sub(r"\s+", " ", t)


def creditos_requeridos_carrera(codigo: str, nombre: str) -> int | None:
    """Retorna créditos totales según carrera; None si no se reconoce."""
    if codigo and codigo.upper() in CREDITOS_POR_CARRERA:
        return CREDITOS_POR_CARRERA[codigo.upper()]
    nombre_upper = (nombre or "").upper()
    for kw, cred in CARRERA_KEYWORDS.items():
        if kw.upper() in nombre_upper:
            return cred
    return None

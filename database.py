"""
database.py — Inicialización de BD y constantes compartidas
"""
import sqlite3
import logging
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("kardex_system.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

DB_PATH = "kardex_udg.db"
CALIFICACION_MINIMA = 60
MAX_REPROBACIONES   = 2
PCT_SERVICIO_SOCIAL = 0.70
PCT_PRACTICAS_PROF  = 0.80
TOP_SUGERENCIAS     = 10
CREDITOS_TOTALES_IELC = 423

# Créditos requeridos por área (según imagen real de la app)
CREDITOS_REQUERIDOS_AREA = {
    "BASICO COMUN":                   187,
    "BASICA COMUN":                   187,
    "BASICO COMUN OBLIGATORIA":       187,
    "BASICA COMUN OBLIGATORIA":       187,
    "BASICO PARTICULAR OBLIGATORIA":  123,
    "BASICA PARTICULAR OBLIGATORIA":  123,
    "ESPECIALIZANTE OBLIGATORIA":      31,
    "ESPECIALIZANTE SELECTIVA":        40,
    "OPTATIVA ABIERTA":                42,
}

CREDITOS_POR_CARRERA = {
    "IELC": 423,
    "ICOM": 423,
}

CARRERA_KEYWORDS = {
    "ELECTRONICA Y COMPUTACION": 423,
    "ELECTRONICA Y COMPUTACION": 423,
}


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = get_connection(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS alumnos (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo                  TEXT UNIQUE NOT NULL,
            nombre                  TEXT,
            carrera                 TEXT,
            codigo_carrera          TEXT,
            nivel                   TEXT,
            centro                  TEXT,
            sede                    TEXT,
            ciclo_admision          TEXT,
            ultimo_ciclo            TEXT,
            situacion               TEXT,
            creditos_adquiridos     INTEGER DEFAULT 0,
            creditos_requeridos     INTEGER DEFAULT 0,
            creditos_faltantes      INTEGER DEFAULT 0,
            promedio                REAL DEFAULT 0.0,
            pdf_hash                TEXT,
            fecha_kardex            TEXT,
            fecha_carga             TEXT,
            fecha_actualizacion     TEXT,
            orientacion_elegida     TEXT DEFAULT '',
            servicio_social         INTEGER DEFAULT 0,
            practicas_profesionales INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS materias (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            alumno_id     INTEGER NOT NULL REFERENCES alumnos(id) ON DELETE CASCADE,
            nrc           TEXT,
            clave         TEXT NOT NULL,
            nombre        TEXT NOT NULL,
            calificacion  INTEGER,
            tipo          TEXT,
            creditos      INTEGER DEFAULT 0,
            horas         INTEGER DEFAULT 0,
            calendario    TEXT,
            fecha_eval    TEXT,
            estatus       TEXT,
            UNIQUE(alumno_id, clave, calendario, tipo)
        );

        CREATE TABLE IF NOT EXISTS horario (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            alumno_id   INTEGER NOT NULL REFERENCES alumnos(id) ON DELETE CASCADE,
            clave       TEXT NOT NULL,
            nombre      TEXT NOT NULL,
            creditos    INTEGER DEFAULT 0,
            calendario  TEXT,
            UNIQUE(alumno_id, clave, calendario)
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
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            alumno_id   INTEGER REFERENCES alumnos(id),
            archivo     TEXT,
            accion      TEXT,
            fecha       TEXT
        );
    """)
    _migrate(conn)
    conn.commit()
    log.info("BD lista: %s", db_path)
    return conn


def _migrate(conn: sqlite3.Connection):
    cur = conn.cursor()
    for table, col, col_def in [
        ("alumnos", "orientacion_elegida",       "TEXT DEFAULT ''"),
        ("alumnos", "servicio_social",            "INTEGER DEFAULT 0"),
        ("alumnos", "practicas_profesionales",    "INTEGER DEFAULT 0"),
    ]:
        try:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
        except Exception:
            pass
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS horario (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alumno_id INTEGER NOT NULL REFERENCES alumnos(id) ON DELETE CASCADE,
                clave TEXT NOT NULL,
                nombre TEXT NOT NULL,
                creditos INTEGER DEFAULT 0,
                calendario TEXT,
                UNIQUE(alumno_id, clave, calendario)
            )
        """)
    except Exception:
        pass
    conn.commit()


def normalizar(texto: str) -> str:
    import re
    if not texto:
        return ""
    t = str(texto).upper().strip()
    for c, s in {"A":"A","E":"E","I":"I","O":"O","U":"U",
                 "A":"A","E":"E","I":"I","O":"O","U":"U",
                 "U":"U","N":"N",
                 "\u00c1":"A","\u00c9":"E","\u00cd":"I","\u00d3":"O","\u00da":"U",
                 "\u00e1":"A","\u00e9":"E","\u00ed":"I","\u00f3":"O","\u00fa":"U",
                 "\u00dc":"U","\u00fc":"U","\u00d1":"N","\u00f1":"N"}.items():
        t = t.replace(c, s)
    return re.sub(r"\s+", " ", t)


def creditos_requeridos_carrera(codigo: str, nombre: str):
    if codigo and codigo.upper() in CREDITOS_POR_CARRERA:
        return CREDITOS_POR_CARRERA[codigo.upper()]
    nombre_upper = (nombre or "").upper()
    for kw, cred in CARRERA_KEYWORDS.items():
        if kw.upper() in nombre_upper:
            return cred
    return None


def creditos_requeridos_area(area: str) -> int:
    area_norm = normalizar(area)
    for key, val in CREDITOS_REQUERIDOS_AREA.items():
        if normalizar(key) == area_norm:
            return val
    return 0

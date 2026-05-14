"""
plan.py — Importador del Plan de Estudios IELC
===============================================
Lee el CSV real de la malla curricular y lo carga en la tabla
plan_estudios de kardex_udg.db.

Estructura del CSV (columnas reales):
  col0: vacía
  col1: Area    (BC, BPO, EO, ES, OA)
  col2: Orientación (AG, OH, OT, OO, OSE, OV...)
  col3: Materia
  col4: Clave
  col5: Tipo
  col6: Teoria
  col7: Práctica
  col8: Totales
  col9: Creditos
  col10: Prerrequisito
"""

import os
import sqlite3

from database import init_db, log

# ─────────────────────────────────────────────────────────────────
# Catálogos de nombres
# ─────────────────────────────────────────────────────────────────
NOMBRE_AREA = {
    "BC" : "Básica Común",
    "BPO": "Básica Particular Obligatoria",
    "EO" : "Especializante Obligatoria",
    "ES" : "Especializante Selectiva",
    "OA" : "Optativa Abierta",
}

NOMBRE_ORIENTACION = {
    "OT" : "Telecomunicaciones",
    "OO" : "Optoelectrónica",
    "OSE": "Sistemas Embebidos",
    "OV" : "Visualización",
    "AG" : "General",
    "OH" : "Sociales y Humanidades",
    "OEA": "Económico Administrativa",
}


def importar_plan_estudios(
    db_path: str = "kardex_udg.db",
    csv_path: str = "Plan de Estudios IELC - Hoja 6.csv",
):
    """
    Carga el CSV del plan de estudios en la BD.
    Limpia la tabla antes de importar para evitar duplicados sucios.
    """
    try:
        import pandas as pd
    except ImportError:
        print("❌ Falta la dependencia 'pandas'. Instálala con:")
        print("   pip install pandas")
        return

    if not os.path.exists(csv_path):
        print(f"❌ No se encontró el archivo: '{csv_path}'")
        print("Archivos en esta carpeta:", os.listdir("."))
        return

    # Leer CSV (las primeras 2 filas son encabezados del documento)
    try:
        df = pd.read_csv(csv_path, skiprows=2, encoding="utf-8",  header=0)
    except UnicodeDecodeError:
        df = pd.read_csv(csv_path, skiprows=2, encoding="latin-1", header=0)

    df.columns = [
        "_vacio", "area_cod", "orientacion_cod", "materia",
        "clave", "tipo", "teoria", "practica",
        "totales", "creditos", "prerrequisito",
    ]

    conn   = init_db(db_path)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM plan_estudios")

    contador, omitidas           = 0, 0
    area_actual, orientacion_actual = "", ""

    for _, row in df.iterrows():
        clave   = str(row["clave"]).strip()
        materia = str(row["materia"]).strip()

        if not clave   or clave.lower()   in ("nan", "clave", ""):
            omitidas += 1
            continue
        if not materia or materia.lower() in ("nan", ""):
            omitidas += 1
            continue

        # Propagar área y orientación (el CSV las omite cuando no cambian)
        area_raw = str(row["area_cod"]).strip()
        ori_raw  = str(row["orientacion_cod"]).strip()
        if area_raw and area_raw.lower() != "nan":
            area_actual = area_raw.upper()
        if ori_raw  and ori_raw.lower()  != "nan":
            orientacion_actual = ori_raw.upper()

        area_nombre        = NOMBRE_AREA.get(area_actual, area_actual)
        orientacion_nombre = NOMBRE_ORIENTACION.get(orientacion_actual, orientacion_actual)

        def safe_int(val, default=0):
            try:
                return int(float(str(val).strip()))
            except (ValueError, TypeError):
                return default

        creditos = safe_int(row["creditos"])
        teoria   = safe_int(row["teoria"])
        practica = safe_int(row["practica"])
        totales  = safe_int(row["totales"]) or teoria + practica

        tipo = str(row["tipo"]).strip()
        if tipo.lower() == "nan":
            tipo = ""

        pre = str(row["prerrequisito"]).strip()
        if pre.lower() in ("nan", "none", ""):
            pre = ""
        # "Simultaneo o posterior a X" → solo conservar "X"
        if pre.lower().startswith("simultaneo o posterior a "):
            pre = pre[len("simultaneo o posterior a "):].strip()

        cursor.execute("""
            INSERT OR REPLACE INTO plan_estudios
              (clave, materia, area_cod, area,
               orientacion_cod, orientacion,
               tipo, teoria, practica, totales, creditos, prerrequisito)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            clave, materia, area_actual, area_nombre,
            orientacion_actual, orientacion_nombre,
            tipo, teoria, practica, totales, creditos, pre,
        ))
        contador += 1

    conn.commit()

    total_bd = cursor.execute("SELECT COUNT(*) FROM plan_estudios").fetchone()[0]
    conn.close()

    print(f"\n✅ Plan de estudios importado:")
    print(f"   Materias cargadas : {contador}")
    print(f"   Filas omitidas    : {omitidas}")
    print(f"   Total en BD       : {total_bd}")
    _mostrar_distribucion(db_path)


def _mostrar_distribucion(db_path: str):
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT area, COUNT(*) AS total, SUM(creditos) AS creditos
        FROM plan_estudios
        GROUP BY area
        ORDER BY area
    """).fetchall()
    conn.close()

    print("\n  Distribución por área:")
    print(f"  {'Área':<40} {'Materias':>8} {'Créditos':>9}")
    print("  " + "─" * 60)
    for r in rows:
        print(f"  {r[0]:<40} {r[1]:>8} {r[2]:>9}")
    print()

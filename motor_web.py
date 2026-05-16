"""
motor_web.py — Motor de Inferencia IA adaptado para Web (Flask / Render)
=========================================================================
Diferencias respecto a motor.py (CMD):
  - SIN llamadas a input() / preguntas interactivas al usuario.
  - Las equivalencias se resuelven AUTOMÁTICAMENTE (fuzzy match ≥ 0.75).
  - Servicio Social y Prácticas Profesionales se leen de `perfil_usuario`
    que llega como parámetro desde el front-end.
  - Retorna dicts Python limpios (JSON-serializable); NO imprime nada.
"""

from collections import defaultdict
from difflib import SequenceMatcher
from typing import Optional

from database import (
    init_db, normalizar,
    PCT_SERVICIO_SOCIAL, PCT_PRACTICAS_PROF,
    MAX_REPROBACIONES, TOP_SUGERENCIAS, CREDITOS_TOTALES_IELC,
    log,
)

UMBRAL_SIMILITUD = 0.75   # similitud mínima para auto-equivalencia
UMBRAL_HORARIO   = 0.80   # similitud mínima para excluir por horario


def _similitud(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _estatus_final(intentos: list) -> dict:
    aprobados  = [i for i in intentos if i.get("estatus") == "APROBADA"]
    reprobados = [i for i in intentos if i.get("estatus") == "REPROBADA"]
    if aprobados:
        mejor = max(aprobados, key=lambda x: (x.get("creditos", 0), x.get("fecha_eval") or ""))
        return {
            "final": "APROBADA",
            "ref":   mejor,
            "calendarios_rep": {r.get("calendario") for r in reprobados} - {None},
        }
    ultimo = max(intentos, key=lambda x: x.get("fecha_eval") or "") if intentos else {}
    return {
        "final": "REPROBADA",
        "ref":   ultimo,
        "calendarios_rep": {r.get("calendario") for r in reprobados} - {None},
    }


def _resolver_equivalencia_auto(nombre_norm: str, clave_kardex: str,
                                 plan_por_nombre: dict, ya_equiv: set) -> Optional[str]:
    """
    Resolución automática de equivalencias sin preguntar al usuario.
    Devuelve la clave del plan con mayor similitud si supera UMBRAL_SIMILITUD,
    o None si no hay candidatas suficientemente similares.
    """
    mejor_cp    = None
    mejor_score = 0.0
    for nom_plan, clave_plan in plan_por_nombre.items():
        if clave_plan in ya_equiv:
            continue
        score = _similitud(nombre_norm, nom_plan)
        if score >= UMBRAL_SIMILITUD and score > mejor_score:
            mejor_score = score
            mejor_cp    = clave_plan
    if mejor_cp:
        log.info("Auto-equiv: '%s' (%s) → plan=%s (%.0f%%)",
                 nombre_norm, clave_kardex, mejor_cp, mejor_score * 100)
    return mejor_cp


class MotorInferencia:
    """Motor de análisis académico adaptado para uso web (sin input)."""

    def __init__(self, db_path: str = "kardex_udg.db"):
        self.conn = init_db(db_path)

    def analizar(
        self,
        codigo_alumno: str,
        horario_claves: set = None,       # set de claves CVE del horario actual
        horario_nombres: set = None,      # set de nombres normalizados del horario
        servicio_social: bool = False,    # ¿el alumno ya completó SS?
        practicas_prof: bool = False,     # ¿el alumno ya completó PP?
        orientacion: str = "",            # orientación elegida (opcional, para filtrar)
    ) -> Optional[dict]:
        """
        Analiza el avance académico del alumno y retorna un dict JSON-serializable.

        Parámetros recibidos desde el front-end:
          - codigo_alumno   : código del alumno en la BD
          - horario_claves  : set de claves CVE que ya tiene inscritas este semestre
          - horario_nombres : set de nombres normalizados del horario actual
          - servicio_social : True si el alumno indica que ya lo acreditó
          - practicas_prof  : True si el alumno indica que ya las acreditó
          - orientacion     : código de orientación elegida (OT, OO, OSE, OV…)
        """
        cursor = self.conn.cursor()

        horario_claves  = horario_claves  or set()
        horario_nombres = horario_nombres or set()

        # ── 1. Alumno ──────────────────────────────────────────────
        cursor.execute("""
            SELECT id, codigo, nombre, carrera, codigo_carrera,
                   creditos_adquiridos, creditos_requeridos,
                   creditos_faltantes, promedio,
                   ciclo_admision, ultimo_ciclo, situacion
            FROM alumnos WHERE codigo = ?
        """, (codigo_alumno,))
        row = cursor.fetchone()
        if not row:
            return {"error": f"El código {codigo_alumno} no está en la base de datos."}

        alumno    = dict(row)
        alumno_id = alumno["id"]

        creditos_actuales   = alumno["creditos_adquiridos"] or 0
        creditos_requeridos = alumno["creditos_requeridos"] or CREDITOS_TOTALES_IELC
        promedio_actual     = alumno["promedio"] or 0.0
        pct_avance          = (creditos_actuales / creditos_requeridos * 100) \
                              if creditos_requeridos > 0 else 0.0

        umbral_servicio  = int(creditos_requeridos * PCT_SERVICIO_SOCIAL)
        umbral_practicas = int(creditos_requeridos * PCT_PRACTICAS_PROF)

        # ── 2. Intentos del alumno ─────────────────────────────────
        cursor.execute("""
            SELECT clave, nombre, estatus, calificacion,
                   creditos, calendario, fecha_eval, tipo
            FROM materias WHERE alumno_id = ?
            ORDER BY clave, fecha_eval
        """, (alumno_id,))
        todos_intentos = [dict(r) for r in cursor.fetchall()]

        # ── 3. Plan de estudios ────────────────────────────────────
        cursor.execute("""
            SELECT clave, materia, area_cod, area,
                   orientacion_cod, orientacion,
                   creditos, prerrequisito
            FROM plan_estudios ORDER BY area_cod, materia
        """)
        plan_rows = cursor.fetchall()

        if not plan_rows:
            return {"error": "El plan de estudios no está cargado. Ejecuta importar-plan primero."}

        plan_por_nombre: dict = {}
        plan_por_clave:  dict = {}
        for pr in plan_rows:
            cp = str(pr["clave"]).strip().upper()
            np = normalizar(str(pr["materia"]))
            plan_por_nombre[np] = cp
            plan_por_clave[cp]  = dict(pr)

        # ── 4. Clasificar aprobadas y reprobadas ───────────────────
        por_clave: dict = defaultdict(list)
        for intento in todos_intentos:
            por_clave[str(intento["clave"]).strip().upper()].append(intento)

        claves_aprobadas  = set()
        nombres_aprobados = set()
        rep_activas: dict = {}
        equiv_resueltas: dict = {}
        ya_equiv: set = set()

        # Pasada A: APROBADAS
        for clave_k, intentos in por_clave.items():
            res = _estatus_final(intentos)
            if res["final"] != "APROBADA":
                continue
            nombre_k    = (res["ref"] or {}).get("nombre", "")
            nombre_norm = normalizar(nombre_k)

            if clave_k in plan_por_clave:
                claves_aprobadas.add(clave_k)
                if nombre_norm:
                    nombres_aprobados.add(nombre_norm)
            elif nombre_norm and nombre_norm in plan_por_nombre:
                cp = plan_por_nombre[nombre_norm]
                claves_aprobadas.add(cp)
                nombres_aprobados.add(nombre_norm)
            else:
                key_eq = f"{clave_k}|{nombre_norm}"
                if key_eq not in equiv_resueltas:
                    cp = _resolver_equivalencia_auto(nombre_norm, clave_k, plan_por_nombre, ya_equiv)
                    equiv_resueltas[key_eq] = cp
                    if cp:
                        ya_equiv.add(cp)
                cp = equiv_resueltas.get(key_eq)
                if cp:
                    claves_aprobadas.add(cp)
                    if nombre_norm:
                        nombres_aprobados.add(nombre_norm)
                elif nombre_norm:
                    nombres_aprobados.add(nombre_norm)

        # Pasada B: REPROBADAS activas
        for clave_k, intentos in por_clave.items():
            res = _estatus_final(intentos)
            if res["final"] != "REPROBADA" or not res["calendarios_rep"]:
                continue
            nombre_k    = (res["ref"] or {}).get("nombre", "")
            nombre_norm = normalizar(nombre_k)

            if clave_k in claves_aprobadas:
                continue
            if nombre_norm and nombre_norm in nombres_aprobados:
                continue
            nombre_cubierto = any(
                nombre_norm and _similitud(nombre_norm, n) >= UMBRAL_SIMILITUD
                for n in nombres_aprobados
            )
            if not nombre_cubierto:
                rep_activas[clave_k] = {
                    "nombre":      nombre_k or clave_k,
                    "calendarios": sorted(res["calendarios_rep"]),
                    "num_intentos": len(intentos),
                }

        # ── 5. Servicio Social y Prácticas (recibidos del front) ───
        puede_ss        = creditos_actuales >= umbral_servicio
        puede_practicas = creditos_actuales >= umbral_practicas

        # ── 6. Helper para filtrar horario actual ──────────────────
        def _en_horario(clave_plan: str, nombre_plan_norm: str) -> bool:
            if clave_plan in horario_claves:
                return True
            if nombre_plan_norm and nombre_plan_norm in horario_nombres:
                return True
            for nh in horario_nombres:
                if not nh or not nombre_plan_norm:
                    continue
                if nombre_plan_norm.startswith(nh) or nh.startswith(nombre_plan_norm):
                    return True
                if _similitud(nombre_plan_norm, nh) >= UMBRAL_HORARIO:
                    return True
            return False

        # ── 7. Clasificar materias del plan ────────────────────────
        disponibles = []
        bloqueadas  = []

        for pr in plan_rows:
            clave_p      = str(pr["clave"]).strip().upper()
            materia      = pr["materia"]
            area_cod     = pr["area_cod"]
            area         = pr["area"]
            ori_cod      = pr["orientacion_cod"] or ""
            ori          = pr["orientacion"] or ""
            creditos_m   = pr["creditos"]
            pre          = (pr["prerrequisito"] or "").strip()
            materia_norm = normalizar(materia)

            if clave_p in claves_aprobadas or materia_norm in nombres_aprobados:
                continue
            if _en_horario(clave_p, materia_norm):
                continue

            if not pre:
                disponibles.append({
                    "clave": clave_p, "nombre": materia,
                    "area_cod": area_cod, "area": area,
                    "orientacion_cod": ori_cod, "orientacion": ori,
                    "creditos": creditos_m, "estado": "Sin prerrequisito",
                })
            else:
                pre_norm  = normalizar(pre)
                pre_clave = pre.upper().strip()
                if pre_norm in nombres_aprobados or pre_clave in claves_aprobadas:
                    disponibles.append({
                        "clave": clave_p, "nombre": materia,
                        "area_cod": area_cod, "area": area,
                        "orientacion_cod": ori_cod, "orientacion": ori,
                        "creditos": creditos_m,
                        "estado": f"Prerrequisito OK: {pre}",
                    })
                else:
                    bloqueadas.append({
                        "clave": clave_p, "nombre": materia,
                        "area_cod": area_cod, "area": area,
                        "orientacion_cod": ori_cod, "orientacion": ori,
                        "creditos": creditos_m, "prerrequisito": pre,
                    })

        # ── 8. Progreso por área ───────────────────────────────────
        if claves_aprobadas:
            ph = ",".join("?" * len(claves_aprobadas))
            cursor.execute(f"""
                SELECT p.area_cod, p.area,
                       COUNT(*) AS total_plan,
                       SUM(p.creditos) AS creditos_plan,
                       COUNT(CASE WHEN UPPER(TRIM(p.clave)) IN ({ph}) THEN 1 END) AS aprobadas,
                       COALESCE(SUM(CASE WHEN UPPER(TRIM(p.clave)) IN ({ph})
                                        THEN p.creditos ELSE 0 END), 0) AS creditos_aprobados
                FROM plan_estudios p
                GROUP BY p.area_cod, p.area ORDER BY p.area_cod
            """, list(claves_aprobadas) * 2)
        else:
            cursor.execute("""
                SELECT area_cod, area, COUNT(*) AS total_plan, SUM(creditos) AS creditos_plan,
                       0 AS aprobadas, 0 AS creditos_aprobados
                FROM plan_estudios GROUP BY area_cod, area ORDER BY area_cod
            """)
        por_area = [dict(r) for r in cursor.fetchall()]

        # ── 9. Alertas ─────────────────────────────────────────────
        alertas = []

        for clave, info in rep_activas.items():
            if len(info["calendarios"]) >= MAX_REPROBACIONES:
                periodos = ", ".join(sorted(info["calendarios"]))
                alertas.append({
                    "tipo": "ARTICULO_33", "icono": "🔥",
                    "descripcion": (
                        f"'{info['nombre']}' ({clave}) reprobada en "
                        f"{len(info['calendarios'])} periodos: {periodos}. "
                        "Revisar aplicación del Artículo 33."
                    ),
                })

        if puede_ss:
            if servicio_social:
                alertas.append({"tipo": "SERVICIO_SOCIAL", "icono": "✅",
                    "descripcion": "Servicio Social: acreditado."})
            else:
                alertas.append({"tipo": "SERVICIO_SOCIAL", "icono": "🟢",
                    "descripcion": (
                        f"Ya puedes realizar Servicio Social "
                        f"({creditos_actuales}/{umbral_servicio} cred.). ¡Pendiente!")})
        else:
            alertas.append({"tipo": "SERVICIO_PENDIENTE", "icono": "⏳",
                "descripcion": (
                    f"Servicio Social: faltan {umbral_servicio - creditos_actuales} créditos "
                    f"({creditos_actuales}/{umbral_servicio}).")})

        if puede_practicas:
            if not servicio_social:
                alertas.append({"tipo": "PRACTICAS_BLOQUEADAS", "icono": "🔒",
                    "descripcion": (
                        f"Prácticas Profesionales: tienes los créditos necesarios "
                        f"({creditos_actuales}/{umbral_practicas}) pero primero debes "
                        "acreditar el Servicio Social.")})
            elif practicas_prof:
                alertas.append({"tipo": "PRACTICAS", "icono": "✅",
                    "descripcion": "Prácticas Profesionales: acreditadas."})
            else:
                alertas.append({"tipo": "PRACTICAS", "icono": "🔵",
                    "descripcion": (
                        f"Ya puedes tramitar Prácticas Profesionales "
                        f"({creditos_actuales}/{umbral_practicas} cred., SS acreditado). ¡Pendiente!")})
        else:
            razon = ""
            if not puede_ss or not servicio_social:
                razon = " (también falta acreditar el Servicio Social)"
            alertas.append({"tipo": "PRACTICAS_PENDIENTE", "icono": "⏳",
                "descripcion": (
                    f"Prácticas Profesionales: faltan {umbral_practicas - creditos_actuales} créditos "
                    f"({creditos_actuales}/{umbral_practicas}){razon}.")})

        if 0 < promedio_actual < 70:
            alertas.append({"tipo": "PROMEDIO_BAJO", "icono": "📉",
                "descripcion": f"Promedio {promedio_actual:.2f} < 70. Considera asesorías."})

        cursor.execute("""
            SELECT clave FROM materias
            WHERE alumno_id=? AND estatus='APROBADA' AND creditos=0 GROUP BY clave
        """, (alumno_id,))
        sin_cred = cursor.fetchall()
        if sin_cred:
            claves_sc = ", ".join(r["clave"] for r in sin_cred)
            alertas.append({"tipo": "CREDITO_ERROR", "icono": "🔴",
                "descripcion": f"Materias aprobadas con 0 créditos: {claves_sc}. "
                               "Verificar con Servicios Escolares."})

        return {
            "alumno":      alumno,
            "pct_avance":  round(pct_avance, 1),
            "umbral_servicio":  umbral_servicio,
            "umbral_practicas": umbral_practicas,
            "puede_ss":         puede_ss,
            "puede_practicas":  puede_practicas,
            "disponibles":      disponibles,
            "bloqueadas":       bloqueadas,
            "alertas":          alertas,
            "por_area":         por_area,
            "rep_activas":      [
                {"clave": k, **v} for k, v in rep_activas.items()
            ],
            "n_aprobadas":      len(claves_aprobadas),
            "equiv_resueltas":  {
                k: v for k, v in equiv_resueltas.items() if v
            },
        }

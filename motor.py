"""
motor.py — Motor de Inferencia IA para sugerencias académicas
=============================================================
Compara el historial del alumno contra el Plan de Estudios IELC y genera:
  - Materias disponibles para el próximo ciclo (prerrequisitos cumplidos)
  - Materias bloqueadas y por qué prerrequisito les falta
  - Alertas institucionales (Art. 33, Servicio Social, Prácticas)
  - Progreso por área de formación

Correcciones respecto a versiones anteriores:
  - JOIN correcto por alumno_id (schema real de extractor.py)
  - Estatus FINAL por materia: si tiene al menos un APROBADA, cuenta como aprobada
    aunque tenga reprobaciones previas → desbloquea prerrequisitos correctamente
  - Art. 33 cuenta periodos DISTINTOS de reprobación, no filas individuales
"""

"""
motor.py — Motor de Inferencia IA para sugerencias académicas
=============================================================
Compara el historial del alumno contra el Plan de Estudios IELC y genera:
  - Materias disponibles para el próximo ciclo (prerrequisitos cumplidos)
  - Materias bloqueadas y por qué prerrequisito les falta
  - Alertas institucionales (Art. 33, Servicio Social, Prácticas)
  - Progreso por área de formación

Correcciones respecto a versiones anteriores:
  - JOIN correcto por alumno_id (schema real de extractor.py)
  - Estatus FINAL por materia: si tiene al menos un APROBADA, cuenta como aprobada
    aunque tenga reprobaciones previas → desbloquea prerrequisitos correctamente
  - Art. 33 cuenta periodos DISTINTOS de reprobación, no filas individuales
"""
"""
motor.py — Motor de Inferencia IA para sugerencias académicas
=============================================================
Compara el historial del alumno contra el Plan de Estudios IELC y genera:
  - Materias disponibles para el próximo ciclo (prerrequisitos cumplidos)
  - Materias bloqueadas y por qué prerrequisito les falta
  - Alertas institucionales (Art. 33, Servicio Social, Prácticas)
  - Progreso por área de formación

Correcciones respecto a versiones anteriores:
  - JOIN correcto por alumno_id (schema real de extractor.py)
  - Estatus FINAL por materia: si tiene al menos un APROBADA, cuenta como aprobada
    aunque tenga reprobaciones previas → desbloquea prerrequisitos correctamente
  - Art. 33 cuenta periodos DISTINTOS de reprobación, no filas individuales
"""

import re
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Optional
 
from database import (
    init_db, normalizar,
    PCT_SERVICIO_SOCIAL, PCT_PRACTICAS_PROF,
    MAX_REPROBACIONES, TOP_SUGERENCIAS, CREDITOS_TOTALES_IELC,
    CREDITOS_REQUERIDOS_AREA,
    log,
)
 
 
def _barra(pct: float, ancho: int = 8) -> str:
    llenas = int(pct / 100 * ancho)
    return "█" * llenas + "░" * (ancho - llenas)
 
 
def _estatus_final(intentos: list) -> dict:
    aprobados = [i for i in intentos if i.get("estatus") == "APROBADA"]
    reprobados = [i for i in intentos if i.get("estatus") == "REPROBADA"]
    if aprobados:
        mejor = max(aprobados, key=lambda x: (x.get("creditos", 0), x.get("fecha_eval") or ""))
        return {
            "final": "APROBADA",
            "ref": mejor,
            "calendarios_rep": {r.get("calendario") for r in reprobados} - {None},
        }
    else:
        ultimo = max(intentos, key=lambda x: x.get("fecha_eval") or "") if intentos else {}
        return {
            "final": "REPROBADA",
            "ref": ultimo,
            "calendarios_rep": {r.get("calendario") for r in reprobados} - {None},
        }
 
 
def _similitud(a: str, b: str) -> float:
    return SequenceMatcher(None, normalizar(a), normalizar(b)).ratio()
 
 
def _calendarios_consecutivos(calendarios: set) -> bool:
    """Detecta si hay 2 calendarios consecutivos en un set de tipo '2023-A','2023-B'."""
    orden = sorted(calendarios)
    for i in range(len(orden) - 1):
        c1, c2 = orden[i], orden[i + 1]
        m1 = re.match(r"(\d{4})-([AB])", c1)
        m2 = re.match(r"(\d{4})-([AB])", c2)
        if not m1 or not m2:
            continue
        y1, s1 = int(m1.group(1)), m1.group(2)
        y2, s2 = int(m2.group(1)), m2.group(2)
        # Consecutivo: A→B mismo año, o B→A año siguiente
        if (y1 == y2 and s1 == "A" and s2 == "B") or (y2 == y1 + 1 and s1 == "B" and s2 == "A"):
            return True
    return False
 
 
class MotorInferencia:
    def __init__(self, db_path: str = "kardex_udg.db"):
        self.conn = init_db(db_path)
 
    def analizar(self, codigo_alumno: str, orientacion: str = None,
                 servicio_social: bool = False, practicas: bool = False,
                 horario_claves: list = None) -> Optional[dict]:
        cursor = self.conn.cursor()
 
        # ── 1. Alumno ────────────────────────────────────────────
        cursor.execute("""
            SELECT id, codigo, nombre, carrera, codigo_carrera,
                   creditos_adquiridos, creditos_requeridos,
                   creditos_faltantes, promedio,
                   ciclo_admision, ultimo_ciclo, situacion,
                   orientacion_elegida, servicio_social, practicas_profesionales
            FROM alumnos WHERE codigo = ?
        """, (codigo_alumno,))
        row = cursor.fetchone()
        if not row:
            return None
        alumno = dict(row)
        alumno_id = alumno["id"]
 
        # Persistir datos de orientación/servicio si se pasan
        if orientacion is not None:
            cursor.execute("UPDATE alumnos SET orientacion_elegida=? WHERE id=?",
                           (orientacion, alumno_id))
            alumno["orientacion_elegida"] = orientacion
        if servicio_social:
            cursor.execute("UPDATE alumnos SET servicio_social=1 WHERE id=?", (alumno_id,))
            alumno["servicio_social"] = 1
        if practicas:
            cursor.execute("UPDATE alumnos SET practicas_profesionales=1 WHERE id=?", (alumno_id,))
            alumno["practicas_profesionales"] = 1
        self.conn.commit()
 
        creditos_kardex = alumno["creditos_adquiridos"] or 0
        creditos_requeridos = alumno["creditos_requeridos"] or CREDITOS_TOTALES_IELC
        orientacion_alumno = alumno.get("orientacion_elegida") or ""
 
        # ── 2. Todos los intentos ─────────────────────────────────
        cursor.execute("""
            SELECT clave, nombre, estatus, calificacion,
                   creditos, calendario, fecha_eval, tipo
            FROM materias WHERE alumno_id = ?
            ORDER BY clave, fecha_eval
        """, (alumno_id,))
        todos_intentos = [dict(r) for r in cursor.fetchall()]
 
        # ── 3. Estatus final + conteo propio de créditos ──────────
        por_clave = defaultdict(list)
        for intento in todos_intentos:
            clave_norm = str(intento["clave"]).strip().upper()
            por_clave[clave_norm].append(intento)
 
        claves_aprobadas = set()
        nombres_aprobados = set()
        rep_activas = {}
        creditos_propios_total = 0
        creditos_propios_por_area = defaultdict(int)
 
        for clave, intentos in por_clave.items():
            resultado = _estatus_final(intentos)
            if resultado["final"] == "APROBADA":
                claves_aprobadas.add(clave)
                nombre_norm = normalizar(resultado["ref"].get("nombre", ""))
                if nombre_norm:
                    nombres_aprobados.add(nombre_norm)
                creditos_mat = resultado["ref"].get("creditos", 0) or 0
                creditos_propios_total += creditos_mat
            else:
                if resultado["calendarios_rep"]:
                    rep_activas[clave] = {
                        "nombre": resultado["ref"].get("nombre", clave),
                        "calendarios": resultado["calendarios_rep"],
                        "creditos": resultado["ref"].get("creditos", 0) or 0,
                    }
 
        # ── 4. Conteo de créditos propios por área ────────────────
        cursor.execute("""
            SELECT p.area, p.creditos
            FROM plan_estudios p
            JOIN (
                SELECT UPPER(TRIM(clave)) AS clave FROM materias
                WHERE alumno_id=? AND estatus='APROBADA'
                GROUP BY UPPER(TRIM(clave))
            ) m ON UPPER(TRIM(p.clave)) = m.clave
        """, (alumno_id,))
        for r in cursor.fetchall():
            creditos_propios_por_area[r["area"]] += r["creditos"]
 
        # ── 5. Comparación con créditos del kardex ────────────────
        diferencia_creditos = creditos_kardex - creditos_propios_total
        discrepancias_area = []

        # Construir mapa normalizado de créditos propios por área
        # para comparar sin importar acentos/mayúsculas
        creditos_propios_norm = {
            normalizar(k): v for k, v in creditos_propios_por_area.items()
        }

        cursor.execute("""
            SELECT area, adquiridos FROM creditos_por_area WHERE alumno_id=?
        """, (alumno_id,))
        for r in cursor.fetchall():
            area = r["area"]
            adq_kardex = r["adquiridos"]
            # Buscar por nombre normalizado para tolerar "BASICO COMUN" vs "Básica Común"
            adq_propio = creditos_propios_norm.get(normalizar(area), 0)
            # Solo reportar discrepancia si la diferencia es significativa (>2 cr)
            # para evitar ruido por materias fuera del plan oficial
            if abs(adq_kardex - adq_propio) > 2:
                discrepancias_area.append({
                    "area": area,
                    "kardex": adq_kardex,
                    "propio": adq_propio,
                })
 
        # ── 6. Horario actual (no suma créditos, pero informa) ─────
        cursor.execute("""
            SELECT clave, nombre, creditos FROM horario WHERE alumno_id=?
        """, (alumno_id,))
        horario_rows = [dict(r) for r in cursor.fetchall()]
        claves_en_horario = {str(r["clave"]).strip().upper() for r in horario_rows}
        creditos_en_horario_por_area = defaultdict(int)
        # Si están en el plan, mapear sus créditos por área (para mostrar, no sumar)
        cursor.execute("SELECT clave, area, creditos FROM plan_estudios")
        plan_creditos_map = {str(r["clave"]).strip().upper(): (r["area"], r["creditos"]) for r in cursor.fetchall()}
        for clave_h in claves_en_horario:
            if clave_h in plan_creditos_map:
                area_h, cred_h = plan_creditos_map[clave_h]
                creditos_en_horario_por_area[area_h] += cred_h
 
        # ── 7. Plan de estudios ───────────────────────────────────
        cursor.execute("""
            SELECT clave, materia, area_cod, area,
                   orientacion_cod, orientacion,
                   creditos, prerrequisito
            FROM plan_estudios ORDER BY area_cod, materia
        """)
        plan_rows = cursor.fetchall()
 
        if not plan_rows:
            return None
 
        # ── 8. Materias reprobadas (prioritarias) ─────────────────
        # Claves reprobadas que están en el plan → deben ser prioritarias
        claves_prioritarias = set(rep_activas.keys())

        # ── 8b. Áreas proyectadas como completas con el horario actual ──
        # Si (créditos aprobados + créditos en horario) >= requeridos oficiales
        # → el área se considera "cubierta proyectivamente": no sugerir más materias
        # de esa área. Los créditos del horario NO se suman al kárdex real.
        areas_proyectadas_completas = set()
        # Construir mapa área → créditos aprobados propios
        creditos_aprobados_por_area = dict(creditos_propios_por_area)
        for area_nombre_plan, cred_hor in creditos_en_horario_por_area.items():
            cred_aprobados = creditos_aprobados_por_area.get(area_nombre_plan, 0)
            req_oficial = 0
            for key, val in CREDITOS_REQUERIDOS_AREA.items():
                if normalizar(key) == normalizar(area_nombre_plan):
                    req_oficial = val
                    break
            if req_oficial > 0 and (cred_aprobados + cred_hor) >= req_oficial:
                areas_proyectadas_completas.add(area_nombre_plan)
                log.info("Área '%s' proyectada completa: %d aprobados + %d en horario >= %d requeridos",
                         area_nombre_plan, cred_aprobados, cred_hor, req_oficial)

        # ── 9. Clasificar materias disponibles/bloqueadas ─────────
        disponibles = []
        bloqueadas = []
 
        for row in plan_rows:
            clave = str(row["clave"]).strip().upper()
            materia = row["materia"]
            area_cod = row["area_cod"]
            area = row["area"]
            ori_cod = row["orientacion_cod"] or ""
            ori = row["orientacion"] or ""
            creditos = row["creditos"]
            pre = (row["prerrequisito"] or "").strip()
            materia_norm = normalizar(materia)
 
            # Ya aprobada → saltar
            if clave in claves_aprobadas or materia_norm in nombres_aprobados:
                continue
 
            # Si está en el horario actual → ya está inscrita, no sugerir
            if clave in claves_en_horario:
                continue

            # Área proyectada completa con el horario → no sugerir más de ella
            # (excepto prioritarias: una materia reprobada siempre se recuerda)
            if area in areas_proyectadas_completas and clave not in claves_prioritarias:
                continue

            # Filtrar por orientación si el alumno ya eligió
            if orientacion_alumno and ori_cod and ori_cod not in ("AG", "OH"):
                if normalizar(orientacion_alumno) not in (normalizar(ori_cod), normalizar(ori)):
                    continue

            es_prioritaria = clave in claves_prioritarias

            if not pre:
                disponibles.append({
                    "clave": clave, "nombre": materia,
                    "area_cod": area_cod, "area": area,
                    "orientacion": ori, "creditos": creditos,
                    "estado": "Sin prerrequisito",
                    "prioritaria": es_prioritaria,
                    "en_horario": False,
                })
            else:
                pre_norm = normalizar(pre)
                pre_clave = pre.upper().strip()
                if pre_norm in nombres_aprobados or pre_clave in claves_aprobadas:
                    disponibles.append({
                        "clave": clave, "nombre": materia,
                        "area_cod": area_cod, "area": area,
                        "orientacion": ori, "creditos": creditos,
                        "estado": f"Prerrequisito OK: {pre}",
                        "prioritaria": es_prioritaria,
                        "en_horario": False,
                    })
                else:
                    bloqueadas.append({
                        "clave": clave, "nombre": materia,
                        "area_cod": area_cod, "area": area,
                        "orientacion": ori, "creditos": creditos,
                        "prerrequisito": pre,
                    })
 
        # Ordenar disponibles: prioritarias primero
        disponibles.sort(key=lambda x: (0 if x["prioritaria"] else 1, x["area_cod"], x["nombre"]))
 
        # ── 10. Progreso por área con créditos CORRECTOS ──────────
        cursor.execute("""
            SELECT p.area_cod, p.area,
                   COUNT(*) AS total_plan,
                   SUM(p.creditos) AS creditos_plan,
                   COUNT(CASE WHEN aprobada.clave IS NOT NULL THEN 1 END) AS aprobadas,
                   COALESCE(SUM(CASE WHEN aprobada.clave IS NOT NULL
                               THEN p.creditos ELSE 0 END), 0) AS creditos_aprobados
            FROM plan_estudios p
            LEFT JOIN (
                SELECT UPPER(TRIM(clave)) AS clave
                FROM materias
                WHERE alumno_id = ? AND estatus = 'APROBADA'
                GROUP BY UPPER(TRIM(clave))
            ) aprobada ON UPPER(TRIM(p.clave)) = aprobada.clave
            GROUP BY p.area_cod, p.area
            ORDER BY p.area_cod
        """, (alumno_id,))
        por_area_raw = [dict(r) for r in cursor.fetchall()]
 
        # Corregir créditos requeridos usando el catálogo oficial
        por_area = []
        for a in por_area_raw:
            area_key = normalizar(a["area"])
            req_oficial = 0
            for key, val in CREDITOS_REQUERIDOS_AREA.items():
                if normalizar(key) == area_key:
                    req_oficial = val
                    break
            cred_horario_area = creditos_en_horario_por_area.get(a["area"], 0)
            por_area.append({
                **a,
                "requeridos_oficial": req_oficial if req_oficial else a["creditos_plan"],
                "creditos_en_horario": cred_horario_area,
            })
 
        # ── 11. Alertas ───────────────────────────────────────────
        alertas = []
        pct_avance = (creditos_kardex / creditos_requeridos * 100) if creditos_requeridos > 0 else 0
 
        # Art. 33 — reprobada en 2+ periodos distintos sin aprobar
        for clave, info in rep_activas.items():
            cals = info["calendarios"]
            if len(cals) >= MAX_REPROBACIONES:
                periodos = ", ".join(sorted(cals))
                if _calendarios_consecutivos(cals):
                    # 2 semestres CONSECUTIVOS → artículo obligatorio
                    alertas.append({
                        "tipo": "ARTICULO_33_CONSECUTIVO", "icono": "🚨",
                        "descripcion": (
                            f"'{info['nombre']}' ({clave}) reprobada en 2 semestres CONSECUTIVOS "
                            f"({periodos}). Debes solicitar artículo y es NECESARIO que visites "
                            f"a tu coordinador."
                        ),
                    })
                else:
                    alertas.append({
                        "tipo": "ARTICULO_33", "icono": "🔥",
                        "descripcion": (
                            f"'{info['nombre']}' ({clave}) reprobada en "
                            f"{len(cals)} periodos: {periodos}. "
                            "Revisar aplicación del Artículo 33."
                        ),
                    })
 
        # Recordatorio de materias reprobadas → registrar el próximo semestre
        for clave, info in rep_activas.items():
            alertas.append({
                "tipo": "RECORDATORIO_REPROBADA", "icono": "📌",
                "descripcion": (
                    f"Recuerda registrar '{info['nombre']}' ({clave}) el próximo semestre. "
                    f"Es una materia PRIORITARIA en tus sugerencias."
                ),
            })
 
        # Servicio Social / Prácticas — usar créditos del kardex (fuente oficial)
        umbral_servicio  = int(creditos_requeridos * PCT_SERVICIO_SOCIAL)
        umbral_practicas = int(creditos_requeridos * PCT_PRACTICAS_PROF)

        # Servicio Social
        if not alumno.get("servicio_social"):
            if creditos_kardex >= umbral_servicio:
                alertas.append({"tipo": "SERVICIO_SOCIAL", "icono": "🟢",
                                "descripcion": (
                                    f"Con {creditos_kardex}/{creditos_requeridos} créditos "
                                    f"({pct_avance:.1f}%) ya puedes realizar Servicio Social "
                                    f"(requiere {umbral_servicio} cr)."
                                )})
            else:
                alertas.append({"tipo": "SERVICIO_PENDIENTE", "icono": "⏳",
                                "descripcion": (
                                    f"Servicio Social: te faltan "
                                    f"{umbral_servicio - creditos_kardex} créditos "
                                    f"({creditos_kardex}/{umbral_servicio} = "
                                    f"{creditos_kardex/umbral_servicio*100:.0f}%)."
                                )})

        # Prácticas Profesionales
        if not alumno.get("practicas_profesionales"):
            if creditos_kardex >= umbral_practicas:
                alertas.append({"tipo": "PRACTICAS", "icono": "🔵",
                                "descripcion": (
                                    f"Con {creditos_kardex}/{creditos_requeridos} créditos "
                                    f"({pct_avance:.1f}%) ya puedes tramitar Prácticas Profesionales "
                                    f"(requiere {umbral_practicas} cr)."
                                )})
            else:
                alertas.append({"tipo": "PRACTICAS_PENDIENTE", "icono": "⏳",
                                "descripcion": (
                                    f"Prácticas Profesionales: te faltan "
                                    f"{umbral_practicas - creditos_kardex} créditos "
                                    f"({creditos_kardex}/{umbral_practicas} = "
                                    f"{creditos_kardex/umbral_practicas*100:.0f}%)."
                                )})
 
        promedio_actual = alumno["promedio"] or 0.0
        if 0 < promedio_actual < 70:
            alertas.append({"tipo": "PROMEDIO_BAJO", "icono": "📉",
                            "descripcion": f"Promedio {promedio_actual:.2f} por debajo de 70."})
 
        return {
            "alumno": alumno,
            "disponibles": disponibles,
            "bloqueadas": bloqueadas,
            "alertas": alertas,
            "por_area": por_area,
            "horario": horario_rows,
            "creditos_propios": creditos_propios_total,
            "discrepancia_creditos": diferencia_creditos,
            "rep_activas": {k: {**v, "calendarios": list(v["calendarios"])}
                            for k, v in rep_activas.items()},
            "areas_proyectadas_completas": list(areas_proyectadas_completas),
        }
 
    def sugerir_materia(self, codigo_alumno: str, query: str, area_manual: str = None) -> dict:
        """
        Busca una materia no cursada en 3 filtros:
        1. Por clave exacta
        2. Por nombre similar (difflib)
        3. Si no encuentra → pide área y lista materias de esa área no cursadas
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT id FROM alumnos WHERE codigo=?", (codigo_alumno,))
        row = cursor.fetchone()
        if not row:
            return {"encontrado": False, "mensaje": "Alumno no encontrado"}
        alumno_id = row["id"]
 
        # Claves aprobadas
        cursor.execute("""
            SELECT UPPER(TRIM(clave)) AS clave FROM materias
            WHERE alumno_id=? AND estatus='APROBADA' GROUP BY UPPER(TRIM(clave))
        """, (alumno_id,))
        aprobadas = {r["clave"] for r in cursor.fetchall()}
 
        # Plan completo
        cursor.execute("SELECT clave, materia, area, area_cod, orientacion, creditos FROM plan_estudios")
        plan = [dict(r) for r in cursor.fetchall()]
 
        query_norm = normalizar(query)
        query_upper = query.strip().upper()
 
        # Filtro 1: clave exacta
        for p in plan:
            if p["clave"].upper() == query_upper and p["clave"].upper() not in aprobadas:
                return {"encontrado": True, "filtro": "clave", "materia": p}
 
        # Filtro 2: nombre similar (umbral 0.6)
        candidatos = [
            (p, _similitud(p["materia"], query))
            for p in plan
            if p["clave"].upper() not in aprobadas
        ]
        candidatos.sort(key=lambda x: -x[1])
        if candidatos and candidatos[0][1] >= 0.55:
            return {"encontrado": True, "filtro": "nombre", "materia": candidatos[0][0],
                    "similitud": round(candidatos[0][1], 2)}
 
        # Filtro 3: no encontró → si tiene área manual, lista materias de esa área
        if area_manual:
            area_norm = normalizar(area_manual)
            lista = [
                p for p in plan
                if normalizar(p["area"]) == area_norm
                and p["clave"].upper() not in aprobadas
            ]
            return {"encontrado": False, "filtro": "area",
                    "mensaje": f"No encontré '{query}'. Materias disponibles en '{area_manual}':",
                    "lista_area": lista}
 
        # No encontró nada → pedir área
        areas_disponibles = list({r["area"] for r in plan})
        return {"encontrado": False, "filtro": "ninguno",
                "mensaje": f"No encontré '{query}' por clave ni nombre similar. ¿A qué área pertenece?",
                "areas": areas_disponibles}
 

    def guardar_horario(self, codigo_alumno: str, materias_horario: list, calendario: str) -> dict:
        """
        Guarda el horario del alumno.
        materias_horario: lista de dicts con {clave, nombre, creditos}

        Para cada materia intenta identificarla en el plan de estudios:
          1. Por clave exacta
          2. Por nombre similar (difflib >= 0.55)
        Si no la encuentra -> la guarda igual pero la reporta en
        'requieren_equivalencia' para que el usuario pueda indicar con
        qué materia del plan hace equivalencia.
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT id FROM alumnos WHERE codigo=?", (codigo_alumno,))
        row = cursor.fetchone()
        if not row:
            return {"ok": False, "error": "Alumno no encontrado"}
        alumno_id = row["id"]

        # Cargar plan completo para hacer matching
        cursor.execute("SELECT clave, materia, area, creditos FROM plan_estudios")
        plan_rows = cursor.fetchall()
        plan_por_clave = {str(r["clave"]).strip().upper(): dict(r) for r in plan_rows}

        cursor.execute("DELETE FROM horario WHERE alumno_id=? AND calendario=?",
                       (alumno_id, calendario))

        requieren_equivalencia = []

        for m in materias_horario:
            clave_orig = str(m.get("clave", "")).upper().strip()
            nombre_orig = m.get("nombre", "")
            creditos_orig = int(m.get("creditos", 0))

            # 1. Buscar por clave exacta
            match_plan = plan_por_clave.get(clave_orig)

            # 2. Si no encontró por clave, buscar por nombre similar
            if not match_plan and nombre_orig:
                candidatos = [
                    (p, _similitud(p["materia"], nombre_orig))
                    for p in plan_por_clave.values()
                ]
                candidatos.sort(key=lambda x: -x[1])
                if candidatos and candidatos[0][1] >= 0.55:
                    match_plan = candidatos[0][0]
                    log.info("Horario: '%s' -> match por nombre '%s' (%.0f%%)",
                             nombre_orig, match_plan["materia"], candidatos[0][1] * 100)

            # Guardar en BD (con datos del plan si se encontró, o con los originales)
            clave_final   = match_plan["clave"].upper() if match_plan else clave_orig
            nombre_final  = match_plan["materia"]       if match_plan else nombre_orig
            creditos_final = (match_plan["creditos"]
                              if match_plan and match_plan["creditos"]
                              else creditos_orig)

            cursor.execute("""
                INSERT OR REPLACE INTO horario (alumno_id, clave, nombre, creditos, calendario)
                VALUES (?,?,?,?,?)
            """, (alumno_id, clave_final, nombre_final, creditos_final, calendario))

            if not match_plan:
                requieren_equivalencia.append({
                    "clave_horario": clave_orig,
                    "nombre_horario": nombre_orig,
                    "creditos": creditos_orig,
                })

        self.conn.commit()
        return {
            "ok": True,
            "guardadas": len(materias_horario),
            "requieren_equivalencia": requieren_equivalencia,
        }

    def asignar_equivalencia(self, codigo_alumno: str, clave_horario: str,
                             clave_plan: str, calendario: str) -> dict:
        """
        El usuario indicó que 'clave_horario' (materia externa/no reconocida)
        equivale a 'clave_plan' en el plan de estudios oficial.
        Actualiza el registro del horario con los datos del plan.
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT id FROM alumnos WHERE codigo=?", (codigo_alumno,))
        row = cursor.fetchone()
        if not row:
            return {"ok": False, "error": "Alumno no encontrado"}
        alumno_id = row["id"]

        cursor.execute(
            "SELECT clave, materia, area, creditos FROM plan_estudios "
            "WHERE UPPER(TRIM(clave))=?",
            (clave_plan.upper().strip(),)
        )
        plan_row = cursor.fetchone()
        if not plan_row:
            return {"ok": False, "error": f"Clave '{clave_plan}' no existe en el plan de estudios"}

        cursor.execute("""
            UPDATE horario SET clave=?, nombre=?, creditos=?
            WHERE alumno_id=? AND UPPER(TRIM(clave))=? AND calendario=?
        """, (
            plan_row["clave"].upper(), plan_row["materia"], plan_row["creditos"],
            alumno_id, clave_horario.upper().strip(), calendario,
        ))

        if cursor.rowcount == 0:
            # Intento sin filtro de calendario por si fue guardado sin él
            cursor.execute("""
                UPDATE horario SET clave=?, nombre=?, creditos=?
                WHERE alumno_id=? AND UPPER(TRIM(clave))=?
            """, (
                plan_row["clave"].upper(), plan_row["materia"], plan_row["creditos"],
                alumno_id, clave_horario.upper().strip(),
            ))

        self.conn.commit()
        return {
            "ok": True,
            "clave_asignada": plan_row["clave"],
            "nombre_asignado": plan_row["materia"],
            "area": plan_row["area"],
        }

"""
motor.py — Motor de Inferencia IA para sugerencias académicas
=============================================================
Compara el historial del alumno contra el Plan de Estudios IELC y genera:
- Materias disponibles para el próximo ciclo (prerrequisitos cumplidos)
- Materias bloqueadas y por qué prerrequisito les falta
- Alertas institucionales (Art. 33, Servicio Social, Prácticas)
- Progreso por área de formación

CAMBIOS v2:
- Comparación de horario por CLAVE y por NOMBRE (normalizado)
- Las materias que ya están en el horario NO se sugieren como disponibles
- Los créditos de materias del horario NO se suman al progreso real por área
  (se muestran como "en curso" aparte, sin inflar los créditos aprobados)
"""

import re
from collections import defaultdict
from typing import Optional

from database import (
    init_db, normalizar,
    PCT_SERVICIO_SOCIAL, PCT_PRACTICAS_PROF,
    MAX_REPROBACIONES, TOP_SUGERENCIAS, CREDITOS_TOTALES_IELC,
    log,
)


def _barra(pct: float, ancho: int = 8) -> str:
    llenas = int(pct / 100 * ancho)
    return "█" * llenas + "░" * (ancho - llenas)


def _estatus_final(intentos: list[dict]) -> dict:
    """
    Determina el estatus FINAL de una materia a partir de todos sus intentos.
    Regla clave: si ALGÚN intento es APROBADA → la materia está aprobada.
    Esto es crítico para no bloquear prerrequisitos de materias que fueron
    reprobadas en ciclos anteriores pero eventualmente aprobadas.
    """
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


class MotorInferencia:
    """Motor de análisis académico e inferencia de materias disponibles."""

    def __init__(self, db_path: str = "kardex_udg.db"):
        self.conn = init_db(db_path)

    def analizar(self, codigo_alumno: str, horario: list[dict] | None = None) -> Optional[dict]:
        """
        Analiza la situación académica completa de un alumno.

        Args:
            codigo_alumno: Código del alumno a analizar.
            horario: Lista de materias actualmente inscritas (del horario vigente).
                     Cada elemento debe tener al menos 'clave' y/o 'nombre'.
                     Ej: [{"clave": "I5886", "nombre": "CALCULO DIFERENCIAL"}, ...]

                     Las materias del horario:
                     1. NO se sugieren como "disponibles" (ya están inscritas).
                     2. Sus créditos NO se suman al avance real por área
                        (aún no están aprobadas).

        Retorna dict con: alumno, disponibles, bloqueadas, alertas, por_area, en_horario
        """
        cursor = self.conn.cursor()

        # ── 1. Obtener alumno ─────────────────────────────────────
        cursor.execute("""
            SELECT id, codigo, nombre, carrera, codigo_carrera,
                   creditos_adquiridos, creditos_requeridos,
                   creditos_faltantes, promedio,
                   ciclo_admision, ultimo_ciclo, situacion
            FROM alumnos
            WHERE codigo = ?
        """, (codigo_alumno,))
        row = cursor.fetchone()
        if not row:
            print(f"❌ El código {codigo_alumno} no está en la base de datos.")
            print("💡 Ejecuta primero: python kardex.py cargar <archivo.pdf>")
            return None

        alumno = dict(row)
        alumno_id = alumno["id"]
        creditos_actuales = alumno["creditos_adquiridos"] or 0
        creditos_requeridos = alumno["creditos_requeridos"] or CREDITOS_TOTALES_IELC
        promedio_actual = alumno["promedio"] or 0.0
        pct_avance = (creditos_actuales / creditos_requeridos * 100) \
            if creditos_requeridos > 0 else 0.0

        umbral_servicio = int(creditos_requeridos * PCT_SERVICIO_SOCIAL)
        umbral_practicas = int(creditos_requeridos * PCT_PRACTICAS_PROF)

        # ── 2. Construir sets de identificadores del horario ──────
        # El horario contiene materias YA INSCRITAS en el ciclo actual.
        # Comparamos tanto por clave como por nombre normalizado.
        horario = horario or []
        horario_claves: set[str] = set()   # claves en MAYÚSCULAS
        horario_nombres: set[str] = set()  # nombres normalizados (sin tildes, min.)

        for h in horario:
            clave_h = str(h.get("clave", "")).strip().upper()
            nombre_h = normalizar(h.get("nombre", ""))
            if clave_h:
                horario_claves.add(clave_h)
            if nombre_h:
                horario_nombres.add(nombre_h)

        # ── 3. Todos los intentos del alumno ──────────────────────
        cursor.execute("""
            SELECT clave, nombre, estatus, calificacion,
                   creditos, calendario, fecha_eval, tipo
            FROM materias
            WHERE alumno_id = ?
            ORDER BY clave, fecha_eval
        """, (alumno_id,))
        todos_intentos = [dict(r) for r in cursor.fetchall()]

        # ── 4. Estatus final por clave ────────────────────────────
        # CORRECCIÓN CENTRAL: agrupar todos los intentos y determinar
        # si la materia está aprobada o reprobada DEFINITIVAMENTE.
        por_clave: dict = defaultdict(list)
        for intento in todos_intentos:
            clave_norm = str(intento["clave"]).strip().upper()
            por_clave[clave_norm].append(intento)

        claves_aprobadas: set[str] = set()
        nombres_aprobados: set[str] = set()
        rep_activas: dict = {}

        for clave, intentos in por_clave.items():
            resultado = _estatus_final(intentos)
            if resultado["final"] == "APROBADA":
                claves_aprobadas.add(clave)
                nombre_norm = normalizar(resultado["ref"].get("nombre", ""))
                if nombre_norm:
                    nombres_aprobados.add(nombre_norm)
            else:
                if resultado["calendarios_rep"]:
                    rep_activas[clave] = {
                        "nombre": resultado["ref"].get("nombre", clave),
                        "calendarios": resultado["calendarios_rep"],
                    }

        # ── 5. Plan de estudios ───────────────────────────────────
        cursor.execute("""
            SELECT clave, materia, area_cod, area,
                   orientacion_cod, orientacion,
                   creditos, prerrequisito
            FROM plan_estudios
            ORDER BY area_cod, materia
        """)
        plan_rows = cursor.fetchall()

        if not plan_rows:
            print("⚠️ La tabla plan_estudios está vacía.")
            print("💡 Ejecuta: python kardex.py importar-plan")
            return None

        # ── 6. Clasificar materias ────────────────────────────────
        disponibles = []
        bloqueadas = []
        en_horario_info = []  # materias del plan que están en el horario activo

        for row in plan_rows:
            clave = str(row["clave"]).strip().upper()
            materia = row["materia"]
            area_cod = row["area_cod"]
            area = row["area"]
            ori = row["orientacion"] or ""
            creditos = row["creditos"]
            pre = (row["prerrequisito"] or "").strip()

            materia_norm = normalizar(materia)

            # ① Ya aprobada en el kárdex → saltar
            if clave in claves_aprobadas or materia_norm in nombres_aprobados:
                continue

            # ② Está en el horario activo → registrar, pero NO sugerir
            #    La comparación es por CLAVE o por NOMBRE normalizado
            en_horario = (
                clave in horario_claves or
                materia_norm in horario_nombres
            )
            if en_horario:
                en_horario_info.append({
                    "clave": clave, "nombre": materia,
                    "area_cod": area_cod, "area": area,
                    "orientacion": ori, "creditos": creditos,
                })
                continue  # no va a disponibles ni a bloqueadas

            # ③ Clasificar: disponible o bloqueada
            if not pre:
                disponibles.append({
                    "clave": clave, "nombre": materia,
                    "area_cod": area_cod, "area": area,
                    "orientacion": ori, "creditos": creditos,
                    "estado": "Sin prerrequisito",
                })
            else:
                pre_norm = normalizar(pre)
                pre_clave = pre.upper().strip()

                # Prerrequisito cumplido: comparar por nombre normalizado O por clave
                if pre_norm in nombres_aprobados or pre_clave in claves_aprobadas:
                    disponibles.append({
                        "clave": clave, "nombre": materia,
                        "area_cod": area_cod, "area": area,
                        "orientacion": ori, "creditos": creditos,
                        "estado": f"Prerrequisito OK: {pre}",
                    })
                else:
                    bloqueadas.append({
                        "clave": clave, "nombre": materia,
                        "area_cod": area_cod, "area": area,
                        "orientacion": ori, "creditos": creditos,
                        "prerrequisito": pre,
                    })

        # ── 7. Progreso por área ──────────────────────────────────
        # Solo se cuentan créditos APROBADOS en el kárdex.
        # Las materias del horario activo se excluyen explícitamente del JOIN
        # para garantizar que sus créditos no se sumen al avance real,
        # aunque un caso raro pudiera tener la misma clave aprobada y en horario.

        if horario_claves:
            placeholders = ",".join("?" * len(horario_claves))
            cursor.execute(f"""
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
                      AND UPPER(TRIM(clave)) NOT IN ({placeholders})
                    GROUP BY UPPER(TRIM(clave))
                ) aprobada ON UPPER(TRIM(p.clave)) = aprobada.clave
                GROUP BY p.area_cod, p.area
                ORDER BY p.area_cod
            """, (alumno_id, *horario_claves))
        else:
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

        por_area = [dict(r) for r in cursor.fetchall()]

        # Calcular créditos del horario por área (informativo, no se suman al real)
        creditos_horario_por_area: dict[str, int] = defaultdict(int)
        for h_mat in en_horario_info:
            creditos_horario_por_area[h_mat["area"]] += h_mat.get("creditos", 0)

        for a in por_area:
            a["creditos_en_horario"] = creditos_horario_por_area.get(a["area"], 0)
            a["area_cubierta"] = a["creditos_aprobados"] >= a["creditos_plan"]

        # ── 8. Alertas ────────────────────────────────────────────
        alertas = []

        # Art. 33 — reprobada en 2+ periodos distintos sin aprobar
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

        # Prácticas / Servicio Social
        if creditos_requeridos > 0:
            if creditos_actuales >= umbral_practicas:
                alertas.append({"tipo": "PRACTICAS", "icono": "🔵", "descripcion": (
                    f"Con {creditos_actuales}/{creditos_requeridos} créditos "
                    f"({pct_avance:.1f}%) ya puedes tramitar Prácticas Profesionales."
                )})
            elif creditos_actuales >= umbral_servicio:
                alertas.append({"tipo": "SERVICIO_SOCIAL", "icono": "🟢", "descripcion": (
                    f"Con {creditos_actuales}/{creditos_requeridos} créditos "
                    f"({pct_avance:.1f}%) ya puedes realizar Servicio Social."
                )})
            else:
                alertas.append({"tipo": "SERVICIO_PENDIENTE", "icono": "⏳", "descripcion": (
                    f"Servicio Social: te faltan {umbral_servicio - creditos_actuales} créditos "
                    f"({creditos_actuales}/{umbral_servicio})."
                )})

        # Promedio bajo
        if 0 < promedio_actual < 70:
            alertas.append({"tipo": "PROMEDIO_BAJO", "icono": "📉", "descripcion": (
                f"Promedio {promedio_actual:.2f} por debajo de 70. Considera asesorías."
            )})

        # Inconsistencia: aprobada con 0 créditos
        cursor.execute("""
            SELECT clave FROM materias
            WHERE alumno_id=? AND estatus='APROBADA' AND creditos=0
            GROUP BY clave
        """, (alumno_id,))
        sin_cred = cursor.fetchall()
        if sin_cred:
            claves_sc = ", ".join(r["clave"] for r in sin_cred)
            alertas.append({"tipo": "CREDITO_ERROR", "icono": "🔴", "descripcion": (
                f"Materias aprobadas con 0 créditos: {claves_sc}. "
                "Verificar con Servicios Escolares."
            )})

        # ── 9. Imprimir reporte ───────────────────────────────────
        self._imprimir(
            alumno, pct_avance, disponibles, bloqueadas,
            alertas, por_area, len(claves_aprobadas), rep_activas,
            en_horario_info,
        )

        return {
            "alumno": alumno,
            "disponibles": disponibles,
            "bloqueadas": bloqueadas,
            "alertas": alertas,
            "por_area": por_area,
            "en_horario": en_horario_info,
        }

    # ── Impresión ──────────────────────────────────────────────────

    def _imprimir(self, alumno, pct_avance, disponibles, bloqueadas,
                  alertas, por_area, n_aprobadas, rep_activas, en_horario_info=None):
        sep  = "═" * 65
        sep2 = "─" * 65
        en_horario_info = en_horario_info or []

        print(f"\n{sep}")
        print(f" 🤖 MOTOR DE INFERENCIA IA — ANÁLISIS ACADÉMICO IELC")
        print(sep)
        print(f" Alumno   : {alumno.get('nombre','N/D')}")
        print(f" Código   : {alumno.get('codigo','N/D')}")
        print(f" Carrera  : {alumno.get('carrera','N/D')} ({alumno.get('codigo_carrera','')})")
        print(f" Situación: {alumno.get('situacion','N/D')} | "
              f"Último ciclo: {alumno.get('ultimo_ciclo','?')}")
        print(f" Créditos : {alumno.get('creditos_adquiridos',0)} / "
              f"{alumno.get('creditos_requeridos',0)} ({pct_avance:.1f}%)")
        print(f" Promedio : {alumno.get('promedio',0.0):.2f}")
        print(f" Materias aprobadas (estatus final): {n_aprobadas}")
        print(sep2)

        # Materias en horario activo (inscritas, no aprobadas aún)
        if en_horario_info:
            print(f"\n📅 EN HORARIO ACTIVO ({len(en_horario_info)}) — créditos pendientes de aprobación:")
            for h in en_horario_info:
                ori = f" · {h['orientacion']}" if h.get("orientacion") else ""
                print(f"   › [{h['clave']}] {h['nombre']}{ori}  ({h['creditos']} cr)")

        # Avance por área
        if por_area:
            print(f"\n📊 AVANCE POR ÁREA (solo créditos APROBADOS):")
            print(f"   {'Área':<40} {'Tot':>4} {'Apr':>4} {'CrPlan':>7} {'CrAdq':>6} {'EnCurso':>8}")
            print("   " + "─" * 74)
            for a in por_area:
                pct_a = (a["aprobadas"] / a["total_plan"] * 100) if a["total_plan"] > 0 else 0
                en_curso_str = f"+{a['creditos_en_horario']}cr" if a.get("creditos_en_horario") else "      -"
                print(
                    f"   {a['area']:<40} {a['total_plan']:>4} {a['aprobadas']:>4} "
                    f"{a['creditos_plan']:>7} {a['creditos_aprobados']:>6} "
                    f"{en_curso_str:>8}  "
                    f"{_barra(pct_a)} {pct_a:.0f}%"
                )

        # Reprobaciones activas
        if rep_activas:
            print(f"\n❌ MATERIAS CON REPROBACIONES PENDIENTES ({len(rep_activas)}):")
            for clave, info in rep_activas.items():
                periodos = ", ".join(sorted(info["calendarios"]))
                print(f"   › [{clave}] {info['nombre']}")
                print(f"     Reprobada en {len(info['calendarios'])} periodo(s): {periodos}")

        # Materias disponibles (excluye las del horario)
        print(f"\n✅ MATERIAS DISPONIBLES ({len(disponibles)} encontradas):")
        if not disponibles:
            print("   No hay materias disponibles — verifica que el plan esté cargado.")
        else:
            por_area_disp: dict = {}
            for d in disponibles:
                por_area_disp.setdefault(d["area"], []).append(d)

            mostradas = 0
            for area, mats in por_area_disp.items():
                if mostradas >= TOP_SUGERENCIAS:
                    break
                print(f"\n   [{area}]")
                for d in mats:
                    if mostradas >= TOP_SUGERENCIAS:
                        break
                    ori = f" · {d['orientacion']}" if d["orientacion"] else ""
                    print(f"   › [{d['clave']}] {d['nombre']}{ori}")
                    print(f"     {d['creditos']} créditos | {d['estado']}")
                    mostradas += 1

            if len(disponibles) > TOP_SUGERENCIAS:
                print(f"\n   … y {len(disponibles) - TOP_SUGERENCIAS} materias más disponibles.")

        # Bloqueadas
        if bloqueadas:
            print(f"\n🔒 MATERIAS BLOQUEADAS ({len(bloqueadas)}):")
            for b in bloqueadas[:6]:
                ori = f" · {b['orientacion']}" if b["orientacion"] else ""
                print(f"   › [{b['clave']}] {b['nombre']}{ori}")
                print(f"     Requiere aprobar: {b['prerrequisito']}")
            if len(bloqueadas) > 6:
                print(f"   … y {len(bloqueadas) - 6} más bloqueadas.")

        # Alertas
        print(f"\n⚠️  ALERTAS INSTITUCIONALES:")
        if not alertas:
            print("   🟢 Sin alertas activas.")
        else:
            for a in alertas:
                print(f"   {a['icono']} [{a['tipo']}] {a['descripcion']}")

        print(f"\n{sep}\n")

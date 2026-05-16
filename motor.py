"""
motor.py — Motor de Inferencia IA para sugerencias académicas
=============================================================
Cambios respecto a versión anterior:
- analizar() acepta `materias_en_curso` (lista de dicts con 'clave' y 'nombre')
  para excluirlas de las sugerencias (comparación por clave primero, luego por nombre).
- Cuando una materia del kárdex no se encuentra en el plan de estudios,
  el motor la registra en `sin_equivalencia` para que la API/frontend
  pueda pedirle al usuario los datos de equivalencia.
- Nuevo método: registrar_equivalencia(clave_kardex, clave_plan, nombre_plan,
  creditos_plan, area_cod, area) — inserta la fila en plan_estudios.
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
    Si ALGÚN intento es APROBADA → la materia está aprobada.
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
        self.db_path = db_path

    # ─────────────────────────────────────────────────────────────
    # NUEVO: registrar equivalencia cuando la materia del kárdex
    # no existe en el plan de estudios.
    # ─────────────────────────────────────────────────────────────
    def registrar_equivalencia(
        self,
        clave_plan: str,
        nombre_plan: str,
        creditos_plan: int,
        area_cod: str,
        area: str,
        prerrequisito: str = "",
        orientacion_cod: str = "",
        orientacion: str = "",
    ) -> bool:
        """
        Inserta una materia en plan_estudios con los datos proporcionados por el usuario.
        Retorna True si se insertó correctamente.
        """
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO plan_estudios
                    (clave, materia, area_cod, area,
                     orientacion_cod, orientacion,
                     creditos, prerrequisito)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                clave_plan.strip().upper(),
                nombre_plan.strip(),
                area_cod.strip(),
                area.strip(),
                orientacion_cod.strip(),
                orientacion.strip(),
                int(creditos_plan),
                prerrequisito.strip(),
            ))
            self.conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            log(f"[registrar_equivalencia] Error: {e}")
            return False

    # ─────────────────────────────────────────────────────────────
    # ANÁLISIS PRINCIPAL
    # ─────────────────────────────────────────────────────────────
    def analizar(
        self,
        codigo_alumno: str,
        materias_en_curso: list[dict] | None = None,
    ) -> Optional[dict]:
        """
        Analiza la situación académica de un alumno.

        Parameters
        ----------
        codigo_alumno : str
            Código UDG del alumno.
        materias_en_curso : list[dict] | None
            Lista de materias que el alumno está cursando AHORA según su horario.
            Cada dict debe tener al menos 'clave' (str) y opcionalmente 'nombre' (str).
            Estas materias NO aparecerán en las sugerencias.

        Returns
        -------
        dict con: alumno, disponibles, bloqueadas, alertas, por_area,
                  sin_equivalencia (materias del kárdex sin match en el plan).
        """
        cursor = self.conn.cursor()

        # ── Normalizar materias_en_curso ──────────────────────────
        # Guardamos claves y nombres en sets para comparación rápida.
        en_curso_claves: set[str] = set()
        en_curso_nombres: set[str] = set()
        for m in (materias_en_curso or []):
            if m.get("clave"):
                en_curso_claves.add(str(m["clave"]).strip().upper())
            if m.get("nombre"):
                en_curso_nombres.add(normalizar(m["nombre"]))

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

        # ── 2. Todos los intentos del alumno ──────────────────────
        cursor.execute("""
            SELECT clave, nombre, estatus, calificacion,
                   creditos, calendario, fecha_eval, tipo
            FROM materias
            WHERE alumno_id = ?
            ORDER BY clave, fecha_eval
        """, (alumno_id,))
        todos_intentos = [dict(r) for r in cursor.fetchall()]

        # ── 3. Estatus final por clave ────────────────────────────
        por_clave: dict = defaultdict(list)
        for intento in todos_intentos:
            clave_norm = str(intento["clave"]).strip().upper()
            por_clave[clave_norm].append(intento)

        claves_aprobadas: set[str] = set()
        nombres_aprobados: set[str] = set()
        # También guardamos nombres normalizados de TODAS las materias del alumno
        # (aprobadas + en curso del kárdex) para detectar sin_equivalencia
        nombres_alumno_por_clave: dict[str, str] = {}  # clave → nombre normalizado
        rep_activas: dict = {}

        for clave, intentos in por_clave.items():
            resultado = _estatus_final(intentos)
            nombre_ref = normalizar(resultado["ref"].get("nombre", ""))
            nombres_alumno_por_clave[clave] = nombre_ref
            if resultado["final"] == "APROBADA":
                claves_aprobadas.add(clave)
                if nombre_ref:
                    nombres_aprobados.add(nombre_ref)
            else:
                if resultado["calendarios_rep"]:
                    rep_activas[clave] = {
                        "nombre": resultado["ref"].get("nombre", clave),
                        "calendarios": resultado["calendarios_rep"],
                    }

        # ── 4. Plan de estudios ───────────────────────────────────
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
            return None

        # Índice del plan para búsqueda por nombre
        plan_por_clave: dict[str, dict] = {}
        plan_por_nombre: dict[str, dict] = {}
        for row in plan_rows:
            r = dict(row)
            plan_por_clave[r["clave"].strip().upper()] = r
            plan_por_nombre[normalizar(r["materia"])] = r

        # ── 5. Detectar materias del alumno sin equivalencia en el plan
        # (solo las NO aprobadas para no pedir datos de materias ya OK)
        sin_equivalencia: list[dict] = []
        for clave, intentos in por_clave.items():
            resultado = _estatus_final(intentos)
            nombre_kardex = resultado["ref"].get("nombre", "")
            nombre_norm = normalizar(nombre_kardex)
            # ¿Existe en el plan por clave o por nombre?
            en_plan = (clave in plan_por_clave) or (nombre_norm and nombre_norm in plan_por_nombre)
            if not en_plan:
                sin_equivalencia.append({
                    "clave_kardex": clave,
                    "nombre_kardex": nombre_kardex,
                    "estatus": resultado["final"],
                    "creditos": resultado["ref"].get("creditos", 0),
                })

        # ── 6. Clasificar materias del plan ──────────────────────
        disponibles = []
        bloqueadas = []

        for row in plan_rows:
            clave = str(row["clave"]).strip().upper()
            materia = row["materia"]
            area_cod = row["area_cod"]
            area = row["area"]
            ori = row["orientacion"] or ""
            creditos = row["creditos"]
            pre = (row["prerrequisito"] or "").strip()
            materia_norm = normalizar(materia)

            # ── Ya aprobada → saltar
            if clave in claves_aprobadas or materia_norm in nombres_aprobados:
                continue

            # ── EN CURSO según horario → saltar (NUEVO)
            # Comparar primero por clave, luego por nombre normalizado
            if clave in en_curso_claves:
                continue
            if materia_norm and materia_norm in en_curso_nombres:
                continue

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

        # ── 8. Alertas ────────────────────────────────────────────
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

        if 0 < promedio_actual < 70:
            alertas.append({"tipo": "PROMEDIO_BAJO", "icono": "📉", "descripcion": (
                f"Promedio {promedio_actual:.2f} por debajo de 70. Considera asesorías."
            )})

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
            en_curso_claves, sin_equivalencia,
        )

        return {
            "alumno": alumno,
            "disponibles": disponibles,
            "bloqueadas": bloqueadas,
            "alertas": alertas,
            "por_area": por_area,
            "sin_equivalencia": sin_equivalencia,   # ← NUEVO
        }

    # ── Impresión ──────────────────────────────────────────────────
    def _imprimir(self, alumno, pct_avance, disponibles, bloqueadas,
                  alertas, por_area, n_aprobadas, rep_activas,
                  en_curso_claves=None, sin_equivalencia=None):
        sep = "═" * 65
        sep2 = "─" * 65
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
        if en_curso_claves:
            print(f" Materias en curso (excluidas de sugerencias): {', '.join(sorted(en_curso_claves))}")
        print(sep2)

        if por_area:
            print(f"\n📊 AVANCE POR ÁREA:")
            print(f" {'Área':<40} {'Tot':>4} {'Apr':>4} {'CrPlan':>7} {'CrAdq':>6}")
            print(" " + "─" * 65)
            for a in por_area:
                pct_a = (a["aprobadas"] / a["total_plan"] * 100) if a["total_plan"] > 0 else 0
                print(
                    f" {a['area']:<40} {a['total_plan']:>4} {a['aprobadas']:>4} "
                    f"{a['creditos_plan']:>7} {a['creditos_aprobados']:>6} "
                    f"{_barra(pct_a)} {pct_a:.0f}%"
                )

        if rep_activas:
            print(f"\n❌ MATERIAS CON REPROBACIONES PENDIENTES ({len(rep_activas)}):")
            for clave, info in rep_activas.items():
                periodos = ", ".join(sorted(info["calendarios"]))
                print(f"  › [{clave}] {info['nombre']}")
                print(f"    Reprobada en {len(info['calendarios'])} periodo(s): {periodos}")

        # Materias sin equivalencia en el plan
        if sin_equivalencia:
            print(f"\n❓ MATERIAS SIN EQUIVALENCIA EN EL PLAN ({len(sin_equivalencia)}):")
            for s in sin_equivalencia:
                print(f"  › [{s['clave_kardex']}] {s['nombre_kardex']} ({s['estatus']})")
            print("  → El sistema pedirá al usuario los datos de equivalencia.")

        print(f"\n✅ MATERIAS DISPONIBLES ({len(disponibles)} encontradas):")
        if not disponibles:
            print("  No hay materias disponibles — verifica que el plan esté cargado.")
        else:
            por_area_disp: dict = {}
            for d in disponibles:
                por_area_disp.setdefault(d["area"], []).append(d)
            mostradas = 0
            for area, mats in por_area_disp.items():
                if mostradas >= TOP_SUGERENCIAS:
                    break
                print(f"\n  [{area}]")
                for d in mats:
                    if mostradas >= TOP_SUGERENCIAS:
                        break
                    ori = f" · {d['orientacion']}" if d["orientacion"] else ""
                    print(f"  › [{d['clave']}] {d['nombre']}{ori}")
                    print(f"    {d['creditos']} créditos | {d['estado']}")
                    mostradas += 1
            if len(disponibles) > TOP_SUGERENCIAS:
                print(f"\n  … y {len(disponibles) - TOP_SUGERENCIAS} materias más disponibles.")

        if bloqueadas:
            print(f"\n🔒 MATERIAS BLOQUEADAS ({len(bloqueadas)}):")
            for b in bloqueadas[:6]:
                ori = f" · {b['orientacion']}" if b["orientacion"] else ""
                print(f"  › [{b['clave']}] {b['nombre']}{ori}")
                print(f"    Requiere aprobar: {b['prerrequisito']}")
            if len(bloqueadas) > 6:
                print(f"  … y {len(bloqueadas) - 6} más bloqueadas.")

        print(f"\n⚠️  ALERTAS INSTITUCIONALES:")
        if not alertas:
            print("  🟢 Sin alertas activas.")
        else:
            for a in alertas:
                print(f"  {a['icono']} [{a['tipo']}] {a['descripcion']}")
        print(f"\n{sep}\n")

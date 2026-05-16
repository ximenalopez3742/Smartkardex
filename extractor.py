"""
extractor.py — Extracción de Kárdex PDF y gestión de la BD
===========================================================
Calibrado al formato real del Kárdex UDG (pdfplumber).

Columnas reales del PDF:
  [0] NRC  [1] Clave  [2] Nombre  [3] Calificación
  [4] Tipo  [5] NC (créditos)  [6] HC (horas)  [7] Fecha
"""

import os
import re
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import pdfplumber

from database import (
    init_db, normalizar, creditos_requeridos_carrera,
    CALIFICACION_MINIMA, MAX_REPROBACIONES,
    PCT_SERVICIO_SOCIAL, PCT_PRACTICAS_PROF,
    log,
)

# ══════════════════════════════════════════════════════════════════
# EXTRACCIÓN DEL PDF
# ══════════════════════════════════════════════════════════════════

def _hash_pdf(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _limpiar(texto) -> str:
    if texto is None:
        return ""
    return re.sub(r"\s+", " ", str(texto)).strip()


def _parse_calificacion(raw: str) -> Optional[int]:
    """Extrae el número de '95 (NOVENTA Y CINCO)' → 95."""
    raw = _limpiar(raw)
    m = re.match(r"^(\d{2,3})", raw)
    return int(m.group(1)) if m else None


def _es_fila_calendario(fila: list) -> Optional[str]:
    if not fila:
        return None
    celda = _limpiar(fila[0]).upper()
    m = re.match(r"CALENDARIO\s+(\d{4}[-–][AB])", celda)
    return m.group(1).replace("–", "-").upper() if m else None


def _es_fila_encabezado(fila: list) -> bool:
    if not fila:
        return False
    primera = _limpiar(fila[0]).upper()
    segunda = _limpiar(fila[1]).upper() if len(fila) > 1 else ""
    return primera in ("CRN", "NRC") and segunda == "CLAVE"


def _separar_califs(celda_raw) -> list[str]:
    """Separa múltiples intentos dentro de una celda separados por \\n."""
    if celda_raw is None:
        return [""]
    resultado, acumulado = [], ""
    for p in str(celda_raw).split("\n"):
        p = p.strip()
        if not p:
            continue
        if re.match(r"^(\d|SD|NP|SIN)", p, re.IGNORECASE) and acumulado:
            resultado.append(acumulado.strip())
            acumulado = p
        else:
            acumulado = (acumulado + " " + p).strip() if acumulado else p
    if acumulado:
        resultado.append(acumulado.strip())
    return resultado or [""]


def _separar_tipos(celda_raw) -> list[str]:
    """Separa ORDINARIO/EXTRAORDINARIO de una celda con \\n."""
    if celda_raw is None:
        return [""]
    texto = " ".join(p.strip() for p in str(celda_raw).split("\n") if p.strip())
    partes = re.split(r"(?=\bORDINARIO\b|\bEXTRAORDINARIO\b)", texto, flags=re.IGNORECASE)
    return [p.strip() for p in partes if p.strip()] or [""]


def _separar_simples(celda_raw) -> list[str]:
    if celda_raw is None:
        return [""]
    return [p.strip() for p in str(celda_raw).split("\n") if p.strip()] or [""]


def _parse_fila_materia(fila: list, calendario: str) -> list[dict]:
    """
    Convierte una fila del PDF en uno o más intentos.
    Maneja el formato con múltiples intentos (ordinario + extraordinario)
    dentro de la misma fila separados por \\n.
    """
    if not fila or len(fila) < 6:
        return []

    clave_raw = _limpiar(str(fila[1]) if fila[1] else "")
    if clave_raw.upper() in ("CLAVE", "") or not clave_raw:
        return []
    if not re.match(r"^[A-Z]{1,3}\d{3,5}$", clave_raw.replace(" ", ""), re.IGNORECASE):
        return []

    nrc    = _limpiar(str(fila[0]) if fila[0] else "")
    clave  = clave_raw
    nombre = _limpiar(str(fila[2]).replace("\n", " ") if fila[2] else "")

    califs = _separar_califs(fila[3])
    tipos  = _separar_tipos(fila[4] if len(fila) > 4 else None)
    ncs    = _separar_simples(fila[5] if len(fila) > 5 else None)
    hcs    = _separar_simples(fila[6] if len(fila) > 6 else None)
    fechas = _separar_simples(fila[7] if len(fila) > 7 else None)

    n_intentos = max(len(califs), len(tipos), len(fechas), len(ncs))

    def get(lst, i):
        return lst[i] if i < len(lst) else (lst[-1] if lst else "")

    intentos = []
    for i in range(n_intentos):
        cal_raw      = get(califs, i)
        tipo         = _limpiar(get(tipos, i))
        nc_raw       = get(ncs, i)
        hc_raw       = get(hcs, i)
        fecha        = get(fechas, i)
        calificacion = _parse_calificacion(cal_raw)

        try:
            creditos = int(nc_raw)
        except (ValueError, TypeError):
            creditos = 0
        try:
            horas = int(hc_raw)
        except (ValueError, TypeError):
            horas = 0

        cal_upper = cal_raw.upper()
        if "SD" in cal_upper or "SIN DERECHO" in cal_upper or "NP" in cal_upper:
            estatus, calificacion = "REPROBADA", None
        elif calificacion is None:
            estatus = "PENDIENTE"
        elif calificacion >= CALIFICACION_MINIMA:
            estatus = "APROBADA"
        else:
            estatus = "REPROBADA"

        intentos.append({
            "nrc": nrc, "clave": clave, "nombre": nombre,
            "calificacion": calificacion, "tipo": tipo,
            "creditos": creditos, "horas": horas,
            "calendario": calendario, "fecha_eval": fecha,
            "estatus": estatus,
        })

    return intentos


def _procesar_tabla_materias(tabla: list, calendario_inicial: str) -> tuple[list, str]:
    """Recorre una tabla extrayendo intentos, actualizando el calendario al vuelo."""
    intentos, calendario_actual = [], calendario_inicial
    for fila in tabla:
        cal = _es_fila_calendario(fila)
        if cal:
            calendario_actual = cal
            log.info("  Calendario detectado: %s", calendario_actual)
            continue
        if _es_fila_encabezado(fila):
            continue
        intentos.extend(_parse_fila_materia(fila, calendario_actual))
    return intentos, calendario_actual


def _parse_areas(tabla: list) -> list:
    """Parsea la tabla de resumen de créditos por área."""
    areas = []
    nombres_area = {
        "BASICO COMUN", "BASICA COMUN OBLIGATORIA",
        "BASICO PARTICULAR OBLIGATORIA", "ESPECIALIZANTE OBLIGATORIA",
        "ESPECIALIZANTE SELECTIVA", "OPTATIVA ABIERTA",
    }
    for fila in tabla:
        celdas = [_limpiar(c) for c in fila]
        nombre_area = None
        for c in celdas:
            if c.upper() in nombres_area:
                nombre_area = c
                break
        if not nombre_area:
            for c in celdas:
                for n in nombres_area:
                    if n in c.upper():
                        nombre_area = c
                        break
                if nombre_area:
                    break
        if nombre_area:
            nums = [int(c) for c in celdas if c != nombre_area and c.isdigit()]
            if len(nums) >= 3:
                areas.append({
                    "area": nombre_area,
                    "requeridos": nums[0], "adquiridos": nums[1], "faltantes": nums[2],
                })
    return areas


def _extraer_cabecera(text: str) -> dict:
    """Parsea el encabezado del PDF. Compatible con ambos formatos reales."""
    alumno = {}
    patrones = {
        "codigo":              r"Código[:\s]+(\d{6,12})",
        "nombre":              r"Nombre[:\s]+([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑa-záéíóúñ ,]+?)(?:\n|Nivel|Admisión|Situación)",
        "nivel":               r"Nivel[:\s]+(LICENCIATURA|MAESTRIA|DOCTORADO|T[ÉE]CNICO)",
        "ciclo_admision":      r"Admisi[oó]n[:\s]+(\d{4}[AB])",
        "ultimo_ciclo":        r"[ÚU]ltimo\s+Ciclo[:\s]+(\d{4}[AB])",
        "situacion":           r"Situaci[oó]n[:\s]+(ACTIVO|BAJA|EGRESADO|TITULADO)",
        "carrera":             r"Carrera[:\s]+(.+?)(?=\n|Centro)",
        "centro":              r"Centro[:\s]+(.+?)(?=\n|Sede)",
        "sede":                r"Sede[:\s]+(.+?)(?=\n|Promedio|Nota|Cr[eé]ditos|$)",
        "creditos_adquiridos": r"Cr[eé]ditos[:\s]+(\d+)",
        "promedio":            r"Promedio[:\s]+(\d{1,2}(?:[.,]\d{1,2})?)",
        "fecha_kardex":        r"Fecha[:\s]+(.+?\d{4})",
    }
    for campo, pat in patrones.items():
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = _limpiar(m.group(1))
            if campo == "creditos_adquiridos":
                try:
                    val = int(val)
                except ValueError:
                    pass
            elif campo == "promedio":
                try:
                    val = float(val.replace(",", "."))
                except ValueError:
                    pass
            alumno[campo] = val

    m = re.search(r"Carrera[:\s]+.+?\(([A-Z]{2,6})\)", text, re.IGNORECASE)
    if m:
        alumno["codigo_carrera"] = m.group(1)

    return alumno


def extraer_pdf(pdf_path: str) -> dict:
    """Lee el PDF y retorna los datos estructurados del alumno y sus materias."""
    log.info("Leyendo: %s", pdf_path)
    alumno, materias, areas = {}, [], []
    full_text, calendario_actual = "", "DESCONOCIDO"

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            full_text += page_text + "\n"

            if not alumno:
                alumno = _extraer_cabecera(page_text)

            for tabla in page.extract_tables():
                if not tabla or len(tabla) < 2:
                    continue

                primera_celda = (_limpiar(tabla[0][0]) if tabla[0] else "").upper()

                if "RESUMEN" in primera_celda:
                    areas = _parse_areas(tabla)
                    continue

                cal_header = _es_fila_calendario(tabla[0])
                if cal_header:
                    calendario_actual = cal_header

                nuevas, calendario_actual = _procesar_tabla_materias(tabla, calendario_actual)
                materias.extend(nuevas)

    # Créditos desde texto si no se encontraron en la cabecera
    if not alumno.get("creditos_adquiridos"):
        m = re.search(r"Créditos[:\s]+(\d+)", full_text)
        if m:
            alumno["creditos_adquiridos"] = int(m.group(1))

    # Créditos requeridos desde catálogo interno (más confiable que el PDF)
    cred_catalogo = creditos_requeridos_carrera(
        alumno.get("codigo_carrera", ""), alumno.get("carrera", "")
    )
    if cred_catalogo:
        alumno["creditos_requeridos"] = cred_catalogo
        adq = alumno.get("creditos_adquiridos", 0) or 0
        alumno["creditos_faltantes"] = max(0, cred_catalogo - adq)
    elif areas:
        for a in areas:
            if a.get("area", "").startswith("_TOTAL"):
                alumno["creditos_requeridos"] = a.get("requeridos", 0)
                alumno["creditos_faltantes"]  = a.get("faltantes", 0)
                break

    return {"alumno": alumno, "materias": materias, "areas": areas}


# ══════════════════════════════════════════════════════════════════
# ALERTAS ACADÉMICAS
# ══════════════════════════════════════════════════════════════════

def _generar_alertas(cur, alumno_id: int, al: dict, materias: list, fecha: str):
    """Genera alertas: Art. 33, Servicio Social, Prácticas, créditos cero."""
    creditos_adq = al.get("creditos_adquiridos") or 0
    creditos_req = al.get("creditos_requeridos") or 0

    # Agrupar: clave → {calendarios donde reprobó} y set de aprobadas
    rep_calendarios: dict[str, set] = {}
    aprobadas_clave: set[str]       = set()

    for m in materias:
        clave = m["clave"]
        if m["estatus"] == "APROBADA":
            aprobadas_clave.add(clave)
        elif m["estatus"] == "REPROBADA":
            rep_calendarios.setdefault(clave, set()).add(m["calendario"])

    alertas = []

    # Art. 33 — reprobada en 2+ periodos distintos sin aprobar
    for clave, calendarios in rep_calendarios.items():
        if clave in aprobadas_clave:
            continue
        if len(calendarios) >= MAX_REPROBACIONES:
            nombre = next((m["nombre"] for m in materias if m["clave"] == clave), clave)
            alertas.append(("ARTICULO",
                f"{nombre} ({clave}) reprobada en {len(calendarios)} periodos distintos "
                f"({', '.join(sorted(calendarios))}). "
                "Revisar aplicación del Artículo 33 del Reglamento General de Evaluaciones."
            ))

    # SD acumulados sin aprobar
    sd_count: dict[str, int] = {}
    for m in materias:
        if m["estatus"] == "REPROBADA" and m["calificacion"] is None:
            sd_count[m["clave"]] = sd_count.get(m["clave"], 0) + 1
    for clave, n_sd in sd_count.items():
        if clave not in aprobadas_clave and n_sd >= 2:
            nombre = next((m["nombre"] for m in materias if m["clave"] == clave), clave)
            alertas.append(("ARTICULO",
                f"{nombre} ({clave}) acumula {n_sd} evaluaciones sin derecho (SD). "
                "Verificar situación reglamentaria."
            ))

    # Servicio Social / Prácticas Profesionales
    if creditos_req > 0:
        pct = creditos_adq / creditos_req
        if pct >= PCT_PRACTICAS_PROF:
            alertas.append(("PRACTICAS",
                f"Llevas {pct*100:.1f}% de créditos ({creditos_adq}/{creditos_req}). "
                "¡Ya puedes tramitar Prácticas Profesionales!"
            ))
        elif pct >= PCT_SERVICIO_SOCIAL:
            alertas.append(("SERVICIO",
                f"Llevas {pct*100:.1f}% de créditos ({creditos_adq}/{creditos_req}). "
                "¡Ya puedes realizar Servicio Social!"
            ))

    # Materias aprobadas con 0 créditos (inconsistencia en el PDF)
    sin_creditos = [m for m in materias if m["estatus"] == "APROBADA" and m["creditos"] == 0]
    if sin_creditos:
        claves = ", ".join(m["clave"] for m in sin_creditos)
        alertas.append(("CREDITO_ERROR",
            f"Materias aprobadas con 0 créditos registrados: {claves}. "
            "Verificar con Servicios Escolares."
        ))

    for tipo, desc in alertas:
        cur.execute(
            "INSERT INTO alertas (alumno_id, tipo, descripcion, activa, fecha) VALUES (?,?,?,1,?)",
            (alumno_id, tipo, desc, fecha)
        )
        log.warning("[ALERTA %s] %s", tipo, desc)


# ══════════════════════════════════════════════════════════════════
# CLASE PRINCIPAL
# ══════════════════════════════════════════════════════════════════

class KardexExtractor:
    """Gestiona la carga, consulta y listado de kárdex en la BD."""

    def __init__(self, db_path: str = "kardex_udg.db"):
        self.conn = init_db(db_path)

    # ── Cargar PDF ─────────────────────────────────────────────────

    def cargar_pdf(self, pdf_path: str) -> dict:
        """
        Procesa un PDF de Kárdex:
          - Alumno nuevo     → INSERT
          - PDF cambió       → UPDATE
          - PDF idéntico     → sin cambios
        """
        if not Path(pdf_path).exists():
            print(f"❌ Archivo no encontrado: {pdf_path}")
            return {}

        try:
            hash_pdf = _hash_pdf(pdf_path)
            datos    = extraer_pdf(pdf_path)
            al       = datos["alumno"]
            materias = datos["materias"]
            areas    = datos["areas"]

            codigo = al.get("codigo")
            if not codigo:
                print("❌ No se encontró el código del alumno en el PDF.")
                return {}

            cur   = self.conn.cursor()
            ahora = datetime.now().isoformat(timespec="seconds")

            cur.execute("SELECT id, pdf_hash FROM alumnos WHERE codigo=?", (codigo,))
            existente = cur.fetchone()

            if existente is None:
                accion = "NUEVO"
                cur.execute("""
                    INSERT INTO alumnos
                      (codigo, nombre, carrera, codigo_carrera, nivel, centro, sede,
                       ciclo_admision, ultimo_ciclo, situacion,
                       creditos_adquiridos, creditos_requeridos, creditos_faltantes,
                       promedio, pdf_hash, fecha_kardex, fecha_carga, fecha_actualizacion)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    codigo, al.get("nombre"), al.get("carrera"), al.get("codigo_carrera"),
                    al.get("nivel"), al.get("centro"), al.get("sede"),
                    al.get("ciclo_admision"), al.get("ultimo_ciclo"), al.get("situacion"),
                    al.get("creditos_adquiridos", 0), al.get("creditos_requeridos", 0),
                    al.get("creditos_faltantes", 0), al.get("promedio", 0.0),
                    hash_pdf, al.get("fecha_kardex"), ahora, ahora,
                ))
                alumno_id = cur.lastrowid
                log.info("NUEVO alumno: %s — %s", codigo, al.get("nombre"))

            elif existente["pdf_hash"] == hash_pdf:
                print(f"ℹ️  Sin cambios: el PDF es idéntico al cargado anteriormente.")
                cur.execute(
                    "INSERT INTO log_cargas (alumno_id, archivo, accion, fecha) VALUES (?,?,?,?)",
                    (existente["id"], os.path.basename(pdf_path), "SIN_CAMBIOS", ahora)
                )
                self.conn.commit()
                return {"accion": "SIN_CAMBIOS", "codigo": codigo}

            else:
                accion    = "ACTUALIZADO"
                alumno_id = existente["id"]
                cur.execute("""
                    UPDATE alumnos SET
                      nombre=?, carrera=?, codigo_carrera=?, nivel=?, centro=?, sede=?,
                      ciclo_admision=?, ultimo_ciclo=?, situacion=?,
                      creditos_adquiridos=?, creditos_requeridos=?, creditos_faltantes=?,
                      promedio=?, pdf_hash=?, fecha_kardex=?, fecha_actualizacion=?
                    WHERE id=?
                """, (
                    al.get("nombre"), al.get("carrera"), al.get("codigo_carrera"),
                    al.get("nivel"), al.get("centro"), al.get("sede"),
                    al.get("ciclo_admision"), al.get("ultimo_ciclo"), al.get("situacion"),
                    al.get("creditos_adquiridos", 0), al.get("creditos_requeridos", 0),
                    al.get("creditos_faltantes", 0), al.get("promedio", 0.0),
                    hash_pdf, al.get("fecha_kardex"), ahora, alumno_id,
                ))
                for tabla in ("materias", "creditos_por_area", "alertas"):
                    cur.execute(f"DELETE FROM {tabla} WHERE alumno_id=?", (alumno_id,))
                log.info("ACTUALIZADO: %s", codigo)

            # Insertar materias (todos los intentos)
            for m in materias:
                cur.execute("""
                    INSERT OR IGNORE INTO materias
                      (alumno_id, nrc, clave, nombre, calificacion, tipo,
                       creditos, horas, calendario, fecha_eval, estatus)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    alumno_id, m["nrc"], m["clave"], m["nombre"],
                    m["calificacion"], m["tipo"], m["creditos"], m["horas"],
                    m["calendario"], m["fecha_eval"], m["estatus"],
                ))

            # Insertar créditos por área
            for a in areas:
                if not a["area"].startswith("_"):
                    cur.execute("""
                        INSERT INTO creditos_por_area (alumno_id, area, requeridos, adquiridos, faltantes)
                        VALUES (?,?,?,?,?)
                    """, (alumno_id, a["area"], a["requeridos"], a["adquiridos"], a["faltantes"]))

            _generar_alertas(cur, alumno_id, al, materias, ahora)

            cur.execute(
                "INSERT INTO log_cargas (alumno_id, archivo, accion, fecha) VALUES (?,?,?,?)",
                (alumno_id, os.path.basename(pdf_path), accion, ahora)
            )
            self.conn.commit()

            print(f"\n✅ Acción: {accion}")
            res = self._resumen(alumno_id)
            self._imprimir_resumen(res)
            return res

        except Exception as e:
            print(f"❌ Error al procesar PDF: {e}")
            log.exception("Error al procesar PDF")
            return {}

    # ── Consultar ──────────────────────────────────────────────────

    def consultar(self, codigo: str):
        cur  = self.conn.cursor()
        fila = cur.execute("SELECT id FROM alumnos WHERE codigo=?", (codigo,)).fetchone()
        if not fila:
            print(f"❌ No se encontró el alumno: {codigo}")
            return
        res = self._resumen(fila["id"])
        self._imprimir_resumen(res)

    # ── Listar ─────────────────────────────────────────────────────

    def listar(self):
        rows = self.conn.execute("""
            SELECT codigo, nombre, carrera, ultimo_ciclo,
                   creditos_adquiridos, creditos_requeridos, promedio
            FROM alumnos ORDER BY nombre
        """).fetchall()

        if not rows:
            print("No hay alumnos registrados.")
            return

        print(f"\n{'Código':<12} {'Nombre':<35} {'Último ciclo':<14} {'Créd':>5} {'Prom':>6}")
        print("─" * 76)
        for r in rows:
            adq = r["creditos_adquiridos"] or 0
            req = r["creditos_requeridos"] or 0
            pct = f"({adq/req*100:.0f}%)" if req > 0 else ""
            print(
                f"{r['codigo']:<12} "
                f"{(r['nombre'] or 'N/D')[:34]:<35} "
                f"{r['ultimo_ciclo'] or '?':<14} "
                f"{adq:>5} "
                f"{r['promedio'] or 0.0:>6.2f} {pct}"
            )
        print()

    # ── Internos ───────────────────────────────────────────────────

    def _resumen(self, alumno_id: int) -> dict:
        from collections import defaultdict
        cur     = self.conn.cursor()
        alumno  = dict(cur.execute("SELECT * FROM alumnos WHERE id=?", (alumno_id,)).fetchone())
        intentos = [dict(r) for r in cur.execute(
            "SELECT * FROM materias WHERE alumno_id=? ORDER BY calendario, clave",
            (alumno_id,)
        ).fetchall()]
        areas   = [dict(r) for r in cur.execute(
            "SELECT area, requeridos, adquiridos, faltantes FROM creditos_por_area WHERE alumno_id=?",
            (alumno_id,)
        ).fetchall()]
        alertas = [dict(r) for r in cur.execute(
            "SELECT tipo, descripcion FROM alertas WHERE alumno_id=? AND activa=1",
            (alumno_id,)
        ).fetchall()]

        por_clave = defaultdict(list)
        for i in intentos:
            por_clave[i["clave"]].append(i)

        aprobadas, reprobadas = [], []
        for clave, lista in por_clave.items():
            if any(i["estatus"] == "APROBADA" for i in lista):
                mejor = max(
                    (i for i in lista if i["estatus"] == "APROBADA"),
                    key=lambda x: (x["creditos"], x["fecha_eval"] or "")
                )
                aprobadas.append(mejor)
            else:
                ultimo = max(lista, key=lambda x: x["fecha_eval"] or "")
                reprobadas.append({**ultimo, "num_intentos": len(lista)})

        return {
            "alumno": alumno,
            "materias": {
                "total_intentos": len(intentos),
                "total_materias": len(por_clave),
                "aprobadas": aprobadas,
                "reprobadas": reprobadas,
            },
            "areas": areas,
            "alertas": alertas,
        }

    def _imprimir_resumen(self, res: dict):
        a   = res["alumno"]
        m   = res["materias"]
        sep = "═" * 65

        adq = a.get("creditos_adquiridos", 0) or 0
        req = a.get("creditos_requeridos", 0) or 0
        pct = (adq / req * 100) if req > 0 else 0.0

        print(f"\n{sep}")
        print(f"  {a.get('nombre', 'N/D')}")
        print(f"  Código    : {a.get('codigo', 'N/D')}")
        print(f"  Carrera   : {a.get('carrera', 'N/D')} ({a.get('codigo_carrera','')})")
        print(f"  Admisión  : {a.get('ciclo_admision','?')}  |  Último ciclo: {a.get('ultimo_ciclo','?')}")
        print(f"  Situación : {a.get('situacion', 'N/D')}")
        print(f"  Créditos  : {adq} / {req}  ({pct:.1f}% completado)")
        print(f"  Promedio  : {a.get('promedio', 0.0):.2f}")
        print(sep)

        print(f"\n📚 Materias distintas : {m['total_materias']}")
        print(f"   ✅ Aprobadas        : {len(m['aprobadas'])}")
        print(f"   ❌ Con reprobaciones: {len(m['reprobadas'])}")
        print(f"   📝 Total de intentos: {m['total_intentos']}")

        if m["reprobadas"]:
            print("\n   Materias con reprobaciones pendientes:")
            for mat in m["reprobadas"]:
                n = mat.get("num_intentos", 1)
                print(f"      • [{mat['clave']}] {mat['nombre']}"
                      f"  Cal: {mat['calificacion'] or 'SD'}"
                      f"  Ciclo: {mat.get('calendario','?')}"
                      f"{'  (' + str(n) + ' intentos)' if n > 1 else ''}")

        if res["areas"]:
            print("\n📊 Créditos por área:")
            print(f"   {'Área':<35} {'Req':>5} {'Adq':>5} {'Falt':>5}")
            print("   " + "─" * 50)
            for ar in res["areas"]:
                print(f"   {ar['area']:<35} {ar['requeridos']:>5} "
                      f"{ar['adquiridos']:>5} {ar['faltantes']:>5}")

        if res["alertas"]:
            iconos = {"ARTICULO":"🚨","CREDITO_ERROR":"🔴","SERVICIO":"🟢","PRACTICAS":"🔵"}
            print(f"\n⚠️  Alertas ({len(res['alertas'])}):")
            for al in res["alertas"]:
                print(f"   {iconos.get(al['tipo'],'⚠️')} [{al['tipo']}]  {al['descripcion']}")
        else:
            print("\n✅ Sin alertas académicas.")

        print(f"\n{sep}\n")

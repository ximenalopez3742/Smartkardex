"""
horario_extractor.py — Extracción del PDF de Horario UDG (versión corregida)
=============================================================================
Estrategia corregida:
  1. PRIMERO intenta leer la tabla del PDF con pdfplumber (extract_tables).
     La tabla tiene columnas: Centro | NRC | CVE | Materia | Sec | CR | ...
     Los nombres partidos en varias filas se fusionan mirando si la fila
     siguiente tiene NRC/CVE vacíos (es continuación del nombre).
  2. Si la tabla no produce resultados (PDF no tabular), cae al método de
     texto plano con regex como respaldo.

Formato real del horario CULAGOS (confirmado con horario_Jazmin.pdf):
  Col 0: Centro  (CULAGOS)
  Col 1: NRC     (182961)
  Col 2: CVE     (IE043)
  Col 3: Materia (puede partirse en múltiples filas)
  Col 4: Sec     (U01)
  Col 5: CR      (8)
  Col 6+: días, edificio, aula, horario, inicio, fin, profesor
"""

import re
import logging
from pathlib import Path

import pdfplumber

log = logging.getLogger("kardex")

# ─── Prefijos de centros UDG ────────────────────────────────────
_CENTROS = (
    "CULAGOS", "CUALTOS", "CUCOSTA", "CUCSH", "CUCS", "CUAAD",
    "CUCEI", "CUCEA", "CUNORTE", "CUSUR", "CUVALLES", "CUCIENEGA", "UDEG",
)
_RE_CENTRO = re.compile(
    r"^(" + "|".join(_CENTROS) + r")", re.IGNORECASE
)
_RE_CVE = re.compile(r"^[A-Z]{2,3}\d{3,5}$", re.IGNORECASE)
_RE_NRC = re.compile(r"^\d{5,6}$")


def _limpiar(texto) -> str:
    if texto is None:
        return ""
    return re.sub(r"\s+", " ", str(texto)).strip()


# ══════════════════════════════════════════════════════════════════
# Estrategia 1: extracción por tabla (pdfplumber)
# ══════════════════════════════════════════════════════════════════

def _extraer_por_tabla(pdf) -> list:
    """
    Recorre todas las tablas del PDF buscando la de materias.
    Fusiona filas de continuación (nombre partido en múltiples filas).
    Retorna lista de dicts {nrc, clave, nombre}.
    """
    materias = {}

    for page in pdf.pages:
        for tabla in page.extract_tables():
            if not tabla:
                continue

            # Detectar si es la tabla de materias buscando una fila con
            # un prefijo de centro en la primera columna
            es_tabla_materias = any(
                _RE_CENTRO.match(_limpiar(fila[0] if fila else ""))
                for fila in tabla
            )
            if not es_tabla_materias:
                continue

            ultima_clave = None  # para acumular nombre partido

            for fila in tabla:
                if not fila or len(fila) < 4:
                    continue

                col0 = _limpiar(fila[0])  # Centro
                col1 = _limpiar(fila[1])  # NRC
                col2 = _limpiar(fila[2])  # CVE
                col3 = _limpiar(fila[3])  # Materia (parte 1 o continuación)

                # ── Fila de encabezado ──
                if col1.upper() in ("NRC", "CRN") or col2.upper() == "CVE":
                    continue

                # ── Fila con centro + NRC + CVE válidos → nueva materia ──
                if _RE_CENTRO.match(col0) and _RE_NRC.match(col1) and _RE_CVE.match(col2):
                    nrc   = col1
                    clave = col2.upper()
                    nombre = col3  # puede estar incompleto

                    if clave not in materias:
                        materias[clave] = {"nrc": nrc, "clave": clave, "nombre": nombre}
                    ultima_clave = clave
                    log.debug("  Nueva materia: [%s] %s (NRC %s)", clave, nombre, nrc)

                # ── Fila de continuación: Centro vacío, NRC vacío, CVE vacío
                #    pero col3 tiene texto → es el resto del nombre ──
                elif (
                    ultima_clave
                    and not col1          # NRC vacío
                    and not _RE_CVE.match(col2)  # CVE vacío o texto del nombre
                    and col3              # tiene más texto
                ):
                    # El nombre partido puede venir en col2 o col3
                    fragmento = " ".join(
                        p for p in [col2, col3] if p and not _RE_CENTRO.match(p)
                    ).strip()
                    if fragmento and ultima_clave in materias:
                        nombre_actual = materias[ultima_clave]["nombre"]
                        # Solo concatenar si el fragmento no está ya incluido
                        if fragmento not in nombre_actual:
                            materias[ultima_clave]["nombre"] = (
                                nombre_actual + " " + fragmento
                            ).strip()
                            log.debug(
                                "  Nombre completado: [%s] %s",
                                ultima_clave, materias[ultima_clave]["nombre"],
                            )

                # ── Fila de horario adicional (mismo Centro, NRC vacío, CVE vacío)
                #    → solo cambia días/aula, no es nombre nuevo; ignorar ──
                elif _RE_CENTRO.match(col0) and not col1 and not col2:
                    pass  # fila de horario secundario, la ignoramos

    return list(materias.values())


# ══════════════════════════════════════════════════════════════════
# Estrategia 2: texto plano + regex (respaldo)
# ══════════════════════════════════════════════════════════════════

_NO_CONTINUACION = re.compile(
    r"^(Centro|NRC|Nota|Para|su\s|Total|Pagina|Página|UNIVERSIDAD|Horario|"
    r"Situacion|Situación|Carrera|Codigo|Código|Nivel|Nombre|Admision|Admisión|Sede)",
    re.IGNORECASE,
)
_PAT_MATERIA_TEXTO = re.compile(
    r"(?:" + "|".join(_CENTROS) + r")\s+"
    r"(\d{5,6})\s+"
    r"([A-Z]{2,3}\d{3,5})\s+"
    r"(.+?)\s+"
    r"(U\d{2})\s+\d+",
    re.IGNORECASE,
)


def _extraer_por_texto(pdf) -> list:
    """Respaldo: texto plano con unión de líneas y regex."""
    full_text = ""
    for page in pdf.pages:
        full_text += (page.extract_text() or "") + "\n"

    lineas_raw = [l.strip() for l in full_text.split("\n") if l.strip()]
    lineas_unidas = []
    for linea in lineas_raw:
        if _RE_CENTRO.match(linea):
            lineas_unidas.append(linea)
        elif _NO_CONTINUACION.match(linea):
            lineas_unidas.append(linea)
        elif lineas_unidas:
            lineas_unidas[-1] += " " + linea
        else:
            lineas_unidas.append(linea)

    materias = {}
    for linea in lineas_unidas:
        m = _PAT_MATERIA_TEXTO.search(linea)
        if not m:
            continue
        nrc   = m.group(1).strip()
        clave = m.group(2).strip().upper()
        nombre = _limpiar(m.group(3))
        if clave not in materias:
            materias[clave] = {"nrc": nrc, "clave": clave, "nombre": nombre}

    return list(materias.values())


# ══════════════════════════════════════════════════════════════════
# Función pública principal
# ══════════════════════════════════════════════════════════════════

def extraer_horario(pdf_path: str) -> dict:
    """
    Lee el PDF de horario UDG y retorna:
      {
        "codigo": str,
        "nombre": str,
        "ciclo":  str,
        "materias": [{"nrc": str, "clave": str, "nombre": str}, ...]
      }
    """
    if not Path(pdf_path).exists():
        log.error("Horario no encontrado: %s", pdf_path)
        return {}

    log.info("Leyendo horario: %s", pdf_path)
    alumno: dict = {}

    with pdfplumber.open(pdf_path) as pdf:
        # ── Datos del alumno (desde texto plano) ──
        full_text = "\n".join(
            page.extract_text() or "" for page in pdf.pages
        )

        m = re.search(r"C[oó]digo[:\s]+(\d{6,12})", full_text, re.IGNORECASE)
        if m:
            alumno["codigo"] = m.group(1).strip()

        m = re.search(
            r"Nombre[:\s]+([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑa-záéíóúñ ]+?)(?:\n|Nivel|Admisi)",
            full_text, re.IGNORECASE,
        )
        if m:
            alumno["nombre"] = _limpiar(m.group(1))

        m = re.search(r"ciclo\s+(\d{4}[AB])", full_text, re.IGNORECASE)
        if m:
            alumno["ciclo"] = m.group(1).upper()

        # ── Materias: estrategia 1 (tabla) ──
        materias = _extraer_por_tabla(pdf)

        # ── Materias: estrategia 2 (texto) si la tabla no dio nada ──
        if not materias:
            log.warning("Tabla vacía, usando extracción por texto plano.")
            materias = _extraer_por_texto(pdf)

    # Limpiar nombres
    for mat in materias:
        mat["nombre"] = _limpiar(mat["nombre"])
        log.info("  Inscrita: [%s] %s (NRC %s)", mat["clave"], mat["nombre"], mat["nrc"])

    resultado = {**alumno, "materias": materias}
    log.info(
        "Horario listo: %d materia(s) — %s",
        len(materias), alumno.get("nombre", "?"),
    )
    return resultado


def claves_en_horario(pdf_path: str) -> set:
    """Devuelve el set de claves CVE actualmente inscritas. Para filtrado rápido."""
    datos = extraer_horario(pdf_path)
    return {m["clave"] for m in datos.get("materias", [])}

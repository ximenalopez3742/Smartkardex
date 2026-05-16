"""
horario_extractor.py — Extracción del PDF de Horario UDG
=========================================================
Extrae las claves (CVE) y nombres de materias que el alumno
tiene ACTUALMENTE inscritas, para excluirlas de las sugerencias
del motor de inferencia.

Estrategia:
  1. Texto plano página por página.
  2. Líneas de continuación (nombre partido) se concatenan a la anterior.
  3. Regex captura NRC, CVE y Nombre de cada materia.
  La clave (CVE) es la pieza crítica — siempre aparece completa.
"""

import re
import logging
from pathlib import Path

import pdfplumber

log = logging.getLogger("kardex")

_PREFIJOS_CENTRO = re.compile(
    r"^(CULAGOS|CUALTOS|CUCOSTA|CUCSH|CUCS|CUAAD|CUCEI|CUCEA|"
    r"CUNORTE|CUSUR|CUVALLES|CUCIENEGA|UDEG)",
    re.IGNORECASE,
)
_NO_CONTINUACION = re.compile(
    r"^(Centro|NRC|Nota|Para|su\s|Total|Pagina|Página|UNIVERSIDAD|Horario|"
    r"Situacion|Situación|Carrera|Codigo|Código|Nivel|Nombre|Admision|Admisión|Sede)",
    re.IGNORECASE,
)
_PAT_MATERIA = re.compile(
    r"(?:CULAGOS|CUALTOS|CUCOSTA|CUCSH|CUCS|CUAAD|CUCEI|CUCEA|"
    r"CUNORTE|CUSUR|CUVALLES|CUCIENEGA|UDEG)\s+"
    r"(\d{5,6})\s+"
    r"([A-Z]{2,3}\d{3,5})\s+"
    r"(.+?)\s+"
    r"(U\d{2})\s+\d+",
    re.IGNORECASE,
)


def _limpiar(texto: str) -> str:
    return re.sub(r"\s+", " ", texto).strip()


def extraer_horario(pdf_path: str) -> dict:
    """
    Lee el PDF de horario y retorna:
      {
        "codigo": str, "nombre": str, "ciclo": str,
        "materias": [{"nrc": str, "clave": str, "nombre": str}, ...]
      }
    """
    if not Path(pdf_path).exists():
        log.error("Horario no encontrado: %s", pdf_path)
        return {}

    log.info("Leyendo horario: %s", pdf_path)
    alumno: dict = {}
    materias: dict = {}
    full_text = ""

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            full_text += (page.extract_text() or "") + "\n"

    # Datos del alumno
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

    # Unir líneas de continuación
    lineas_raw = [l.strip() for l in full_text.split("\n") if l.strip()]
    lineas_unidas = []
    for linea in lineas_raw:
        if _PREFIJOS_CENTRO.match(linea):
            lineas_unidas.append(linea)
        elif _NO_CONTINUACION.match(linea):
            lineas_unidas.append(linea)
        elif lineas_unidas:
            lineas_unidas[-1] += " " + linea
        else:
            lineas_unidas.append(linea)

    # Extraer materias
    for linea in lineas_unidas:
        match = _PAT_MATERIA.search(linea)
        if not match:
            continue
        nrc   = match.group(1).strip()
        clave = match.group(2).strip().upper()
        nombre = _limpiar(match.group(3))
        if clave not in materias:
            materias[clave] = {"nrc": nrc, "clave": clave, "nombre": nombre}
            log.info("  Inscrita: [%s] %s (NRC %s)", clave, nombre, nrc)

    resultado = {**alumno, "materias": list(materias.values())}
    log.info("Horario listo: %d materia(s) — %s", len(resultado["materias"]), alumno.get("nombre", "?"))
    return resultado


def claves_en_horario(pdf_path: str) -> set:
    """Devuelve el set de claves CVE actualmente inscritas. Para filtrado rápido."""
    datos = extraer_horario(pdf_path)
    return {m["clave"] for m in datos.get("materias", [])}

"""
extractor_horario.py — Extracción del PDF de Horario UDG (versión web)
=======================================================================
Adaptado de horario_extractor.py para el servidor Flask.

app.py espera:
    from extractor_horario import extraer_horario_pdf

    datos = extraer_horario_pdf(pdf_path)
    # datos = {
    #   "codigo":    str,
    #   "nombre":    str,
    #   "calendario": str,          ← ciclo 2026A / 2025B etc.
    #   "materias":  [{"nrc": str, "clave": str, "nombre": str, "creditos": int}, ...]
    # }
"""

import re
import logging
from pathlib import Path

import pdfplumber

log = logging.getLogger("kardex")

# ── Patrones ──────────────────────────────────────────────────────────────────

_PREFIJOS_CENTRO = re.compile(
    r"^(CULAGOS|CUALTOS|CUCOSTA|CUCSH|CUCS|CUAAD|CUCEI|CUCEA|"
    r"CUNORTE|CUSUR|CUVALLES|CUCIENEGA|UDEG)",
    re.IGNORECASE,
)

_NO_CONTINUACION = re.compile(
    r"^(Centro|NRC|Nota|Para|su\s|Total|Pagina|P[áa]gina|UNIVERSIDAD|Horario|"
    r"Situaci[oó]n|Carrera|C[oó]digo|Nivel|Nombre|Admisi[oó]n|Sede|DATOS)",
    re.IGNORECASE,
)

# Captura: NRC(5-6d)  CVE(letras+nums)  Nombre  Sección  Créditos
_PAT_MATERIA = re.compile(
    r"(?:CULAGOS|CUALTOS|CUCOSTA|CUCSH|CUCS|CUAAD|CUCEI|CUCEA|"
    r"CUNORTE|CUSUR|CUVALLES|CUCIENEGA|UDEG)\s+"
    r"(\d{5,6})\s+"          # NRC
    r"([A-Z]{2,3}\d{3,5})\s+"  # CVE / Clave
    r"(.+?)\s+"              # Nombre materia
    r"(U\d{2})\s+"           # Sección
    r"(\d+)",                # Créditos
    re.IGNORECASE,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _limpiar(texto: str) -> str:
    return re.sub(r"\s+", " ", texto).strip()


def _unir_lineas(full_text: str) -> list[str]:
    """
    Une líneas de continuación (nombre de materia partido en varias líneas).
    Regla: si una línea NO empieza con prefijo de centro NI con palabras
    reservadas de encabezado, se pega a la línea anterior.
    """
    lineas_raw = [l.strip() for l in full_text.split("\n") if l.strip()]
    resultado = []
    for linea in lineas_raw:
        if _PREFIJOS_CENTRO.match(linea):
            resultado.append(linea)
        elif _NO_CONTINUACION.match(linea):
            resultado.append(linea)
        elif resultado:
            resultado[-1] += " " + linea
        else:
            resultado.append(linea)
    return resultado


# ── Función principal (interfaz pública) ──────────────────────────────────────

def extraer_horario_pdf(pdf_path: str) -> dict:
    """
    Lee el PDF de horario UDG y retorna un dict con:
      {
        "codigo":     str,   # Código del alumno
        "nombre":     str,   # Nombre completo
        "calendario": str,   # Ciclo p.ej. "2026A"
        "materias":   list,  # [{"nrc", "clave", "nombre", "creditos"}, ...]
      }

    Es la función que importa app.py.
    """
    if not Path(pdf_path).exists():
        log.error("Horario PDF no encontrado: %s", pdf_path)
        return {"codigo": "", "nombre": "", "calendario": "", "materias": []}

    log.info("Extrayendo horario: %s", pdf_path)

    full_text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            full_text += (page.extract_text() or "") + "\n"

    # ── Datos del alumno ──────────────────────────────────────────────────────
    codigo, nombre, calendario = "", "", ""

    m = re.search(r"C[oó]digo[:\s]+(\d{6,12})", full_text, re.IGNORECASE)
    if m:
        codigo = m.group(1).strip()

    m = re.search(
        r"Nombre[:\s]+([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑa-záéíóúñ ]+?)(?:\n|Nivel|Admisi)",
        full_text, re.IGNORECASE,
    )
    if m:
        nombre = _limpiar(m.group(1))

    # Ciclo: "ciclo 2026A" o "para el ciclo 2026A"
    m = re.search(r"ciclo\s+(\d{4}[AB])", full_text, re.IGNORECASE)
    if m:
        calendario = m.group(1).upper()

    # ── Extracción de materias ────────────────────────────────────────────────
    lineas = _unir_lineas(full_text)
    materias: dict[str, dict] = {}          # clave → materia (dedup)

    for linea in lineas:
        match = _PAT_MATERIA.search(linea)
        if not match:
            continue
        nrc      = match.group(1).strip()
        clave    = match.group(2).strip().upper()
        nombre_m = _limpiar(match.group(3))
        creditos = int(match.group(5)) if match.group(5).isdigit() else 0

        if clave not in materias:
            materias[clave] = {
                "nrc":      nrc,
                "clave":    clave,
                "nombre":   nombre_m,
                "creditos": creditos,
            }
            log.info("  Materia: [%s] %s  NRC=%s  CR=%s", clave, nombre_m, nrc, creditos)

    lista_materias = list(materias.values())
    log.info(
        "Horario listo: %d materia(s) | alumno=%s | ciclo=%s",
        len(lista_materias), nombre or codigo, calendario,
    )

    return {
        "codigo":     codigo,
        "nombre":     nombre,
        "calendario": calendario,
        "materias":   lista_materias,
    }


# ── Utilidad de filtrado rápido (conservada del CLI) ──────────────────────────

def claves_en_horario(pdf_path: str) -> set:
    """Devuelve el set de claves CVE actualmente inscritas. Para filtrado rápido."""
    datos = extraer_horario_pdf(pdf_path)
    return {m["clave"] for m in datos.get("materias", [])}

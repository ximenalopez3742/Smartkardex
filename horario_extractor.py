"""
horario_extractor.py — Extracción del PDF de Horario UDG CULAGOS
=================================================================
Calibrado con coordenadas reales del PDF (horario_Jazmin.pdf).

Columnas reales (x0 aproximado):
  ~12   → Centro    (CULAGOS)
  ~53   → NRC       (182961)
  ~85   → CVE       (IE043)
  ~112  → Materia   (puede partir en 2-3 líneas)
  ~198  → Sec       (U01)
  ~223  → CR        (8)
  ~238-318 → Días   (L M I J V S D)
  ~332  → Edificio  (UELC, UELL)
  ~372  → Aula      (C203, L301, C204)
  ~398  → Horario   (1200-1355)
  ~439  → Inicio    (19-01-2026)
  ~484  → Fin       (29-05-2026)
  ~531  → Profesor
"""

import re
import logging
from collections import defaultdict
from pathlib import Path

import pdfplumber

log = logging.getLogger("kardex")

# ── Rangos de columnas (x0_min, x0_max) ─────────────────────────
X_CENTRO  = (8,   45)
X_NRC     = (45,  80)
X_CVE     = (80,  108)
X_MATERIA = (108, 195)

# Palabras que NO son parte del nombre aunque estén en x0≈108-195
# (palabras del pie de página del horario UDG)
_STOPWORDS_PIE = {
    "NOTA:", "NOTA", "PARA", "SU", "MAYOR", "INFORMACIÓN", "INFORMACION",
    "FAVOR", "DE", "PONERSE", "EN", "CONTACTO", "CON", "EL", "COORDINADOR",
    "CARRERA", "O", "CONTROL", "ESCOLAR.", "ESCOLAR",
}

_RE_CVE    = re.compile(r"^[A-Z]{2,3}\d{3,5}$", re.IGNORECASE)
_RE_NRC    = re.compile(r"^\d{5,6}$")
_RE_HORA   = re.compile(r"^\d{4}-\d{4}$")
_RE_FECH   = re.compile(r"^\d{2}-\d{2}-\d{4}$")
_RE_CENTRO = re.compile(
    r"^(CULAGOS|CUALTOS|CUCOSTA|CUCSH|CUCS|CUAAD|"
    r"CUCEI|CUCEA|CUNORTE|CUSUR|CUVALLES|CUCIENEGA|UDEG)$",
    re.IGNORECASE,
)

# Y máxima de la tabla (debajo de esto empieza el pie de página)
# El PDF de Jazmín tiene la tabla hasta top≈530, el pie arranca en ~560
Y_MAX_TABLA = 545


def _limpiar_nombre(texto: str) -> str:
    """Limpia el nombre: espacios dobles, quita espacio antes de S final partido."""
    texto = re.sub(r"\s+", " ", texto).strip()
    # Caso "TELECOMUNICACIONE S" → "TELECOMUNICACIONES"
    texto = re.sub(r"([A-ZÁÉÍÓÚÑ]{4,})\s+([A-Z])$", lambda m: m.group(1) + m.group(2), texto)
    return texto


def _en_rango(x: float, rango: tuple) -> bool:
    return rango[0] <= x < rango[1]


def _palabras_en_banda(palabras: list, x_rango: tuple) -> list:
    return [w["text"] for w in palabras if _en_rango(w["x0"], x_rango)]


def _extraer_por_palabras(pdf) -> list:
    materias: dict = {}
    ultima_clave: str = None

    for page in pdf.pages:
        words = page.extract_words(x_tolerance=3, y_tolerance=3)
        if not words:
            continue

        # Agrupar por banda Y (±4pt)
        bandas: dict = defaultdict(list)
        for w in words:
            y_key = round(w["top"] / 4) * 4
            bandas[y_key].append(w)

        for y_key in sorted(bandas.keys()):
            # Ignorar filas fuera de la tabla (pie de página)
            if y_key > Y_MAX_TABLA:
                continue

            fila = sorted(bandas[y_key], key=lambda w: w["x0"])

            centro_words  = _palabras_en_banda(fila, X_CENTRO)
            nrc_words     = _palabras_en_banda(fila, X_NRC)
            cve_words     = _palabras_en_banda(fila, X_CVE)
            materia_words = _palabras_en_banda(fila, X_MATERIA)

            tiene_centro = any(_RE_CENTRO.match(t) for t in centro_words)
            nrc_val      = next((t for t in nrc_words if _RE_NRC.match(t)), "")
            cve_val      = next((t for t in cve_words if _RE_CVE.match(t)), "")
            tiene_hora   = any(
                _RE_HORA.match(w["text"]) or _RE_FECH.match(w["text"])
                for w in fila
            )

            # ── Fila principal: Centro + NRC + CVE ───────────────
            if tiene_centro and nrc_val and cve_val:
                clave  = cve_val.upper()
                nombre = " ".join(materia_words).strip()

                if clave not in materias:
                    materias[clave] = {
                        "nrc":    nrc_val,
                        "clave":  clave,
                        "nombre": nombre,
                    }
                ultima_clave = clave
                log.debug("Materia: [%s] '%s' NRC=%s", clave, nombre, nrc_val)
                continue

            # ── Fila de continuación de nombre ───────────────────
            # Hay palabras en la columna de materia, sin hora/fecha,
            # sin NRC ni CVE, y las palabras no son del pie de página
            if (
                ultima_clave
                and materia_words
                and not tiene_hora
                and not nrc_val
                and not cve_val
            ):
                # Filtrar stopwords del pie de página
                fragmento_tokens = [
                    t for t in materia_words
                    if t.upper() not in _STOPWORDS_PIE
                ]
                fragmento = " ".join(fragmento_tokens).strip()

                if fragmento and ultima_clave in materias:
                    actual = materias[ultima_clave]["nombre"]
                    if fragmento not in actual:
                        materias[ultima_clave]["nombre"] = (
                            actual + " " + fragmento
                        ).strip()
                        log.debug(
                            "  Nombre ampliado [%s]: '%s'",
                            ultima_clave, materias[ultima_clave]["nombre"],
                        )

    return list(materias.values())


def _extraer_datos_alumno(full_text: str) -> dict:
    alumno = {}

    m = re.search(r"C[oó]digo\s*:?\s*(\d{6,12})", full_text, re.IGNORECASE)
    if m:
        alumno["codigo"] = m.group(1).strip()

    m = re.search(
        r"Nombre\s*:?\s*([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s]+?)"
        r"(?:\n|Nivel|Admisi[oó]n|Situaci[oó]n|$)",
        full_text, re.IGNORECASE,
    )
    if m:
        alumno["nombre"] = re.sub(r"\s+", " ", m.group(1)).strip()

    m = re.search(r"ciclo\s*:?\s*(\d{4}[AB])", full_text, re.IGNORECASE)
    if m:
        alumno["ciclo"] = m.group(1).upper()

    return alumno


def extraer_horario(pdf_path: str) -> dict:
    """
    Lee el PDF de horario UDG y retorna:
      {
        "codigo":   str,
        "nombre":   str,
        "ciclo":    str,
        "materias": [{"nrc": str, "clave": str, "nombre": str}, ...]
      }
    """
    if not Path(pdf_path).exists():
        log.error("Horario no encontrado: %s", pdf_path)
        return {}

    log.info("Leyendo horario: %s", pdf_path)

    with pdfplumber.open(pdf_path) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        alumno    = _extraer_datos_alumno(full_text)
        materias  = _extraer_por_palabras(pdf)

    # Limpiar nombres finales
    for mat in materias:
        mat["nombre"] = _limpiar_nombre(mat["nombre"])
        log.info("  Inscrita: [%s] %s (NRC %s)", mat["clave"], mat["nombre"], mat["nrc"])

    resultado = {**alumno, "materias": materias}
    log.info(
        "Horario listo: %d materia(s) — %s — ciclo %s",
        len(materias), alumno.get("nombre", "?"), alumno.get("ciclo", "?"),
    )
    return resultado


def claves_en_horario(pdf_path: str) -> set:
    """Devuelve el set de claves CVE actualmente inscritas."""
    datos = extraer_horario(pdf_path)
    return {m["clave"] for m in datos.get("materias", [])}

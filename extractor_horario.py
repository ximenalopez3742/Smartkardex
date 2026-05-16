"""
extractor_horario.py — Extracción del PDF de Horario UDG (versión web)
=======================================================================
El PDF de SIIAUESCOLAR parte el nombre de la materia entre 2-3 líneas:
  Línea A: CULAGOS NRC CVE [NOMBRE_P1] U01 CR dias edificio aula ...
  Línea B: [NOMBRE_P2] apellido_profesor ...
  Línea C: [SUFIJO_1-3_letras] (solo si la palabra fue partida por el PDF)

Interfaz pública:
    from extractor_horario import extraer_horario_pdf
"""

import re
import logging
from pathlib import Path
import pdfplumber

log = logging.getLogger("kardex")

_CENTROS = (
    "CULAGOS","CUALTOS","CUCOSTA","CUCSH","CUCS","CUAAD",
    "CUCEI","CUCEA","CUNORTE","CUSUR","CUVALLES","CUCIENEGA","UDEG",
)

_PAT_PRINCIPAL = re.compile(
    r"^(?:" + "|".join(_CENTROS) + r")\s+"
    r"(\d{5,6})\s+"
    r"([A-Z]{2,3}\d{3,5})\s+"
    r"(.+?)\s+"
    r"(U\d{2})\s+"
    r"(\d+)",
    re.IGNORECASE,
)

# Días de semana válidos — estas letras solas NO son sufijo de nombre
_DIAS = {"L", "M", "I", "J", "V", "S", "D"}

_RE_DIA_EDIFICIO = re.compile(
    r"^(?:[LMIJVSD]\s+(?:UELC|UELL|[A-Z]{3,})|UELC|UELL)",
    re.IGNORECASE,
)


def _limpiar(t: str) -> str:
    return re.sub(r"\s+", " ", str(t or "")).strip()


def _extraer_datos_alumno(full_text: str):
    codigo = nombre = calendario = ""
    m = re.search(r"C[oó]digo[:\s]+(\d{6,12})", full_text, re.IGNORECASE)
    if m:
        codigo = m.group(1).strip()
    m = re.search(
        r"Nombre[:\s]+([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑa-záéíóúñ ]+?)(?:\n|Nivel|Admisi)",
        full_text, re.IGNORECASE,
    )
    if m:
        nombre = _limpiar(m.group(1))
    m = re.search(r"ciclo\s+(\d{4}[AB])", full_text, re.IGNORECASE)
    if m:
        calendario = m.group(1).upper()
    return codigo, nombre, calendario


def _primera_palabra_nombre(linea: str) -> str | None:
    """
    Devuelve la primera palabra si es parte de un nombre de materia.
    Descarta: líneas de día/edificio, días sueltos, líneas vacías.
    """
    if not linea.strip():
        return None
    if _RE_DIA_EDIFICIO.match(linea):
        return None
    primera = linea.split()[0]
    # Días sueltos (1 letra) → no son sufijo de nombre
    if primera.upper() in _DIAS and len(primera) == 1:
        return None
    if re.match(r"^[A-ZÁÉÍÓÚÑ]+$", primera, re.IGNORECASE):
        return primera
    return None


def _es_sufijo_partido(token: str) -> bool:
    """
    True si el token es un fragmento partido por el PDF
    (1-3 letras que NO son día de semana suelto).
    Ej: "S" de "TELECOMUNICACIONES" → True solo si no es día.
    Usamos heurística: si es 1 letra y es un día conocido → False.
    """
    if not token or not re.match(r"^[A-ZÁÉÍÓÚÑ]{1,3}$", token, re.IGNORECASE):
        return False
    # S sola puede ser sufijo de plural si viene después de vocal
    # Solo excluir como día si NO viene precedido de vocal al final de parte_b
    # (esta lógica se maneja en el llamador con el contexto de parte_b)
    if len(token) == 1 and token.upper() in _DIAS and token.upper() != "S":
        return False
    return True


def extraer_horario_pdf(pdf_path: str) -> dict:
    if not Path(pdf_path).exists():
        log.error("Horario PDF no encontrado: %s", pdf_path)
        return {"codigo": "", "nombre": "", "calendario": "", "materias": []}

    full_text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            full_text += (page.extract_text() or "") + "\n"

    codigo, nombre, calendario = _extraer_datos_alumno(full_text)
    lineas = [l.rstrip() for l in full_text.split("\n")]
    materias: dict[str, dict] = {}

    for idx, linea in enumerate(lineas):
        m = _PAT_PRINCIPAL.match(linea)
        if not m:
            continue

        nrc      = m.group(1).strip()
        cve      = m.group(2).strip().upper()
        nombre_m = _limpiar(m.group(3))
        creditos = int(m.group(5)) if m.group(5).isdigit() else 0

        # Línea B: puede tener más palabras del nombre
        if idx + 1 < len(lineas):
            parte_b = _primera_palabra_nombre(lineas[idx + 1])
            if parte_b:
                nombre_m = nombre_m + " " + parte_b

                # Línea C: solo si parte_b fue cortada (sufijo 1-3 letras, no día)
                if idx + 2 < len(lineas):
                    linea_c = lineas[idx + 2]
                    tok_c = linea_c.split()[0] if linea_c.split() else ""
                    if _es_sufijo_partido(tok_c):
                        nombre_m = nombre_m + tok_c   # pegar sin espacio

        materias[cve] = {
            "nrc":      nrc,
            "clave":    cve,
            "nombre":   nombre_m,
            "creditos": creditos,
        }
        log.info("  %s | %s | CR=%s", cve, nombre_m, creditos)

    lista = list(materias.values())
    log.info("Horario listo: %d materias | %s | %s", len(lista), nombre or codigo, calendario)
    return {"codigo": codigo, "nombre": nombre, "calendario": calendario, "materias": lista}


def claves_en_horario(pdf_path: str) -> set:
    datos = extraer_horario_pdf(pdf_path)
    return {m["clave"] for m in datos.get("materias", [])}

















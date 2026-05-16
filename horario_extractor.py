"""
horario_extractor.py — Extracción del PDF de Horario UDG (CULAGOS)
===================================================================
Estrategia:
  1. Extracción por PALABRAS con coordenadas (pdfplumber extract_words).
     Es la más robusta para este PDF porque las celdas de la tabla tienen
     anchos variables según los días marcados (L/M/I/J/V/S/D), por lo que
     extract_tables() produce columnas inconsistentes entre filas.
     Con las palabras y sus coordenadas x0 reconstruimos las columnas
     manualmente, agrupando por bandas horizontales (una banda = una fila).

  2. Si la estrategia de palabras no produce resultados, cae al método de
     texto plano con regex como respaldo.

Columnas reales del horario CULAGOS (horario_Jazmin.pdf):
  Centro | NRC | CVE | Materia | Sec | CR | L | M | I | J | V | S | D
  | Edificio | Aula | Horario | Inicio | Fin | Profesor

El problema principal: "Materia" puede ocupar 2-3 filas lógicas
(nombre partido), y las filas de horario adicional comparten NRC/CVE
vacíos con las filas de continuación de nombre.

La clave para distinguirlas:
  - Fila de nombre-continuación: col Edificio y Aula están vacías
  - Fila de horario-adicional:   col Edificio y Aula tienen texto
"""

import re
import logging
from pathlib import Path
from collections import defaultdict

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
_RE_CVE    = re.compile(r"^[A-Z]{2,3}\d{3,5}$", re.IGNORECASE)
_RE_NRC    = re.compile(r"^\d{5,6}$")
_RE_HORA   = re.compile(r"^\d{4}-\d{4}$")       # 1200-1355
_RE_FECHA  = re.compile(r"^\d{2}-\d{2}-\d{4}$") # 19-01-2026
_RE_AULA   = re.compile(r"^[A-Z]\d{3}$")        # C203, L301
_RE_EDIF   = re.compile(r"^U[A-Z]{3,4}$")       # UELC, UELL


def _limpiar(texto) -> str:
    if texto is None:
        return ""
    return re.sub(r"\s+", " ", str(texto)).strip()


# ══════════════════════════════════════════════════════════════════
# Estrategia 1: extracción por palabras con coordenadas x0
# ══════════════════════════════════════════════════════════════════

def _extraer_por_palabras(pdf) -> list:
    """
    Usa pdfplumber extract_words() para reconstruir columnas por posición x.
    Agrupa palabras de la misma línea (misma banda y0±3pt) y detecta
    las columnas por proximidad a los anchos conocidos del horario CULAGOS.

    Retorna lista de dicts {nrc, clave, nombre}.
    """
    materias = {}          # clave → dict
    ultima_clave = None

    for page in pdf.pages:
        words = page.extract_words(
            x_tolerance=3,
            y_tolerance=3,
            keep_blank_chars=False,
            use_text_flow=False,
        )
        if not words:
            continue

        # Agrupar palabras por banda y (top redondeado a 2pt)
        bandas: dict[int, list] = defaultdict(list)
        for w in words:
            y_key = round(w["top"] / 2) * 2
            bandas[y_key].append(w)

        # Ordenar bandas de arriba a abajo
        for y_key in sorted(bandas.keys()):
            fila_words = sorted(bandas[y_key], key=lambda w: w["x0"])
            textos     = [w["text"] for w in fila_words]
            xs         = [w["x0"]   for w in fila_words]

            # ── ¿Contiene un centro UDG? → puede ser fila de materia ──
            centros_en_fila = [
                i for i, t in enumerate(textos)
                if _RE_CENTRO.match(t)
            ]
            nrcs_en_fila = [
                i for i, t in enumerate(textos)
                if _RE_NRC.match(t)
            ]
            cves_en_fila = [
                i for i, t in enumerate(textos)
                if _RE_CVE.match(t)
            ]

            # ── Fila principal de materia: tiene Centro + NRC + CVE ──
            if centros_en_fila and nrcs_en_fila and cves_en_fila:
                ic  = centros_en_fila[0]
                in_ = nrcs_en_fila[0]
                iv  = cves_en_fila[0]

                nrc   = textos[in_]
                clave = textos[iv].upper()

                # Nombre: todo lo que está entre CVE y la primera
                # palabra que sea Sec (U01), un día (L/M/I/J/V/S/D
                # aislado), edificio o aula
                nombre_tokens = _extraer_nombre(textos, iv)

                if clave not in materias:
                    materias[clave] = {
                        "nrc":    nrc,
                        "clave":  clave,
                        "nombre": nombre_tokens,
                    }
                ultima_clave = clave
                log.debug("  Materia: [%s] %s (NRC %s)", clave, nombre_tokens, nrc)
                continue

            # ── Sin Centro ni NRC ni CVE → continuación de nombre
            #    SOLO si no hay datos de horario (hora/fecha/aula/edificio) ──
            if ultima_clave and not centros_en_fila and not nrcs_en_fila and not cves_en_fila:
                tiene_horario = any(
                    _RE_HORA.match(t) or _RE_FECHA.match(t)
                    or _RE_AULA.match(t) or _RE_EDIF.match(t)
                    for t in textos
                )
                if not tiene_horario:
                    # Es continuación del nombre de la materia
                    # Filtrar tokens que NO son días ni secciones
                    fragmento = " ".join(
                        t for t in textos
                        if not re.match(r"^(U\d{2}|\d+|[LMIJVSD])$", t, re.IGNORECASE)
                    ).strip()
                    if fragmento and ultima_clave in materias:
                        nombre_actual = materias[ultima_clave]["nombre"]
                        if fragmento not in nombre_actual:
                            materias[ultima_clave]["nombre"] = (
                                nombre_actual + " " + fragmento
                            ).strip()
                            log.debug(
                                "  Nombre ampliado: [%s] → %s",
                                ultima_clave, materias[ultima_clave]["nombre"],
                            )

    return list(materias.values())


def _extraer_nombre(textos: list, idx_cve: int) -> str:
    """
    Extrae el nombre de la materia a partir de los tokens que siguen
    al CVE, deteniéndose antes de Sec (U01), días sueltos o números de créditos.
    """
    tokens = []
    for t in textos[idx_cve + 1:]:
        # Parar en Sec (U01, U02...)
        if re.match(r"^U\d{2}$", t):
            break
        # Parar en un dígito suelto (créditos)
        if re.match(r"^\d{1,2}$", t):
            break
        # Parar en día aislado
        if re.match(r"^[LMIJVSD]$", t, re.IGNORECASE) and len(t) == 1:
            break
        # Parar en edificio o aula
        if _RE_EDIF.match(t) or _RE_AULA.match(t):
            break
        tokens.append(t)
    return " ".join(tokens).strip()


# ══════════════════════════════════════════════════════════════════
# Estrategia 2: extract_tables con fusión de filas (respaldo)
# ══════════════════════════════════════════════════════════════════

def _extraer_por_tabla(pdf) -> list:
    """
    Usa pdfplumber extract_tables().
    Menos confiable para este PDF (columnas variables), pero sirve
    como segundo respaldo antes del texto plano.
    """
    materias = {}
    ultima_clave = None

    for page in pdf.pages:
        for tabla in page.extract_tables():
            if not tabla:
                continue

            es_tabla_materias = any(
                _RE_CENTRO.match(_limpiar(f[0] if f else ""))
                for f in tabla
            )
            if not es_tabla_materias:
                continue

            for fila in tabla:
                if not fila or len(fila) < 4:
                    continue

                cols = [_limpiar(c) for c in fila]
                # Buscar NRC y CVE en cualquier posición de la fila
                nrc_val = next((c for c in cols if _RE_NRC.match(c)), "")
                cve_val = next((c for c in cols if _RE_CVE.match(c)), "")

                if not nrc_val or not cve_val:
                    # Posible continuación de nombre
                    if ultima_clave:
                        tiene_horario = any(
                            _RE_HORA.match(c) or _RE_FECHA.match(c) for c in cols
                        )
                        if not tiene_horario:
                            fragmento = " ".join(
                                c for c in cols
                                if c and not _RE_CENTRO.match(c)
                                and not re.match(r"^(U\d{2}|\d+|[LMIJVSD])$", c, re.IGNORECASE)
                            ).strip()
                            if fragmento and ultima_clave in materias:
                                nombre_actual = materias[ultima_clave]["nombre"]
                                if fragmento not in nombre_actual:
                                    materias[ultima_clave]["nombre"] = (
                                        nombre_actual + " " + fragmento
                                    ).strip()
                    continue

                clave  = cve_val.upper()
                # Nombre: tokens entre CVE y Sec
                idx_cve = next(i for i, c in enumerate(cols) if c == cve_val)
                nombre  = _extraer_nombre(cols, idx_cve)

                if clave not in materias:
                    materias[clave] = {"nrc": nrc_val, "clave": clave, "nombre": nombre}
                ultima_clave = clave

    return list(materias.values())


# ══════════════════════════════════════════════════════════════════
# Estrategia 3: texto plano + regex (último respaldo)
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
    """Último respaldo: texto plano con unión de líneas y regex."""
    full_text = ""
    for page in pdf.pages:
        full_text += (page.extract_text() or "") + "\n"

    lineas_raw    = [l.strip() for l in full_text.split("\n") if l.strip()]
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
        nrc    = m.group(1).strip()
        clave  = m.group(2).strip().upper()
        nombre = _limpiar(m.group(3))
        if clave not in materias:
            materias[clave] = {"nrc": nrc, "clave": clave, "nombre": nombre}

    return list(materias.values())


# ══════════════════════════════════════════════════════════════════
# Extracción de datos del alumno (código, nombre, ciclo)
# ══════════════════════════════════════════════════════════════════

def _extraer_datos_alumno(full_text: str) -> dict:
    """
    Extrae código, nombre y ciclo del texto completo del PDF.
    Maneja variaciones de formato (con/sin dos puntos, con/sin tilde).
    """
    alumno = {}

    # Código: acepta con o sin ":" y con espacios variables
    # El PDF de CULAGOS pone el código en la tabla de datos del alumno
    m = re.search(
        r"C[oó]digo\s*:?\s*(\d{6,12})",
        full_text, re.IGNORECASE
    )
    if m:
        alumno["codigo"] = m.group(1).strip()

    # Nombre: en la tabla aparece como "Nombre: JAZMIN MARISOL RAMIREZ MONTOYA"
    # Puede estar seguido de salto de línea, "Nivel", "Admisión", etc.
    m = re.search(
        r"Nombre\s*:?\s*([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s]+?)(?:\n|$|Nivel|Admisi[oó]n|Situaci[oó]n)",
        full_text, re.IGNORECASE
    )
    if m:
        alumno["nombre"] = _limpiar(m.group(1))

    # Ciclo: "ciclo 2026A" o "ciclo: 2026A"
    m = re.search(r"ciclo\s*:?\s*(\d{4}[AB])", full_text, re.IGNORECASE)
    if m:
        alumno["ciclo"] = m.group(1).upper()

    return alumno


# ══════════════════════════════════════════════════════════════════
# Función pública principal
# ══════════════════════════════════════════════════════════════════

def extraer_horario(pdf_path: str) -> dict:
    """
    Lee el PDF de horario UDG y retorna:
      {
        "codigo":   str,
        "nombre":   str,
        "ciclo":    str,
        "materias": [{"nrc": str, "clave": str, "nombre": str}, ...]
      }

    Intenta 3 estrategias en orden:
      1. Palabras con coordenadas (más robusta para CULAGOS)
      2. extract_tables con búsqueda flexible de NRC/CVE
      3. Texto plano + regex (último recurso)
    """
    if not Path(pdf_path).exists():
        log.error("Horario no encontrado: %s", pdf_path)
        return {}

    log.info("Leyendo horario: %s", pdf_path)

    with pdfplumber.open(pdf_path) as pdf:

        # Texto completo para datos del alumno
        full_text = "\n".join(
            page.extract_text() or "" for page in pdf.pages
        )
        alumno = _extraer_datos_alumno(full_text)

        # Estrategia 1: palabras con coordenadas
        materias = _extraer_por_palabras(pdf)
        if materias:
            log.info("Estrategia 1 (palabras): %d materia(s) extraída(s).", len(materias))
        else:
            log.warning("Estrategia 1 vacía, probando extract_tables...")
            # Estrategia 2: tablas con búsqueda flexible
            materias = _extraer_por_tabla(pdf)
            if materias:
                log.info("Estrategia 2 (tablas): %d materia(s) extraída(s).", len(materias))
            else:
                log.warning("Estrategia 2 vacía, usando texto plano...")
                # Estrategia 3: texto plano
                materias = _extraer_por_texto(pdf)
                log.info("Estrategia 3 (texto): %d materia(s) extraída(s).", len(materias))

    # Limpiar nombres finales
    for mat in materias:
        mat["nombre"] = _limpiar(mat["nombre"])
        log.info(
            "  Inscrita: [%s] %s (NRC %s)",
            mat["clave"], mat["nombre"], mat["nrc"]
        )

    resultado = {**alumno, "materias": materias}
    log.info(
        "Horario listo: %d materia(s) — alumno: %s — ciclo: %s",
        len(materias),
        alumno.get("nombre", "?"),
        alumno.get("ciclo",  "?"),
    )
    return resultado


def claves_en_horario(pdf_path: str) -> set:
    """Devuelve el set de claves CVE actualmente inscritas. Para filtrado rápido."""
    datos = extraer_horario(pdf_path)
    return {m["clave"] for m in datos.get("materias", [])}

"""
horario_extractor.py — Extractor de Horario SIIAU UDG
======================================================
Parsea el PDF "Horario del Estudiante" de la UDG (SIIAU) y devuelve:
  - datos del alumno (código, nombre, carrera, ciclo)
  - lista de materias con NRC, clave, nombre, créditos, días, horario,
    aula, edificio, profesor y sesiones detalladas por día.

Formato soportado: tabla de una sola página con columnas:
  Centro | NRC | CVE | Materia | Sec | CR | L M I J V S D |
  Edificio | Aula | Horario | Inicio | Fin | Profesor

Uso:
    from horario_extractor import extraer_horario_siiau
    alumno, materias = extraer_horario_siiau("horario.pdf")
"""

import re
from collections import defaultdict

# ── Rangos X de cada columna (en puntos PDF) ─────────────────────
# Medidos directamente del PDF de referencia de CULAGOS UDG.
COL_X = {
    'centro':   (0,    53),
    'nrc':      (53,   85),
    'cve':      (85,   115),
    'materia':  (110,  198),
    'sec':      (196,  222),
    'cr':       (222,  237),
    'L':        (237,  252),   # Lunes
    'M':        (248,  268),   # Martes
    'I':        (264,  282),   # Miércoles
    'J':        (280,  296),   # Jueves
    'V':        (290,  308),   # Viernes
    'S':        (305,  322),   # Sábado
    'D':        (320,  340),   # Domingo
    'edificio': (338,  375),
    'aula':     (373,  398),
    'horario':  (397,  436),
    'inicio':   (438,  480),
    'fin':      (483,  525),
    'profesor': (528,  900),
}

DIAS_MAP = {
    'L': 'Lunes',
    'M': 'Martes',
    'I': 'Miércoles',
    'J': 'Jueves',
    'V': 'Viernes',
    'S': 'Sábado',
    'D': 'Domingo',
}

# Y mínima donde empieza la tabla de materias (debajo del encabezado del alumno)
Y_TABLA_INICIO = 220
Y_TABLA_FIN    = 560


def _col_of(word: dict) -> str | None:
    """Devuelve el nombre de columna al que pertenece una palabra según su X central."""
    xc = (word['x0'] + word['x1']) / 2
    for col, (x0, x1) in COL_X.items():
        if x0 <= xc < x1:
            return col
    return None


def _dedup_profesor(texto: str) -> str:
    """Elimina repeticiones del nombre del profesor (el PDF lo repite por sesión)."""
    palabras = texto.split()
    # Si la longitud es par y la primera mitad == segunda mitad → deduplicar
    n = len(palabras)
    if n >= 4 and n % 2 == 0:
        mitad = n // 2
        if palabras[:mitad] == palabras[mitad:]:
            return ' '.join(palabras[:mitad])
    # Buscar repetición de subcadenas de al menos 3 palabras
    for size in range(n // 2, 2, -1):
        chunk = palabras[:size]
        resto = palabras[size:]
        if resto[:size] == chunk:
            return ' '.join(chunk)
    return texto


def extraer_alumno(tables: list) -> dict:
    """Extrae datos del alumno de la primera tabla del PDF (encabezado)."""
    alumno = {}
    if not tables:
        return alumno
    for row in tables[0]:
        cells = [str(c or '').strip() for c in row]
        if len(cells) < 2:
            continue
        flat = ' '.join(cells)
        # Código y Nombre
        if cells[0] == 'Código:':
            alumno['codigo'] = cells[1]
            if len(cells) > 3:
                alumno['nombre'] = cells[3]
        # Carrera
        if cells[0] == 'Carrera:':
            alumno['carrera'] = cells[1]
        # Ciclo (último valor que coincide con patrón 20XXA/B)
        for c in cells:
            if re.fullmatch(r'\d{4}[AB]', c):
                alumno['ciclo'] = c   # el último encontrado = ciclo actual
    return alumno


def extraer_materias(words: list) -> list[dict]:
    """
    Parsea la zona de la tabla de materias a partir de palabras con coordenadas.
    Devuelve lista de materias con todos sus campos.
    """
    # Filtrar palabras dentro de la zona de la tabla
    tabla_words = [w for w in words if Y_TABLA_INICIO < w['top'] < Y_TABLA_FIN]

    # Agrupar palabras en filas (tolerancia ±5 px en Y)
    filas_dict: dict = defaultdict(list)
    for w in tabla_words:
        y_key = round(w['top'] / 5) * 5
        filas_dict[y_key].append(w)

    # Convertir a lista de dicts columna→[textos]
    filas_parsed = []
    for y in sorted(filas_dict.keys()):
        rd: dict = defaultdict(list)
        for w in filas_dict[y]:
            c = _col_of(w)
            if c:
                rd[c].append(w['text'])
        filas_parsed.append(rd)

    # ── Ensamblar materias ────────────────────────────────────────
    materias = []
    current = None

    for rd in filas_parsed:
        nrc       = ' '.join(rd.get('nrc', []))
        cve       = ' '.join(rd.get('cve', []))
        mat_frag  = ' '.join(rd.get('materia', []))
        cr        = ' '.join(rd.get('cr', []))
        sec       = ' '.join(rd.get('sec', []))
        edi       = ' '.join(rd.get('edificio', []))
        aula      = ' '.join(rd.get('aula', []))
        hor       = ' '.join(rd.get('horario', []))
        ini       = ' '.join(rd.get('inicio', []))
        fin       = ' '.join(rd.get('fin', []))
        prof_frag = ' '.join(rd.get('profesor', []))
        dias_fila = [d for d in 'LMIJVSD' if rd.get(d)]

        # Nueva materia: fila con NRC de 5-6 dígitos
        if re.fullmatch(r'\d{5,6}', nrc):
            if current:
                materias.append(current)
            hora_ini = hor[:4] if hor else ''
            hora_fin = hor[5:] if (hor and '-' in hor) else ''
            current = {
                'nrc':        nrc,
                'clave':      cve,
                'nombre':     mat_frag,
                'seccion':    sec,
                'creditos':   int(cr) if cr.isdigit() else 0,
                'edificio':   edi,
                'aula':       aula,
                'horario':    hor,
                'hora_inicio': hora_ini,
                'hora_fin':    hora_fin,
                'inicio':     ini,
                'fin':        fin,
                'profesor':   prof_frag,
                'dias':       dias_fila,
                'sesiones':   [],
            }
            if dias_fila and hor:
                current['sesiones'].append({
                    'dias': dias_fila,
                    'aula': aula,
                    'edificio': edi,
                    'horario': hor,
                    'hora_inicio': hora_ini,
                    'hora_fin': hora_fin,
                })

        elif current is not None:
            # Continuar materia: acumular nombre multilinea y profesor
            if mat_frag:
                current['nombre'] = (current['nombre'] + ' ' + mat_frag).strip()
            if prof_frag:
                current['profesor'] = (current['profesor'] + ' ' + prof_frag).strip()
            # Nueva sesión con días/horario distintos
            if dias_fila and hor:
                hora_ini = hor[:4]
                hora_fin = hor[5:] if '-' in hor else ''
                current['sesiones'].append({
                    'dias': dias_fila,
                    'aula': aula or current.get('aula', ''),
                    'edificio': edi or current.get('edificio', ''),
                    'horario': hor,
                    'hora_inicio': hora_ini,
                    'hora_fin': hora_fin,
                })
                if not current['dias']:
                    current['dias'] = dias_fila
            # Rellenar campos vacíos
            if not current['aula'] and aula:
                current['aula'] = aula
            if not current['edificio'] and edi:
                current['edificio'] = edi
            if not current['horario'] and hor:
                current['horario'] = hor
                current['hora_inicio'] = hor[:4]
                current['hora_fin'] = hor[5:] if '-' in hor else ''

    if current:
        materias.append(current)

    # ── Post-procesar cada materia ────────────────────────────────
    for m in materias:
        # Nombre limpio (sin espacios dobles, corregir palabras cortadas)
        m['nombre'] = re.sub(r'\s+', ' ', m['nombre']).strip()
        # Corregir truncamiento SIIAU (ej: "TELECOMUNICACIONE" → "TELECOMUNICACIONES")
        m['nombre'] = re.sub(r'TELECOMUNICACIONE\b', 'TELECOMUNICACIONES', m['nombre'])

        # Deduplicar profesor
        m['profesor'] = _dedup_profesor(re.sub(r'\s+', ' ', m['profesor']).strip())

        # Unificar días de todas las sesiones
        todos_dias: set = set()
        for s in m['sesiones']:
            todos_dias.update(s['dias'])
        m['dias_str']    = ''.join(d for d in 'LMIJVSD' if d in todos_dias)
        m['dias_nombres'] = [DIAS_MAP[d] for d in 'LMIJVSD' if d in todos_dias]

    return materias


def extraer_horario_siiau(pdf_path: str) -> tuple[dict, list[dict]]:
    """
    Función principal: extrae alumno y materias del PDF de horario SIIAU.

    Args:
        pdf_path: Ruta al archivo PDF.

    Returns:
        (alumno, materias)
        alumno   → dict con codigo, nombre, carrera, ciclo
        materias → lista de dicts con todos los campos de cada materia
    """
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError(
            "Dependencia faltante: instala pdfplumber con  pip install pdfplumber"
        )

    with pdfplumber.open(pdf_path) as pdf:
        page   = pdf.pages[0]
        tables = page.extract_tables()
        words  = page.extract_words(keep_blank_chars=False, x_tolerance=3, y_tolerance=3)

    alumno   = extraer_alumno(tables)
    materias = extraer_materias(words)

    return alumno, materias


# ── Prueba rápida desde línea de comandos ─────────────────────────
if __name__ == '__main__':
    import sys, json

    pdf = sys.argv[1] if len(sys.argv) > 1 else 'horario.pdf'
    alumno, materias = extraer_horario_siiau(pdf)

    print('\n══ ALUMNO ══')
    for k, v in alumno.items():
        print(f'  {k}: {v}')

    print(f'\n══ MATERIAS ({len(materias)}) ══')
    for m in materias:
        print(f"\n  [{m['nrc']}] {m['clave']} — {m['nombre']}")
        print(f"  Créditos: {m['creditos']}  |  Días: {m['dias_str']} ({', '.join(m['dias_nombres'])})")
        print(f"  Horario:  {m['horario']}  |  Aula: {m['edificio']}-{m['aula']}")
        print(f"  Profesor: {m['profesor']}")
        if len(m['sesiones']) > 1:
            print(f"  Sesiones:")
            for s in m['sesiones']:
                print(f"    {''.join(s['dias'])} {s['horario']} {s['edificio']}-{s['aula']}")

    total_cred = sum(m['creditos'] for m in materias)
    print(f'\nTotal créditos: {total_cred}')

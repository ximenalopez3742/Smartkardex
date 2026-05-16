"""
Sistema de Kárdex UDG — Punto de entrada unificado
===================================================
Uso:
  python kardex.py cargar   <archivo.pdf>
  python kardex.py analizar <codigo_alumno>
  python kardex.py consultar <codigo_alumno>
  python kardex.py listar
  python kardex.py importar-plan [archivo.csv]

Ejemplos:
  python kardex.py cargar     mi_kardex.pdf
  python kardex.py importar-plan
  python kardex.py analizar   222937383
  python kardex.py consultar  222937383
  python kardex.py listar
"""

import sys
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        _mostrar_ayuda()
        sys.exit(0)

    cmd = sys.argv[1].lower()

    if cmd == "cargar":
        if len(sys.argv) < 3:
            print("❌ Uso: python kardex.py cargar <archivo.pdf>")
            sys.exit(1)
        from extractor import KardexExtractor
        extractor = KardexExtractor()
        extractor.cargar_pdf(sys.argv[2])

    elif cmd == "importar-plan":
        csv = sys.argv[2] if len(sys.argv) >= 3 else "Plan de Estudios IELC - Hoja 6.csv"
        from plan import importar_plan_estudios
        importar_plan_estudios(csv_path=csv)

    elif cmd == "analizar":
        if len(sys.argv) < 3:
            print("❌ Uso: python kardex.py analizar <codigo_alumno> [--horario horario.pdf]")
            sys.exit(1)
        # Buscar flag --horario
        horario_pdf = ""
        args = sys.argv[3:]
        if "--horario" in args:
            idx = args.index("--horario")
            if idx + 1 < len(args):
                horario_pdf = args[idx + 1]
                if not Path(horario_pdf).exists():
                    print(f"❌ Archivo de horario no encontrado: {horario_pdf}")
                    sys.exit(1)
            else:
                print("❌ Debes indicar el archivo PDF después de --horario")
                sys.exit(1)
        from motor import MotorInferencia
        motor = MotorInferencia()
        motor.analizar(sys.argv[2], horario_pdf=horario_pdf)

    elif cmd == "consultar":
        if len(sys.argv) < 3:
            print("❌ Uso: python kardex.py consultar <codigo_alumno>")
            sys.exit(1)
        from extractor import KardexExtractor
        extractor = KardexExtractor()
        extractor.consultar(sys.argv[2])

    elif cmd == "listar":
        from extractor import KardexExtractor
        extractor = KardexExtractor()
        extractor.listar()

    else:
        print(f"❌ Comando desconocido: '{cmd}'")
        _mostrar_ayuda()
        sys.exit(1)


def _mostrar_ayuda():
    print("""
╔══════════════════════════════════════════════════════════╗
║        Sistema de Kárdex UDG / IELC — Ayuda             ║
╚══════════════════════════════════════════════════════════╝

Comandos disponibles:

  cargar <archivo.pdf>
      Extrae un kárdex PDF y lo guarda en la base de datos.
      Si el alumno ya existe, actualiza sus datos.

  importar-plan [archivo.csv]
      Carga el plan de estudios IELC desde un CSV.
      Por defecto busca: "Plan de Estudios IELC - Hoja 6.csv"

  analizar <codigo> [--horario horario.pdf]
      Muestra materias disponibles, bloqueadas y alertas
      académicas para el alumno indicado.
      Con --horario excluye de las sugerencias las materias
      que el alumno ya tiene inscritas en el ciclo actual.

  consultar <codigo>
      Muestra el resumen completo del kárdex del alumno.

  listar
      Lista todos los alumnos registrados en la base de datos.

Flujo de trabajo recomendado:
  1. python kardex.py cargar         mi_kardex.pdf
  2. python kardex.py importar-plan
  3. python kardex.py analizar       222937383
     python kardex.py analizar       222937383 --horario mi_horario.pdf
""")


if __name__ == "__main__":
    main()

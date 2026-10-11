"""Un Excel por jefe de campaña, usando la dimensión jefe_negocio."""
from pathlib import Path
import sys

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from analytics.generacion_reportes import ejecutar

if __name__ == "__main__":
    raise SystemExit(ejecutar("jefe_negocio", base_dir=BASE_DIR))

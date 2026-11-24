"""Build the OpenHIS diagnosis catalogue from the MIMIC diagnosis dictionary."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "simulator"))
from tools.build_mimic_diagnosis_catalogue import build, main

if __name__ == "__main__":
    main()

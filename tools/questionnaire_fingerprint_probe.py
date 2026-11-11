"""Forwarder shim for probe relocated to tools/probes/questionnaire_fingerprint_probe.py."""
import runpy
from pathlib import Path

_target = Path(__file__).resolve().parent / "probes" / "questionnaire_fingerprint_probe.py"
if __name__ == "__main__":
    runpy.run_path(str(_target), run_name="__main__")

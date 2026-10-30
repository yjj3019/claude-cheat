"""Compatibility entry point for the opt-in backend probe."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modelQualityProbe import main

if __name__ == "__main__":
    raise SystemExit(main())

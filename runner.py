#!/usr/bin/env python3
"""Compatibility entry point for the Meu INSS workflow."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from inss.workflow import main


if __name__ == "__main__":
    sys.exit(main())

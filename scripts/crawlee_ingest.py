#!/usr/bin/env python3
"""Compatibility entry point for the shared Crawlee capture channel."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.capture.crawlee import main

if __name__ == "__main__":
    raise SystemExit(main())

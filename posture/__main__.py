"""Entry point for ``python -m posture``."""

from __future__ import annotations

from .service import main

if __name__ == "__main__":
    raise SystemExit(main())

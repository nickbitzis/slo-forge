"""Capture README screenshots with headless Chrome. Start the server first: make run.

Usage: uv run python scripts/screenshot.py --port 8835
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "chromium",
]
DOCS = Path(__file__).resolve().parents[1] / "docs"


def chrome() -> str:
    for c in CHROME_PATHS:
        if Path(c).exists() or shutil.which(c):
            return c
    raise SystemExit("Chrome not found")


def shoot(url: str, out: Path, width: int, height: int) -> None:
    args = [chrome(), "--headless=new", "--disable-gpu", "--hide-scrollbars",
            f"--window-size={width},{height}", "--virtual-time-budget=8000",
            f"--screenshot={out}", url]
    subprocess.run(args, check=True, capture_output=True, timeout=90)
    print(f"wrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8835)
    a = ap.parse_args()
    base = f"http://127.0.0.1:{a.port}"
    DOCS.mkdir(exist_ok=True)
    shoot(f"{base}/#/alerts", DOCS / "screenshot.png", 1400, 1250)
    shoot(f"{base}/#/", DOCS / "overview.png", 1400, 900)
    shoot(f"{base}/#/slo/checkout-api/availability/40.9/42.4", DOCS / "slo-detail.png",
          1400, 1700)


if __name__ == "__main__":
    main()

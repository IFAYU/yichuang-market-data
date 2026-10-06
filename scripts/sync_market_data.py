"""Publish the latest release (out/manifest.json) into the risk-profiler app. Usage (from risk-profiler):  npm run sync-market-data
Validates schemaVersion, hashes and required files first; on any failure the app's previous valid snapshot is left untouched."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from mitw.cli import main  # noqa: E402

DEFAULT_TARGET = ROOT.parent / "risk-profiler" / "public" / "market-data"

if __name__ == "__main__":
    args = sys.argv[1:] or ["--target", str(DEFAULT_TARGET)]
    sys.exit(main(["sync", *args]))

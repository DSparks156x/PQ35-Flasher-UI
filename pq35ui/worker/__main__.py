import sys
from pathlib import Path

# Make the repo root importable so `flasher` (the submodule) resolves even
# when the worker is started by a bare interpreter with no packages installed.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pq35ui.worker.server import main  # noqa: E402

raise SystemExit(main())

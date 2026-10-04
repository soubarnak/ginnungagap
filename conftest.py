"""Run the tests against the checkout's sources, not against an installed copy of Spaces.

Added for the Void port (void/docs/void.md): with the package installed, a plain
`python3 -m pytest` would otherwise test the installed code.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

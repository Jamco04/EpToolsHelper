"""Double-click this file to open the eptools window (needs Python 3.9+ from python.org).

No console window appears because the file ends in .pyw.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eptools.gui import main  # noqa: E402

main()

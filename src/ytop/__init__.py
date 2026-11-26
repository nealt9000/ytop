"""ytop - offline YouTube playlist preserver."""

import os

__version__ = "0.6.0"
# Set by the stick's boot.py from build.json; empty for a development copy.
__build__ = os.environ.get("YTOP_BUILD_ID", "")

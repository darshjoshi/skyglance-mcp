"""Entry point for the SkyGlance Spotter Claude plugin (see .mcp.json).

The plugin runs the same server as `pip install skyglance`; .mcp.json sets
SKYGLANCE_EDITION=directory, which src/skyglance/server.py reads at import.
"""

from skyglance.server import main

main()

"""Run the data/model/report CLI for python -m outage_explorer.

The installed outage-explorer console script targets the same cli.main function; Django
init/serve/worker commands use the separate web.local entry point.
"""

from .cli import main

raise SystemExit(main())

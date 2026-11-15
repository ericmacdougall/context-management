"""Long-running projector process (run under a wrapper that may kill it).
usage: projector_main.py <tenants comma list|all> [recipes=1] [live=1] [batch=500] [fence=1]"""
import sys
from common import *
from memsys import projector

tenants = None if sys.argv[1] == "all" else sys.argv[1].split(",")
recipes = [int(x) for x in (sys.argv[2] if len(sys.argv) > 2 else "1").split(",")]
live = int(sys.argv[3]) if len(sys.argv) > 3 else 1
batch = int(sys.argv[4]) if len(sys.argv) > 4 else 500
fence = (sys.argv[5] != "0") if len(sys.argv) > 5 else True
conn, lock, es = conns()
esx.install_templates(es)
projector.Projector(conn, lock, es, recipes=recipes, live_recipe=live, batch=batch, tenants=tenants, fence=fence).run(until_idle=False, sleep=0.02)

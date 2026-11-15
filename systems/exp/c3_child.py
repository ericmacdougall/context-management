"""Child process for the crash-between-phases test: run an erasure that kills itself at the named point."""
import sys
from common import *
from memsys import erasure

tenant, subject, point = sys.argv[1:4]
conn, lock, es = conns()
erasure.erase_subject(conn, lock, es, tenant, subject, fail_at="kill:" + point)

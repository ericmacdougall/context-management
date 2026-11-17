"""Run the OLD write path (feat/systems-skeleton a13b524, shipped read-only to WP_HOME/oldsrc) against THIS stream's own
containers only (conf pg_container and es_container: databases emlab + keys, created for this purpose, and the
stream's single-node Elasticsearch). The previous owner's containers and indices are never addressed: the port is asserted before any old
module touches a database, and the old HTTPS client is replaced by a plain-HTTP one bound to this stream's ES."""
import http.client
import os
import pathlib
import sys

from wp import config

OLD = config.HOME / "oldsrc" / "systems"
os.environ["MEMSYS_HOME"] = str(config.HOME / "oldhome")
sys.path.insert(0, str(OLD))

from memsys import db as odb  # noqa: E402

assert int((odb.HOME / "pg_port").read_text()) == config.PG_PORT, "old code must only reach this stream's Postgres"
assert int((odb.HOME / "kms_port").read_text()) == config.PG_PORT
assert config.ES_CONTAINER.startswith(config.OWN_PREFIX), "old code must only reach this stream's Elasticsearch"

from memsys import esc  # noqa: E402


class OldES(esc.ES):
    """The old client with its transport pointed at this stream's single-node ES (no credentials needed there)."""
    def __init__(self, admin=False):
        self.conn = None
        self._auth = "none"

    def _c(self):
        if self.conn is None:
            self.conn = http.client.HTTPConnection("127.0.0.1", config.ES_PORT, timeout=120)
        return self.conn


esc.ES = OldES
OLD_HEAD = (config.HOME / "oldsrc" / "OLD_HEAD").read_text().strip()


def conns():
    return odb.connect(autocommit=True), odb.connect(autocommit=True, application_name="memsys-lock"), OldES()


def kill_backend(conn_to_kill, via):
    pid = conn_to_kill.info.backend_pid
    via.execute("select pg_terminate_backend(%s)", (pid,))

"""The Sect -- a small hierarchical task-orchestration framework.

A disciple only ever needs this::

    from sect import Disciple

    d = Disciple(name="scribe", arts=["summarize"])
    d.register()
    for mission in d.poll_missions():
        ...

The server lives in :mod:`sect.core` and is installed separately via the ``core`` extra.
Importing this package never pulls in FastAPI or asyncpg.
"""

from sect.env import load_dotenv
from sect.errors import (
    AuthError,
    MissionNotClaimable,
    NotFound,
    NotMissionHolder,
    PermanentFailure,
    SectError,
    SectUnavailable,
)
from sect.models import (
    DiscipleRecord,
    DiscipleStats,
    Mission,
    MissionStatus,
    SectStats,
)
from sect.realms import REALMS, Realm, realm_rank

__version__ = "0.1.0"

__all__ = [
    "REALMS",
    "AuthError",
    "DiscipleRecord",
    "DiscipleStats",
    "Mission",
    "MissionNotClaimable",
    "MissionStatus",
    "NotFound",
    "NotMissionHolder",
    "PermanentFailure",
    "Realm",
    "SectError",
    "SectStats",
    "SectUnavailable",
    "__version__",
    "load_dotenv",
    "realm_rank",
]


def __getattr__(name: str) -> object:
    # Disciple and SectMaster live in sect.client, which imports httpx. Exposing them
    # lazily keeps `import sect` cheap and keeps the import graph honest.
    if name in ("Disciple", "SectMaster"):
        from sect import client

        return getattr(client, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

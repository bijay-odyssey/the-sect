"""Cultivation realms: the maturity tier a disciple has been granted.

A realm is metadata. Nothing in the claim path reads it -- it exists so a status
board can rank disciples by how much you trust them, and so promotion is a thing
the Sect grants rather than a thing a worker asserts about itself.

The ladder is deliberately short in v0.1. Extending it is two edits that must land
together: add the tier to ``Realm`` below, and add a migration widening the ``realm``
CHECK constraint on ``disciples``. ``tests/test_api.py::test_realms_match_database``
fails if the two ever drift apart.
"""

from typing import Literal, get_args

Realm = Literal[
    "qi-condensation",
    "foundation-establishment",
    "core-formation",
]

#: The ladder in ascending order. Index is rank.
REALMS: tuple[Realm, ...] = get_args(Realm)

#: Every disciple begins here. Not settable at registration.
STARTING_REALM: Realm = "qi-condensation"


def realm_rank(realm: str) -> int:
    """Ordinal of ``realm`` in the ladder, or ``-1`` if it is not a known realm."""
    try:
        return REALMS.index(realm)  # type: ignore[arg-type]
    except ValueError:
        return -1

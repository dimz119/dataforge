"""Model discovery for the `streams` app.

Concrete models live in `streams.domain.models` (project-folder-structure §2.1)
and are re-exported here so Django's app registry finds them.
"""

from streams.domain.models import *  # noqa: F403

"""Model discovery for the `audit` app.

Concrete models live in `audit.domain.models` (project-folder-structure §2.1)
and are re-exported here so Django's app registry finds them.
"""

from audit.domain.models import *  # noqa: F403

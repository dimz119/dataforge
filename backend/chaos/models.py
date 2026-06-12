"""Model discovery for the `chaos` app.

Concrete models live in `chaos.domain.models` (project-folder-structure §2.1)
and are re-exported here so Django's app registry finds them.
"""

from chaos.domain.models import *  # noqa: F403

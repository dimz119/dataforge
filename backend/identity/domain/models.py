"""Domain models for the Identity context.

Phase 2 replaces this placeholder with the full Identity user model
(database-schema §3.1) and the verification/reset token model (§3.2).
"""

import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Custom user placeholder set as AUTH_USER_MODEL before the first migration
    (phase-01 scope) so the project never ships on the unswappable default user.

    Phase 2 replaces this placeholder with the full Identity user model:
    normalized email login, Argon2id password hashing, soft-delete tombstone
    (database-schema §3.1; security-architecture §3.1).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        # BE-APP-1: table name fixed by database-schema.md, never Django's default.
        db_table = "users"

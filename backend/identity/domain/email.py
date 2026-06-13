"""EmailAddress value object (domain-model §2.1).

Case-insensitively unique, normalized to lowercase at the boundary (INV-ID-1).
Normalization is a pure function so the manager, serializers, and the unique
functional index (``lower(email)``) all agree on one canonical form.
"""


def normalize_email(raw: str) -> str:
    """Trim surrounding whitespace and lowercase the whole address (INV-ID-1).

    DataForge does not split local/domain parts for normalization: the unique
    index is ``lower(email)`` (database-schema §3.1), so the canonical form is
    simply the lowercased, stripped string. Idempotent.
    """
    return raw.strip().lower()

"""Use-case services for the Identity context (application layer, §3.1).

Public entry points are grouped by concern:
  * `identity.application.accounts` — signup, verify, reset, change, deletion
  * `identity.application.auth` — login, refresh rotation, logout, revocation
  * `identity.application.tokens` — single-use token issuance/consumption
  * `identity.application.permissions` — INV-ID-2 verified-user gate (reused by tenancy)

Re-exported here for a stable import surface.
"""

from identity.application import accounts, auth, permissions, tokens

__all__ = ["accounts", "auth", "permissions", "tokens"]

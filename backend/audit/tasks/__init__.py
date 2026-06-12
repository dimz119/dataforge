"""The Audit context defines no Celery tasks (backend-architecture §2.2).

This package exists so the §3.2 "App layering" contract's `api : tasks`
layer resolves in every container; it stays empty unless the §2.2 table
changes.
"""

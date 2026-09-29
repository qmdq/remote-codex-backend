"""Database schema is created idempotently on connect.

This module is intentionally small so future migrations can introduce explicit
version records without changing the startup contract.
"""

MIGRATION_VERSION = 1

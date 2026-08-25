"""Shared internal library for AIRIVU CSense backend services.

Nothing in this package should ever trust a client-supplied tenant_id (TRD-SEC §7.2).
All tenant scoping flows from a verified TenantContext derived from an authenticated,
signed access token.
"""

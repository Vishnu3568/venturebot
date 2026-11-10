"""Role-Based Access Control (RBAC) and permission matrices for VentureBot.

Implements the approved security roles and explicit permission boundaries
per Step 90.16.5A (§4) and Step 90.16.5B (§6):
- ROLE_VIEWER: Read-only access to experiments, audit logs, and metrics. Emergency stop permitted.
- ROLE_OPERATOR: Standard human operator managing pilot experiments. Full read, approval, execution,
  and emergency-stop permissions.
- ROLE_ADMIN: Security administrator managing operators, credentials, and emergency stop.
"""

from __future__ import annotations

from enum import Enum
from typing import Final

from venturebot.database.models import VALID_OPERATOR_ROLES


class SecurityError(Exception):
    """Base class for all VentureBot security-related exceptions."""


class AuthenticationError(SecurityError):
    """Raised when authentication fails (invalid credentials, inactive account, etc.)."""


class AuthorizationError(SecurityError):
    """Raised when an authenticated entity attempts an unauthorized action."""


class SessionError(SecurityError):
    """Raised when session validation fails (expired, revoked, inactive, invalid)."""


class AccountInactiveError(AuthenticationError):
    """Raised when an operator account is inactive or disabled."""


# Role Constants
ROLE_VIEWER: Final[str] = "ROLE_VIEWER"
ROLE_OPERATOR: Final[str] = "ROLE_OPERATOR"
ROLE_ADMIN: Final[str] = "ROLE_ADMIN"

# Permission Constants
PERM_EXPERIMENT_READ: Final[str] = "PERM_EXPERIMENT_READ"
PERM_EXPERIMENT_APPROVE: Final[str] = "PERM_EXPERIMENT_APPROVE"
PERM_MUTATION_EXECUTE: Final[str] = "PERM_MUTATION_EXECUTE"
PERM_EMERGENCY_STOP: Final[str] = "PERM_EMERGENCY_STOP"
PERM_SECURITY_ADMIN: Final[str] = "PERM_SECURITY_ADMIN"

ALL_PERMISSIONS: Final[frozenset[str]] = frozenset({
    PERM_EXPERIMENT_READ,
    PERM_EXPERIMENT_APPROVE,
    PERM_MUTATION_EXECUTE,
    PERM_EMERGENCY_STOP,
    PERM_SECURITY_ADMIN,
})

# Explicit Role-Permission Matrix (Step 90.16.5A §4.2 & Step 90.16.5B §6)
# Note: Emergency stop is available to ALL authenticated roles (Step 90.16.5B §6).
ROLE_PERMISSIONS: Final[dict[str, frozenset[str]]] = {
    ROLE_VIEWER: frozenset({
        PERM_EXPERIMENT_READ,
        PERM_EMERGENCY_STOP,
    }),
    ROLE_OPERATOR: frozenset({
        PERM_EXPERIMENT_READ,
        PERM_EXPERIMENT_APPROVE,
        PERM_MUTATION_EXECUTE,
        PERM_EMERGENCY_STOP,
    }),
    ROLE_ADMIN: frozenset({
        PERM_EXPERIMENT_READ,
        PERM_EMERGENCY_STOP,
        PERM_SECURITY_ADMIN,
    }),
}


def has_permission(role: str, permission: str) -> bool:
    """Check if a given role possesses the requested permission.

    Fails closed (returns False) for unknown roles or unrecognized permissions.

    Args:
        role: Operator role string (e.g. 'ROLE_OPERATOR').
        permission: Permission string (e.g. 'PERM_MUTATION_EXECUTE').

    Returns:
        True if permitted, False otherwise.
    """
    if not isinstance(role, str) or not isinstance(permission, str):
        return False
    if role not in VALID_OPERATOR_ROLES or permission not in ALL_PERMISSIONS:
        return False
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


def check_permission(role: str, permission: str) -> None:
    """Enforce that a role possesses a required permission.

    Args:
        role: Operator role string.
        permission: Required permission string.

    Raises:
        AuthorizationError: If permission is not granted to the role.
    """
    if not has_permission(role, permission):
        raise AuthorizationError(
            f"Permission denied: role {role!r} lacks required permission {permission!r}"
        )


def get_role_permissions(role: str) -> frozenset[str]:
    """Retrieve the set of allowed permissions for a role.

    Args:
        role: Operator role string.

    Returns:
        Immutable set of permission strings, or empty frozenset if role is invalid.
    """
    return ROLE_PERMISSIONS.get(role, frozenset())


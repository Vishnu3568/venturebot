"""Minimal local CLI interface for interactive capability ticket issuance.

Implements Step 90.16.8 CLI Issuance Requirements:
- Validates operator session.
- Displays safe operation summary without exposing credentials.
- Requires explicit interactive confirmation before issuance.
- Obtains operator passphrase securely via getpass (no echo, no CLI argument).
- Never logs or exposes passphrases, signing keys, or raw credentials.
- Local only: No network or web endpoints.
"""

from __future__ import annotations

import getpass
import logging
from typing import Callable
from sqlalchemy.orm import Session

from venturebot.security.capability import CapabilityRequest, issue_capability
from venturebot.security.rbac import AuthorizationError, ROLE_OPERATOR
from venturebot.security.session import validate_session

logger = logging.getLogger(__name__)


def request_capability_issuance_cli(
    db_session: Session,
    session_token: str,
    request: CapabilityRequest,
    *,
    signing_key: bytes | None = None,
    get_passphrase_fn: Callable[[str], str] | None = None,
    confirm_fn: Callable[[str], str] | None = None,
    output_fn: Callable[[str], None] | None = None,
) -> str:
    """Interactively authenticate and issue a mutation capability ticket via local console.

    Enforces:
    - Session validation before prompt.
    - Explicit confirmation of exact mutation details.
    - Secure non-echoing passphrase input (never accepted via CLI arguments).
    - Safe summary display with zero credential leakage.

    Args:
        db_session: Active SQLAlchemy session.
        session_token: Active authenticated operator session token.
        request: Explicit CapabilityRequest specifying the mutation parameters.
        signing_key: Optional explicit 256-bit symmetric signing key.
        get_passphrase_fn: Optional passphrase prompter (defaults to getpass.getpass).
        confirm_fn: Optional confirmation prompter (defaults to input).
        output_fn: Optional output printer (defaults to print).

    Returns:
        str: Opaque signed capability ticket string.

    Raises:
        AuthorizationError: If confirmation is declined, session is invalid, or role is unauthorized.
    """
    _getpass = get_passphrase_fn or getpass.getpass
    _input = confirm_fn or input
    _print = output_fn or print

    # 1. Validate Session & Operator Role
    session_record, operator = validate_session(db_session, session_token)
    if operator.role != ROLE_OPERATOR:
        raise AuthorizationError(
            f"Operator {operator.username!r} with role {operator.role!r} is not authorized "
            f"to issue mutation capabilities. Only {ROLE_OPERATOR!r} is permitted."
        )

    # 2. Display Safe Issuance Summary (No secrets)
    _print("\n" + "=" * 60)
    _print("VENTUREBOT MUTATION CAPABILITY AUTHORIZATION REQUEST")
    _print("=" * 60)
    _print(f"Operator:            {operator.username} ({operator.role})")
    _print(f"Experiment ID:       {request.experiment_id}")
    _print(f"Target Tier:         {request.target_tier}")
    _print(f"Exact Operation:     {request.exact_operation}")
    _print(f"Target Endpoint:     {request.target_endpoint} [{request.http_method}]")
    _print(f"Expected Parent ID:  {request.expected_parent_id}")
    _print(f"Replacement Name:    {request.replacement_name}")
    _print(f"Parameters Digest:   {request.parameters_digest or '(empty)'}")
    _print(f"Budget Ceiling:      {request.budget_ceiling_currency} {request.budget_ceiling_amount}")
    _print(f"Validity Window:     {request.validity_duration_seconds}s (max 900s)")
    _print("=" * 60)

    # 3. Explicit Operator Confirmation
    confirmation = _input("Authorize and issue signed capability ticket? [y/N]: ").strip().lower()
    if confirmation not in ("y", "yes"):
        _print("Operation cancelled by operator. No capability ticket was issued.")
        raise AuthorizationError("Capability issuance aborted: confirmation declined by operator.")

    # 4. Secure Interactive Passphrase Entry (No echo)
    passphrase = _getpass("Enter operator master passphrase to authorize: ")
    if not passphrase:
        raise AuthorizationError("Passphrase cannot be empty. Capability issuance aborted.")

    # 5. Issue Capability Ticket via Engine
    capability_orm, signed_ticket = issue_capability(
        db_session,
        session_token,
        passphrase,
        request,
        signing_key=signing_key,
    )

    # 6. Display Safe Success Summary
    _print("\n" + "=" * 60)
    _print("CAPABILITY TICKET SUCCESSFULLY ISSUED")
    _print("=" * 60)
    _print(f"Ticket ID:           {capability_orm.id}")
    _print(f"Status:              {capability_orm.status}")
    _print(f"Issued At:           {capability_orm.issued_at.isoformat()}")
    _print(f"Expires At:          {capability_orm.expires_at.isoformat()}")
    _print("=" * 60 + "\n")

    return signed_ticket


"""Default-off local contract adapter for the synthetic Assaf OS ledger.

``TrustedOwnerContext`` is an injected, synthetic analogue of server-side session,
Origin/CSRF, policy, and Telegram approval checks.  It is deliberately **not** an
authentication implementation or proof and this module is not registered with the
application runtime.  A future server integration must construct the context only
from freshly verified server state; client payloads never supply its fields.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from app.prototypes.assaf_os_domain import ENTITIES, canonical_input
from app.prototypes.assaf_os_primary import LocalPrimaryError, LocalPrincipal

API_VERSION = "assaf-os.primary-local.v1"
PRIMARY_SOURCE = "postgresql"
KNOWN_SCOPES = frozenset({"assaf-os:read", "assaf-os:propose", "assaf-os:approve"})
CHANNELS = frozenset({"OS", "MIA", "TELEGRAM_APPROVAL"})

_WORKSPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_RECORD_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{2,63}$")
_PROPOSAL_ID_RE = re.compile(r"^PRO_[0-9a-f]{32}$")


class AdapterError(Exception):
    """Sanitized stable adapter failure, with exact recovery identifiers when known."""

    def __init__(
        self,
        code: str,
        *,
        request_id: str | None = None,
        decision_id: str | None = None,
    ) -> None:
        self.code = code
        self.request_id = request_id
        self.decision_id = decision_id
        super().__init__(code)


@dataclass(frozen=True)
class TrustedOwnerContext:
    """Fresh server-derived authorization facts; synthetic context is not authentication."""

    numeric_owner: int
    workspace: str
    authority_epoch: int
    channel: Literal["OS", "MIA", "TELEGRAM_APPROVAL"]
    scopes: frozenset[str]
    auth_expires_at: datetime
    mutation_allowed: bool
    browser_mutation_verified: bool


def _canonical_uuid(value: Any, code: str) -> str:
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise AdapterError(code) from None
    return value


def _aware(value: Any) -> bool:
    try:
        return (
            type(value) is datetime
            and value.tzinfo is not None
            and value.utcoffset() is not None
        )
    except (OverflowError, TypeError, ValueError):
        return False


class LocalPrimaryAdapter:
    """Bounded facade over an already default-off ``LocalLedgerPrimary`` instance.

    The callback is invoked and fully validated for every call.  The facade does not
    read settings, register routes, authenticate users, retry decisions, or contact a
    mirror/provider.  Its browser verification boolean is only a test analogue of a
    server's Origin/CSRF check and must never be treated as proof on its own.
    """

    def __init__(
        self,
        ledger: Any,
        authorize: Callable[[], TrustedOwnerContext],
        enabled: bool = False,
    ) -> None:
        if not callable(authorize) or type(enabled) is not bool:
            raise AdapterError("invalid_adapter_configuration")
        self._ledger = ledger
        self._authorize = authorize
        self._enabled = enabled

    def _enabled_guard(self) -> None:
        if self._enabled is not True:
            raise AdapterError("adapter_disabled")

    def _context(
        self,
        scope: str,
        *,
        mutation: bool = False,
        deciding: bool = False,
    ) -> tuple[TrustedOwnerContext, LocalPrincipal]:
        self._enabled_guard()
        try:
            context = self._authorize()
        except Exception:
            raise AdapterError("authorization_unavailable") from None

        if type(context) is not TrustedOwnerContext:
            raise AdapterError("invalid_trusted_context")
        if (
            type(context.numeric_owner) is not int
            or not 0 < context.numeric_owner <= 99_999_999_999_999_999_999
            or type(context.workspace) is not str
            or _WORKSPACE_RE.fullmatch(context.workspace) is None
            or type(context.authority_epoch) is not int
            or context.authority_epoch <= 0
            or type(context.channel) is not str
            or context.channel not in CHANNELS
            or type(context.scopes) is not frozenset
            or any(type(item) is not str for item in context.scopes)
            or not context.scopes <= KNOWN_SCOPES
            or not _aware(context.auth_expires_at)
            or type(context.mutation_allowed) is not bool
            or type(context.browser_mutation_verified) is not bool
        ):
            raise AdapterError("invalid_trusted_context")
        try:
            expired = context.auth_expires_at.astimezone(UTC) <= datetime.now(UTC)
        except (OverflowError, TypeError, ValueError):
            raise AdapterError("invalid_trusted_context") from None
        if expired:
            raise AdapterError("authentication_expired")

        binding = getattr(self._ledger, "binding", None)
        if (
            binding is None
            or getattr(binding, "owner_id", None) != str(context.numeric_owner)
            or getattr(binding, "workspace_id", None) != context.workspace
            or getattr(binding, "authority_epoch", None) != context.authority_epoch
        ):
            raise AdapterError("authority_changed")
        if scope not in context.scopes:
            raise AdapterError("scope_denied")
        if deciding and context.channel == "MIA":
            raise AdapterError("owner_approval_required")
        if mutation:
            if context.mutation_allowed is not True:
                raise AdapterError("mutation_disabled")
            if context.channel == "OS" and context.browser_mutation_verified is not True:
                raise AdapterError("browser_mutation_verification_required")
        return context, LocalPrincipal(
            owner_id=str(context.numeric_owner),
            source=context.channel,
        )

    @staticmethod
    def _base(context: TrustedOwnerContext) -> dict[str, Any]:
        return {
            "api_version": API_VERSION,
            "primary_source": PRIMARY_SOURCE,
            "workspace_id": context.workspace,
            "authority_epoch": context.authority_epoch,
        }

    @staticmethod
    def _mirror(raw: Any, *, default_status: str) -> dict[str, Any]:
        mirror = deepcopy(raw) if type(raw) is dict else {"status": default_status}
        if mirror.get("status") not in {"pending", "not_required"}:
            raise ValueError("invalid mirror response")
        mirror["separate"] = True
        return mirror

    @staticmethod
    def _local_error(exc: LocalPrimaryError) -> AdapterError:
        code = exc.code if type(exc.code) is str and exc.code else "primary_rejected"
        return AdapterError(code)

    def contract(self) -> dict[str, Any]:
        """Return static local contract metadata after a fresh authorized read check."""
        context, _ = self._context("assaf-os:read")
        return self._base(context) | {
            "enabled": True,
            "context_trust": "synthetic_server_callback_only_not_authentication",
            "approval": "explicit_owner_decision_only",
            "mirror": {"status": "pending", "separate": True},
        }

    def snapshot(self, limit: int = 100, after: dict[str, str] | None = None) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise AdapterError("invalid_page_limit")
        if after is not None and (
            type(after) is not dict
            or any(
                type(entity) is not str
                or entity not in ENTITIES
                or type(record_id) is not str
                or _RECORD_ID_RE.fullmatch(record_id) is None
                for entity, record_id in after.items()
            )
        ):
            raise AdapterError("invalid_page_cursor")
        context, principal = self._context("assaf-os:read")
        try:
            raw = self._ledger.snapshot(principal, limit=limit, after=deepcopy(after))
            if (
                type(raw) is not dict
                or raw.get("primary_source") != PRIMARY_SOURCE
                or raw.get("workspace_id") != context.workspace
                or raw.get("authority_epoch") != context.authority_epoch
                or type(raw.get("observed_version")) is not int
                or raw["observed_version"] < 0
                or type(raw.get("records")) is not dict
                or set(raw["records"]) != set(ENTITIES)
            ):
                raise ValueError("invalid snapshot response")
            result = deepcopy(raw)
            result.update(self._base(context))
            result.update(
                {
                    "commit_version": None,
                    "verified_primary": True,
                    "verified_receipt": False,
                    "mirror": self._mirror(raw.get("mirror"), default_status="pending"),
                }
            )
            return result
        except LocalPrimaryError as exc:
            raise self._local_error(exc) from None
        except Exception:
            raise AdapterError("primary_unavailable") from None

    def propose(
        self,
        request_id: str,
        entity: str,
        inputs: dict[str, Any],
        record_id: str | None = None,
        expected_revision: int = 0,
        approval_expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        request_id = _canonical_uuid(request_id, "invalid_request_id")
        if type(entity) is not str or entity not in (*ENTITIES, "promote_lead"):
            raise AdapterError("invalid_entity")
        if type(inputs) is not dict:
            raise AdapterError("invalid_input_shape")
        if record_id is not None and (
            type(record_id) is not str or _RECORD_ID_RE.fullmatch(record_id) is None
        ):
            raise AdapterError("invalid_record_id")
        if type(expected_revision) is not int or expected_revision < 0:
            raise AdapterError("invalid_revision")
        if approval_expires_at is not None and not _aware(approval_expires_at):
            raise AdapterError("invalid_approval_expiry")
        try:
            if entity == "promote_lead":
                if inputs or record_id is None:
                    raise AdapterError("invalid_promotion_input")
            else:
                canonical_input(entity, inputs, creating=record_id is None)
        except LocalPrimaryError as exc:
            raise self._local_error(exc) from None

        context, principal = self._context("assaf-os:propose", mutation=True)
        if context.channel == "TELEGRAM_APPROVAL":
            raise AdapterError("proposal_source_required")
        try:
            raw = self._ledger.propose(
                principal,
                request_id,
                entity,
                deepcopy(inputs),
                record_id=record_id,
                expected_revision=expected_revision,
                approval_expires_at=approval_expires_at,
            )
            if (
                type(raw) is not dict
                or _PROPOSAL_ID_RE.fullmatch(raw.get("proposal_id", "")) is None
                or raw.get("workspace_id") != context.workspace
                or raw.get("owner_id") != str(context.numeric_owner)
                or raw.get("epoch") != context.authority_epoch
                or raw.get("request_id") != request_id
                or raw.get("source") != context.channel
                or raw.get("entity") != entity
                or raw.get("status") not in {
                    "pending",
                    "committed",
                    "rejected",
                    "expired",
                    "target_changed",
                }
                or _canonical_uuid(raw.get("approval_id"), "invalid_primary_response")
                is None
                or _canonical_uuid(raw.get("rejection_id"), "invalid_primary_response")
                is None
            ):
                raise ValueError("invalid proposal response")
            result = deepcopy(raw)
            result.update(self._base(context))
            result.update(
                {
                    "observed_version": None,
                    "commit_version": None,
                    "verified_primary": False,
                    "verified_receipt": False,
                    "mirror": {"status": "not_required", "separate": True},
                }
            )
            return result
        except LocalPrimaryError as exc:
            raise self._local_error(exc) from None
        except AdapterError:
            raise AdapterError("inconclusive", request_id=request_id) from None
        except Exception:
            raise AdapterError("inconclusive", request_id=request_id) from None

    def decide(
        self,
        proposal_id: str,
        request_id: str,
        decision: Literal["approve", "reject"] = "approve",
        explicit_owner_decision: bool = False,
    ) -> dict[str, Any]:
        if type(proposal_id) is not str or _PROPOSAL_ID_RE.fullmatch(proposal_id) is None:
            raise AdapterError("invalid_proposal_id")
        request_id = _canonical_uuid(request_id, "invalid_request_id")
        if type(decision) is not str or decision not in {"approve", "reject"}:
            raise AdapterError("invalid_decision")
        if type(explicit_owner_decision) is not bool or explicit_owner_decision is not True:
            raise AdapterError("explicit_owner_decision_required")
        context, principal = self._context(
            "assaf-os:approve", mutation=True, deciding=True
        )
        try:
            raw = self._ledger.decide(principal, proposal_id, request_id, decision)
            return self._receipt_result(raw, context, request_id)
        except LocalPrimaryError as exc:
            if exc.code == "primary_verification_failed":
                raise AdapterError("inconclusive", decision_id=request_id) from None
            raise self._local_error(exc) from None
        except Exception:
            raise AdapterError("inconclusive", decision_id=request_id) from None

    def receipt(self, request_id: str) -> dict[str, Any]:
        request_id = _canonical_uuid(request_id, "invalid_request_id")
        context, principal = self._context("assaf-os:read")
        try:
            raw = self._ledger.receipt(principal, request_id)
            return self._receipt_result(raw, context, request_id)
        except LocalPrimaryError as exc:
            if exc.code == "primary_verification_failed":
                raise AdapterError("inconclusive", decision_id=request_id) from None
            raise self._local_error(exc) from None
        except Exception:
            raise AdapterError("inconclusive", decision_id=request_id) from None

    def _receipt_result(
        self,
        raw: Any,
        context: TrustedOwnerContext,
        request_id: str,
    ) -> dict[str, Any]:
        if type(raw) is not dict:
            raise ValueError("invalid receipt response")
        if raw.get("status") == "unknown":
            if raw.get("request_id") != request_id or raw.get("verified_primary") is not False:
                raise ValueError("invalid unknown receipt")
            return self._base(context) | {
                "status": "unknown",
                "request_id": request_id,
                "observed_version": None,
                "commit_version": None,
                "verified_primary": False,
                "verified_receipt": False,
                "mirror": {"status": "pending", "separate": True},
            }

        receipt = raw.get("receipt")
        if (
            raw.get("primary_source") != PRIMARY_SOURCE
            or raw.get("workspace_id") != context.workspace
            or raw.get("authority_epoch") != context.authority_epoch
            or raw.get("verified_primary") is not True
            or type(raw.get("observed_version")) is not int
            or type(receipt) is not dict
            or receipt.get("request_id") != request_id
            or receipt.get("status") not in {
                "committed",
                "rejected",
                "expired",
                "target_changed",
            }
            or type(receipt.get("commit_version")) is not int
            or receipt["commit_version"] <= 0
            or raw["observed_version"] < receipt["commit_version"]
        ):
            raise ValueError("invalid verified receipt")
        result = deepcopy(raw)
        result.update(self._base(context))
        result.update(
            {
                "commit_version": receipt["commit_version"],
                "verified_receipt": True,
                "mirror": self._mirror(
                    raw.get("mirror"),
                    default_status=(
                        "pending" if receipt["status"] == "committed" else "not_required"
                    ),
                ),
            }
        )
        return result


__all__ = [
    "API_VERSION",
    "AdapterError",
    "LocalPrimaryAdapter",
    "TrustedOwnerContext",
]

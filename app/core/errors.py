class MiaError(Exception):
    """Domain error safe to surface without stack traces."""

    code = "mia_error"
    http_status = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class MergeRejected(MiaError):
    code = "merge_rejected"
    http_status = 409


class WebhookRejected(MiaError):
    code = "webhook_rejected"
    http_status = 401


class PolicyDenied(MiaError):
    code = "policy_denied"
    http_status = 403


class AuthenticationRequired(MiaError):
    code = "authentication_required"
    http_status = 401


class PermissionDenied(MiaError):
    code = "permission_denied"
    http_status = 403


class CapabilityUnavailable(MiaError):
    code = "capability_unavailable"
    http_status = 404


class InvalidArguments(MiaError):
    code = "invalid_arguments"
    http_status = 400


class RateLimited(MiaError):
    code = "rate_limited"
    http_status = 429


class ProviderUnavailable(MiaError):
    code = "provider_unavailable"
    http_status = 503


class ExternalServiceError(MiaError):
    code = "external_service_error"
    http_status = 502


class ApprovalRequired(MiaError):
    code = "approval_required"
    http_status = 409

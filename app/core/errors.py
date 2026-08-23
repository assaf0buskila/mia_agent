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

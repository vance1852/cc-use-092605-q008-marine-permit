"""海域许可联动服务向 API 和 CLI 暴露的稳定错误。"""


class PermitError(RuntimeError):
    code = "permit_error"
    status = 400


class NotFound(PermitError):
    code = "not_found"
    status = 404


class Conflict(PermitError):
    code = "conflict"
    status = 409


class Forbidden(PermitError):
    code = "forbidden"
    status = 403


class InvalidState(PermitError):
    code = "invalid_state"
    status = 409


class ValidationFailed(PermitError):
    code = "validation_failed"
    status = 422

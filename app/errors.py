class AppError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code


class AuthError(AppError):
    status = 401
    code = "login_required"


class AdapterNotReady(AppError):
    status = 501
    code = "adapter_not_ready"

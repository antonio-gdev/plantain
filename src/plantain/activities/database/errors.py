"""Safe database activity exceptions."""


class DatabaseActivityError(RuntimeError):
    """Database failure whose message is safe for logs and reports."""


__all__ = ["DatabaseActivityError"]

from __future__ import annotations


class AgentError(Exception):
    code = "agent.error"
    retryable = False
    status = "internal_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class ValidationError(AgentError):
    code = "protocol.invalid_message"
    status = "invalid_message"


class UnsupportedVersionError(AgentError):
    code = "protocol.unsupported_version"
    status = "unsupported_version"


class AuthenticationError(AgentError):
    code = "auth.invalid_token"
    status = "unauthorized"


class PairingError(AgentError):
    code = "pair.invalid_code"
    status = "pairing_failed"


class ProjectError(AgentError):
    code = "project.invalid_path"
    status = "project_error"


class ProjectNotFoundError(ProjectError):
    code = "project.not_found"


class ProjectExistsError(ProjectError):
    code = "project.exists"


class PathNotAllowedError(ProjectError):
    code = "project.not_allowed"


class ProjectAuthorizationRequired(ProjectError):
    code = "project.authorization_required"


class TurnError(AgentError):
    code = "turn.failed"


class TurnBusyError(TurnError):
    code = "turn.busy"
    status = "conflict"


class TurnNotFoundError(TurnError):
    code = "turn.not_found"

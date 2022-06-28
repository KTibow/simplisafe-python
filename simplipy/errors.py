"""Define package errors."""
from __future__ import annotations

from typing import Any


class SimplipyError(Exception):
    """A base error."""

    pass


class EndpointUnavailableError(SimplipyError):
    """An error related to accessing an endpoint that isn't available in the plan."""

    pass


class InvalidCredentialsError(SimplipyError):
    """An error related to invalid credentials."""

    pass


class PinError(SimplipyError):
    """An error related to invalid PINs or PIN operations."""

    pass


class RequestError(SimplipyError):
    """An error related to invalid requests."""

    pass


class Verify2FAError(SimplipyError):
    """An error related to the 2FA process."""

    pass


class Verify2FAPending(SimplipyError):
    """An "error" related to 2FA still pending."""

    pass


class WebsocketError(SimplipyError):
    """An error related to generic websocket errors."""

    pass


class CannotConnectError(WebsocketError):
    """Define a error when the websocket can't be connected to."""

    pass


class ConnectionClosedError(WebsocketError):
    """Define a error when the websocket closes unexpectedly."""

    pass


class ConnectionFailedError(WebsocketError):
    """Define a error when the websocket connection fails."""

    pass


class InvalidMessageError(WebsocketError):
    """Define a error related to an invalid message from the websocket server."""

    pass


class NotConnectedError(WebsocketError):
    """Define a error when the websocket isn't properly connected to."""

    pass


DATA_ERROR_MAP: dict[str, type[SimplipyError]] = {
    "NoRemoteManagement": EndpointUnavailableError,
    "PinError": PinError,
}


def raise_on_data_error(data: dict[str, Any]) -> None:
    """Raise a specific error if the data payload suggests there is one."""
    error_type = data.get("type")

    if error_type not in DATA_ERROR_MAP:
        return

    raise DATA_ERROR_MAP[error_type](data["message"])

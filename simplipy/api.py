"""Define a SimpliSafe account."""
import asyncio
import base64
from json.decoder import JSONDecodeError
from typing import Dict, Optional, Type, TypeVar, Union
from uuid import uuid4

from aiohttp import ClientSession, ClientTimeout
from aiohttp.client_exceptions import ClientError

from simplipy.const import LOGGER
from simplipy.errors import (
    EndpointUnavailable,
    InvalidCredentialsError,
    PendingAuthorizationError,
    RequestError,
)
from simplipy.system.v2 import SystemV2
from simplipy.system.v3 import SystemV3

API_URL_HOSTNAME = "api.simplisafe.com"
API_URL_BASE = f"https://{API_URL_HOSTNAME}/v1"
API_URL_MFA_OOB = "http://simplisafe.com/oauth/grant-type/mfa-oob"

DEFAULT_APP_VERSION = "1.62.0"
DEFAULT_REQUEST_RETRIES = 3
DEFAULT_REQUEST_RETRY_INTERVAL = 3
DEFAULT_TIMEOUT = 10
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_6) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/13.1.2 Safari/605.1.15"
)

CLIENT_ID_TEMPLATE = "{0}.WebApp.simplisafe.com"
DEVICE_ID_TEMPLATE = (
    'WebApp; useragent="Safari 13.1 (SS-ID: {0}) / macOS 10.15.6"; uuid="{1}"; id="{0}"'
)


ApiType = TypeVar("ApiType", bound="API")


def generate_device_id(client_id: str) -> str:
    """Generate a random 10-character ID to use as the SimpliSafe device ID."""
    seed = base64.b64encode(client_id.encode()).decode()[:10]
    return f"{seed[:5]}-{seed[5:]}"


class API:  # pylint: disable=too-many-instance-attributes
    """An API object to interact with the SimpliSafe cloud.

    Note that this class shouldn't be instantiated directly; instead, the
    :meth:`simplipy.API.login_via_credentials` and :meth:`simplipy.API.login_via_token`
    class methods should be used.

    :param session: The ``aiohttp`` ``ClientSession`` session used for all HTTP requests
    :type session: ``aiohttp.client.ClientSession``
    :param client_id: The SimpliSafe client ID to use for this API object
    :type client_id: ``str``
    :param request_retry_interval: The number of seconds between request retries
    :type client_id: ``int``
    """

    def __init__(
        self,
        *,
        session: Optional[ClientSession] = None,
        client_id: Optional[str] = None,
        request_retry_interval: int = DEFAULT_REQUEST_RETRY_INTERVAL,
    ) -> None:
        """Initialize."""
        self._client_id = client_id or str(uuid4())
        self._refresh_tried: bool = False
        self._request_retry_interval = request_retry_interval
        self._session: ClientSession = session

        # These will get filled in after initial authentication:
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self.email: Optional[str] = None
        self.user_id: Optional[int] = None

        self.client_id_string = CLIENT_ID_TEMPLATE.format(self._client_id)
        self.device_id_string = DEVICE_ID_TEMPLATE.format(
            generate_device_id(self._client_id), self._client_id
        )
        self.subscription_data: Dict[int, dict] = {}

    @property
    def access_token(self) -> Optional[str]:
        """Return the current access token.

        :rtype: ``str``
        """
        return self._access_token

    @property
    def client_id(self) -> str:
        """Return the client ID of the API."""
        return self._client_id

    @property
    def refresh_token(self) -> Optional[str]:
        """Return the current refresh token.

        :rtype: ``str``
        """
        return self._refresh_token

    @classmethod
    async def login_via_credentials(
        cls: Type[ApiType],
        email: str,
        password: str,
        *,
        session: Optional[ClientSession] = None,
        client_id: Optional[str] = None,
        request_retry_interval: int = DEFAULT_REQUEST_RETRY_INTERVAL,
    ) -> ApiType:
        """Create an API object from a email address and password.

        :param email: A SimpliSafe email address
        :type email: ``str``
        :param password: A SimpliSafe password
        :type password: ``str``
        :param session: An ``aiohttp`` ``ClientSession``
        :type session: ``aiohttp.client.ClientSession``
        :param client_id: The SimpliSafe client ID to use for this API object
        :type client_id: ``str``
        :param request_retry_interval: The number of seconds between request retries
        :type client_id: ``int``
        :rtype: :meth:`simplipy.API`
        """
        instance = cls(
            session=session,
            client_id=client_id,
            request_retry_interval=request_retry_interval,
        )
        instance.email = email

        await instance.authenticate(
            {
                "grant_type": "password",
                "username": email,
                "password": password,
                "client_id": instance.client_id_string,
                "device_id": instance.device_id_string,
                "app_version": DEFAULT_APP_VERSION,
                "scope": "offline_access",
            }
        )

        return instance

    @classmethod
    async def login_via_token(
        cls: Type[ApiType],
        refresh_token: str,
        *,
        session: Optional[ClientSession] = None,
        client_id: Optional[str] = None,
        request_retry_interval: int = DEFAULT_REQUEST_RETRY_INTERVAL,
    ) -> ApiType:
        """Create an API object from a refresh token.

        :param refresh_token: A SimpliSafe refresh token
        :type refresh_token: ``str``
        :param session: An ``aiohttp`` ``ClientSession``
        :type session: ``aiohttp.client.ClientSession``
        :param client_id: The SimpliSafe client ID to use for this API object
        :type client_id: ``str``
        :param request_retry_interval: The number of seconds between request retries
        :type client_id: ``int``
        :rtype: :meth:`simplipy.API`
        """
        instance = cls(
            session=session,
            client_id=client_id,
            request_retry_interval=request_retry_interval,
        )
        await instance.refresh_access_token(refresh_token)
        return instance

    async def authenticate(self, payload: dict) -> None:
        """Authenticate the API object using an authentication payload."""
        LOGGER.debug("Authentication payload: %s", payload)

        token_resp = await self.request("post", "api/token", json=payload)

        if "mfa_token" in token_resp:
            mfa_challenge_response = await self.request(
                "post",
                "api/mfa/challenge",
                json={
                    "challenge_type": "oob",
                    "client_id": self.client_id_string,
                    "mfa_token": token_resp["mfa_token"],
                },
            )

            await self.request(
                "post",
                "api/token",
                json={
                    "client_id": self.client_id_string,
                    "grant_type": API_URL_MFA_OOB,
                    "mfa_token": token_resp["mfa_token"],
                    "oob_code": mfa_challenge_response["oob_code"],
                    "scope": "offline_access",
                },
            )

            raise PendingAuthorizationError(
                f"Check your email for an MFA link, then use {self._client_id} "
                "as the client_id parameter in future API calls"
            )

        # Set access and refresh tokens:
        self._access_token = token_resp["access_token"]
        self._refresh_token = token_resp["refresh_token"]

        # Fetch the SimpliSafe user ID:
        auth_check_resp = await self.request("get", "api/authCheck")
        self.user_id = auth_check_resp["userId"]

    async def get_systems(self) -> Dict[int, Union[SystemV2, SystemV3]]:
        """Get systems associated to the associated SimpliSafe account.

        In the dict that is returned, the keys are the system ID and the values are
        actual ``System`` objects.

        :rtype: ``Dict[int, simplipy.system.System]``
        """
        await self.update_subscription_data()

        systems = {}

        for system_id, subscription in self.subscription_data.items():
            version = subscription["location"]["system"]["version"]

            system: Union[SystemV2, SystemV3]
            if version == 2:
                system = SystemV2(self, system_id)
            else:
                system = SystemV3(self, system_id)

            # Skip deactivated systems:
            if not system.active:
                LOGGER.info("Skipping deactivated system: %s", system_id)
                continue

            # Update the system, but don't include system data itself, since it will
            # already have been fetched when the API was first queried:
            await system.update(include_system=False)
            await system.generate_entities()
            systems[system_id] = system

        return systems

    async def request(  # pylint: disable=too-many-branches
        self, method: str, endpoint: str, **kwargs
    ) -> dict:
        """Make an API request."""
        kwargs.setdefault("headers", {})
        if self._access_token:
            kwargs["headers"]["Authorization"] = f"Bearer {self._access_token}"
        kwargs["headers"]["Content-Type"] = "application/json; charset=utf-8"
        kwargs["headers"]["Host"] = API_URL_HOSTNAME
        kwargs["headers"]["User-Agent"] = DEFAULT_USER_AGENT

        use_running_session = self._session and not self._session.closed

        if use_running_session:
            session = self._session
        else:
            session = ClientSession(timeout=ClientTimeout(total=DEFAULT_TIMEOUT))

        data = {}
        retries = 0
        while retries < DEFAULT_REQUEST_RETRIES:
            try:
                async with session.request(
                    method, f"{API_URL_BASE}/{endpoint}", **kwargs
                ) as resp:
                    try:
                        data = await resp.json(content_type=None)
                    except JSONDecodeError:
                        message = await resp.text()
                        data = {"error": message}

                    LOGGER.debug("Data received from /%s: %s", endpoint, data)

                    if isinstance(data, str):
                        # In some cases, the SimpliSafe API will return a quoted string
                        # in its response body (e.g., "\"node not found\""), which is
                        # technically valid JSON. Additionally, SimpliSafe sets that
                        # response's Content-Type header to application/json (#smh).
                        # Together, these factors will allow a non-true-JSON  payload to
                        # escape the try/except above. So, if we get here, we use the
                        # string value (with quotes removed) to raise an error:
                        message = data.replace('"', "")
                        data = {"error": message}

                    resp.raise_for_status()
                    return data
            except ClientError as err:
                # If we get an "error" related to MFA, the response body data is
                # necessary for continuing on, so we swallow the error and return
                # that data:
                if data.get("error") == "mfa_required":
                    return data

                if data.get("type") == "NoRemoteManagement":
                    raise EndpointUnavailable(
                        f"Endpoint unavailable in plan: {endpoint}"
                    ) from None

                if "401" in str(err):
                    if self._refresh_tried or not self._access_token:
                        raise InvalidCredentialsError(
                            "Invalid username/password"
                        ) from None
                    if self._refresh_token and not self._refresh_tried:
                        LOGGER.info("401 detected; attempting refresh token")
                        self._refresh_tried = True
                        await self.refresh_access_token(self._refresh_token)

                if "403" in str(err):
                    raise InvalidCredentialsError("Invalid username/password") from None

                LOGGER.warning(
                    "Error while requesting /%s: %s (attempt %s of %s)",
                    endpoint,
                    err,
                    retries + 1,
                    DEFAULT_REQUEST_RETRIES,
                )
                retries += 1
                await asyncio.sleep(self._request_retry_interval)
            finally:
                if not use_running_session:
                    await session.close()

        raise RequestError(
            f"Requesting /{endpoint} failed after {retries} tries"
        ) from None

    async def refresh_access_token(self, refresh_token: Optional[str]) -> None:
        """Regenerate an access token.

        :param refresh_token: The refresh token to use
        :type refresh_token: str
        """
        await self.authenticate(
            {
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "refresh_token": refresh_token,
            }
        )

    async def update_subscription_data(self) -> None:
        """Update our internal "raw data" listing of subscriptions."""
        subscription_resp = await self.request(
            "get", f"users/{self.user_id}/subscriptions", params={"activeOnly": "true"}
        )

        for subscription in subscription_resp["subscriptions"]:
            if "version" not in subscription["location"]["system"]:
                LOGGER.error(
                    "Skipping location with missing system data: %s",
                    subscription["location"]["sid"],
                )
                continue

            self.subscription_data[subscription["sid"]] = subscription

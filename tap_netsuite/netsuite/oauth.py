"""OAuth 2.0 authorization-code token refresh for NetSuite REST/SuiteQL."""
import base64
import time

import requests
import singer

from .exceptions import SymonException

LOGGER = singer.get_logger()

TOKEN_PATH = '/services/rest/auth/oauth2/v1/token'

DEFAULT_TOKEN_TTL_SECONDS = 3600
EXPIRY_SKEW_SECONDS = 360

SANDBOX_ACCOUNT_SUFFIX = '_SB1'


def resolve_account_id(account, is_sandbox=False):
    """Return the account ID, including the sandbox suffix."""
    if account is None:
        return None
    if is_sandbox is True and not account.upper().endswith(SANDBOX_ACCOUNT_SUFFIX):
        return account + SANDBOX_ACCOUNT_SUFFIX
    return account


def account_host(account_id):
    """Return the REST hostname account component."""
    return account_id.lower().replace('_', '-')


def service_base_url(account_id):
    return f'https://{account_host(account_id)}.suitetalk.api.netsuite.com'


def token_error(response):
    """Convert a token error to a user-facing exception."""
    try:
        body = response.json()
    except ValueError:
        body = {}

    error = body.get('error', '')
    description = body.get('error_description', '')
    LOGGER.error('NetSuite token request failed (%s): %s %s', response.status_code, error, description)

    if error == 'invalid_client':
        return SymonException(
            'NetSuite rejected the OAuth client configuration. Check the integration record and try again.',
            'netSuite.NetSuiteInvalidOAuthClient'
        )

    if error == 'invalid_grant':
        return SymonException(
            'NetSuite rejected the authentication request. Please reconnect and try again.',
            'netSuite.NetSuiteInvalidOAuthGrant'
        )

    if error in ('invalid_scope', 'insufficient_scope'):
        return SymonException(
            'The NetSuite integration record does not grant REST web services access. Enable '
            'the REST Web Services scope on the integration record and try again.',
            'netSuite.NetSuiteInsufficientScope'
        )

    return SymonException(
        'Could not authenticate with NetSuite using OAuth 2.0. Please check the integration '
        'configuration and try again.',
        'netSuite.NetSuiteOAuthFailed'
    )


class NetSuiteOAuthAuthorizationCodeTokenManager:
    """Refresh OAuth 2.0 authorization-code access tokens during long imports."""

    def __init__(self,
                 account_id,
                 client_id,
                 client_secret,
                 refresh_token=None,
                 access_token=None,
                 session=None):
        self.account_id = account_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.session = session or requests.Session()

        self._access_token = access_token
        if access_token:
            self._expires_at = time.time() + DEFAULT_TOKEN_TTL_SECONDS - EXPIRY_SKEW_SECONDS
        else:
            self._expires_at = 0

    @property
    def token_url(self):
        return f'{service_base_url(self.account_id)}{TOKEN_PATH}'

    def access_token(self):
        if self._access_token is None or time.time() >= self._expires_at:
            self._refresh()
        return self._access_token

    def invalidate(self):
        self._access_token = None
        self._expires_at = 0

    def _refresh(self):
        if not self.refresh_token:
            raise SymonException(
                'The NetSuite connection is missing a refresh token. Please reconnect and try again.',
                'netSuite.NetSuiteMissingRefreshToken'
            )

        try:
            credentials = f'{self.client_id}:{self.client_secret}'.encode('utf-8')
            response = self.session.post(
                self.token_url,
                data={
                    'grant_type': 'refresh_token',
                    'refresh_token': self.refresh_token
                },
                headers={
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Authorization': f'Basic {base64.b64encode(credentials).decode("ascii")}'
                },
                timeout=60
            )
        except requests.exceptions.ConnectionError as e:
            message = str(e)
            if 'Name or service not known' in message or 'nodename nor servname provided, or not known' in message:
                raise SymonException(
                    'The account ID provided is incorrect. Please check the account ID and try again.',
                    'netSuite.NetSuiteInvalidAccountID'
                ) from e
            raise SymonException(
                'Could not refresh the NetSuite OAuth access token.',
                'netSuite.NetSuiteOAuthFailed'
            ) from e

        if response.status_code != 200:
            raise token_error(response)

        body = response.json()
        self._access_token = body.get('access_token')
        if not self._access_token:
            raise SymonException(
                'NetSuite did not return an access token while refreshing.',
                'netSuite.NetSuiteOAuthFailed'
            )

        new_refresh_token = body.get('refresh_token')
        if new_refresh_token and new_refresh_token != self.refresh_token:
            # The tap cannot persist this; the connection must be re-authorized once the old token expires.
            LOGGER.warning('NetSuite issued a new refresh token that will not be saved to the connection')
            self.refresh_token = new_refresh_token

        expires_in = body.get('expires_in', DEFAULT_TOKEN_TTL_SECONDS)
        try:
            expires_in = int(expires_in)
        except (TypeError, ValueError):
            expires_in = DEFAULT_TOKEN_TTL_SECONDS

        self._expires_at = time.time() + max(expires_in - EXPIRY_SKEW_SECONDS, 0)
        LOGGER.info('Refreshed a NetSuite OAuth 2.0 access token valid for %s seconds', expires_in)

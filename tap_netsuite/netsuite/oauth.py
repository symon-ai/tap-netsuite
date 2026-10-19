"""Create and cache NetSuite OAuth 2.0 access tokens."""
import time

import jwt
import requests
import singer

from .exceptions import SymonException

LOGGER = singer.get_logger()

TOKEN_PATH = '/services/rest/auth/oauth2/v1/token'
CLIENT_ASSERTION_TYPE = 'urn:ietf:params:oauth:client-assertion-type:jwt-bearer'
# NetSuite requires PS256 for M2M OAuth.
SIGNING_ALGORITHM = 'PS256'

# REST web services scope.
DEFAULT_SCOPE = ['rest_webservices']

# NetSuite's maximum assertion lifetime.
ASSERTION_TTL_SECONDS = 3600

# Renew six minutes early for long requests.
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


class NetSuiteOAuthTokenManager:
    def __init__(self,
                 account_id,
                 client_id,
                 certificate_id,
                 private_key,
                 scope=None,
                 session=None):
        self.account_id = account_id
        self.client_id = client_id
        self.certificate_id = certificate_id
        self.private_key = private_key
        self.scope = scope or DEFAULT_SCOPE
        self.session = session or requests.Session()

        self._access_token = None
        self._expires_at = 0

    @property
    def token_url(self):
        return f'{service_base_url(self.account_id)}{TOKEN_PATH}'

    def access_token(self):
        """Return a valid token, minting one if needed."""
        if self._access_token is None or time.time() >= self._expires_at:
            self._mint()
        return self._access_token

    def invalidate(self):
        """Clear the cached token."""
        self._access_token = None
        self._expires_at = 0

    def _build_assertion(self):
        now = int(time.time())
        payload = {
            'iss': self.client_id,
            'scope': self.scope,
            'aud': self.token_url,
            'iat': now,
            'exp': now + ASSERTION_TTL_SECONDS
        }

        try:
            return jwt.encode(
                payload,
                self.private_key,
                algorithm=SIGNING_ALGORITHM,
                headers={'kid': self.certificate_id}
            )
        except Exception as e:
            raise SymonException(
                'Could not sign the NetSuite authentication request. The stored certificate key '
                'pair may need to be regenerated.',
                'netSuite.NetSuiteAssertionSigningFailed'
            ) from e

    def _mint(self):
        assertion = self._build_assertion()

        try:
            response = self.session.post(
                self.token_url,
                data={
                    'grant_type': 'client_credentials',
                    'client_assertion_type': CLIENT_ASSERTION_TYPE,
                    'client_assertion': assertion
                },
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=60
            )
        except requests.exceptions.ConnectionError as e:
            message = str(e)
            if 'Name or service not known' in message or 'nodename nor servname provided, or not known' in message:
                raise SymonException(
                    'The account ID provided is incorrect. Please check the account ID and try again.',
                    'netSuite.NetSuiteInvalidAccountID'
                ) from e
            raise

        if response.status_code != 200:
            raise self._token_error(response)

        body = response.json()
        self._access_token = body.get('access_token')
        if not self._access_token:
            raise SymonException(
                'NetSuite did not return an access token. Please check the integration '
                'configuration and try again.',
                'netSuite.NetSuiteOAuthFailed'
            )

        expires_in = body.get('expires_in', ASSERTION_TTL_SECONDS)
        try:
            expires_in = int(expires_in)
        except (TypeError, ValueError):
            expires_in = ASSERTION_TTL_SECONDS

        self._expires_at = time.time() + max(expires_in - EXPIRY_SKEW_SECONDS, 0)
        LOGGER.info('Minted a NetSuite OAuth 2.0 access token valid for %s seconds', expires_in)

    @staticmethod
    def _token_error(response):
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
                'NetSuite rejected the client ID or certificate. Check that the client ID matches '
                'the integration record, that the certificate ID matches the client credentials '
                'mapping, and that the mapping uses the certificate downloaded from Symon.',
                'netSuite.NetSuiteInvalidOAuthClient'
            )

        if error == 'invalid_grant':
            return SymonException(
                'NetSuite rejected the authentication request. The certificate mapping may have '
                'expired or been removed, or the mapped employee or role may no longer have '
                'access. Recreate the mapping in NetSuite and try again.',
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

"""Tests for OAuth 2.0 client credentials token minting."""
import time

import jwt
import pytest
import responses

from tap_netsuite.netsuite.exceptions import SymonException
from tap_netsuite.netsuite.oauth import (
    CLIENT_ASSERTION_TYPE,
    NetSuiteOAuthTokenManager,
    account_host,
    resolve_account_id,
    service_base_url,
)

from .conftest import CERTIFICATE_ID, CLIENT_ID

TOKEN_URL = 'https://1234567.suitetalk.api.netsuite.com/services/rest/auth/oauth2/v1/token'


def token_response(access_token='access-token', expires_in=3600):
    return {'access_token': access_token, 'expires_in': expires_in, 'token_type': 'Bearer'}


class TestAccountAddressing:
    def test_production_account_is_unchanged(self):
        assert resolve_account_id('1234567', is_sandbox=False) == '1234567'

    def test_sandbox_account_gains_suffix(self):
        assert resolve_account_id('1234567', is_sandbox=True) == '1234567_SB1'

    def test_sandbox_suffix_is_not_applied_twice(self):
        assert resolve_account_id('1234567_SB1', is_sandbox=True) == '1234567_SB1'

    def test_host_lowercases_and_hyphenates(self):
        # NetSuite serves 1234567_SB1 from 1234567-sb1.suitetalk.api.netsuite.com
        assert account_host('1234567_SB1') == '1234567-sb1'

    def test_service_base_url(self):
        assert service_base_url('1234567') == 'https://1234567.suitetalk.api.netsuite.com'

    def test_sandbox_service_base_url(self):
        assert service_base_url('1234567_SB1') == \
            'https://1234567-sb1.suitetalk.api.netsuite.com'


class TestAssertion:
    def test_assertion_is_signed_with_rs256_and_carries_certificate_id_as_kid(
            self, token_manager, public_key):
        assertion = token_manager._build_assertion()

        header = jwt.get_unverified_header(assertion)
        assert header['alg'] == 'RS256'
        assert header['kid'] == CERTIFICATE_ID

        claims = jwt.decode(assertion, public_key, algorithms=['RS256'], audience=TOKEN_URL)
        assert claims['iss'] == CLIENT_ID
        assert claims['aud'] == TOKEN_URL
        assert claims['scope'] == ['rest_webservices']
        assert claims['exp'] - claims['iat'] == 3600

    def test_unusable_private_key_is_reported_as_a_signing_failure(self):
        manager = NetSuiteOAuthTokenManager(
            account_id='1234567',
            client_id=CLIENT_ID,
            certificate_id=CERTIFICATE_ID,
            private_key='not a pem'
        )

        with pytest.raises(SymonException) as excinfo:
            manager._build_assertion()

        assert excinfo.value.code == 'netSuite.NetSuiteAssertionSigningFailed'


class TestMinting:
    @responses.activate
    def test_posts_client_credentials_grant_with_jwt_bearer_assertion(self, token_manager):
        responses.add(responses.POST, TOKEN_URL, json=token_response(), status=200)

        assert token_manager.access_token() == 'access-token'

        request = responses.calls[0].request
        body = dict(pair.split('=', 1) for pair in request.body.split('&'))
        assert body['grant_type'] == 'client_credentials'
        assert body['client_assertion_type'] == CLIENT_ASSERTION_TYPE.replace(':', '%3A')
        assert body['client_assertion']

    @responses.activate
    def test_token_is_cached_between_calls(self, token_manager):
        responses.add(responses.POST, TOKEN_URL, json=token_response(), status=200)

        token_manager.access_token()
        token_manager.access_token()
        token_manager.access_token()

        assert len(responses.calls) == 1

    @responses.activate
    def test_token_is_reminted_once_expired(self, token_manager):
        responses.add(responses.POST, TOKEN_URL, json=token_response('first'), status=200)
        responses.add(responses.POST, TOKEN_URL, json=token_response('second'), status=200)

        assert token_manager.access_token() == 'first'
        token_manager._expires_at = time.time() - 1
        assert token_manager.access_token() == 'second'

    @responses.activate
    def test_expiry_is_held_back_from_the_stated_lifetime(self, token_manager):
        """A token expiring mid-request would fail the call, so it is retired early."""
        responses.add(responses.POST, TOKEN_URL, json=token_response(expires_in=3600), status=200)

        before = time.time()
        token_manager.access_token()

        # 3600s lifetime less the 360s skew.
        assert token_manager._expires_at - before == pytest.approx(3240, abs=5)

    @responses.activate
    def test_invalidate_forces_a_remint(self, token_manager):
        responses.add(responses.POST, TOKEN_URL, json=token_response('first'), status=200)
        responses.add(responses.POST, TOKEN_URL, json=token_response('second'), status=200)

        assert token_manager.access_token() == 'first'
        token_manager.invalidate()
        assert token_manager.access_token() == 'second'

    @responses.activate
    def test_missing_access_token_in_response_is_an_error(self, token_manager):
        responses.add(responses.POST, TOKEN_URL, json={'expires_in': 3600}, status=200)

        with pytest.raises(SymonException) as excinfo:
            token_manager.access_token()

        assert excinfo.value.code == 'netSuite.NetSuiteOAuthFailed'

    @responses.activate
    def test_unparseable_expires_in_falls_back_to_the_default_lifetime(self, token_manager):
        responses.add(responses.POST, TOKEN_URL,
                      json=token_response(expires_in='soon'), status=200)

        token_manager.access_token()

        assert token_manager._expires_at > time.time()


class TestErrorMapping:
    @responses.activate
    @pytest.mark.parametrize('error,expected_code', [
        ('invalid_client', 'netSuite.NetSuiteInvalidOAuthClient'),
        ('invalid_grant', 'netSuite.NetSuiteInvalidOAuthGrant'),
        ('invalid_scope', 'netSuite.NetSuiteInsufficientScope'),
        ('insufficient_scope', 'netSuite.NetSuiteInsufficientScope'),
        ('something_else', 'netSuite.NetSuiteOAuthFailed'),
    ])
    def test_token_errors_map_to_actionable_codes(self, token_manager, error, expected_code):
        responses.add(responses.POST, TOKEN_URL, json={'error': error}, status=400)

        with pytest.raises(SymonException) as excinfo:
            token_manager.access_token()

        assert excinfo.value.code == expected_code

    @responses.activate
    def test_non_json_error_body_is_tolerated(self, token_manager):
        responses.add(responses.POST, TOKEN_URL, body='<html>gateway error</html>', status=502)

        with pytest.raises(SymonException) as excinfo:
            token_manager.access_token()

        assert excinfo.value.code == 'netSuite.NetSuiteOAuthFailed'

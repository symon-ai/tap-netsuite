import os
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

ACCOUNT_ID = '1234567'
CLIENT_ID = 'client-id-abc'
CERTIFICATE_ID = 'certificate-id-xyz'


@pytest.fixture(scope='session')
def rsa_key_pair():
    """A throwaway RSA key pair. Generated once because 2048-bit generation is not free."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode('utf-8')

    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode('utf-8')

    return private_pem, public_pem


@pytest.fixture
def private_key(rsa_key_pair):
    return rsa_key_pair[0]


@pytest.fixture
def public_key(rsa_key_pair):
    return rsa_key_pair[1]


@pytest.fixture
def token_manager(private_key):
    from tap_netsuite.netsuite.oauth import NetSuiteOAuthTokenManager

    return NetSuiteOAuthTokenManager(
        account_id=ACCOUNT_ID,
        client_id=CLIENT_ID,
        certificate_id=CERTIFICATE_ID,
        private_key=private_key
    )


class StubTokenManager:
    """Token manager that hands out a fixed token and counts invalidations."""

    def __init__(self, token='stub-token'):
        self.token = token
        self.calls = 0
        self.invalidations = 0

    def access_token(self):
        self.calls += 1
        return self.token

    def invalidate(self):
        self.invalidations += 1


@pytest.fixture
def stub_token_manager():
    return StubTokenManager()

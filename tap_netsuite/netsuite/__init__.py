#!/usr/bin/env python3
import os
import singer
from typing import Dict
import singer.utils as singer_utils
from singer import metadata, metrics
from tap_netsuite.netsuite.exceptions import SymonException

LOGGER = singer.get_logger()

# Authentication methods, matching the values the connection service stores on the connection.
# The transport follows from the method: Oracle does not support OAuth 2.0 for SOAP web
# services, so OAuth 2.0 connections read through REST/SuiteQL.
AUTH_METHOD_TBA = 'tba'
AUTH_METHOD_OAUTH2 = 'oauth2ClientCredentials'


def _get_abs_path(path: str) -> str:
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), path)


def _load_object_definitions() -> Dict:
    """Loads a JSON schema file for a given
    NetSuite Report resource into a dict representation.
    """
    schema_path = _get_abs_path("schemas")
    return singer.utils.load_json(f"{schema_path}/object_definition.json")


NS_OBJECT_DEFINITIONS = _load_object_definitions()
NS_OBJECTS = NS_OBJECT_DEFINITIONS.keys()


def field_to_property_schema(field):  # pylint:disable=too-many-branches

    number_type = {
        "type": [
            "null",
            "number"
        ]
    }

    string_type = {
        "type": [
            "string",
            "null"
        ]
    }

    boolean_type = {
        "type": [
            "boolean",
            "null"
        ]
    }

    datetime_type = {
        "anyOf": [
            {
                "type": "string",
                "format": "date-time"
            },
            string_type
        ]
    }

    object_type = {
        "type": [
            "null",
            "object"
        ]
    }

    array_type = {
        "type": "array"
    }

    ns_types = {
        "number": number_type,
        "string": string_type,
        "datetime": datetime_type,
        "object": object_type,
        "array": array_type,
        "boolean": boolean_type,
        "object_reference": string_type,
        "email": string_type,
        "address": string_type,
        "metadata": string_type
    }

    ns_type = field['type']
    property_schema = ns_types[ns_type]

    return property_schema


class NetSuite:

    def __init__(self,
                 ns_account=None,
                 ns_consumer_key=None,
                 ns_consumer_secret=None,
                 ns_token_key=None,
                 ns_token_secret=None,
                 is_sandbox=True,
                 select_fields_by_default=None,
                 default_start_date=None,
                 ns_auth_method=None,
                 ns_client_id=None,
                 ns_certificate_id=None,
                 ns_private_key=None):

        self.ns_account = ns_account
        self.ns_consumer_key = ns_consumer_key
        self.ns_consumer_secret = ns_consumer_secret
        self.ns_token_key = ns_token_key
        self.ns_token_secret = ns_token_secret
        self.is_sandbox = is_sandbox

        self.ns_auth_method = ns_auth_method or AUTH_METHOD_TBA
        self.ns_client_id = ns_client_id
        self.ns_certificate_id = ns_certificate_id
        self.ns_private_key = ns_private_key
        self.select_fields_by_default = select_fields_by_default is True or (
                isinstance(select_fields_by_default, str) and select_fields_by_default.lower() == 'true')

        self.default_start_date = default_start_date

        self.data_source = None

        # validate start_date
        if default_start_date is not None:
            singer_utils.strptime(default_start_date)

    def describe(self, sobject=None):
        """Describes all objects or a specific object"""
        if sobject is None:
            return NS_OBJECTS
        else:
            return NS_OBJECT_DEFINITIONS[sobject]

    def connect(self, caching=True):
        """Open a connection using whichever transport the auth method implies.

        The data sources are imported here rather than at module scope so that each one only
        pulls in its own transport stack: the SOAP path needs zeep and the NetSuite SDK, the
        SuiteQL path needs neither.
        """
        if self.ns_auth_method == AUTH_METHOD_OAUTH2:
            from tap_netsuite.netsuite.suiteql_datasource import SuiteQLDataSource

            self.data_source = SuiteQLDataSource(
                NS_OBJECT_DEFINITIONS,
                account=self.ns_account,
                client_id=self.ns_client_id,
                certificate_id=self.ns_certificate_id,
                private_key=self.ns_private_key,
                is_sandbox=self.is_sandbox
            )
        elif self.ns_auth_method == AUTH_METHOD_TBA:
            from tap_netsuite.netsuite.soap_datasource import SoapDataSource

            self.data_source = SoapDataSource(
                NS_OBJECT_DEFINITIONS,
                account=self.ns_account,
                consumer_key=self.ns_consumer_key,
                consumer_secret=self.ns_consumer_secret,
                token_key=self.ns_token_key,
                token_secret=self.ns_token_secret,
                is_sandbox=self.is_sandbox,
                caching=caching
            )
        else:
            raise SymonException(
                f'Unknown NetSuite authentication method: {self.ns_auth_method}.',
                'netSuite.NetSuiteUnknownAuthMethod'
            )

        self.data_source.connect()

    def connect_tba(self, caching=True):
        """Backwards-compatible alias for the token-based authentication path."""
        self.ns_auth_method = AUTH_METHOD_TBA
        self.connect(caching=caching)

    def get_start_date(self, state, catalog_entry):
        catalog_metadata = metadata.to_map(catalog_entry['metadata'])
        replication_key = catalog_metadata.get((), {}).get('replication-key')

        return (singer.get_bookmark(state,
                                    catalog_entry['tap_stream_id'],
                                    replication_key) or self.default_start_date)

    def query(self, catalog_entry, state):
        start_date = self.get_start_date(state, catalog_entry)
        return self.data_source.query_stream(catalog_entry, start_date)

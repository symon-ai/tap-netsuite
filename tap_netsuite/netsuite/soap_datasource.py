"""SOAP SuiteTalk data source, authenticated with token-based authentication (TBA).

This is the legacy transport. NetSuite blocks new TBA integration records from 2027.1, stops
honouring TBA credentials in 2028.1, and removes SOAP web services entirely in 2028.2, so this
path exists only to keep established connections running until customers move to
``SuiteQLDataSource``.
"""
import types

import singer
from zeep.helpers import serialize_object

from .datasource import DataSource, project_record
from .netsuite_connection import ExtendedNetSuiteConnection

LOGGER = singer.get_logger()

SANDBOX_ACCOUNT_SUFFIX = '_SB1'


class SoapDataSource(DataSource):
    def __init__(self,
                 object_definitions,
                 account=None,
                 consumer_key=None,
                 consumer_secret=None,
                 token_key=None,
                 token_secret=None,
                 is_sandbox=False,
                 caching=True):
        super().__init__(object_definitions)

        self.account = account
        if account is not None and is_sandbox is True:
            self.account = account + SANDBOX_ACCOUNT_SUFFIX

        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.token_key = token_key
        self.token_secret = token_secret
        self.caching = caching
        self.client = None

    def connect(self):
        self.client = ExtendedNetSuiteConnection(
            account=self.account,
            consumer_key=self.consumer_key,
            consumer_secret=self.consumer_secret,
            token_key=self.token_key,
            token_secret=self.token_secret,
            caching=self.caching
        )

    def query_stream(self, catalog_entry, start_date):
        stream = catalog_entry['stream']
        display_names = self.display_names(catalog_entry)
        internal_name_by_display_name = self.internal_name_by_display_name(stream)

        result = self.client.query_entity(stream, {
            'searchValue': start_date,
            'type': 'dateTime',
            'operator': 'onOrAfter'
        })

        for page in self._as_pages(result):
            yield [
                project_record(serialize_object(record), display_names, internal_name_by_display_name)
                for record in page
            ]

    @staticmethod
    def _as_pages(result):
        """Normalise the SDK's return value, which is a page generator for paginated entities
        and a bare collection for the rest."""
        if isinstance(result, types.GeneratorType):
            return result
        if result is None:
            return []
        return [result]

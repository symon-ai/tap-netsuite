"""SOAP data source for legacy TBA connections."""
import singer

from .datasource import DataSource
from .netsuite_connection import ExtendedNetSuiteConnection
from .oauth import resolve_account_id

LOGGER = singer.get_logger()


class Soap(DataSource):
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

        self.account = resolve_account_id(account, is_sandbox)

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
        return self.client.query_entity(stream,
                                        {'searchValue': start_date,
                                         'type': 'dateTime',
                                         'operator': 'onOrAfter'})

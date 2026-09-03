"""SuiteQL query execution over the NetSuite REST query service.

The service has hard limits that shape how extraction has to work:

* at most 1,000 rows per response;
* at most 100,000 rows reachable through ``offset``, so offset paging cannot walk a large table;
* 300 seconds per call.

Because of the offset ceiling we paginate by key instead: every query is ordered by its key
column and each page asks for rows beyond the last key seen. That is stable under concurrent
writes as well, which plain offset paging is not.
"""
import time

import requests
import singer

from .exceptions import SymonException, TapNetSuiteQuotaExceededException
from .oauth import service_base_url

LOGGER = singer.get_logger()

QUERY_PATH = '/services/rest/query/v1/suiteql'

# The service rejects anything above 1,000.
MAX_PAGE_SIZE = 1000

# NetSuite's own limit is 300s; allow a little headroom for connection setup.
REQUEST_TIMEOUT_SECONDS = 330

RETRY_STATUS_CODES = (429, 500, 502, 503, 504)
MAX_RETRIES = 5
RETRY_BACKOFF_SECONDS = 2


class SuiteQLQuery:
    """A single stream's query, rendered fresh for each page.

    ``select`` maps result aliases to SuiteQL expressions. Aliases are what come back as JSON
    keys, so they are also the join point with the catalog's field mapping.

    ``key_column`` is the SQL expression ordered and filtered on for keyset pagination, while
    ``key_alias`` is the response key the resulting value is read back from. They differ whenever
    the key is table-qualified, which it always is once joins are involved.
    """

    KEY_ALIAS = 'keycol'

    def __init__(self, select, from_clause, key_column='id', where=None, key_alias=None):
        self.select = dict(select)
        self.from_clause = from_clause
        self.key_column = key_column
        self.key_alias = key_alias or self.KEY_ALIAS
        self.where = list(where or [])

        # Pagination reads the key back out of every row, so make sure it is selected.
        self.select.setdefault(self.key_alias, key_column)

    def select_list(self):
        return ', '.join(
            expression if expression == alias else f'{expression} AS {alias}'
            for alias, expression in self.select.items()
        )

    def render(self, after_key=None):
        predicates = list(self.where)

        if after_key is not None:
            predicates.append(f'{self.key_column} > {_sql_literal(after_key)}')

        sql = f'SELECT {self.select_list()} FROM {self.from_clause}'
        if predicates:
            sql += ' WHERE ' + ' AND '.join(f'({predicate})' for predicate in predicates)
        sql += f' ORDER BY {self.key_column} ASC'

        return sql


def _sql_literal(value):
    """Render a key value as a SuiteQL literal.

    NetSuite returns internal IDs as JSON strings even though the underlying column is numeric,
    so numeric-looking keys are emitted unquoted to keep the comparison on the indexed type.
    """
    try:
        return str(int(value))
    except (TypeError, ValueError):
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'"


def incremental_predicate(column, start_date):
    """Build the ``lastmodifieddate`` filter for an incremental stream.

    NetSuite requires ``TO_DATE`` for date comparisons; a bare string literal forces an implicit
    conversion that defeats the index.
    """
    if not start_date:
        return None

    timestamp = str(start_date).replace('T', ' ').replace('Z', '')
    if '.' in timestamp:
        timestamp = timestamp.split('.', 1)[0]
    if '+' in timestamp:
        timestamp = timestamp.split('+', 1)[0]
    timestamp = timestamp.strip()

    escaped = timestamp.replace("'", "''")
    return f"{column} >= TO_DATE('{escaped}', 'YYYY-MM-DD HH24:MI:SS')"


class SuiteQLClient:
    def __init__(self, account_id, token_manager, session=None, page_size=MAX_PAGE_SIZE):
        self.account_id = account_id
        self.token_manager = token_manager
        self.session = session or requests.Session()
        self.page_size = min(page_size, MAX_PAGE_SIZE)

    @property
    def query_url(self):
        return f'{service_base_url(self.account_id)}{QUERY_PATH}'

    def execute(self, sql):
        """Run one statement and return its rows, without pagination.

        Used for metadata probes where a single page is all that is needed.
        """
        return (self._post(sql).get('items') or [])

    def iter_pages(self, query):
        """Yield pages of rows for a query, paginating by key column."""
        after_key = None
        pages = 0

        while True:
            sql = query.render(after_key=after_key)
            LOGGER.debug('SuiteQL: %s', sql)

            body = self._post(sql)
            items = body.get('items') or []
            if not items:
                return

            yield items
            pages += 1

            if len(items) < self.page_size:
                return

            next_key = items[-1].get(query.key_alias)
            if next_key is None:
                # Without a key we cannot advance safely; stopping beats silently looping on the
                # same page forever.
                LOGGER.warning(
                    'SuiteQL response is missing key column %s; stopping pagination after %s pages',
                    query.key_alias, pages
                )
                return

            after_key = next_key

    def _post(self, sql, attempt=1):
        headers = {
            'Authorization': f'Bearer {self.token_manager.access_token()}',
            'Content-Type': 'application/json',
            'Prefer': 'transient'
        }

        try:
            response = self.session.post(
                self.query_url,
                params={'limit': self.page_size, 'offset': 0},
                json={'q': sql},
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS
            )
        except requests.exceptions.Timeout as e:
            raise SymonException(
                'The NetSuite query timed out. Narrowing the import start date usually resolves '
                'this by reducing the amount of data scanned.',
                'netSuite.NetSuiteQueryTimeout'
            ) from e

        if response.status_code == 200:
            return response.json()

        if response.status_code == 401:
            # The token was rejected before our computed expiry. Re-mint once, then give up.
            if attempt == 1:
                LOGGER.info('NetSuite rejected the access token; re-minting and retrying')
                self.token_manager.invalidate()
                return self._post(sql, attempt=attempt + 1)
            raise SymonException(
                'NetSuite rejected the access token. Please reconnect and try again.',
                'netSuite.NetSuiteOAuthFailed'
            )

        if response.status_code in RETRY_STATUS_CODES and attempt <= MAX_RETRIES:
            delay = RETRY_BACKOFF_SECONDS ** attempt
            LOGGER.info(
                'NetSuite returned %s; retrying in %ss (attempt %s of %s)',
                response.status_code, delay, attempt, MAX_RETRIES
            )
            time.sleep(delay)
            return self._post(sql, attempt=attempt + 1)

        raise self._query_error(response, sql)

    @staticmethod
    def _query_error(response, sql):
        try:
            body = response.json()
        except ValueError:
            body = {}

        detail = body.get('o:errorDetails') or []
        detail_text = '; '.join(filter(None, (item.get('detail') for item in detail))) or body.get('detail', '')
        LOGGER.error('SuiteQL request failed (%s): %s | query: %s', response.status_code, detail_text, sql)

        if response.status_code == 429:
            return TapNetSuiteQuotaExceededException(
                f'NetSuite rejected the request for exceeding its concurrency limits: {detail_text}'
            )

        if response.status_code == 403:
            return SymonException(
                'The NetSuite role mapped to this integration does not have permission to run '
                'SuiteQL queries or to read this record type. Check the role permissions and the '
                'REST Web Services scope on the integration record.',
                'netSuite.NetSuiteInsufficientPermissions'
            )

        if response.status_code == 400:
            return SymonException(
                f'NetSuite rejected the query: {detail_text}',
                'netSuite.NetSuiteInvalidQuery'
            )

        return SymonException(
            f'NetSuite returned an unexpected error ({response.status_code}): {detail_text}',
            'netSuite.NetSuiteQueryFailed'
        )

"""Execute paginated SuiteQL queries over REST."""
import time

import requests
import singer
import singer.utils as singer_utils

from .exceptions import SymonException, TapNetSuiteQuotaExceededException
from .oauth import service_base_url

LOGGER = singer.get_logger()

QUERY_PATH = '/services/rest/query/v1/suiteql'

# NetSuite allows up to 1,000 rows per response.
MAX_PAGE_SIZE = 1000

# Slightly above NetSuite's 300-second server limit so its error response arrives first.
REQUEST_TIMEOUT_SECONDS = 330

RETRY_STATUS_CODES = (429, 500, 502, 503, 504)
MAX_RETRIES = 3
MAX_TIMEOUT_RETRIES = 1
RETRY_BACKOFF_SECONDS = 2
# Concurrency slots are shared with the account's other integrations, so wait longer on 429.
RATE_LIMIT_BACKOFF_SECONDS = 15
MAX_RETRY_AFTER_SECONDS = 120


def _retry_delay(response, attempt):
    """Return how long to wait before retrying, honoring Retry-After when present."""
    headers = getattr(response, 'headers', None) or {}
    try:
        retry_after = int(headers.get('Retry-After'))
    except (TypeError, ValueError):
        retry_after = None
    if retry_after is not None and retry_after >= 0:
        return min(retry_after, MAX_RETRY_AFTER_SECONDS)

    if response.status_code == 429:
        return RATE_LIMIT_BACKOFF_SECONDS * attempt
    return RETRY_BACKOFF_SECONDS ** attempt


class SuiteQLQuery:
    """Build SuiteQL statements for one stream."""

    KEY_ALIAS = 'keycol'
    TIEBREAK_ALIAS = 'tiebreakcol'

    def __init__(self, select, from_clause, key_column='id', where=None, key_alias=None,
                 tiebreak_column=None):
        self.select = dict(select)
        self.from_clause = from_clause
        self.key_column = key_column
        self.key_alias = key_alias or self.KEY_ALIAS
        # Set when key_column is not unique on its own, e.g. transactionline.id
        # is only unique within a transaction.
        self.tiebreak_column = tiebreak_column
        self.where = list(where or [])

        # Include the key needed for pagination.
        self.select.setdefault(self.key_alias, key_column)
        if tiebreak_column:
            self.select.setdefault(self.TIEBREAK_ALIAS, tiebreak_column)

    def next_key(self, row):
        """Return the pagination key of a row, or None if it is missing."""
        key = row.get(self.key_alias)
        if not self.tiebreak_column:
            return key
        tiebreak = row.get(self.TIEBREAK_ALIAS)
        if key is None or tiebreak is None:
            return None
        return (key, tiebreak)

    def select_list(self):
        return ', '.join(
            expression if expression == alias else f'{expression} AS {alias}'
            for alias, expression in self.select.items()
        )

    def render(self, after_key=None):
        predicates = list(self.where)

        if after_key is not None and self.tiebreak_column:
            key, tiebreak = after_key
            key_literal = sql_literal(key)
            predicates.append(
                f'{self.key_column} > {key_literal} OR '
                f'({self.key_column} = {key_literal} AND {self.tiebreak_column} > {sql_literal(tiebreak)})'
            )
        elif after_key is not None:
            predicates.append(f'{self.key_column} > {sql_literal(after_key)}')

        sql = f'SELECT {self.select_list()} FROM {self.from_clause}'
        if predicates:
            sql += ' WHERE ' + ' AND '.join(f'({predicate})' for predicate in predicates)
        sql += f' ORDER BY {self.key_column} ASC'
        if self.tiebreak_column:
            sql += f', {self.tiebreak_column} ASC'

        return sql


def sql_literal(value):
    """Format a value for a SuiteQL query."""
    try:
        return str(int(value))
    except (TypeError, ValueError):
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'"


def incremental_predicate(column, start_date):
    """Build a last-modified filter."""
    if not start_date:
        return None

    # TO_DATE has no time-zone component, so normalize to UTC.
    timestamp = singer_utils.strptime_to_utc(str(start_date)).strftime('%Y-%m-%d %H:%M:%S')
    return f"{column} >= TO_DATE('{timestamp}', 'YYYY-MM-DD HH24:MI:SS')"


class SuiteQLClient:
    def __init__(self, account_id, token_manager, session=None, page_size=MAX_PAGE_SIZE):
        self.account_id = account_id
        self.token_manager = token_manager
        self.session = session or requests.Session()
        self.page_size = min(page_size, MAX_PAGE_SIZE)

    @property
    def query_url(self):
        return f'{service_base_url(self.account_id)}{QUERY_PATH}'

    def execute(self, sql, limit=None):
        """Run one query without pagination."""
        return (self._post(sql, limit=limit).get('items') or [])

    def iter_pages(self, query):
        """Yield keyset-paginated pages."""
        after_key = None

        while True:
            sql = query.render(after_key=after_key)
            LOGGER.debug('SuiteQL: %s', sql)

            body = self._post(sql)
            items = body.get('items') or []
            if not items:
                return

            yield items

            if len(items) < self.page_size:
                return

            next_key = query.next_key(items[-1])
            if next_key is None:
                raise SymonException(
                    f'NetSuite returned a page without the {query.key_alias} pagination column, '
                    'so the remaining rows could not be read.',
                    'netSuite.NetSuiteQueryFailed'
                )

            after_key = next_key

    def _post(self, sql, limit=None):
        limit = min(limit or self.page_size, MAX_PAGE_SIZE)
        token_refreshed = False
        retries = 0
        timeouts = 0

        while True:
            headers = {
                'Authorization': f'Bearer {self.token_manager.access_token()}',
                'Content-Type': 'application/json',
                'Prefer': 'transient'
            }

            try:
                response = self.session.post(
                    self.query_url,
                    params={'limit': limit, 'offset': 0},
                    json={'q': sql},
                    headers=headers,
                    timeout=REQUEST_TIMEOUT_SECONDS
                )
            except requests.exceptions.Timeout as e:
                if timeouts < MAX_TIMEOUT_RETRIES:
                    timeouts += 1
                    delay = RETRY_BACKOFF_SECONDS ** timeouts
                    LOGGER.info('NetSuite query timed out; retrying in %ss (attempt %s of %s)',
                                delay, timeouts, MAX_TIMEOUT_RETRIES)
                    time.sleep(delay)
                    continue
                raise SymonException(
                    'The NetSuite query timed out. Narrowing the import start date usually resolves '
                    'this by reducing the amount of data scanned.',
                    'netSuite.NetSuiteQueryTimeout'
                ) from e

            if response.status_code == 200:
                return response.json()

            if response.status_code == 401:
                # Refresh once if NetSuite rejects the token.
                if not token_refreshed:
                    token_refreshed = True
                    LOGGER.info('NetSuite rejected the access token; re-minting and retrying')
                    self.token_manager.invalidate()
                    continue
                raise SymonException(
                    'NetSuite rejected the access token. Please reconnect and try again.',
                    'netSuite.NetSuiteOAuthFailed'
                )

            if response.status_code in RETRY_STATUS_CODES and retries < MAX_RETRIES:
                retries += 1
                delay = _retry_delay(response, retries)
                LOGGER.info(
                    'NetSuite returned %s; retrying in %ss (attempt %s of %s)',
                    response.status_code, delay, retries, MAX_RETRIES
                )
                time.sleep(delay)
                continue

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
            message = (
                'The NetSuite role mapped to this integration does not have permission to run '
                'SuiteQL queries or to read this record type. Check the role permissions and the '
                'REST Web Services scope on the integration record.'
            )
            if detail_text:
                message += f' NetSuite reported: {detail_text}'
            return SymonException(message, 'netSuite.NetSuiteInsufficientPermissions')

        if response.status_code == 400:
            return SymonException(
                f'NetSuite rejected the query: {detail_text}',
                'netSuite.NetSuiteInvalidQuery'
            )

        return SymonException(
            f'NetSuite returned an unexpected error ({response.status_code}): {detail_text}',
            'netSuite.NetSuiteQueryFailed'
        )

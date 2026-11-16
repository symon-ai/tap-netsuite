"""Execute paginated SuiteQL queries over REST."""
import time

import requests
import singer
import singer.utils as singer_utils

from .exceptions import SymonException, TapNetSuiteQuotaExceededException
from .oauth import service_base_url

LOGGER = singer.get_logger()

QUERY_PATH = '/services/rest/query/v1/suiteql'
METADATA_CATALOG_PATH = '/services/rest/record/v1/metadata-catalog'

# NetSuite allows up to 1,000 rows per response.
MAX_PAGE_SIZE = 1000

# Slightly above NetSuite's 300-second server limit so its error response arrives first.
REQUEST_TIMEOUT_SECONDS = 330

RETRY_STATUS_CODES = (429, 500, 502, 503, 504)
MAX_RETRIES = 3
MAX_TIMEOUT_RETRIES = 1
# First attempt, one token refresh, and every timeout and status retry.
MAX_ATTEMPTS = 1 + 1 + MAX_TIMEOUT_RETRIES + MAX_RETRIES
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
    """Build SuiteQL statements for one stream.

    `select` maps a short output alias to a SQL expression. Short aliases keep the
    SQL small and let us read each value back from the response row by alias.

    Example for Customer, selecting Name, Subsidiary and Last Modified Date with a
    start date, on the second page (after_key=1000):

        SELECT c.entityid AS c0,
               c.subsidiary AS c1, r1.name AS n1,
               TO_CHAR(c.lastmodifieddate, 'YYYY-MM-DD HH24:MI:SS') AS c2,
               c.id AS keycol
        FROM customer c
        LEFT JOIN subsidiary r1 ON r1.id = c.subsidiary
        WHERE (c.lastmodifieddate >= TO_DATE('2026-01-01 00:00:00', 'YYYY-MM-DD HH24:MI:SS'))
          AND (c.id > 1000)
        ORDER BY c.id ASC

    render() returns this on a single line; it is wrapped here for readability.
    """

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

        # Always select the pagination key (and tiebreak) so the last row of each page
        # tells us where the next page starts, e.g. `c.id AS keycol`.
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
        # {'c0': 'c.entityid', 'id': 'id'} -> "c.entityid AS c0, id"
        return ', '.join(
            expression if expression == alias else f'{expression} AS {alias}'
            for alias, expression in self.select.items()
        )

    def render(self, after_key=None):
        predicates = list(self.where)

        # Keyset pagination: instead of OFFSET, each page asks for rows whose key is
        # greater than the last key already read. after_key is None on the first page.
        if after_key is not None and self.tiebreak_column:
            # With a tiebreak, the key alone repeats across rows, so continue from the
            # (key, tiebreak) pair: rows with a later key, or the same key and a later tiebreak.
            #   (tl.transaction > 102 OR (tl.transaction = 102 AND tl.id > 7))
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
            # Parenthesize each predicate so an OR inside one can't leak into the others.
            sql += ' WHERE ' + ' AND '.join(f'({predicate})' for predicate in predicates)
        # The ORDER BY must match the pagination predicate, or pages would skip or repeat rows.
        sql += f' ORDER BY {self.key_column} ASC'
        if self.tiebreak_column:
            sql += f', {self.tiebreak_column} ASC'

        return sql


def sql_literal(value):
    """Format a value for a SuiteQL query."""
    # Numbers go in bare (1000); anything else is quoted with ' doubled ('O''Brien').
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

    def execute(self, sql, limit=None, retry_server_errors=True):
        """Run one query without pagination."""
        return (self._post(sql, limit=limit, retry_server_errors=retry_server_errors).get('items') or [])

    def record_field_names(self, record_type):
        """Return every field NetSuite defines for a REST record type, including custom fields."""
        response = self.session.get(
            f'{service_base_url(self.account_id)}{METADATA_CATALOG_PATH}/{record_type}',
            headers={
                'Authorization': f'Bearer {self.token_manager.access_token()}',
                # JSON Schema lists each field under "properties", whether or not any row has a value.
                'Accept': 'application/schema+json'
            },
            timeout=REQUEST_TIMEOUT_SECONDS
        )
        if response.status_code != 200:
            raise SymonException(
                f'NetSuite returned {response.status_code} for the {record_type} record metadata.',
                'netSuite.NetSuiteQueryFailed'
            )
        return list((response.json().get('properties') or {}).keys())

    def iter_pages(self, query):
        """Yield keyset-paginated pages."""
        # Each loop yields one page of up to page_size rows, then requests the page
        # starting after the last row's key. Stops when NetSuite returns no rows.
        items = self._fetch_page(query, after_key=None)
        while items:
            yield items

            # A short page means NetSuite has nothing left; skip the extra empty request.
            if len(items) < self.page_size:
                return

            # Rows are sorted by key, so the last row holds the largest key read so far.
            next_key = query.next_key(items[-1])
            if next_key is None:
                raise SymonException(
                    f'NetSuite returned a page without the {query.key_alias} pagination column, '
                    'so the remaining rows could not be read.',
                    'netSuite.NetSuiteQueryFailed'
                )

            items = self._fetch_page(query, after_key=next_key)

    def _fetch_page(self, query, after_key):
        sql = query.render(after_key=after_key)
        LOGGER.debug('SuiteQL: %s', sql)
        return self._post(sql).get('items') or []

    def _post(self, sql, limit=None, retry_server_errors=True):
        limit = min(limit or self.page_size, MAX_PAGE_SIZE)
        token_refreshed = False
        retries = 0
        timeouts = 0

        # Retry loop. Each failure type has its own budget: one token refresh on 401,
        # MAX_TIMEOUT_RETRIES on timeouts, and MAX_RETRIES on 429/5xx. Anything else raises,
        # so every path returns or raises within MAX_ATTEMPTS.
        for _ in range(MAX_ATTEMPTS):
            # Built on every attempt so a refreshed token is picked up after a 401.
            headers = {
                'Authorization': f'Bearer {self.token_manager.access_token()}',
                'Content-Type': 'application/json',
                # Required by the SuiteQL REST endpoint.
                'Prefer': 'transient'
            }

            try:
                response = self.session.post(
                    self.query_url,
                    # offset stays 0: paging is done in the WHERE clause (see SuiteQLQuery.render),
                    # because NetSuite's offset paging gets slower and is capped on large tables.
                    params={'limit': limit, 'offset': 0},
                    json={'q': sql},
                    headers=headers,
                    timeout=REQUEST_TIMEOUT_SECONDS
                )
            except requests.exceptions.Timeout as e:
                if timeouts < MAX_TIMEOUT_RETRIES:
                    timeouts += 1
                    delay = RETRY_BACKOFF_SECONDS ** timeouts
                    LOGGER.info('NetSuite query timed out; retrying in %ss', delay)
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
                    self.token_manager.invalidate()
                    continue
                raise SymonException(
                    'NetSuite rejected the access token. Please reconnect and try again.',
                    'netSuite.NetSuiteOAuthFailed'
                )

            retryable = response.status_code in RETRY_STATUS_CODES and (
                retry_server_errors or response.status_code == 429)
            if retryable and retries < MAX_RETRIES:
                retries += 1
                delay = _retry_delay(response, retries)
                LOGGER.info('NetSuite returned %s; retrying in %ss', response.status_code, delay)
                time.sleep(delay)
                continue

            # Not retryable (or out of retries): convert to a user-facing error.
            raise self._query_error(response, sql)

        raise SymonException(
            'NetSuite did not return a response after repeated retries.',
            'netSuite.NetSuiteQueryFailed'
        )

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
            # The tap builds the SQL, so the user can't act on NetSuite's detail; it's logged above.
            return SymonException(
                'NetSuite could not run the import query for this record type. Please contact support.',
                'netSuite.NetSuiteInvalidQuery'
            )

        return SymonException(
            f'NetSuite returned an unexpected error ({response.status_code}): {detail_text}',
            'netSuite.NetSuiteQueryFailed'
        )

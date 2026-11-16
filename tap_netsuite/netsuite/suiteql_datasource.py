"""REST/SuiteQL data source for OAuth 2.0 connections."""
import datetime
import os
import re
from collections import defaultdict

import requests
import singer

from .datasource import DataSource
from .exceptions import SymonException, TapNetSuiteException
from .oauth import NetSuiteOAuthAuthorizationCodeTokenManager, resolve_account_id
from .suiteql import MAX_PAGE_SIZE, SuiteQLClient, SuiteQLQuery, incremental_predicate, sql_literal

LOGGER = singer.get_logger()

BARE_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

# Keep IN lists below Oracle's 1,000-item limit.
SUBLIST_BATCH_SIZE = 500

# Rows sampled to discover custom columns; REST omits null fields, so one row is not enough.
CUSTOM_COLUMN_SAMPLE_SIZE = 200

SUITEQL_DATETIME_FORMAT = 'YYYY-MM-DD HH24:MI:SS'
PYTHON_DATETIME_FORMAT = '%Y-%m-%d %H:%M:%S'

# Errors meaning NetSuite rejected the query itself, so a selected column can't be read.
COLUMN_REJECTED_CODES = ('netSuite.NetSuiteQueryFailed', 'netSuite.NetSuiteInvalidQuery')

STREAM_METADATA_FILENAME = 'suiteql_streams.json'
TRANSACTION_LINE_TABLE = 'transactionline'
TRANSACTION_LINE_ALIAS = 'tl'


def _load_stream_metadata():
    schema_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'schemas')
    return singer.utils.load_json(os.path.join(schema_path, STREAM_METADATA_FILENAME))


_RAW_STREAM_METADATA = _load_stream_metadata()

SUITEQL_STREAM_METADATA = {
    stream: value for stream, value in _RAW_STREAM_METADATA.items()
    if not stream.startswith('_')
}
# [SOAP line field, transactionline column, kind]; kind is column, ref, boolean or signed.
TRANSACTION_LINE_FIELDS = _RAW_STREAM_METADATA.get('_transactionLineFields', [])
MAINLINE_ALIAS = 'ml'


def qualify(expression, alias):
    """Qualify a bare column with a table alias."""
    if BARE_IDENTIFIER.match(expression):
        return f'{alias}.{expression}'
    return expression


def _suiteql_boolean(value):
    """Convert SuiteQL's 'T'/'F' strings to booleans; bool('F') would be True."""
    if isinstance(value, str) and value.upper() in ('T', 'F'):
        return value.upper() == 'T'
    return value


# SuiteQL returns short codes for enum columns; SOAP returns the enum names.
VALUE_MAPS = {
    'accountType': {
        'AcctPay': '_accountsPayable',
        'AcctRec': '_accountsReceivable',
        'Bank': '_bank',
        'COGS': '_costOfGoodsSold',
        'CredCard': '_creditCard',
        'DeferExpense': '_deferredExpense',
        'DeferRevenue': '_deferredRevenue',
        'Equity': '_equity',
        'Expense': '_expense',
        'FixedAsset': '_fixedAsset',
        'Income': '_income',
        'LongTermLiab': '_longTermLiability',
        'NonPosting': '_nonPosting',
        'OthAsset': '_otherAsset',
        'OthCurrAsset': '_otherCurrentAsset',
        'OthCurrLiab': '_otherCurrentLiability',
        'OthExpense': '_otherExpense',
        'OthIncome': '_otherIncome',
        'Stat': '_statistical',
        'UnbilledRec': '_unbilledReceivable',
    },
    # Boolean fields typed as string in the schema; SOAP's True/False ends up as 'True'/'False'.
    'booleanString': {
        'T': 'True',
        'F': 'False',
    },
}


def _strip_display_prefix(value):
    """BUILTIN.DF on transaction status returns 'Customer Payment : Deposited'; SOAP returns 'Deposited'."""
    if isinstance(value, str) and ' : ' in value:
        return value.rsplit(' : ', 1)[1]
    return value


def _batches(values, size):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _in_list(values):
    return ', '.join(sql_literal(value) for value in values)


def _utc_datetime_expr(expression):
    """Render a datetime column in a fixed format; the account is configured for UTC."""
    return f"TO_CHAR({expression}, '{SUITEQL_DATETIME_FORMAT}')"


def _utc_isoformat(value):
    """Convert a TO_CHAR datetime to an ISO 8601 UTC string."""
    if not value:
        return None
    try:
        parsed = datetime.datetime.strptime(value, PYTHON_DATETIME_FORMAT)
    except (TypeError, ValueError):
        return value
    return parsed.replace(tzinfo=datetime.timezone.utc).isoformat()


class SuiteQLDataSource(DataSource):
    def __init__(self,
                 object_definitions,
                 account=None,
                 client_id=None,
                 access_token=None,
                 client_secret=None,
                 refresh_token=None,
                 is_sandbox=False,
                 page_size=MAX_PAGE_SIZE):
        super().__init__(object_definitions)

        self.account_id = resolve_account_id(account, is_sandbox)
        self.client_id = client_id
        self.access_token = access_token
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.page_size = page_size

        self.token_manager = None
        self.client = None
        self._custom_columns_by_table = {}

    def connect(self):
        missing = [
            name for name, value in (
                ('account ID', self.account_id),
                ('client ID', self.client_id),
                ('client secret', self.client_secret),
                ('access token', self.access_token),
                ('refresh token', self.refresh_token)
            ) if not value
        ]
        if missing:
            raise SymonException(
                f'The NetSuite connection is missing the {", ".join(missing)}. '
                'Please reconnect and try again.',
                'netSuite.NetSuiteMissingOAuthConfig'
            )

        self.token_manager = NetSuiteOAuthAuthorizationCodeTokenManager(
            account_id=self.account_id,
            client_id=self.client_id,
            client_secret=self.client_secret,
            refresh_token=self.refresh_token,
            access_token=self.access_token
        )
        self.client = SuiteQLClient(
            account_id=self.account_id,
            token_manager=self.token_manager,
            page_size=self.page_size
        )

    def stream_metadata(self, stream):
        try:
            return SUITEQL_STREAM_METADATA[stream]
        except KeyError:
            raise SymonException(
                f'The stream {stream} is not available over NetSuite REST web services.',
                'netSuite.NetSuiteStreamUnsupported'
            ) from None

    def query_stream(self, catalog_entry, start_date):
        stream = catalog_entry['stream']
        metadata = self.stream_metadata(stream)
        display_names = self.display_names(catalog_entry)
        plan = self._build_plan(stream, metadata, display_names, start_date)

        if plan['optional'] and not self._query_runs(plan['query']):
            LOGGER.warning('%s: NetSuite could not read %s on this account; they will be null',
                           stream, ', '.join(sorted(plan['optional'])))
            display_names = [name for name in display_names if name not in plan['optional']]
            plan = self._build_plan(stream, metadata, display_names, start_date)

        for page in self.client.iter_pages(plan['query']):
            yield self._assemble_page(page, plan)

    def _query_runs(self, query):
        """Run one row of the query to check that every selected column is readable."""
        try:
            self.client.execute(query.render(), limit=1)
        except SymonException as e:
            # Some columns exist but fail on certain accounts (400, or 500 after retries);
            # a 403 or timeout is a real error.
            if e.code in COLUMN_REJECTED_CODES:
                return False
            raise
        return True

    def _build_plan(self, stream, metadata, display_names, start_date):
        """Build a SuiteQL query from catalog fields.

        Returns the query plus a "plan": for each catalog field, which output alias
        holds its value and how to convert it. _assemble_page uses the plan to turn
        each SuiteQL row back into a SOAP-shaped record.

        Output aliases encode the field's position in display_names:
            c{i}        plain column, or the ID of a reference    (c0)
            n{i}        display name of a reference               (n1)
            r{i}        table alias of a reference's JOIN         (r1)
            j{i}        table alias of a joinColumn's JOIN        (j4)
            ml          the transaction's main line, shared by all mainlineRef fields
            f{i}x{j}    j-th custom column of a custom-field list (f5x0)

        See SuiteQLQuery for an example of the resulting SQL.
        """
        alias = metadata['alias']
        fields_by_display_name = {
            field['displayName']: field
            for field in self.object_definitions[stream]
        }

        # SQL being built: output alias -> expression, and LEFT JOIN clauses.
        select = {}
        joins = []
        # Plan for _assemble_page, keyed by catalog display name.
        columns = {}
        boolean_columns = set()
        datetime_columns = set()
        prefixed_columns = set()
        value_maps = {}
        refs = {}
        custom_field_targets = {}
        line_targets = {}
        unsupported = []
        optional = []

        for index, display_name in enumerate(display_names):
            field = fields_by_display_name.get(display_name) or {}
            # From suiteql_streams.json "fields"; object_definition.json only has the name and type.
            expression = metadata.get('fields', {}).get(display_name)
            if expression is None:
                unsupported.append(display_name)
                continue

            kind = expression['kind']
            if expression.get('optional'):
                optional.append(display_name)

            if kind == 'column':
                # A value read straight from the main table: `c.entityid AS c0`.
                key = f'c{index}'
                column_expr = qualify(expression['expr'], alias)
                if expression.get('display'):
                    # Display text instead of the stored code: BUILTIN.DF(t.status) AS c3.
                    column_expr = f'BUILTIN.DF({column_expr})'
                    if expression.get('stripPrefix'):
                        prefixed_columns.add(display_name)
                field_type = field.get('type')
                if field_type == 'datetime':
                    # Selected as text with TO_CHAR so the format is predictable; converted
                    # to ISO 8601 in _assemble_page.
                    column_expr = _utc_datetime_expr(column_expr)
                    datetime_columns.add(display_name)
                select[key] = column_expr
                columns[display_name] = key
                if field_type == 'boolean':
                    boolean_columns.add(display_name)
                if 'valueMap' in expression:
                    value_maps[display_name] = VALUE_MAPS[expression['valueMap']]

            elif kind == 'ref':
                # A reference to another record. SOAP returns these as {internalId, name},
                # so select both the ID and a display name.
                id_key = f'c{index}'
                name_key = f'n{index}'
                select[id_key] = f'{alias}.{expression["column"]}'
                if 'table' in expression:
                    # Name comes from a joined table:
                    #   c.subsidiary AS c1, r1.name AS n1 ... LEFT JOIN subsidiary r1 ON r1.id = c.subsidiary
                    # LEFT JOIN keeps rows whose reference is empty.
                    join_alias = f'r{index}'
                    select[name_key] = f'{join_alias}.{expression["nameColumn"]}'
                    joins.append(
                        f'LEFT JOIN {expression["table"]} {join_alias} '
                        f'ON {join_alias}.id = {alias}.{expression["column"]}'
                    )
                else:
                    # No table to join (e.g. list values); BUILTIN.DF returns NetSuite's
                    # display text for the ID: BUILTIN.DF(c.entitystatus) AS n2.
                    select[name_key] = f'BUILTIN.DF({alias}.{expression["column"]})'
                refs[display_name] = (id_key, name_key)

            elif kind == 'mainlineRef':
                # Header department/class/location live on the transaction's main line:
                #   ml.department AS c3, BUILTIN.DF(ml.department) AS n3
                #   ... LEFT JOIN transactionline ml ON ml.transaction = t.id AND ml.mainline = 'T'
                id_key = f'c{index}'
                name_key = f'n{index}'
                column = f'{MAINLINE_ALIAS}.{expression["column"]}'
                select[id_key] = column
                select[name_key] = f'BUILTIN.DF({column})'
                mainline_join = (
                    f'LEFT JOIN {TRANSACTION_LINE_TABLE} {MAINLINE_ALIAS} '
                    f"ON {MAINLINE_ALIAS}.transaction = {alias}.id AND {MAINLINE_ALIAS}.mainline = 'T'"
                )
                if mainline_join not in joins:
                    joins.append(mainline_join)
                refs[display_name] = (id_key, name_key)

            elif kind == 'joinColumn':
                # A value read from a related record:
                #   j4.balance AS c4 ... LEFT JOIN customer j4 ON j4.id = t.entity
                key = f'c{index}'
                join_alias = f'j{index}'
                select[key] = f'{join_alias}.{expression["expr"]}'
                joins.append(
                    f'LEFT JOIN {expression["table"]} {join_alias} '
                    f'ON {join_alias}.id = {alias}.{expression["column"]}'
                )
                columns[display_name] = key

            elif kind == 'customFields':
                # Custom fields are ordinary columns with an account-specific prefix
                # (e.g. custentity_region), so find them first, then select each one:
                #   c.custentity_region AS f5x0, c.custentity_tier AS f5x1
                custom_columns = self._custom_columns(
                    metadata['table'], expression['prefix'], alias, metadata.get('where', []),
                    metadata.get('recordTypes') or metadata.get('recordType')
                )
                keys = {}
                for offset, column in enumerate(custom_columns):
                    key = f'f{index}x{offset}'
                    select[key] = f'{alias}.{column}'
                    keys[column] = key
                custom_field_targets[display_name] = keys

            elif kind == 'transactionLines':
                # Nothing selected here: lines are fetched per page in a second query
                # (see _attach_transaction_lines).
                line_targets[display_name] = expression.get('wrapper', 'line')

            else:
                unsupported.append(display_name)

        if unsupported:
            LOGGER.info('%s: fields without a SuiteQL mapping will be null: %s',
                        stream, ', '.join(sorted(unsupported)))

        key_column = qualify(metadata['keyColumn'], alias)

        # Static filters from suiteql_streams.json, plus the incremental start date:
        #   c.lastmodifieddate >= TO_DATE('2026-01-01 00:00:00', 'YYYY-MM-DD HH24:MI:SS')
        where = list(metadata.get('where', []))
        replication_column = metadata.get('replication')
        if replication_column and start_date:
            predicate = incremental_predicate(qualify(replication_column, alias), start_date)
            if predicate:
                where.append(predicate)

        from_clause = f'{metadata["table"]} {alias}'
        if joins:
            from_clause += ' ' + ' '.join(joins)

        query = SuiteQLQuery(
            select=select,
            from_clause=from_clause,
            key_column=key_column,
            where=where
        )

        return {
            'query': query,
            'columns': columns,
            'booleanColumns': boolean_columns,
            'datetimeColumns': datetime_columns,
            'prefixedColumns': prefixed_columns,
            'valueMaps': value_maps,
            'refs': refs,
            'customFields': custom_field_targets,
            'lines': line_targets,
            # Sales transactions store line amounts negated; SOAP shows them positive.
            'lineSign': metadata.get('lineSign', 1),
            'unsupported': unsupported,
            # Columns that fail on some accounts; query_stream drops them if the query can't run.
            'optional': optional
        }

    def _assemble_page(self, rows, plan):
        """Build SOAP-compatible records from SuiteQL rows."""
        key_alias = plan['query'].key_alias
        records = []

        for row in rows:
            record = {}

            for display_name, key in plan['columns'].items():
                value = row.get(key)
                if display_name in plan['booleanColumns']:
                    value = _suiteql_boolean(value)
                elif display_name in plan['datetimeColumns']:
                    value = _utc_isoformat(value)
                elif display_name in plan['prefixedColumns']:
                    value = _strip_display_prefix(value)
                elif display_name in plan['valueMaps']:
                    value = plan['valueMaps'][display_name].get(value, value)
                record[display_name] = value

            for display_name, (id_key, name_key) in plan['refs'].items():
                record[display_name] = self._build_ref(row.get(id_key), row.get(name_key))

            for display_name, keys in plan['customFields'].items():
                custom_fields = [
                    {'scriptId': column, 'value': row.get(key)}
                    for column, key in keys.items()
                    if row.get(key) is not None
                ]
                record[display_name] = {'customField': custom_fields} if custom_fields else None

            # Keep each record's key so transaction lines can be matched back to it.
            records.append((row.get(key_alias), record))

        if plan['lines']:
            self._attach_transaction_lines(records, plan['lines'], plan.get('lineSign', 1))

        return [record for _, record in records]

    def _attach_transaction_lines(self, records, line_targets, sign=1):
        """Fetch the line items for one page of transactions and attach them in SOAP's shape.

        Runs one query per batch of up to SUBLIST_BATCH_SIZE transaction IDs:

            SELECT tl.transaction AS parenttxn, tl.item AS l0, BUILTIN.DF(tl.item) AS ln0,
                   tl.linesequencenumber AS l1, ..., tl.transaction AS keycol, tl.id AS tiebreakcol
            FROM transactionline tl
            WHERE (tl.transaction IN (101, 102, 103)) AND (tl.mainline = 'F') AND (tl.taxline = 'F')
            ORDER BY tl.transaction ASC, tl.id ASC

        Line IDs repeat across transactions, so pagination uses (transaction, id).
        """
        keys = [key for key, _ in records if key is not None]
        lines_by_key = defaultdict(list)

        if keys and TRANSACTION_LINE_FIELDS:
            select = {'parenttxn': f'{TRANSACTION_LINE_ALIAS}.transaction'}
            for index, (_, column, kind) in enumerate(TRANSACTION_LINE_FIELDS):
                select[f'l{index}'] = f'{TRANSACTION_LINE_ALIAS}.{column}'
                if kind == 'ref':
                    select[f'ln{index}'] = f'BUILTIN.DF({TRANSACTION_LINE_ALIAS}.{column})'

            for batch in _batches(keys, SUBLIST_BATCH_SIZE):
                query = SuiteQLQuery(
                    select=select,
                    from_clause=f'{TRANSACTION_LINE_TABLE} {TRANSACTION_LINE_ALIAS}',
                    key_column=f'{TRANSACTION_LINE_ALIAS}.transaction',
                    tiebreak_column=f'{TRANSACTION_LINE_ALIAS}.id',
                    where=[
                        f'{TRANSACTION_LINE_ALIAS}.transaction IN ({_in_list(batch)})',
                        # Skip the header (mainline) and tax lines; SOAP's line sublist excludes them.
                        f"{TRANSACTION_LINE_ALIAS}.mainline = 'F'",
                        f"{TRANSACTION_LINE_ALIAS}.taxline = 'F'"
                    ]
                )

                for page in self.client.iter_pages(query):
                    for row in page:
                        line = {}
                        for index, (soap_name, _, kind) in enumerate(TRANSACTION_LINE_FIELDS):
                            value = row.get(f'l{index}')
                            if kind == 'ref':
                                value = self._build_ref(value, row.get(f'ln{index}'))
                            elif kind == 'boolean':
                                value = _suiteql_boolean(value)
                            elif kind == 'signed' and isinstance(value, (int, float)):
                                value = value * sign
                            line[soap_name] = value
                        # str() because the key may come back as int on one query and str on the other.
                        lines_by_key[str(row.get('parenttxn'))].append(line)

        for display_name, wrapper in line_targets.items():
            for key, record in records:
                lines = lines_by_key.get(str(key), [])
                record[display_name] = {wrapper: lines} if lines else None

    @staticmethod
    def _build_ref(internal_id, name):
        """Build a SOAP-style record reference."""
        if internal_id is None and name is None:
            return None
        return {
            'name': name,
            'internalId': None if internal_id is None else str(internal_id),
            'externalId': None,
            'type': None
        }

    def _custom_columns(self, table, prefix, alias, where, record_types=None):
        """Find account-specific custom columns.

        record_types is one REST record type or a list of them; the item table spans
        several (inventoryItem, serviceSaleItem, ...), each with its own custom fields.
        """
        if isinstance(record_types, str):
            record_types = [record_types]
        record_types = tuple(record_types or ())
        cache_key = (table, prefix, tuple(where), record_types)
        if cache_key in self._custom_columns_by_table:
            return self._custom_columns_by_table[cache_key]

        # Sample rows with SELECT * and collect every key with the prefix. REST leaves
        # out null fields, so a single row would miss columns that happen to be empty.
        columns = set()
        sql = f'SELECT * FROM {table} {alias}'
        if where:
            sql += ' WHERE ' + ' AND '.join(f'({predicate})' for predicate in where)
        try:
            rows = self.client.execute(sql, limit=CUSTOM_COLUMN_SAMPLE_SIZE)
            columns = {key for row in rows or [] for key in row if key.startswith(prefix)}
        except SymonException as e:
            LOGGER.warning('Could not sample %s for custom fields: %s', table, e)

        # The sample still misses fields that are null in every sampled row, so add any
        # the record metadata defines.
        if record_types:
            columns |= self._metadata_custom_columns(table, prefix, alias, record_types, columns)

        columns = sorted(columns)
        self._custom_columns_by_table[cache_key] = columns
        return columns

    def _metadata_custom_columns(self, table, prefix, alias, record_types, known_columns):
        """Return custom columns listed in the record metadata but missing from the sample."""
        field_names = set()
        for record_type in record_types:
            try:
                field_names.update(self.client.record_field_names(record_type))
            except (SymonException, requests.RequestException, ValueError) as e:
                LOGGER.warning('Could not read %s metadata for custom fields: %s', record_type, e)

        candidates = sorted(
            {name.lower() for name in field_names if name.lower().startswith(prefix)} - known_columns
        )
        if not candidates:
            return set()

        # Not every metadata field is a SuiteQL column (e.g. fields that aren't stored),
        # and one bad column fails the whole stream query. Check them together first,
        # then one at a time only if that fails.
        try:
            if self._columns_selectable(table, alias, candidates):
                return set(candidates)
            return {column for column in candidates if self._columns_selectable(table, alias, [column])}
        except (SymonException, TapNetSuiteException) as e:
            LOGGER.warning('Could not check %s custom fields from metadata: %s', table, e)
            return set()

    def _columns_selectable(self, table, alias, columns):
        """Return True if SuiteQL accepts a query selecting these columns.

        Raises on errors that say nothing about the columns (rate limit, timeout, permissions).
        """
        sql = f'SELECT {", ".join(f"{alias}.{column}" for column in columns)} FROM {table} {alias}'
        try:
            self.client.execute(sql, limit=1)
            return True
        except SymonException as e:
            if e.code in COLUMN_REJECTED_CODES:
                return False
            raise

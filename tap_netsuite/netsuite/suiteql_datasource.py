"""REST/SuiteQL data source, authenticated with OAuth 2.0 client credentials.

This is the forward-looking transport. It reproduces the record shapes the SOAP path emits so
that existing downstream pipelines keep working after a customer re-creates their connection:

* scalar fields come from mapped SuiteQL columns;
* fields SOAP returns as a nested ``RecordRef`` are selected as an id plus a joined name and
  reassembled into the same nested dict;
* ``CustomFieldList`` is rebuilt from the account's custom columns, which are discovered at
  runtime because they differ per account;
* transaction sublists are fetched per page from ``transactionline`` and grouped back onto their
  parent record.

Column expressions live in ``schemas/object_definition.json`` under ``suiteqlExpr``; see
``scripts/generate_suiteql_mapping.py``.
"""
import os
import re
from collections import defaultdict

import singer

from .datasource import DataSource
from .exceptions import SymonException
from .oauth import NetSuiteOAuthTokenManager, resolve_account_id
from .suiteql import MAX_PAGE_SIZE, SuiteQLClient, SuiteQLQuery, incremental_predicate

LOGGER = singer.get_logger()

BARE_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

# Oracle caps an IN list at 1000 entries; stay well inside it.
SUBLIST_BATCH_SIZE = 500

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
TRANSACTION_LINE_COLUMNS = _RAW_STREAM_METADATA.get('_transactionLineColumns', [])


def qualify(expression, alias):
    """Prefix a bare column name with the primary table alias.

    Joins bring in columns like ``name`` that exist on several tables, so unqualified references
    would be ambiguous. Anything that is not a plain identifier is assumed to be a complete
    expression already.
    """
    if BARE_IDENTIFIER.match(expression):
        return f'{alias}.{expression}'
    return expression


def _batches(values, size):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _in_list(values):
    rendered = []
    for value in values:
        try:
            rendered.append(str(int(value)))
        except (TypeError, ValueError):
            escaped = str(value).replace("'", "''")
            rendered.append(f"'{escaped}'")
    return ', '.join(rendered)


class SuiteQLDataSource(DataSource):
    def __init__(self,
                 object_definitions,
                 account=None,
                 client_id=None,
                 certificate_id=None,
                 private_key=None,
                 is_sandbox=False,
                 page_size=MAX_PAGE_SIZE):
        super().__init__(object_definitions)

        self.account_id = resolve_account_id(account, is_sandbox)
        self.client_id = client_id
        self.certificate_id = certificate_id
        self.private_key = private_key
        self.page_size = page_size

        self.token_manager = None
        self.client = None
        self._custom_columns_by_table = {}

    def connect(self):
        missing = [
            name for name, value in (
                ('account ID', self.account_id),
                ('client ID', self.client_id),
                ('certificate ID', self.certificate_id),
                ('certificate private key', self.private_key)
            ) if not value
        ]
        if missing:
            raise SymonException(
                f'The NetSuite connection is missing the {", ".join(missing)}. '
                'Please reconnect and try again.',
                'netSuite.NetSuiteMissingOAuthConfig'
            )

        self.token_manager = NetSuiteOAuthTokenManager(
            account_id=self.account_id,
            client_id=self.client_id,
            certificate_id=self.certificate_id,
            private_key=self.private_key
        )
        self.client = SuiteQLClient(
            account_id=self.account_id,
            token_manager=self.token_manager,
            page_size=self.page_size
        )

        # Mint a token now so credential problems surface with an actionable message rather than
        # partway through the first stream.
        self.token_manager.access_token()

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
        plan = self._build_plan(stream, metadata, self.display_names(catalog_entry), start_date)

        for page in self.client.iter_pages(plan['query']):
            yield self._assemble_page(page, plan)

    def _build_plan(self, stream, metadata, display_names, start_date):
        """Turn the catalog's requested fields into a query plus assembly instructions."""
        alias = metadata['alias']
        fields_by_display_name = {
            field['displayName']: field
            for field in self.object_definitions[stream]
        }

        select = {}
        joins = []
        columns = {}
        refs = {}
        custom_field_targets = {}
        line_targets = {}
        unsupported = []

        for index, display_name in enumerate(display_names):
            expression = (fields_by_display_name.get(display_name) or {}).get('suiteqlExpr')
            if expression is None:
                unsupported.append(display_name)
                continue

            kind = expression['kind']

            if kind == 'column':
                key = f'c{index}'
                select[key] = qualify(expression['expr'], alias)
                columns[display_name] = key

            elif kind == 'ref':
                id_key = f'c{index}'
                name_key = f'n{index}'
                join_alias = f'r{index}'
                select[id_key] = f'{alias}.{expression["column"]}'
                select[name_key] = f'{join_alias}.{expression["nameColumn"]}'
                joins.append(
                    f'LEFT JOIN {expression["table"]} {join_alias} '
                    f'ON {join_alias}.id = {alias}.{expression["column"]}'
                )
                refs[display_name] = (id_key, name_key)

            elif kind == 'customFields':
                # Custom fields are ordinary columns in SuiteQL, so they are selected inline with
                # everything else rather than costing an extra round trip per page.
                custom_columns = self._custom_columns(metadata['table'], expression['prefix'])
                keys = {}
                for offset, column in enumerate(custom_columns):
                    key = f'f{index}x{offset}'
                    select[key] = f'{alias}.{column}'
                    keys[column] = key
                custom_field_targets[display_name] = keys

            elif kind == 'transactionLines':
                line_targets[display_name] = expression.get('wrapper', 'line')

            else:
                unsupported.append(display_name)

        if unsupported:
            LOGGER.warning(
                '%s: no SuiteQL mapping for %s; these fields will be null. '
                'See tap_netsuite/netsuite/schemas/suiteql_streams.json',
                stream, ', '.join(sorted(unsupported))
            )

        key_column = qualify(metadata['keyColumn'], alias)

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
            'refs': refs,
            'customFields': custom_field_targets,
            'lines': line_targets,
            'unsupported': unsupported
        }

    def _assemble_page(self, rows, plan):
        """Rebuild SOAP-shaped records from a flat SuiteQL page."""
        key_alias = plan['query'].key_alias
        records = []

        for row in rows:
            record = {}

            for display_name, key in plan['columns'].items():
                record[display_name] = row.get(key)

            for display_name, (id_key, name_key) in plan['refs'].items():
                record[display_name] = self._build_ref(row.get(id_key), row.get(name_key))

            for display_name, keys in plan['customFields'].items():
                custom_fields = [
                    {'scriptId': column, 'value': row.get(key)}
                    for column, key in keys.items()
                    if row.get(key) is not None
                ]
                record[display_name] = {'customField': custom_fields} if custom_fields else None

            for display_name in plan['unsupported']:
                record[display_name] = None

            records.append((row.get(key_alias), record))

        if plan['lines']:
            self._attach_transaction_lines(records, plan['lines'])

        return [record for _, record in records]

    def _attach_transaction_lines(self, records, line_targets):
        keys = [key for key, _ in records if key is not None]
        lines_by_key = defaultdict(list)

        if keys and TRANSACTION_LINE_COLUMNS:
            select = {'parenttxn': f'{TRANSACTION_LINE_ALIAS}.transaction'}
            for index, column in enumerate(TRANSACTION_LINE_COLUMNS):
                select[f'l{index}'] = f'{TRANSACTION_LINE_ALIAS}.{column}'

            for batch in _batches(keys, SUBLIST_BATCH_SIZE):
                query = SuiteQLQuery(
                    select=select,
                    from_clause=f'{TRANSACTION_LINE_TABLE} {TRANSACTION_LINE_ALIAS}',
                    key_column=f'{TRANSACTION_LINE_ALIAS}.id',
                    where=[f'{TRANSACTION_LINE_ALIAS}.transaction IN ({_in_list(batch)})']
                )

                for page in self.client.iter_pages(query):
                    for row in page:
                        line = {
                            column: row.get(f'l{index}')
                            for index, column in enumerate(TRANSACTION_LINE_COLUMNS)
                        }
                        lines_by_key[str(row.get('parenttxn'))].append(line)

        for display_name, wrapper in line_targets.items():
            for key, record in records:
                lines = lines_by_key.get(str(key), [])
                record[display_name] = {wrapper: lines} if lines else None

    @staticmethod
    def _build_ref(internal_id, name):
        """Mirror the shape zeep produces for a SOAP RecordRef."""
        if internal_id is None and name is None:
            return None
        return {
            'name': name,
            'internalId': None if internal_id is None else str(internal_id),
            'externalId': None,
            'type': None
        }

    def _custom_columns(self, table, prefix):
        """Discover the account's custom columns on a table.

        Custom fields are real columns in SuiteQL rather than a separate sublist, and which ones
        exist is account-specific, so probe a single row and keep the matching column names.
        """
        cache_key = (table, prefix)
        if cache_key in self._custom_columns_by_table:
            return self._custom_columns_by_table[cache_key]

        columns = []
        try:
            rows = self.client.execute(f'SELECT TOP 1 * FROM {table}')
            if rows:
                columns = sorted(key for key in rows[0] if key.startswith(prefix))
            else:
                LOGGER.info('%s has no rows; cannot discover custom columns', table)
        except SymonException as e:
            LOGGER.warning('Could not discover custom columns on %s: %s', table, e)

        self._custom_columns_by_table[cache_key] = columns
        return columns

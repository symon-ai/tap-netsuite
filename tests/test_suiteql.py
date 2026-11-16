import unittest
from unittest import mock

import requests

from tap_netsuite.netsuite import NS_OBJECT_DEFINITIONS, suiteql, suiteql_datasource
from tap_netsuite.netsuite.exceptions import SymonException
from tap_netsuite.netsuite.suiteql import SuiteQLClient, SuiteQLQuery, incremental_predicate
from tap_netsuite.netsuite.suiteql_datasource import (
    TRANSACTION_LINE_FIELDS,
    SuiteQLDataSource,
)


def response(status_code, body=None):
    resp = mock.Mock()
    resp.status_code = status_code
    resp.json.return_value = body or {}
    return resp


class FakeTokenManager:
    def __init__(self):
        self.invalidations = 0

    def access_token(self):
        return 'token'

    def invalidate(self):
        self.invalidations += 1


class TestSuiteQLQuery(unittest.TestCase):
    def test_single_key_render(self):
        query = SuiteQLQuery(select={'c0': 't.name'}, from_clause='customer t', key_column='t.id')
        self.assertEqual(
            query.render(after_key=5),
            'SELECT t.name AS c0, t.id AS keycol FROM customer t WHERE (t.id > 5) ORDER BY t.id ASC'
        )

    def test_composite_key_render(self):
        query = SuiteQLQuery(
            select={},
            from_clause='transactionline tl',
            key_column='tl.transaction',
            tiebreak_column='tl.id',
            where=["tl.mainline = 'F'"]
        )
        sql = query.render(after_key=(10, 3))
        self.assertIn("(tl.mainline = 'F') AND "
                      "(tl.transaction > 10 OR (tl.transaction = 10 AND tl.id > 3))", sql)
        self.assertTrue(sql.endswith('ORDER BY tl.transaction ASC, tl.id ASC'))

    def test_composite_next_key(self):
        query = SuiteQLQuery(select={}, from_clause='x', key_column='a', tiebreak_column='b')
        self.assertEqual(query.next_key({'keycol': 1, 'tiebreakcol': 2}), (1, 2))
        self.assertIsNone(query.next_key({'keycol': 1}))


class TestIncrementalPredicate(unittest.TestCase):
    def test_utc(self):
        self.assertEqual(
            incremental_predicate('c.lastmodifieddate', '2024-01-01T00:00:00Z'),
            "c.lastmodifieddate >= TO_DATE('2024-01-01 00:00:00', 'YYYY-MM-DD HH24:MI:SS')"
        )

    def test_negative_offset_converted_to_utc(self):
        self.assertIn("'2024-01-01 05:00:00'",
                      incremental_predicate('d', '2024-01-01T00:00:00-05:00'))

    def test_positive_offset_and_fraction(self):
        self.assertIn("'2023-12-31 22:00:00'",
                      incremental_predicate('d', '2024-01-01T00:00:00.123+02:00'))

    def test_empty(self):
        self.assertIsNone(incremental_predicate('d', None))


class TestSuiteQLClientRetries(unittest.TestCase):
    def setUp(self):
        self.session = mock.Mock()
        self.tokens = FakeTokenManager()
        self.client = SuiteQLClient('123', self.tokens, session=self.session)
        sleep = mock.patch.object(suiteql.time, 'sleep')
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_401_after_5xx_still_refreshes(self):
        self.session.post.side_effect = [response(503), response(401), response(200, {'items': [1]})]
        self.assertEqual(self.client.execute('SELECT 1'), [1])
        self.assertEqual(self.tokens.invalidations, 1)

    def test_second_401_fails(self):
        self.session.post.side_effect = [response(401), response(401)]
        with self.assertRaises(SymonException):
            self.client.execute('SELECT 1')

    def test_retries_exhausted(self):
        self.session.post.side_effect = [response(503)] * (suiteql.MAX_RETRIES + 1)
        with self.assertRaises(SymonException):
            self.client.execute('SELECT 1')
        self.assertEqual(self.session.post.call_count, suiteql.MAX_RETRIES + 1)

    def test_server_error_not_retried_when_disabled(self):
        self.session.post.side_effect = [response(500)]
        with self.assertRaises(SymonException):
            self.client.execute('SELECT 1', retry_server_errors=False)
        self.assertEqual(self.session.post.call_count, 1)

    def test_rate_limit_still_retried_when_server_retries_disabled(self):
        self.session.post.side_effect = [response(429), response(200, {'items': []})]
        self.assertEqual(self.client.execute('SELECT 1', retry_server_errors=False), [])

    def test_timeout_retried_once(self):
        self.session.post.side_effect = [requests.exceptions.Timeout(), response(200, {'items': []})]
        self.assertEqual(self.client.execute('SELECT 1'), [])

    def test_iter_pages_uses_composite_key(self):
        page_one = [{'keycol': 1, 'tiebreakcol': 1}, {'keycol': 2, 'tiebreakcol': 1}]
        page_two = [{'keycol': 2, 'tiebreakcol': 2}]
        self.session.post.side_effect = [response(200, {'items': page_one}),
                                         response(200, {'items': page_two})]
        self.client.page_size = 2
        query = SuiteQLQuery(select={}, from_clause='x', key_column='a', tiebreak_column='b')

        pages = list(self.client.iter_pages(query))

        self.assertEqual(pages, [page_one, page_two])
        second_sql = self.session.post.call_args_list[1].kwargs['json']['q']
        self.assertIn('(a > 2 OR (a = 2 AND b > 1))', second_sql)


class TestTransactionLines(unittest.TestCase):
    def test_lines_attached_in_soap_shape(self):
        source = SuiteQLDataSource({}, account='123')
        index = {soap_name: i for i, (soap_name, _, _) in enumerate(TRANSACTION_LINE_FIELDS)}
        row = {'parenttxn': 10, f'l{index["item"]}': 7, f'ln{index["item"]}': 'Widget',
               f'l{index["amount"]}': -25.0, f'l{index["isClosed"]}': 'T'}
        source.client = mock.Mock()
        source.client.iter_pages.return_value = [[row]]
        records = [(10, {}), (11, {})]

        source._attach_transaction_lines(records, {'ItemList': 'item'}, sign=-1)

        line = records[0][1]['ItemList']['item'][0]
        self.assertEqual(line['item']['internalId'], '7')
        self.assertEqual(line['item']['name'], 'Widget')
        self.assertEqual(line['amount'], 25.0)
        self.assertIs(line['isClosed'], True)
        self.assertIsNone(records[1][1]['ItemList'])
        query = source.client.iter_pages.call_args.args[0]
        self.assertEqual(query.tiebreak_column, 'tl.id')

    def test_custom_columns_merged_across_rows(self):
        source = SuiteQLDataSource({}, account='123')
        source.client = mock.Mock()
        source.client.execute.return_value = [{'custentity_a': 1}, {'custentity_b': 2, 'id': 1}]
        self.assertEqual(source._custom_columns('customer', 'custentity', 'c', []),
                         ['custentity_a', 'custentity_b'])

    def test_custom_columns_use_stream_filter_and_sample_limit(self):
        source = SuiteQLDataSource({}, account='123')
        source.client = mock.Mock()
        source.client.execute.return_value = []
        source._custom_columns('transaction', 'custbody', 't', ["t.recordtype = 'invoice'"])
        source.client.execute.assert_called_once_with(
            "SELECT * FROM transaction t WHERE (t.recordtype = 'invoice')",
            limit=suiteql_datasource.CUSTOM_COLUMN_SAMPLE_SIZE
        )

    def test_custom_columns_add_metadata_fields_missing_from_sample(self):
        source = SuiteQLDataSource({}, account='123')
        source.client = mock.Mock()
        # Sample, then the combined check of the metadata-only column.
        source.client.execute.side_effect = [[{'custentity_a': 1}], []]
        source.client.record_field_names.return_value = ['companyName', 'custentity_a', 'custentity_EMPTY']

        self.assertEqual(source._custom_columns('customer', 'custentity', 'c', [], 'customer'),
                         ['custentity_a', 'custentity_empty'])
        source.client.record_field_names.assert_called_once_with('customer')
        source.client.execute.assert_called_with('SELECT c.custentity_empty FROM customer c', limit=1)

    def test_custom_columns_drop_metadata_fields_suiteql_rejects(self):
        source = SuiteQLDataSource({}, account='123')
        source.client = mock.Mock()
        rejected = SymonException('bad column', 'netSuite.NetSuiteInvalidQuery')
        # Sample, combined check fails, then one check per column.
        source.client.execute.side_effect = [[], rejected, [], rejected]
        source.client.record_field_names.return_value = ['custentity_ok', 'custentity_unstored']

        self.assertEqual(source._custom_columns('customer', 'custentity', 'c', [], 'customer'),
                         ['custentity_ok'])

    def test_custom_columns_fall_back_to_sample_when_metadata_fails(self):
        source = SuiteQLDataSource({}, account='123')
        source.client = mock.Mock()
        source.client.execute.return_value = [{'custentity_a': 1}]
        source.client.record_field_names.side_effect = SymonException('forbidden', 'netSuite.NetSuiteQueryFailed')

        self.assertEqual(source._custom_columns('customer', 'custentity', 'c', [], 'customer'),
                         ['custentity_a'])

    def test_custom_columns_merge_metadata_across_record_types(self):
        source = SuiteQLDataSource({}, account='123')
        source.client = mock.Mock()
        source.client.execute.side_effect = [[], []]
        fields = {'inventoryItem': ['custitem_color'], 'serviceSaleItem': ['custitem_hours']}

        def record_field_names(record_type):
            if record_type not in fields:
                raise SymonException('not found', 'netSuite.NetSuiteQueryFailed')
            return fields[record_type]

        source.client.record_field_names.side_effect = record_field_names

        self.assertEqual(
            source._custom_columns('item', 'custitem', 'i', [],
                                   ['inventoryItem', 'kitItem', 'serviceSaleItem']),
            ['custitem_color', 'custitem_hours']
        )

    def test_record_field_names_reads_schema_properties(self):
        session = mock.Mock()
        session.get.return_value = response(200, {'properties': {'id': {}, 'custbody_x': {}}})
        client = SuiteQLClient('123_SB1', FakeTokenManager(), session=session)

        self.assertEqual(client.record_field_names('salesOrder'), ['id', 'custbody_x'])
        url = session.get.call_args.args[0]
        self.assertEqual(url, 'https://123-sb1.suitetalk.api.netsuite.com'
                              '/services/rest/record/v1/metadata-catalog/salesOrder')
        self.assertEqual(session.get.call_args.kwargs['headers']['Accept'], 'application/schema+json')


class TestCurrenciesSuiteQL(unittest.TestCase):
    def setUp(self):
        self.source = SuiteQLDataSource(NS_OBJECT_DEFINITIONS, account='123')
        self.source.client = mock.Mock()
        self.source.client.iter_pages.side_effect = lambda query: iter([[]])
        self.catalog_entry = {
            'stream': 'Currencies',
            'schema': {'properties': {
                field['displayName']: {} for field in NS_OBJECT_DEFINITIONS['Currencies']
            }}
        }

    def query_sql(self):
        list(self.source.query_stream(self.catalog_entry, None))
        return self.source.client.iter_pages.call_args.args[0].render().lower()

    def test_incl_in_fx_rate_updates_is_optional(self):
        metadata = suiteql_datasource.SUITEQL_STREAM_METADATA['Currencies']
        plan = self.source._build_plan('Currencies', metadata, list(self.catalog_entry['schema']['properties']), None)

        self.assertIn('InclInFxRateUpdates', plan['optional'])
        self.assertIn('inclinfxrateupdates', plan['query'].render().lower())

    def test_optional_column_kept_when_query_runs(self):
        self.source.client.execute.return_value = []

        self.assertIn('inclinfxrateupdates', self.query_sql())
        self.assertFalse(self.source.client.execute.call_args.kwargs['retry_server_errors'])

    def test_optional_column_dropped_when_query_fails(self):
        self.source.client.execute.side_effect = SymonException('boom', 'netSuite.NetSuiteQueryFailed')

        self.assertNotIn('inclinfxrateupdates', self.query_sql())

    def test_permission_error_not_swallowed(self):
        self.source.client.execute.side_effect = SymonException(
            'denied', 'netSuite.NetSuiteInsufficientPermissions')

        with self.assertRaises(SymonException):
            self.query_sql()


class TestOptionalRefs(unittest.TestCase):
    def setUp(self):
        self.source = SuiteQLDataSource(NS_OBJECT_DEFINITIONS, account='123')
        self.source._custom_columns = mock.Mock(return_value=[])
        self.source.client = mock.Mock()
        self.source.client.iter_pages.side_effect = lambda query: iter([[]])
        self.catalog_entry = {
            'stream': 'PurchaseOrder',
            'schema': {'properties': {'TranId': {}, 'Class': {}, 'Department': {}}}
        }

    def test_mainline_refs_dropped_when_query_fails(self):
        self.source.client.execute.side_effect = SymonException('boom', 'netSuite.NetSuiteInvalidQuery')

        list(self.source.query_stream(self.catalog_entry, None))
        sql = self.source.client.iter_pages.call_args.args[0].render().lower()

        self.assertNotIn('ml.class', sql)
        self.assertNotIn('ml.department', sql)
        self.assertNotIn('transactionline ml', sql)


class TestCustomerPaymentsSuiteQL(unittest.TestCase):
    def setUp(self):
        self.source = SuiteQLDataSource(NS_OBJECT_DEFINITIONS, account='123')
        self.source._custom_columns = mock.Mock(return_value=[])
        display_names = ['Status', 'Cleared', 'Account', 'CustomerRef', 'Balance']
        self.plan = self.source._build_plan(
            'CustomerPayments', suiteql_datasource.SUITEQL_STREAM_METADATA['CustomerPayments'],
            display_names, None
        )
        self.aliases = {display_name: f'c{i}' for i, display_name in enumerate(display_names)}

    def test_query_uses_display_text_and_customer_join(self):
        sql = self.plan['query'].render()
        self.assertIn('BUILTIN.DF(t.status) AS c0', sql)
        self.assertIn('BUILTIN.DF(t.account) AS n2', sql)
        self.assertIn('BUILTIN.DF(t.entity) AS n3', sql)
        self.assertIn('j4.balance AS c4', sql)
        self.assertIn('LEFT JOIN customer j4 ON j4.id = t.entity', sql)

    def test_values_match_soap_format(self):
        row = {'c0': 'Customer Payment : Deposited', 'c1': 'T', 'c4': 12.5, 'keycol': 1}
        record = self.source._assemble_page([row], self.plan)[0]
        self.assertEqual(record['Status'], 'Deposited')
        self.assertEqual(record['Cleared'], 'True')
        self.assertEqual(record['Balance'], 12.5)


class TestMainlineRefs(unittest.TestCase):
    def test_department_and_class_share_one_mainline_join(self):
        source = SuiteQLDataSource(NS_OBJECT_DEFINITIONS, account='123')
        plan = source._build_plan(
            'Invoice', suiteql_datasource.SUITEQL_STREAM_METADATA['Invoice'],
            ['Department', 'Class'], None
        )
        sql = plan['query'].render()
        self.assertIn('BUILTIN.DF(ml.department) AS n0', sql)
        self.assertIn('BUILTIN.DF(ml.class) AS n1', sql)
        self.assertEqual(sql.count('LEFT JOIN transactionline ml'), 1)
        self.assertEqual(plan['lineSign'], -1)


class TestDatetimeColumns(unittest.TestCase):
    def setUp(self):
        self.source = SuiteQLDataSource(
            {'Customer': [{'displayName': 'lastModifiedDate', 'type': 'datetime'}]},
            account='123'
        )
        self.metadata = {'alias': 'c', 'table': 'customer', 'keyColumn': 'id',
                         'fields': {'lastModifiedDate': {'kind': 'column', 'expr': 'lastmodifieddate'}}}

    def test_selected_as_fixed_format(self):
        plan = self.source._build_plan(
            'Customer', self.metadata, ['lastModifiedDate'], None
        )
        self.assertEqual(plan['query'].select['c0'],
                         "TO_CHAR(c.lastmodifieddate, 'YYYY-MM-DD HH24:MI:SS')")

    def test_returned_as_utc_iso(self):
        plan = self.source._build_plan(
            'Customer', self.metadata, ['lastModifiedDate'], None
        )
        records = self.source._assemble_page([{'c0': '2024-03-05 14:07:09', 'keycol': 1}], plan)
        self.assertEqual(records[0]['lastModifiedDate'], '2024-03-05T14:07:09+00:00')


class TestRetryDelay(unittest.TestCase):
    def test_honors_retry_after(self):
        resp = response(429)
        resp.headers = {'Retry-After': '7'}
        self.assertEqual(suiteql._retry_delay(resp, 1), 7)

    def test_rate_limit_backs_off_longer_than_5xx(self):
        resp_429, resp_503 = response(429), response(503)
        resp_429.headers = resp_503.headers = {}
        self.assertGreater(suiteql._retry_delay(resp_429, 1), suiteql._retry_delay(resp_503, 1))


if __name__ == '__main__':
    unittest.main()

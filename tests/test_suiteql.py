import unittest
from unittest import mock

import requests

from tap_netsuite.netsuite import suiteql, suiteql_datasource
from tap_netsuite.netsuite.exceptions import SymonException
from tap_netsuite.netsuite.suiteql import SuiteQLClient, SuiteQLQuery, incremental_predicate
from tap_netsuite.netsuite.suiteql_datasource import (
    TRANSACTION_LINE_COLUMNS,
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
    def test_lines_attached_with_booleans(self):
        source = SuiteQLDataSource({}, account='123')
        index = {column: i for i, column in enumerate(TRANSACTION_LINE_COLUMNS)}
        row = {'parenttxn': 10, f'l{index["id"]}': 1, f'l{index["mainline"]}': 'F',
               f'l{index["isclosed"]}': 'T'}
        source.client = mock.Mock()
        source.client.iter_pages.return_value = [[row]]
        records = [(10, {}), (11, {})]

        source._attach_transaction_lines(records, {'ItemList': 'item'})

        line = records[0][1]['ItemList']['item'][0]
        self.assertIs(line['mainline'], False)
        self.assertIs(line['isclosed'], True)
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


class TestDatetimeColumns(unittest.TestCase):
    def setUp(self):
        self.source = SuiteQLDataSource(
            {'Customer': [{'displayName': 'lastModifiedDate', 'type': 'datetime',
                           'suiteqlExpr': {'kind': 'column', 'expr': 'lastmodifieddate'}}]},
            account='123'
        )

    def test_selected_as_fixed_format(self):
        plan = self.source._build_plan(
            'Customer', {'alias': 'c', 'table': 'customer', 'keyColumn': 'id'},
            ['lastModifiedDate'], None
        )
        self.assertEqual(plan['query'].select['c0'],
                         "TO_CHAR(c.lastmodifieddate, 'YYYY-MM-DD HH24:MI:SS')")

    def test_returned_as_utc_iso(self):
        plan = self.source._build_plan(
            'Customer', {'alias': 'c', 'table': 'customer', 'keyColumn': 'id'},
            ['lastModifiedDate'], None
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

"""Tests for SuiteQL query rendering and pagination."""
import pytest
import responses

from tap_netsuite.netsuite.exceptions import SymonException, TapNetSuiteQuotaExceededException
from tap_netsuite.netsuite.suiteql import (
    MAX_PAGE_SIZE,
    SuiteQLClient,
    SuiteQLQuery,
    incremental_predicate,
)

QUERY_URL = 'https://1234567.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql'


def page(items, has_more=False):
    return {'items': items, 'hasMore': has_more, 'count': len(items)}


def rows(start, count, key='keycol'):
    return [{key: str(identifier), 'c0': f'value-{identifier}'}
            for identifier in range(start, start + count)]


class TestQueryRendering:
    def test_renders_select_from_and_order_by(self):
        query = SuiteQLQuery(select={'c0': 't.tranid'}, from_clause='transaction t',
                             key_column='t.id')

        sql = query.render()

        assert sql == 'SELECT t.tranid AS c0, t.id AS keycol FROM transaction t ORDER BY t.id ASC'

    def test_key_column_is_selected_automatically(self):
        query = SuiteQLQuery(select={}, from_clause='transaction t', key_column='t.id')

        assert query.select['keycol'] == 't.id'

    def test_alias_is_omitted_when_it_equals_the_expression(self):
        query = SuiteQLQuery(select={'name': 'name'}, from_clause='currency cur',
                             key_column='id')

        assert 'name AS name' not in query.render()
        assert 'SELECT name,' in query.render()

    def test_static_predicates_are_wrapped_and_combined(self):
        query = SuiteQLQuery(
            select={'c0': 't.id'},
            from_clause='transaction t',
            key_column='t.id',
            where=["t.recordtype = 'invoice'", 't.posting = 1']
        )

        sql = query.render()

        assert "WHERE (t.recordtype = 'invoice') AND (t.posting = 1)" in sql

    def test_keyset_predicate_is_appended_for_later_pages(self):
        query = SuiteQLQuery(select={'c0': 't.id'}, from_clause='transaction t',
                             key_column='t.id')

        sql = query.render(after_key='4242')

        assert '(t.id > 4242)' in sql
        assert sql.endswith('ORDER BY t.id ASC')

    def test_non_numeric_keys_are_quoted_and_escaped(self):
        query = SuiteQLQuery(select={'c0': 'i.id'}, from_clause='item i', key_column='i.id')

        assert "(i.id > 'ab''cd')" in query.render(after_key="ab'cd")

    def test_join_clauses_ride_along_in_the_from_clause(self):
        query = SuiteQLQuery(
            select={'c0': 't.currency', 'n0': 'r0.name'},
            from_clause='transaction t LEFT JOIN currency r0 ON r0.id = t.currency',
            key_column='t.id'
        )

        assert 'LEFT JOIN currency r0 ON r0.id = t.currency' in query.render()


class TestIncrementalPredicate:
    def test_uses_to_date_rather_than_a_bare_string(self):
        """A string literal forces an implicit conversion that defeats the index."""
        predicate = incremental_predicate('t.lastmodifieddate', '2026-01-01T00:00:00Z')

        assert predicate == (
            "t.lastmodifieddate >= TO_DATE('2026-01-01 00:00:00', 'YYYY-MM-DD HH24:MI:SS')"
        )

    def test_strips_fractional_seconds(self):
        predicate = incremental_predicate('t.lastmodifieddate', '2026-01-01T00:00:00.123456Z')

        assert "TO_DATE('2026-01-01 00:00:00'" in predicate

    def test_strips_numeric_offsets(self):
        predicate = incremental_predicate('t.lastmodifieddate', '2026-01-01T05:30:00+05:30')

        assert "TO_DATE('2026-01-01 05:30:00'" in predicate

    def test_no_start_date_means_no_predicate(self):
        assert incremental_predicate('t.lastmodifieddate', None) is None
        assert incremental_predicate('t.lastmodifieddate', '') is None


class TestPagination:
    def make_client(self, stub_token_manager, page_size=MAX_PAGE_SIZE):
        return SuiteQLClient(account_id='1234567', token_manager=stub_token_manager,
                             page_size=page_size)

    def query(self):
        return SuiteQLQuery(select={'c0': 't.tranid'}, from_clause='transaction t',
                            key_column='t.id')

    @responses.activate
    def test_sends_bearer_token_and_transient_preference(self, stub_token_manager):
        responses.add(responses.POST, QUERY_URL, json=page(rows(1, 1)), status=200)

        list(self.make_client(stub_token_manager, page_size=10).iter_pages(self.query()))

        request = responses.calls[0].request
        assert request.headers['Authorization'] == 'Bearer stub-token'
        assert request.headers['Prefer'] == 'transient'
        assert 'limit=10' in request.url

    @responses.activate
    def test_page_size_is_capped_at_the_service_maximum(self, stub_token_manager):
        client = self.make_client(stub_token_manager, page_size=50000)

        assert client.page_size == MAX_PAGE_SIZE

    @responses.activate
    def test_a_short_page_ends_pagination(self, stub_token_manager):
        responses.add(responses.POST, QUERY_URL, json=page(rows(1, 3)), status=200)

        pages = list(self.make_client(stub_token_manager, page_size=10).iter_pages(self.query()))

        assert len(pages) == 1
        assert len(responses.calls) == 1

    @responses.activate
    def test_walks_pages_by_key_rather_than_offset(self, stub_token_manager):
        """Offset paging cannot pass 100,000 rows, so each page filters on the last key seen."""
        responses.add(responses.POST, QUERY_URL, json=page(rows(1, 2)), status=200)
        responses.add(responses.POST, QUERY_URL, json=page(rows(3, 2)), status=200)
        responses.add(responses.POST, QUERY_URL, json=page(rows(5, 1)), status=200)

        pages = list(self.make_client(stub_token_manager, page_size=2).iter_pages(self.query()))

        assert [len(p) for p in pages] == [2, 2, 1]
        assert 't.id >' not in responses.calls[0].request.body.decode()
        assert '(t.id > 2)' in responses.calls[1].request.body.decode()
        assert '(t.id > 4)' in responses.calls[2].request.body.decode()

    @responses.activate
    def test_empty_first_page_yields_nothing(self, stub_token_manager):
        responses.add(responses.POST, QUERY_URL, json=page([]), status=200)

        assert list(self.make_client(stub_token_manager).iter_pages(self.query())) == []

    @responses.activate
    def test_pagination_stops_when_the_key_is_absent_from_the_response(self, stub_token_manager):
        """Advancing without a key would re-request the same page forever."""
        responses.add(responses.POST, QUERY_URL,
                      json=page([{'c0': 'a'}, {'c0': 'b'}]), status=200)

        pages = list(self.make_client(stub_token_manager, page_size=2).iter_pages(self.query()))

        assert len(pages) == 1


class TestErrorHandling:
    def make_client(self, stub_token_manager):
        return SuiteQLClient(account_id='1234567', token_manager=stub_token_manager, page_size=10)

    def query(self):
        return SuiteQLQuery(select={'c0': 't.id'}, from_clause='transaction t',
                            key_column='t.id')

    @responses.activate
    def test_a_rejected_token_is_reminted_once(self, stub_token_manager):
        responses.add(responses.POST, QUERY_URL, json={}, status=401)
        responses.add(responses.POST, QUERY_URL, json=page(rows(1, 1)), status=200)

        pages = list(self.make_client(stub_token_manager).iter_pages(self.query()))

        assert len(pages) == 1
        assert stub_token_manager.invalidations == 1

    @responses.activate
    def test_a_repeatedly_rejected_token_fails(self, stub_token_manager):
        responses.add(responses.POST, QUERY_URL, json={}, status=401)
        responses.add(responses.POST, QUERY_URL, json={}, status=401)

        with pytest.raises(SymonException) as excinfo:
            list(self.make_client(stub_token_manager).iter_pages(self.query()))

        assert excinfo.value.code == 'netSuite.NetSuiteOAuthFailed'

    @responses.activate
    def test_bad_query_reports_the_netsuite_detail(self, stub_token_manager):
        responses.add(
            responses.POST, QUERY_URL, status=400,
            json={'o:errorDetails': [{'detail': 'Invalid field: nosuchcolumn'}]}
        )

        with pytest.raises(SymonException) as excinfo:
            list(self.make_client(stub_token_manager).iter_pages(self.query()))

        assert excinfo.value.code == 'netSuite.NetSuiteInvalidQuery'
        assert 'nosuchcolumn' in str(excinfo.value)

    @responses.activate
    def test_forbidden_points_at_role_permissions(self, stub_token_manager):
        responses.add(responses.POST, QUERY_URL, json={}, status=403)

        with pytest.raises(SymonException) as excinfo:
            list(self.make_client(stub_token_manager).iter_pages(self.query()))

        assert excinfo.value.code == 'netSuite.NetSuiteInsufficientPermissions'

    @responses.activate
    def test_timeout_suggests_narrowing_the_start_date(self, stub_token_manager, monkeypatch):
        import requests

        def raise_timeout(*args, **kwargs):
            raise requests.exceptions.Timeout()

        client = self.make_client(stub_token_manager)
        monkeypatch.setattr(client.session, 'post', raise_timeout)

        with pytest.raises(SymonException) as excinfo:
            list(client.iter_pages(self.query()))

        assert excinfo.value.code == 'netSuite.NetSuiteQueryTimeout'

    @responses.activate
    def test_retries_transient_failures_then_succeeds(self, stub_token_manager, monkeypatch):
        monkeypatch.setattr('time.sleep', lambda _seconds: None)
        responses.add(responses.POST, QUERY_URL, json={}, status=503)
        responses.add(responses.POST, QUERY_URL, json=page(rows(1, 1)), status=200)

        pages = list(self.make_client(stub_token_manager).iter_pages(self.query()))

        assert len(pages) == 1

    @responses.activate
    def test_exhausted_retries_on_rate_limiting_raise_quota_exceeded(
            self, stub_token_manager, monkeypatch):
        monkeypatch.setattr('time.sleep', lambda _seconds: None)
        for _ in range(8):
            responses.add(responses.POST, QUERY_URL, json={}, status=429)

        with pytest.raises(TapNetSuiteQuotaExceededException):
            list(self.make_client(stub_token_manager).iter_pages(self.query()))


class TestExecute:
    @responses.activate
    def test_execute_returns_items_without_pagination(self, stub_token_manager):
        responses.add(responses.POST, QUERY_URL, json=page(rows(1, 2)), status=200)

        client = SuiteQLClient(account_id='1234567', token_manager=stub_token_manager)
        result = client.execute('SELECT TOP 1 * FROM transaction')

        assert len(result) == 2
        assert len(responses.calls) == 1

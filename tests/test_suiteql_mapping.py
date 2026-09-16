"""Invariants the generated SuiteQL mapping must hold.

These guard the output of scripts/generate_suiteql_mapping.py. They cannot prove an expression
names a real NetSuite column -- only scripts/suiteql_parity.py can, against a live account --
but they do catch a stream or field falling out of the mapping entirely.
"""
import pytest

from tap_netsuite.netsuite import NS_OBJECT_DEFINITIONS
from tap_netsuite.netsuite.suiteql_datasource import (
    SUITEQL_STREAM_METADATA,
    TRANSACTION_LINE_COLUMNS,
)

# The tap emits these as the replication key, with casing that differs per stream for historical
# reasons. Downstream pipelines bookmark on them, so the casing has to survive the migration.
REPLICATION_KEY_DISPLAY_NAMES = ['lastModifiedDate', 'LastModifiedDate', 'LastModDate']

VALID_KINDS = {'column', 'ref', 'customFields', 'transactionLines', 'unsupported'}

ALL_STREAMS = sorted(NS_OBJECT_DEFINITIONS)


def fields(stream):
    return NS_OBJECT_DEFINITIONS[stream]


def expression(stream, display_name):
    for field in fields(stream):
        if field['displayName'] == display_name:
            return field.get('suiteqlExpr')
    raise AssertionError(f'{stream} has no field {display_name}')


class TestCoverage:
    def test_every_stream_has_query_metadata(self):
        assert set(NS_OBJECT_DEFINITIONS) == set(SUITEQL_STREAM_METADATA)

    @pytest.mark.parametrize('stream', ALL_STREAMS)
    def test_every_field_is_mapped(self, stream):
        unmapped = [f['displayName'] for f in fields(stream) if not f.get('suiteqlExpr')]

        assert unmapped == []

    @pytest.mark.parametrize('stream', ALL_STREAMS)
    def test_every_expression_declares_a_known_kind(self, stream):
        kinds = {f['suiteqlExpr']['kind'] for f in fields(stream)}

        assert kinds <= VALID_KINDS

    def test_the_full_field_set_is_still_covered(self):
        assert sum(len(fields(s)) for s in ALL_STREAMS) == 861


class TestPrimaryKey:
    @pytest.mark.parametrize('stream', ALL_STREAMS)
    def test_id_maps_to_the_suiteql_primary_key(self, stream):
        """SOAP calls it internalId; in SuiteQL it is id, and the emitted name stays Id."""
        assert expression(stream, 'Id') == {'kind': 'column', 'expr': 'id'}

    @pytest.mark.parametrize('stream', ALL_STREAMS)
    def test_key_column_is_declared_for_pagination(self, stream):
        assert SUITEQL_STREAM_METADATA[stream]['keyColumn']


class TestReplicationKeys:
    @pytest.mark.parametrize('stream', ALL_STREAMS)
    def test_replication_key_casing_is_preserved_and_mapped(self, stream):
        present = [f['displayName'] for f in fields(stream)
                   if f['displayName'] in REPLICATION_KEY_DISPLAY_NAMES]
        declared = SUITEQL_STREAM_METADATA[stream].get('replication')

        if not present:
            # No replication key means the stream is FULL_TABLE.
            assert declared is None
            return

        assert len(present) == 1
        assert declared is not None

        spec = expression(stream, present[0])
        assert spec['kind'] == 'column'
        assert spec['expr'] == declared

    def test_the_three_historical_spellings_are_all_still_in_use(self):
        """Guards against a well-meaning rename that would break existing bookmarks."""
        spellings = {
            f['displayName'] for stream in ALL_STREAMS for f in fields(stream)
            if f['displayName'] in REPLICATION_KEY_DISPLAY_NAMES
        }

        assert spellings == set(REPLICATION_KEY_DISPLAY_NAMES)


class TestRefs:
    def test_refs_are_complete(self):
        for stream in ALL_STREAMS:
            for field in fields(stream):
                spec = field['suiteqlExpr']
                if spec['kind'] != 'ref':
                    continue
                assert spec.keys() >= {'column', 'table', 'nameColumn'}, \
                    f'{stream}.{field["displayName"]}'

    def test_every_declared_ref_reaches_the_generated_mapping(self):
        for stream, metadata in SUITEQL_STREAM_METADATA.items():
            for display_name in metadata.get('refs', {}):
                assert expression(stream, display_name)['kind'] == 'ref', \
                    f'{stream}.{display_name}'

    def test_ref_suffixed_fields_are_all_treated_as_refs(self):
        """A *Ref field left as a plain column would emit an id where a record was expected."""
        for stream in ALL_STREAMS:
            for field in fields(stream):
                if not field['displayName'].endswith('Ref'):
                    continue
                assert field['suiteqlExpr']['kind'] in ('ref', 'unsupported'), \
                    f'{stream}.{field["displayName"]}'


class TestSublists:
    def test_list_fields_are_never_plain_columns(self):
        """SuiteQL has no column for a sublist, so guessing one would always fail."""
        for stream in ALL_STREAMS:
            for field in fields(stream):
                if not field['displayName'].lower().endswith('list'):
                    continue
                assert field['suiteqlExpr']['kind'] in (
                    'customFields', 'transactionLines', 'unsupported'
                ), f'{stream}.{field["displayName"]}'

    def test_custom_field_lists_declare_a_prefix(self):
        for stream in ALL_STREAMS:
            for field in fields(stream):
                spec = field['suiteqlExpr']
                if spec['kind'] == 'customFields':
                    assert spec['prefix'].startswith('cust'), f'{stream}.{field["displayName"]}'

    def test_transaction_line_sublists_declare_a_wrapper(self):
        for stream in ALL_STREAMS:
            for field in fields(stream):
                spec = field['suiteqlExpr']
                if spec['kind'] == 'transactionLines':
                    assert spec.get('wrapper'), f'{stream}.{field["displayName"]}'

    def test_transaction_line_columns_are_configured(self):
        assert 'id' in TRANSACTION_LINE_COLUMNS
        assert 'item' in TRANSACTION_LINE_COLUMNS


class TestStreamMetadata:
    @pytest.mark.parametrize('stream', ALL_STREAMS)
    def test_table_and_alias_are_declared(self, stream):
        metadata = SUITEQL_STREAM_METADATA[stream]

        assert metadata['table']
        assert metadata['alias']

    @pytest.mark.parametrize('stream', ALL_STREAMS)
    def test_static_predicates_are_alias_qualified(self, stream):
        """Joins introduce ambiguous column names, so predicates must name their table."""
        metadata = SUITEQL_STREAM_METADATA[stream]

        for predicate in metadata.get('where', []):
            assert predicate.startswith(f'{metadata["alias"]}.'), f'{stream}: {predicate}'

    def test_transaction_streams_are_narrowed_to_one_record_type(self):
        """Every transaction type shares one table, so an unfiltered stream would mix them."""
        transaction_streams = [
            stream for stream, metadata in SUITEQL_STREAM_METADATA.items()
            if metadata['table'] == 'transaction'
        ]

        assert transaction_streams
        for stream in transaction_streams:
            where = SUITEQL_STREAM_METADATA[stream].get('where', [])
            assert any('recordtype' in predicate for predicate in where), stream

    def test_inventory_item_is_narrowed_within_the_shared_item_table(self):
        assert SUITEQL_STREAM_METADATA['InventoryItem']['where'] == ["i.itemtype = 'InvtPart'"]
        assert SUITEQL_STREAM_METADATA['Items']['where'] == []


class TestVerificationState:
    def test_streams_are_flagged_unverified_until_the_harness_runs(self):
        """Derived expressions are candidates. This flag is how we track which streams have
        actually been confirmed against a NetSuite account by scripts/suiteql_parity.py."""
        unverified = [stream for stream, metadata in SUITEQL_STREAM_METADATA.items()
                      if not metadata.get('verified')]

        assert unverified, 'update this test as streams become verified'

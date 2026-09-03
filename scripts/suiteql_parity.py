#!/usr/bin/env python3
"""Validate the SuiteQL mapping against a real NetSuite account.

Most of the column expressions in object_definition.json are derived by lowercasing the SOAP
field name. That is the right guess for the majority of columns but it is only a guess, so this
harness asks NetSuite which ones are actually wrong instead of relying on review.

Two modes, both safe to run against production because every query is read-only:

  columns   Probe every mapped column per stream. Cheap, needs only OAuth credentials, and
            produces the work list of expressions that need an override.

  compare   Extract the same records over SOAP and over SuiteQL and diff them field by field.
            Needs both credential sets for the same account. This is the check that catches
            expressions NetSuite accepts but which hold the wrong value.

Usage:

    python scripts/suiteql_parity.py columns --config config.json
    python scripts/suiteql_parity.py compare --config config.json --limit 200
    python scripts/suiteql_parity.py columns --config config.json --stream Invoice

The config file is an ordinary tap config. For `compare` it must carry both the TBA secrets and
the OAuth 2.0 fields.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

# pylint: disable=wrong-import-position
from tap_netsuite.netsuite import NS_OBJECT_DEFINITIONS, AUTH_METHOD_OAUTH2, AUTH_METHOD_TBA, NetSuite
from tap_netsuite.netsuite.exceptions import SymonException
from tap_netsuite.netsuite.suiteql import SuiteQLQuery
from tap_netsuite.netsuite.suiteql_datasource import (
    SUITEQL_STREAM_METADATA,
    SuiteQLDataSource,
    qualify,
)


def build_oauth_source(config):
    source = SuiteQLDataSource(
        NS_OBJECT_DEFINITIONS,
        account=config['ns_account'],
        client_id=config['ns_client_id'],
        certificate_id=config['ns_certificate_id'],
        private_key=config['ns_private_key'],
        is_sandbox=config.get('is_sandbox') is True
    )
    source.connect()
    return source


def mapped_columns(stream):
    """Column expressions for a stream, keyed by the display name they back."""
    metadata = SUITEQL_STREAM_METADATA[stream]
    alias = metadata['alias']
    columns = {}

    for field in NS_OBJECT_DEFINITIONS[stream]:
        expression = field.get('suiteqlExpr') or {}
        if expression.get('kind') == 'column':
            columns[field['displayName']] = {
                'sql': qualify(expression['expr'], alias),
                'derived': bool(expression.get('derived'))
            }
        elif expression.get('kind') == 'ref':
            columns[field['displayName']] = {
                'sql': f'{alias}.{expression["column"]}',
                'derived': False,
                'refTable': expression['table'],
                'refNameColumn': expression['nameColumn']
            }

    return columns


def probe(source, metadata, expressions):
    """Return None when NetSuite accepts every expression, else the error text."""
    select = ', '.join(f'{sql} AS p{index}' for index, sql in enumerate(expressions))
    sql = f'SELECT {select} FROM {metadata["table"]} {metadata["alias"]}'
    where = metadata.get('where') or []
    if where:
        sql += ' WHERE ' + ' AND '.join(f'({predicate})' for predicate in where)

    try:
        source.client.execute(f'SELECT TOP 1 * FROM ({sql})')
        return None
    except SymonException as e:
        return str(e)


def check_columns(source, stream):
    metadata = SUITEQL_STREAM_METADATA[stream]
    columns = mapped_columns(stream)

    result = {
        'stream': stream,
        'table': metadata['table'],
        'mapped': len(columns),
        'derived': sum(1 for spec in columns.values() if spec['derived']),
        'rejected': {},
        'refJoinFailures': {},
        'unsupported': sorted(
            field['displayName'] for field in NS_OBJECT_DEFINITIONS[stream]
            if (field.get('suiteqlExpr') or {}).get('kind') == 'unsupported'
        )
    }

    # One probe for the whole stream is enough when everything is valid, which is the common
    # case after the first pass. Only fall back to per-column probes when it fails.
    if probe(source, metadata, [spec['sql'] for spec in columns.values()]) is None:
        print(f'  {stream}: all {len(columns)} columns accepted')
    else:
        for display_name, spec in sorted(columns.items()):
            error = probe(source, metadata, [spec['sql']])
            if error is not None:
                result['rejected'][display_name] = {'sql': spec['sql'], 'error': error}
        print(f'  {stream}: {len(result["rejected"])} of {len(columns)} columns rejected')

    # Ref joins are validated separately because a bad name column only breaks the join.
    for display_name, spec in sorted(columns.items()):
        if 'refTable' not in spec:
            continue
        alias = metadata['alias']
        sql = (
            f'SELECT TOP 1 rr.{spec["refNameColumn"]} AS refname '
            f'FROM {metadata["table"]} {alias} '
            f'LEFT JOIN {spec["refTable"]} rr ON rr.id = {spec["sql"]}'
        )
        try:
            source.client.execute(sql)
        except SymonException as e:
            result['refJoinFailures'][display_name] = {
                'table': spec['refTable'],
                'nameColumn': spec['refNameColumn'],
                'error': str(e)
            }

    if result['refJoinFailures']:
        print(f'  {stream}: {len(result["refJoinFailures"])} ref joins failed')

    return result


def normalize(value):
    """Collapse representational differences that are not parity failures.

    SOAP returns booleans and numbers as typed values while SuiteQL returns NetSuite's 'T'/'F'
    strings and decimal strings, and the two transports format timestamps differently.
    """
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        return value
    if value in ('T', 'true', 'True'):
        return True
    if value in ('F', 'false', 'False'):
        return False

    text = str(value).strip()
    try:
        number = float(text)
        return round(number, 4)
    except ValueError:
        pass

    return text.replace('T', ' ').replace('Z', '').strip()


def catalog_entry_for(stream):
    """A catalog entry selecting every field of a stream."""
    properties = {
        field['displayName']: {'type': ['null', 'string']}
        for field in NS_OBJECT_DEFINITIONS[stream]
    }
    return {
        'stream': stream,
        'tap_stream_id': stream,
        'schema': {'type': 'object', 'properties': properties},
        'metadata': []
    }


def take(pages, limit):
    records = []
    for page in pages:
        for record in page:
            records.append(record)
            if len(records) >= limit:
                return records
    return records


def compare_stream(config, stream, limit):
    entry = catalog_entry_for(stream)
    start_date = config.get('start_date')

    soap = NetSuite(
        ns_account=config['ns_account'],
        ns_consumer_key=config['ns_consumer_key'],
        ns_consumer_secret=config['ns_consumer_secret'],
        ns_token_key=config['ns_token_key'],
        ns_token_secret=config['ns_token_secret'],
        is_sandbox=config.get('is_sandbox') is True,
        default_start_date=start_date,
        select_fields_by_default=True,
        ns_auth_method=AUTH_METHOD_TBA
    )
    soap.connect()

    rest = NetSuite(
        ns_account=config['ns_account'],
        is_sandbox=config.get('is_sandbox') is True,
        default_start_date=start_date,
        select_fields_by_default=True,
        ns_auth_method=AUTH_METHOD_OAUTH2,
        ns_client_id=config['ns_client_id'],
        ns_certificate_id=config['ns_certificate_id'],
        ns_private_key=config['ns_private_key']
    )
    rest.connect()

    soap_records = take(soap.data_source.query_stream(entry, start_date), limit)
    rest_records = take(rest.data_source.query_stream(entry, start_date), limit)

    soap_by_id = {str(record.get('Id')): record for record in soap_records}
    rest_by_id = {str(record.get('Id')): record for record in rest_records}
    shared = sorted(set(soap_by_id) & set(rest_by_id))

    mismatches = {}
    for record_id in shared:
        for display_name in soap_by_id[record_id]:
            expected = normalize(soap_by_id[record_id].get(display_name))
            actual = normalize(rest_by_id[record_id].get(display_name))
            if expected != actual:
                bucket = mismatches.setdefault(display_name, {'count': 0, 'examples': []})
                bucket['count'] += 1
                if len(bucket['examples']) < 3:
                    bucket['examples'].append({
                        'id': record_id,
                        'soap': repr(soap_by_id[record_id].get(display_name))[:200],
                        'suiteql': repr(rest_by_id[record_id].get(display_name))[:200]
                    })

    result = {
        'stream': stream,
        'soapRecords': len(soap_records),
        'suiteqlRecords': len(rest_records),
        'comparedRecords': len(shared),
        'onlyInSoap': sorted(set(soap_by_id) - set(rest_by_id))[:20],
        'onlyInSuiteQL': sorted(set(rest_by_id) - set(soap_by_id))[:20],
        'fieldMismatches': mismatches
    }

    agreeing = len(soap_by_id[shared[0]]) - len(mismatches) if shared else 0
    print(
        f'  {stream}: compared {len(shared)} records, '
        f'{agreeing} fields agree, {len(mismatches)} differ'
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', choices=['columns', 'compare'])
    parser.add_argument('--config', required=True)
    parser.add_argument('--stream', action='append',
                        help='limit to one stream; repeatable')
    parser.add_argument('--limit', type=int, default=100,
                        help='records per stream in compare mode')
    parser.add_argument('--out', default='suiteql_parity_report.json')
    args = parser.parse_args()

    with open(args.config, encoding='utf-8') as fp:
        config = json.load(fp)

    streams = args.stream or sorted(SUITEQL_STREAM_METADATA)
    results = []

    if args.mode == 'columns':
        source = build_oauth_source(config)
        print(f'Probing {len(streams)} streams against account {source.account_id}')
        for stream in streams:
            try:
                results.append(check_columns(source, stream))
            except SymonException as e:
                print(f'  {stream}: FAILED {e}')
                results.append({'stream': stream, 'error': str(e)})
    else:
        print(f'Comparing {len(streams)} streams, up to {args.limit} records each')
        for stream in streams:
            try:
                results.append(compare_stream(config, stream, args.limit))
            except SymonException as e:
                print(f'  {stream}: FAILED {e}')
                results.append({'stream': stream, 'error': str(e)})

    with open(args.out, 'w', encoding='utf-8') as fp:
        json.dump({'mode': args.mode, 'results': results}, fp, indent=2)

    print(f'\nWrote {args.out}')

    rejected = sum(len(r.get('rejected', {})) for r in results)
    mismatched = sum(len(r.get('fieldMismatches', {})) for r in results)
    if rejected or mismatched:
        print(f'{rejected} rejected columns, {mismatched} mismatched fields need overrides in '
              'tap_netsuite/netsuite/schemas/suiteql_streams.json')
        return 1

    print('No mapping problems found')
    return 0


if __name__ == '__main__':
    sys.exit(main())

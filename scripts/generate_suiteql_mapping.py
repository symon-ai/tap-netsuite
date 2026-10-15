#!/usr/bin/env python3
"""Materialize the ``suiteqlExpr`` mapping onto every field in object_definition.json.

The mapping is generated rather than hand-written so that all 861 fields are explicit and
reviewable in the schema file, while the rules that produce them stay in one place. Edit
schemas/suiteql_streams.json and re-run:

    python scripts/generate_suiteql_mapping.py

Derivation rules, in precedence order:

1. ``Id`` maps to the SuiteQL primary key ``id`` (SOAP calls it ``internalId``).
2. An explicit entry in the stream's ``refs`` becomes a two-column ref, reassembled at runtime
   into the nested dict shape that SOAP returns for a RecordRef.
3. An explicit entry in the stream's ``sublists`` becomes that sublist kind.
4. Any remaining field whose display name ends in ``List`` is a sublist we have not mapped, so it
   is marked unsupported rather than guessed at.
5. An explicit entry in the stream's ``columns`` overrides the default.
6. An explicit entry in the stream's ``unsupported`` list is marked unsupported.
7. Everything else derives from the SOAP field name lowercased, which is the SuiteQL column
   naming convention.

Rule 7 is a *candidate*, not a guarantee. scripts/suiteql_parity.py validates every generated
expression against a real NetSuite account and reports the ones NetSuite rejects.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_DIR = os.path.join(HERE, '..', 'tap_netsuite', 'netsuite', 'schemas')
OBJECT_DEFINITION_PATH = os.path.join(SCHEMA_DIR, 'object_definition.json')
STREAM_METADATA_PATH = os.path.join(SCHEMA_DIR, 'suiteql_streams.json')

PRIMARY_KEY_DISPLAY_NAME = 'Id'


def derive_column(soap_name):
    return soap_name.lower()


def build_expression(stream_metadata, field):
    display_name = field['displayName']
    soap_name = field['name']

    if display_name == PRIMARY_KEY_DISPLAY_NAME:
        return {'kind': 'column', 'expr': 'id'}

    ref = stream_metadata.get('refs', {}).get(display_name)
    if ref is not None:
        return {
            'kind': 'ref',
            'column': ref['column'],
            'table': ref['table'],
            'nameColumn': ref['nameColumn']
        }

    sublist = stream_metadata.get('sublists', {}).get(display_name)
    if sublist is not None:
        return dict(sublist)

    if display_name.lower().endswith('list'):
        return {'kind': 'unsupported', 'reason': 'sublist not mapped'}

    override = stream_metadata.get('columns', {}).get(display_name)
    if override is not None:
        return {'kind': 'column', 'expr': override}

    if display_name in stream_metadata.get('unsupported', []):
        return {'kind': 'unsupported', 'reason': 'no SuiteQL equivalent established'}

    return {'kind': 'column', 'expr': derive_column(soap_name), 'derived': True}


def main():
    with open(OBJECT_DEFINITION_PATH, encoding='utf-8') as fp:
        object_definitions = json.load(fp)
    with open(STREAM_METADATA_PATH, encoding='utf-8') as fp:
        stream_metadata_by_stream = json.load(fp)

    missing = [
        stream for stream in object_definitions
        if stream not in stream_metadata_by_stream
    ]
    if missing:
        print(f'No SuiteQL stream metadata for: {", ".join(sorted(missing))}', file=sys.stderr)
        return 1

    counts = {'column': 0, 'derived': 0, 'ref': 0, 'customFields': 0,
              'transactionLines': 0, 'unsupported': 0}

    for stream, fields in object_definitions.items():
        stream_metadata = stream_metadata_by_stream[stream]
        for field in fields:
            expression = build_expression(stream_metadata, field)
            field['suiteqlExpr'] = expression

            kind = expression['kind']
            counts[kind] = counts.get(kind, 0) + 1
            if expression.get('derived'):
                counts['derived'] += 1

    with open(OBJECT_DEFINITION_PATH, 'w', encoding='utf-8') as fp:
        json.dump(object_definitions, fp, indent=2)
        fp.write('\n')

    total = sum(len(fields) for fields in object_definitions.values())
    print(f'Mapped {total} fields across {len(object_definitions)} streams:')
    print(f'  columns              {counts["column"]} ({counts["derived"]} derived, unverified)')
    print(f'  refs                 {counts["ref"]}')
    print(f'  custom field lists   {counts["customFields"]}')
    print(f'  transaction lines    {counts["transactionLines"]}')
    print(f'  unsupported          {counts["unsupported"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

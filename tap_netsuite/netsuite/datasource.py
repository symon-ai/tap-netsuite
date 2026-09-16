"""Transport-agnostic contract between the tap and NetSuite.

A data source owns one way of talking to NetSuite: SOAP SuiteTalk with token-based
authentication, or REST/SuiteQL with OAuth 2.0. Oracle does not support OAuth 2.0 for SOAP
web services, so the authentication method and the transport cannot be chosen independently.

Implementations are responsible for returning records already keyed by the ``displayName``
values used in the discovered catalog, so that everything downstream of ``query_stream``
(bookmarks, schema transformation, record emission) stays transport-agnostic.
"""
import datetime
from abc import ABC, abstractmethod


def project_record(record, display_names, internal_name_by_display_name):
    """Re-key a raw record onto the catalog's display names.

    Every requested display name is present in the result, holding ``None`` when the record
    carries no value, because the emitted schema declares all of them.
    """
    projected = {}
    for display_name in display_names:
        internal_name = internal_name_by_display_name.get(display_name, display_name)
        value = record.get(internal_name, None)
        if isinstance(value, datetime.datetime):
            value = value.isoformat()
        projected[display_name] = value

    return projected


class DataSource(ABC):
    def __init__(self, object_definitions):
        self.object_definitions = object_definitions

    @abstractmethod
    def connect(self):
        """Establish the session or credentials the transport needs.

        Raises a ``SymonException`` with a user-facing message when the supplied credentials
        or account identifier are unusable.
        """

    @abstractmethod
    def query_stream(self, catalog_entry, start_date):
        """Yield pages of records for one stream.

        Each yielded page is an iterable of dicts keyed by catalog display name. Records at or
        after ``start_date`` on the stream's replication key are returned; streams without a
        replication key ignore it and return everything.
        """

    def internal_name_by_display_name(self, stream):
        return {
            field.get('displayName'): field.get('name')
            for field in self.object_definitions[stream]
        }

    @staticmethod
    def display_names(catalog_entry):
        return list(catalog_entry['schema'].get('properties', {}))

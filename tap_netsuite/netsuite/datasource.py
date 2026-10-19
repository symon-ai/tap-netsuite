"""Common interface for SOAP and REST NetSuite data sources."""
import datetime
from abc import ABC, abstractmethod


def project_record(record, display_names, internal_name_by_display_name):
    """Map a raw record to the catalog's field names."""
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
        """Connect to NetSuite."""

    @abstractmethod
    def query_stream(self, catalog_entry, start_date):
        """Yield pages of records for one stream."""

    def internal_name_by_display_name(self, stream):
        return {
            field.get('displayName'): field.get('name')
            for field in self.object_definitions[stream]
        }

    @staticmethod
    def display_names(catalog_entry):
        return list(catalog_entry['schema'].get('properties', {}))

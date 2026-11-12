"""Common interface for SOAP and REST NetSuite data sources."""
from abc import ABC, abstractmethod


class DataSource(ABC):
    def __init__(self, object_definitions):
        self.object_definitions = object_definitions

    @abstractmethod
    def connect(self):
        """Connect to NetSuite."""

    @abstractmethod
    def query_stream(self, catalog_entry, start_date):
        """Return the records for one stream, as pages or a single result."""

    @staticmethod
    def display_names(catalog_entry):
        return list(catalog_entry['schema'].get('properties', {}))

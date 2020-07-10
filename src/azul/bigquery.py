import abc
from abc import (
    abstractmethod,
)
from functools import cached_property
from typing import Iterable

from google.cloud import bigquery

from azul.types import (
    JSON,
    JSONs,
)


class AbstractBigQueryAdapter(abc.ABC):

    @abstractmethod
    def run_sql(self, query: str) -> Iterable[JSON]:
        """
        Evaluate an SQL query and iterate rows.
        """
        raise NotImplementedError

    @abstractmethod
    def assert_table_exists(self, dataset_name: str, table_name: str) -> None:
        """
        Raise exception if the specified table does not exist.
        """
        raise NotImplementedError

    @abstractmethod
    def create_table(self, dataset_name: str, table_name: str, schema: JSONs, rows: JSONs) -> None:
        """
        Create a new table within an existing dataset. Only used for testing.
        :param dataset_name: dataset that contains the new table.
        :param table_name: name of the new table.
        :param schema: sequence of column schemas, each with keys 'name', 'mode', and 'type'.
        :param rows: sequence of row values mapping every column defined in the schema to a value.
        :return: None
        """
        raise NotImplementedError


class BigQueryAdapter(AbstractBigQueryAdapter):

    @cached_property
    def client(self):
        from azul import config
        return bigquery.Client(project=config.tdr_bigquery_dataset.project)

    def run_sql(self, query: str) -> Iterable[JSON]:
        return self.client.query(query)

    def assert_table_exists(self, dataset_name: str, table_name: str) -> None:
        self.client.get_table(f'{dataset_name}.{table_name}')

    def create_table(self, dataset_name: str, table_name: str, schema: JSONs, rows: JSONs = ()) -> None:
        raise NotImplementedError('This is currently only used for testing')
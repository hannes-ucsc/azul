from operator import (
    itemgetter,
)
import unittest

from azul import (
    CatalogName,
    config,
)
from azul.indexer import (
    SourcedBundleFQID,
)
from indexer import (
    IndexerTestCase,
)
from indexer.test_tdr import (
    TDRAnvilPluginTestCase,
)


class TestAnvil(IndexerTestCase, TDRAnvilPluginTestCase):
    bundle_fqid = SourcedBundleFQID(uuid='fff032e81a42cab036c7d29bd63c3141',
                                    version='',
                                    source=TDRAnvilPluginTestCase.source)

    def setUp(self) -> None:
        super().setUp()
        self.index_service.create_indices(self.catalog)

    def tearDown(self):
        self.index_service.delete_indices(self.catalog)
        super().tearDown()

    @classmethod
    def catalog_config(cls) -> dict[CatalogName, config.Catalog]:
        return {
            cls.catalog: config.Catalog(name=cls.catalog,
                                        atlas='anvil',
                                        internal=False,
                                        plugins=dict(metadata=config.Catalog.Plugin(name='anvil'),
                                                     repository=config.Catalog.Plugin(name='tdr_anvil')),
                                        sources={TDRAnvilPluginTestCase.source})
        }

    def test_indexing(self):
        self.maxDiff = None
        self._index_canned_bundle(self.bundle_fqid)
        hits = self._get_all_hits()
        hits.sort(key=itemgetter('_id'))
        expected_hits = self._load_canned_result(self.bundle_fqid)
        self.assertEqual(expected_hits, hits)

    # FIXME: Switch to using datarepo_row_id for partitioning and entity IDs
    #        https://github.com/DataBiosphere/azul/issues/4341
    @unittest.skip('TinyQuery does not support SHA1')
    def test_fetch_bundle(self):
        canned_bundle = self._load_canned_bundle(self.bundle_fqid)
        self._make_mock_tdr_tables(self.bundle_fqid)
        plugin = self.plugin_for_source_spec(canned_bundle.fqid.source.spec)
        bundle = plugin.fetch_bundle(self.bundle_fqid)
        self.assertEqual(canned_bundle.fqid, bundle.fqid)
        self.assertEqual(canned_bundle.manifest, bundle.manifest)
        self.assertEqual(canned_bundle.metadata_files, bundle.metadata_files)
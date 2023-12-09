from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
)
import logging
from typing import (
    Collection,
)

from more_itertools import (
    first,
)

from azul import (
    CatalogName,
    cached_property,
    config,
    require,
)
from azul.azulclient import (
    AzulClient,
)
from azul.logging import (
    configure_script_logging,
)
from azul.plugins.repository.tdr import (
    TDRPlugin,
)
from azul.terra import (
    SourceRef as TDRSourceRef,
    TDRClient,
    TDRSourceSpec,
)

log = logging.getLogger(__name__)


class TerraValidator:

    @cached_property
    def catalogs(self) -> Collection[CatalogName]:
        result = [
            catalog.name
            for catalog in config.catalogs.values()
            if config.is_tdr_enabled(catalog.name)
        ]
        assert result, config.catalogs
        return result

    @cached_property
    def azul_client(self) -> AzulClient:
        return AzulClient()

    def repository_plugin(self, catalog: CatalogName) -> TDRPlugin:
        assert catalog in self.catalogs, catalog
        plugin = self.azul_client.repository_plugin(catalog)
        assert isinstance(plugin, TDRPlugin), plugin
        return plugin

    @property
    def tdr(self) -> TDRClient:
        # This property is cached per-thread on the plugin instance
        return self.repository_plugin(first(self.catalogs)).tdr

    @property
    def public_tdr(self) -> TDRClient:
        # This property is cached per-thread on the plugin instance
        return self.repository_plugin(first(self.catalogs)).public_tdr

    def validate_registration(self) -> None:
        for tdr in self.tdr, self.public_tdr:
            tdr.register_with_sam()
            require(tdr.is_registered())

    def validate_managed_access(self) -> None:
        public_snapshots = self.public_tdr.snapshot_ids()
        all_snapshots = self.tdr.snapshot_ids()
        diff = public_snapshots - all_snapshots
        require(not diff,
                'The public service account can access snapshots that the indexer '
                'service account cannot', diff)

    def validate_source(self, catalog: CatalogName, source_spec: TDRSourceSpec) -> None:
        source = self.tdr.lookup_source(source_spec)
        log.info('TDR client is authorized for API access to %s.', source_spec)
        require(source.project == source_spec.project,
                'Actual Google project of TDR source differs from configured one',
                source.project, source_spec.project)
        # Uppercase is standard for multi-regions in the documentation but TDR
        # returns 'us' in lowercase
        require(source.location.lower() == config.tdr_source_location.lower(),
                'Actual storage location of TDR source differs from configured one',
                source.location, config.tdr_source_location)
        # FIXME: Eliminate azul.terra.TDRClient.TDRSource
        #        https://github.com/DataBiosphere/azul/issues/5524
        ref = TDRSourceRef(id=source.id, spec=source_spec)
        plugin = self.repository_plugin(catalog)
        subgraph_count = sum(plugin.list_partitions(ref).values())
        require(subgraph_count > 0,
                'Source spec is empty (bad prefix?)', source_spec)

    def validate_all_sources(self) -> None:
        futures = []
        all_sources = set()
        with ThreadPoolExecutor(max_workers=8) as tpe:
            for catalog in self.catalogs:
                catalog_sources = config.sources(catalog)
                for source in catalog_sources - all_sources:
                    source = TDRSourceSpec.parse(source)
                    futures.append(tpe.submit(self.validate_source, catalog, source))
                all_sources |= catalog_sources
            for completed_future in as_completed(futures):
                futures.remove(completed_future)
                e = completed_future.exception()
                if e is not None:
                    for running_future in futures:
                        running_future.cancel()
                    raise e


def main():
    configure_script_logging(log)
    validator = TerraValidator()
    validator.validate_registration()
    validator.validate_all_sources()
    validator.validate_managed_access()


if __name__ == '__main__':
    main()

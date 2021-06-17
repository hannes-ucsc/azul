"""
This repository plugin allows reading from a canned staging area like the one in
the GitHub repo https://github.com/HumanCellAtlas/schema-test-data .

NOTE: Use of this plugin requires a GitHub personal access token specified in an
environment variable `GITHUB_TOKEN`. See:
https://github.com/DataBiosphere/hca-metadata-api#github-credentials

Due to this requirement, this plugin cannot be used to index data directly from
the canned staging area, however it can be used with the `can_bundle.py` script
to create a local canned bundle from files in the canned staging area.
"""
from dataclasses import (
    dataclass,
)
import logging
import time
from typing import (
    AbstractSet,
    List,
    Optional,
    Sequence,
    Type,
    cast,
)

from furl import (
    furl,
)
from humancellatlas.data.metadata.helpers.staging_area import (
    GitHubStagingAreaFactory,
    StagingArea,
)

from azul import (
    CatalogName,
    config,
    lru_cache,
    require,
)
from azul.indexer import (
    Bundle,
    SimpleSourceName,
    SourceRef,
    SourcedBundleFQID,
)
from azul.plugins import (
    RepositoryFileDownload,
    RepositoryPlugin,
)
from azul.types import (
    JSON,
    MutableJSON,
    MutableJSONs,
)
from azul.uuids import (
    validate_uuid_prefix,
)

log = logging.getLogger(__name__)


class CannedSourceRef(SourceRef[SimpleSourceName, 'CannedSourceRef']):
    pass


CannedBundleFQID = SourcedBundleFQID[CannedSourceRef]


@dataclass(frozen=True)
class Plugin(RepositoryPlugin[SimpleSourceName, CannedSourceRef]):
    _sources: AbstractSet[SimpleSourceName]

    @classmethod
    def create(cls, catalog: CatalogName) -> RepositoryPlugin:
        return cls(
            frozenset(
                SimpleSourceName.parse(name)
                for name in config.canned_sources(catalog)
            )
        )

    @property
    def sources(self) -> AbstractSet[SimpleSourceName]:
        return self._sources

    def lookup_source_id(self, name: SimpleSourceName) -> str:
        return name

    @lru_cache
    def staging_area(self, source_name: SimpleSourceName) -> StagingArea:
        factory = GitHubStagingAreaFactory.from_url(source_name)
        return factory.load_staging_area()

    def _assert_source(self, source: CannedSourceRef):
        assert source.name in self.sources, (source, self.sources)

    def list_bundles(self, source: CannedSourceRef, prefix: str) -> List[CannedBundleFQID]:
        self._assert_source(source)
        prefix = source.name.prefix + prefix
        validate_uuid_prefix(prefix)
        log.info('Listing bundles with prefix %r in source %r.', prefix, source)
        bundle_fqids = []
        for link in self.staging_area(source.name).links.values():
            if link.uuid.startswith(prefix):
                bundle_fqids.append(SourcedBundleFQID(source=source,
                                                      uuid=link.uuid,
                                                      version=link.version))
        log.info('There are %i bundle(s) with prefix %r in source %r.',
                 len(bundle_fqids), prefix, source)
        return bundle_fqids

    def fetch_bundle(self, bundle_fqid: CannedBundleFQID) -> Bundle:
        self._assert_source(bundle_fqid.source)
        now = time.time()
        staging_area = self.staging_area(bundle_fqid.source.name)
        version, manifest, metadata = staging_area.get_bundle_metadata(bundle_fqid.uuid)
        if bundle_fqid.version is None:
            bundle_fqid = SourcedBundleFQID(source=bundle_fqid.source,
                                            uuid=bundle_fqid.uuid,
                                            version=version)
        bundle = CannedBundle(fqid=bundle_fqid,
                              manifest=cast(MutableJSONs, manifest),
                              metadata_files=cast(MutableJSON, metadata))
        assert version == bundle.version, (version, bundle)
        log.info("It took %.003fs to download bundle %s.%s",
                 time.time() - now, bundle.uuid, bundle.version)
        return bundle

    def drs_uri(self, drs_path: str) -> str:
        netloc = config.drs_domain or config.api_lambda_domain('service')
        return f'drs://{netloc}/{drs_path}'

    def portal_db(self) -> Sequence[JSON]:
        return []

    def dss_deletion_subscription_query(self, prefix: str) -> JSON:
        return {}

    def dss_subscription_query(self, prefix: str) -> JSON:
        return {}

    def _construct_file_url(self, source_url: str, file_name: str) -> str:
        """
        >>> source_url = 'https://github.com/USER/REPO/tree/REF/tests'
        >>> Plugin._construct_file_url(Plugin, source_url, 'foo.zip')
        'https://github.com/USER/REPO/raw/REF/tests/foo.zip'

        >>> Plugin._construct_file_url(Plugin, source_url, '')
        Traceback (most recent call last):
        ...
        azul.RequirementError: file_name cannot be empty
        """
        url = furl(source_url)
        require(url.path.segments[2] == 'tree', source_url)
        url.path.segments[2] = 'raw'
        require(len(file_name) > 0, 'file_name cannot be empty')
        require(not file_name.endswith('/'), file_name)
        for segment in file_name.split('/'):
            url.path.segments.append(segment)
        return url.url

    def direct_file_url(self,
                        file_uuid: str,
                        *,
                        file_version: Optional[str] = None,
                        replica: Optional[str] = None,
                        ) -> Optional[str]:
        # Check all sources for the file. If a file_version was specified return
        # when we find a match, otherwise continue checking all sources and
        # return the URL for the match with the latest (largest) version.
        found_version = None
        found_url = None
        for source_name in self.sources:
            staging_area = self.staging_area(source_name)
            try:
                descriptor = staging_area.descriptors[file_uuid]
            except KeyError:
                continue
            else:
                actual_file_version = descriptor.content['file_version']
                if file_version:
                    if file_version == actual_file_version:
                        file_name = descriptor.content['file_name']
                        return self._construct_file_url(source_name, file_name)
                else:
                    if found_version is None or actual_file_version > found_version:
                        file_name = descriptor.content['file_name']
                        found_url = self._construct_file_url(source_name, file_name)
                        found_version = actual_file_version
        return found_url

    def file_download_class(self) -> Type[RepositoryFileDownload]:
        return CannedFileDownload


class CannedFileDownload(RepositoryFileDownload):
    _location: Optional[str] = None
    _retry_after: Optional[int] = None

    def update(self, plugin: RepositoryPlugin) -> None:
        assert isinstance(plugin, Plugin)
        url = plugin.direct_file_url(file_uuid=self.file_uuid,
                                     file_version=self.file_version,
                                     replica=None)
        self._location = url

    @property
    def location(self) -> Optional[str]:
        return self._location

    @property
    def retry_after(self) -> Optional[int]:
        return self._retry_after


class CannedBundle(Bundle[CannedSourceRef]):

    def drs_path(self, manifest_entry: JSON) -> Optional[str]:
        return None
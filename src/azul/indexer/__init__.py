from abc import (
    ABC,
    abstractmethod,
)
from collections import (
    Counter,
    defaultdict,
)
from itertools import groupby
import logging
from operator import attrgetter
import time
from typing import (
    Iterable,
    List,
    Mapping,
    MutableMapping,
    MutableSet,
    Union,
    Tuple,
    Optional,
)

from elasticsearch import (
    ConflictError,
    ElasticsearchException,
)
from elasticsearch.helpers import (
    parallel_bulk,
    scan,
    streaming_bulk,
)
from humancellatlas.data.metadata.helpers.dss import download_bundle_metadata
from more_itertools import one

from azul import config
import azul.dss
from azul.deployment import aws
from azul.es import ESClientFactory
from azul.transformer import (
    Aggregate,
    AggregatingTransformer,
    Contribution,
    Document,
    DocumentCoordinates,
    EntityReference,
    FieldTypes,
    Transformer,
    BundleUUID,
    VersionType,
)
from azul.types import JSON

log = logging.getLogger(__name__)

Tallies = Mapping[EntityReference, int]


class BaseIndexer(ABC):
    """
    The base indexer class provides the framework to do indexing.
    """

    @abstractmethod
    def mapping(self) -> JSON:
        raise NotImplementedError()

    def settings(self, index_name) -> JSON:
        # Setting a large number of shards for the contributions indexes (i.e. not aggregate) greatly speeds up indexing
        # which is our biggest bottleneck, however doing the same for the aggregate index dramatically limits searching.
        # This was because every search had to check every shard and nodes became overburdened with so many requests.
        # Instead we try using one shard per ES node which is optimal for searching since it allows parallelization of
        # requests (though maybe at the cost of higher contention during indexing).
        _, aggregate = config.parse_es_index_name(index_name)
        num_shards = aws.es_instance_count if aggregate else config.indexer_concurrency
        return {
            "index": {
                "number_of_shards": num_shards,
                "number_of_replicas": 1,
                "refresh_interval": f"{config.es_refresh_interval}s"
            }
        }

    @classmethod
    @abstractmethod
    def transformers(cls) -> Iterable[Transformer]:
        raise NotImplementedError()

    @abstractmethod
    def entities(self) -> Iterable[str]:
        raise NotImplementedError()

    @classmethod
    def field_types(cls) -> FieldTypes:
        """
        Returns a mapping of fields to field types

        :return: dict with nested keys matching Elasticsearch fields and values with the field's type
        """
        field_types = {}
        for transformer in cls.transformers():
            field_types.update(transformer.field_types())
        return {
            **Contribution.field_types(field_types),
            **Aggregate.field_types(field_types)
        }

    def index_names(self, aggregate=None) -> List[str]:
        aggregates = (False, True) if aggregate is None else (aggregate,)
        return [config.es_index_name(entity, aggregate=aggregate)
                for entity in self.entities()
                for aggregate in aggregates]

    def index(self, dss_notification: JSON) -> None:
        """
        Index the bundle referenced by the given notification. This is an inefficient default implementation. A more
        efficient implementation would transform many bundles, collect their contributions and aggregate all affected
        entities at the end.
        """
        contributions = self.transform(dss_notification, delete=False)
        tallies = self.contribute(contributions)
        self.aggregate(tallies)

    def transform(self, dss_notification: JSON, delete: bool) -> List[Contribution]:
        """
        Transform the metadata in the bundle referenced by the given notification into a list of contributions to
        documents, each document representing one metadata entity in the index.
        """
        bundle_uuid = dss_notification['match']['bundle_uuid']
        bundle_version = dss_notification['match']['bundle_version']
        manifest, metadata_files = self._get_bundle(bundle_uuid, bundle_version)

        contributions = []
        # Filter out bundles that don't have project metadata. `project.json` is used in very old v5 bundles which only
        # occur as cans in tests these days.
        if 'project_0.json' in metadata_files or 'project.json' in metadata_files:
            bundle_uuid, bundle_version = self._add_test_modifications(bundle_uuid,
                                                                       bundle_version,
                                                                       manifest,
                                                                       metadata_files,
                                                                       dss_notification)
            # FIXME: this seems out of place. Consider creating indices at deploy time and avoid the mostly
            # redundant requests for every notification (https://github.com/DataBiosphere/azul/issues/427)
            self._create_indices()
            log.info('Transforming metadata for bundle %s, version %s.', bundle_uuid, bundle_version)
            for transformer in self.transformers():
                contributions.extend(transformer.transform(uuid=bundle_uuid,
                                                           version=bundle_version,
                                                           deleted=delete,
                                                           manifest=manifest,
                                                           metadata_files=metadata_files))
        else:
            log.warning('Ignoring bundle %s, version %s because it lacks project metadata.')
        return contributions

    def _create_indices(self):
        es_client = ESClientFactory.get()
        for index_name in self.index_names():
            es_client.indices.create(index=index_name,
                                     ignore=[400],
                                     body=dict(settings=self.settings(index_name),
                                               mappings=dict(doc=self.mapping())))

    def _get_bundle(self, bundle_uuid, bundle_version):
        now = time.time()
        dss_client = azul.dss.direct_access_client(num_workers=config.num_dss_workers)
        _, manifest, metadata_files = download_bundle_metadata(client=dss_client,
                                                               replica='aws',
                                                               uuid=bundle_uuid,
                                                               version=bundle_version,
                                                               num_workers=config.num_dss_workers)
        log.info("It took %.003fs to download bundle %s.%s", time.time() - now, bundle_uuid, bundle_version)
        assert _ == bundle_version
        return manifest, metadata_files

    def _add_test_modifications(self, bundle_uuid, bundle_version, manifest, metadata_files, dss_notification):
        try:
            test_name = dss_notification['test_name']
        except KeyError:
            return bundle_uuid, bundle_version
        else:
            for file in manifest:
                if file['name'] == 'project_0.json':
                    test_uuid = dss_notification['test_uuid']
                    file['uuid'] = test_uuid
                    project_json = metadata_files['project_0.json']
                    project_json['project_core']['project_short_name'] = test_name
                    project_json['provenance']['document_id'] = test_uuid
                    break
            else:
                assert False
            # When indexing a test bundle we want to change its UUID so that we
            # can delete it later. We change the version to ensure that the test
            # bundle will always be selected to contribute to a shared entity
            # (the test bunde version was set to the current time when the
            # notification is sent).
            return dss_notification['test_bundle_uuid'], dss_notification['test_bundle_version']

    def contribute(self, contributions: List[Contribution]) -> Tallies:
        """
        Write the given entity contributions to the index and return tallies, a
        dictionary tracking the number of contributions made to each entity.

        Tallies for overwritten documents are not counted. This means a tally
        with a count of 0 may exist. This is ok. See description of aggregate().
        """
        writer = self._create_writer()
        tallies = Counter(c.entity for c in contributions)
        while True:
            writer.write(contributions)
            if not writer.retries:
                break
            contributions = [c for c in contributions if c.coordinates in writer.retries]
        writer.raise_on_errors()
        overwrites = (c for c in contributions if c.version_type is VersionType.none)
        for contribution in overwrites:
            tallies[contribution.entity] -= 1
        return tallies

    def aggregate(self, tallies: Tallies):
        """
        Read all contributions to the entities listed in the given tallies from
        the index, aggregate the contributions into one aggregate per entity and
         write the resulting aggregates to the index.

        Normally there is a 1 to 1 correspondence between number of
        contributions for an entity and the value for a tally, however tallies
        are not counted for updates. This means, in the case of a duplicate
        notification or writing over an already populated index, it's possible
        to receive a tally with a value of 0. We still need to aggregate (if the
        indexed format changed for example). Tallies are a lower bound for the
        number of contributions in the index for a given entity.
        """
        writer = self._create_writer()
        while True:
            # Read the aggregates
            old_aggregates = self._read_aggregates(tallies)
            total_tallies = Counter(tallies)
            total_tallies.update({
                old_aggregate.entity: old_aggregate.num_contributions
                for old_aggregate in old_aggregates.values()
            })
            # Read all contributions from Elasticsearch
            contributions = self._read_contributions(total_tallies)
            actual_tallies = Counter(contribution.entity for contribution in contributions)
            assert len(tallies) == len(actual_tallies)
            assert all(tallies[entity] <= actual_tally for entity, actual_tally in actual_tallies.items())
            # Combine the contributions into old_aggregates, one per entity
            new_aggregates = self._aggregate(contributions)
            # Set the expected document version from the old version
            for new_aggregate in new_aggregates:
                old_aggregate = old_aggregates.pop(new_aggregate.entity, None)
                new_aggregate.version = None if old_aggregate is None else old_aggregate.version
            # Empty out any unreferenced aggregates (can only happen for deletions)
            for old_aggregate in old_aggregates.values():
                old_aggregate.contents = {}
                new_aggregates.append(old_aggregate)
            # Write aggregates to Elasticsearch
            writer.write(new_aggregates)
            # Retry if necessary
            if not writer.retries:
                break
            tallies = {aggregate.entity: tallies[aggregate.entity]
                       for aggregate in new_aggregates
                       if aggregate.coordinates in writer.retries}
        writer.raise_on_errors()

    def _read_aggregates(self, entities: Iterable[EntityReference]) -> MutableMapping[EntityReference, Aggregate]:
        request = dict(docs=[dict(_type=Aggregate.type,
                                  _index=Aggregate.index_name(entity.entity_type),
                                  _id=entity.entity_id)  # FIXME: assumes that document_id is entity_id for aggregates
                             for entity in entities])
        response = ESClientFactory.get().mget(body=request, _source_include=Aggregate.mandatory_source_fields())
        aggregates = (Aggregate.from_index(self.field_types(), doc) for doc in response['docs'] if doc['found'])
        aggregates = {a.entity: a for a in aggregates}
        return aggregates

    def _read_contributions(self, tallies: Tallies) -> List[Contribution]:
        es_client = ESClientFactory.get()
        query = {
            "query": {
                "terms": {
                    "entity_id.keyword": [e.entity_id for e in tallies.keys()]
                }
            }
        }
        index = sorted(list({config.es_index_name(e.entity_type, aggregate=False) for e in tallies.keys()}))
        # scan() uses a server-side cursor and is expensive. Only use it if the number of contributions is large
        page_size = 100
        num_contributions = sum(tallies.values())
        hits = None
        if num_contributions <= page_size:
            log.info('Reading %i expected contribution(s) using search().', num_contributions)
            response = es_client.search(index=index, body=query, size=page_size, doc_type=Document.type)
            total_hits = response['hits']['total']
            if total_hits <= page_size:
                hits = response['hits']['hits']
                assert len(hits) == total_hits
            else:
                log.info('Expected only %i contribution(s) but got %i.', num_contributions, total_hits)
                num_contributions = total_hits
        if hits is None:
            log.info('Reading %i expected contribution(s) using scan().', num_contributions)
            hits = scan(es_client, index=index, query=query, size=page_size, doc_type=Document.type)
        contributions = [Contribution.from_index(self.field_types(), hit) for hit in hits]

        entity_key = attrgetter('entity')
        log.info('Read %i contribution(s). Breakdown by entity: %s',
                 len(contributions),
                 {
                     f'{entity.entity_type}/{entity.entity_id}': sum(1 for _ in contribution_group)
                     for entity, contribution_group in groupby(sorted(contributions, key=entity_key), key=entity_key)
                 })
        return contributions

    def _aggregate(self, contributions: List[Contribution]) -> List[Aggregate]:
        # Group contributions by entity and bundle UUID
        contributions_by_bundle: Mapping[Tuple[EntityReference, BundleUUID], List[Contribution]] = defaultdict(list)
        tallies = Counter()
        for contribution in contributions:
            contributions_by_bundle[contribution.entity, contribution.bundle_uuid].append(contribution)
            # Track the raw, unfiltered number of contributions per entity
            tallies[contribution.entity] += 1

        # For each entity and bundle, find the most recent contribution that is not a deletion
        contributions_by_entity: Mapping[EntityReference, List[Contribution]] = defaultdict(list)
        for (entity, bundle_uuid), contributions in contributions_by_bundle.items():
            contributions = sorted(contributions, key=attrgetter('bundle_version', 'bundle_deleted'), reverse=True)
            for bundle_version, group in groupby(contributions, key=attrgetter('bundle_version')):
                contribution = next(group)
                if not contribution.bundle_deleted:
                    assert bundle_uuid == contribution.bundle_uuid
                    assert bundle_version == contribution.bundle_version
                    assert entity == contribution.entity
                    contributions_by_entity[entity].append(contribution)
                    break

        # Create lookup for transformer by entity type
        transformers = {t.entity_type(): t for t in self.transformers() if isinstance(t, AggregatingTransformer)}

        # Aggregate contributions for the same entity
        aggregates = []
        for entity, contributions in contributions_by_entity.items():
            log.info('Selected %i contributions to be aggregated for entity %s/%s.',
                     len(contributions), entity.entity_type, entity.entity_id)
            transformer = transformers[entity.entity_type]
            contents = transformer.aggregate(contributions)
            bundles = [dict(uuid=c.bundle_uuid, version=c.bundle_version) for c in contributions]
            aggregate = Aggregate(entity=entity,
                                  version=None,
                                  contents=contents,
                                  bundles=bundles,
                                  num_contributions=tallies[entity])
            aggregates.append(aggregate)

        return aggregates

    def delete(self, dss_notification: JSON) -> None:
        """
        Synchronous form of delete that is currently only used for testing.

        In production code, there is an SQS queue between the calls to
        `contribute()` and `aggregate()`.
        """
        # FIXME: this only works if the bundle version is not being indexed
        #        concurrently. The fix could be to optimistically lock on the
        #        aggregate version (https://github.com/DataBiosphere/azul/issues/611)
        contributions = self.transform(dss_notification, delete=True)
        # FIXME: these are all modified contributions, not new ones. This also
        #        happens when we reindex without deleting the indices first. The
        #        tallies refer to number of updated or added contributions but
        #        we treat them as if they are all new when we estimate the
        #        number of contributions per bundle.
        # https://github.com/DataBiosphere/azul/issues/610
        tallies = self.contribute(contributions)
        self.aggregate(tallies)

    def _create_writer(self) -> 'IndexWriter':
        # We allow one conflict retry in the case of duplicate notifications and
        # switch from 'add' to 'update'. After that, there should be no
        # conflicts because we use an SQS FIFO message group per entity. For
        # other errors we use SQS message redelivery to take care of the
        # retries.
        return IndexWriter(self.field_types(), refresh=False, conflict_retry_limit=1, error_retry_limit=0)


class IndexWriter:

    def __init__(self,
                 field_types: FieldTypes,
                 refresh: Union[bool, str],
                 conflict_retry_limit: int,
                 error_retry_limit: int) -> None:
        """
        :param field_types: A mapping of field paths to field type

        :param refresh: https://www.elastic.co/guide/en/elasticsearch/reference/5.5/docs-refresh.html

        :param conflict_retry_limit: The maximum number of retries (the second
                                     attempt is the first retry) on version
                                     conflicts. Specify 0 for no retries or None
                                     for unlimited retries.

        :param error_retry_limit: The maximum number of retries (the second
                                  attempt is the first retry) on other errors.
                                  Specify 0 for no retries or None for
                                  unlimited retries.
        """
        super().__init__()
        self.field_types = field_types
        self.refresh = refresh
        self.conflict_retry_limit = conflict_retry_limit
        self.error_retry_limit = error_retry_limit
        self.es_client = ESClientFactory.get()
        self.errors: MutableMapping[DocumentCoordinates, int] = defaultdict(int)
        self.conflicts: MutableMapping[DocumentCoordinates, int] = defaultdict(int)
        self.retries: Optional[MutableSet[DocumentCoordinates]] = None

    bulk_threshold = 32

    def write(self, documents: List[Document]):
        """
        Make an attempt to write the documents into the index, updating local
        state with failures and conflicts

        :param documents: Documents to index
        """
        # documents.sort(key=attrgetter('coordinates'))
        self.retries = set()
        if len(documents) < self.bulk_threshold:
            self._write_individually(documents)
        else:
            self._write_bulk(documents)

    def _write_individually(self, documents: Iterable[Document]):
        log.info('Writing documents individually')
        for doc in documents:
            try:
                method = self.es_client.delete if doc.delete else self.es_client.index
                method(refresh=self.refresh, **doc.to_index(self.field_types))
            except ConflictError as e:
                self._on_conflict(doc, e)
            except ElasticsearchException as e:
                self._on_error(doc, e)
            else:
                self._on_success(doc)

    def _write_bulk(self, documents: Iterable[Document]):
        documents: Mapping[DocumentCoordinates, Document] = {doc.coordinates: doc for doc in documents}
        actions = []
        for coords, doc in documents.items():
            actions.append(doc.to_index(self.field_types, bulk=True))
        if len(actions) < 1024:
            log.info('Writing documents using streaming_bulk().')
            helper = streaming_bulk
        else:
            log.info('Writing documents using parallel_bulk().')
            helper = parallel_bulk
        response = helper(client=self.es_client,
                          actions=actions,
                          refresh=self.refresh,
                          raise_on_error=False,
                          max_chunk_bytes=10485760)
        for success, info in response:
            op_type, info = one(info.items())
            assert op_type in ('index', 'create', 'delete')
            coordinates = DocumentCoordinates(document_index=info['_index'], document_id=info['_id'])
            doc = documents[coordinates]
            if success:
                self._on_success(doc)
            else:
                if info['status'] == 409:
                    self._on_conflict(doc, info)
                else:
                    self._on_error(doc, info)

    def _on_success(self, doc: Document):
        coordinates = doc.coordinates
        self.conflicts.pop(coordinates, None)
        self.errors.pop(coordinates, None)
        if isinstance(doc, Aggregate):
            log.debug('Successfully wrote document %s/%s with %i contribution(s).',
                      coordinates.document_index, coordinates.document_id, doc.num_contributions)
        else:
            log.debug('Successfully wrote document %s/%s.',
                      coordinates.document_index, coordinates.document_id)

    def _on_error(self, doc: Document, e):
        self.errors[doc.coordinates] += 1
        if self.error_retry_limit is None or self.errors[doc.coordinates] <= self.error_retry_limit:
            action = 'retrying'
            self.retries.add(doc.coordinates)
        else:
            action = 'giving up'
        log.warning('There was a general error with document %r: %r. Total # of errors: %i, %s.',
                    doc.coordinates, e, self.errors[doc.coordinates], action)

    def _on_conflict(self, doc: Document, e):
        self.conflicts[doc.coordinates] += 1
        self.errors.pop(doc.coordinates, None)  # a conflict resets the error count
        if self.conflict_retry_limit is None or self.conflicts[doc.coordinates] <= self.conflict_retry_limit:
            action = 'retrying'
            self.retries.add(doc.coordinates)
        else:
            action = 'giving up'
        if doc.version_type is VersionType.create_only:
            log.warning('Writing document %r requires overwrite. Possible causes include duplicate notifications '
                        'or reindexing without clearing the index.', doc.coordinates)
            # Try again but allow overwriting
            doc.version_type = VersionType.none
        else:
            log.warning('There was a conflict with document %r: %r. Total # of errors: %i, %s.',
                        doc.coordinates, e, self.conflicts[doc.coordinates], action)

    def raise_on_errors(self):
        if self.errors or self.conflicts:
            log.warning('Failures: %r', self.errors)
            log.warning('Conflicts: %r', self.conflicts)
            raise RuntimeError('Failed to index documents. Failures: %i, conflicts: %i.' %
                               (len(self.errors), len(self.conflicts)))
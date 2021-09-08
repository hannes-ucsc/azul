import json
from typing import (
    Mapping,
)

import attr
from chalice import (
    BadRequestError,
    Response,
)
from furl import (
    furl,
)

from azul import (
    CatalogName,
    cached_property,
    config,
)
from azul.chalice import (
    GoneError,
)
from azul.service import (
    Controller,
)
from azul.service.async_manifest_service import (
    AsyncManifestService,
    InvalidTokenError,
    Token,
)
from azul.service.manifest_service import (
    Manifest,
    ManifestFormat,
    ManifestPartition,
    ManifestService,
    ManifestUrlFunc,
)
from azul.service.storage_service import (
    StorageService,
)
from azul.types import (
    JSON,
)


@attr.s(frozen=True, auto_attribs=True, kw_only=True)
class ManifestController(Controller):
    step_function_lambda_name: str
    manifest_url_func: ManifestUrlFunc

    @cached_property
    def async_service(self) -> AsyncManifestService:
        name = config.state_machine_name(self.step_function_lambda_name)
        async_service = AsyncManifestService(name)
        return async_service

    @cached_property
    def service(self) -> ManifestService:
        return ManifestService(StorageService(), self.file_url_func)

    partition_state_key = 'partition'

    manifest_state_key = 'manifest'

    def get_manifest(self, state: JSON) -> JSON:
        partition = ManifestPartition.from_json(state[self.partition_state_key])
        result = self.service.get_manifest(format_=ManifestFormat(state['format_']),
                                           catalog=state['catalog'],
                                           filters=state['filters'],
                                           partition=partition,
                                           object_key=state['object_key'])
        if isinstance(result, ManifestPartition):
            assert not result.is_last, result
            return {
                **state,
                self.partition_state_key: result.to_json()
            }
        elif isinstance(result, Manifest):
            return {
                # The presence of this key terminates the step function loop
                self.manifest_state_key: result.to_json()
            }
        else:
            assert False, type(result)

    def get_manifest_async(self,
                           *,
                           self_url: str,
                           catalog: CatalogName,
                           query_params: Mapping[str, str],
                           fetch: bool):

        token = query_params.get('token')
        if token is None:
            format_ = ManifestFormat(query_params['format'])
            filters = self.service.parse_filters(query_params['filters'])
            try:
                object_key = query_params['objectKey']
            except KeyError:
                object_key, manifest = self.service.get_cached_manifest(format_=format_,
                                                                        catalog=catalog,
                                                                        filters=filters)
                if manifest is None:
                    assert object_key is not None
                    partition = ManifestPartition.first()
                    state = {
                        'format_': format_.value,
                        'catalog': catalog,
                        'filters': filters,
                        'object_key': object_key,
                        self.partition_state_key: partition.to_json()
                    }
                    token = self.async_service.start_generation(state)
            else:
                manifest = self.service.get_cached_manifest_with_object_key(
                    format_=format_,
                    catalog=catalog,
                    filters=filters,
                    object_key=object_key
                )
                if manifest is None:
                    raise GoneError('The requested manifest has expired, '
                                    'please request a new one')
        else:
            try:
                token = Token.decode(token)
                token_or_state = self.async_service.inspect_generation(token)
            except InvalidTokenError as e:
                raise BadRequestError(e.args) from e
            else:
                if isinstance(token_or_state, Token):
                    token, manifest = token_or_state, None
                elif isinstance(token_or_state, JSON.__origin__):
                    manifest = Manifest.from_json(token_or_state[self.manifest_state_key])
                else:
                    assert False, token_or_state

        if manifest is None:
            location = furl(self_url, args={'token': token.encode()})
            body = {
                'Status': 301,
                'Location': location.url,
                'Retry-After': token.wait_time
            }
        else:
            if fetch:
                url = self.manifest_url_func(fetch=False,
                                             catalog=manifest.catalog,
                                             format_=manifest.format_,
                                             filters=json.dumps(manifest.filters),
                                             objectKey=manifest.object_key)
            else:
                url = manifest.location
            body = {
                'Status': 302,
                'Location': url,
                'CommandLine': self.service.command_lines(manifest, url)
            }

        if fetch:
            return Response(body=body)
        else:
            headers = {k: str(body[k]) for k in body.keys() & {'Location', 'Retry-After'}}
            return Response(body='', status_code=body['Status'], headers=headers)
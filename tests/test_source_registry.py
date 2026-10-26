import pytest
from pydantic import ValidationError
from relay.source_registry import SourceRegistry


def registry(**overrides):
    source = dict(category='sut', scope_id='service-a', environments=['uat'],
                  provider='local', location='/private/docs', files=['swagger.json'], format='openapi')
    source.update(overrides)
    return SourceRegistry.model_validate({'sources': {'contract': source}})


def test_authorized_original_source_and_private_location():
    value = registry()
    assert value.authorize('contract', 'sut', 'service-a', 'uat').files == ['swagger.json']
    assert '/private/docs' not in value.model_dump_json()


@pytest.mark.parametrize('identity', [
    ('unknown', 'sut', 'service-a', 'uat'),
    ('contract', 'automation', 'service-a', 'uat'),
    ('contract', 'sut', 'service-b', 'uat'),
    ('contract', 'sut', 'service-a', 'prod'),
])
def test_scope_isolation(identity):
    with pytest.raises(ValueError):
        registry().authorize(*identity)


def test_disabled_source_and_returned_copy():
    with pytest.raises(ValueError):
        registry(enabled=False).authorize('contract', 'sut', 'service-a', 'uat')
    value = registry()
    value.authorize('contract', 'sut', 'service-a', 'uat').environments.append('prod')
    assert value.sources['contract'].environments == ['uat']


@pytest.mark.parametrize('path', ['../secret.txt', '/swagger.json', 'a//b', '.env', 'a/credentials.json', 'a/*.json'])
def test_unsafe_paths(path):
    with pytest.raises(ValidationError):
        registry(files=[path])


@pytest.mark.parametrize('location', ['relative/path', '/a/../b', '/a/./b'])
def test_invalid_roots(location):
    with pytest.raises(ValidationError):
        registry(location=location)

import pytest
import requests

from scripts.website_analysis.proxy_adapters import adapter_from_env, BrightDataAdapter, DirectAdapter
from scripts.website_analysis.ads_transparency_experiment import EvidenceSession


def test_brightdata_encodes_credentials_and_keeps_tls_verification():
    adapter = BrightDataAdapter('brd-customer-test-zone-residential', 'p@ss:/word')
    options = adapter.request_options()
    assert options['proxies']['https'] == 'http://brd-customer-test-zone-residential:p%40ss%3A%2Fword@brd.superproxy.io:33335'
    assert options['verify'] is True
    assert 'p@ss' not in repr(adapter)


def test_explicit_provider_does_not_fall_back_to_direct():
    with pytest.raises(ValueError, match='BRIGHTDATA_USERNAME'):
        adapter_from_env('brightdata', {})
    assert adapter_from_env('direct', {'OXYLABS_USERNAME': 'ignored'}).request_options() == DirectAdapter().request_options()


def test_oxylabs_and_custom_ca():
    adapter = adapter_from_env('oxylabs', {'OXYLABS_USERNAME': 'customer-test', 'OXYLABS_PASSWORD': 'secret', 'OXYLABS_CA_BUNDLE': 'trusted.pem'})
    assert adapter.request_options()['proxies']['http'].endswith('@pr.oxylabs.io:7777')
    assert adapter.request_options()['verify'] == 'trusted.pem'


def test_evidence_transport_uses_adapter_without_logging_credentials(tmp_path, monkeypatch):
    calls = []
    def send(self, method, url, **kwargs):
        calls.append(kwargs)
        response = requests.Response()
        response.status_code = 200
        response._content = b'{}'
        return response
    monkeypatch.setattr(requests.Session, 'request', send)
    adapter = BrightDataAdapter('test', 'secret')
    with EvidenceSession(tmp_path, adapter=adapter) as session:
        assert session.trust_env is False
        session.post('https://example.com', proxies={})
    assert calls[0]['proxies'] == adapter.request_options()['proxies']
    assert calls[0]['verify'] is True
    evidence = (tmp_path / 'http.json').read_text()
    assert 'secret' not in evidence and 'brightdata' in evidence


def test_population_cannot_change_provider_on_resume(tmp_path):
    import json
    from scripts.website_analysis import ads_transparency_population as population
    (tmp_path / 'manifest.json').write_text(json.dumps({'companies': [], 'domains': []}))
    with pytest.raises(SystemExit) as error:
        population.main(['--output', str(tmp_path), '--proxy-provider', 'brightdata', '--dry-run'])
    assert error.value.code == 2
    assert not (tmp_path / 'state.json').exists()

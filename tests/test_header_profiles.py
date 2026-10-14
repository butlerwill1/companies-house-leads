import requests
from scripts.website_analysis.header_profiles import HeaderRotation, BROWSER_PROFILES
from scripts.website_analysis.ads_transparency_experiment import EvidenceSession


def test_rotation_cycles_and_research_remains_default():
    rotation = HeaderRotation('browser-rotate')
    assert [rotation.next_profile() for _ in range(4)] == [*BROWSER_PROFILES, BROWSER_PROFILES[0]]
    assert HeaderRotation().next_profile().name == 'research'


def test_profile_stays_consistent_preserves_payload_headers_and_logs_name(tmp_path, monkeypatch):
    calls = []
    def send(self, method, url, **kwargs):
        calls.append(kwargs)
        response = requests.Response()
        response.status_code = 200
        response._content = b'{}'
        return response
    monkeypatch.setattr(requests.Session, 'request', send)
    profile = BROWSER_PROFILES[0]
    with EvidenceSession(tmp_path, header_profile=profile) as session:
        session.get('https://example.com/robots.txt', headers={'User-Agent': 'research'})
        session.post('https://example.com/ads', headers={'user-agent': 'research', 'Content-Type': 'application/x-www-form-urlencoded'})
    assert calls[0]['headers']['User-Agent'] == calls[1]['headers']['User-Agent'] == profile.user_agent
    assert calls[1]['headers']['Content-Type'] == 'application/x-www-form-urlencoded'
    assert 'companies-house-leads' not in calls[1]['headers']['User-Agent']
    assert profile.name in (tmp_path / 'http.json').read_text()

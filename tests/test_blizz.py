"""Tests for blizz.py's pure parsing logic — no network involved."""
import blizz


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def test_list_connected_realms_extracts_ids_from_hrefs(monkeypatch):
    payload = {"connected_realms": [
        {"href": "https://eu.api.blizzard.com/data/wow/connected-realm/1403?namespace=dynamic-eu"},
        {"href": "https://eu.api.blizzard.com/data/wow/connected-realm/1096?namespace=dynamic-eu"},
    ]}
    monkeypatch.setattr(blizz, "api_get", lambda *a, **k: FakeResponse(payload))
    assert blizz.list_connected_realms() == [1403, 1096]


def test_list_connected_realms_handles_empty(monkeypatch):
    monkeypatch.setattr(blizz, "api_get", lambda *a, **k: FakeResponse({}))
    assert blizz.list_connected_realms() == []


def test_connected_realm_slugs_extracts_member_slugs(monkeypatch):
    payload = {"realms": [{"slug": "draenor"}]}
    monkeypatch.setattr(blizz, "api_get", lambda *a, **k: FakeResponse(payload))
    assert blizz.connected_realm_slugs(1403) == ["draenor"]


def test_connected_realm_slugs_handles_multi_realm_cluster(monkeypatch):
    payload = {"realms": [{"slug": "silvermoon"}, {"slug": "die-aldor"}]}
    monkeypatch.setattr(blizz, "api_get", lambda *a, **k: FakeResponse(payload))
    assert blizz.connected_realm_slugs(1096) == ["silvermoon", "die-aldor"]


def test_connected_realm_realms_extracts_name_slug_and_category(monkeypatch):
    payload = {"realms": [{"name": "Draenor", "slug": "draenor", "category": "English"}]}
    monkeypatch.setattr(blizz, "api_get", lambda *a, **k: FakeResponse(payload))
    assert blizz.connected_realm_realms(1403) == [{"name": "Draenor", "slug": "draenor", "category": "English"}]


def test_connected_realm_population_extracts_type(monkeypatch):
    payload = {"population": {"type": "FULL", "name": "Full"}}
    monkeypatch.setattr(blizz, "api_get", lambda *a, **k: FakeResponse(payload))
    assert blizz.connected_realm_population(1403) == "FULL"


def test_connected_realm_population_handles_missing_field(monkeypatch):
    monkeypatch.setattr(blizz, "api_get", lambda *a, **k: FakeResponse({}))
    assert blizz.connected_realm_population(1403) is None

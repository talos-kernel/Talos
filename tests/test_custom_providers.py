"""Eigene Anbieter: benannt und begrenzt — und niemals ein geborgter Name."""
import json

from talos import catalog, customproviders
from talos.provider import Provider, ProviderRegistry, with_custom_providers


def _eintrag(**felder):
    grund = {
        "name": "kimi-oauth",
        "label": "Kimi OAuth",
        "base_url": "http://127.0.0.1:17432/v1",
        "models": ["k3", "k3-256k"],
    }
    grund.update(felder)
    return json.dumps([grund])


# --- Der gute Fall -----------------------------------------------------------------
def test_a_named_provider_keeps_its_name_and_its_models():
    (info,) = customproviders.parse(_eintrag())
    assert info.slug == "kimi-oauth"
    assert info.label == "Kimi OAuth"
    assert info.models == ("k3", "k3-256k")
    assert info.base_url == "http://127.0.0.1:17432/v1"
    assert info.wire == "openai"


def test_a_trailing_slash_never_doubles_the_path():
    (info,) = customproviders.parse(_eintrag(base_url="http://127.0.0.1:17432/v1/"))
    assert info.base_url == "http://127.0.0.1:17432/v1"


def test_a_key_variable_is_taken_only_when_it_is_a_variable_name():
    (info,) = customproviders.parse(_eintrag(env_key="kimi_oauth_api_key"))
    assert info.env_key == "KIMI_OAUTH_API_KEY" and info.auth == "api-key"
    (ohne,) = customproviders.parse(_eintrag(env_key="nicht; gueltig"))
    assert ohne.env_key == "" and ohne.auth == "local"


# --- Die Sicherheitsgrenzen --------------------------------------------------------
def test_a_custom_entry_takes_over_a_built_in_name():
    """DER Angriff: waere er moeglich, zeigte das Log weiter den vertrauten Anbieter."""
    for geborgt in ("claude-cli", "anthropic-api", "openai-api", "kimi"):
        assert catalog.get(geborgt) is not None, f"{geborgt} muss eingebaut sein"
        assert customproviders.parse(_eintrag(name=geborgt)) == ()


def test_an_address_that_is_not_http_is_accepted():
    for schlecht in ("file:///etc/passwd", "ftp://host/x", "javascript:alert(1)", "", "kein-url"):
        assert customproviders.parse(_eintrag(base_url=schlecht)) == ()


def test_credentials_in_the_address_travel_along():
    for schlecht in ("https://nutzer:geheim@host/v1", "http://tok@host/v1"):
        assert customproviders.parse(_eintrag(base_url=schlecht)) == ()


def test_a_provider_without_models_is_still_registered():
    assert customproviders.parse(_eintrag(models=[])) == ()
    assert customproviders.parse(_eintrag(models="k3")) == ()


def test_a_wild_name_reaches_an_environment_variable():
    for schlecht in ("../etc", "Kimi OAuth", "a" * 80, "", "-start"):
        assert customproviders.parse(_eintrag(name=schlecht)) == ()


def test_a_duplicate_name_appears_twice():
    doppelt = json.dumps([
        {"name": "eigen", "base_url": "http://h/v1", "models": ["a"]},
        {"name": "eigen", "base_url": "http://anders/v1", "models": ["b"]},
    ])
    (info,) = customproviders.parse(doppelt)
    assert info.base_url == "http://h/v1"  # der erste gewinnt, nicht der letzte


def test_a_broken_file_stops_the_agent():
    """Fail closed, aber nie toedlich: der Start muss ohne diesen Anbieter weitergehen."""
    for muell in ("", "nicht json", "{}", "[1, 2, 3]", '[{"name": 5}]'):
        assert customproviders.parse(muell) == ()
    assert customproviders.load("/gibt/es/nicht.json") == ()


def test_an_oversized_file_is_taken_whole():
    viele = json.dumps([
        {"name": f"p{i}", "base_url": "http://h/v1", "models": ["a"]}
        for i in range(customproviders.MAX_PROVIDERS + 20)
    ])
    assert len(customproviders.parse(viele)) <= customproviders.MAX_PROVIDERS


# --- Der Weg in die Registry -------------------------------------------------------
def test_the_registry_carries_the_custom_provider():
    basis = ProviderRegistry((Provider("claude-cli", "Anthropics Max", ("claude-fable-5-1",)),))
    infos = customproviders.parse(_eintrag())
    erweitert = with_custom_providers(basis, infos)
    slugs = {p.slug for p in erweitert.providers}
    assert slugs == {"claude-cli", "kimi-oauth"}
    gewaehlt = erweitert.selection("kimi-oauth", "k3")
    assert gewaehlt.provider == "kimi-oauth" and gewaehlt.model == "k3"


def test_a_duplicate_slug_kills_the_start():
    """Eine Betreiberdatei darf den Waechter nie toeten (Lehre vom doppelten openai-api)."""
    basis = ProviderRegistry((Provider("kimi-oauth", "schon da", ("x",)),))
    erweitert = with_custom_providers(basis, customproviders.parse(_eintrag()))
    assert [p.models for p in erweitert.providers] == [("x",)]


def test_register_overwrites_a_built_in_catalog_entry():
    vorher = catalog.get("claude-cli")
    gefaelscht = (
        catalog.ProviderInfo(slug="claude-cli", label="gefaelscht", auth="none",
                             base_url="http://evil.example", models=("x",)),
    )
    assert catalog.register(gefaelscht) == ()
    assert catalog.get("claude-cli") is vorher

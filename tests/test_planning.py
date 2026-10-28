import pytest

from wordpress_manager.planning import setup_plan, site_plan


def test_setup_preserves_detected_php(observer):
    plan = setup_plan(observer.snapshot())
    items = {item["component"]: item for item in plan["items"]}
    assert items["php8.5-fpm"]["action"] == "preserve"
    assert items["php8.5-fpm"]["version"] == "8.5.12"
    assert items["phpmyadmin"]["action"] == "review-installation"
    assert plan["executable"] is False
    assert plan["id"] == setup_plan(observer.snapshot())["id"]


def test_setup_unknown_does_not_install(observer):
    snapshot = observer.snapshot()
    snapshot["software"][0].update(version=None, state="unknown")
    plan = setup_plan(snapshot)
    assert plan["items"][0]["action"] == "review"


def test_running_unpackaged_service_needs_review(observer):
    snapshot = observer.snapshot()
    snapshot["software"][0].update(version=None, state="healthy")
    assert setup_plan(snapshot)["items"][0]["action"] == "review"


def test_changed_baseline_changes_plan(observer):
    snapshot = observer.snapshot()
    first = setup_plan(snapshot)
    snapshot["software"][0]["version"] = "changed"
    assert setup_plan(snapshot)["baseline_digest"] != first["baseline_digest"]


def test_site_plan_isolated_resources_no_automatic_staging():
    first = site_plan("clientone", "One.Example.com")
    second = site_plan("clienttwo", "two.example.com")
    for field in ("linux_user", "database", "document_root", "fpm_socket", "nginx_config"):
        assert first[field] != second[field]
    assert first["domain"] == "one.example.com"
    assert first["create_staging"] is False
    assert first["executable"] is False
    assert first["id"] == site_plan("clientone", "one.example.com")["id"]


@pytest.mark.parametrize("identifier", ["../root", "-bad", "Abc", "ab", "a"*17, "abc;id", "abc\n", "abc_def", "ébc"])
def test_identifier_rejects_unsafe_characters(identifier):
    with pytest.raises(ValueError):
        site_plan(identifier, "example.com")


@pytest.mark.parametrize("domain", ["https://example.com", "example.com/a", "*.example.com", "example.com;id", "localhost", "127.0.0.1", "-bad.example.com", "x..com", "example.com\n", "example.com.", "exämple.com", "a"*64+".com", "a.123"])
def test_domain_rejects_unsafe_input(domain):
    with pytest.raises(ValueError):
        site_plan("abc", domain)


def test_existing_inventory_is_preserved():
    with pytest.raises(ValueError):
        site_plan("abc", "example.com", existing_ids=["abc"])
    with pytest.raises(ValueError):
        site_plan("abc", "example.com", existing_domains=["EXAMPLE.COM"])

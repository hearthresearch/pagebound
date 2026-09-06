import builtins

import pytest

from pagebound import sync
from pagebound.errors import PageboundError


def test_the_public_sync_surface():
    assert callable(sync.sync_items)
    assert callable(sync.iter_items)
    assert callable(sync.zotero_client)
    assert sync.ZoteroItem.__name__ == "ZoteroItem"
    assert sync.library_key("group", 12345) == "group:12345"


def test_zotero_client_explains_the_missing_extra(monkeypatch):
    real_import = builtins.__import__

    def no_pyzotero(name, *args, **kwargs):
        if name.startswith("pyzotero"):
            raise ModuleNotFoundError("No module named 'pyzotero'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pyzotero)
    with pytest.raises(PageboundError, match=r"pagebound\[zotero\]"):
        sync.zotero_client()


def test_zotero_client_builds_a_local_client_for_the_user_library(monkeypatch):
    seen: dict = {}

    class FakeZotero:
        def __init__(self, library_id, library_type, local=False, **kwargs):
            seen.update(library_id=library_id, library_type=library_type, local=local)

    import types
    fake_module = types.SimpleNamespace(zotero=types.SimpleNamespace(Zotero=FakeZotero))
    monkeypatch.setitem(__import__("sys").modules, "pyzotero", fake_module)
    monkeypatch.setitem(__import__("sys").modules, "pyzotero.zotero", fake_module.zotero)

    client, key = sync.zotero_client()
    assert isinstance(client, FakeZotero)
    assert seen == {"library_id": 0, "library_type": "user", "local": True}
    assert key == "user"

    client, key = sync.zotero_client(library="12345")
    assert seen == {"library_id": "12345", "library_type": "group", "local": True}
    assert key == "group:12345"

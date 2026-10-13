"""Tests for the dashboard-resource registration in frontend.py.

No Home Assistant needed: frontend.py only touches Lovelace's resource
collection through a handful of methods, which a fake stands in for.

    python tests/test_frontend.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULE = next(ROOT.glob("custom_components/*/frontend.py"))
spec = importlib.util.spec_from_file_location("card_frontend", MODULE)
frontend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(frontend)

failures = 0


def check(label, got, want):
    global failures
    ok = got == want
    failures += not ok
    print(f"  {'ok  ' if ok else 'FAIL'} {label} => {got!r}" + ("" if ok else f"  (want {want!r})"))


class FakeResources:
    """The parts of ResourceStorageCollection frontend.py relies on."""

    def __init__(self, items=(), *, yaml=False):
        self.store = None if yaml else object()
        self.loaded = False
        self._items = {item["id"]: dict(item) for item in items}
        self._next = 0

    async def async_load(self):
        self.load_calls = getattr(self, "load_calls", 0) + 1

    def async_items(self):
        return list(self._items.values())

    async def async_create_item(self, data):
        assert set(data) == {"res_type", "url"}, data
        self._next += 1
        item = {"id": f"new{self._next}", "type": data["res_type"], "url": data["url"]}
        self._items[item["id"]] = item
        return item

    async def async_update_item(self, item_id, updates):
        item = self._items[item_id]
        if "res_type" in updates:
            item["type"] = updates["res_type"]
        if "url" in updates:
            item["url"] = updates["url"]
        return item

    async def async_delete_item(self, item_id):
        del self._items[item_id]


def hass_with(resources):
    hass = types.SimpleNamespace(data={})
    if resources is not None:
        hass.data["lovelace"] = types.SimpleNamespace(resources=resources)
    return hass


PATH = "/integration/card.js"
OTHER = {"id": "other", "type": "module", "url": "/hacsfiles/mini-graph-card/mini-graph-card-bundle.js?hacstag=1"}
NEAR = {"id": "near", "type": "module", "url": "/integration/card.js.map"}


async def main():
    print("register")
    res = FakeResources([OTHER])
    ok = await frontend.async_register_card_resource(hass_with(res), PATH, "1.0")
    check("returns True in storage mode", ok, True)
    check("loads the collection first", res.loaded, True)
    check("adds one module resource with the version",
          [(i["type"], i["url"]) for i in res.async_items() if i["id"] != "other"],
          [("module", "/integration/card.js?v=1.0")])

    await frontend.async_register_card_resource(hass_with(res), PATH, "1.0")
    check("is idempotent", len(res.async_items()), 2)

    print("upgrade")
    res = FakeResources([{"id": "mine", "type": "module", "url": f"{PATH}?v=0.9"}, OTHER])
    await frontend.async_register_card_resource(hass_with(res), PATH, "1.0")
    check("updates the existing resource in place",
          {i["id"]: i["url"] for i in res.async_items()}["mine"], f"{PATH}?v=1.0")
    check("without adding another", len(res.async_items()), 2)

    print("duplicates")
    res = FakeResources([
        {"id": "a", "type": "module", "url": PATH},
        {"id": "b", "type": "js", "url": f"{PATH}?v=0.1"},
        OTHER,
        NEAR,
    ])
    await frontend.async_register_card_resource(hass_with(res), PATH, "2.0")
    mine = [i for i in res.async_items() if i["url"].split("?")[0] == PATH]
    check("leaves exactly one copy of the card", [(i["id"], i["type"], i["url"]) for i in mine],
          [("a", "module", f"{PATH}?v=2.0")])
    check("and doesn't touch other resources, even with a similar path",
          sorted(i["id"] for i in res.async_items()), ["a", "near", "other"])

    print("concurrent setups")
    res = FakeResources()
    hass = hass_with(res)
    await asyncio.gather(*(frontend.async_register_card_resource(hass, PATH, "1.0") for _ in range(4)))
    check("several entries setting up at once still add one resource", len(res.async_items()), 1)

    print("yaml mode / no lovelace")
    res = FakeResources([OTHER], yaml=True)
    check("YAML resources are reported as not writable",
          await frontend.async_register_card_resource(hass_with(res), PATH, "1.0"), False)
    check("and left alone", len(res.async_items()), 1)
    check("no Lovelace data at all is handled",
          await frontend.async_register_card_resource(hass_with(None), PATH, "1.0"), False)

    print("remove")
    res = FakeResources([{"id": "mine", "type": "module", "url": f"{PATH}?v=1.0"}, OTHER, NEAR])
    await frontend.async_remove_card_resource(hass_with(res), PATH)
    check("removes only the card's resource", sorted(i["id"] for i in res.async_items()), ["near", "other"])
    await frontend.async_remove_card_resource(hass_with(None), PATH)
    check("and is a no-op without Lovelace", True, True)


asyncio.run(main())
print(f"\n{failures} FAILURE(S)" if failures else "\nall tests passed")
sys.exit(1 if failures else 0)

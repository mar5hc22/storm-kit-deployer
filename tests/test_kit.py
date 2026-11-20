"""The kit that ships in this package, checked without a network.

The first tests read the kit folder and make sure nothing a template points at
is missing. The last one deploys every tool against a stand-in for ArcGIS
Online and reads back everything the deployer sent, so a broken template or a
missing file fails here and not half way through a real deployment.
"""

import copy
import io
import json
import re
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deployer"))

import deploy  # noqa: E402
import generated  # noqa: E402
import server  # noqa: E402

KIT = ROOT / "kit"
ALL_TOOLS = ["hub_site", "imagery_compare", "drone_video", "team_status", "fema_damage", "community_status", "request_intake"]
TARGET_ROOT = "https://services9.arcgis.com/TARGETORG/arcgis/rest/services"
SIMPLE = {"portal.url", "user.username", "event.name", "event.NAME", "org.name", "form.itemInfo"}


def kit_index() -> dict:
    return json.loads((KIT / "kit.json").read_text(encoding="utf-8"))


def template(item_id: str) -> dict:
    return json.loads((KIT / "items" / item_id / "template.json").read_text(encoding="utf-8"))


def texts_of(item_id: str):
    """Every text file of a kit item, including the files inside a form package."""
    folder = KIT / "items" / item_id
    for path in folder.rglob("*"):
        if not path.is_file() or path.suffix in (".png", ".bin"):
            continue
        if path.suffix == ".zip":
            archive = zipfile.ZipFile(path)
            for name in archive.namelist():
                if name.lower().endswith(deploy.TEXT_SUFFIXES):
                    yield "%s!%s" % (path.name, name), archive.read(name).decode("utf-8")
        else:
            yield path.name, path.read_text(encoding="utf-8")


# --- the kit folder ----------------------------------------------------------


def test_every_item_has_its_template_and_its_files():
    kit = kit_index()
    assert set(kit) >= {"kit_name", "source_org", "tools", "items"}
    on_disk = {p.name for p in (KIT / "items").iterdir() if p.is_dir()}
    assert on_disk == set(kit["items"]), "kit.json and the items folder disagree"
    for item_id, meta in kit["items"].items():
        tpl = template(item_id)
        folder = KIT / "items" / item_id
        assert tpl["type"] == meta["type"] and tpl["title"] == meta["title"]
        assert set(meta.get("deps", [])) <= set(kit["items"]), "%s depends on an item the kit does not carry" % meta["title"]
        data = tpl.get("data") or {}
        if data.get("file"):
            assert (folder / data["file"]).is_file(), "%s: %s is missing" % (meta["title"], data["file"])
        if tpl.get("thumbnail"):
            assert (folder / tpl["thumbnail"]).is_file(), "%s: its thumbnail is missing" % meta["title"]
        for rel in tpl.get("resources") or []:
            assert (folder / "resources" / rel).is_file(), "%s: resource %s is missing" % (meta["title"], rel)
        if meta["type"] == "Feature Service":
            service = tpl["service"]
            assert service["view_layers"] if service["is_view"] else (service.get("layers") or service.get("tables"))


def test_every_tool_lists_items_the_kit_carries():
    kit = kit_index()
    assert [t["key"] for t in kit["tools"]] == ["team_status", "community_status", "fema_damage", "drone_video"]
    for tool in kit["tools"]:
        assert tool["name"] and tool["about"]
        assert set(tool["items"]) <= set(kit["items"])
        assert deploy.plan_items(kit, [tool["key"]])  # its dependencies resolve


def test_templates_only_use_placeholders_the_deployer_fills():
    kit = kit_index()
    for item_id, meta in kit["items"].items():
        for name, text in texts_of(item_id):
            for found in deploy.unresolved(text):
                inner = found.strip("{}")
                item = deploy.PLACEHOLDER.fullmatch(found)
                if item:
                    assert item.group(1) in kit["items"], "%s (%s) points at %s, which the kit does not carry" % (meta["title"], name, item.group(1))
                else:
                    assert inner in SIMPLE, "%s (%s) uses %s, which the deployer does not fill" % (meta["title"], name, found)


def test_the_kit_carries_no_accounts_tokens_or_builder_notes():
    kit = kit_index()
    assert "report" not in kit and not any("source_title" in meta for meta in kit["items"].values())
    patterns = {
        "a token in a link": re.compile(r"[?&]token=[A-Za-z0-9_\-.]{8,}"),
        "an email address": re.compile(r"[\w.+-]+@[\w-]+\.(gov|com|org|net|us)\b"),
        "an item owner": re.compile(r'"owner"\s*:\s*"(?!\{\{)[^"]+"'),
        "a key or password": re.compile(r'(?i)(api[_-]?key|secret|password)["\']?\s*[:=]\s*["\'][^"\'{]{6,}'),
    }
    for item_id, meta in kit["items"].items():
        for name, text in texts_of(item_id):
            for what, pattern in patterns.items():
                assert not pattern.search(text), "%s (%s) carries %s" % (meta["title"], name, what)


def test_the_page_lists_the_seven_tools_in_the_order_they_were_asked_for():
    info = server.kit_info()
    assert [t["key"] for t in info["tools"]] == ALL_TOOLS
    assert all(t["count"] > 0 for t in info["tools"])
    assert info["left_out"] and info["linked"]


# --- a stand-in for ArcGIS Online ---------------------------------------------


class Response:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    """Answers the two things the deployer asks outside the sharing API."""

    def __init__(self):
        self.sent = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.sent.append(("GET", url))
        if url.startswith("https://hub.arcgis.com/api/v3/domains/"):
            return Response(404)  # the site address is free
        return Response(200, {"singleFusedMapCache": True, "layers": [], "tables": []})  # a public service that answers

    def post(self, url, data=None, headers=None, timeout=None):
        self.sent.append(("POST", url))
        assert url == "https://hub.arcgis.com/api/v3/domains"
        return Response(200, {"id": 4242, "clientKey": "CLIENTKEY", "hostname": json.loads(data)["hostname"]})

    def delete(self, url, headers=None, timeout=None):
        self.sent.append(("DELETE", url))
        return Response(200)


class FakeOnline:
    url = "https://www.arcgis.com"
    _referer = None

    def __init__(self, org_id="TARGETORG"):
        self.org = org_id
        self.session = FakeSession()
        self.items, self.data, self.resources, self.services = {}, {}, {}, {}
        self.groups, self.shared, self.deleted, self.posts = [], {}, [], []
        self.n = 0

    # -- who and where -------------------------------------------------------

    def describe(self):
        return {"portal": self.url, "org_url": "https://target.maps.arcgis.com", "org_name": "Target Org", "org_id": self.org,
                "username": "ann", "full_name": "Ann", "role": "org_admin", "privileges": []}

    def self_info(self):
        return {"id": self.org, "urlKey": "target", "name": "Target Org", "defaultBasemap": {}, "defaultExtent": {}}

    def token(self):
        return "TOKEN"

    def _new(self):
        self.n += 1
        return "%032x" % (0xF00D0000 + self.n)

    def item(self, item_id):
        return dict(self.items[item_id])

    def item_data_bytes(self, item_id):
        return self.data[item_id]

    # -- reads ---------------------------------------------------------------

    def _split(self, service):
        tables = [x for x in service["layers"] if x.get("_table")]
        return [x for x in service["layers"] if not x.get("_table")], tables

    def get(self, path, params=None, timeout=None):
        if path.endswith("isServiceNameAvailable"):
            return {"available": True}
        if path.endswith("/query"):
            return {"count": 0}
        if path.startswith("content/users/"):
            return {"folders": [], "items": []}
        for url, service in self.services.items():
            if path == url:
                layers, tables = self._split(service)
                described = {}
                for source in [service["create"]] + service["updates"]:
                    described.update({k: source[k] for k in deploy.LATE_TEXT if k in source})
                return dict(described, layers=[{"id": x["id"], "name": x.get("name")} for x in layers], tables=[{"id": x["id"], "name": x.get("name")} for x in tables])
            if path.startswith(url + "/"):
                layer = next(x for x in service["layers"] if str(x["id"]) == path.rsplit("/", 1)[1])
                return {"id": layer["id"], "fields": [f for f in layer.get("fields", []) if f.get("visible", True)]}
        raise AssertionError("unexpected GET " + path)

    # -- writes --------------------------------------------------------------

    def post(self, path, params=None, files=None, timeout=None):
        params = dict(params or {})
        self.posts.append((path, params, files))
        if path.endswith("/createFolder"):
            return {"folder": {"id": "FOLDER", "title": params["title"]}}
        if path == "community/createGroup":
            group = {"id": self._new(), "title": params["title"]}
            self.groups.append(group)
            return {"success": True, "group": group}
        if path.endswith("/createService"):
            create = json.loads(params["createParameters"])
            item_id = self._new()
            url = "%s/%s/FeatureServer" % (TARGET_ROOT, create["name"])
            assert url not in self.services, "two services were given the name %s" % create["name"]
            self.services[url] = {"create": create, "layers": [], "updates": [], "made_as_view": params.get("isView") == "true"}
            self.items[item_id] = {"id": item_id, "type": "Feature Service", "title": create["name"], "typeKeywords": ["Feature Service", "Hosted Service"]}
            return {"success": True, "itemId": item_id, "serviceurl": url}
        if "/rest/admin/services/" in path:
            return self._definition(path, params)
        if path.endswith("/addItem"):
            item_id = self._new()
            self.items[item_id] = {"id": item_id, "type": params["type"], "title": params["title"], "typeKeywords": [k for k in (params.get("typeKeywords") or "").split(",") if k]}
            if "text" in params:
                self.data[item_id] = params["text"].encode("utf-8")
            if files and "file" in files:
                self.data[item_id] = files["file"][1]
            return {"success": True, "id": item_id}
        if path.endswith("/update"):
            item_id = path.split("/")[-2]
            assert item_id in self.items
            if "text" in params:
                self.data[item_id] = params["text"].encode("utf-8")
            if files and "file" in files:
                self.data[item_id] = files["file"][1]
            if "typeKeywords" in params:
                self.items[item_id]["typeKeywords"] = params["typeKeywords"].split(",")
            self.items[item_id].update({k: v for k, v in params.items() if k in ("title", "url", "snippet", "description")})
            return {"success": True}
        if path.endswith("/addResources"):
            item_id = path.split("/")[-2]
            name = "/".join(x for x in (params.get("resourcesPrefix"), params["fileName"]) if x)
            self.resources.setdefault(item_id, {})[name] = files["file"][1]
            return {"success": True}
        if path.endswith("/share"):
            self.shared[path.split("/")[-2]] = params
            return {"notSharedWith": []}
        if path.endswith("/addRelationship"):
            assert params["originItemId"] in self.items and params["destinationItemId"] in self.items
            return {"success": True}
        if path.endswith("/delete"):
            self.deleted.append(path)
            return {"success": True}
        raise AssertionError("unexpected POST " + path)

    def _definition(self, path, params):
        url, verb = path.replace("/rest/admin/services/", "/rest/services/").rsplit("/", 1)
        body = json.loads(params.get("addToDefinition") or params.get("updateDefinition"))
        if verb == "addToDefinition":
            service = self.services[url]
            incoming = [(x, False) for x in body.get("layers", [])] + [(x, True) for x in body.get("tables", [])]
            for entry, is_table in incoming:
                existing = next((layer for layer in service["layers"] if layer["id"] == entry["id"]), None)
                if existing:
                    existing.update(entry)
                    continue
                entry = dict(entry, _table=is_table)
                view = (entry.get("adminLayerInfo") or {}).get("viewLayerDefinition")
                if view:
                    assert service["made_as_view"], "a view layer was added to a service that was not created as a view"
                    source = self.services["%s/%s/FeatureServer" % (TARGET_ROOT, view["sourceServiceName"])]
                    source_layer = next(x for x in source["layers"] if x["id"] == view["sourceLayerId"])
                    entry["fields"] = copy.deepcopy(source_layer.get("fields", []))
                service["layers"].append(entry)
        elif url in self.services:
            self.services[url]["updates"].append(body)
        else:  # one layer of a service
            service_url, layer_id = url.rsplit("/", 1)
            layer = next(x for x in self.services[service_url]["layers"] if str(x["id"]) == layer_id)
            for change in body.get("fields", []):
                next(f for f in layer["fields"] if f["name"] == change["name"]).update(change)
            layer.update({k: v for k, v in body.items() if k != "fields"})
        return {"success": True}

    # -- everything the deployer sent, as text ---------------------------------

    def sent_text(self):
        for item_id, raw in self.data.items():
            item = self.items[item_id]
            if item["type"] == "Form":
                archive = zipfile.ZipFile(io.BytesIO(raw))
                for name in archive.namelist():
                    if name.lower().endswith(deploy.TEXT_SUFFIXES):
                        yield "%s!%s" % (item["title"], name), archive.read(name).decode("utf-8")
            elif item["type"] != "Image":
                yield item["title"], raw.decode("utf-8")
        for item_id, files in self.resources.items():
            for name, raw in files.items():
                if name.lower().endswith(".json"):
                    yield "%s resource %s" % (self.items[item_id]["title"], name), raw.decode("utf-8")
        for url, service in self.services.items():
            yield url, json.dumps(service)
        for item in self.items.values():
            yield item["title"] + " (item)", json.dumps(item)


@pytest.fixture()
def online(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "RUNS", tmp_path / "runs")
    return FakeOnline()


# --- the whole kit, deployed ----------------------------------------------------


def test_every_tool_deploys_and_reads_back_clean(online):
    lines = []
    deployer = deploy.Deployer(online, KIT, "Spring Flood 2027", ALL_TOOLS, log=lines.append)
    planned = deployer.plan()
    record = deployer.run()
    assert record["finished"], "\n".join(lines[-10:])
    assert record["problems"] == [], record["problems"]
    assert not [w for w in record["warnings"]], record["warnings"]

    counts = {}
    for item in record["items"]:
        counts[item["type"]] = counts.get(item["type"], 0) + 1
    assert counts == {"Feature Service": 17, "Web Map": 9, "Dashboard": 6, "Form": 4, "Web Experience": 3, "Image": 2, "Notebook": 1, "Hub Site Application": 1}
    assert len(record["items"]) == 43 == len([row for row in planned if row["type"] != "Group"])
    assert all(item["title"].startswith("Spring Flood 2027") for item in record["items"])
    assert record["folder"]["title"] == "Spring Flood 2027 Storm Response Kit"

    # nothing left to fill in, and nothing still naming an item of the org the kit came from
    source_ids = set(kit_index()["items"])
    for where, text in online.sent_text():
        assert deploy.unresolved(text) == [], "%s still has %s" % (where, deploy.unresolved(text)[:3])
        named = set(re.findall(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", text.lower())) & source_ids
        assert not named, "%s still names %d kit item id(s)" % (where, len(named))

    # views are made as views, over the layer they belong to, with their hidden fields hidden
    kit = kit_index()
    for item_id, meta in kit["items"].items():
        if meta["type"] != "Feature Service":
            continue
        service = template(item_id)["service"]
        made = online.services[deployer.created[item_id]["url"]]
        assert made["made_as_view"] == bool(service["is_view"])
        for view_layer in service.get("view_layers") or []:
            layer = next(x for x in made["layers"] if x["id"] == view_layer["id"])
            showing = {f["name"] for f in layer["fields"] if f.get("visible", True)}
            assert not showing & set(view_layer.get("hidden_fields") or [])

    # a survey's layer is made before the survey, and still ends up naming it
    form_id = deployer.created[generated.TEAM_FORM]["itemId"]
    for key, text in (("cdf503732eb14812adbcedb6d9937050", "Feature Service for survey %s"),  # Team Update
                      ("a724ca755df04d8b93a702636a4d27d7", "Feature service view of results for the survey %s")):  # its results view
        assert online.get(deployer.created[key]["url"])["serviceDescription"] == text % form_id

    # the Hub site points at the apps of this run and everything is in its group
    site = next(i for i in record["items"] if i["type"] == "Hub Site Application")
    assert site["url"] == "https://spring-flood-2027-response-target.hub.arcgis.com"
    layout = json.dumps(json.loads(online.data[site["itemId"]])["values"]["layout"])
    for key in (generated.TEAM_DASHBOARD, generated.COMMUNITY_DASHBOARD, generated.DAMAGE_DASHBOARD):
        assert deployer.created[key]["itemId"] in layout
    assert len(online.groups) == 1 and record["groups"] == online.groups
    assert set(online.shared) == {i["itemId"] for i in record["items"]}
    assert all(p["groups"] == online.groups[0]["id"] and p["everyone"] == "false" and p["org"] == "false" for p in online.shared.values())

    # each Experience Builder app's draft equals what is published
    for item in record["items"]:
        if item["type"] == "Web Experience":
            assert online.resources[item["itemId"]]["config/config.json"] == online.data[item["itemId"]]

    # and it can be taken out again from its run file
    failed = deploy.remove(online, deployer.run_path(), log=lines.append)
    assert failed == 0 and len([p for p in online.deleted if "/items/" in p]) == 43
    assert ("DELETE", "https://hub.arcgis.com/api/v3/domains/4242") in online.session.sent


@pytest.mark.parametrize("tool", ALL_TOOLS)
def test_each_tool_deploys_on_its_own(online, tool):
    record = deploy.Deployer(online, KIT, "Solo Test", [tool], log=lambda m: None).run()
    assert record["finished"] and record["problems"] == [] and record["items"]


def test_the_org_the_kit_came_from_is_refused(online):
    online.org = kit_index()["source_org"]["id"]
    with pytest.raises(deploy.PortalError, match="built from"):
        deploy.Deployer(online, KIT, "Spring Flood 2027", ALL_TOOLS, log=lambda m: None).run()
    assert online.posts == []

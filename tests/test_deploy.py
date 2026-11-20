"""The deployer, run against a fake portal. No network, no ArcGIS Pro."""

import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deployer"))

import deploy  # noqa: E402
import generated  # noqa: E402
import server  # noqa: E402
from portal import Portal, PortalError, ReadOnlyError  # noqa: E402

SRC, VIEW, MAP, DASH, FORM = ("a" * 32, "b" * 32, "c" * 32, "d" * 32, "e" * 32)
TARGET_ROOT = "https://services9.arcgis.com/TARGETORG/arcgis/rest/services"


# --- pure helpers ------------------------------------------------------------


def test_slug_and_small_helpers():
    assert deploy.slug("Spring flood 2027!") == "SpringFlood2027"
    assert deploy.slug("") == "Kit"
    assert deploy.admin_url(TARGET_ROOT + "/X/FeatureServer") == "https://services9.arcgis.com/TARGETORG/arcgis/rest/admin/services/X/FeatureServer"
    assert deploy.extent_param([[-180, -90], [180, 90]]) == "-180,-90,180,90"
    assert deploy.extent_param([]) is None
    assert deploy.without("Create,Query,Sync,ChangeTracking", "Sync", "ChangeTracking") == "Create,Query"
    assert deploy.app_url("Dashboard", "x", "https://o.maps.arcgis.com") == "https://o.maps.arcgis.com/apps/dashboards/x"
    assert deploy.app_url("Feature Service", "x", "https://o.maps.arcgis.com") is None


def test_resolve_fills_items_and_names_and_escapes_for_json():
    created = {SRC: {"itemId": "new1", "url": TARGET_ROOT + "/A/FeatureServer", "name": "A"}}
    text = json.dumps({"id": "{{%s.itemId}}" % SRC, "url": "{{%s.url}}/0" % SRC, "q": "x?u={{%s.url.enc}}" % SRC, "n": "{{%s.name}}" % SRC, "t": "{{event.name}} status", "later": "{{%s.itemId}}" % VIEW})
    out = json.loads(deploy.resolve(text, created, {"event.name": 'Storm "Bob" \\ 1'}))
    assert out["id"] == "new1" and out["url"] == TARGET_ROOT + "/A/FeatureServer/0" and out["n"] == "A"
    assert out["q"] == "x?u=https%3A%2F%2Fservices9.arcgis.com%2FTARGETORG%2Farcgis%2Frest%2Fservices%2FA%2FFeatureServer"
    assert out["t"] == 'Storm "Bob" \\ 1 status'
    assert out["later"] == "{{%s.itemId}}" % VIEW  # not created yet: left for the next pass
    assert deploy.unresolved(json.dumps(out)) == ["{{%s.itemId}}" % VIEW]


def kit_index():
    return {
        "source_org": {"id": "SOURCEORG", "url": "https://source.maps.arcgis.com"},
        "tools": [
            {"key": "team", "name": "Team", "about": "", "items": [DASH, FORM], "primary": DASH},
            {"key": "other", "name": "Other", "about": "", "items": [MAP], "primary": None},
        ],
        "items": {
            SRC: {"type": "Feature Service", "title": "Updates", "deps": [FORM]},
            VIEW: {"type": "Feature Service", "title": "Updates (results)", "deps": [SRC, FORM], "is_view": True},
            MAP: {"type": "Web Map", "title": "Map", "deps": [VIEW]},
            DASH: {"type": "Dashboard", "title": "Dashboard", "deps": [MAP]},
            FORM: {"type": "Form", "title": "Form", "deps": [VIEW]},
        },
    }


def test_plan_items_pulls_in_dependencies_and_orders_services_first():
    order = deploy.plan_items(kit_index(), ["team"])
    assert set(order) == {SRC, VIEW, MAP, DASH, FORM}
    assert order[0] == SRC and order[1] == VIEW
    assert deploy.plan_items(kit_index(), ["other"])[:2] == [SRC, VIEW]
    with pytest.raises(ValueError):
        deploy.plan_items(kit_index(), ["nope"])


# --- a fake portal -----------------------------------------------------------


class FakePortal:
    url = "https://www.arcgis.com"

    def __init__(self, org_id="TARGETORG"):
        self.org_id_value = org_id
        self.calls = []
        self.items = {}
        self.data = {}
        self.services = {}
        self.n = 0
        self.session = None
        self._referer = None

    def describe(self):
        return {"portal": self.url, "org_url": "https://target.maps.arcgis.com", "org_name": "Target Org", "org_id": self.org_id_value, "username": "ann", "full_name": "Ann", "role": "org_admin", "privileges": []}

    def _new(self):
        self.n += 1
        return "%032x" % self.n

    def item(self, item_id):
        return dict(self.items[item_id])

    def item_data_bytes(self, item_id):
        return self.data[item_id]

    def get(self, path, params=None, timeout=None):
        self.calls.append(("GET", path, params))
        if path.endswith("isServiceNameAvailable"):
            return {"available": True}
        if path.endswith("/query"):
            return {"count": 0}
        for url, service in self.services.items():
            if path == url:
                return {"layers": [x for x in service["layers"] if x.get("type") != "Table"], "tables": [x for x in service["layers"] if x.get("type") == "Table"]}
            if path.startswith(url + "/"):
                layer = next(x for x in service["layers"] if str(x["id"]) == path.rsplit("/", 1)[1])
                return {"fields": [f for f in layer.get("fields", []) if f.get("visible", True)]}
        raise AssertionError("unexpected GET " + path)

    def post(self, path, params=None, files=None, timeout=None):
        params = dict(params or {})
        self.calls.append(("POST", path, params, files))
        if path.endswith("/createFolder"):
            return {"folder": {"id": "FOLDER"}}
        if path.endswith("/createService"):
            create = json.loads(params["createParameters"])
            item_id = self._new()
            url = "%s/%s/FeatureServer" % (TARGET_ROOT, create["name"])
            self.services[url] = {"create": create, "layers": [], "is_view_param": params.get("isView")}
            self.items[item_id] = {"id": item_id, "type": "Feature Service", "title": create["name"], "typeKeywords": ["Feature Service", "Hosted Service"]}
            return {"success": True, "itemId": item_id, "serviceurl": url}
        if "/rest/admin/services/" in path:
            url, verb = path.replace("/rest/admin/services/", "/rest/services/").rsplit("/", 1)
            body = json.loads(params.get("addToDefinition") or params.get("updateDefinition"))
            if verb == "addToDefinition":
                for x in body.get("layers", []) + body.get("tables", []):
                    existing = next((layer for layer in self.services[url]["layers"] if layer["id"] == x["id"]), None)
                    if existing:
                        existing.update(x)
                    else:
                        if "adminLayerInfo" in x and "viewLayerDefinition" in x["adminLayerInfo"]:
                            source_name = x["adminLayerInfo"]["viewLayerDefinition"]["sourceServiceName"]
                            source = self.services["%s/%s/FeatureServer" % (TARGET_ROOT, source_name)]
                            x = dict(x, fields=[dict(f) for f in source["layers"][0]["fields"]])
                        self.services[url]["layers"].append(x)
            elif url in self.services:
                self.services[url].setdefault("updates", []).append(body)
            else:  # a layer-level update
                service_url, layer_id = url.rsplit("/", 1)
                layer = next(x for x in self.services[service_url]["layers"] if str(x["id"]) == layer_id)
                for f in body.get("fields", []):
                    next(x for x in layer["fields"] if x["name"] == f["name"]).update(f)
                layer.update({k: v for k, v in body.items() if k != "fields"})
            return {"success": True}
        if path.endswith("/addItem"):
            item_id = self._new()
            self.items[item_id] = {"id": item_id, "type": params["type"], "title": params["title"], "typeKeywords": (params.get("typeKeywords") or "").split(",")}
            if "text" in params:
                self.data[item_id] = params["text"].encode()
            return {"success": True, "id": item_id}
        if path.endswith("/update"):
            item_id = path.split("/")[-2]
            if "text" in params:
                self.data[item_id] = params["text"].encode()
            if files and "file" in files:
                self.data[item_id] = files["file"][1]
            if "typeKeywords" in params:
                self.items[item_id]["typeKeywords"] = params["typeKeywords"].split(",")
            self.items[item_id].update({k: v for k, v in params.items() if k in ("title", "url")})
            return {"success": True}
        if path.endswith(("/addRelationship", "/share", "/addResources", "/delete")):
            return {"success": True}
        raise AssertionError("unexpected POST " + path)


def write_kit(folder: Path):
    index = kit_index()
    (folder / "items").mkdir(parents=True)
    (folder / "kit.json").write_text(json.dumps(index), encoding="utf-8")

    def put(key, template, data=None, zipped=None):
        d = folder / "items" / key
        d.mkdir()
        (d / "template.json").write_text(json.dumps(dict({"key": key, "type": index["items"][key]["type"], "title": index["items"][key]["title"], "tags": [], "resources": [], "relationships": []}, **template)), encoding="utf-8")
        if data is not None:
            (d / "data.json").write_text(json.dumps(data), encoding="utf-8")
        if zipped is not None:
            (d / "data.zip").write_bytes(zipped)

    fields = [{"name": "objectid"}, {"name": "team"}, {"name": "phone"}]
    put(SRC, {"extra_keywords": ["Survey123", "{{%s.itemId}}" % FORM], "service": {"name": "Updates", "is_view": False, "create": {"capabilities": "Create,Query,Editing,Sync", "spatialReference": {"wkid": 4326}}, "layers": [{"id": 0, "name": "survey", "fields": fields}], "tables": []}})
    put(VIEW, {"extra_keywords": ["StakeholderView"], "service": {"name": "Updates_results", "is_view": True, "source": "{{%s.itemId}}" % SRC, "create": {"capabilities": "Query", "spatialReference": {"wkid": 4326}},
                                                                 "view_layers": [{"id": 0, "name": "survey", "is_table": False, "source_layer_id": 0, "hidden_fields": ["phone"], "definition_query": "team IS NOT NULL", "update": {}}]}})
    put(MAP, {"typeKeywords": ["Web Map"], "data": {"file": "data.json", "json": True}}, data={"operationalLayers": [{"id": "results", "title": "{{event.name}} updates", "itemId": "{{%s.itemId}}" % VIEW, "url": "{{%s.url}}/0" % VIEW}]})
    put(DASH, {"typeKeywords": ["Dashboard"], "data": {"file": "data.json", "json": True}}, data={"header": "{{event.NAME}} ({{org.name}})", "map": "{{%s.itemId}}" % MAP, "layerId": "results", "link": "{{portal.url}}/x"})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("esriinfo/form.info", json.dumps({"serviceInfo": {"itemId": "{{%s.itemId}}" % VIEW, "url": "{{%s.url}}" % VIEW}}))
        z.writestr("esriinfo/form.itemInfo", "{{form.itemInfo}}")
        z.writestr("esriinfo/form.xml", '<xls-{{%s.itemId}} id="survey"/>' % FORM)
    put(FORM, {"typeKeywords": ["Form", "Survey123"], "data": {"file": "data.zip", "name": "form.zip", "form": True}, "relationships": [{"type": "Survey2Service", "to": VIEW}]}, zipped=buf.getvalue())
    return folder


@pytest.fixture()
def kit(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "RUNS", tmp_path / "runs")
    return write_kit(tmp_path / "kit")


def test_deploy_end_to_end(kit):
    portal = FakePortal()
    lines = []
    deployer = deploy.Deployer(portal, kit, "Spring Flood", ["team"], log=lines.append)
    record = deployer.run()
    assert record["finished"] and record["problems"] == [] and len(record["items"]) == 5
    ids = {i["key"]: i["itemId"] for i in record["items"]}

    posts = [c for c in portal.calls if c[0] == "POST"]
    created = [json.loads(c[2]["createParameters"]) for c in posts if c[1].endswith("/createService")]
    assert [c.get("isView", False) for c in created] == [False, True]
    assert created[0]["capabilities"] == "Create,Query,Editing"  # Sync is added once the layers exist
    assert created[0]["name"].startswith("Updates_SpringFlood_") and created[0]["preserveLayerIds"] is True
    view_call = next(c for c in posts if c[1].endswith("/createService") and c[2].get("isView"))
    assert view_call[2]["isView"] == "true"

    view_url = deployer.created[VIEW]["url"]
    view_layer = portal.services[view_url]["layers"][0]
    assert view_layer["adminLayerInfo"]["viewLayerDefinition"] == {"sourceServiceName": deployer.created[SRC]["name"], "sourceLayerId": 0, "sourceLayerFields": "*"}
    assert view_layer["viewDefinitionQuery"] == "team IS NOT NULL"
    assert [f["name"] for f in view_layer["fields"] if f.get("visible", True)] == ["objectid", "team"]
    assert {"capabilities": "Create,Query,Editing,Sync"} in portal.services[deployer.created[SRC]["url"]]["updates"]

    webmap = json.loads(portal.data[ids[MAP]])
    assert webmap["operationalLayers"][0] == {"id": "results", "title": "Spring Flood updates", "itemId": ids[VIEW], "url": view_url + "/0"}
    dashboard = json.loads(portal.data[ids[DASH]])
    assert dashboard == {"header": "SPRING FLOOD (Target Org)", "map": ids[MAP], "layerId": "results", "link": "https://target.maps.arcgis.com/x"}

    z = zipfile.ZipFile(io.BytesIO(portal.data[ids[FORM]]))
    assert json.loads(z.read("esriinfo/form.info")) == {"serviceInfo": {"itemId": ids[VIEW], "url": view_url}}
    assert z.read("esriinfo/form.xml").decode() == '<xls-%s id="survey"/>' % ids[FORM]
    assert json.loads(z.read("esriinfo/form.itemInfo"))["id"] == ids[FORM]

    assert ids[FORM] in portal.items[ids[SRC]]["typeKeywords"] and "Survey123" in portal.items[ids[SRC]]["typeKeywords"]
    assert "Hosted Service" in portal.items[ids[SRC]]["typeKeywords"]  # what the platform set is kept
    assert portal.items[ids[DASH]]["title"] == "Spring Flood - Dashboard"
    relate = next(c for c in posts if c[1].endswith("/addRelationship"))
    assert relate[2] == {"originItemId": ids[FORM], "destinationItemId": ids[VIEW], "relationshipType": "Survey2Service"}
    assert not any(c[1].endswith("/share") for c in posts)  # private unless asked
    run_file = deployer.run_path()
    assert json.loads(run_file.read_text(encoding="utf-8"))["folder"] == {"id": "FOLDER", "title": "Spring Flood Storm Response Kit"}


def test_share_with_the_organization_when_asked(kit):
    portal = FakePortal()
    deploy.Deployer(portal, kit, "Spring Flood", ["team"], share="org", log=lambda m: None).run()
    shares = [c for c in portal.calls if c[0] == "POST" and c[1].endswith("/share")]
    assert len(shares) == 5 and all(c[2]["org"] == "true" and c[2]["everyone"] == "false" for c in shares)


def test_refuses_the_org_the_kit_came_from(kit):
    portal = FakePortal(org_id="SOURCEORG")
    with pytest.raises(PortalError, match="built from"):
        deploy.Deployer(portal, kit, "Spring Flood", ["team"], log=lambda m: None).run()
    assert not [c for c in portal.calls if c[0] == "POST"]


@pytest.mark.parametrize("name", ["", "x", "Storm <script>", "Storm & Flood", "a" * 70, "50% storm"])
def test_refuses_event_names_that_would_not_be_safe_in_titles(kit, name):
    portal = FakePortal()
    with pytest.raises(PortalError):
        deploy.Deployer(portal, kit, name, ["team"], log=lambda m: None).run()
    assert not [c for c in portal.calls if c[0] == "POST"]


def test_a_failed_run_is_still_on_record(kit):
    portal = FakePortal()
    real_post = portal.post

    def failing(path, params=None, files=None, timeout=None):
        if path.endswith("/addItem"):
            raise PortalError("no more credits")
        return real_post(path, params, files, timeout)

    portal.post = failing
    deployer = deploy.Deployer(portal, kit, "Spring Flood", ["team"], log=lambda m: None)
    with pytest.raises(PortalError):
        deployer.run()
    saved = json.loads(deployer.run_path().read_text(encoding="utf-8"))
    assert [i["type"] for i in saved["items"]] == ["Feature Service", "Feature Service"] and not saved["finished"]


def test_remove_only_in_the_org_and_account_that_made_the_run(kit, tmp_path):
    portal = FakePortal()
    deployer = deploy.Deployer(portal, kit, "Spring Flood", ["team"], log=lambda m: None)
    deployer.run()
    other = FakePortal()
    other.describe = lambda: dict(FakePortal.describe(other), username="someone_else")
    with pytest.raises(PortalError, match="signed in"):
        deploy.remove(other, deployer.run_path(), log=lambda m: None)


# --- generated tools ----------------------------------------------------------


def test_requests_layer_definition():
    spec = generated.requests_service()
    layer = spec["layers"][0]
    names = [f["name"] for f in layer["fields"]]
    assert layer["geometryType"] == "esriGeometryPolygon" and layer["hasAttachments"]
    assert {"request_type", "area_name", "priority", "needed_by", "status", "requester_name"} <= set(names)
    codes = {f["name"]: [v["code"] for v in f["domain"]["codedValues"]] for f in layer["fields"] if f.get("domain")}
    assert codes["request_type"] == ["drone_video", "drone_imagery", "home_assessment", "infrastructure_assessment"]
    assert layer["templates"][0]["prototype"]["attributes"] == {"status": "new", "priority": "normal"}
    assert "Shape__Area" not in names


def test_templates_only_use_placeholders_the_deployer_fills():
    known = {"portal.url", "event.name", "requests.itemId", "requests.url", "requests_map.itemId", "requests_dashboard.itemId",
             "imagery_map.itemId", "imagery.before_url", "imagery.before_type", "imagery.before_title", "imagery.after_url", "imagery.after_type",
             "imagery.after_title", "video_lines_view.itemId", "video_lines_view.url", "video_lines.itemId", "video.server_url"}
    for path in generated.TEMPLATES.glob("*.*"):
        text = path.read_text(encoding="utf-8")
        json.loads(text)
        used = {p.strip("{}") for p in deploy.unresolved(text)}
        assert used <= known, "%s uses %s" % (path.name, used - known)


def test_layer_type_from_the_service_description():
    assert generated.layer_type("https://x/arcgis/rest/services/A/MapServer", {"singleFusedMapCache": True}) == "ArcGISTiledMapServiceLayer"
    assert generated.layer_type("https://x/arcgis/rest/services/A/MapServer", {}) == "ArcGISMapServiceLayer"
    assert generated.layer_type("https://x/arcgis/rest/services/A/ImageServer", {}) == "ArcGISImageServiceLayer"
    assert generated.layer_type("https://x/arcgis/rest/services/A/ImageServer", {"singleFusedMapCache": True}) == "ArcGISTiledImageServiceLayer"


def test_hub_subdomain_fits_a_dns_label():
    name = generated.hub_subdomain("An Extremely Long Event Name For A Very Large Coastal Storm 2027", "snocoPlanning")
    assert len(name + "-snocoplanning") <= 63 and name.endswith("-response") and not name.startswith("-")
    assert generated.hub_subdomain("Kit Test", "org", 2) == "kit-test-response-3"


def test_hub_layout_has_a_section_only_for_what_was_deployed():
    layout = generated.hub_layout("Spring Flood", "Target Org", "https://t.maps.arcgis.com", {"team_dashboard": "T" * 32, "team_form": "F" * 32, "requests_app": "R" * 32})
    text = json.dumps(layout)
    cards = [c["component"] for s in layout["sections"] for r in s["rows"] for c in r["cards"]]
    assert [c["settings"]["itemId"] for c in cards if c["name"] == "app-card"] == ["T" * 32]
    assert "Team status" in text and "Request data collection" in text
    assert "Community status" not in text and "Drone video" not in text
    assert "https://survey123.arcgis.com/share/%s?portalUrl=https://t.maps.arcgis.com" % ("F" * 32) in text
    assert layout["header"]["component"]["settings"]["title"] == "Spring Flood Storm Response"


def test_implied_tools():
    assert generated.with_implied(["drone_video"], ["hub_site"]) == ["hub_site", "drone_video_app"]
    assert generated.with_implied(["team_status"], []) == []
    assert "drone_video_app" not in [t["key"] for t in generated.catalog()]


# --- the page's request checks and the read-only portal -------------------------


def test_check_request():
    keys = {"team_status", "imagery_compare", "drone_video"}
    good, problem = server.check_request({"event": " Spring Flood 2027 ", "tools": ["team_status", "imagery_compare"], "share": "org",
                                          "options": {"imagery_after_url": "https://img.example/arcgis/rest/services/Ortho/ImageServer", "video_server_url": "https://v.example/video/rest/services"}}, keys)
    assert problem is None
    assert good == {"event": "Spring Flood 2027", "org_name": "", "tools": ["team_status", "imagery_compare"], "share": "org",
                    "options": {"imagery_after_url": "https://img.example/arcgis/rest/services/Ortho/ImageServer"}}  # the video option needs its tool
    assert server.check_request({"event": "ok name", "tools": []}, keys)[1]
    assert server.check_request({"event": "ok name", "tools": ["nope"]}, keys)[1]
    assert server.check_request({"event": "bad & name", "tools": ["team_status"]}, keys)[1]
    assert server.check_request({"event": "ok name", "tools": ["imagery_compare"], "options": {"imagery_after_url": "http://insecure.example/x"}}, keys)[1]
    assert server.check_request({"event": "ok name", "tools": ["imagery_compare"], "options": {"imagery_after_url": "https://x.example/a?token=abc"}}, keys)[1]
    assert server.check_request({"event": "ok name", "tools": ["team_status"], "share": "everyone"}, keys)[0]["share"] == "private"


class FakeSession:
    def __init__(self):
        self.sent = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.sent.append(("GET", url, params, headers))

        class Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"ok": True}

        return Resp()

    def post(self, *args, **kwargs):
        raise AssertionError("a read-only portal must not POST")


def test_read_only_portal_refuses_post_and_sends_the_referer():
    session = FakeSession()
    portal = Portal("https://source.maps.arcgis.com/", read_only=True, session=session, token_source=lambda: {"token": "T", "referer": "http://pro", "expires": 0})
    assert portal.get("portals/self") == {"ok": True}
    method, url, params, headers = session.sent[0]
    assert url == "https://source.maps.arcgis.com/sharing/rest/portals/self" and params == {"f": "json", "token": "T"} and headers == {"Referer": "http://pro"}
    with pytest.raises(ReadOnlyError):
        portal.post("content/users/x/addItem", {"title": "nope"})

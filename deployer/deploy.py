"""Deploy the selected tools of the kit into the org ArcGIS Pro is signed in to.

Reads only the kit folder beside this one. It never contacts the org the kit
was built from, and it refuses to deploy into that org.

    propy deployer/deploy.py --portal https://www.arcgis.com --whoami
    propy deployer/deploy.py --portal https://www.arcgis.com --event "Spring Flood 2027" --tools team_status,fema_damage --plan
    propy deployer/deploy.py --portal https://www.arcgis.com --event "Spring Flood 2027" --tools team_status,fema_damage
    propy deployer/deploy.py --portal https://www.arcgis.com --remove deployer/runs/<run>.json --yes

Everything a run creates goes into one new folder and is listed in a run file
under deployer/runs/, which --remove reads to take the deployment out again.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import re
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from portal import Portal, PortalError  # noqa: E402

KIT = ROOT / "kit"
RUNS = HERE / "runs"
PLACEHOLDER = re.compile(r"\{\{([0-9a-f]{32})\.(itemId|url\.enc|url|name)\}\}")
ANY_PLACEHOLDER = re.compile(r"\{\{[\w.]+\}\}")
TEXT_SUFFIXES = (".info", ".iteminfo", ".json", ".webform", ".xml", ".txt", ".html", ".js", ".css")
EVENT_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 \-_.,()]{1,58}$")
# service text that can name an item made after the service (a survey's layer names its form)
LATE_TEXT = ("serviceDescription", "description", "copyrightText")


# --- pure helpers (covered by tests/test_deploy.py) --------------------------


def slug(text: str, limit: int = 18) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", (text or "").title())[:limit] or "Kit"


def plan_items(kit: dict, tool_keys: list[str]) -> list[str]:
    """The items the chosen tools need, with everything they depend on.

    Ordered so that a thing comes after what it depends on where that is
    possible; feature services first, views after their sources.
    """
    tools = {t["key"]: t for t in kit["tools"]}
    unknown = [k for k in tool_keys if k not in tools]
    if unknown:
        raise ValueError("Unknown tool(s): %s" % ", ".join(unknown))
    wanted: dict[str, None] = {}

    def add(item_id: str):
        if item_id in wanted:
            return
        if item_id not in kit["items"]:
            raise ValueError("The kit has no item %s" % item_id)
        wanted[item_id] = None
        for dep in kit["items"][item_id].get("deps", []):
            add(dep)

    for key in tool_keys:
        for item_id in tools[key]["items"]:
            add(item_id)

    def rank(item_id: str) -> int:
        item = kit["items"][item_id]
        if item["type"] != "Feature Service":
            return 2
        return 1 if item.get("is_view") else 0

    return sorted(wanted, key=rank)


def resolve(text: str, created: dict, simple: dict) -> str:
    """Fill placeholders. Values are escaped for a JSON string, which also suits XML here."""

    def esc(value: str) -> str:
        return json.dumps(value)[1:-1]

    def item(match):
        key, attr = match.group(1), match.group(2)
        if key not in created:
            return match.group(0)
        value = created[key].get("itemId" if attr == "itemId" else "name" if attr == "name" else "url")
        if value is None:
            return match.group(0)
        return quote(value, safe="") if attr == "url.enc" else esc(value)

    text = PLACEHOLDER.sub(item, text)
    for name, value in simple.items():
        text = text.replace("{{%s}}" % name, esc(value))
    return text


def unresolved(text: str) -> list[str]:
    return sorted(set(ANY_PLACEHOLDER.findall(text or "")))


def admin_url(service_url: str) -> str:
    return re.sub(r"/rest/services/", "/rest/admin/services/", service_url, count=1, flags=re.IGNORECASE)


def extent_param(extent) -> str | None:
    try:
        (xmin, ymin), (xmax, ymax) = extent
        return "%s,%s,%s,%s" % (xmin, ymin, xmax, ymax)
    except (TypeError, ValueError):
        return None


def without(capabilities: str, *drop: str) -> str:
    return ",".join(c for c in (capabilities or "").split(",") if c.strip() and c.strip() not in drop)


def app_url(item_type: str, item_id: str, org_url: str, keywords=()) -> str | None:
    if item_type == "Dashboard":
        return "%s/apps/dashboards/%s" % (org_url, item_id)
    if item_type == "Web Map":
        return "%s/apps/mapviewer/index.html?webmap=%s" % (org_url, item_id)
    if item_type == "Web Experience":
        return "https://experience.arcgis.com/experience/%s" % item_id
    if item_type == "Form":
        return "https://survey123.arcgis.com/share/%s?portalUrl=%s" % (item_id, org_url)
    if item_type == "Notebook":
        return "%s/home/notebook/notebook.html?id=%s" % (org_url, item_id)
    return None


# --- the deployment ----------------------------------------------------------


class Deployer:
    def __init__(self, portal: Portal, kit_dir: Path, event_name: str, tools: list[str], org_name: str | None = None,
                 share: str = "private", options: dict | None = None, log=print):
        self.portal = portal
        self.kit_dir = Path(kit_dir)
        self.kit = json.loads((self.kit_dir / "kit.json").read_text(encoding="utf-8"))
        for item_id, meta in self.kit["items"].items():
            template = self.template(item_id)
            meta["is_view"] = bool((template.get("service") or {}).get("is_view"))
        self.event_name = event_name.strip()
        self.tools = tools
        self.share = share
        self.options = options or {}
        self.log = log
        self.who = portal.describe()
        self.org_name = (org_name or self.who["org_name"] or "").strip()
        self.user = self.who["username"]
        self.org_url = self.who["org_url"]
        self.tag = "%s_%s" % (slug(self.event_name), format(int(time.time()) % 0xFFFF, "04x"))
        self.simple = {
            "portal.url": self.org_url,
            "user.username": self.user,
            "event.name": self.event_name,
            "event.NAME": self.event_name.upper(),
            "org.name": self.org_name,
        }
        self.created: dict[str, dict] = {}
        self.order: list[str] = []
        self.folder_id = None
        self.folder_title = "%s Storm Response Kit" % self.event_name
        self.warnings: list[str] = []
        self.groups: list[dict] = []
        self.notes: dict = {}
        self.started = dt.datetime.now(dt.timezone.utc)

    # -- small things --------------------------------------------------------

    def template(self, item_id: str) -> dict:
        return json.loads((self.kit_dir / "items" / item_id / "template.json").read_text(encoding="utf-8"))

    def say(self, message: str):
        self.log(message)

    def warn(self, message: str):
        self.warnings.append(message)
        self.log("WARNING: " + message)

    def fill(self, value):
        """Resolve placeholders in a string or a JSON-able value."""
        if isinstance(value, str):
            return resolve(value, self.created, self.simple)
        return json.loads(resolve(json.dumps(value), self.created, self.simple))

    def guard(self):
        source = self.kit.get("source_org") or {}
        if source.get("id") and source["id"] == self.who["org_id"]:
            raise PortalError("This is the org the kit was built from (%s). Deploy into a different org." % self.who["org_name"])
        if not EVENT_NAME_OK.match(self.event_name):
            raise PortalError("Give the event a name of 2 to 59 letters, digits, spaces or - _ . , ( ).")
        needed = {"portal:user:createItem", "portal:publisher:publishFeatures"}
        missing = needed - set(self.who["privileges"])
        if missing and self.who["role"] != "org_admin":
            raise PortalError("%s cannot create content or publish hosted layers in %s." % (self.user, self.who["org_name"]))

    def title(self, base: str) -> str:
        return "%s - %s" % (self.event_name, base)

    def remember(self, key: str, **record):
        self.created[key] = record
        if key not in self.order:
            self.order.append(key)
        self.save_run()

    # -- folder --------------------------------------------------------------

    def make_folder(self):
        try:
            body = self.portal.post("content/users/%s/createFolder" % self.user, {"title": self.folder_title})
            self.folder_id = body["folder"]["id"]
            self.say("Created folder \"%s\"" % self.folder_title)
        except PortalError as exc:
            if "not available" not in str(exc) and "already exists" not in str(exc).lower():
                raise
            content = self.portal.get("content/users/%s" % self.user)
            match = [f for f in content.get("folders", []) if f["title"] == self.folder_title]
            if not match:
                raise
            self.folder_id = match[0]["id"]
            self.say("Using the existing folder \"%s\"" % self.folder_title)

    def _user_path(self, tail: str) -> str:
        return "content/users/%s/%s/%s" % (self.user, self.folder_id, tail) if self.folder_id else "content/users/%s/%s" % (self.user, tail)

    # -- feature services ----------------------------------------------------

    def _service_name(self, hint: str) -> str:
        name = re.sub(r"\W+", "_", "%s_%s" % (hint, self.tag))
        for _ in range(5):
            body = self.portal.get("portals/%s/isServiceNameAvailable" % self.who["org_id"], {"name": name, "type": "Feature Service"})
            if body.get("available"):
                return name
            name += format(int(time.time() * 1000) % 0xFF, "02x")
        raise PortalError("Could not find a free service name starting %s" % hint)

    def _create_service(self, key: str, title: str, name: str, params: dict) -> dict:
        fields = {"createParameters": json.dumps(params), "outputType": "featureService"}
        if params.get("isView"):
            fields["isView"] = "true"  # without this the service is made as a plain layer
        body = self.portal.post(self._user_path("createService"), fields)
        if not body.get("success", True) or not body.get("itemId"):
            raise PortalError("createService for %s did not succeed: %s" % (name, json.dumps(body)[:300]))
        # on record before anything else can fail, so a broken run can still be removed
        self.remember(key, itemId=body["itemId"], url=body["serviceurl"], name=name, type="Feature Service", title=title)
        return body

    def _definition(self, url: str, verb: str, definition: dict, layer_id=None):
        path = admin_url(url) + ("/%s" % layer_id if layer_id is not None else "") + "/" + verb
        key = "addToDefinition" if verb == "addToDefinition" else "updateDefinition"
        return self.portal.post(path, {key: json.dumps(definition)}, timeout=600)

    def _plain_service(self, key: str, title: str, name: str, create: dict, layers: list, tables: list) -> str:
        body = self._create_service(key, title, name, create)
        url = body["serviceurl"]
        # relationships go in afterwards: a layer cannot name a related layer
        # that does not exist yet
        related = [{"id": x["id"], "relationships": x.pop("relationships")} for x in layers + tables if x.get("relationships")]
        for x in layers + tables:
            x.pop("relationships", None)
        if layers or tables:
            self._definition(url, "addToDefinition", {"layers": layers, "tables": tables})
        if related:
            self._definition(url, "addToDefinition", {"layers": related})
        return url

    def add_service(self, key: str, title: str, name_hint: str, create: dict, layers: list, tables: list) -> dict:
        """A hosted layer the deployer defines itself (not one from the kit)."""
        name = self._service_name(name_hint)
        create = dict(create, name=name, preserveLayerIds=True)
        url = self._plain_service(key, self.title(title), name, create, layers, tables)
        record = self.created[key]
        self.portal.post(
            "content/users/%s/items/%s/update" % (self.user, record["itemId"]),
            {"title": record["title"], "snippet": create.get("serviceDescription") or "", "tags": "storm response kit"},
        )
        self.say("Created hosted layer \"%s\" (%d layer%s)" % (record["title"], len(layers) + len(tables), "" if len(layers) + len(tables) == 1 else "s"))
        assert record["url"] == url
        return record

    def add_item(self, key: str, item_type: str, title: str, text: str | None = None, file: tuple | None = None, keywords=None,
                 extent: str | None = None, properties: dict | None = None, snippet: str | None = None, tags: str | None = None,
                 prefix: bool = True) -> str:
        """An item the deployer builds itself. Returns its id."""
        params = {"type": item_type, "title": self.title(title) if prefix else title, "tags": tags or "storm response kit"}
        if keywords:
            params["typeKeywords"] = ",".join(keywords)
        if text is not None:
            left = unresolved(text)
            if left:
                raise PortalError("%s still has unfilled placeholders: %s" % (title, ", ".join(left[:6])))
            params["text"] = text
        if extent:
            params["extent"] = extent
        if properties:
            params["properties"] = json.dumps(properties)
        if snippet:
            params["snippet"] = snippet
        files = {"file": file} if file else None
        body = self.portal.post(self._user_path("addItem"), params, files=files)
        self.remember(key, itemId=body["id"], url=None, name=None, type=item_type, title=params["title"])
        if item_type != "Hub Site Application":
            self.say("Created %s \"%s\"" % (item_type.lower(), params["title"]))
        return body["id"]

    def deploy_service(self, key: str):
        template = self.template(key)
        svc = template["service"]
        name = self._service_name(svc["name"])
        create = dict(self.fill(svc["create"]))
        for field in LATE_TEXT:
            if isinstance(create.get(field), str) and unresolved(create[field]):
                create[field] = ""  # names something not made yet; finish_service_item fills it in
        wanted_caps = create.get("capabilities") or "Query"
        create.update({"name": name, "preserveLayerIds": True})
        title = self.title(template["title"])
        if svc["is_view"]:
            source_key = PLACEHOLDER.search(svc["source"]).group(1)
            source = self.created[source_key]
            settings = {k: create[k] for k in ("maxRecordCount", "editorTrackingInfo", "description", "serviceDescription", "copyrightText") if k in create}
            create = {
                "name": name,
                "isView": True,
                "sourceSchemaChangesAllowed": True,
                "isUpdatableView": True,
                "spatialReference": create.get("spatialReference"),
                "initialExtent": create.get("initialExtent"),
                "capabilities": "Query",
                "preserveLayerIds": True,
                "options": {"dataSourceType": "relational"},
            }
            body = self._create_service(key, title, name, create)
            url = body["serviceurl"]
            layers, tables = [], []
            for view_layer in svc["view_layers"]:
                entry = {
                    "id": view_layer["id"],
                    "name": view_layer["name"],
                    "adminLayerInfo": {
                        "viewLayerDefinition": {
                            "sourceServiceName": source["name"],
                            "sourceLayerId": view_layer["source_layer_id"],
                            "sourceLayerFields": "*",
                        }
                    },
                }
                if view_layer.get("is_table"):
                    entry["type"] = "Table"
                (tables if view_layer.get("is_table") else layers).append(entry)
            self._definition(url, "addToDefinition", {"layers": layers, "tables": tables})
            if settings:
                try:
                    self._definition(url, "updateDefinition", settings)
                except PortalError as exc:
                    self.warn("%s: service settings not applied (%s)" % (template["title"], exc))
            for view_layer in svc["view_layers"]:
                update = dict(self.fill(view_layer.get("update") or {}))
                update.pop("capabilities", None)
                if view_layer.get("hidden_fields"):
                    update["fields"] = [{"name": f, "visible": False} for f in view_layer["hidden_fields"]]
                if view_layer.get("definition_query"):
                    update["viewDefinitionQuery"] = view_layer["definition_query"]
                if update:
                    try:
                        self._definition(url, "updateDefinition", update, layer_id=view_layer["id"])
                    except PortalError as exc:
                        self.warn("%s layer %s: could not apply its view settings (%s)" % (template["title"], view_layer["id"], exc))
        else:
            create["capabilities"] = without(wanted_caps, "Sync", "ChangeTracking") or "Query"
            url = self._plain_service(key, title, name, create, self.fill(svc.get("layers") or []), self.fill(svc.get("tables") or []))
        if wanted_caps != create["capabilities"]:
            try:
                self._definition(url, "updateDefinition", {"capabilities": wanted_caps})
            except PortalError as exc:
                self.warn("%s: left with capabilities %s; %s could not be set (%s)" % (template["title"], create["capabilities"], wanted_caps, exc))
        # read back: the layers must be there under the ids the maps expect
        back = self.portal.get(url)
        got = sorted(x["id"] for x in (back.get("layers") or []) + (back.get("tables") or []))
        want = sorted(x["id"] for x in (svc.get("view_layers") or (svc.get("layers") or []) + (svc.get("tables") or [])))
        if got != want:
            raise PortalError("%s was created with layer ids %s, expected %s" % (template["title"], got, want))
        self.say("Created %s \"%s\" (%d layer%s)" % ("view" if svc["is_view"] else "hosted layer", self.title(template["title"]), len(got), "" if len(got) == 1 else "s"))

    def finish_service_item(self, key: str):
        template = self.template(key)
        record = self.created[key]
        current = self.portal.item(record["itemId"])
        keywords = sorted(set(current.get("typeKeywords") or []) | set(self.fill(template.get("extra_keywords") or [])))
        params = {
            "title": record["title"],
            "snippet": self.fill(template.get("snippet") or "") or "",
            "description": self.fill(template.get("description") or "") or "",
            "tags": ",".join(template.get("tags") or []),
            "typeKeywords": ",".join(keywords),
        }
        data = template.get("data")
        if data and data.get("json"):
            text = (self.kit_dir / "items" / key / data["file"]).read_text(encoding="utf-8")
            params["text"] = resolve(text, self.created, self.simple)
        self.portal.post("content/users/%s/items/%s/update" % (self.user, record["itemId"]), params)
        # the other items exist now, so text that names one of them can be filled
        create = (template.get("service") or {}).get("create") or {}
        late = {f: self.fill(create[f]) for f in LATE_TEXT if isinstance(create.get(f), str) and unresolved(create[f])}
        late = {f: text for f, text in late.items() if not unresolved(text)}
        if late:
            try:
                self._definition(record["url"], "updateDefinition", late)
            except PortalError as exc:
                self.warn("%s: its description was left blank (%s)" % (template["title"], exc))

    # -- everything else -----------------------------------------------------

    def create_shell(self, key: str):
        template = self.template(key)
        params = {
            "type": template["type"],
            "title": self.title(template["title"]),
            "tags": ",".join(template.get("tags") or []),
        }
        body = self.portal.post(self._user_path("addItem"), params)
        self.remember(key, itemId=body["id"], url=None, name=None, type=template["type"], title=params["title"])

    def _form_zip(self, key: str, item_info: dict) -> bytes:
        src = zipfile.ZipFile(self.kit_dir / "items" / key / "data.zip")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                body = src.read(info.filename)
                lower = info.filename.lower()
                if lower.endswith(".iteminfo"):
                    body = json.dumps(item_info).encode("utf-8")
                elif lower.endswith(TEXT_SUFFIXES) and not info.is_dir():
                    try:
                        body = resolve(body.decode("utf-8"), self.created, self.simple).encode("utf-8")
                    except UnicodeDecodeError:
                        pass
                dst.writestr(info.filename, body)
        return buf.getvalue()

    def fill_item(self, key: str):
        template = self.template(key)
        record = self.created[key]
        item_id = record["itemId"]
        item_dir = self.kit_dir / "items" / key
        keywords = self.fill(template.get("typeKeywords") or [])
        params = {
            "title": record["title"],
            "snippet": self.fill(template.get("snippet") or "") or "",
            "description": self.fill(template.get("description") or "") or "",
            "typeKeywords": ",".join(keywords),
        }
        for field in ("accessInformation", "licenseInfo", "culture"):
            if template.get(field):
                params[field] = template[field]
        if template.get("properties"):
            params["properties"] = json.dumps(self.fill(template["properties"]))
        extent = extent_param(template.get("extent"))
        if extent:
            params["extent"] = extent
        url = app_url(template["type"], item_id, self.org_url) if template["type"] == "Web Experience" else None
        if url:
            params["url"] = url
            record["url"] = url
        files = None
        data = template.get("data") or {}
        text = None
        if data.get("json"):
            text = resolve((item_dir / data["file"]).read_text(encoding="utf-8"), self.created, self.simple)
            left = unresolved(text)
            if left:
                raise PortalError("%s still has unfilled placeholders: %s" % (template["title"], ", ".join(left[:6])))
            params["text"] = text
        elif data.get("form"):
            info = self.portal.item(item_id)
            files = {"file": ("%s.zip" % item_id, self._form_zip(key, info), "application/zip")}
        elif data.get("file"):
            files = {"file": (data.get("name") or "data", (item_dir / data["file"]).read_bytes())}
        self.portal.post("content/users/%s/items/%s/update" % (self.user, item_id), params, files=files)

        if template.get("thumbnail"):
            thumb = item_dir / template["thumbnail"]
            try:
                self.portal.post(
                    "content/users/%s/items/%s/update" % (self.user, item_id), {}, files={"thumbnail": (thumb.name, thumb.read_bytes())}
                )
            except PortalError as exc:
                self.warn("%s: thumbnail not set (%s)" % (template["title"], exc))

        # resources go up as files: the text parameter of addResources is sanitized
        for rel in template.get("resources") or []:
            body = (item_dir / "resources" / rel).read_bytes()
            if rel.lower().endswith(".json"):
                if template["type"] == "Web Experience" and rel == "config/config.json" and text is not None:
                    body = text.encode("utf-8")  # the draft starts equal to what is published
                else:
                    body = resolve(body.decode("utf-8"), self.created, self.simple).encode("utf-8")
            prefix, _, file_name = rel.rpartition("/")
            fields = {"fileName": file_name}
            if prefix:
                fields["resourcesPrefix"] = prefix
            self.portal.post("content/users/%s/items/%s/addResources" % (self.user, item_id), fields, files={"file": (file_name, body)})
        self.say("Created %s \"%s\"" % (template["type"].lower(), record["title"]))

    def relate(self, key: str):
        template = self.template(key)
        for rel in template.get("relationships") or []:
            if rel["to"] not in self.created:
                continue
            try:
                self.portal.post(
                    "content/users/%s/addRelationship" % self.user,
                    {
                        "originItemId": self.created[key]["itemId"],
                        "destinationItemId": self.created[rel["to"]]["itemId"],
                        "relationshipType": rel["type"],
                    },
                )
            except PortalError as exc:
                self.warn("%s: relationship %s not added (%s)" % (template["title"], rel["type"], exc))

    def share_items(self):
        if self.share != "org":
            return
        for key in self.order:
            record = self.created[key]
            try:
                self.portal.post(
                    "content/users/%s/items/%s/share" % (self.user, record["itemId"]), {"everyone": "false", "org": "true", "groups": ""}
                )
            except PortalError as exc:
                self.warn("%s: not shared with the organization (%s)" % (record["title"], exc))
        self.say("Shared %d items with the organization" % len(self.order))

    # -- checks --------------------------------------------------------------

    def verify(self) -> list[str]:
        """Read back what was made. Returns problems; empty means it checks out."""
        problems = []
        source_org = (self.kit.get("source_org") or {}).get("id")
        source_ids = set(self.kit["items"])
        maps: dict[str, set] = {}

        def leaks(title: str, text: str):
            found = sorted(set(m.lower() for m in re.findall(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])", text)) & source_ids)
            if found:
                problems.append("%s: still names %d item(s) of the org the kit came from" % (title, len(found)))
            if source_org and re.search(r"/%s/arcgis/rest/services/" % re.escape(source_org), text, re.IGNORECASE):
                links = set(re.findall(r"https?://[^\s\"'<>\\]*/%s/arcgis/rest/services/[^/\s\"'<>\\]+" % re.escape(source_org), text, re.IGNORECASE))
                self.say("   %s keeps %d link(s) to public layers of the source org" % (title, len(links)))

        for key in self.order:
            record = self.created[key]
            try:
                item = self.portal.item(record["itemId"])
            except PortalError as exc:
                problems.append("%s: cannot be read back (%s)" % (record["title"], exc))
                continue
            if item["type"] == "Feature Service":
                try:
                    info = self.portal.get(record["url"])
                    if unresolved(json.dumps(info)):
                        problems.append("%s: unfilled placeholders in the service's description" % record["title"])
                    template = self.template(key) if key in self.kit["items"] else {}
                    views = {v["id"]: v for v in (template.get("service") or {}).get("view_layers") or []}
                    for layer in (info.get("layers") or []) + (info.get("tables") or []):
                        self.portal.get("%s/%s/query" % (record["url"], layer["id"]), {"where": "1=1", "returnCountOnly": "true"})
                        hidden = {f.lower() for f in (views.get(layer["id"]) or {}).get("hidden_fields") or []}
                        if hidden:
                            shown = {f["name"].lower() for f in self.portal.get("%s/%s" % (record["url"], layer["id"])).get("fields") or []}
                            if hidden & shown:
                                problems.append("%s layer %s: %d field(s) that should be hidden are showing" % (record["title"], layer["id"], len(hidden & shown)))
                except PortalError as exc:
                    problems.append("%s: service does not answer (%s)" % (record["title"], exc))
                continue
            if item["type"] == "Form":
                try:
                    z = zipfile.ZipFile(io.BytesIO(self.portal.item_data_bytes(record["itemId"])))
                    text = "\n".join(z.read(n).decode("utf-8", "replace") for n in z.namelist() if n.lower().endswith(TEXT_SUFFIXES) and not n.lower().endswith(".iteminfo"))
                except Exception as exc:  # noqa: BLE001
                    problems.append("%s: form package cannot be read back (%s)" % (record["title"], exc))
                    continue
                if unresolved(text):
                    problems.append("%s: unfilled placeholders in the form package" % record["title"])
                leaks(record["title"], text)
                continue
            if item["type"] in ("Web Map", "Dashboard", "Web Experience", "Hub Site Application", "Hub Page", "Web Mapping Application"):
                try:
                    text = self.portal.item_data_bytes(record["itemId"]).decode("utf-8", "replace")
                except PortalError as exc:
                    problems.append("%s: data cannot be read back (%s)" % (record["title"], exc))
                    continue
                left = unresolved(text)
                if left:
                    problems.append("%s: unfilled placeholders %s" % (record["title"], ", ".join(left[:5])))
                leaks(record["title"], text)
                if item["type"] == "Web Map":
                    try:
                        data = json.loads(text)

                        def ids(layers):
                            for layer in layers or []:
                                yield layer.get("id")
                                yield from ids(layer.get("layers"))

                        maps[record["itemId"]] = set(ids(data.get("operationalLayers"))) | set(ids(data.get("tables")))
                    except ValueError:
                        problems.append("%s: data is not JSON" % record["title"])
                if item["type"] == "Dashboard":
                    for match in re.finditer(r'"layerId":\s*"([^"]+)"', text):
                        if maps and not any(match.group(1) in layer_ids for layer_ids in maps.values()):
                            problems.append("%s: reads layer %s, which is in none of the deployed maps" % (record["title"], match.group(1)))
                if item["type"] == "Web Map":
                    for url in sorted(set(re.findall(r'"url":\s*"(https?://[^"]+/(?:FeatureServer|MapServer|ImageServer)(?:/\d+)?)"', text))):
                        try:
                            self.portal.get(url) if "arcgis.com/" in url and self.who["org_id"] in url else self._anonymous(url)
                        except Exception as exc:  # noqa: BLE001 - any failure is a finding
                            problems.append("%s: layer %s does not answer (%s)" % (record["title"], url, str(exc)[:120]))
        return problems

    def _anonymous(self, url: str):
        resp = self.portal.session.get(url, params={"f": "json"}, timeout=60)
        body = resp.json()
        if isinstance(body, dict) and body.get("error"):
            raise PortalError("%s" % body["error"].get("message"))
        return body

    # -- run file ------------------------------------------------------------

    def run_path(self) -> Path:
        RUNS.mkdir(exist_ok=True)
        stamp = self.started.strftime("%Y%m%d-%H%M%S")
        return RUNS / ("%s_%s_%s.json" % (stamp, self.who["org_url"].split("//")[1].split(".")[0], slug(self.event_name)))

    def save_run(self, finished=False, problems=None):
        record = {
            "started": self.started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished": finished,
            "portal": self.portal.url,
            "org_url": self.org_url,
            "org_name": self.who["org_name"],
            "username": self.user,
            "event": self.event_name,
            "tools": self.tools,
            "share": self.share,
            "folder": {"id": self.folder_id, "title": self.folder_title},
            "groups": self.groups,
            "notes": self.notes,
            "items": [dict(self.created[k], key=k,
                           open=self.created[k]["url"] if self.created[k]["type"] == "Hub Site Application" else app_url(self.created[k]["type"], self.created[k]["itemId"], self.org_url),
                           page="%s/home/item.html?id=%s" % (self.org_url, self.created[k]["itemId"])) for k in self.order],
            "warnings": self.warnings,
            "problems": problems or [],
        }
        self.run_path().write_text(json.dumps(record, indent=1), encoding="utf-8")
        return record

    # -- the whole thing -----------------------------------------------------

    def plan(self) -> list[dict]:
        import generated

        keys = [k for k in self.tools if k in {t["key"] for t in self.kit["tools"]}]
        rows = [dict(self.kit["items"][i], key=i) for i in plan_items(self.kit, keys)]
        return rows + generated.plan(self, generated.with_implied(keys, [k for k in self.tools if k not in keys]))

    def run(self) -> dict:
        import generated

        self.guard()
        kit_tools = {t["key"] for t in self.kit["tools"]}
        keys = [k for k in self.tools if k in kit_tools]
        extra = generated.with_implied(keys, [k for k in self.tools if k not in kit_tools])
        generated.check(self, extra)
        items = plan_items(self.kit, keys)
        self.say("Deploying into %s as %s" % (self.who["org_name"], self.user))
        self.make_folder()
        services = [i for i in items if self.kit["items"][i]["type"] == "Feature Service"]
        others = [i for i in items if i not in services]
        for key in services:
            self.deploy_service(key)
        for key in others:
            self.create_shell(key)
        for key in services:
            self.finish_service_item(key)
        for key in others:
            self.fill_item(key)
        for key in others:
            self.relate(key)
        generated.deploy(self, extra)
        self.share_items()
        self.say("Checking what was created")
        problems = self.verify()
        for problem in problems:
            self.say("PROBLEM: " + problem)
        record = self.save_run(finished=True, problems=problems)
        self.say("Done: %d items in folder \"%s\"%s" % (len(self.order), self.folder_title, "" if not problems else ", %d problem(s)" % len(problems)))
        return record


def recheck(portal: Portal, kit_dir: Path, run_files: list[Path], log=print) -> list[str]:
    """Run the read-back checks again over what earlier runs created."""
    first = json.loads(run_files[0].read_text(encoding="utf-8"))
    deployer = Deployer(portal, kit_dir, first["event"], first["tools"], share=first["share"], log=log)
    deployer.save_run = lambda *a, **k: None  # a re-check must not rewrite the run file
    for run_file in run_files:
        run = json.loads(run_file.read_text(encoding="utf-8"))
        if run["org_url"] != deployer.org_url:
            raise PortalError("%s was made in %s; you are signed in to %s." % (run_file.name, run["org_url"], deployer.org_url))
        for item in run["items"]:
            deployer.created[item["key"]] = item
            deployer.order.append(item["key"])
    problems = deployer.verify()
    for problem in problems:
        log("PROBLEM: " + problem)
    log("Checked %d items: %s" % (len(deployer.order), "no problems" if not problems else "%d problem(s)" % len(problems)))
    return problems


def remove(portal: Portal, run_file: Path, log=print) -> int:
    """Delete what a run created (to the recycle bin), newest first."""
    run = json.loads(run_file.read_text(encoding="utf-8"))
    who = portal.describe()
    if who["org_url"] != run["org_url"] or who["username"] != run["username"]:
        raise PortalError("That run was made in %s as %s; you are signed in to %s as %s." % (run["org_url"], run["username"], who["org_url"], who["username"]))
    failed = 0
    for item in reversed(run["items"]):
        try:
            if item.get("hub_domain_id"):
                headers = {"Authorization": portal.token()}
                if portal._referer:
                    headers["Referer"] = portal._referer
                resp = portal.session.delete("https://hub.arcgis.com/api/v3/domains/%s" % item["hub_domain_id"], headers=headers, timeout=60)
                log("Released the Hub site address (HTTP %s)" % resp.status_code)
            portal.post("content/users/%s/items/%s/delete" % (who["username"], item["itemId"]), {})
            log("Deleted %s" % item["title"])
        except PortalError as exc:
            if "does not exist" in str(exc) or exc.code in (400, 404) and "not" in str(exc).lower():
                log("Already gone: %s" % item["title"])
            else:
                failed += 1
                log("Could not delete %s: %s" % (item["title"], exc))
    for group in run.get("groups") or []:
        try:
            portal.post("community/groups/%s/delete" % group["id"], {})
            log("Deleted group \"%s\"" % group["title"])
        except PortalError as exc:
            log("Group \"%s\" not deleted: %s" % (group["title"], exc))
    if not failed and run["folder"].get("id"):
        try:
            left = portal.get("content/users/%s/%s" % (who["username"], run["folder"]["id"])).get("items", [])
            if not left:
                portal.post("content/users/%s/%s/delete" % (who["username"], run["folder"]["id"]), {})
                log("Deleted folder \"%s\"" % run["folder"]["title"])
            else:
                log("Folder \"%s\" still holds %d other item(s); left in place" % (run["folder"]["title"], len(left)))
        except PortalError as exc:
            log("Folder not removed: %s" % exc)
    return failed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--portal", help="the portal to deploy into; Pro must hold a sign-in for it")
    ap.add_argument("--portals", action="store_true", help="list the portals Pro knows, as JSON, and stop")
    ap.add_argument("--job", help="a JSON file with portal, event, org_name, tools, share, options and plan (used by the web page)")
    ap.add_argument("--kit", default=str(KIT))
    ap.add_argument("--whoami", action="store_true", help="print who you are there, as JSON, and stop")
    ap.add_argument("--event", help="the event's name; it prefixes every title")
    ap.add_argument("--org-name", help="what the org is called in titles (default: the org's name)")
    ap.add_argument("--tools", help="comma-separated tool keys")
    ap.add_argument("--share", choices=["private", "org"], default="private")
    ap.add_argument("--option", action="append", default=[], help="name=value, passed to the generated tools")
    ap.add_argument("--plan", action="store_true", help="list what would be created and stop")
    ap.add_argument("--remove", help="a run file: delete what that run created")
    ap.add_argument("--check", nargs="+", help="run file(s): read back what they created and report problems")
    ap.add_argument("--yes", action="store_true", help="confirm --remove")
    args = ap.parse_args(argv)

    def log(message):
        print(message, flush=True)

    plan_as_json = False
    if args.job:
        job = json.loads(Path(args.job).read_text(encoding="utf-8"))
        args.portal = job["portal"]
        args.event = job.get("event")
        args.org_name = job.get("org_name")
        args.tools = ",".join(job.get("tools") or [])
        args.share = job.get("share") or "private"
        args.option = ["%s=%s" % (k, v) for k, v in (job.get("options") or {}).items() if v]
        args.plan = bool(job.get("plan"))
        plan_as_json = True
    try:
        if args.portals:
            from portal import pro_portals

            print("PORTALS " + json.dumps(pro_portals()), flush=True)
            return 0
        if not args.portal:
            ap.error("--portal is needed")
        portal = Portal(args.portal)
        if args.whoami:
            who = portal.describe()
            kit = json.loads((Path(args.kit) / "kit.json").read_text(encoding="utf-8")) if (Path(args.kit) / "kit.json").exists() else {}
            who["is_kit_source"] = bool(kit) and (kit.get("source_org") or {}).get("id") == who["org_id"]
            who["privileges"] = len(who["privileges"])
            print("WHOAMI " + json.dumps(who), flush=True)
            return 0
        if args.check:
            return 1 if recheck(portal, Path(args.kit), [Path(p) for p in args.check], log) else 0
        if args.remove:
            if not args.yes:
                print("Add --yes to delete what %s created." % args.remove)
                return 2
            return 1 if remove(portal, Path(args.remove), log) else 0
        if not args.event or not args.tools:
            ap.error("--event and --tools are needed to deploy")
        options = dict(o.split("=", 1) for o in args.option if "=" in o)
        deployer = Deployer(portal, Path(args.kit), args.event, [t.strip() for t in args.tools.split(",") if t.strip()],
                            org_name=args.org_name, share=args.share, options=options, log=log)
        if args.plan:
            deployer.guard()
            rows = [{"type": row["type"], "title": deployer.title(row["title"])} for row in deployer.plan()]
            if plan_as_json:
                print("PLAN " + json.dumps({"rows": rows, "folder": deployer.folder_title}), flush=True)
            else:
                for row in rows:
                    print("  %-22s %s" % (row["type"], row["title"]))
            return 0
        try:
            record = deployer.run()
        except Exception:
            deployer.save_run(finished=False)
            log("RUNFILE " + str(deployer.run_path()))
            raise
        log("RUNFILE " + str(deployer.run_path()))
        return 1 if record["problems"] else 0
    except (PortalError, ValueError) as exc:
        print("ERROR: %s" % exc, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())

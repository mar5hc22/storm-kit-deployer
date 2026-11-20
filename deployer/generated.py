"""Tools the deployer builds itself instead of copying from the kit.

The source org has nothing to copy for these (a request form), or what it has
cannot be copied (an app on an Enterprise portal, a page on a web host), or a
copy would carry the last event's content (the Hub site).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from portal import PortalError

TEMPLATES = Path(__file__).resolve().parent / "templates"

# kit items the generated tools build on, by the id they have in the kit
VIDEO_LINES = "b2f8f0b331734c5b8c189b41f3a5757e"
VIDEO_LINES_VIEW = "3b6f36b1be4e406e9419c4d34ff4a914"
TEAM_FORM = "1e8c409fb72948feb6ecd51d431c4797"
TEAM_DASHBOARD = "715d7cd6e6ca4ccf8fe381543ab011c0"
COMMUNITY_DASHBOARD = "c515919c2c804b0e89cc43a7f79e1f70"
COMMUNITY_EDITOR = "0808ac356cc743b5826ca8de2d7fb1d4"
DAMAGE_FORM = "29920d92b407461f8ba96a8d4089bad0"
DAMAGE_DASHBOARD = "f8f08acd5fe94493826f522a09bfb178"

WORLD_IMAGERY = "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer"
CLARITY_IMAGERY = "https://clarity.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/MapServer"
EXB_KEYWORDS = [
    "EXB Experience", "JavaScript", "Ready To Use", "Web Application", "Web Experience",
    "Web Mapping Application", "Web Page", "Web Site", "status: Published",
]


def template(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


# --- pure helpers (covered by tests/test_deploy.py) --------------------------


def requests_service() -> dict:
    """The definition of the layer that holds area-based requests."""

    def text(name, alias, length, **more):
        return dict({"name": name, "type": "esriFieldTypeString", "alias": alias, "sqlType": "sqlTypeOther", "length": length, "nullable": True, "editable": True, "domain": None, "defaultValue": None}, **more)

    def coded(name, values):
        return {"type": "codedValue", "name": name, "codedValues": [{"name": label, "code": code} for code, label in values]}

    request_types = [
        ("drone_video", "Drone footage (video)"),
        ("drone_imagery", "Drone imagery (orthomosaic)"),
        ("home_assessment", "Home assessments"),
        ("infrastructure_assessment", "Infrastructure assessment"),
    ]
    priorities = [("low", "Low"), ("normal", "Normal"), ("high", "High"), ("urgent", "Urgent")]
    statuses = [("new", "New"), ("accepted", "Accepted"), ("scheduled", "Scheduled"), ("in_progress", "In progress"), ("complete", "Complete"), ("declined", "Declined")]
    tracking = [
        {"name": "CreationDate", "type": "esriFieldTypeDate", "alias": "CreationDate", "sqlType": "sqlTypeOther", "length": 8, "nullable": True, "editable": False, "domain": None, "defaultValue": None},
        {"name": "Creator", "type": "esriFieldTypeString", "alias": "Creator", "sqlType": "sqlTypeOther", "length": 128, "nullable": True, "editable": False, "domain": None, "defaultValue": None},
        {"name": "EditDate", "type": "esriFieldTypeDate", "alias": "EditDate", "sqlType": "sqlTypeOther", "length": 8, "nullable": True, "editable": False, "domain": None, "defaultValue": None},
        {"name": "Editor", "type": "esriFieldTypeString", "alias": "Editor", "sqlType": "sqlTypeOther", "length": 128, "nullable": True, "editable": False, "domain": None, "defaultValue": None},
    ]
    fields = [
        {"name": "objectid", "type": "esriFieldTypeOID", "alias": "OBJECTID", "sqlType": "sqlTypeOther", "nullable": False, "editable": False, "domain": None, "defaultValue": None},
        {"name": "globalid", "type": "esriFieldTypeGlobalID", "alias": "GlobalID", "sqlType": "sqlTypeOther", "length": 38, "nullable": False, "editable": False, "domain": None, "defaultValue": None},
        text("request_type", "Request", 40, domain=coded("request_type", request_types)),
        text("area_name", "Area name", 120),
        text("details", "Details", 2000),
        text("priority", "Priority", 20, domain=coded("request_priority", priorities), defaultValue="normal"),
        {"name": "needed_by", "type": "esriFieldTypeDate", "alias": "Needed by", "sqlType": "sqlTypeOther", "length": 8, "nullable": True, "editable": True, "domain": None, "defaultValue": None},
        text("status", "Status", 20, domain=coded("request_status", statuses), defaultValue="new"),
        text("requester_name", "Requested by", 120),
        text("requester_org", "Organization", 120),
        text("requester_contact", "Phone or email", 200),
        text("assigned_to", "Assigned to", 120),
        text("response_notes", "Response notes", 2000),
    ] + tracking
    layer = {
        "id": 0,
        "name": "requests",
        "type": "Feature Layer",
        "description": "Area-based requests for drone footage, drone imagery and home or infrastructure assessments.",
        "geometryType": "esriGeometryPolygon",
        "extent": {"xmin": -20037508.34, "ymin": -20037508.34, "xmax": 20037508.34, "ymax": 20037508.34, "spatialReference": {"wkid": 102100, "latestWkid": 3857}},
        "objectIdField": "objectid",
        "globalIdField": "globalid",
        "displayField": "area_name",
        "hasAttachments": True,
        "hasM": False,
        "hasZ": False,
        "allowGeometryUpdates": True,
        "capabilities": "Create,Delete,Query,Update,Editing",
        "maxRecordCount": 2000,
        "supportedQueryFormats": "JSON, geoJSON, PBF",
        "editFieldsInfo": {"creationDateField": "CreationDate", "creatorField": "Creator", "editDateField": "EditDate", "editorField": "Editor"},
        "fields": fields,
        "types": [],
        "templates": [
            {
                "name": "Data collection request",
                "description": "Draw the area you need covered",
                "drawingTool": "esriFeatureEditToolPolygon",
                "prototype": {"attributes": {"status": "new", "priority": "normal"}},
            }
        ],
        "drawingInfo": {
            "renderer": {
                "type": "simple",
                "symbol": {"type": "esriSFS", "style": "esriSFSSolid", "color": [211, 47, 47, 90], "outline": {"type": "esriSLS", "style": "esriSLSSolid", "color": [211, 47, 47, 255], "width": 2}},
            }
        },
        "adminLayerInfo": {"geometryField": {"name": "Shape"}},
    }
    create = {
        "serviceDescription": "Area-based data collection requests",
        "hasStaticData": False,
        "maxRecordCount": 2000,
        "supportedQueryFormats": "JSON",
        "capabilities": "Create,Delete,Query,Update,Editing",
        "spatialReference": {"wkid": 102100, "latestWkid": 3857},
        "initialExtent": layer["extent"],
        "allowGeometryUpdates": True,
        "units": "esriMeters",
        "xssPreventionInfo": {"xssPreventionEnabled": True, "xssPreventionRule": "InputOnly", "xssInputRule": "rejectInvalid"},
        "editorTrackingInfo": {"enableEditorTracking": True, "enableOwnershipAccessControl": False, "allowOthersToQuery": True, "allowOthersToUpdate": True, "allowOthersToDelete": True, "allowAnonymousToQuery": True, "allowAnonymousToUpdate": True, "allowAnonymousToDelete": True},
    }
    return {"create": create, "layers": [layer], "tables": []}


def layer_type(url: str, info: dict) -> str:
    """The web map layerType for an image or map service, from its own description."""
    tiled = bool(info.get("singleFusedMapCache")) or (bool(info.get("tileInfo")) and info.get("cacheType") in (None, "Map", "Raster"))
    if "/imageserver" in url.lower():
        return "ArcGISTiledImageServiceLayer" if tiled else "ArcGISImageServiceLayer"
    return "ArcGISTiledMapServiceLayer" if tiled else "ArcGISMapServiceLayer"


def hub_subdomain(event_name: str, org_key: str, attempt: int = 0) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", event_name.lower()).strip("-") or "storm"
    suffix = "-response" + ("-%d" % (attempt + 1) if attempt else "")
    room = 63 - len(org_key) - 1 - len(suffix)
    return (base[:room].strip("-") + suffix).strip("-")


def section(cards: list[dict], background: str = "#ffffff", color: str = "#323232") -> dict:
    """A Hub layout section. cards is a list of rows, each a list of (width, component, settings)."""
    return {
        "containment": "fixed",
        "isFooter": False,
        "style": {"background": {"isFile": False, "isUrl": True, "state": "", "display": {}, "transparency": 0, "position": {"x": "center", "y": "center"}, "color": background, "image": ""}, "color": color},
        "rows": [{"cards": [{"component": {"name": name, "settings": settings}, "width": width} for width, name, settings in row]} for row in cards],
    }


def markdown(html: str) -> tuple:
    return (12, "markdown-card", {"markdown": html, "schemaVersion": 2.1})


def app_card(item_id: str, height: int = 760) -> tuple:
    return (
        12,
        "app-card",
        {
            "itemId": item_id, "itemUrl": "", "itemTitle": "", "title": "", "page": "", "height": str(height), "scrollable": True,
            "mobileItem": "", "mobileItemId": "", "mobileItemTitle": "", "mobileTitle": "", "mobileHeight": 500, "mobileScrollable": True,
            "shareable": True, "mobileShareable": True, "shareableOnHover": True, "mobileShareableOnHover": True,
        },
    )


def button(label: str, url: str) -> str:
    return '<a class="btn btn-primary" style="margin: 4px 8px 4px 0;" target="_blank" rel="noopener" href="%s">%s</a>' % (url, label)


def hub_layout(event: str, org: str, org_url: str, made: dict, player_url: str | None = None) -> dict:
    """The site's home page, built from whichever tools were deployed.

    made maps a short name (team_form, team_dashboard, ...) to an item id.
    """

    def survey(item_id):
        return "https://survey123.arcgis.com/share/%s?portalUrl=%s" % (item_id, org_url)

    def experience(item_id):
        return "https://experience.arcgis.com/experience/%s" % item_id

    sections = [
        section(
            [[(12, "spacer-card", {"height": "40"})],
             [markdown('<div style="text-align: center;" class="banner-heading"><strong>%s</strong></div><p style="text-align: center; font-size: 22px;">%s storm response workspace</p>' % (event, org))],
             [(12, "spacer-card", {"height": "40"})]],
            background="#005e95", color="#ffffff",
        )
    ]
    names = []
    body = []
    if made.get("team_dashboard"):
        names.append("Team status")
        links = button("Open the team update form", survey(made["team_form"])) if made.get("team_form") else ""
        body.append(section([[markdown('<h2 id="team-status">Team status</h2><p>Field teams file a short update each day: where they are, where they are going, what they need and what is waiting on a decision.</p><p>%s</p>' % links)], [app_card(made["team_dashboard"], 900)]]))
    if made.get("community_dashboard"):
        names.append("Community status")
        links = button("Update a community", experience(made["community_editor"])) if made.get("community_editor") else ""
        body.append(section([[markdown('<h2 id="community-status">Community status</h2><p>One record per impacted community: airport, power, water, who is on the ground, and whether drone footage exists.</p><p>%s</p>' % links)], [app_card(made["community_dashboard"], 800)]], background="#f4f6f8"))
    if made.get("damage_dashboard"):
        names.append("Damage assessment")
        links = button("Open the home damage assessment form", survey(made["damage_form"])) if made.get("damage_form") else ""
        body.append(section([[markdown('<h2 id="damage-assessment">Damage assessment</h2><p>Home damage assessments collected in Survey123, counted and listed for FEMA reporting.</p><p>%s</p>' % links)], [app_card(made["damage_dashboard"], 800)]]))
    if made.get("requests_app"):
        names.append("Request data collection")
        body.append(section([[markdown('<h2 id="requests">Request data collection</h2><p>Draw an area and ask for drone footage, drone imagery, or a home or infrastructure assessment there. The response team tracks each request to completion.</p><p>%s</p>' % button("Make a request", experience(made["requests_app"])))], [app_card(made["requests_dashboard"], 760)] if made.get("requests_dashboard") else []], background="#f4f6f8"))
    if made.get("imagery_app"):
        names.append("Drone imagery comparison")
        body.append(section([[markdown('<h2 id="imagery">Drone imagery comparison</h2><p>Swipe between imagery from before the storm and drone imagery flown after it.</p>')], [app_card(made["imagery_app"], 700)]]))
    if made.get("video_map"):
        names.append("Drone video")
        if player_url:
            video = [(12, "iframe-card", {"src": player_url, "height": "800", "scrollable": True, "shareable": False, "mobileShareable": True, "shareableOnHover": True, "mobileShareableOnHover": True})]
            note = "<p>Pan the map to list the drone flights in view, then pick one to play it with its footprint on the map.</p>"
        else:
            video = []
            note = "<p>The video layers and the notebook that fills them are in place. The player is a web page that needs a web host; once it is hosted, its address goes here.</p>"
        body.append(section([[markdown('<h2 id="drone-video">Drone video</h2>' + note)]] + ([video] if video else []), background="#f4f6f8"))
    intro = "<p style=\"text-align: center; font-size: 18px;\">A workspace for %s staff and partners to reach the tools and data supporting the %s response.</p>" % (org, event)
    if names:
        intro += '<p style="text-align: center;">%s</p>' % " &middot; ".join(names)
    sections.append(section([[markdown(intro)]]))
    sections.extend(body)
    return {
        "sections": sections,
        "header": {"component": {"name": "site-header", "settings": {"fullWidth": False, "iframeHeight": "150px", "iframeUrl": "", "links": [], "logoUrl": "", "title": "%s Storm Response" % event, "markdown": ""}}},
        "footer": {"component": {"name": "site-footer", "settings": {"footerType": "none", "markdown": ""}}},
    }


# --- deployment steps ----------------------------------------------------------


def _experience(d, key: str, title: str, config_text: str):
    config = json.loads(config_text)
    version = config.get("exbVersion", "1.18.0")
    keywords = EXB_KEYWORDS + ["version:%s" % version, "publishVersion:%s" % version]
    item_id = d.add_item(key, "Web Experience", title, text=config_text, keywords=keywords)
    url = "https://experience.arcgis.com/experience/%s" % item_id
    d.portal.post("content/users/%s/items/%s/update" % (d.user, item_id), {"url": url})
    d.created[key]["url"] = url
    # the draft, as a file: the text parameter of addResources is sanitized
    d.portal.post(
        "content/users/%s/items/%s/addResources" % (d.user, item_id),
        {"fileName": "config.json", "resourcesPrefix": "config"},
        files={"file": ("config.json", config_text.encode("utf-8"))},
    )
    return item_id


def plan_requests(d):
    return [
        {"type": "Feature Service", "title": "Data Collection Requests"},
        {"type": "Web Map", "title": "Data Collection Requests Map"},
        {"type": "Dashboard", "title": "Data Collection Requests"},
        {"type": "Web Experience", "title": "Request Data Collection"},
    ]


def deploy_requests(d):
    spec = requests_service()
    record = d.add_service("requests", "Data Collection Requests", "DataCollectionRequests", spec["create"], spec["layers"], spec["tables"])
    d.simple.update({"requests.itemId": record["itemId"], "requests.url": record["url"]})
    map_id = d.add_item("requests_map", "Web Map", "Data Collection Requests Map", text=d.fill(template("requests_webmap.json")),
                        keywords=["ArcGIS Online", "Explorer Web Map", "Map", "Online Map", "Web Map"], extent="-180,15,-60,72")
    d.simple["requests_map.itemId"] = map_id
    dash_id = d.add_item("requests_dashboard", "Dashboard", "Data Collection Requests", text=d.fill(template("requests_dashboard.json")),
                         keywords=["Dashboard", "Operations Dashboard", "ArcGIS Dashboards"])
    d.simple["requests_dashboard.itemId"] = dash_id
    _experience(d, "requests_app", "Request Data Collection", d.fill(template("request_app.json")))


def plan_imagery(d):
    return [{"type": "Web Map", "title": "Imagery Comparison Map"}, {"type": "Web Experience", "title": "Drone Imagery Comparison"}]


def deploy_imagery(d):
    before = (d.options.get("imagery_before_url") or WORLD_IMAGERY).strip().rstrip("/")
    after = (d.options.get("imagery_after_url") or "").strip().rstrip("/")
    after_title = "After: drone imagery"
    if not after:
        after, after_title = CLARITY_IMAGERY, "After: stand-in imagery (swap in your drone imagery)"
        d.say("   No drone imagery service was given; the After layer is a stand-in to replace")
    types = {}
    for name, url in (("before", before), ("after", after)):
        try:
            types[name] = layer_type(url, d._anonymous(url))
        except Exception as exc:  # noqa: BLE001
            if name == "after" and url != CLARITY_IMAGERY:
                raise PortalError("The drone imagery service %s does not answer (%s)" % (url, str(exc)[:160]))
            d.warn("%s imagery service %s could not be read (%s); assuming a tiled map service" % (name.title(), url, str(exc)[:120]))
            types[name] = "ArcGISTiledMapServiceLayer"
    d.simple.update({
        "imagery.before_url": before, "imagery.before_type": types["before"], "imagery.before_title": "Before: World Imagery" if before == WORLD_IMAGERY else "Before",
        "imagery.after_url": after, "imagery.after_type": types["after"], "imagery.after_title": after_title,
    })
    map_id = d.add_item("imagery_map", "Web Map", "Imagery Comparison Map", text=d.fill(template("imagery_webmap.json")),
                        keywords=["ArcGIS Online", "Explorer Web Map", "Map", "Online Map", "Web Map"])
    d.simple["imagery_map.itemId"] = map_id
    _experience(d, "imagery_app", "Drone Imagery Comparison", d.fill(template("imagery_app.json")))


def plan_video(d):
    return [{"type": "Web Map", "title": "Drone Video Map"}, {"type": "Notebook", "title": "Update Video Lines"}]


def deploy_video(d):
    if VIDEO_LINES_VIEW not in d.created:
        return
    d.simple.update({
        "video_lines_view.itemId": d.created[VIDEO_LINES_VIEW]["itemId"],
        "video_lines_view.url": d.created[VIDEO_LINES_VIEW]["url"],
        "video_lines.itemId": d.created[VIDEO_LINES]["itemId"],
        "video.server_url": (d.options.get("video_server_url") or "").strip().rstrip("/"),
    })
    map_id = d.add_item("video_map", "Web Map", "Drone Video Map", text=d.fill(template("video_webmap.json")),
                        keywords=["ArcGIS Online", "Explorer Web Map", "Map", "Online Map", "Web Map"])
    notebook = d.fill(template("video_lines_notebook.ipynb"))
    d.add_item("video_notebook", "Notebook", "Update Video Lines", file=("update_video_lines.ipynb", notebook.encode("utf-8")),
               keywords=["Notebook", "Python"],
               properties={"notebookRuntimeName": "ArcGIS Notebook Python 3 Standard", "notebookRuntimeVersion": "12.0"},
               snippet="Fills the Video Lines layer with one line per video service on a Video Server.")
    # The player reads these from a config.json hosted beside it (video-player/).
    d.notes["video_player_config"] = {
        "portalUrl": d.org_url,
        "appId": "",
        "webmapId": map_id,
        "videoLinesLayerUrl": d.created[VIDEO_LINES_VIEW]["url"] + "/0",
        "enterprisePortalUrl": "",
        "tokenBrokerUrl": "",
    }
    d.say("   The video player's config.json for this deployment is in the run file")
    if not d.simple["video.server_url"]:
        d.say("   No Video Server address was given; set VIDEO_SERVER_URL in the notebook before running it")


def plan_hub(d):
    return [{"type": "Group", "title": "Storm Response Kit"}, {"type": "Hub Site Application", "title": "Storm Response"}]


def deploy_hub(d):
    org = d.portal.self_info()
    org_key = org["urlKey"]
    headers = {"Authorization": d.portal.token(), "Content-Type": "application/json"}
    if d.portal._referer:
        headers["Referer"] = d.portal._referer
    hostname = None
    for attempt in range(6):
        candidate = "%s-%s.hub.arcgis.com" % (hub_subdomain(d.event_name, org_key, attempt), org_key.lower())
        resp = d.portal.session.get("https://hub.arcgis.com/api/v3/domains/%s" % candidate, headers=headers, timeout=60)
        if resp.status_code == 404:
            hostname = candidate
            break
    if not hostname:
        raise PortalError("Could not find a free Hub site address for %s" % d.event_name)
    subdomain = hostname[: -len("-%s.hub.arcgis.com" % org_key.lower())]

    group = d.portal.post("community/createGroup", {
        "title": d.title("Storm Response Kit"),
        "description": "Everything the %s storm response kit deployed. The Hub site lists what is shared here." % d.event_name,
        "snippet": "Content of the %s storm response kit" % d.event_name,
        "tags": "Hub Group,Hub Content Group,Hub Site Group,storm response kit",
        "access": "private",
        "isInvitationOnly": "true",
    })["group"]
    d.groups.append({"id": group["id"], "title": group["title"]})
    d.save_run()

    title = "%s Storm Response" % d.event_name
    site_id = d.add_item(
        "hub_site", "Hub Site Application", title, prefix=False,
        keywords=["Hub", "hubSite", "hubSolution", "JavaScript", "Map", "Mapping Site", "Online Map", "OpenData", "Ready To Use", "selfConfigured", "Web Map", "Registered App"],
        tags="Hub Site,storm response kit",
        properties={"createdFrom": "basicDefaultSite Solution Template (embedded)", "schemaVersion": 1.8, "contentGroupId": group["id"], "children": []},
        snippet="Landing page for the %s storm response tools" % d.event_name,
    )
    body = {"hostname": hostname, "siteId": site_id, "siteTitle": title, "orgId": org["id"], "orgKey": org_key, "orgTitle": org["name"], "sslOnly": True}
    resp = d.portal.session.post("https://hub.arcgis.com/api/v3/domains", data=json.dumps(body), headers=headers, timeout=120)
    if resp.status_code != 200:
        raise PortalError("Hub would not register %s (HTTP %s): %s" % (hostname, resp.status_code, resp.text[:300]))
    domain = resp.json()
    d.created["hub_site"]["hub_domain_id"] = domain.get("id")
    d.save_run()

    ids = {k: v["itemId"] for k, v in d.created.items()}
    made = {
        "team_form": ids.get(TEAM_FORM), "team_dashboard": ids.get(TEAM_DASHBOARD),
        "community_dashboard": ids.get(COMMUNITY_DASHBOARD), "community_editor": ids.get(COMMUNITY_EDITOR),
        "damage_form": ids.get(DAMAGE_FORM), "damage_dashboard": ids.get(DAMAGE_DASHBOARD),
        "requests_app": ids.get("requests_app"), "requests_dashboard": ids.get("requests_dashboard"),
        "imagery_app": ids.get("imagery_app"), "video_map": ids.get("video_map"),
    }
    player_url = (d.options.get("video_player_url") or "").strip() or None
    layout = hub_layout(d.event_name, d.org_name, d.org_url, made, player_url)
    data = {
        "catalog": {"groups": [group["id"]]},
        "feeds": {},
        "values": {
            "title": title,
            "defaultHostname": hostname,
            "subdomain": subdomain,
            "public": False,
            "faviconUrl": "",
            "uiVersion": "2.4",
            "siteId": str(domain.get("id") or ""),
            "clientId": domain.get("clientKey") or "",
            "updatedBy": d.user,
            "map": {"basemaps": {"primary": org.get("defaultBasemap") or {}}},
            "defaultExtent": org.get("defaultExtent") or {},
            "pages": [],
            "theme": {
                "header": {"background": "#005e95", "text": "#ffffff"},
                "body": {"background": "#ffffff", "text": "#323232", "link": "#005e95"},
                "button": {"background": "#005e95", "text": "#ffffff"},
                "logo": {"small": ""},
                "fonts": {"base": {"url": "", "family": "Avenir Next"}, "heading": {"url": "", "family": "Avenir Next"}},
                "globalNav": {"background": "#ffffff", "text": "#000000"},
            },
            "headerCss": "",
            "footerCss": "",
            "capabilities": ["api_explorer", "pages", "my_data", "social_logins", "json_chart_card", "document_iframes", "items_view", "app_page", "underlinedLinks", "globalNav", "socialSharing"],
            "telemetry": {"consentNotice": {"isTheme": True, "consentText": "", "policyURL": ""}, "customAnalytics": {"ga": {"customerTracker": {"enabled": False}}}},
            "contentViews": {"sidePanelOpen": {"app": True, "map": True, "dataset": True, "document": True, "feedback": True}},
            "layout": layout,
        },
    }
    url = "https://" + hostname
    d.portal.post("content/users/%s/items/%s/update" % (d.user, site_id), {"text": json.dumps(data), "url": url})
    d.created["hub_site"]["url"] = url
    d.save_run()
    # everything the run made goes in the site's group, so the site can show it
    shared = 0
    for key in d.order:
        try:
            d.portal.post("content/users/%s/items/%s/share" % (d.user, d.created[key]["itemId"]), {"everyone": "false", "org": "true" if d.share == "org" else "false", "groups": group["id"]})
            shared += 1
        except PortalError as exc:
            d.warn("%s: not shared to the site's group (%s)" % (d.created[key]["title"], exc))
    d.say("Created Hub site \"%s\" at %s and shared %d items to its group" % (title, url, shared))


TOOLS: dict[str, dict] = {
    "request_intake": {
        "name": "Area-based requests",
        "about": "A form where someone draws an area and asks for drone footage, drone imagery, or a home or infrastructure assessment, plus a tracker for the response team. New: the source org has nothing like it.",
        "plan": plan_requests, "deploy": deploy_requests, "order": 10,
    },
    "imagery_compare": {
        "name": "Drone Imagery Comparison",
        "about": "A swipe app over a before and an after imagery layer. Built new, because the source app runs on an Enterprise portal. Give it your drone imagery service, or swap the stand-in layer later.",
        "plan": plan_imagery, "deploy": deploy_imagery, "order": 20,
    },
    "drone_video_app": {
        "name": "Drone Video map and notebook",
        "about": "Comes with Drone Video.", "hidden": True,
        "plan": plan_video, "deploy": deploy_video, "order": 30,
    },
    "hub_site": {
        "name": "Hub site",
        "about": "A landing page with a section for each tool deployed in this run. Built from the selection instead of copied, so it carries none of the last event's links.",
        "plan": plan_hub, "deploy": deploy_hub, "order": 90,
    },
}
IMPLIED = {"drone_video": "drone_video_app"}


def catalog() -> list[dict]:
    return [dict(key=key, name=tool["name"], about=tool["about"], generated=True) for key, tool in TOOLS.items() if not tool.get("hidden")]


def with_implied(kit_keys: list[str], extra: list[str]) -> list[str]:
    out = list(extra)
    for key in kit_keys:
        implied = IMPLIED.get(key)
        if implied and implied not in out:
            out.append(implied)
    return out


def check(deployer, keys: list[str]):
    unknown = [k for k in keys if k not in TOOLS]
    if unknown:
        raise ValueError("Unknown tool(s): %s" % ", ".join(unknown))


def plan(deployer, keys: list[str]) -> list[dict]:
    check(deployer, keys)
    rows = []
    for key in sorted(keys, key=lambda k: TOOLS[k].get("order", 50)):
        rows.extend(TOOLS[key]["plan"](deployer))
    return rows


def deploy(deployer, keys: list[str]):
    check(deployer, keys)
    for key in sorted(keys, key=lambda k: TOOLS[k].get("order", 50)):
        TOOLS[key]["deploy"](deployer)

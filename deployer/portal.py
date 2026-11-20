"""Sign in through ArcGIS Pro and call the ArcGIS sharing API.

A stand-alone Python process can sign in to any portal ArcGIS Pro holds a saved
sign-in for. arcpy.SignInToPortal(url) is session-only there: it does not move
the active portal of a running Pro. So one process can read the source org and
another can write the target org while Pro stays on whatever the user left it.

No password or token is ever written to disk or printed.
"""

from __future__ import annotations

import json
import time

TRANSIENT = {500, 502, 503, 504}


class PortalError(RuntimeError):
    def __init__(self, message, code=None, url=None, details=None):
        super().__init__(message)
        self.code = code
        self.url = url
        self.details = details or []


class ReadOnlyError(PortalError):
    pass


def normalize_portal_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url.lower().startswith("http"):
        url = "https://" + url
    return url


def pro_portals() -> dict:
    """The portals Pro knows, the active one, and which hold a sign-in."""
    import arcpy

    out = {"active": normalize_portal_url(arcpy.GetActivePortalURL()), "portals": []}
    for url in arcpy.ListPortalURLs():
        try:
            info = arcpy.GetPortalInfo(url) or {}
        except Exception:  # a portal that is offline still belongs in the list
            info = {}
        out["portals"].append(
            {
                "url": normalize_portal_url(url),
                "organization": info.get("organization") or "",
                "role": info.get("role") or "",
                "signed_in": bool(info.get("role")),
            }
        )
    return out


class Portal:
    """A signed-in portal. With read_only=True every POST is refused."""

    def __init__(self, url: str | None = None, read_only: bool = False, session=None, token_source=None):
        self.read_only = read_only
        self._token = None
        self._expires = 0.0
        self._referer = None
        if session is None:
            import requests

            session = requests.Session()
        self.session = session
        if token_source is None:
            import arcpy

            if url:
                first = arcpy.SignInToPortal(normalize_portal_url(url) + "/")
            else:
                first = arcpy.GetSigninToken()
            if not first:
                raise PortalError("ArcGIS Pro has no sign-in for %s. Open Pro and sign in to it first." % (url or "the active portal"))
            self.url = normalize_portal_url(arcpy.GetActivePortalURL())
            self._adopt(first)
            token_source = arcpy.GetSigninToken
        else:
            self.url = normalize_portal_url(url)
        self._token_source = token_source
        self.rest = self.url + "/sharing/rest"
        self._self = None

    # -- sign-in -------------------------------------------------------------

    def _adopt(self, tok: dict):
        self._token = tok["token"]
        self._referer = tok.get("referer")
        self._expires = float(tok.get("expires") or 0)

    def token(self) -> str:
        if self._token is None or (self._expires and self._expires - time.time() < 300):
            tok = self._token_source()
            if not tok:
                raise PortalError("The ArcGIS Pro sign-in for %s has ended." % self.url)
            self._adopt(tok)
        return self._token

    def _headers(self) -> dict:
        return {"Referer": self._referer} if self._referer else {}

    # -- requests ------------------------------------------------------------

    def _url(self, path: str) -> str:
        return path if path.lower().startswith("http") else self.rest + "/" + path.lstrip("/")

    def _send(self, method: str, path: str, params=None, files=None, raw=False, timeout=120):
        url = self._url(path)
        last = None
        for attempt, delay in enumerate((0.5, 1.5, None)):
            fields = dict(params or {})
            fields.setdefault("f", "json")
            fields["token"] = self.token()
            try:
                if method == "GET":
                    resp = self.session.get(url, params=fields, headers=self._headers(), timeout=timeout)
                else:
                    resp = self.session.post(url, data=fields, files=files, headers=self._headers(), timeout=timeout)
            except Exception as exc:  # network reset; a timeout is not retried
                if "timed out" in str(exc).lower() or delay is None:
                    raise PortalError("%s %s failed: %s" % (method, url, exc), url=url)
                last = exc
                time.sleep(delay)
                continue
            if resp.status_code in TRANSIENT and delay is not None and method == "GET":
                time.sleep(delay)
                continue
            if raw:
                if resp.status_code >= 400:
                    raise PortalError("%s %s returned HTTP %s" % (method, url, resp.status_code), code=resp.status_code, url=url)
                return resp
            try:
                body = resp.json()
            except ValueError:
                if delay is not None and method == "GET":
                    time.sleep(delay)
                    continue
                raise PortalError("%s %s did not return JSON (HTTP %s)" % (method, url, resp.status_code), code=resp.status_code, url=url)
            if isinstance(body, dict) and body.get("error"):
                err = body["error"]
                raise PortalError(
                    "%s %s: %s %s" % (method, url, err.get("message"), "; ".join(map(str, err.get("details") or []))),
                    code=err.get("code"),
                    url=url,
                    details=err.get("details"),
                )
            return body
        raise PortalError("%s %s kept failing: %s" % (method, url, last), url=url)

    def get(self, path: str, params=None, timeout=120):
        return self._send("GET", path, params, timeout=timeout)

    def get_raw(self, path: str, params=None, timeout=300):
        """The response itself, for item data and files. f=json is not added."""
        url = self._url(path)
        fields = dict(params or {})
        fields["token"] = self.token()
        for delay in (0.5, 1.5, None):
            resp = self.session.get(url, params=fields, headers=self._headers(), timeout=timeout)
            if resp.status_code in TRANSIENT and delay is not None:
                time.sleep(delay)
                continue
            if resp.status_code >= 400:
                raise PortalError("GET %s returned HTTP %s" % (url, resp.status_code), code=resp.status_code, url=url)
            return resp
        raise PortalError("GET %s kept failing" % url, url=url)

    def post(self, path: str, params=None, files=None, timeout=300):
        if self.read_only:
            raise ReadOnlyError("Refusing to POST to %s: this portal is opened read-only." % self._url(path), url=self._url(path))
        return self._send("POST", path, params, files=files, timeout=timeout)

    # -- conveniences --------------------------------------------------------

    def self_info(self) -> dict:
        if self._self is None:
            self._self = self.get("portals/self")
        return self._self

    @property
    def username(self) -> str:
        return (self.self_info().get("user") or {}).get("username")

    @property
    def org_id(self) -> str:
        return self.self_info().get("id")

    @property
    def org_url(self) -> str:
        info = self.self_info()
        if info.get("urlKey") and info.get("customBaseUrl"):
            return "https://%s.%s" % (info["urlKey"], info["customBaseUrl"])
        return self.url

    def describe(self) -> dict:
        info = self.self_info()
        user = info.get("user") or {}
        return {
            "portal": self.url,
            "org_url": self.org_url,
            "org_name": info.get("name"),
            "org_id": info.get("id"),
            "username": user.get("username"),
            "full_name": user.get("fullName"),
            "role": user.get("role"),
            "privileges": sorted(user.get("privileges") or []),
        }

    def item(self, item_id: str) -> dict:
        return self.get("content/items/%s" % item_id)

    def item_data_bytes(self, item_id: str) -> bytes:
        return self.get_raw("content/items/%s/data" % item_id).content

    def item_data_json(self, item_id: str):
        raw = self.item_data_bytes(item_id)
        if not raw.strip():
            return None
        body = json.loads(raw.decode("utf-8"))
        if isinstance(body, dict) and body.get("error"):
            err = body["error"]
            raise PortalError("data of %s: %s" % (item_id, err.get("message")), code=err.get("code"))
        return body

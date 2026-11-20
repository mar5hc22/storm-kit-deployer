"""The deployer page: sign in, tick the tools you want, deploy them into your org.

A local web page over deploy.py. Nothing can be listed or deployed until you
have signed in. Signing in uses the sign-in ArcGIS Pro already holds for the
portal you pick: the page never sees a password or a token, and the org the
kit was built from cannot be chosen as the target.

    deploy.cmd
    python deployer/server.py --port 47620

The page answers this machine's browser only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import generated  # noqa: E402

KIT = ROOT / "kit"
RUNS = HERE / "runs"
DEFAULT_PORT = 47620
EVENT_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 \-_.,()]{1,58}$")
URL_OK = re.compile(r"^https://[A-Za-z0-9.\-]+(:\d+)?(/[A-Za-z0-9._~\-/%()]*)?$")
# the order the tools were asked for in
TOOL_ORDER = ["hub_site", "imagery_compare", "drone_video", "team_status", "fema_damage", "community_status", "request_intake"]
OPTIONS = {
    "imagery_after_url": {"tool": "imagery_compare", "label": "Your drone imagery service (optional)", "hint": "An image service or tiled map service URL. Left blank, a stand-in layer is used that you swap later."},
    "video_server_url": {"tool": "drone_video", "label": "Your Video Server services directory (optional)", "hint": "For example https://host/video/rest/services. It is written into the notebook that fills the video lines."},
    "video_player_url": {"tool": "drone_video", "label": "Address of your hosted video player page (optional)", "hint": "Left blank, the Hub site says the player still needs a web host."},
}


def note(message: str):
    """One line in the console and in deployer/runs/server.log. Never a token or a cookie."""
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    print(line, flush=True)
    try:
        RUNS.mkdir(exist_ok=True)
        with open(RUNS / "server.log", "a", encoding="utf-8") as log:
            log.write(line + "\n")
    except OSError:
        pass


def find_propy() -> str | None:
    for base in (os.environ.get("ProgramFiles"), os.environ.get("LocalAppData") and os.path.join(os.environ["LocalAppData"], "Programs")):
        if base:
            path = Path(base) / "ArcGIS" / "Pro" / "bin" / "Python" / "Scripts" / "propy.bat"
            if path.exists():
                return str(path)
    return None


def kit_info() -> dict:
    """What the page shows about the kit: its tools, and what it leaves out."""
    path = KIT / "kit.json"
    if not path.exists():
        return {"missing": True, "tools": []}
    kit = json.loads(path.read_text(encoding="utf-8"))
    tools = {}
    for tool in kit["tools"]:
        tools[tool["key"]] = {"key": tool["key"], "name": tool["name"], "about": tool["about"], "count": len(tool["items"]), "generated": False}
    for tool in generated.catalog():
        tools[tool["key"]] = {"key": tool["key"], "name": tool["name"], "about": tool["about"], "count": len(generated.TOOLS[tool["key"]]["plan"](None)), "generated": True}
    ordered = [tools[k] for k in TOOL_ORDER if k in tools] + [t for k, t in tools.items() if k not in TOOL_ORDER]
    return {
        "name": kit.get("kit_name"),
        "built": kit.get("built"),
        "tools": ordered,
        "left_out": kit.get("left_out") or [],
        "linked": kit.get("linked") or [],
        "options": OPTIONS,
    }


def check_request(body: dict, tool_keys: set[str]) -> tuple[dict | None, str | None]:
    """Validate what the page sent. Returns the job, or a message for the person."""
    event = str(body.get("event") or "").strip()
    if not EVENT_NAME_OK.match(event):
        return None, "Give the event a name of 2 to 59 letters, digits, spaces or - _ . , ( )."
    tools = [t for t in body.get("tools") or [] if isinstance(t, str)]
    if not tools:
        return None, "Tick at least one tool."
    unknown = [t for t in tools if t not in tool_keys]
    if unknown:
        return None, "Unknown tool: %s" % ", ".join(unknown)
    org_name = str(body.get("org_name") or "").strip()
    if org_name and not re.match(r"^[A-Za-z0-9][A-Za-z0-9 \-_.,()]{0,58}$", org_name):
        return None, "Use letters, digits, spaces or - _ . , ( ) in the organization name."
    share = body.get("share") if body.get("share") in ("private", "org") else "private"
    options = {}
    for name, value in (body.get("options") or {}).items():
        value = str(value or "").strip()
        if name not in OPTIONS or not value or OPTIONS[name]["tool"] not in tools:
            continue
        if not URL_OK.match(value):
            return None, "%s must be an https address with no query string." % OPTIONS[name]["label"].split(" (")[0]
        options[name] = value
    return {"event": event, "org_name": org_name, "tools": tools, "share": share, "options": options}, None


class App:
    def __init__(self, propy: str):
        self.propy = propy
        self.sessions: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.portals_cache: tuple[float, dict] | None = None
        self.lock = threading.Lock()
        self.asking = threading.Lock()  # one question to ArcGIS Pro at a time
        self.work = Path(tempfile.mkdtemp(prefix="stormkit-"))
        try:
            source = (json.loads((KIT / "kit.json").read_text(encoding="utf-8")).get("source_org") or {}).get("url") or ""
        except (OSError, ValueError):
            source = ""
        self.source_url = source.lower().rstrip("/")

    # -- helper processes ----------------------------------------------------

    def _command(self, *args: str) -> list[str]:
        return [self.propy, "-u", str(HERE / "deploy.py"), *args]

    def helper(self, *args: str, timeout: int = 180) -> tuple[int, list[str]]:
        proc = subprocess.run(self._command(*args), capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        return proc.returncode, [line for line in proc.stdout.splitlines() if line.strip()]

    def portals(self, refresh: bool = False) -> dict:
        """The orgs ArcGIS Pro knows. Asked once and kept; refresh asks again."""
        with self.asking:
            with self.lock:
                if self.portals_cache and not refresh and time.time() - self.portals_cache[0] < 900:
                    return self.portals_cache[1]
            started = time.time()
            try:
                code, lines = self.helper("--portals")
            except subprocess.TimeoutExpired:
                note("portals: ArcGIS Pro's Python did not answer within 3 minutes")
                raise RuntimeError("ArcGIS Pro's Python did not answer within 3 minutes. Is ArcGIS Pro open?")
            found = next((json.loads(line[8:]) for line in lines if line.startswith("PORTALS ")), None)
            if found is None:
                note("portals: no answer from ArcGIS Pro's Python (exit %s): %s" % (code, " | ".join(lines[-3:])[:300]))
                raise RuntimeError("ArcGIS Pro's Python did not answer. Is ArcGIS Pro installed and signed in?")
            for portal in found["portals"]:
                portal["is_kit_source"] = bool(self.source_url) and portal["url"].lower().rstrip("/") == self.source_url
            note("portals: %d known, signed in to %s (%.0f s)" % (
                len(found["portals"]), ", ".join(p["organization"] or p["url"] for p in found["portals"] if p["signed_in"]) or "none", time.time() - started))
            with self.lock:
                self.portals_cache = (time.time(), found)
            return found

    def sign_in(self, portal_url: str) -> dict:
        portal_url = portal_url.strip().rstrip("/")
        known = {p["url"].lower().rstrip("/"): p for p in self.portals()["portals"]}
        if portal_url.lower() not in known:
            note("sign-in refused: %r is not a portal ArcGIS Pro knows" % portal_url[:120])
            raise ValueError("ArcGIS Pro has no portal %s." % portal_url)
        started = time.time()
        try:
            with self.asking:
                code, lines = self.helper("--portal", portal_url, "--whoami", "--kit", str(KIT))
        except subprocess.TimeoutExpired:
            note("sign-in to %s: no answer within 3 minutes" % portal_url)
            raise ValueError("ArcGIS Pro did not answer within 3 minutes. Check that ArcGIS Pro is open and not waiting on a sign-in window, then try again.")
        who = next((json.loads(line[7:]) for line in lines if line.startswith("WHOAMI ")), None)
        if who is None:
            error = next((line[7:] for line in lines if line.startswith("ERROR: ")), None)
            note("sign-in to %s failed (exit %s, %.0f s): %s" % (portal_url, code, time.time() - started, error or " | ".join(lines[-3:])[:300] or "no output"))
            raise ValueError(error or "ArcGIS Pro has no working sign-in for %s. Open ArcGIS Pro, sign in to it there, and try again." % portal_url)
        if who.get("is_kit_source"):
            note("sign-in to %s refused: it is the org the kit was built from" % portal_url)
            raise ValueError("%s is the org this kit was built from, so it cannot be the place the tools are deployed to. Choose the org you want the tools in." % who["org_name"])
        token = secrets.token_urlsafe(32)
        self.sessions[token] = {"portal": portal_url, "who": who, "since": time.time()}
        note("signed in to %s as %s (%.0f s)" % (who["org_name"], who["username"], time.time() - started))
        return {"token": token, "who": who}

    # -- jobs ----------------------------------------------------------------

    def start(self, session: dict, job: dict, plan: bool) -> str:
        job_id = secrets.token_hex(8)
        spec = dict(job, portal=session["portal"], plan=plan)
        spec_path = self.work / ("%s.json" % job_id)
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        proc = subprocess.Popen(
            self._command("--job", str(spec_path), "--kit", str(KIT)),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
        )
        state = {"id": job_id, "plan": plan, "lines": [], "done": False, "exit": None, "run": None, "plan_rows": None, "event": job["event"], "org": session["who"]["org_url"]}
        self.jobs[job_id] = state

        def pump():
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                if line.startswith("RUNFILE "):
                    state["run_file"] = line[8:].strip()
                elif line.startswith("PLAN "):
                    state["plan_rows"] = json.loads(line[5:])
                else:
                    state["lines"].append(line)
            state["exit"] = proc.wait()
            run_file = state.get("run_file")
            if run_file and Path(run_file).exists():
                state["run"] = json.loads(Path(run_file).read_text(encoding="utf-8"))
            state["done"] = True
            try:
                spec_path.unlink()
            except OSError:
                pass

        threading.Thread(target=pump, daemon=True).start()
        return job_id

    def busy(self) -> bool:
        return any(not j["done"] and not j["plan"] for j in self.jobs.values())

    def runs(self, org_url: str) -> list[dict]:
        out = []
        if RUNS.exists():
            for path in sorted(RUNS.glob("*.json"), reverse=True):
                try:
                    run = json.loads(path.read_text(encoding="utf-8"))
                except ValueError:
                    continue
                if run.get("org_url") != org_url:
                    continue
                site = next((i for i in run["items"] if i["type"] == "Hub Site Application"), None)
                out.append({
                    "file": path.name, "event": run["event"], "started": run["started"], "finished": run.get("finished"),
                    "count": len(run["items"]), "problems": len(run.get("problems") or []), "folder": run["folder"]["title"],
                    "site": site["url"] if site else None,
                })
        return out[:20]


def make_handler(app: App, port: int):
    allowed_hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}
    page = (HERE / "index.html").read_bytes

    class Handler(BaseHTTPRequestHandler):
        server_version = "StormKitDeployer"

        def log_message(self, *args):  # quiet
            return

        def _send(self, status: int, body, content_type="application/json", cookie: str | None = None):
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type + ("; charset=utf-8" if "json" in content_type or "html" in content_type else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            if cookie is not None:
                self.send_header("Set-Cookie", "kit=%s; Path=/; HttpOnly; SameSite=Strict%s" % (cookie, "" if cookie else "; Max-Age=0"))
            self.end_headers()
            self.wfile.write(data)

        def _session(self) -> dict | None:
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "kit" and value in app.sessions:
                    return app.sessions[value]
            return None

        def _guard(self) -> bool:
            if self.headers.get("Host", "") not in allowed_hosts:
                self._send(403, {"error": "This page answers this machine only."})
                return False
            return True

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 65536:
                raise ValueError("too large")
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(body, dict):
                raise ValueError("not an object")
            return body

        def do_GET(self):  # noqa: N802
            if not self._guard():
                return
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                return self._send(200, page(), "text/html")
            session = self._session()
            if path == "/api/state":
                if not session:
                    return self._send(200, {"signed_in": False})
                return self._send(200, {"signed_in": True, "who": session["who"], "kit": kit_info(), "busy": app.busy()})
            if path == "/api/portals":
                try:
                    return self._send(200, app.portals(refresh="refresh=1" in self.path))
                except Exception as exc:  # noqa: BLE001
                    return self._send(500, {"error": str(exc)})
            if not session:
                return self._send(401, {"error": "Sign in first."})
            if path == "/api/job":
                job_id = self.path.partition("id=")[2][:32]
                job = app.jobs.get(job_id)
                if not job or job["org"] != session["who"]["org_url"]:
                    return self._send(404, {"error": "No such job."})
                return self._send(200, {k: job.get(k) for k in ("id", "plan", "lines", "done", "exit", "run", "plan_rows", "event")})
            if path == "/api/runs":
                return self._send(200, {"runs": app.runs(session["who"]["org_url"])})
            return self._send(404, {"error": "Not found."})

        def do_POST(self):  # noqa: N802
            if not self._guard():
                return
            if self.headers.get("X-Kit") != "1":
                return self._send(403, {"error": "Requests must come from the page."})
            path = self.path.split("?")[0]
            try:
                body = self._body()
            except ValueError:
                return self._send(400, {"error": "Could not read the request."})
            if path == "/api/signin":
                try:
                    result = app.sign_in(str(body.get("portal") or ""))
                except ValueError as exc:
                    return self._send(403, {"error": str(exc)})
                except Exception as exc:  # noqa: BLE001
                    return self._send(500, {"error": str(exc)})
                return self._send(200, {"signed_in": True, "who": result["who"]}, cookie=result["token"])
            if path == "/api/signout":
                for part in (self.headers.get("Cookie") or "").split(";"):
                    name, _, value = part.strip().partition("=")
                    if name == "kit":
                        app.sessions.pop(value, None)
                return self._send(200, {"signed_in": False}, cookie="")
            session = self._session()
            if not session:
                return self._send(401, {"error": "Sign in first."})
            if path in ("/api/plan", "/api/deploy"):
                job, problem = check_request(body, {t["key"] for t in kit_info()["tools"]})
                if problem:
                    return self._send(400, {"error": problem})
                if path == "/api/deploy" and app.busy():
                    return self._send(409, {"error": "A deployment is already running. Wait for it to finish."})
                note("%s: %s, tools %s, by %s" % ("plan" if path == "/api/plan" else "DEPLOY", job["event"], ",".join(job["tools"]), session["who"]["username"]))
                return self._send(200, {"job": app.start(session, job, plan=path == "/api/plan")})
            return self._send(404, {"error": "Not found."})

    return Handler


def serve(app: App, port: int, tries: int = 10) -> ThreadingHTTPServer:
    last = None
    for candidate in range(port, port + tries):
        try:
            return ThreadingHTTPServer(("127.0.0.1", candidate), make_handler(app, candidate))
        except OSError as exc:
            last = exc
    raise SystemExit("No free port between %d and %d: %s" % (port, port + tries - 1, last))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="default %d; the next free port is used if it is taken" % DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    propy = find_propy()
    if not propy:
        print("ArcGIS Pro's Python (propy.bat) was not found. The deployer signs in through ArcGIS Pro.")
        return 1
    if not (KIT / "kit.json").exists():
        print("The kit folder is missing: %s has no kit.json. Download the whole package again." % KIT)
        return 1
    app = App(propy)
    server = serve(app, args.port)
    url = "http://127.0.0.1:%d/" % server.server_address[1]
    print("Storm Response Kit deployer: %s" % url)
    print("Close this window or press Ctrl+C to stop it.")
    note("started on %s" % url)

    def warm():  # have the list of orgs ready before the page asks for it
        try:
            app.portals()
        except Exception as exc:  # noqa: BLE001 - the page shows the same message
            note("portals: %s" % exc)

    threading.Thread(target=warm, daemon=True).start()
    if not args.no_browser:
        threading.Timer(0.6, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

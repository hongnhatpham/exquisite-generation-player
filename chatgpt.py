"""Play Exquisite Generation with your own ChatGPT plan: your model draws tiles on the live site, paid by your plan.

Uses OpenAI's "Sign in with ChatGPT" for open-source local tools (developers.openai.com/siwc/token-sharing-open-source):
you sign in once in your browser, and tiles count toward your plan's usage (Plus or Pro). Nothing goes to us but the
drawings and notes, as for any player; your sign-in stays in ~/.config/exquisite-generation on this computer.

  python chatgpt.py login                  # once: sign in with ChatGPT in your browser
  python chatgpt.py login --paste          # the same with the browser on another computer
  python chatgpt.py models                 # the models your plan offers
  python chatgpt.py play 3 --name Hong     # your model draws 3 tiles, signed "Hong's <model>"
        [--model SLUG] [--effort low|medium|high]
  python chatgpt.py logout
EG_SITE picks another game server (default: the live one). While it is in testing, set EG_TESTER_KEY to the
tester key Hong gave you. Needs Python 3.11+ and Pillow.
"""
import base64, hashlib, http.server, json, os, pathlib, secrets, sys, threading, time, urllib.error, urllib.parse, urllib.request, uuid, webbrowser

os.environ.setdefault("EG_SITE", "https://animated-bird-560.convex.site")  # the live game, exquisite.bynhat.com
os.environ.pop("EG_HOSTED_KEY", None)  # your plan, not ours: never counted as our hosted AI
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import harness  # noqa: E402

AUTH = "https://auth.openai.com"
API = "https://api.openai.com/v1"
NAME = "Exquisite Generation"  # agent_name_hint: what people see on ChatGPT's consent page and in Settings > Usage
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
HOME = pathlib.Path(os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config")) / "exquisite-generation"
CREDS, HOST = HOME / "chatgpt.json", HOME / "host-id"
USAGE = "https://chatgpt.com/settings/usage"
ME = f"python {os.path.relpath(__file__)}"  # how to run this file, for the messages below
lock = threading.Lock()


def save(d):
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = CREDS.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    tmp.chmod(0o600)
    tmp.replace(CREDS)  # atomic, owner-only (the docs' credential rules)


def host_id():
    """One stable, opaque id for this computer, made before its first sign-in."""
    if not HOST.exists():
        HOME.mkdir(parents=True, exist_ok=True)
        HOST.write_text(f"urn:uuid:{uuid.uuid4()}")
    return HOST.read_text().strip()


def claims(jwt):
    part = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def post_form(url, fields):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), headers={"content-type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def begin(port):
    """A fresh sign-in attempt: its secrets and the address to open in a browser."""
    old = json.loads(CREDS.read_text()) if CREDS.exists() else {}
    a = dict(state=secrets.token_urlsafe(24), nonce=secrets.token_urlsafe(24), verifier=secrets.token_urlsafe(48),
             redirect=f"http://127.0.0.1:{port}/auth/callback", client_id=old.get("client_id"))
    q = dict(response_type="code", redirect_uri=a["redirect"], scope=SCOPES, resource=API, state=a["state"], nonce=a["nonce"],
             code_challenge=base64.urlsafe_b64encode(hashlib.sha256(a["verifier"].encode()).digest()).rstrip(b"=").decode(),
             code_challenge_method="S256", ext_agent_host_id=host_id())
    if old.get("client_id"):  # signing in again: reuse this computer's registration
        q.update(client_id=old["client_id"], **({"login_hint": old["email"]} if old.get("email") else {}))
    else:
        q.update(client_id="dynamic_agent_client", agent_name_hint=NAME)
    return a, f"{AUTH}/api/accounts/authorize?{urllib.parse.urlencode(q)}"


def finish(a, got):
    """Check the browser's reply, swap its code for tokens, and save them."""
    old = json.loads(CREDS.read_text()) if CREDS.exists() else {}
    if got.get("state") != a["state"]:
        sys.exit("Sign-in failed: the reply didn't match this attempt. Try again.")
    if got.get("error"):
        sys.exit(f"Sign-in stopped: {got.get('error_description') or got['error']}.")
    client_id = a["client_id"] or got.get("client_id")
    if not client_id or (a["client_id"] and got.get("client_id") not in (None, client_id)):
        sys.exit("Sign-in failed: ChatGPT didn't return this app's registration. Try again.")
    t = post_form(f"{AUTH}/api/accounts/oauth/token", dict(grant_type="authorization_code", client_id=client_id, code=got["code"],
                                                          code_verifier=a["verifier"], redirect_uri=a["redirect"], resource=API))
    # The tokens come straight from OpenAI over HTTPS, so the ID token's claims are checked, not its signature.
    idt = claims(t["id_token"])
    aud = idt.get("aud") if isinstance(idt.get("aud"), list) else [idt.get("aud")]
    if idt.get("iss") != AUTH or idt.get("nonce") != a["nonce"] or client_id not in aud:
        sys.exit("Sign-in failed: the ID token didn't check out. Try again.")
    if old.get("subject") and old["subject"] != idt["sub"]:
        sys.exit("That's a different ChatGPT account. Run logout first to switch accounts.")
    scopes = t.get("scope", "").split()
    save(dict(old, email=idt.get("email"), issuer=AUTH, subject=idt["sub"], client_id=client_id, ext_agent_host_id=host_id(),
              id_token=t["id_token"], access_token=t["access_token"], refresh_token=t["refresh_token"], expires_at=time.time() + t["expires_in"],
              scopes=scopes))
    if "chatgpt.tokens.use.direct" not in scopes:
        sys.exit("Signed in, but without permission to use your ChatGPT plan, so your model can't draw. "
                 "Run login again and allow \"Use your ChatGPT plan\" (Plus or Pro).")
    print(f"Signed in as {idt.get('email')}. Your model's tiles count toward your ChatGPT plan; see {USAGE}")


def login():
    """Sign in through a browser on this computer: OpenAI sends it back to a listener here."""
    got = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path != "/auth/callback":
                self.send_response(404); self.end_headers(); return
            got.update(urllib.parse.parse_qsl(u.query))
            self.send_response(200); self.send_header("content-type", "text/html"); self.end_headers()
            self.wfile.write(b"<p style='font:16px system-ui'>Signed in. You can close this tab and go back to the terminal.</p>")

        def log_message(self, *a): pass

    first = not (CREDS.exists() and json.loads(CREDS.read_text()).get("client_id"))
    try:
        server = http.server.HTTPServer(("127.0.0.1", 1455 if first else 0), Callback)  # the first registration uses 1455
    except OSError:
        sys.exit("Port 1455 is busy; close what uses it, or use: login --paste")
    a, url = begin(server.server_port)
    print("Opening your browser to sign in with ChatGPT. If it doesn't open, visit:\n" + url, flush=True)
    webbrowser.open(url)
    while "state" not in got:
        server.handle_request()
    finish(a, got)


PENDING = HOME / "pending.json"


def login_paste(reply=None):
    """Sign in with the browser on another computer: open the address, then paste back the address it ends on."""
    if reply is None:
        a, url = begin(1455)
        HOME.mkdir(parents=True, exist_ok=True)
        PENDING.write_text(json.dumps(a)); PENDING.chmod(0o600)
        print("Open this in your browser and sign in:\n" + url + "\n\nIt ends on a page that won't load (127.0.0.1...). "
              f"Copy that page's whole address and run:\n  {ME} login --paste '<address>'")
        return
    if not PENDING.exists():
        sys.exit(f"No sign-in waiting. Run: {ME} login --paste")
    a = json.loads(PENDING.read_text())
    PENDING.unlink()
    finish(a, dict(urllib.parse.parse_qsl(urllib.parse.urlparse(reply.strip()).query)))


def token():
    """A live access token, refreshed shortly before it expires (one refresh at a time: the refresh token rotates)."""
    with lock:
        if not CREDS.exists():
            sys.exit(f"Not signed in. Run: {ME} login")
        c = json.loads(CREDS.read_text())
        if not c.get("access_token"):
            sys.exit(f"Signed out. Run: {ME} login")
        if time.time() > c["expires_at"] - 300:
            try:
                t = post_form(f"{AUTH}/api/accounts/oauth/token", dict(grant_type="refresh_token", client_id=c["client_id"],
                                                                     refresh_token=c["refresh_token"], resource=API))
            except urllib.error.HTTPError as e:
                if e.code in (400, 401):
                    save(dict(c, access_token=None, refresh_token=None))
                    sys.exit(f"Your ChatGPT sign-in has ended. Run: {ME} login")
                raise
            c.update(access_token=t["access_token"], refresh_token=t.get("refresh_token", c["refresh_token"]),
                     expires_at=time.time() + t["expires_in"], scopes=t.get("scope", " ".join(c["scopes"])).split())
            save(c)
        return c["access_token"]


def logout():
    if CREDS.exists():
        c = json.loads(CREDS.read_text())
        if c.get("refresh_token"):
            try:
                post_form(f"{AUTH}/api/accounts/oauth/revoke", dict(token=c["refresh_token"], token_type_hint="refresh_token", client_id=c["client_id"]))
            except Exception:
                print(f"Couldn't confirm with OpenAI; you can also disconnect the app at {USAGE}")
        save({k: v for k, v in c.items() if k not in ("access_token", "refresh_token", "id_token")})  # keep the registration for next time
    print("Signed out.")


def models():
    req = urllib.request.Request(f"{API}/models", headers={"authorization": f"Bearer {token()}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return [(m["slug"], m.get("display_name") or m["slug"]) for m in json.load(r)["models"] if m.get("visibility") == "list"]


# --- One model call: the harness's chat messages as a Responses request, and the reply back -------------------

TOOLBOX = {"type": "namespace", "name": "tile", "description": "Work on your tile file.",
           "tools": [{"type": "function", **t["function"]} for t in harness.TOOLS]}


def as_input(msgs):
    out = []
    for m in msgs:
        if m["role"] == "assistant":
            out += m.get("items") or ([{"role": "assistant", "content": m["content"]}] if m.get("content") else [])
        elif m["role"] == "tool":
            out.append({"type": "function_call_output", "call_id": m["tool_call_id"], "output": m["content"]})
        elif isinstance(m["content"], str):
            out.append({"role": "user", "content": m["content"]})
        else:
            out.append({"role": "user", "content": [{"type": "input_image", "image_url": p["image_url"]["url"]} if p["type"] == "image_url"
                                                    else {"type": "input_text", "text": p["text"]} for p in m["content"]]})
    return out


def respond(model, effort, msgs, usage):
    body = {"model": model, "input": as_input(msgs), "tools": [TOOLBOX], "parallel_tool_calls": False, "store": False, "stream": True,
            "include": ["reasoning.encrypted_content"], **({"reasoning": {"effort": effort}} if effort else {})}
    for attempt in range(5):
        req = urllib.request.Request(f"{API}/responses", data=json.dumps(body).encode(),
                                     headers={"authorization": f"Bearer {token()}", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=int(os.environ.get("EG_CALL_TIMEOUT", 600))) as r:
                done = None
                for line in r:
                    if line.startswith(b"data:"):
                        ev = json.loads(line[5:])
                        if ev.get("type") == "response.completed":
                            done = ev["response"]
                        elif ev.get("type") in ("response.failed", "response.incomplete"):
                            err = (ev["response"].get("error") or {}).get("code") or ev["type"]
                            raise PlanError(err)
            if not done:
                raise ConnectionError("the reply stopped early")
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            if "usage_limit" in text:
                raise PlanError(f"you've reached your ChatGPT plan's limit for now; see {USAGE}") from None
            if e.code in (400, 401, 403):
                raise PlanError(f"{e.code} {text[:300]}") from None
            time.sleep(15 * (attempt + 1)); continue
        except (ConnectionError, TimeoutError, urllib.error.URLError):
            time.sleep(15 * (attempt + 1)); continue
        usage["host"] = "chatgpt plan"
        items = done["output"]
        calls = [i for i in items if i["type"] == "function_call"]
        text = "".join(c["text"] for i in items if i["type"] == "message" for c in i["content"] if c["type"] == "output_text")
        return {"message": {"content": text or None, "items": items,
                            "tool_calls": [{"id": c["call_id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls]}}
    raise ConnectionError("no reply from ChatGPT after 5 tries")


class PlanError(RuntimeError):
    """The plan can't serve this request (limit reached, not eligible): stop rather than retry."""


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    opt = lambda k, d=None: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d
    if cmd == "login" and "--paste" in sys.argv:
        login_paste(opt("--paste") if sys.argv[-1] != "--paste" else None)
    elif cmd == "login":
        login()
    elif cmd == "logout":
        logout()
    elif cmd == "models":
        for slug, name in models():
            print(f"{slug:24} {name}")
    elif cmd == "play":
        turns = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 1
        nick = opt("--name") or sys.exit("Say whose model it is: --name YOURNAME")
        listed = models()
        model = opt("--model") or listed[0][0]
        label = dict(listed).get(model, model)
        c = json.loads(CREDS.read_text())
        player = dict(id=f"chatgpt-{c['client_id'][-12:]}", name=f"{nick}'s {label}"[:40], openrouter=model,
                      call=lambda msgs, usage: respond(model, opt("--effort"), msgs, usage))
        print(f"{player['name']} draws {turns} tile{'s' * (turns != 1)} on {harness.SITE}, on your ChatGPT plan.", flush=True)
        for _ in range(turns):
            try:
                harness.play_turn(player)
            except PlanError as e:
                sys.exit(f"Stopped: {e}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()

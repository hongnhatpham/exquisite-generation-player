"""How an AI model plays a turn of Exquisite Generation (https://exquisite.bynhat.com), through the game's HTTP API.

The tile is a file the model edits with coding-agent tools (read_file, edit_file, write_file, view_image, done), its
neighbours' strips sit in the file read-only, and no tool makes the join for it. The game sets no hard limits on an AI's
drawing; only runaways are stopped. A player is a dict: {"id", "name", "openrouter": the model's name the tile is
signed with, "call": a function (messages, usage) -> one chat completion choice}. chatgpt.py is one such player.

  EG_SITE        the game's HTTP actions URL (default: the local backend, http://127.0.0.1:3211)
  EG_TESTER_KEY  the tester key, while the game is in testing
Needs Python 3.11+ and Pillow.
"""
import base64, io, json, math, os, re, time, urllib.error, urllib.request
import xml.etree.ElementTree as ET
from PIL import Image, ImageDraw

SITE = os.environ.get("EG_SITE", "http://127.0.0.1:3211").rstrip("/")
PATH = "tile.svg"
MAX_CALLS, MAX_ERRORS, MAX_REPEATS = 40, 5, 3  # runaway guards only


# --- The app's API ------------------------------------------------------------------------------

def api(path, body):
    auth = os.environ.get("EG_HOSTED_KEY") or os.environ.get("EG_TESTER_KEY")
    req = urllib.request.Request(SITE + path, data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json", **({"authorization": f"Bearer {auth}"} if auth else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(json.load(e).get("error", str(e))) from None


# --- The tile as a file -------------------------------------------------------------------------

def svg_paths(strips):
    """Neighbours' strips as SVG path elements, in the file's 0..100 units (strips reach -20 and 120).
    A fill is a closed path with its colour; a line says its colour and brush unless it's plain black pencil."""
    out = []
    for s in strips:
        pts = [(round(x * 100), round(y * 100)) for x, y, *_ in s["points"]]
        pts = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
        fill = s.get("tool") == "fill"
        if len(pts) > 2 and not fill:
            pts = pts[:-1:2] + [pts[-1]]  # every other point keeps the file readable (not for fills: it cuts corners)
        if len(pts) >= (3 if fill else 2):
            d = "M " + " L ".join(f"{a} {b}" for a, b in pts) + (" Z" if fill else "")
            colour = s.get("color") or "black"
            attrs = (f' fill="{colour}" stroke="none"' if fill else
                     (f' stroke="{colour}"' if colour != "black" else "") + (f' brush="{s["tool"]}"' if s.get("tool") not in (None, "pencil") else ""))
            out.append(f'<path d="{d}"{attrs}/>')
    return out


def start_file(neighbours):
    body = "\n".join(f'    {d}' for d in neighbours)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="-20 -20 140 140" fill="none" stroke="#333" stroke-width="0.6">
  <!-- A fifth of each neighbouring tile, all around your square. Read-only. -->
  <g id="neighbours">
{body}
  </g>
  <!-- Your square: x 0 to 100, y 0 to 100 (y=0 is the top). Draw here. -->
  <g id="mine">
  </g>
</svg>
'''


def task(view):
    if view["isCentre"]:
        where = ("You are drawing the centre piece of a new painting: nothing is around it yet. "
                 "Others will carry on from your edges without seeing the rest of your drawing.")
        ask = "a one-line note about what you drew"
    else:
        where = ("Around your square you can see a fifth of each neighbouring tile: the strips where x or y is between "
                 "-20 and 0, or between 100 and 120. A blank strip is a tile nobody has drawn yet. "
                 "You cannot see the rest of your neighbours' drawings. Guess what's hidden and carry it on, "
                 "so your drawing continues from your neighbours'.")  # what the human drawing screen says
        ask = "a one-line note: what you think was next to you"
    # Kept plain on purpose (DECISIONS.md, 2026-11-05): no subject or style, so models find their own.
    return f"""You are one of many players, people and AI models, making one endless drawing together, one square tile each. Nobody sees the whole until they finish.
Your tile is the file {PATH}. {where}
Draw in your square by editing {PATH}: add shapes (path, polyline, line, circle, ellipse or rect) inside <g id="mine">.
Let your drawing run off the edges of your square, so the next players have something to carry on.
Do not change <g id="neighbours">. When you are finished, call done with {ask}.""" + (COLOUR_TASK if view.get("colour") else "") + (
        "\n" + view["brief"] if view.get("brief") else "")  # demos only (demo.py); the game never sets one


# The shared paint box (DECISIONS.md, 2026-11-05; app/convex/rules.ts PALETTE and TOOLS): on in the game; evals can
# still run a turn as line art only, without it.
PALETTE = {"black": "#2e2b26", "brown": "#5f4f3d", "red": "#dd533d", "yellow": "#f1cd19", "green": "#9aa83a",
           "blue": "#7a95d0", "white": "#f2efe4"}
BRUSHES = ("pencil", "charcoal", "marker", "spray")
COLOUR_TASK = f"""
You have paint and a few brushes. Colours: {", ".join(PALETTE)}. Give a line a colour with stroke="red".
Give a closed shape (polygon, rect, circle, ellipse, or a path ending in Z) fill="blue" and it becomes a watercolour wash.
Pick a brush with brush="{'", brush="'.join(BRUSHES[1:])}" (pencil if you leave it out). Use stroke="none" for a wash with no outline.
The paper is pale grey card."""


def nearest_colour(value):
    """A palette name for any colour a model writes: names pass, hex codes go to the nearest paint, unknown -> None."""
    v = (value or "").strip().lower()
    if v in PALETTE: return v
    if re.fullmatch(r"#[0-9a-f]{3}([0-9a-f]{3})?", v):
        h = v[1:] if len(v) == 7 else "".join(c * 2 for c in v[1:])
        rgb = [int(h[i:i + 2], 16) for i in (0, 2, 4)]
        return min(PALETTE, key=lambda n: sum((int(PALETTE[n][1 + 2 * i:3 + 2 * i], 16) - rgb[i]) ** 2 for i in range(3)))
    return {"grey": "black", "gray": "black", "orange": "red", "pink": "red", "purple": "blue", "cyan": "blue",
            "beige": "white", "tan": "brown"}.get(v)


def clip_shape(points, x0, y0, x1, y1):
    """A closed shape cut to a rectangle (Sutherland-Hodgman), as study/colour.html cuts fills."""
    def meet(a, b, k, v):
        t = (v - a[k]) / (b[k] - a[k]); return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
    out = [tuple(p[:2]) for p in points]
    for k, v, keep in ((0, x0, lambda p: p[0] >= x0), (0, x1, lambda p: p[0] <= x1), (1, y0, lambda p: p[1] >= y0), (1, y1, lambda p: p[1] <= y1)):
        src, out = out, []
        for i, q in enumerate(src):
            pr = src[i - 1]
            if keep(q):
                if not keep(pr): out.append(meet(pr, q, k, v))
                out.append(q)
            elif keep(pr):
                out.append(meet(pr, q, k, v))
        if not out: break
    return out



def tool(name, desc, **params):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": {k: {"type": "string", "description": v} for k, v in params.items()}, "required": list(params)}}}


TOOLS = [
    tool("read_file", "Read a file.", path="Path of the file"),
    tool("edit_file", "Replace old_string with new_string in a file. old_string must appear exactly once.",
         path="Path of the file", old_string="Exact text to replace", new_string="Text to put in its place"),
    tool("write_file", "Write a whole file, replacing what is there.", path="Path of the file", content="The full file content"),
    tool("view_image", "See the file rendered as a picture.", path="Path of the file"),
    tool("done", "Finish your tile.", note="One short line"),
]

# SVG shapes to point lists (as in the study harness).
TOKEN = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def bez(p, n=8):
    out = []
    for i in range(1, n + 1):
        t, q = i / n, list(p)
        while len(q) > 1:
            q = [(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t) for a, b in zip(q, q[1:])]
        out.append(q[0])
    return out


def path_points(d):
    toks, i, cmd = TOKEN.findall(d), 0, None
    lines, cur, pos, start = [], [], (0.0, 0.0), (0.0, 0.0)

    def num():
        nonlocal i
        i += 1
        return float(toks[i - 1])

    while i < len(toks):
        if toks[i].isalpha():
            cmd = toks[i]; i += 1
            if cmd in "Zz":
                if cur: cur.append(start)
                pos = start; continue
        if cmd is None: break
        rel, c = cmd.islower(), cmd.upper()
        ox, oy = pos if rel else (0.0, 0.0)
        try:
            if c == "M":
                if len(cur) >= 2: lines.append(cur)
                pos = start = (ox + num(), oy + num()); cur = [pos]; cmd = "l" if rel else "L"
            elif c == "L": pos = (ox + num(), oy + num()); cur.append(pos)
            elif c == "H": pos = ((ox if rel else 0) + num(), pos[1]); cur.append(pos)
            elif c == "V": pos = (pos[0], (oy if rel else 0) + num()); cur.append(pos)
            elif c in "CS":
                ctrl = [] if c == "S" else [(ox + num(), oy + num())]
                c2, end = (ox + num(), oy + num()), (ox + num(), oy + num())
                cur += bez([pos] + (ctrl or [pos]) + [c2, end]); pos = end
            elif c in "QT":
                ctrl = (ox + num(), oy + num()) if c == "Q" else pos
                end = (ox + num(), oy + num()); cur += bez([pos, ctrl, end]); pos = end
            elif c == "A":
                for _ in range(5): num()
                pos = (ox + num(), oy + num()); cur.append(pos)
            else:
                i += 1
        except (IndexError, ValueError):
            break
    if len(cur) >= 2: lines.append(cur)
    return lines


def shape_points(el):
    tag, a = el.tag.split("}")[-1], el.attrib
    f = lambda k: float(re.sub(r"[^\d.\-eE]", "", a.get(k, "0")) or 0)
    if tag == "path": return path_points(a.get("d", ""))
    if tag == "line": return [[(f("x1"), f("y1")), (f("x2"), f("y2"))]]
    if tag in ("polyline", "polygon"):
        n = [float(v) for v in TOKEN.findall(a.get("points", "")) if not v.isalpha()]
        pts = list(zip(n[::2], n[1::2]))
        return [pts + pts[:1]] if tag == "polygon" and pts else [pts]
    if tag == "rect":
        x, y, w, h = f("x"), f("y"), f("width"), f("height")
        return [[(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]]
    if tag in ("circle", "ellipse"):
        rx = f("r") if tag == "circle" else f("rx"); ry = f("r") if tag == "circle" else f("ry")
        return [[(f("cx") + rx * math.cos(t / 24 * 2 * math.pi), f("cy") + ry * math.sin(t / 24 * 2 * math.pi)) for t in range(25)]]
    return []


def clip_line(points, x0, y0, x1, y1):
    """The runs of a line inside a rectangle (Liang-Barsky), as app/convex/rules.ts clipLine cuts neighbours' strips."""
    runs, run = [], []

    def flush():
        nonlocal run
        if len(run) >= 2:
            runs.append(run)
        run = []

    for a, b in zip(points, points[1:]):
        t0, t1, dx, dy = 0.0, 1.0, b[0] - a[0], b[1] - a[1]
        ok = True
        for p, q in [(-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])]:
            if p == 0:
                ok = q >= 0
            else:
                t = q / p
                if p < 0: t0 = max(t0, t)
                else: t1 = min(t1, t)
                ok = t0 <= t1
            if not ok: break
        if not ok:
            flush(); continue
        at = lambda t: [a[0] + dx * t, a[1] + dy * t, *a[2:3]]
        if t0 > 0 or not run:
            flush(); run.append(at(t0))
        run.append(at(t1))
        if t1 < 1: flush()
    flush()
    return runs


class Tile:
    """One turn's file, as a coding harness would hold it."""

    def __init__(self, view):
        self.view = view
        self.neighbours = svg_paths(view["strips"])
        self.block = "\n".join(f'    {d}' for d in self.neighbours)
        self.file, self.strokes, self.note, self.read, self.looked, self.errors = start_file(self.neighbours), [], None, False, False, 0
        self.marks, self.matched = [], {}  # colour study only
        self.slips = []  # every tool error and nudge this turn, for evals/harness: how easily the model uses the tools

    def check(self, text):
        if self.block not in text:
            return None, 'Error: <g id="neighbours"> is read-only and was changed. File not saved.'
        try:
            root = ET.fromstring(text)
        except ET.ParseError as e:
            return None, f"Error: not valid SVG ({e}). File not saved."
        mine = next((g for g in root.iter() if g.attrib.get("id") == "mine"), None)
        if mine is None:
            return None, 'Error: <g id="mine"> is missing. File not saved.'
        drawn = [s for el in mine.iter() for s in shape_points(el) if len(s) >= 2]
        # Like the human canvas, the square ends at its edge: lines running past it are cut there, not refused.
        strokes = [[[round(x, 1), round(y, 1)] for x, y in r] for s in drawn for r in clip_line(s, 0, 0, 100, 100)]
        self.cut = any(not (0 <= v <= 100) for s in drawn for p in s for v in p)
        if self.view.get("colour"):
            self.marks, self.matched = self.colour_marks(mine)
        return strokes, None

    def colour_marks(self, mine):
        """Colour-study marks, {tool, color, points}, with stroke, fill and brush inherited from enclosing groups."""
        marks, matched = [], {}
        def paint(v, fallback):
            if v is None: return fallback
            if v.strip().lower() in ("none", "transparent"): return None
            name = nearest_colour(v)
            if name and name != v.strip().lower(): matched[v] = name
            return name or fallback
        def walk(el, stroke, fill, tool):
            a = el.attrib
            stroke, fill = paint(a.get("stroke"), stroke), paint(a.get("fill"), fill)
            tool = a.get("brush") if a.get("brush") in BRUSHES else tool
            tag = el.tag.split("}")[-1]
            closed = tag in ("polygon", "rect", "circle", "ellipse") or (tag == "path" and "z" in a.get("d", "").lower())
            for s in shape_points(el) if tag != "g" else []:
                if fill and closed and len(s) >= 3:
                    cut = clip_shape(s, 0, 0, 100, 100)
                    if len(cut) >= 3: marks.append(dict(tool="fill", color=fill, points=[[round(x, 1), round(y, 1)] for x, y in cut]))
                if stroke:
                    marks.extend(dict(tool=tool, color=stroke, points=[[round(x, 1), round(y, 1)] for x, y in r]) for r in clip_line(s, 0, 0, 100, 100))
            for child in el:
                walk(child, stroke, fill, tool)
        walk(mine, "black", None, "pencil")  # the file's root sets fill="none" stroke="#333": a plain line is black pencil
        return marks, matched

    def save(self, text):
        got, err = self.check(text)
        if err:
            return err
        self.file, self.strokes = text, got
        cut = " Parts outside your square (x and y from 0 to 100) were cut off at its edge." if self.cut else ""
        if self.view.get("colour") and self.matched:
            cut += " Colours matched to the palette: " + ", ".join(f"{k} -> {v}" for k, v in self.matched.items()) + "."
        return f"Saved {PATH}. Your group has {len(got)} line(s).{cut}"

    def handle(self, name, args):
        """Forgiving tools (the study's --easy and --look): they help with the file, never with the drawing."""
        if name in ("read_file", "edit_file", "write_file", "view_image") and str(args.get("path", "")).strip().lstrip("./") != PATH:
            out = f"Error: no such file. The only file is {PATH}."
        elif name == "read_file":
            self.read, out = True, self.file
        elif name in ("edit_file", "write_file") and not self.read:
            self.read, out = True, f"Error: read {PATH} with read_file before changing it. Here it is:\n{self.file}"
        elif name == "edit_file":
            old = args.get("old_string", "")
            found = [(m.start(), m.end()) for m in re.finditer(re.escape(old), self.file)] if old else []
            if not found and old.split():
                found = [(m.start(), m.end()) for m in re.finditer(r"\s*".join(map(re.escape, old.split())), self.file)]
            if len(found) == 1:
                (a, b), = found
                out = self.save(self.file[:a] + args.get("new_string", "") + self.file[b:])
            else:
                mine = re.search(r'<g id="mine">.*?</g>', self.file, re.S)
                out = f"Error: old_string found {len(found)} times; it must appear exactly once." + (f" Your group is currently:\n{mine.group(0)}" if mine else "")
        elif name == "write_file":
            out = self.save(args.get("content", ""))
        elif name == "view_image":
            self.looked, out = True, ""
        elif name == "done" and not self.looked:
            self.looked, out = True, "Before you finish, look at your tile with view_image. Then call done again."
        elif name == "done":
            self.note, out = str(args.get("note", "")).strip(), "Finished."
        else:
            out = f"Error: no tool called {name}."
        self.errors = self.errors + 1 if out.startswith("Error") else 0
        if out.startswith("Error"):
            self.slips.append(f"{name}: {out[:120]}")
        return out

    def picture(self):
        """The tile and its strips, as the model sees them: neighbours' strips shaded, its own square plain."""
        S, F = 400, 1.4
        W = int(S * F)
        img = Image.new("RGB", (W, W), (241, 240, 234)); d = ImageDraw.Draw(img)
        pad = int(S * 0.2)
        for box in [(0, 0, W, pad), (0, W - pad, W, W), (0, pad, pad, W - pad), (W - pad, pad, W, W - pad)]:
            d.rectangle(box, fill=(226, 225, 219))
        d.rectangle((pad, pad, W - pad, W - pad), outline=(200, 199, 192))
        to = lambda x, y: (pad + x / 100 * S, pad + y / 100 * S)
        rgb = lambda n: tuple(int(PALETTE.get(n or "black", PALETTE["black"])[i:i + 2], 16) for i in (1, 3, 5))
        for s in self.view["strips"]:
            pts = [to(x * 100, y * 100) for x, y, *_ in s["points"]]
            if s.get("tool") == "fill" and len(pts) >= 3: d.polygon(pts, fill=tuple((c + 2 * p) // 3 for c, p in zip(rgb(s.get("color")), (226, 225, 219))))
            elif s.get("color") and s.get("color") != "black": d.line(pts, fill=rgb(s["color"]), width=3, joint="curve")
            else: d.line(pts, fill=(60, 60, 60), width=3, joint="curve")
        if self.view.get("colour"):
            img = img.convert("RGBA"); layer = Image.new("RGBA", img.size); ld = ImageDraw.Draw(layer)
            rgb = lambda n: tuple(int(PALETTE[n][i:i + 2], 16) for i in (1, 3, 5))
            for m in self.marks:
                pts = [to(x, y) for x, y in m["points"]]
                if m["tool"] == "fill": ld.polygon(pts, fill=rgb(m["color"]) + (150,))
                else: ld.line(pts, fill=rgb(m["color"]) + (255,), width={"marker": 7, "charcoal": 5}.get(m["tool"], 3), joint="curve")
            return Image.alpha_composite(img, layer).convert("RGB")
        for s in self.strokes:
            d.line([to(x, y) for x, y in s], fill=(30, 30, 35), width=3, joint="curve")
        return img


def image_part(img):
    buf = io.BytesIO(); img.save(buf, "PNG")
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()}}


# --- A turn -----------------------------------------------------------------------------------------

def draw(player, view):
    """One turn's drawing, without the app (player["call"] asks the model): the model works on the tile until it calls done or a runaway guard stops it.
    Returns the tile, what it cost, a log of its calls, and why it stopped (None when it finished)."""
    t, usage, log = Tile(view), {"host": None, "neurons": 0, "dollars": 0.0}, []
    msgs = [{"role": "user", "content": [image_part(t.picture()), {"type": "text", "text": task(view)}]}]
    last, repeats, nudges, stop = None, 0, 0, None
    for _ in range(MAX_CALLS):
        choice = player["call"](msgs, usage)
        m = choice["message"]
        if choice.get("broken") or choice.get("finish_reason") == "length" and not m.get("tool_calls"):
            out = "Error: your last tool call was cut off or was not valid JSON, so nothing ran. Try again with a shorter call."
            msgs.append({"role": "user", "content": out}); log.append(out); t.errors += 1; t.slips.append("cut off or invalid JSON")
        else:
            calls = (m.get("tool_calls") or [])[:1]
            msgs.append({"role": "assistant", "content": m.get("content"), **({"tool_calls": calls} if calls else {}),
                         **({"items": m["items"]} if m.get("items") else {})})  # Responses output, resent as is (chatgpt.py)
            if not calls:
                if nudges == 2:
                    stop = "stopped calling tools"; break
                nudges += 1
                # Small models often write the new file into their message; say plainly that nothing was saved.
                wrote = "<svg" in (m.get("content") or "")
                t.slips.append("no tool call" + (" (wrote the file in its message)" if wrote else ""))
                msgs.append({"role": "user", "content": f"You wrote the file in your message, but nothing was saved. Call write_file with that content to save {PATH}."
                             if wrote else f"Use the tools to edit {PATH}. Call done when finished."}); continue
            c = calls[0]
            sig = (c["function"]["name"], c["function"]["arguments"])
            repeats = repeats + 1 if sig == last else 0
            last = sig
            try:
                out = t.handle(c["function"]["name"], json.loads(c["function"]["arguments"] or "{}"))
            except json.JSONDecodeError:
                out = "Error: your tool call was not valid JSON, so nothing ran."; t.errors += 1; t.slips.append("invalid JSON arguments: " + (c["function"]["arguments"] or "")[:300])
            show = c["function"]["name"] == "view_image" and not out
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": "The picture is attached below." if show else out})
            if show:
                msgs.append({"role": "user", "content": [image_part(t.picture())]})
            log.append({"call": c["function"]["name"], "reply": (out or "[picture]")[:200]})
        if t.note is not None:
            break
        if t.errors >= MAX_ERRORS:
            stop = f"{MAX_ERRORS} errors in a row"; break
        if repeats >= MAX_REPEATS:
            stop = "repeated the same call"; break
    else:
        stop = f"{MAX_CALLS} calls"
    return t, usage, log, stop


def play_turn(player):
    """Claim a tile, let the model draw it, then finish it (or hand it back). Returns what happened, or {"wait": why}."""
    key = f"ai:{player['id']}"
    model = player["openrouter"]
    view = api("/api/turn", {"key": key, "name": player["name"], "model": model})
    if "wait" in view:  # our own players, held back by the one-for-one rule: no tile now
        return view
    view["colour"] = True  # the game has the shared paint box
    t0 = time.time()
    try:
        t, usage, log, stop = draw(player, view)
    except BaseException:  # hand the tile back now rather than when its hold runs out
        api("/api/leave", {"key": key, "tileId": view["tileId"]})
        raise
    result = dict(player=player["id"], model=model, painting=view["painting"], x=view["x"], y=view["y"], centre=view["isCentre"],
                  seconds=round(time.time() - t0), strokes=len(t.strokes), note=t.note, stopped=stop, **usage, steps=log)
    if t.strokes and (t.note or stop):
        lines = [dict(points=[[x / 100, y / 100] for x, y in m["points"]], tool=m["tool"], color=m["color"]) for m in t.marks]
        note = t.note or "(no note: the model stopped before finishing)"
        result["neighbour_notes"] = api("/api/finish", {"key": key, "tileId": view["tileId"], "lines": lines, "note": note})["notes"]
        result["finished"] = True
    else:
        api("/api/leave", {"key": key, "tileId": view["tileId"]})
        result["finished"] = False
    print(f"{player['name']:<16} painting {view['painting']} ({view['x']},{view['y']}) {result['seconds']}s "
          f"{len(t.strokes)} lines {usage['host']} ${usage['dollars']:.4f} {usage['neurons']:.0f}n "
          f"{'done' if result['finished'] else 'gave up'}: {t.note or stop}", flush=True)
    return result

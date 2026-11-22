"""Draw the README graphics into a folder. Run from the repo root: python assets/make_svgs.py assets."""
import math
import sys
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)

BG, INK, MUTED, SOFT = "#060a14", "#e7ecf6", "#8d99b3", "#a9b4cc"
CARD, LINE = "#0d1424", "#94a3b8"
VIOLET, VIOLET_T = "#8b5cf6", "#c4b5fd"
CYAN, CYAN_T = "#22d3ee", "#67e8f9"
TEAL, TEAL_T = "#2dd4bf", "#5eead4"
GREEN, GREEN_T = "#22c55e", "#86efac"
ROSE, ROSE_T = "#f43f5e", "#fda4af"
AMBER, AMBER_T = "#f59e0b", "#fcd34d"
TINT = {CYAN: CYAN_T, TEAL: TEAL_T, VIOLET: VIOLET_T, GREEN: GREEN_T, ROSE: ROSE_T, AMBER: AMBER_T}


def w(text, size, bold=False, mono=False):
    k = 0.61 if mono else (0.57 if bold else 0.53)
    return len(text) * size * k


def t(x, y, s, size=14, fill=INK, weight=None, anchor=None, cls="sans", extra=""):
    a = f' font-size="{size}" fill="{fill}"'
    if weight:
        a += f' font-weight="{weight}"'
    if anchor:
        a += f' text-anchor="{anchor}"'
    return f'<text class="{cls}" x="{x:.1f}" y="{y:.1f}"{a}{extra}>{escape(s)}</text>'


def chip(x, y, s, color, tcolor, size=13, h=30, pad=14, bold=True):
    cw = w(s, size, bold) + 2 * pad
    return (f'<rect x="{x:.1f}" y="{y}" width="{cw:.0f}" height="{h}" rx="{h/2}" fill="{color}" fill-opacity=".13" '
            f'stroke="{color}" stroke-opacity=".5"/>'
            + t(x + cw / 2, y + h / 2 + size * 0.36, s, size, tcolor, "600" if bold else None, "middle")), cw


def head(W, H, title, desc):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-labelledby="t d">
  <title id="t">{escape(title)}</title>
  <desc id="d">{escape(desc)}</desc>
  <defs>
    <linearGradient id="grad" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#a78bfa"/><stop offset=".45" stop-color="#6366f1"/><stop offset="1" stop-color="#22d3ee"/></linearGradient>
    <linearGradient id="gradH" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#a78bfa"/><stop offset=".5" stop-color="#6366f1"/><stop offset="1" stop-color="#22d3ee"/></linearGradient>
    <linearGradient id="gradV" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#a78bfa"/><stop offset="1" stop-color="#6366f1"/></linearGradient>
    <linearGradient id="gradC" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#67e8f9"/><stop offset="1" stop-color="#0891b2"/></linearGradient>
    <radialGradient id="glowV" cx=".08" cy="-.05" r=".7"><stop offset="0" stop-color="#8b5cf6" stop-opacity=".35"/><stop offset="1" stop-color="#8b5cf6" stop-opacity="0"/></radialGradient>
    <radialGradient id="glowC" cx="1" cy=".1" r=".6"><stop offset="0" stop-color="#22d3ee" stop-opacity=".2"/><stop offset="1" stop-color="#22d3ee" stop-opacity="0"/></radialGradient>
    <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse"><path d="M40 0H0V40" fill="none" stroke="#94a3b8" stroke-opacity=".07"/></pattern>
    <linearGradient id="fadeGrid" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".9"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>
    <mask id="gridMask"><rect width="{W}" height="{H}" fill="url(#fadeGrid)"/></mask>
    <filter id="shadow" x="-20%" y="-20%" width="140%" height="150%"><feDropShadow dx="0" dy="14" stdDeviation="16" flood-color="#000" flood-opacity=".5"/></filter>
    <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="{CYAN}"/></marker>
    <marker id="arrowV" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="{VIOLET_T}"/></marker>
    <marker id="arrowR" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="{ROSE}"/></marker>
    <marker id="arrowG" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="{GREEN}"/></marker>
    <style>
      .sans{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}}
      .mono{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace}}
      .flow{{stroke-dasharray:6 7;animation:flow 1.1s linear infinite}}
      @keyframes flow{{to{{stroke-dashoffset:-26}}}}
      .pulse{{animation:pulse 2.4s ease-in-out infinite}}
      @keyframes pulse{{0%,100%{{opacity:.45}}50%{{opacity:1}}}}
      .grow{{transform-box:fill-box;transform-origin:50% 100%;animation:grow 1.1s cubic-bezier(.2,.8,.2,1) both}}
      @keyframes grow{{from{{transform:scaleY(0)}}to{{transform:scaleY(1)}}}}
      .pop{{transform-box:fill-box;transform-origin:50% 50%;animation:pop 2.8s ease-in-out infinite}}
      @keyframes pop{{0%,100%{{transform:scale(1)}}50%{{transform:scale(1.08)}}}}
      @media (prefers-reduced-motion:reduce){{.flow,.pulse,.grow,.pop{{animation:none}}}}
    </style>
  </defs>
  <rect width="{W}" height="{H}" rx="24" fill="{BG}"/>
  <rect width="{W}" height="{H}" rx="24" fill="url(#glowV)"/>
  <rect width="{W}" height="{H}" rx="24" fill="url(#glowC)"/>
  <rect width="{W}" height="{H}" rx="24" fill="url(#grid)" mask="url(#gridMask)"/>
  <rect x=".5" y=".5" width="{W-1}" height="{H-1}" rx="24" fill="none" stroke="{LINE}" stroke-opacity=".18"/>
'''


def card(x, y, cw, ch, stroke=LINE, sop=".22", fill=CARD, rx=16, shadow=True):
    f = ' filter="url(#shadow)"' if shadow else ""
    return f'<rect x="{x}" y="{y}" width="{cw}" height="{ch}" rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-opacity="{sop}"{f}/>'


def title_block(title, sub, y=70):
    return t(48, y, title, 30, INK, "800", extra=' letter-spacing="-.6"') + t(48, y + 30, sub, 15.5, MUTED)


def report_page(x, y, pw, ph):
    """A white annual-report page with a P&L table and the revenue row highlighted."""
    s = [f'<g filter="url(#shadow)"><rect x="{x}" y="{y}" width="{pw}" height="{ph}" rx="12" fill="#f8fafc"/></g>']
    ix, iw = x + 20, pw - 40
    s.append(f'<rect x="{ix}" y="{y+22}" width="26" height="26" rx="7" fill="url(#grad)"/>')
    s.append(f'<rect x="{ix+36}" y="{y+25}" width="{iw*0.42:.0f}" height="9" rx="4.5" fill="#cbd5e1"/>')
    s.append(f'<rect x="{ix+36}" y="{y+40}" width="{iw*0.28:.0f}" height="7" rx="3.5" fill="#e2e8f0"/>')
    s.append(f'<rect x="{ix+iw-60}" y="{y+25}" width="60" height="7" rx="3.5" fill="#e2e8f0"/>')
    s.append(f'<line x1="{ix}" y1="{y+62}" x2="{ix+iw}" y2="{y+62}" stroke="#e2e8f0"/>')
    s.append(t(ix, y + 88, "PROFIT AND LOSS", 11, "#334155", "800", extra=' letter-spacing="1"'))
    s.append(t(ix + iw, y + 88, "₹ crore", 9.5, "#64748b", None, "end"))
    ty = y + 100
    c1, c2 = ix + iw * 0.62, ix + iw
    s.append(f'<rect x="{ix}" y="{ty}" width="{iw}" height="22" fill="#eef2ff"/>')
    s.append(t(ix + 6, ty + 15, "Particulars", 9.5, "#475569", "700"))
    s.append(t(c1 + 22, ty + 15, "FY25", 9.5, "#475569", "700", "end"))
    s.append(t(c2 - 6, ty + 15, "FY24", 9.5, "#475569", "700", "end"))
    rows = [("Revenue from operations" if pw >= 300 else "Revenue from ops", "4,500", "4,000"), ("Other income", "120", "95"),
            ("Total expenses", "3,610", "3,280"), ("Profit before tax", "1,010", "815"),
            ("Tax expense", "255", "205"), ("Profit for the year", "755", "610")]
    for i, (a, b, c) in enumerate(rows):
        ry = ty + 22 + i * 24
        if i == 0:
            s.append(f'<rect class="pulse" x="{ix-4}" y="{ry+2}" width="{iw+8}" height="22" rx="4" fill="{CYAN}" '
                     f'fill-opacity=".14" stroke="{CYAN}" stroke-width="1.6" stroke-dasharray="4 3"/>')
        bold = "700" if i in (0, 5) else None
        s.append(t(ix + 6, ry + 17, a, 9.5, "#334155", bold))
        s.append(t(c1 + 22, ry + 17, b, 9.5, "#0f172a", "700" if i == 0 else bold, "end"))
        s.append(t(c2 - 6, ry + 17, c, 9.5, "#334155", bold, "end"))
        s.append(f'<line x1="{ix}" y1="{ry+24}" x2="{ix+iw}" y2="{ry+24}" stroke="#eef2f7"/>')
    py = ty + 22 + 6 * 24 + 22
    for k, frac in enumerate((0.95, 0.88, 0.92, 0.6)):
        if py + k * 14 + 7 < y + ph - 30:
            s.append(f'<rect x="{ix}" y="{py + k*14}" width="{iw*frac:.0f}" height="7" rx="3.5" fill="#e2e8f0"/>')
    s.append(t(ix + iw, y + ph - 14, "p. 87", 9.5, "#94a3b8", None, "end"))
    return "\n".join(s), (ix + iw + 4, ty + 22 + 13)


def icon(kind, cx, cy, c, sw=2.2):
    st = f'fill="none" stroke="{c}" stroke-width="{sw}" stroke-linecap="round" stroke-linejoin="round"'
    if kind == "upload":
        return (f'<path d="M{cx-13} {cy-18}h17l9 9v27h-26z" {st}/><path d="M{cx+4} {cy-18}v9h9" {st}/>'
                f'<path d="M{cx-0.5} {cy+12}v-14M{cx-6} {cy+3}l5.5-5.5 5.5 5.5" {st}/>')
    if kind == "parse":
        return (f'<rect x="{cx-17}" y="{cy-18}" width="34" height="36" rx="3" {st}/>'
                f'<path d="M{cx-17} {cy-8}h34M{cx-17} {cy+2}h34M{cx-17} {cy+11}h34M{cx-4} {cy-8}v26" {st}/>')
    if kind == "index":
        return (f'<circle cx="{cx-3}" cy="{cy-3}" r="12" {st}/><path d="M{cx+6} {cy+6}l9 9" {st}/>'
                f'<path d="M{cx-9} {cy-3}h12M{cx-9} {cy+2}h8" {st}/>')
    if kind == "plan":
        return (f'<rect x="{cx-6}" y="{cy-19}" width="12" height="10" rx="2" {st}/>'
                f'<rect x="{cx-20}" y="{cy+9}" width="12" height="10" rx="2" {st}/>'
                f'<rect x="{cx-6}" y="{cy+9}" width="12" height="10" rx="2" {st}/>'
                f'<rect x="{cx+8}" y="{cy+9}" width="12" height="10" rx="2" {st}/>'
                f'<path d="M{cx} {cy-9}v18M{cx} {cy}h-14v9M{cx} {cy}h14v9" {st}/>')
    if kind == "calc":
        return (f'<rect x="{cx-14}" y="{cy-19}" width="28" height="38" rx="4" {st}/>'
                f'<rect x="{cx-8}" y="{cy-13}" width="16" height="7" rx="1.5" {st}/>'
                + "".join(f'<circle cx="{cx-7+dx*7}" cy="{cy+3+dy*8}" r="1.8" fill="{c}"/>'
                          for dx in range(3) for dy in range(2)))
    if kind == "verify":
        return (f'<path d="M{cx} {cy-19}l15 5v11c0 10-6.5 16.5-15 20c-8.5-3.5-15-10-15-20v-11z" {st}/>'
                f'<path d="M{cx-7} {cy}l5 5l9-10" {st}/>')
    if kind == "report":
        return (f'<path d="M{cx-14} {cy-19}h28v38h-28z" {st}/>'
                f'<path d="M{cx-8} {cy-9}l2.5 2.5 4-4M{cx+1} {cy-8}h7M{cx-8} {cy+1}l2.5 2.5 4-4M{cx+1} {cy+2}h7M{cx-8} {cy+12}h16" {st}/>')
    if kind == "hex":
        pts = " ".join(f"{cx+19*math.cos(k*math.pi/3+math.pi/6):.1f},{cy+19*math.sin(k*math.pi/3+math.pi/6):.1f}"
                       for k in range(6))
        return f'<polygon points="{pts}" {st}/><circle cx="{cx}" cy="{cy}" r="5" {st}/>'
    if kind == "user":
        return (f'<circle cx="{cx}" cy="{cy-7}" r="7.5" {st}/><path d="M{cx-14} {cy+17}c0-9 6-14 14-14s14 5 14 14" {st}/>')
    if kind == "db":
        return (f'<ellipse cx="{cx}" cy="{cy-10}" rx="13" ry="5" {st}/>'
                f'<path d="M{cx-13} {cy-10}v20c0 3 6 5 13 5s13-2 13-5v-20M{cx-13} {cy}c0 3 6 5 13 5s13-2 13-5" {st}/>')
    raise ValueError(kind)


def icon_tile(kind, x, y, c, size=56):
    return (f'<rect x="{x}" y="{y}" width="{size}" height="{size}" rx="14" fill="{c}" fill-opacity=".12" '
            f'stroke="{c}" stroke-opacity=".45"/>' + icon(kind, x + size / 2, y + size / 2, c))


# =========================================================================== banner
def banner():
    W, H = 1280, 460
    s = [head(W, H, "AEIDS: Autonomous Enterprise Intelligence & Decision System",
              "A company report page whose revenue row is cited by a verified finding: a growth figure computed by "
              "tested code, with its inputs and the source page.")]
    s.append('<rect x="64" y="64" width="52" height="52" rx="14" fill="url(#grad)"/>')
    s.append(icon("verify", 90, 91, "#fff", 2.4))
    s.append(t(132, 86, "AEIDS", 17, INK, "700", extra=' letter-spacing=".5"'))
    s.append(t(132, 107, "Autonomous Enterprise Intelligence & Decision System", 14, MUTED))
    s.append(t(64, 190, "Answers you can", 56, INK, "800", extra=' letter-spacing="-1.5"'))
    s.append(t(64, 252, "trace to the page.", 56, "url(#gradH)", "800", extra=' letter-spacing="-1.5"'))
    s.append(t(64, 300, "AI agents that read annual and ESG reports,", 19, SOFT))
    s.append(t(64, 326, "check each other's work and cite every claim.", 19, SOFT))
    x = 64
    for label, c in (("Every claim cited", VIOLET), ("No LLM arithmetic", CYAN), ("Verified before reported", TEAL)):
        g, cw = chip(x, 358, label, c, TINT[c], 13.5, 34, 16)
        s.append(g)
        x += cw + 12
    page, (hx, hy) = report_page(672, 66, 262, 330)
    s.append(page)
    cx, cy, cw, ch = 970, 92, 250, 286
    s.append(card(cx, cy, cw, ch))
    s.append(t(cx + 20, cy + 32, "FINDING", 11, MUTED, "700", extra=' letter-spacing="1.4"'))
    s.append(f'<g class="pop"><rect x="{cx+cw-108}" y="{cy+15}" width="92" height="25" rx="12.5" fill="{GREEN}" '
             f'fill-opacity=".15" stroke="{GREEN}" stroke-opacity=".55"/>'
             + t(cx + cw - 62, cy + 32, "✓ VERIFIED", 11, GREEN_T, "700", "middle", extra=' letter-spacing=".6"') + "</g>")
    s.append(t(cx + 20, cy + 72, "Revenue grew", 21, INK, "800"))
    s.append(t(cx + 20, cy + 99, "12.5% year on year", 21, "url(#gradH)", "800"))
    s.append(f'<line x1="{cx+20}" y1="{cy+118}" x2="{cx+cw-20}" y2="{cy+118}" stroke="{LINE}" stroke-opacity=".2"/>')
    rows = (("CALCULATION", "growth_rate(4500, 4000)", VIOLET_T), ("INPUTS", "INR · crore · FY25 vs FY24", CYAN_T),
            ("SOURCE", "p. 87 · Profit and Loss", TEAL_T))
    for i, (lab, val, c) in enumerate(rows):
        yy = cy + 144 + i * 46
        s.append(t(cx + 20, yy, lab, 10.5, MUTED, "700", extra=' letter-spacing="1.2"'))
        s.append(t(cx + 20, yy + 20, val, 12.5, c, None, cls="mono"))
    sy = cy + 144 + 2 * 46 + 15
    s.append(f'<path class="flow" d="M{hx+2} {hy} C {hx+40} {hy}, {cx-36} {sy}, {cx-4} {sy}" fill="none" '
             f'stroke="{CYAN}" stroke-width="2" marker-end="url(#arrow)"/>')
    s.append(t(1216, 430, "illustrative example", 11, MUTED, None, "end", extra=' font-style="italic"'))
    s.append("</svg>")
    return "\n".join(s)


# =========================================================================== pipeline
def pipeline():
    W, H = 1280, 560
    s = [head(W, H, "How AEIDS answers a question",
              "Seven steps: upload, parse, index (once per report), then plan, investigate, verify and report "
              "(once per question). Rejected claims go back for a limited number of rework rounds.")]
    s.append(title_block("How AEIDS answers a question",
                         "Reports are prepared once, when they are uploaded. Every question then runs through a team of agents."))
    steps = [
        ("upload", "Upload", ["PDF annual or ESG", "report, versioned,", "duplicates skipped"], CYAN),
        ("parse", "Parse", ["TableLens reads", "text, tables and", "charts, page + box"], CYAN),
        ("index", "Index", ["Chunks embedded,", "searchable by", "meaning + words"], CYAN),
        ("plan", "Plan", ["Supervisor splits", "the question into", "typed tasks"], VIOLET_T),
        ("calc", "Investigate", ["Agents quote the", "evidence; tested", "code does the maths"], VIOLET_T),
        ("verify", "Verify", ["Each claim checked", "against its quote", "and its numbers"], GREEN),
        ("report", "Report", ["Verified findings", "only, all cited,", "gaps listed"], AMBER),
    ]
    x0, cw, gap, y0, ch = 48, 150, 22, 186, 214
    la_end = x0 + 3 * cw + 2 * gap
    lb_start = x0 + 3 * (cw + gap)
    s.append(t(x0, 160, "DATA FOUNDATION · once per upload", 12, CYAN_T, "700", extra=' letter-spacing="1.3"'))
    s.append(f'<line x1="{x0}" y1="171" x2="{la_end}" y2="171" stroke="{CYAN}" stroke-opacity=".45"/>')
    s.append(t(lb_start, 160, "AGENT WORKFLOW · once per question", 12, VIOLET_T, "700", extra=' letter-spacing="1.3"'))
    s.append(f'<line x1="{lb_start}" y1="171" x2="{W-48}" y2="171" stroke="{VIOLET}" stroke-opacity=".55"/>')
    for i, (ic, name, lines, c) in enumerate(steps):
        x = x0 + i * (cw + gap)
        s.append(card(x, y0, cw, ch, c, ".35"))
        s.append(f'<circle cx="{x+26}" cy="{y0+28}" r="13" fill="url(#grad)"/>')
        s.append(t(x + 26, y0 + 33, str(i + 1), 13, "#fff", "800", "middle"))
        s.append(icon_tile(ic, x + cw - 72, y0 + 16, c))
        s.append(t(x + 16, y0 + 116, name, 17, INK, "800"))
        for k, ln in enumerate(lines):
            s.append(t(x + 16, y0 + 142 + k * 19, ln, 12.5, SOFT))
        if i < len(steps) - 1:
            ax = x + cw + 3
            s.append(f'<path class="flow" d="M{ax} {y0+ch/2} H{ax+gap-6}" stroke="{CYAN}" stroke-width="2" '
                     f'marker-end="url(#arrow)" fill="none"/>')
    vx = x0 + 5 * (cw + gap) + cw / 2
    ix = x0 + 4 * (cw + gap) + cw / 2
    by = y0 + ch + 2
    s.append(f'<path class="flow" d="M{vx} {by} C {vx} {by+62}, {ix} {by+62}, {ix} {by+8}" fill="none" '
             f'stroke="{ROSE}" stroke-width="2" marker-end="url(#arrowR)"/>')
    s.append(t((vx + ix) / 2, by + 76, "wrong number or citation → rejected → rework (limited rounds)", 12.5,
               ROSE_T, "600", "middle"))
    s.append(t((x0 + la_end) / 2, by + 40, "a new version becomes current only after its index is verified", 12.5,
               CYAN_T, "600", "middle"))
    yy = H - 52
    s.append(t(48, yy + 19, "BUILT WITH", 11, MUTED, "700", extra=' letter-spacing="1.4"'))
    x = 140
    for lab in ("Python 3.11", "FastAPI", "PostgreSQL", "Qdrant", "Redis + RQ", "TableLens", "OpenRouter · Qwen3",
                "Pydantic v2"):
        g, cwid = chip(x, yy, lab, LINE, "#cbd5e1", 12, 28, 12, False)
        s.append(g)
        x += cwid + 10
    s.append("</svg>")
    return "\n".join(s)


# =========================================================================== agents
def agents():
    W, H = 1280, 620
    s = [head(W, H, "The AEIDS agent team",
              "A question goes to the Supervisor, which sends typed tasks to the Document Intelligence, Financial "
              "Analyst and Verification agents; only verified findings reach the report. PostgreSQL stores all state.")]
    s.append(title_block("The agent team",
                         "Like an analyst team with a strict auditor. Agents exchange typed messages through the Supervisor, never directly."))

    def box(x, y, bw, bh, ic, c, label, name, lines, chip_text=None):
        o = [card(x, y, bw, bh, c, ".4"),
             f'<rect x="{x+14}" y="{y}" width="{bw-28}" height="3" rx="1.5" fill="{c}" fill-opacity=".9"/>',
             icon_tile(ic, x + 20, y + 22, c, 52),
             t(x + 88, y + 41, label, 11, TINT.get(c, c), "700", extra=' letter-spacing="1.4"'),
             t(x + 88, y + 66, name, 20, INK, "800")]
        for k, ln in enumerate(lines):
            o.append(t(x + 20, y + 102 + k * 20, ln, 13.5, SOFT))
        if chip_text:
            g, _ = chip(x + 20, y + bh - 44, chip_text, c, TINT.get(c, c), 12, 28, 12)
            o.append(g)
        return "\n".join(o)

    y1, h1 = 140, 132
    s.append(box(48, y1, 300, h1, "user", AMBER, "YOU", "A question",
                 ["“How dependent is NVIDIA on", "its largest customers?”"]))
    s.append(box(446, y1, 388, h1, "hex", VIOLET_T, "TEAM LEAD", "Supervisor",
                 ["Plans the tasks and their order. The only", "one allowed to change task state."]))
    s.append(box(932, y1, 300, h1, "report", AMBER, "REPORT WRITER", "Report",
                 ["Verified findings only, each", "cited; gaps listed as limitations."]))
    my = y1 + h1 / 2
    s.append(f'<path class="flow" d="M352 {my} H440" stroke="{CYAN}" stroke-width="2" fill="none" marker-end="url(#arrow)"/>')
    s.append(f'<path class="flow" d="M838 {my} H926" stroke="{GREEN}" stroke-width="2" fill="none" marker-end="url(#arrowG)"/>')
    s.append(t(882, my - 12, "verified", 11.5, GREEN_T, "600", "middle"))

    y2, h2, bw = 352, 196, 376
    xs = (48, 452, 856)
    specs = [
        ("index", CYAN, "RESEARCHER", "Document Intelligence",
         ["Searches the reports and returns exact", "quotes with page, section and box."], "hybrid search · evidence quotes"),
        ("calc", VIOLET, "FINANCIAL ANALYST", "Financial Analyst",
         ["Turns figures into ratios and growth.", "Maths by tested code, never the LLM."], "calc module · Decimal, never float"),
        ("verify", GREEN, "AUDITOR", "Verification",
         ["Checks every claim against its source.", "A wrong number or citation is rejected."], "rejected → bounded rework"),
    ]
    for x, (ic, c, lab, name, lines, ct) in zip(xs, specs):
        s.append(box(x, y2, bw, h2, ic, c, lab, name, lines, ct))
    sb = y1 + h1 + 2
    for x, sx in zip(xs, (560, 640, 720)):
        tx = x + bw / 2
        s.append(f'<path class="flow" d="M{sx} {sb} C {sx} {sb+44}, {tx} {y2-44}, {tx} {y2-6}" fill="none" '
                 f'stroke="{VIOLET_T}" stroke-width="2" stroke-opacity=".9" marker-end="url(#arrowV)"/>')
    pill = "typed TaskMessage ↓   AgentResult ↑"
    pw = w(pill, 12, mono=True) + 28
    s.append(f'<rect x="{640-pw/2:.0f}" y="{(sb+y2)/2-15:.0f}" width="{pw:.0f}" height="28" rx="14" fill="{BG}" '
             f'stroke="{VIOLET}" stroke-opacity=".6"/>')
    s.append(t(640, (sb + y2) / 2 + 3.5, pill, 12, VIOLET_T, "600", "middle", cls="mono"))
    by = H - 50
    for x in xs:
        s.append(f'<path d="M{x+bw/2} {y2+h2+2} V{by-6}" stroke="{LINE}" stroke-opacity=".45" stroke-dasharray="2 4"/>')
    s.append(card(48, by - 6, W - 96, 44, TEAL, ".4", shadow=False, rx=14))
    s.append(icon("db", 78, by + 18, TEAL_T, 2))
    lead = "PostgreSQL holds the shared state:"
    s.append(f'<text class="sans" x="106" y="{by+21}" font-size="13.5" fill="{SOFT}"><tspan font-size="14.5" '
             f'font-weight="700" fill="{INK}">{lead}</tspan> every task, result, quote and state change is saved, '
             f'so a workflow resumes after a crash and can be audited.</text>')
    s.append("</svg>")
    return "\n".join(s)


# =========================================================================== trust
def trust():
    W, H = 1280, 560
    s = [head(W, H, "From a claim back to the page",
              "Illustrative example: a revenue figure on page 87 feeds a stored calculation; the finding is verified "
              "against the quote and the calculation, and an injected wrong number is rejected.")]
    s.append(title_block("Every number can be traced and recomputed",
                         "Illustrative example with a made-up company. Nothing reaches the report until it matches its source."))
    top = 150
    for x, lab, c in ((48, "1 · SOURCE", CYAN_T), (448, "2 · CALCULATION", VIOLET_T), (872, "3 · VERIFIED FINDING", GREEN_T)):
        s.append(t(x, top, lab, 12, c, "700", extra=' letter-spacing="1.4"'))
    page, (hx, hy) = report_page(48, top + 16, 340, 300)
    s.append(page)
    cx, cy, cw, ch = 448, top + 16, 372, 300
    s.append(card(cx, cy, cw, ch, VIOLET, ".45"))
    s.append(t(cx + 22, cy + 40, "growth_rate", 19, VIOLET_T, "700", cls="mono"))
    s.append(t(cx + cw - 22, cy + 40, "calc v1.0", 11.5, MUTED, None, "end", cls="mono"))
    for i, (lab, val, per) in enumerate((("current", "4500", "FY2025"), ("previous", "4000", "FY2024"))):
        ry = cy + 64 + i * 46
        s.append(f'<rect x="{cx+18}" y="{ry}" width="{cw-36}" height="36" rx="10" fill="#111a2e" stroke="{LINE}" stroke-opacity=".18"/>')
        s.append(t(cx + 32, ry + 23, lab, 12, MUTED, None, cls="mono"))
        s.append(t(cx + 112, ry + 23, val, 14, INK, "700", cls="mono"))
        x = cx + 164
        for tag, c in (("INR", CYAN), ("crore", TEAL), (per, VIOLET)):
            g, tw = chip(x, ry + 7, tag, c, TINT[c], 11, 22, 8)
            s.append(g)
            x += tw + 6
    s.append(t(cx + 22, cy + 186, "(4500 − 4000) / 4000 × 100", 15, SOFT, None, cls="mono"))
    s.append(t(cx + 22, cy + 232, "= 12.5 %", 34, "url(#gradH)", "800"))
    s.append(t(cx + 22, cy + 266, "Function, inputs and version are stored,", 12.5, MUTED))
    s.append(t(cx + 22, cy + 284, "so anyone can recompute the number.", 12.5, MUTED))
    fx, fy, fw, fh = 872, top + 16, 360, 300
    s.append(card(fx, fy, fw, fh, GREEN, ".45"))
    s.append(t(fx + 22, fy + 44, "Revenue grew 12.5%", 22, INK, "800"))
    s.append(t(fx + 22, fy + 68, "FY2024 → FY2025 · Acme Ltd", 13, MUTED))
    for i, ck in enumerate(("Quote found on page 87", "Value equals the calculation", "Units, scale and period agree",
                            "Current version of the report")):
        yy = fy + 110 + i * 36
        s.append(f'<circle cx="{fx+33}" cy="{yy-5}" r="11" fill="{GREEN}" fill-opacity=".16" stroke="{GREEN}" stroke-opacity=".6"/>')
        s.append(f'<path d="M{fx+28} {yy-5}l3.5 3.5 6.5-7" fill="none" stroke="{GREEN_T}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>')
        s.append(t(fx + 54, yy, ck, 14, INK))
    s.append(f'<g class="pop"><rect x="{fx+22}" y="{fy+fh-48}" width="118" height="30" rx="15" fill="{GREEN}" '
             f'fill-opacity=".15" stroke="{GREEN}" stroke-opacity=".6"/>'
             + t(fx + 81, fy + fh - 28.5, "✓ VERIFIED", 12.5, GREEN_T, "700", "middle", extra=' letter-spacing=".6"') + "</g>")
    s.append(f'<path class="flow" d="M{hx+2} {hy} C {hx+30} {hy}, {cx-36} {cy+120}, {cx-6} {cy+120}" fill="none" '
             f'stroke="{CYAN}" stroke-width="2" marker-end="url(#arrow)"/>')
    s.append(f'<path class="flow" d="M{cx+cw+4} {cy+222} C {cx+cw+30} {cy+222}, {fx-30} {fy+40}, {fx-6} {fy+40}" '
             f'fill="none" stroke="{VIOLET_T}" stroke-width="2" marker-end="url(#arrowV)"/>')
    ry = H - 66
    s.append(f'<rect x="48" y="{ry}" width="{W-96}" height="40" rx="12" fill="{ROSE}" fill-opacity=".1" stroke="{ROSE}" stroke-opacity=".5"/>')
    s.append(t(70, ry + 25.5, "✗  INJECTED TEST", 12, ROSE_T, "800", extra=' letter-spacing="1.2"'))
    s.append(t(216, ry + 25.5, "A finding that claims 13.5% does not equal its calculation (12.5%), so it is rejected and a rework task is created.",
               13.5, "#fecdd3"))
    s.append("</svg>")
    return "\n".join(s)


# =========================================================================== benchmark
def benchmark():
    W, H = 1280, 470
    s = [head(W, H, "Retrieval benchmark",
              "Share of 31 questions whose correct page is in the top 5 and top 10 results: exact words 55% and 81%, "
              "meaning 61% and 81%, hybrid 77% and 90%.")]
    s.append(title_block("Does search find the right page?",
                         "31 questions with known answer pages, over the annual reports of Apple, Microsoft, NVIDIA, Tesla and Alphabet."))
    gx, gy, gw, gh = 96, 168, 700, 210
    for v in (0, 25, 50, 75, 100):
        yy = gy + gh - gh * v / 100
        s.append(f'<line x1="{gx}" y1="{yy}" x2="{gx+gw}" y2="{yy}" stroke="{LINE}" stroke-opacity="{.35 if v == 0 else .12}"/>')
        s.append(t(gx - 12, yy + 4, f"{v}%", 11.5, MUTED, None, "end"))
    data = (("Exact words", "keyword · PostgreSQL", 55, 81), ("Meaning", "dense · Qdrant", 61, 81),
            ("Hybrid", "both, fused with RRF", 77, 90))
    gwid, bw = gw / 3, 58
    for i, (name, sub, r5, r10) in enumerate(data):
        cxg = gx + gwid * i + gwid / 2
        for j, (v, fill, tc) in enumerate(((r5, "url(#gradV)", VIOLET_T), (r10, "url(#gradC)", CYAN_T))):
            bx = cxg - bw - 5 + j * (bw + 10)
            bh = gh * v / 100
            op = "1" if i == 2 else ".7"
            s.append(f'<rect x="{bx}" y="{gy+gh-bh}" width="{bw}" height="{bh}" rx="8" fill="{fill}" fill-opacity="{op}"/>')
            s.append(t(bx + bw / 2, gy + gh - bh - 9, f"{v}%", 14, tc, "800", "middle"))
        if i == 2:
            s.append(f'<rect class="pulse" x="{cxg-bw-17}" y="{gy-12}" width="{2*bw+34}" height="{gh+12}" rx="12" '
                     f'fill="none" stroke="{VIOLET}" stroke-opacity=".55" stroke-dasharray="4 4"/>')
        s.append(t(cxg, gy + gh + 26, name, 15, INK if i == 2 else SOFT, "800", "middle"))
        s.append(t(cxg, gy + gh + 45, sub, 11.5, MUTED, None, "middle"))
    s.append(f'<rect x="{gx}" y="140" width="14" height="14" rx="4" fill="url(#gradV)"/>')
    s.append(t(gx + 22, 152, "right page in the top 5", 12.5, SOFT))
    s.append(f'<rect x="{gx+190}" y="140" width="14" height="14" rx="4" fill="url(#gradC)"/>')
    s.append(t(gx + 212, 152, "right page in the top 10", 12.5, SOFT))
    px, py, pw, ph = 852, 140, 380, 290
    s.append(card(px, py, pw, ph, VIOLET, ".4"))
    s.append(t(px + 28, py + 40, "HYBRID SEARCH, THE DEFAULT", 11, VIOLET_T, "700", extra=' letter-spacing="1.4"'))
    s.append(t(px + 28, py + 106, "77%", 60, "url(#gradH)", "800", extra=' letter-spacing="-2"'))
    s.append(t(px + 168, py + 84, "of questions: the right", 14, SOFT))
    s.append(t(px + 168, py + 104, "page is in the top 5", 14, SOFT))
    s.append(t(px + 28, py + 184, "90%", 60, "url(#gradH)", "800", extra=' letter-spacing="-2"'))
    s.append(t(px + 168, py + 162, "of questions: the right", 14, SOFT))
    s.append(t(px + 168, py + 182, "page is in the top 10", 14, SOFT))
    s.append(f'<line x1="{px+28}" y1="{py+214}" x2="{px+pw-28}" y2="{py+214}" stroke="{LINE}" stroke-opacity=".2"/>')
    s.append(t(px + 28, py + 242, "Repeatable: a pinned embedding provider and", 12.5, MUTED))
    s.append(t(px + 28, py + 261, "cached question vectors give identical ranks.", 12.5, MUTED))
    s.append("</svg>")
    return "\n".join(s)


for name, fn in (("banner", banner), ("pipeline", pipeline), ("agents", agents), ("trust", trust),
                 ("benchmark", benchmark)):
    (OUT / f"{name}.svg").write_text(fn(), encoding="utf-8")
    print("wrote", name)

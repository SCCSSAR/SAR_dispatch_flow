#!/usr/bin/env python3
"""Regenerate the README flow diagram: python3 docs/assets/make-diagram.py (from repo root).

Both theme variants come from ONE geometry definition so they cannot drift apart. Kept as a
generator rather than two hand-edited SVGs for that reason. No third-party imports.

The grouping mirrors the actual buttons in frontend/index.html and must stay honest:
  #incident-map-btn      "Create Incident Map"       -> CalTopo               (click 1)
  #everbridge-slack-btn  "Everbridge + Slack + D4H"  -> those three together  (click 2)
  #gdoc-btn              "Google Doc"                -> optional helper
If a button is split, merged or renamed, this diagram is wrong until it is regenerated.

Two rendering constraints worth knowing before editing:
  * No <style> blocks. GitHub sanitises SVGs served from a repo and strips them, which is why
    the theme switch is a <picture> with two files rather than prefers-color-scheme inside one.
  * No <tspan> inside a <text> that is middle- or end-anchored. Renderers measure the run
    before the tspan and the tail lands in the wrong place; start-anchored is fine.

Palette and card grammar inherit from the team's "Dispatching How-To" SOP deck (rev 2026-01-27,
slide 2), so this reads as its successor. #F1592A is the shared house orange -- it is also the
logo orange. Slack keeps WhatsApp's purple: same slot in the flow after the 2026 migration.
"""
import html, os

THEMES = {
    "light": dict(
        page="#FFFFFF", panel="#DFF2F9", card="#FFFFFF", stroke="#CBD5D0", group="#EDF1EF",
        ink="#22312A", muted="#5F6E64", rail="#9AA9A1", bar="#F1592A", bar_ink="#FFFFFF",
        turbo="#283F2E", foot="#F5F7F6",
        caltopo="#5F8049", everbridge="#DD5920", slack="#7B3C8D", d4h="#3A7C9E", gdoc="#6B7869"),
    "dark": dict(
        page="#0D1117", panel="#122029", card="#161D19", stroke="#2E3B33", group="#131A16",
        ink="#E7EDE9", muted="#9CAAA1", rail="#4A5A52", bar="#F1592A", bar_ink="#FFFFFF",
        turbo="#1F3A2A", foot="#141B17",
        caltopo="#7FA766", everbridge="#E8703A", slack="#A15FB4", d4h="#4F9CC2", gdoc="#8B9A88"),
}

SPAN = "Form to fully dispatched: < 5 minutes"   # spans the WHOLE flow -- it sits in the
# title bar, NOT inside the Dispatch Turbo card. Inside the card it scoped to that box and
# read as "5 minutes here, and THEN you still have to click" (Bill, 2026-09-07).
TURBO_ROLE = "Automates the dispatch flow"       # parallel with every other card's role text

TURBO = ["Gemini extracts every field: MP Subject Data, Planning Data, Urgency Fields",
         "LKP geocoded; real staging ranked",
         "Dispatcher reviews and corrects (~30 s)"]

GROUPS = [
    dict(label="CLICK 1", action="Create Incident Map", optional=False, cards=[
        ("CALTOPO", "Tactical Mapping", "caltopo",
         ["Create private map, seeded with MP subject information",
          "ICP marker and staging recommendations"])]),
    dict(label="CLICK 2", action="Everbridge + Slack + D4H", optional=False, cards=[
        ("EVERBRIDGE", "Notification", "everbridge",
         ["Page the members and specialty teams",
          "Poll for responses"]),
        ("SLACK", "Team Coordination", "slack",
         ["Create private incident channel for responders, live tally for search management",
          "Pinned info: Incident and MP info, CalTopo map, Apple/Google maps links"]),
        ("D4H", "Incident Documentation", "d4h",
         ["Create incident record",
          "Automatically sync with attendees, specialty teams, MP info, map"])]),
    dict(label="OPTIONAL", action="Google Doc", optional=True, cards=[
        ("GOOGLE DOC", "Working Document", "gdoc",
         ["Create pre-filled working document",
          "Share with every dispatcher — for teams who work the incident in Docs"])]),
]

W, H = 1240, 780
BAR_H, FOOT_H = 48, 40
PANEL_W = 246
TB_X, TB_W, TURBO_BAND = 292, 286, 56
TURBO_CHARS = 30
SPINE_X = 620
GX, GW = 660, 560
CX, CW, BAND_H = 676, 528, 30
BULLET_CHARS = 72   # ~461px at 12.5px — fits CW with padding
LABEL_H, PAD, GAP = 26, 10, 8

def esc(s): return html.escape(s, quote=False)

def top_rounded(x, y, w, h, r):
    """Band with rounded TOP corners only — avoids a clipPath (GitHub sanitises SVG)."""
    return f"M{x} {y+r} A{r} {r} 0 0 1 {x+r} {y} H{x+w-r} A{r} {r} 0 0 1 {x+w} {y+r} V{y+h} H{x} Z"

def card_height(bullets):
    """Cards size to their content — a long bullet wraps rather than overflowing the card."""
    lines = sum(len(wrap(b, BULLET_CHARS)) for b in bullets)
    return BAND_H + 16 + lines * 18 + 6 * (len(bullets) - 1) + 8


def wrap(text, n):
    out, line = [], ""
    for word in text.split():
        if len(line + " " + word) > n: out.append(line); line = word
        else: line = (line + " " + word).strip()
    out.append(line)
    return out

def build(t):
    o = []; a = o.append
    # Layout is derived, not hard-coded: the Turbo card sizes to its own bullets and centres
    # on the fan of groups, so changing its text cannot silently overflow or misalign it.
    fan_top, group_gap = 70, 18
    gh_all = [LABEL_H + sum(card_height(c[3]) for c in g["cards"])
              + (len(g["cards"]) - 1) * GAP + PAD for g in GROUPS]
    fan_bottom = fan_top + sum(gh_all) + group_gap * (len(gh_all) - 1)
    turbo_lines = sum(len(wrap(b, TURBO_CHARS)) for b in TURBO)
    TB_H = TURBO_BAND + 28 + turbo_lines * 17 + 12 * (len(TURBO) - 1) + 6
    TB_Y = (fan_top + fan_bottom) / 2 - TB_H / 2
    a(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
      f'role="img" aria-labelledby="ti de" font-family="-apple-system, BlinkMacSystemFont, '
      f'&quot;Segoe UI&quot;, Helvetica, Arial, sans-serif">')
    a('<title id="ti">Dispatch Turbo: initial response timeline</title>')
    a('<desc id="de">The call-out form arrives as a photo or fillable PDF. Dispatch Turbo extracts '
      'every field with Gemini, geocodes the last known position, ranks real staging locations, and '
      'the dispatcher reviews and corrects it in about thirty seconds. Two clicks then dispatch that '
      'same reviewed data: the first creates the CalTopo incident map, the second notifies Everbridge, '
      'opens the Slack incident channel and creates the D4H record together. An optional further click '
      'creates a pre-filled Google Doc for teams who work the incident in Docs. The manual process '
      'entered the same data into each of those systems in turn.</desc>')
    a(f'<rect width="{W}" height="{H}" fill="{t["page"]}"/>')

    a(f'<rect x="0" y="0" width="{W}" height="{BAR_H}" fill="{t["bar"]}"/>')
    a(f'<text x="24" y="32" font-size="21" font-weight="700" fill="{t["bar_ink"]}">'
      f'Dispatch Turbo: Initial Response Timeline</text>')
    a(f'<text x="{W-24}" y="{32}" text-anchor="end" font-size="14.5" font-weight="600" '
      f'fill="{t["bar_ink"]}">{esc(SPAN)}</text>')

    a(f'<rect x="0" y="{BAR_H}" width="{PANEL_W}" height="{H-BAR_H-FOOT_H}" fill="{t["panel"]}"/>')
    cy = 422
    a(f'<circle cx="{PANEL_W/2}" cy="{cy-74}" r="38" fill="none" stroke="{t["ink"]}" '
      f'stroke-width="2.5"/>')
    a(f'<text x="{PANEL_W/2}" y="{cy-66}" text-anchor="middle" font-size="26" font-weight="700" '
      f'fill="{t["ink"]}">T+0</text>')
    a(f'<text x="{PANEL_W/2}" y="{cy+2}" text-anchor="middle" font-size="16" font-weight="700" '
      f'fill="{t["ink"]}">FORM ARRIVES</text>')
    for i, ln in enumerate(["Photo of the paper", "call-out form, or the", "fillable PDF"]):
        a(f'<text x="{PANEL_W/2}" y="{cy+28+i*19}" text-anchor="middle" font-size="13" '
          f'fill="{t["muted"]}">{esc(ln)}</text>')

    a(f'<path d="M{PANEL_W+12} {TB_Y+TB_H/2} H{TB_X-14}" stroke="{t["rail"]}" stroke-width="3" '
      f'stroke-linecap="round"/>')
    a(f'<path d="M{TB_X-21} {TB_Y+TB_H/2-7} l8 7 -8 7" fill="none" stroke="{t["rail"]}" '
      f'stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>')

    a(f'<rect x="{TB_X}" y="{TB_Y}" width="{TB_W}" height="{TB_H}" rx="10" fill="{t["card"]}" '
      f'stroke="{t["stroke"]}" stroke-width="1.5"/>')
    a(f'<path d="{top_rounded(TB_X, TB_Y, TB_W, TURBO_BAND, 10)}" fill="{t["turbo"]}"/>')
    a(f'<text x="{TB_X+TB_W/2}" y="{TB_Y+26}" text-anchor="middle" font-size="17" '
      f'font-weight="700" fill="#FFFFFF">DISPATCH TURBO</text>')
    a(f'<text x="{TB_X+TB_W/2}" y="{TB_Y+45}" text-anchor="middle" font-size="12.5" '
      f'fill="#FFFFFF" opacity="0.85">{esc(TURBO_ROLE)}</text>')
    by = TB_Y + TURBO_BAND + 28
    for b in TURBO:
        a(f'<circle cx="{TB_X+22}" cy="{by-5}" r="3.5" fill="{t["bar"]}"/>')
        lines = wrap(b, TURBO_CHARS)
        for j, ln in enumerate(lines):
            a(f'<text x="{TB_X+36}" y="{by+j*17}" font-size="13.5" fill="{t["ink"]}">{esc(ln)}</text>')
        by += len(lines) * 17 + 12

    y = 70
    centres = []
    for g in GROUPS:
        heights = [card_height(c[3]) for c in g["cards"]]
        gh = LABEL_H + sum(heights) + (len(heights) - 1) * GAP + PAD
        dash = ' stroke-dasharray="6 5"' if g["optional"] else ""
        a(f'<rect x="{GX}" y="{y}" width="{GW}" height="{gh}" rx="12" fill="{t["group"]}" '
          f'stroke="{t["stroke"]}" stroke-width="1.5"{dash}/>')
        a(f'<text x="{GX+16}" y="{y+18}" font-size="11.5" font-weight="700" '
          f'fill="{t["muted"] if g["optional"] else t["bar"]}" letter-spacing="0.6">'
          f'{esc(g["label"])}</text>')
        a(f'<text x="{GX+16+(72 if g["optional"] else 58)}" y="{y+18}" font-size="11.5" '
          f'fill="{t["muted"]}">· {esc(g["action"])}</text>')
        cyy = y + LABEL_H
        for (name, role, ckey, bullets), chh in zip(g["cards"], heights):
            col = t[ckey]
            a(f'<rect x="{CX}" y="{cyy}" width="{CW}" height="{chh}" rx="9" fill="{t["card"]}" '
              f'stroke="{t["stroke"]}" stroke-width="1.5"/>')
            a(f'<path d="{top_rounded(CX, cyy, CW, BAND_H, 9)}" fill="{col}"/>')
            a(f'<text x="{CX+14}" y="{cyy+20}" font-size="14" font-weight="700" fill="#FFFFFF" '
              f'letter-spacing="0.4">{esc(name)}</text>')
            a(f'<text x="{CX+CW-14}" y="{cyy+20}" text-anchor="end" font-size="12.5" '
              f'fill="#FFFFFF" opacity="0.9">{esc(role)}</text>')
            byy = cyy + BAND_H + 22
            for b in bullets:
                a(f'<circle cx="{CX+18}" cy="{byy-4}" r="3" fill="{col}"/>')
                for j, ln in enumerate(wrap(b, BULLET_CHARS)):
                    a(f'<text x="{CX+32}" y="{byy+j*18}" font-size="12.5" fill="{t["ink"]}">'
                      f'{esc(ln)}</text>')
                byy += len(wrap(b, BULLET_CHARS)) * 18 + 6
            cyy += chh + GAP
        centres.append(y + gh / 2)
        y += gh + 18

    a(f'<path d="M{TB_X+TB_W+8} {TB_Y+TB_H/2} H{SPINE_X}" stroke="{t["rail"]}" stroke-width="3" '
      f'stroke-linecap="round"/>')
    a(f'<path d="M{SPINE_X} {centres[0]} V{centres[-1]}" stroke="{t["rail"]}" stroke-width="3" '
      f'stroke-linecap="round"/>')
    for c, g in zip(centres, GROUPS):
        d = ' stroke-dasharray="6 5"' if g["optional"] else ""
        a(f'<path d="M{SPINE_X} {c} H{GX-10}" stroke="{t["rail"]}" stroke-width="2.5" '
          f'stroke-linecap="round"{d}/>')
        a(f'<path d="M{GX-17} {c-7} l8 7 -8 7" fill="none" stroke="{t["rail"]}" '
          f'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>')

    fy = H - FOOT_H
    a(f'<rect x="0" y="{fy}" width="{W}" height="{FOOT_H}" fill="{t["foot"]}"/>')
    a(f'<text x="24" y="{fy+25}" font-size="13" fill="{t["muted"]}">The same systems as the manual '
      f'SOP — but the dispatcher enters the data <tspan font-weight="700" fill="{t["ink"]}">once, '
      f'not once per system</tspan>.</text>')
    a('</svg>')
    return "\n".join(o) + "\n"

os.makedirs("docs/assets", exist_ok=True)
for name, t in THEMES.items():
    p = f"docs/assets/dispatch-flow-{name}.svg"
    open(p, "w", encoding="utf-8").write(build(t))
    print(p, os.path.getsize(p), "bytes")

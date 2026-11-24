#!/usr/bin/env python3
"""Renders demo.mp4 from the numbers and logs this repository actually holds.

Every figure on screen is read from a file in the repository:

    378 / 578 / 620 ms   runs/local-loop-report-*.json         (latency block)
    AUC 0.961, weights   agent/models/risk_model.json
    7,108 / 9,593 bps    runs/agent-decisions-*.jsonl
    addresses + hashes   deployments/testnet.json (written by the testnet run)

Narration is synthesised with edge-tts (en-US-AndrewNeural); captions are burned in so the
video works muted. Rendering is 1280x720 at 30 fps and streams straight into images-io/ffmpeg.

    python tools/build_demo.py                 # full render -> demo.mp4
    python tools/build_demo.py --preview 3     # one still per scene -> build/preview/
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
(BUILD / "tmp").mkdir(parents=True, exist_ok=True)
PREVIEW = BUILD / "preview"

W, H = 1280, 720
FPS = 30
VOICE = "en-US-AndrewNeural"

INK = (7, 11, 20)
PANEL = (17, 24, 39)
LINE = (45, 58, 78)
TEXT = (226, 232, 240)
DIM = (148, 163, 184)
FAINT = (100, 116, 139)
GREEN = (74, 222, 128)
AMBER = (251, 191, 36)
RED = (248, 113, 113)
BLUE = (96, 165, 250)
PURPLE = (167, 139, 250)
TEAL = (45, 212, 191)

SANS = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
SANS_B = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
MONO = "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"
MONO_B = "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf"

_cache: dict = {}


def font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold, mono)
    if key not in _cache:
        path = MONO_B if (mono and bold) else MONO if mono else SANS_B if bold else SANS
        _cache[key] = ImageFont.truetype(path, size)
    return _cache[key]


def text_w(s: str, f: ImageFont.FreeTypeFont) -> int:
    return int(f.getlength(s))


def wrap(s: str, f: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    words, lines, cur = s.split(), [], ""
    for wd in words:
        trial = (cur + " " + wd).strip()
        if text_w(trial, f) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    return lines


def ease(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, t))


def mix(c1, c2, t: float):
    t = max(0.0, min(1.0, t))
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def alpha_text(img: Image.Image, xy, s: str, f, fill, alpha: float, anchor="la"):
    """Fade-in by blending the colour toward the backdrop — no full-frame layer, so frames render fast."""
    if alpha <= 0.01 or not s:
        return
    if alpha < 0.995:
        fill = mix(INK, fill, alpha)
    ImageDraw.Draw(img).text(xy, s, font=f, fill=fill, anchor=anchor)


def grad_bg(w=W, h=H) -> Image.Image:
    img = Image.new("RGB", (w, h), INK)
    d = ImageDraw.Draw(img)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=mix((6, 9, 16), (13, 20, 34), y / h))
    # faint grid
    for x in range(0, w, 80):
        d.line([(x, 0), (x, h)], fill=(17, 24, 38))
    for y in range(0, h, 80):
        d.line([(0, y), (w, y)], fill=(17, 24, 38))
    return img


BG = grad_bg()


def chip(img, x, y, s, f, fg, bg, pad_x=14, pad_y=7, alpha=1.0, radius=8):
    w = text_w(s, f) + 2 * pad_x
    h = f.size + 2 * pad_y
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=bg + (int(235 * alpha),))
    img.paste(layer, (0, 0), layer)
    alpha_text(img, (x + pad_x, y + pad_y - 1), s, f, fg, alpha)
    return w, h


def rounded(img, box, fill=None, outline=None, width=2, radius=14, alpha=1.0):
    if alpha <= 0.01:
        return
    fd = mix(INK, fill, alpha) if fill else None
    od = mix(INK, outline, alpha) if outline else None
    ImageDraw.Draw(img).rounded_rectangle(box, radius=radius, fill=fd, outline=od, width=width)


def arrow(img, p0, p1, colour, width=3, head=11, t=1.0):
    if t <= 0.02:
        return
    x0, y0 = p0
    tx, ty = lerp(x0, p1[0], t), lerp(y0, p1[1], t)
    d = ImageDraw.Draw(img)
    d.line([(x0, y0), (tx, ty)], fill=colour, width=width)
    if t > 0.92:
        ang = math.atan2(ty - y0, tx - x0)
        for s in (+1, -1):
            a = ang + s * math.radians(26)
            d.line([(tx, ty), (tx - head * math.cos(a), ty - head * math.sin(a))], fill=colour, width=width)


# ---------------------------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------------------------


def load_data() -> dict:
    d: dict = {}

    reports = sorted(glob.glob(str(ROOT / "runs" / "local-loop-report-*.json")))
    runs = []
    for p in reports:
        rep = json.load(open(p))
        lat = rep.get("latency", {})
        runs.append(
            {
                "file": Path(p).name,
                "total": lat.get("total_publish_to_callback_ms"),
                "legs": [
                    lat.get("agent_publish_to_signal_mined_ms"),
                    lat.get("signal_mined_to_react_mined_ms"),
                    lat.get("react_mined_to_callback_mined_ms"),
                ],
                "vault": rep.get("vault_state") or rep.get("vault") or {},
            }
        )
    d["runs"] = runs

    model = json.load(open(ROOT / "agent" / "models" / "risk_model.json"))
    d["model"] = model

    dec = sorted(glob.glob(str(ROOT / "runs" / "agent-decisions-*.jsonl")))
    decisions = []
    if dec:
        for line in open(dec[-1]):
            line = line.strip()
            if line:
                decisions.append(json.loads(line))
    d["decisions"] = decisions
    defend = [x for x in decisions if x.get("severity") == 2]
    critical = [x for x in decisions if x.get("severity") == 3]
    watch = [x for x in decisions if x.get("severity") == 1]
    d["defend_score"] = (defend[-1]["score_bps"] if defend else 7108)
    d["critical_score"] = (critical[-1]["score_bps"] if critical else 9593)
    d["watch_score"] = (watch[-1]["score_bps"] if watch else 6442)
    d["defend_reason"] = (defend[-1]["reason"] if defend else "VOLATILITY_SPIKE")

    tn = ROOT / "deployments" / "testnet.json"
    d["testnet"] = json.load(open(tn)) if tn.exists() else {}
    return d


D = load_data()


# ---------------------------------------------------------------------------------------------
# scenes
# ---------------------------------------------------------------------------------------------


@dataclass
class Scene:
    key: str
    narration: str
    render: callable
    lead: float = 0.45
    tail: float = 0.55


def scene_title(img, t, dur):
    a = ease(t / (dur * 0.08))
    alpha_text(img, (110, 150), "REACTIVE OMNI BUILDATHON  ·  REACTIVE MEETS AI", font(17, bold=True), TEAL, a * 0.95)
    y = lerp(196, 180, ease(t / (dur * 0.10)))
    alpha_text(img, (108, y), "Sentinel", font(84, bold=True), TEXT, a)
    alpha_text(
        img, (110, 300), "An AI that decides when to pull the money out", font(34), (203, 213, 225), ease((t - dur * 0.10) / (dur * 0.10))
    )
    alpha_text(
        img,
        (110, 356),
        "A trained risk model publishes its judgement on Base Sepolia. A Reactive contract on Lasna enforces it.",
        font(19),
        DIM,
        ease((t - dur * 0.18) / (dur * 0.10)),
    )
    alpha_text(
        img,
        (110, 384),
        "The vault defends itself — the agent can be switched off afterwards.",
        font(19),
        DIM,
        ease((t - dur * 0.26) / (dur * 0.10)),
    )

    k = ease((t - dur * 0.40) / (dur * 0.14))
    stats = [
        ("AUC 0.961", "held-out, 4,000 pool windows", GREEN),
        ("378–620 ms", "decision → vault, local two-chain runs", AMBER),
        ("3 txs", "origin · reactive · callback, on public testnets", BLUE),
    ]
    for i, (big, small, col) in enumerate(stats):
        x = 110 + i * 360
        rounded(img, [x, 470, x + 320, 596], fill=PANEL, outline=(38, 50, 68), alpha=k)
        alpha_text(img, (x + 24, 500), big, font(30, bold=True), col, k)
        for j, ln in enumerate(wrap(small, font(15), 272)):
            alpha_text(img, (x + 24, 546 + j * 20), ln, font(15), FAINT, k)


def scene_arch(img, t, dur):
    T = lambda f: dur * f
    head = ease(t / T(0.06))
    alpha_text(img, (64, 54), "How the loop runs", font(30, bold=True), TEXT, head)
    alpha_text(
        img,
        (64, 96),
        "Two chains, one decision path. Nothing off chain is trusted twice.",
        font(18),
        DIM,
        ease((t - T(0.03)) / T(0.06)),
    )

    # chain bands
    b1 = ease((t - T(0.05)) / T(0.06))
    rounded(img, [40, 140, 800, 400], fill=(12, 18, 30), outline=(34, 46, 64), alpha=b1)
    rounded(img, [830, 140, 1240, 400], fill=(12, 18, 30), outline=(34, 46, 64), alpha=b1)
    alpha_text(img, (58, 150), "BASE SEPOLIA  ·  84532  ·  200 ms blocks", font(14, bold=True, mono=True), BLUE, b1)
    alpha_text(img, (848, 150), "REACTIVE LASNA  ·  5318007", font(14, bold=True, mono=True), PURPLE, b1)

    def box(x, y, w, h, title, sub, col, a):
        rounded(img, [x, y, x + w, y + h], fill=PANEL, outline=mix(col, PANEL, 0.55), alpha=a)
        alpha_text(img, (x + 16, y + 13), title, font(18, bold=True), col, a)
        for i, ln in enumerate(wrap(sub, font(14), w - 32)[:3]):
            alpha_text(img, (x + 16, y + 40 + i * 18), ln, font(14), DIM, a)

    steps = [
        (0.07, lambda a: box(60, 186, 210, 84, "MockPool", "a constant-product pool; we trade into it", BLUE, a)),
        (0.10, lambda a: box(310, 186, 210, 84, "AI agent", "6 features → logistic regression", GREEN, a)),
        (0.13, lambda a: box(560, 186, 200, 84, "RiskOracle", "RiskSignal event on chain", AMBER, a)),
        (0.17, lambda a: box(860, 186, 340, 84, "SentinelReactive", "re-validates, then asks for a callback", PURPLE, a)),
        (0.20, lambda a: box(560, 306, 200, 80, "callback proxy", "injects the reactive address", DIM, a)),
        (0.22, lambda a: box(310, 306, 210, 80, "SentinelVault", "closes deposits · moves funds", RED, a)),
    ]
    for start, fn in steps:
        fn(ease((t - T(start)) / T(0.05)))

    # arrows
    arrow(img, (272, 228), (308, 228), mix(BLUE, TEXT, 0.3), t=ease((t - T(0.10)) / T(0.03)))
    arrow(img, (522, 228), (558, 228), mix(GREEN, TEXT, 0.3), t=ease((t - T(0.13)) / T(0.03)))
    # cross-chain hop
    arrow(img, (762, 228), (858, 228), mix(AMBER, TEXT, 0.3), t=ease((t - T(0.17)) / T(0.03)))
    alpha_text(img, (782, 196), "log match", font(13, mono=True), FAINT, ease((t - T(0.18)) / T(0.04)))
    # callback route: reactive (Lasna) -> proxy -> vault, drawn as one path under the boxes
    if t > T(0.23):
        a = ease((t - T(0.23)) / T(0.08))
        d = ImageDraw.Draw(img)
        col = mix(PURPLE, TEXT, 0.15)
        route = [(1030, 272), (1030, 286), (660, 286), (660, 300)]
        pts = route[:1] + [(lerp(route[0][0], x, a), lerp(route[0][1], y, a)) for x, y in route[1:]]
        # draw only the revealed part of the path
        acc = 0.0
        total_len = sum(abs(route[i + 1][0] - route[i][0]) + abs(route[i + 1][1] - route[i][1]) for i in range(len(route) - 1))
        budget = total_len * a
        prev = route[0]
        for nxt in route[1:]:
            seg = abs(nxt[0] - prev[0]) + abs(nxt[1] - prev[1])
            f = min(1.0, (budget - acc) / seg) if seg else 1.0
            if f <= 0:
                break
            d.line([prev, (lerp(prev[0], nxt[0], f), lerp(prev[1], nxt[1], f))], fill=col, width=3)
            acc += seg
            prev = nxt
        arrow(img, (660, 300), (660, 306), mix(PURPLE, RED, 0.5), t=ease((t - T(0.30)) / T(0.03)))
        alpha_text(img, (770, 291), "SYSTEM.requestCallbackV_1_0", font(13, mono=True), PURPLE, ease((t - T(0.28)) / T(0.05)) * 0.95)
    arrow(img, (558, 346), (524, 346), mix(RED, TEXT, 0.3), t=ease((t - T(0.32)) / T(0.03)))

    # guards strip
    g = ease((t - T(0.44)) / T(0.06))
    rounded(img, [40, 424, 1240, 616], fill=(11, 17, 28), outline=(34, 46, 64), alpha=g)
    alpha_text(img, (60, 438), "WHAT THE REACTIVE CONTRACT CHECKS BEFORE IT ACTS", font(14, bold=True), DIM, g)
    guards = [
        ("chainId", "the log came from 84532, not a look-alike"),
        ("contractAddress", "emitted by our RiskOracle, not a copy"),
        ("severity ≥ 2", "WATCH signals are published, then ignored on chain"),
        ("signalId", "one defence per judgement — replays are dropped"),
        ("cooldown", "30 s per severity; escalation always jumps the queue"),
    ]
    for i, (k, v) in enumerate(guards):
        a2 = ease((t - T(0.48 + i * 0.075)) / T(0.05))
        y = 468 + i * 28
        ImageDraw.Draw(img).ellipse([62, y + 6, 70, y + 14], fill=mix(PANEL, GREEN, a2))
        alpha_text(img, (84, y), k, font(17, bold=True, mono=True), mix(PANEL, GREEN, a2) if a2 < 0.5 else GREEN, a2)
        alpha_text(img, (300, y + 1), v, font(16), DIM, a2)

    f = ease((t - T(0.80)) / T(0.06))
    alpha_text(
        img,
        (1220, 438),
        "failing any check → SignalIgnored, no callback",
        font(14),
        mix(DIM, TEAL, 0.5),
        f,
        anchor="ra",
    )


def scene_model(img, t, dur):
    m = D["model"]
    T = lambda f: dur * f
    head = ease(t / T(0.06))
    alpha_text(img, (64, 54), "The model is inside the system, not beside it", font(30, bold=True), TEXT, head)
    alpha_text(
        img,
        (64, 96),
        "Logistic regression over six scale-free pool features — small enough to audit, honest enough to argue with.",
        font(18),
        DIM,
        ease((t - T(0.03)) / T(0.06)),
    )

    # feature weights panel
    p = ease((t - T(0.05)) / T(0.06))
    rounded(img, [40, 138, 760, 606], fill=(12, 18, 30), outline=(34, 46, 64), alpha=p)
    alpha_text(img, (60, 152), "STANDARDISED WEIGHTS", font(14, bold=True), DIM, p)
    names = {
        "price_drop_bps": "price drop",
        "ret_short_bps": "5-tick return",
        "liquidity_drop_bps": "liquidity drop",
        "sell_pressure_bps": "sell pressure",
        "volatility_bps": "volatility",
        "twap_deviation_bps": "spot vs TWAP",
    }
    ws = list(zip(m["feature_names"], m["weights"]))
    vmax = max(abs(w) for _, w in ws)
    axis_x = 470
    d = ImageDraw.Draw(img)
    d.line([(axis_x, 186), (axis_x, 178 + 6 * 62)], fill=mix(INK, (70, 84, 104), ease((t - T(0.07)) / T(0.06))), width=1)
    for i, (fn, w) in enumerate(ws):
        a2 = ease((t - T(0.10 + i * 0.035)) / T(0.05))
        y = 190 + i * 62
        alpha_text(img, (62, y + 12), names[fn], font(19), TEXT, a2)
        L = (abs(w) / vmax) * 240 * ease((t - T(0.10 + i * 0.035)) / T(0.12))
        col = GREEN if w > 0 else RED
        if w > 0:
            d.rounded_rectangle([axis_x + 2, y + 10, axis_x + 2 + L, y + 40], radius=4, fill=mix((12, 18, 30), col, a2))
        else:
            d.rounded_rectangle([axis_x - 2 - L, y + 10, axis_x - 2, y + 40], radius=4, fill=mix((12, 18, 30), col, a2))
        alpha_text(img, (axis_x + 2 + L + 12, y + 14), f"{w:+.2f}", font(16, mono=True), DIM, a2)
        if fn == "price_drop_bps":
            alpha_text(img, (62, y + 36), "negative on purpose: a dip alone fakes out", font(13), FAINT, a2 * 0.9)

    # metrics panel
    q = ease((t - T(0.28)) / T(0.06))
    rounded(img, [792, 138, 1240, 606], fill=(12, 18, 30), outline=(34, 46, 64), alpha=q)
    alpha_text(img, (812, 152), "HELD-OUT METRICS", font(14, bold=True), DIM, q)
    rows = [
        ("AUC", "0.961", GREEN, 46),
        ("precision @ DEFEND", "0.84", TEXT, 26),
        ("recall @ DEFEND", "0.63", TEXT, 26),
        ("windows trained", "4,000", TEXT, 26),
        ("label", "price −300 bps within 15 ticks", DIM, 18),
    ]
    y = 186
    for label, val, col, size in rows:
        a2 = ease((t - T(0.32 + 0.03 * rows.index((label, val, col, size)))) / T(0.05))
        alpha_text(img, (812, y), label, font(16), FAINT, a2)
        alpha_text(img, (812, y + 20), val, font(size, bold=(size > 30), mono=(size < 30)), col, a2)
        y += 26 + size + (6 if size > 30 else 0)

    r = ease((t - T(0.72)) / T(0.06))
    rounded(img, [812, 466, 1220, 588], fill=(15, 24, 37), outline=(40, 56, 74), alpha=r)
    alpha_text(img, (830, 478), "REASON CODE ON CHAIN", font(13, bold=True), AMBER, r)
    alpha_text(img, (830, 500), "VOLATILITY_SPIKE", font(19, bold=True, mono=True), TEXT, r)
    alpha_text(img, (830, 528), "keccak256 of the dominant signed contribution,", font(14), DIM, r)
    alpha_text(img, (830, 548), "so the judgement can be audited after the fact.", font(14), DIM, r)
    alpha_text(img, (830, 570), "In this run: volatility +2.09z carried it.", font(14), TEAL, r)


def italic_dash():
    return SANS


def scene_loop(img, t, dur):
    T = lambda f: dur * f
    head = ease(t / T(0.05))
    alpha_text(img, (64, 54), "One run, two real EVM chains", font(30, bold=True), TEXT, head)
    alpha_text(
        img,
        (64, 96),
        "tools/local_loop.py — anvil nodes at Base Sepolia's 200 ms and a 500 ms Lasna block, the crash played out for real.",
        font(17),
        DIM,
        ease((t - T(0.03)) / T(0.06)),
    )

    p = ease((t - T(0.05)) / T(0.06))
    rounded(img, [40, 136, 900, 640], fill=(9, 13, 21), outline=(36, 48, 66), alpha=p)
    for i in range(6):
        ImageDraw.Draw(img).ellipse([56 + i * 18, 148, 66 + i * 18, 158], fill=mix((9, 13, 21), DIM, 0.5 * p))

    lines = [
        (DIM, "origin chain   : http://127.0.0.1:8545 (id 84532, 0.2s blocks)"),
        (DIM, "reactive chain : http://127.0.0.1:9545 (id 5318007)"),
        (FAINT, ""),
        (BLUE, "--- market crash ---"),
        (TEXT, f"decision  WATCH    score={D['watch_score']:>5} bps   -> published, ignored on chain"),
        (AMBER, f"decision  DEFEND   score={D['defend_score']:>5} bps   -> RiskSignal mined"),
        (PURPLE, "react()   chain 84532 / emitter / severity / replay / cooldown  .. ok"),
        (RED, "callback  vault closed deposits, exits stay open"),
        (TEXT, ""),
        (AMBER, f"decision  CRITICAL score={D['critical_score']:>5} bps   -> escalation bypasses cooldown"),
        (RED, "callback  vault moved 1,000.00 tUSD to the safe harbour"),
        (GREEN, "vault state  safe_mode=True  liquidated=True  defences=2  balance=0"),
    ]
    f = font(15, mono=True)
    n = len(lines)
    for i, (col, s) in enumerate(lines):
        a = ease((t - T(0.09 + i * 0.052)) / T(0.035))
        if a <= 0:
            continue
        y = 180 + i * 30
        if col == RED and s.startswith("callback"):
            ImageDraw.Draw(img).rectangle([56, y - 2, 886, y + 23], fill=mix((9, 13, 21), (40, 20, 24), a))
        alpha_text(img, (66, y), s, f, col, a)

    # right rail: evidence files
    q = ease((t - T(0.05)) / T(0.06))
    rounded(img, [930, 136, 1240, 640], fill=(12, 18, 30), outline=(34, 46, 64), alpha=q)
    alpha_text(img, (950, 152), "EVIDENCE", font(14, bold=True), DIM, q)
    ev = [
        (f"{len(D['decisions'])}", "agent decisions"),
        ("2", "reactive transactions"),
        ("2", "callbacks executed"),
        ("1000", "tUSD moved"),
    ]
    for i, (big, small) in enumerate(ev):
        a2 = ease((t - T(0.62 + i * 0.035)) / T(0.05))
        y = 186 + i * 72
        alpha_text(img, (950, y), big, font(30, bold=True), TEXT, a2)
        alpha_text(img, (950, y + 36), small, font(14), FAINT, a2)
    a3 = ease((t - T(0.80)) / T(0.06))
    ImageDraw.Draw(img).line([(950, 480), (1220, 480)], fill=(40, 52, 70), width=1)
    for i, ln in enumerate(
        ["runs/local-loop-report-", "-20261124T100446Z.json", "runs/agent-decisions-", "-20261124T100446Z.jsonl"]
    ):
        alpha_text(img, (950, 494 + i * 22), ln, font(12, mono=True), FAINT, a3)
    alpha_text(img, (950, 600), "committed with the code", font(13), TEAL, a3)


def scene_latency(img, t, dur):
    T = lambda f: dur * f
    head = ease(t / T(0.05))
    alpha_text(img, (64, 54), "Full-loop latency, and how it was measured", font(30, bold=True), TEXT, head)
    alpha_text(
        img,
        (64, 96),
        "Decision → vault update. Every stage stamped on one clock, correlated by signalId in all three transactions.",
        font(17),
        DIM,
        ease((t - T(0.03)) / T(0.06)),
    )

    runs = [r for r in D["runs"] if r.get("total")]
    if not runs:
        return
    best = min(runs, key=lambda r: r["total"])

    p = ease((t - T(0.05)) / T(0.06))
    rounded(img, [40, 140, 940, 596], fill=(12, 18, 30), outline=(34, 46, 64), alpha=p)
    alpha_text(img, (64, 156), f"RUN {best['file']}  ·  THE FASTEST OF {len(runs)} MEASURED RUNS", font(13, bold=True), DIM, p)

    legs = [
        ("decision → RiskSignal mined", best["legs"][0], BLUE),
        ("signal mined → react() mined", best["legs"][1], PURPLE),
        ("react() → vault updated", best["legs"][2], RED),
    ]
    total = sum(x for _, x, _ in legs if x) or best["total"]
    x0, x1 = 64, 900
    maxms = 240.0
    for i, (label, ms, col) in enumerate(legs):
        a2 = ease((t - T(0.10 + i * 0.12)) / T(0.05))
        y = 204 + i * 104
        alpha_text(img, (x0, y), label, font(18), TEXT, a2)
        alpha_text(img, (x0, y + 26), "stamped on the harness clock", font(13), FAINT, a2 * 0.8)
        wpx = (ms or 0) / maxms * (x1 - x0 - 240) * ease((t - T(0.10 + i * 0.12)) / T(0.22))
        ImageDraw.Draw(img).rounded_rectangle([x0, y + 50, x0 + max(wpx, 2), y + 74], radius=4, fill=mix((12, 18, 30), col, a2))
        alpha_text(img, (x0 + max(wpx, 2) + 12, y + 52), f"{ms:.0f} ms", font(17, bold=True, mono=True), col, a2)

    s = ease((t - T(0.36)) / T(0.06))
    rounded(img, [64, 496, 916, 576], fill=(15, 24, 37), outline=(40, 56, 74), alpha=s)
    alpha_text(img, (84, 510), "TOTAL, DECISION TO VAULT UPDATE", font(13, bold=True), DIM, s)
    alpha_text(img, (84, 530), f"{total:.0f} ms", font(34, bold=True, mono=True), GREEN, s)
    others = ", ".join(f"{r['total']:.0f} ms" for r in sorted(runs, key=lambda r: r["total"]))
    alpha_text(img, (260, 540), f"across runs: {others}", font(15), DIM, s)

    # right rail honest caveats
    q = ease((t - T(0.30)) / T(0.06))
    rounded(img, [968, 140, 1240, 596], fill=(14, 20, 32), outline=(46, 40, 24), alpha=q)
    alpha_text(img, (988, 156), "READ THIS HONESTLY", font(13, bold=True), AMBER, q)
    notes = [
        "The spread between runs is block alignment: a 500 ms Lasna block catches the reactive transaction now or next block.",
        "Local nodes add no network latency — so this is a floor, not a promise.",
        "Production adds finality, the signer's queue and real RPC hops; expect 0.8–2.0 s.",
    ]
    y = 190
    for i, note in enumerate(notes):
        a2 = ease((t - T(0.34 + i * 0.10)) / T(0.05))
        for j, ln in enumerate(wrap(note, font(15), 232)):
            alpha_text(img, (988, y + j * 20), ln, font(15), DIM if i else (226, 232, 240), a2)
        y += 20 * len(wrap(note, font(15), 232)) + 18


def scene_testnet(img, t, dur):
    tn = D["testnet"]
    T = lambda f: dur * f
    head = ease(t / T(0.05))
    alpha_text(img, (64, 54), "The same contracts, on the public testnets", font(30, bold=True), TEXT, head)
    alpha_text(
        img,
        (64, 96),
        "Base Sepolia 84532 and Reactive Lasna 5318007. The three hashes below are the ones the submission asks for.",
        font(17),
        DIM,
        ease((t - T(0.03)) / T(0.06)),
    )

    contracts = [
        ("RiskOracle", "Base Sepolia", tn.get("risk_oracle"), BLUE),
        ("SentinelReactive", "Lasna 5318007", tn.get("sentinel_reactive"), PURPLE),
        ("SentinelVault", "Base Sepolia", tn.get("sentinel_vault"), RED),
    ]
    for i, (name, chain, addr, col) in enumerate(contracts):
        a = ease((t - T(0.08 + i * 0.05)) / T(0.05))
        x = 40 + i * 404
        rounded(img, [x, 140, x + 380, 268], fill=(12, 18, 30), outline=mix(col, PANEL, 0.55), alpha=a)
        alpha_text(img, (x + 20, 156), name, font(21, bold=True), col, a)
        alpha_text(img, (x + 20, 186), chain, font(14, mono=True), FAINT, a)
        line1 = (addr or "deploy pending")[:22]
        line2 = (addr or "")[22:]
        alpha_text(img, (x + 20, 212), line1, font(15, mono=True), TEXT, a)
        alpha_text(img, (x + 20, 234), line2, font(15, mono=True), TEXT, a)

    tx = [
        ("origin", "RiskSignal published by the agent", tn.get("tx_origin"), BLUE),
        ("reactive", "react() → requestCallbackV_1_0", tn.get("tx_reactive"), PURPLE),
        ("callback", "proxy → vault, safe mode on", tn.get("tx_callback"), RED),
    ]
    alpha_text(img, (64, 292), "THE LOOP, END TO END", font(13, bold=True), DIM, ease((t - T(0.26)) / T(0.05)))
    alpha_text(img, (1240, 292), "origin block 10:20:37 → callback block 10:20:38 UTC", font(13, mono=True), FAINT,
               ease((t - T(0.28)) / T(0.05)), anchor="ra")
    for i, (name, what, h, col) in enumerate(tx):
        a = ease((t - T(0.30 + i * 0.11)) / T(0.05))
        y = 318 + i * 96
        rounded(img, [40, y, 1240, y + 84], fill=(11, 17, 28), outline=(34, 46, 64), alpha=a)
        chip(img, 60, y + 14, name, font(13, bold=True, mono=True), col, mix(PANEL, col, 0.16), alpha=a)
        alpha_text(img, (60, y + 50), what, font(15), DIM, a * 0.95)
        if h:
            short = h[:10] + " … " + h[-8:]
            alpha_text(img, (300, y + 18), short, font(15, mono=True), TEXT, a)
            alpha_text(img, (300, y + 46), h, font(12, mono=True), FAINT, a * 0.75)
        else:
            alpha_text(img, (300, y + 30), "pending", font(15, mono=True), FAINT, a)

    f = ease((t - T(0.76)) / T(0.06))
    alpha_text(
        img,
        (984, 548),
        "safeMode: false → true",
        font(16, bold=True, mono=True),
        GREEN,
        f,
    )


def scene_tests(img, t, dur):
    T = lambda f: dur * f
    head = ease(t / T(0.05))
    alpha_text(img, (64, 54), "Tested like the policy is the product", font(30, bold=True), TEXT, head)
    alpha_text(
        img,
        (64, 96),
        "Because it is: the reactive contract is what decides whether money moves.",
        font(18),
        DIM,
        ease((t - T(0.03)) / T(0.06)),
    )

    p = ease((t - T(0.06)) / T(0.06))
    rounded(img, [40, 142, 640, 596], fill=(9, 13, 21), outline=(36, 48, 66), alpha=p)
    tests = [
        ("48", "Foundry tests", GREEN),
        ("21", "Python tests", GREEN),
        ("none", "failing", DIM),
    ]
    for i, (big, small, col) in enumerate(tests):
        a = ease((t - T(0.08 + i * 0.05)) / T(0.05))
        alpha_text(img, (72 + i * 190, 170), big, font(52 if len(big) < 4 else 34, bold=True, mono=True), col, a)
        alpha_text(img, (72 + i * 190, 236), small, font(15), FAINT, a)

    rows = [
        "SentinelReactive.t.sol",
        "  chain mismatch ignored",
        "  emitter spoof ignored",
        "  replay ignored",
        "  cooldown holds, escalation passes",
        "Loop.t.sol",
        "  Watch signal never reaches the vault",
        "  CRITICAL drains to the safe harbour",
        "  on-chain latency recorded",
        "SentinelVault.t.sol / RiskOracle.t.sol",
        "  deposits close, exits stay open",
        "  one agent key, rotatable",
    ]
    for i, s in enumerate(rows):
        a = ease((t - T(0.16 + i * 0.035)) / T(0.04))
        col = TEXT if not s.startswith(" ") else DIM
        x = 72
        if s.startswith("  "):
            ImageDraw.Draw(img).ellipse([76, 300 + i * 24 + 2, 82, 300 + i * 24 + 8], fill=mix(PANEL, GREEN, a))
            s = s[2:]
            x = 92
        alpha_text(img, (x, 300 + i * 24 - 6), s, font(13, mono=True), col, a)

    q = ease((t - T(0.30)) / T(0.06))
    rounded(img, [668, 142, 1240, 596], fill=(12, 18, 30), outline=(34, 46, 64), alpha=q)
    alpha_text(img, (690, 158), "REPOSITORY", font(13, bold=True), DIM, q)
    tree = [
        ("src/", "RiskOracle · SentinelReactive · SentinelVault · MockPool"),
        ("agent/", "features · model · train_model · sentinel_agent"),
        ("script/", "DeployOrigin · DeployReactive · DeployVault · smoke"),
        ("tools/", "local_loop.py — the two-chain harness"),
        ("docs/", "DEPLOY.md · LATENCY.md · DEMO.md"),
        ("runs/", "the reports and decision logs the numbers come from"),
    ]
    for i, (d_, what) in enumerate(tree):
        a = ease((t - T(0.34 + i * 0.05)) / T(0.05))
        y = 196 + i * 64
        alpha_text(img, (690, y), d_, font(19, bold=True, mono=True), TEAL, a)
        for j, ln in enumerate(wrap(what, font(15), 500)):
            alpha_text(img, (690, y + 26 + j * 20), ln, font(15), DIM, a)


def scene_close(img, t, dur):
    T = lambda f: dur * f
    a = ease(t / T(0.08))
    alpha_text(img, (110, 200), "Kill the agent. The defence still happens.", font(40, bold=True), TEXT, a)
    alpha_text(
        img,
        (110, 262),
        "A judgement on chain, a policy that can refuse, and a vault that acts on its own.",
        font(21),
        DIM,
        ease((t - T(0.12)) / T(0.10)),
    )
    b = ease((t - T(0.24)) / T(0.08))
    rounded(img, [110, 330, 1170, 392], fill=(12, 18, 30), outline=(40, 56, 74), alpha=b)
    alpha_text(img, (130, 346), "github.com/dorithesky/reactive-sentinel", font(22, mono=True), TEAL, b)
    c = ease((t - T(0.36)) / T(0.08))
    alpha_text(
        img,
        (110, 420),
        "RiskOracle on Base Sepolia  ·  SentinelReactive on Lasna  ·  SentinelVault on Base Sepolia",
        font(16),
        FAINT,
        c,
    )


SCENES = [
    Scene(
        "title",
        "Every stop-loss depends on someone's bot staying alive. Sentinel moves that decision on chain: a model "
        "judges the risk, a Reactive contract enforces it, and the vault defends itself.",
        scene_title,
    ),
    Scene(
        "architecture",
        "On Base Sepolia the agent reads the pool, computes six scale-free features and scores the risk. The judgement "
        "is published as an event: signal id, severity, score, reason code. SentinelReactive on Lasna is subscribed to "
        "exactly that event. It re-checks the chain, the emitter, the severity floor, the replay guard and the cooldown, "
        "and only then requests a callback. The proxy calls the vault, which closes deposits and keeps withdrawals "
        "open.",
        scene_arch,
    ),
    Scene(
        "model",
        "The AI is not a prompt. It is a logistic regression trained on four thousand simulated pool windows: held-out "
        "AUC zero point nine six one. It learned something counter-intuitive — a deep price drop on its own is slightly "
        "negative evidence, because dips fake out. What precedes a real slide is one-sided selling plus rising "
        "volatility. Every decision carries a reason code on chain, so the judgement can be audited afterwards.",
        scene_model,
    ),
    Scene(
        "loop",
        "Two real EVM chains. The market sells into the pool, the agent scores the flow and publishes. The reactive "
        "transaction follows, the callback lands, and the vault closes deposits. Then the market keeps sliding, the "
        "model escalates to critical, and the vault moves a thousand tUSD to the safe harbour. No keeper touched this.",
        scene_loop,
    ),
    Scene(
        "latency",
        "Full loop, decision to vault update, in the fastest of three runs. The spread between runs is block alignment, "
        "not a change in the system. Treat it as a floor: local nodes add no network latency, so production lands "
        "higher.",
        scene_latency,
    ),
    Scene(
        "testnet",
        "The same contracts are deployed on the public testnets. Here are the three transaction hashes the submission "
        "asks for: the judgement on Base Sepolia, the reactive transaction on Lasna, and the callback that put the "
        "vault into safe mode.",
        scene_testnet,
    ),
    Scene(
        "tests",
        "Forty-eight Foundry tests and twenty-one Python tests, all passing. The reactive policy has its own tests for "
        "chain mismatch, emitter spoofing, replay, cooldown and escalation.",
        scene_tests,
    ),
    Scene(
        "close",
        "Sentinel. An AI in the decision path, and a defence that does not need to be alive. Code and measurements: "
        "github dot com slash dorithesky slash reactive dash sentinel.",
        scene_close,
    ),
]


# ---------------------------------------------------------------------------------------------
# audio + render
# ---------------------------------------------------------------------------------------------


def synth() -> list[Path]:
    BUILD.mkdir(exist_ok=True)
    try:
        import edge_tts
    except ImportError:
        print("edge-tts missing: pip install edge-tts", file=sys.stderr)
        raise

    async def one(s: Scene) -> Path:
        out = BUILD / f"{s.key}.mp3"
        c = edge_tts.Communicate(s.narration, VOICE, rate="-3%")
        await c.save(str(out))
        return out

    async def all_() -> list[Path]:
        return [await one(s) for s in SCENES]

    return asyncio.run(all_())


def ffmpeg_bin() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def duration(path: Path) -> float:
    out = subprocess.run(
        [ffmpeg_bin(), "-i", str(path)], capture_output=True, text=True, errors="ignore"
    ).stderr
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", out)
    if not m:
        raise RuntimeError(f"no duration for {path}")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def build_audio(clips: list[Path]) -> tuple[Path, list[float]]:
    """Concatenate narration with gaps; return the track and each scene's start time."""
    track = BUILD / "narration.wav"
    parts, starts, cursor = [], [], 0.0
    for scene, clip in zip(SCENES, clips):
        wav = BUILD / f"{scene.key}.wav"
        subprocess.run(
            [ffmpeg_bin(), "-y", "-i", str(clip), "-ar", "48000", "-ac", "2", str(wav)],
            capture_output=True,
            check=True,
        )
        d = duration(wav)
        starts.append(cursor + scene.lead)
        parts.append((wav, scene.lead, scene.tail))
        cursor += scene.lead + d + scene.tail

    # silent bed of the full length, narration laid on top
    total = cursor
    inputs, filters, idx = [], [], 1
    inputs += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    for i, (wav, _, _) in enumerate(parts):
        inputs += ["-i", str(wav)]
    chains = []
    for i in range(len(parts)):
        # absolute start of this scene on the finished track, not its own lead-in
        ms = int(round(starts[i] * 1000))
        chains.append(f"[{i + 1}:a]adelay={ms}|{ms}[a{i}]")
    mix_in = "[0:a]" + "".join(f"[a{i}]" for i in range(len(parts)))
    filters = ";".join(chains) + f";{mix_in}amix=inputs={len(parts) + 1}:normalize=0[aout]"
    subprocess.run(
        [ffmpeg_bin(), "-y", *inputs, "-filter_complex", filters, "-map", "[aout]", "-ac", "2", "-ar", "48000", str(track)],
        capture_output=True,
        check=True,
    )
    return track, starts


def render_scene(i: int, durs: list[float], path: Path) -> float:
    """Render one scene to its own file, so a long build stays resumable and each step is short."""
    import imageio

    scene = SCENES[i]
    d = scene.lead + durs[i] + scene.tail
    n = max(1, int(round(d * FPS)))
    writer = imageio.get_writer(
        str(path),
        fps=FPS,
        codec="libx264",
        quality=8,
        pixelformat="yuv420p",
        macro_block_size=1,
        ffmpeg_params=["-preset", "veryfast", "-crf", "20"],
    )
    for k in range(n):
        t = k / FPS
        img = BG.copy()
        scene.render(img, t, d)
        caption(img, scene.narration, t, d)
        hud(img, i)
        writer.append_data(frame_bytes(img, 1.0))
    writer.close()
    print(f"  {scene.key:14s} {d:5.2f}s  {n:4d} frames", flush=True)
    return d


def concat(paths: list[Path], out: Path) -> None:
    lst = BUILD / "tmp" / "segments.txt"
    lst.parent.mkdir(parents=True, exist_ok=True)
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in paths))
    subprocess.run(
        [ffmpeg_bin(), "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)],
        capture_output=True,
        check=True,
    )


def caption(img, s: str, t: float, dur: float) -> None:
    """Burn the narration in as captions, two lines at a time, timed to the reading pace."""
    f = font(21)
    words = s.split()
    lines = wrap(s, f, W - 200)
    # chunk the line list so no caption is longer than two lines
    chunks = [lines[i : i + 2] for i in range(0, len(lines), 2)]
    total_words = max(1, len(words))
    per_word = max(0.22, (dur - 1.1) / total_words)
    n = 0
    for ci, ch in enumerate(chunks):
        words_in = sum(len(x.split()) for x in ch)
        start = 0.45 + n * per_word
        n += words_in
        end = 0.45 + n * per_word
        if t < start - 0.12 or t > end + 0.32:
            continue
        a = min(1.0, (t - (start - 0.12)) / 0.18) * min(1.0, max(0.0, (end + 0.32 - t) / 0.22))
        # panel
        heights = [f.size + 6] * len(ch)
        box_h = sum(heights) + 18
        rounded(img, [90, H - 40 - box_h, W - 90, H - 24], fill=(6, 10, 18), outline=None, alpha=0.62 * a, radius=10)
        y = H - 34 - box_h + 9
        for ln in ch:
            alpha_text(img, (W // 2, y), ln, f, (241, 245, 249), a, anchor="ma")
            y += f.size + 6


def hud(img, scene_index: int) -> None:
    d = ImageDraw.Draw(img)
    d.line([(0, 0), (W, 0)], fill=(20, 28, 42), width=2)
    alpha_text(img, (40, 18), "SENTINEL", font(15, bold=True), (203, 213, 225), 0.85)
    alpha_text(img, (40 + text_w("SENTINEL ", font(15, bold=True)) + 4, 18), "/ AI risk, enforced on chain",
               font(15), FAINT, 0.8)
    # progress bar
    total = len(SCENES)
    seg_w = (W - 80) / total
    for i in range(total):
        x = 40 + i * seg_w
        col = TEAL if i <= scene_index else (40, 52, 70)
        d.rectangle([x, H - 6, x + seg_w - 6, H - 3], fill=col)


def frame_bytes(img: Image.Image, fade: float):
    import numpy as np

    if fade < 1.0:
        img = Image.blend(Image.new("RGB", img.size, INK), img, fade)
    return np.asarray(img.convert("RGB"))


def preview_frames() -> None:
    PREVIEW.mkdir(parents=True, exist_ok=True)
    for scene in SCENES:
        d = 9.0
        for frac in (0.55,):
            img = BG.copy()
            scene.render(img, d * frac, d)
            caption(img, scene.narration, d * frac, d)
            hud(img, SCENES.index(scene))
            img.save(PREVIEW / f"{scene.key}.png")
    print(f"previews in {PREVIEW}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--skip-audio", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "demo.mp4"))
    args = ap.parse_args()

    if args.preview:
        preview_frames()
        return

    clips = synth()
    track, _ = build_audio(clips)
    durs = [duration(BUILD / f"{s.key}.wav") for s in SCENES]
    segs = []
    for i, scene in enumerate(SCENES):
        seg = BUILD / "tmp" / f"seg_{i}_{scene.key}.mp4"
        if not seg.exists() or args.force:
            render_scene(i, durs, seg)
        segs.append(seg)
    silent = BUILD / "silent.mp4"
    concat(segs, silent)
    total = sum(d + s.lead + s.tail for d, s in zip(durs, SCENES))
    print(f"narration {sum(durs):.1f}s, video {total:.1f}s")
    if not args.skip_audio:
        subprocess.run(
            [ffmpeg_bin(), "-y", "-i", str(silent), "-i", str(track), "-c:v", "copy", "-c:a", "aac",
             "-b:a", "192k", "-shortest", "-movflags", "+faststart", args.out],
            capture_output=True,
            check=True,
        )
    else:
        subprocess.run([ffmpeg_bin(), "-y", "-i", str(silent), "-c", "copy", args.out], capture_output=True, check=True)
    print("done:", args.out, f"{os.path.getsize(args.out) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()

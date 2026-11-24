#!/usr/bin/env python3
"""news-image-20261123 v3 -- "The Day in World News", Monday 23 November 2026.

Renders one 1024x1024 PNG newspaper page to /output/entry.png using only the
Python 3.13 standard library. A small TrueType rasteriser is included below
because no third-party imaging library may be used. The render is fully
deterministic: fixed data, fixed layout, integer blending, no clocks, no
randomness, no network.
"""

import os
import struct
import sys
import zlib

# --------------------------------------------------------------------------
# TrueType loading (cmap, hmtx, glyf outlines incl. composites)
# --------------------------------------------------------------------------


def _u16(buf, off):
    return struct.unpack_from(">H", buf, off)[0]


def _i16(buf, off):
    return struct.unpack_from(">h", buf, off)[0]


def _u32(buf, off):
    return struct.unpack_from(">I", buf, off)[0]


class Font:
    """Minimal read-only TrueType font: advances + flattened outlines."""

    def __init__(self, path):
        with open(path, "rb") as fh:
            self.data = fh.read()
        data = self.data
        num_tables = _u16(data, 4)
        self.tables = {}
        for i in range(num_tables):
            off = 12 + i * 16
            tag = data[off:off + 4].decode("latin-1")
            self.tables[tag] = (_u32(data, off + 8), _u32(data, off + 12))
        head = self.tables["head"][0]
        self.units_per_em = _u16(data, head + 18)
        self.index_to_loc = _i16(data, head + 50)
        maxp = self.tables["maxp"][0]
        self.num_glyphs = _u16(data, maxp + 4)
        hhea = self.tables["hhea"][0]
        self.num_h_metrics = _u16(data, hhea + 34)
        self._parse_cmap()
        self._outline_cache = {}
        self._bitmap_cache = {}

    # -- cmap (format 4 preferred, format 12 fallback) --------------------
    def _parse_cmap(self):
        data = self.data
        base, _ = self.tables["cmap"]
        n = _u16(data, base + 2)
        chosen = None
        for i in range(n):
            off = base + 4 + i * 8
            platform = _u16(data, off)
            sub_off = _u32(data, off + 4)
            fmt = _u16(data, base + sub_off)
            if fmt == 4 and platform in (3, 0):
                chosen = ("fmt4", base + sub_off)
                break
            if fmt == 12 and chosen is None:
                chosen = ("fmt12", base + sub_off)
        if chosen is None:
            raise ValueError("no usable cmap subtable")
        kind, start = chosen
        self.cmap = {}
        if kind == "fmt4":
            seg_count = _u16(data, start + 6) // 2
            end_base = start + 14
            start_base = end_base + 2 * seg_count + 2
            delta_base = start_base + 2 * seg_count
            range_base = delta_base + 2 * seg_count
            for s in range(seg_count):
                end_c = _u16(data, end_base + 2 * s)
                start_c = _u16(data, start_base + 2 * s)
                delta = _i16(data, delta_base + 2 * s)
                range_off = _u16(data, range_base + 2 * s)
                for code in range(start_c, min(end_c, 0xFFFD) + 1):
                    if range_off == 0:
                        gid = (code + delta) & 0xFFFF
                    else:
                        gpos = range_base + 2 * s + range_off + 2 * (code - start_c)
                        gid = _u16(data, gpos)
                        if gid:
                            gid = (gid + delta) & 0xFFFF
                    if gid:
                        self.cmap[code] = gid
        else:
            groups = _u32(data, start + 12)
            for g in range(groups):
                goff = start + 16 + g * 12
                first = _u32(data, goff)
                last = _u32(data, goff + 4)
                gid0 = _u32(data, goff + 8)
                for code in range(first, min(last, first + 0x10000) + 1):
                    if code > 0xFFFF:
                        break
                    self.cmap[code] = gid0 + (code - first)

    def glyph_id(self, ch):
        gid = self.cmap.get(ord(ch))
        if gid is None:
            gid = self.cmap.get(ord("?"), 0)
        return gid

    def advance(self, gid):
        data = self.data
        base, _ = self.tables["hmtx"]
        idx = min(gid, self.num_h_metrics - 1)
        return _u16(data, base + idx * 4)

    # -- glyf outlines ------------------------------------------------------
    def _glyph_offset(self, gid):
        data = self.data
        base, _ = self.tables["loca"]
        if self.index_to_loc == 1:
            return _u32(data, base + gid * 4)
        return _u16(data, base + gid * 2) * 2

    def outline(self, gid):
        """Return list of contours; each contour is a flat point list."""
        if gid in self._outline_cache:
            return self._outline_cache[gid]
        result = self._outline_raw(gid, 0)
        self._outline_cache[gid] = result
        return result

    def _outline_raw(self, gid, depth):
        data = self.data
        gbase, _ = self.tables["glyf"]
        off = gbase + self._glyph_offset(gid)
        if gid + 1 < self.num_glyphs:
            nxt = gbase + self._glyph_offset(gid + 1)
        else:
            nxt = gbase + self.tables["glyf"][1]
        if off >= nxt or depth > 4:
            return []
        contours_count = _i16(data, off)
        if contours_count >= 0:
            return self._simple_outline(data, off, contours_count)
        return self._composite_outline(data, off, depth)

    def _simple_outline(self, data, off, contours_count):
        end_pts = [_u16(data, off + 10 + 2 * i) for i in range(contours_count)]
        total_pts = end_pts[-1] + 1 if end_pts else 0
        pos = off + 10 + 2 * contours_count
        instr_len = _u16(data, pos)
        pos += 2 + instr_len
        flags = []
        while len(flags) < total_pts:
            flag = data[pos]
            pos += 1
            flags.append(flag)
            if flag & 0x08:
                rep = data[pos]
                pos += 1
                flags.extend([flag] * rep)
        xs = []
        x = 0
        for flag in flags:
            if flag & 0x02:
                dx = data[pos]
                pos += 1
                x += dx if (flag & 0x10) else -dx
            elif flag & 0x10:
                pass
            else:
                x += _i16(data, pos)
                pos += 2
            xs.append(x)
        ys = []
        y = 0
        for flag in flags:
            if flag & 0x04:
                dy = data[pos]
                pos += 1
                y += dy if (flag & 0x20) else -dy
            elif flag & 0x20:
                pass
            else:
                y += _i16(data, pos)
                pos += 2
            ys.append(y)
        points = [(xs[i], ys[i], bool(flags[i] & 0x01)) for i in range(total_pts)]
        contours = []
        start = 0
        for end in end_pts:
            contours.append(_flatten_contour(points[start:end + 1]))
            start = end + 1
        return contours

    def _composite_outline(self, data, off, depth):
        pos = off + 10
        contours = []
        while True:
            flags = _u16(data, pos)
            comp_gid = _u16(data, pos + 2)
            pos += 4
            if flags & 0x0001:
                arg1 = _i16(data, pos)
                arg2 = _i16(data, pos + 2)
                pos += 4
            else:
                arg1 = struct.unpack_from(">b", data, pos)[0]
                arg2 = struct.unpack_from(">b", data, pos + 1)[0]
                pos += 2
            sx = sy = 1.0
            if flags & 0x0008:
                sx = sy = _i16(data, pos) / 16384.0
                pos += 2
            elif flags & 0x0040:
                sx = _i16(data, pos) / 16384.0
                sy = _i16(data, pos + 2) / 16384.0
                pos += 4
            elif flags & 0x0080:
                pos += 8  # 2x2 matrix: not expected in these fonts; skip values
            dx, dy = (arg1, arg2) if (flags & 0x0002) else (0, 0)
            for contour in self._outline_raw(comp_gid, depth + 1):
                moved = [(int(round(px * sx)) + dx, int(round(py * sy)) + dy)
                         for (px, py) in contour]
                contours.append(moved)
            if not (flags & 0x0020):
                break
        return contours


def _flatten_contour(points):
    """Convert one TrueType contour (on/off-curve points) to a polygon."""
    if not points:
        return []
    # Rotate so the contour starts on an on-curve point.
    seq = list(points)
    if not seq[0][2]:
        if seq[-1][2]:
            seq = [seq[-1]] + seq[:-1]
        else:
            mx = (seq[0][0] + seq[-1][0]) / 2.0
            my = (seq[0][1] + seq[-1][1]) / 2.0
            seq = [(mx, my, True)] + seq
    out = [(seq[0][0], seq[0][1])]
    i = 1
    n = len(seq)
    while i <= n:
        cur = seq[i % n]
        if cur[2]:
            out.append((cur[0], cur[1]))
            i += 1
        else:
            nxt = seq[(i + 1) % n]
            end = (nxt[0], nxt[1]) if nxt[2] else ((cur[0] + nxt[0]) / 2.0,
                                                   (cur[1] + nxt[1]) / 2.0)
            steps = 6
            x0, y0 = out[-1]
            for s in range(1, steps + 1):
                t = s / steps
                mt = 1.0 - t
                px = mt * mt * x0 + 2 * mt * t * cur[0] + t * t * end[0]
                py = mt * mt * y0 + 2 * mt * t * cur[1] + t * t * end[1]
                out.append((px, py))
            i += 1 if nxt[2] else 1
            if nxt[2]:
                i += 1
    return out


# --------------------------------------------------------------------------
# Glyph rasterisation: scanline non-zero winding fill, 4x supersampling
# --------------------------------------------------------------------------

SS = 4


def glyph_bitmap(font, gid, size_px):
    """Return (coverage bytes, w, h, left, top) for a glyph at size_px.

    Coverage is a w*h bytearray of 0..255 alpha values. `left`/`top` give the
    bitmap origin relative to the pen position on the baseline.
    """
    key = (gid, round(size_px * 4))
    if key in font._bitmap_cache:
        return font._bitmap_cache[key]
    scale = size_px / font.units_per_em
    contours = font.outline(gid)
    if not contours:
        result = (bytearray(), 0, 0, 0, 0)
        font._bitmap_cache[key] = result
        return result
    xs = [p[0] for c in contours for p in c]
    ys = [p[1] for c in contours for p in c]
    x_min, x_max = min(xs) * scale, max(xs) * scale
    y_min, y_max = min(ys) * scale, max(ys) * scale
    left = int(x_min // 1)
    top = int(-(-y_max // 1))  # ceil of y_max in px, bitmap rows go downward
    w = int(x_max // 1) - left + 1
    h = top + int(-(y_min // 1)) + 1
    sw, sh = w * SS, h * SS
    cover = bytearray(sw * sh)
    # Build edges in supersampled bitmap coordinates (y flipped).
    edges = []
    for contour in contours:
        pts = [((px * scale - left) * SS, (top - py * scale) * SS)
               for (px, py) in contour]
        for i in range(len(pts)):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % len(pts)]
            if y1 != y2:
                edges.append((x1, y1, x2, y2))
    for row in range(sh):
        yc = row + 0.5
        hits = []
        for (x1, y1, x2, y2) in edges:
            if (y1 <= yc < y2) or (y2 <= yc < y1):
                xi = x1 + (yc - y1) * (x2 - x1) / (y2 - y1)
                hits.append((xi, 1 if y2 > y1 else -1))
        if not hits:
            continue
        hits.sort()
        winding = 0
        base = row * sw
        for j in range(len(hits) - 1):
            winding += hits[j][1]
            if winding != 0:
                xa = int(hits[j][0] + 0.5)
                xb = int(hits[j + 1][0] + 0.5)
                for px in range(max(xa, 0), min(xb, sw)):
                    cover[base + px] = 255
        # Any pixel the scanline centre sits inside also counts partially;
        # edge columns get coverage from neighbouring rows via SS average.
    out = bytearray(w * h)
    block = SS * SS
    for y in range(h):
        for x in range(w):
            total = 0
            for dy in range(SS):
                row_base = (y * SS + dy) * sw + x * SS
                for dx in range(SS):
                    total += cover[row_base + dx]
            out[y * w + x] = total * 255 // (block * 255)
    result = (out, w, h, left, top)
    font._bitmap_cache[key] = result
    return result


# --------------------------------------------------------------------------
# Canvas
# --------------------------------------------------------------------------


class Canvas:
    def __init__(self, w, h, color):
        self.w = w
        self.h = h
        self.px = bytearray(color) * (w * h)

    def blend(self, x, y, color, alpha):
        if 0 <= x < self.w and 0 <= y < self.h and alpha:
            i = (y * self.w + x) * 3
            if alpha >= 255:
                self.px[i:i + 3] = bytes(color)
            else:
                inv = 255 - alpha
                self.px[i] = (color[0] * alpha + self.px[i] * inv) // 255
                self.px[i + 1] = (color[1] * alpha + self.px[i + 1] * inv) // 255
                self.px[i + 2] = (color[2] * alpha + self.px[i + 2] * inv) // 255

    def rect(self, x, y, w, h, color):
        x0, y0 = max(int(x), 0), max(int(y), 0)
        x1, y1 = min(int(x + w), self.w), min(int(y + h), self.h)
        if x1 <= x0 or y1 <= y0:
            return
        row = bytes(color) * (x1 - x0)
        for yy in range(y0, y1):
            i = (yy * self.w + x0) * 3
            self.px[i:i + len(row)] = row

    def circle(self, cx, cy, r, color):
        r2 = r * r
        for yy in range(int(cy - r - 1), int(cy + r + 2)):
            for xx in range(int(cx - r - 1), int(cx + r + 2)):
                d2 = (xx - cx) ** 2 + (yy - cy) ** 2
                if d2 <= r2:
                    self.blend(xx, yy, color, 255)
                elif d2 <= (r + 1) * (r + 1):
                    frac = (r + 1) - d2 ** 0.5
                    if frac > 0:
                        self.blend(xx, yy, color, int(255 * frac))

    def polygon(self, pts, color):
        ys = [p[1] for p in pts]
        y_lo, y_hi = max(int(min(ys)), 0), min(int(max(ys)) + 1, self.h)
        edges = []
        for i in range(len(pts)):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % len(pts)]
            if y1 != y2:
                edges.append((x1, y1, x2, y2))
        for yy in range(y_lo, y_hi):
            yc = yy + 0.5
            hits = []
            for (x1, y1, x2, y2) in edges:
                if (y1 <= yc < y2) or (y2 <= yc < y1):
                    hits.append(x1 + (yc - y1) * (x2 - x1) / (y2 - y1))
            hits.sort()
            for j in range(0, len(hits) - 1, 2):
                xa = max(int(hits[j] + 0.5), 0)
                xb = min(int(hits[j + 1] + 0.5), self.w)
                for xx in range(xa, xb):
                    i = (yy * self.w + xx) * 3
                    self.px[i:i + 3] = bytes(color)

    def line(self, x1, y1, x2, y2, color, width=1):
        steps = max(abs(x2 - x1), abs(y2 - y1), 1)
        steps = int(steps * 2) + 1
        for s in range(steps + 1):
            t = s / steps
            xx = x1 + (x2 - x1) * t
            yy = y1 + (y2 - y1) * t
            self.rect(xx - width / 2, yy - width / 2, width, width, color)


# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------


def text_width(font, text, size, spacing=0.0):
    scale = size / font.units_per_em
    total = 0.0
    for ch in text:
        total += font.advance(font.glyph_id(ch)) * scale + spacing
    return total - (spacing if text else 0.0)


def draw_text(cv, x, y_base, text, font, size, color, spacing=0.0):
    pen = float(x)
    scale = size / font.units_per_em
    for ch in text:
        gid = font.glyph_id(ch)
        bmp, w, h, left, top = glyph_bitmap(font, gid, size)
        if w and h:
            bx = int(round(pen)) + left
            by = int(round(y_base)) - top
            for yy in range(h):
                row = bmp[yy * w:(yy + 1) * w]
                for xx in range(w):
                    a = row[xx]
                    if a:
                        cv.blend(bx + xx, by + yy, color, a)
        pen += font.advance(gid) * scale + spacing
    return pen


def draw_text_centered(cv, cx, y_base, text, font, size, color, spacing=0.0):
    w = text_width(font, text, size, spacing)
    return draw_text(cv, cx - w / 2, y_base, text, font, size, color, spacing)


def draw_text_right(cv, xr, y_base, text, font, size, color, spacing=0.0):
    w = text_width(font, text, size, spacing)
    return draw_text(cv, xr - w, y_base, text, font, size, color, spacing)


def wrap_text(font, text, size, max_width, spacing=0.0):
    words = text.split()
    lines = []
    cur = ""
    for word in words:
        trial = word if not cur else cur + " " + word
        if text_width(font, trial, size, spacing) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def fit_size(font, text, base_size, max_width, spacing=0.0, floor=10.0):
    size = base_size
    while size > floor and text_width(font, text, size, spacing) > max_width:
        size -= 1.0
    return size


# --------------------------------------------------------------------------
# PNG output (filter 0 rows, zlib level 9 -> deterministic bytes)
# --------------------------------------------------------------------------


def save_png(cv, path):
    raw = bytearray()
    stride = cv.w * 3
    for y in range(cv.h):
        raw.append(0)
        raw += cv.px[y * stride:(y + 1) * stride]

    def chunk(tag, payload):
        out = struct.pack(">I", len(payload)) + tag + payload
        out += struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        return out

    ihdr = struct.pack(">IIBBBBB", cv.w, cv.h, 8, 2, 0, 0, 0)
    blob = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(blob)
    return len(blob)


# --------------------------------------------------------------------------
# Fonts (Liberation first, DejaVu fallback -- same environment family)
# --------------------------------------------------------------------------

FONT_CANDIDATES = {
    "serif_b": ["/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"],
    "serif": ["/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"],
    "serif_i": ["/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"],
    "sans_b": ["/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
}


def load_fonts():
    fonts = {}
    for role, paths in FONT_CANDIDATES.items():
        for path in paths:
            if os.path.exists(path):
                fonts[role] = Font(path)
                break
        if role not in fonts:
            raise RuntimeError("no font found for role " + role)
    return fonts


# --------------------------------------------------------------------------
# The day's stories -- all reported Monday 23 November 2026
# --------------------------------------------------------------------------

LEADS = [
    {
        "banner": "+31%", "banner_color": (22, 40, 63),
        "cat": "MARKETS · NEW YORK",
        "headline": "Nvidia Brings Back Huang; Shares Rocket",
        "desc": "Nvidia reinstated Jensen Huang as chief executive. The stock "
                "leapt 31% and the Nasdaq jumped 6% as the AI trade roared back.",
        "source": "REUTERS", "status": "AI TRADE REVIVES",
    },
    {
        "banner": "7 DRONES", "banner_color": (176, 42, 33),
        "cat": "WAR AT SEA · LIBYA",
        "headline": "Ukraine Sinks Russian Corvette at Benghazi",
        "desc": "Kyiv says seven naval drones struck the corvette Merkury in "
                "Benghazi port, carrying the sea war to the Mediterranean.",
        "source": "THE INDEPENDENT", "status": "KYIV CONFIRMS",
    },
    {
        "banner": "258", "banner_color": (46, 93, 74),
        "cat": "POLITICS · WASHINGTON",
        "headline": "Democrats Clinch a Historic House Majority",
        "desc": "Democrats won at least 258 House seats, their largest majority "
                "since 1992, with four races still uncalled.",
        "source": "RUSSELL RECORD", "status": "BIGGEST SINCE 1992",
    },
    {
        "banner": "COP31", "banner_color": (166, 124, 32),
        "cat": "CLIMATE · SUMMIT",
        "headline": "Climate Summit Collapses Over Fossil Fuels",
        "desc": "The UN climate talks ended with no deal to phase out coal, oil "
                "and gas -- a serious setback for climate action.",
        "source": "RNA NEWS", "status": "NO DEAL REACHED",
    },
]

MIDS = [
    {"cat": "DISASTER · INDONESIA",
     "headline": "Factory Fire Kills 24 in Banten",
     "desc": "Rescuers searched a burned candy factory for about 30 workers still missing.",
     "source": "REUTERS"},
    {"cat": "MEXICO · CARTELS",
     "headline": "Kingpin 'El Chapo Isidro' Captured",
     "desc": "Elite troops seized the FBI top-ten fugitive in Guasave; four soldiers died.",
     "source": "ASSOCIATED PRESS"},
    {"cat": "CYBER · WASHINGTON",
     "headline": "FBI Probes Hack of Police Database",
     "desc": "Agents investigate an apparent breach of the national crime records system.",
     "source": "AP WIRE"},
    {"cat": "GERMANY · SCHOOLS",
     "headline": "450 Schools Get Bomb Threats",
     "desc": "Hundreds of schools across Germany received emailed threats Monday morning.",
     "source": "REUTERS / BILD"},
    {"cat": "NIGERIA · BAYELSA",
     "headline": "Boat Capsize Kills 24 Mourners",
     "desc": "A boat carrying mourners to a funeral overturned; the search continued.",
     "source": "WIRE REPORTS"},
    {"cat": "SPACE · CHINA",
     "headline": "Shenzhou-24 Crew Unveiled",
     "desc": "China named its next station crew, including a first foreign astronaut.",
     "source": "XINHUA"},
    {"cat": "GULF · HORMUZ",
     "headline": "Iran Vows to Counter Hormuz Blockade",
     "desc": "Tehran pledged 'every means' against a US blockade as three ships were hit.",
     "source": "ARAB NEWS"},
    {"cat": "SPAIN · CATALONIA",
     "headline": "Court Clears Puigdemont's Return",
     "desc": "The Supreme Court shelved the case against the exiled Catalan leader.",
     "source": "REUTERS"},
]

ALSO = [
    "Russian strikes kill 14 in Kyiv Oblast -- Kyiv Independent",
    "Fighting disrupts Ebola response in Congo -- WHO",
    "US sues to recover $1B in hacked Binance crypto -- Reuters",
    "Trump pauses Army lead-azide detonation in Iowa -- Reuters",
    "Delta delays 1,300 flights in tech failure -- wires",
    "Bangladesh opens mass typhoid vaccine drive -- wires",
    "Modi and Balen Shah thaw India-Nepal ties -- wires",
    "Prabowo names two vice-president picks -- SCMP",
    "Singer lashed in Iran over no-hijab concert -- wires",
    "Drought deepens crisis for millions in Afghanistan -- PBS",
    "Colombia fuel fund owes Ecopetrol 11.4T pesos -- El Colombiano",
    "Brazil audit court sets CBS tax rate 11.3468% -- wires",
    "Chile budget clears lower house, heads to Senate -- wires",
    "Bolsonaro rushed to ICU in Brasilia -- wires",
    "Pope jokes after rosary hits his eye in Peru -- wires",
    "Student protests sweep Europe's cities -- RNA",
    "Russian cargo ship burns in Gulf of Finland -- wires",
    "Princess Kate tells cancer story in 'Heal' film -- wires",
    "New Zealand hears Estonia's Russia advice -- RNZ",
    "Canberra puts AI at centre of foreign policy -- SCMP",
    "Skubal signs record $450M Mets deal -- wires",
    "Manchester concert evacuated in substance scare -- Independent",
]

# Pin locations for the map: (longitude, latitude, label)
PINS = [
    (-99, 40, "US"), (30, 50, "Kyiv"), (20, 32, "Libya"), (106, -6, "Java"),
    (-108, 25, "Mexico"), (13, 52, "Berlin"), (6, 5, "Nigeria"),
    (116, 40, "China"), (56, 26, "Hormuz"), (-3, 40, "Spain"),
    (-74, 4, "Bogota"), (-48, -15, "Brasilia"), (77, 28, "India"),
    (90, 24, "Dhaka"), (51, 35, "Tehran"), (28, -26, "Africa"),
    (149, -35, "Canberra"), (174, -41, "Wellington"),
]

STORY_COUNT = len(LEADS) + len(MIDS) + len(ALSO)  # 34
CONTINENTS = 6


# --------------------------------------------------------------------------
# Page layout
# --------------------------------------------------------------------------

PAPER = (247, 242, 231)
CARD = (253, 251, 244)
NAVY = (22, 40, 63)
INK = (22, 24, 29)
RED = (176, 42, 33)
GOLD = (201, 162, 39)
FAINT = (74, 70, 60)
BORDER = (206, 196, 164)
CHIP_BG = (233, 226, 205)
LAND = (228, 216, 184)
LAND_EDGE = (170, 155, 112)
W = H = 1024
MARGIN = 26


def chip(cv, x, y, text, font, size, bg, fg):
    w = text_width(font, text, size) + 13
    cv.rect(x, y, w, 17, bg)
    draw_text(cv, x + 6.5, y + 12.5, text, font, size, fg)
    return w


def card_frame(cv, x, y, w, h):
    cv.rect(x, y, w, h, CARD)
    cv.rect(x, y, w, 1.5, BORDER)
    cv.rect(x, y + h - 1.5, w, 1.5, BORDER)
    cv.rect(x, y, 1.5, h, BORDER)
    cv.rect(x + w - 1.5, y, 1.5, h, BORDER)


def lead_card(cv, fonts, x, y, w, h, idx, story):
    card_frame(cv, x, y, w, h)
    banner_h = 56
    cv.rect(x + 1.5, y + 1.5, w - 3, banner_h, story["banner_color"])
    bsize = fit_size(fonts["serif_b"], story["banner"], 37, w - 30)
    draw_text_centered(cv, x + w / 2, y + 41, story["banner"],
                       fonts["serif_b"], bsize, (255, 255, 255))
    cy = y + banner_h + 25
    cv.circle(x + 21, cy - 4, 11, RED)
    draw_text_centered(cv, x + 21, cy, str(idx), fonts["sans_b"], 12,
                       (255, 255, 255))
    draw_text(cv, x + 40, cy, story["cat"], fonts["sans_b"], 10.5, RED,
              spacing=1.0)
    lines = wrap_text(fonts["serif_b"], story["headline"], 16, w - 24)
    hy = cy + 26
    for ln in lines[:3]:
        draw_text(cv, x + 12, hy, ln, fonts["serif_b"], 16, INK)
        hy += 19
    dlines = wrap_text(fonts["serif"], story["desc"], 11.5, w - 24)
    dy = hy + 6
    for ln in dlines[:4]:
        draw_text(cv, x + 12, dy, ln, fonts["serif"], 11.5, FAINT)
        dy += 14.5
    chip_y = y + h - 27
    used = chip(cv, x + 12, chip_y, story["source"], fonts["sans_b"], 9.5,
                CHIP_BG, INK)
    chip(cv, x + 18 + used, chip_y, story["status"], fonts["sans_b"], 9.5,
         NAVY, (255, 255, 255))


def mid_card(cv, fonts, x, y, w, h, story):
    card_frame(cv, x, y, w, h)
    draw_text(cv, x + 11, y + 19, story["cat"], fonts["sans_b"], 10, RED,
              spacing=1.0)
    lines = wrap_text(fonts["serif_b"], story["headline"], 14.5, w - 22)
    hy = y + 41
    for ln in lines[:2]:
        draw_text(cv, x + 11, hy, ln, fonts["serif_b"], 14.5, INK)
        hy += 17
    dlines = wrap_text(fonts["serif"], story["desc"], 11, w - 22)
    dy = hy + 4
    for ln in dlines[:2]:
        draw_text(cv, x + 11, dy, ln, fonts["serif"], 11, FAINT)
        dy += 13.5
    draw_text(cv, x + 11, y + h - 9, story["source"], fonts["sans_b"], 9,
              (140, 130, 105))


# Stylised continent outlines as (lon, lat) polygons -- a simple world map.
CONTINENT_POLYS = [
    [(-168, 66), (-150, 70), (-135, 69), (-122, 71), (-110, 68), (-98, 69),
     (-88, 66), (-82, 66), (-79, 62), (-72, 58), (-65, 60), (-56, 52),
     (-61, 46), (-67, 44), (-71, 41), (-74, 39), (-76, 35), (-79, 31),
     (-81, 26), (-84, 23), (-87, 21), (-91, 19), (-95, 16), (-97, 16),
     (-93, 15), (-88, 13), (-83, 9), (-79, 9), (-85, 12), (-91, 14),
     (-96, 16), (-102, 18), (-105, 22), (-109, 23), (-113, 28), (-117, 33),
     (-122, 37), (-124, 45), (-129, 53), (-140, 60), (-155, 59), (-165, 60)],
    [(-79, 9), (-75, 11), (-71, 12), (-64, 11), (-60, 9), (-52, 6), (-50, 0),
     (-44, -3), (-35, -8), (-38, -15), (-40, -21), (-46, -24), (-52, -28),
     (-56, -33), (-58, -38), (-63, -41), (-65, -45), (-67, -50), (-69, -54),
     (-72, -54), (-73, -49), (-73, -42), (-72, -35), (-71, -28), (-70, -20),
     (-73, -16), (-77, -12), (-80, -6), (-81, -1), (-80, 3), (-77, 6)],
    [(-10, 36), (-9, 43), (-2, 44), (-5, 48), (0, 50), (4, 52), (8, 55),
     (8, 58), (11, 59), (16, 60), (21, 65), (28, 69), (38, 67), (44, 65),
     (42, 60), (38, 55), (33, 52), (30, 50), (28, 46), (31, 46), (36, 43),
     (30, 41), (25, 40), (22, 38), (19, 41), (15, 40), (13, 44), (10, 44),
     (7, 44), (3, 42), (0, 39), (-2, 37)],
    [(-6, 35), (0, 36), (10, 37), (19, 32), (28, 31), (32, 30), (35, 28),
     (38, 22), (40, 16), (43, 12), (48, 11), (51, 12), (48, 5), (42, -2),
     (40, -10), (38, -18), (36, -22), (33, -27), (28, -33), (20, -35),
     (17, -32), (15, -27), (13, -18), (12, -10), (9, -2), (9, 4), (4, 6),
     (-2, 5), (-8, 5), (-13, 8), (-17, 12), (-17, 16), (-16, 21), (-15, 26),
     (-13, 29), (-10, 31)],
    [(44, -13), (47, -14), (50, -17), (48, -22), (46, -25), (44, -23),
     (43, -18), (44, -15)],
    [(26, 41), (30, 46), (38, 47), (48, 46), (58, 47), (70, 49), (82, 50),
     (95, 50), (108, 51), (120, 53), (132, 49), (140, 48), (142, 52),
     (150, 55), (158, 52), (163, 58), (171, 60), (178, 65), (175, 68),
     (160, 70), (145, 72), (128, 72), (110, 74), (95, 76), (82, 73),
     (70, 72), (60, 69), (52, 68), (46, 65), (42, 60), (38, 54), (34, 50),
     (30, 46)],
    [(35, 31), (39, 31), (43, 29), (48, 30), (50, 28), (54, 26), (57, 27),
     (59, 22), (58, 19), (55, 17), (51, 15), (46, 13), (43, 12), (41, 15),
     (39, 20), (37, 24), (35, 28)],
    [(68, 23), (72, 20), (73, 16), (76, 9), (78, 8), (80, 13), (84, 19),
     (88, 22), (91, 22), (89, 24), (84, 24), (78, 25), (72, 24)],
    [(92, 22), (95, 17), (97, 12), (99, 8), (101, 4), (103, 2), (102, 8),
     (100, 13), (98, 16), (100, 19), (103, 20), (106, 20), (108, 15),
     (109, 12), (107, 9), (105, 10), (104, 17), (102, 22), (98, 24),
     (94, 24)],
    [(95, 5), (99, 2), (103, -2), (106, -5), (102, -3), (98, 1)],
    [(106, -6), (110, -7), (114, -8), (114, -9), (106, -8)],
    [(109, 1), (113, 3), (117, 5), (119, 2), (117, -2), (113, -3), (110, -2)],
    [(113, -22), (114, -28), (115, -33), (118, -35), (124, -33), (129, -32),
     (132, -32), (135, -35), (138, -36), (141, -38), (145, -38), (149, -37),
     (150, -33), (151, -28), (153, -25), (153, -20), (150, -15), (146, -11),
     (142, -11), (141, -14), (142, -17), (140, -18), (136, -15), (135, -12),
     (132, -11), (129, -14), (126, -14), (122, -17), (118, -20)],
    [(167, -45), (170, -43), (173, -40), (176, -38), (174, -40), (172, -43),
     (169, -46)],
]


def draw_map(cv, fonts, x, y, w, h):
    def proj(lon, lat):
        return (x + (lon + 175) / 355.0 * w, y + (82 - lat) / 140.0 * h)

    for poly in CONTINENT_POLYS:
        pts = [proj(lon, lat) for (lon, lat) in poly]
        cv.polygon(pts, LAND)
        for i in range(len(pts)):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % len(pts)]
            cv.line(x1, y1, x2, y2, LAND_EDGE, 1)
    for (lon, lat, label) in PINS:
        px, py = proj(lon, lat)
        cv.circle(px, py, 4.5, RED)
        cv.circle(px, py, 1.8, (255, 255, 255))
        label_w = text_width(fonts["sans_b"], label, 8)
        lx = px + 7 if px + 7 + label_w < x + w else px - 7 - label_w
        draw_text(cv, lx, py + 3, label, fonts["sans_b"], 8, NAVY)


def also_panel(cv, fonts, x, y, w):
    """Draw the 'ALSO ON MONDAY' header and its two numbered columns."""
    draw_text(cv, x, y + 20, "ALSO ON MONDAY", fonts["sans_b"], 14.5,
              NAVY, spacing=1.5)
    draw_text_right(cv, W - MARGIN, y + 20, "22 MORE STORIES",
                    fonts["sans_b"], 10.5, RED, spacing=1.5)
    cv.rect(x, y + 28, W - MARGIN - x, 2.5, GOLD)
    col_w = (W - MARGIN - x - 16) // 2
    half = (len(ALSO) + 1) // 2
    for col in range(2):
        items = ALSO[col * half:(col + 1) * half]
        cx = x + col * (col_w + 16)
        ry = y + 52
        for j, item in enumerate(items):
            draw_text(cv, cx, ry, str(col * half + j + 1), fonts["sans_b"],
                      10.5, RED)
            size = fit_size(fonts["serif"], item, 11, col_w - 24, floor=9.0)
            draw_text(cv, cx + 24, ry, item, fonts["serif"], size, INK)
            ry += 25


def render():
    fonts = load_fonts()
    cv = Canvas(W, H, PAPER)

    # Masthead ------------------------------------------------------------
    cv.rect(0, 0, W, 132, NAVY)
    cv.rect(0, 132, W, 4, GOLD)
    draw_text(cv, MARGIN, 38, "THE DAY IN WORLD NEWS", fonts["sans_b"], 18,
              GOLD, spacing=5)
    draw_text_right(cv, W - MARGIN, 38,
                    "%d STORIES · %d CONTINENTS" % (STORY_COUNT, CONTINENTS),
                    fonts["sans_b"], 14, GOLD, spacing=2)
    draw_text(cv, MARGIN, 93, "Monday 23 November 2026", fonts["serif_b"], 52,
              (255, 255, 255))
    draw_text(cv, MARGIN, 121,
              "The whole world in one day -- as the wires reported it on "
              "Monday, and no other day.",
              fonts["serif_i"], 15.5, (206, 196, 168))

    # Lead cards ----------------------------------------------------------
    grid_w = W - 2 * MARGIN
    card_w = (grid_w - 36) // 4
    lead_y, lead_h = 150, 238
    for i, story in enumerate(LEADS):
        lead_card(cv, fonts, MARGIN + i * (card_w + 12), lead_y, card_w,
                  lead_h, i + 1, story)

    # Mid cards -----------------------------------------------------------
    mid_y, mid_h = 402, 112
    for i, story in enumerate(MIDS):
        row, col = divmod(i, 4)
        mid_card(cv, fonts, MARGIN + col * (card_w + 12),
                 mid_y + row * (mid_h + 8), card_w, mid_h, story)

    # Bottom band ---------------------------------------------------------
    bot_y = 646
    draw_text(cv, MARGIN, bot_y + 20, "WHERE IT HAPPENED", fonts["sans_b"],
              14.5, NAVY, spacing=1.5)
    cv.rect(MARGIN, bot_y + 28, 336, 2.5, GOLD)
    draw_map(cv, fonts, MARGIN, bot_y + 38, 336, 282)
    cv.rect(374, bot_y + 4, 1.5, 318, BORDER)
    also_panel(cv, fonts, 386, bot_y, W - MARGIN - 386)

    # Footer --------------------------------------------------------------
    cv.rect(MARGIN, 980, grid_w, 2, GOLD)
    draw_text(cv, MARGIN, 1008,
              "SOURCES: REUTERS · AP · XINHUA · BILD · SCMP · RNZ · PBS · "
              "EL COLOMBIANO · INDEPENDENT · ARAB NEWS",
              fonts["sans_b"], 9.5, (110, 100, 80))
    box_x, box_y, box_w, box_h = 636, 986, 362, 32
    cv.rect(box_x, box_y, box_w, 1.5, RED)
    cv.rect(box_x, box_y + box_h - 1.5, box_w, 1.5, RED)
    cv.rect(box_x, box_y, 1.5, box_h, RED)
    cv.rect(box_x + box_w - 1.5, box_y, 1.5, box_h, RED)
    draw_text(cv, box_x + 10, box_y + 14, "DATE CHECK", fonts["sans_b"], 9.5,
              RED, spacing=1)
    draw_text(cv, box_x + 92, box_y + 14,
              "All 34 stories reported Mon 23 Nov 2026.",
              fonts["serif"], 10, INK)
    draw_text(cv, box_x + 10, box_y + 27,
              "Off-day items found in research were discarded.",
              fonts["serif"], 10, INK)
    return cv


def main():
    cv = render()
    out_dir = "/output"
    os.makedirs(out_dir, exist_ok=True)
    size = save_png(cv, os.path.join(out_dir, "entry.png"))
    print("wrote /output/entry.png bytes=%d" % size)


if __name__ == "__main__":
    main()

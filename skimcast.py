#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "pillow"]
# ///
"""skimcast VIDEO [--out DIR] [--min-change PCT] [--max N]: turn a screen recording
into the few timestamped screenshots an AI agent needs to read it.

A screenshot is kept when the screen has changed and then stopped changing, so a page
that loads or scrolls is caught once it settles. During a long continuous change
(scrolling, a playing video) one is kept every 2 seconds. Mouse movement alone is not
a change, but when the mouse stops somewhere new for 1.5 seconds that moment is kept,
because a resting mouse is usually pointing at something. When there are more than
--max, the screenshots most like the one before them are dropped first. Each screenshot
gets a band on top with its time since the recording began, and every 9 consecutive
screenshots are also laid out as one 3x3 overview grid.

Writes NNN_MMmSS.Ss.png files, grid_*.png files and index.md to DIR (default: VIDEO_skimcast next to the
video) and prints one line per screenshot."""
import argparse, html, json, math, subprocess, sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont

SAMPLE_FPS = 5           # frames per second looked at; screen recordings rarely need more
WIDTH = 640              # analysis width; the screenshots themselves stay full size
PIXEL_DELTA = 25         # grey-level difference that counts as a changed pixel, above video noise
BURST_SECONDS = 2.0      # during non-stop change, keep one screenshot this often
MOUSE_REST_SECONDS = 1.5 # a mouse that stops this long somewhere new is pointing at something
CURSOR_NOISE = 8         # changed pixels below this are video noise, above it the cursor moved
CURSOR_PIXELS = 100      # a change bigger than this between two samples is not just the cursor
CURSOR_SPREAD = 0.1      # nor is a change spread wider than this share of the screen
MOUSE_TRAVEL = 0.03      # the mouse must have travelled this share of the screen width
PANEL_EDGE = 0.04        # top and bottom share of the screen holding panels, clocks and tray icons
GRID_SIDE = 3            # an overview grid holds GRID_SIDE x GRID_SIDE consecutive screenshots
GRID_WIDTH = 2000        # the widest image Claude Code shows without shrinking it
GRID_GAP = 8
MARK_BELOW = 2.0         # box what changed when less than this percent of the screen changed
MARK_MOST = 5            # more separate changed areas than this are scattered, and boxes would be noise
MARK_CELL = 8            # changed pixels this close (analysis pixels) belong to one area
CURSOR_SIZE = 40         # a changed area no bigger than this many screen pixels is the cursor
MARK_COLOR = "#ff00ff"


def clock(seconds):
    return f"{int(seconds // 60):02d}:{seconds % 60:04.1f}"


def probe(video):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,width,height:format=duration", "-of", "json", video],
                         capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    picture = next(s for s in info["streams"] if s["codec_type"] == "video")
    has_sound = any(s["codec_type"] == "audio" for s in info["streams"])
    return picture["width"], picture["height"], float(info["format"]["duration"]), has_sound


def samples(video, height):
    size = WIDTH * height
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-i", video, "-vf",
                             f"fps={SAMPLE_FPS},scale={WIDTH}:{height},format=gray",
                             "-f", "rawvideo", "-"], stdout=subprocess.PIPE)
    index = 0
    while len(chunk := proc.stdout.read(size)) == size:
        yield index / SAMPLE_FPS, np.frombuffer(chunk, np.uint8).reshape(height, WIDTH).astype(np.int16)
        index += 1
    proc.wait()


def changed(a, b):
    return int((np.abs(a - b) > PIXEL_DELTA).sum())


def mouse_step(a, b):
    """Compare two samples outside the top and bottom panels, where clocks and tray
    icons change on their own. Returns None when nothing moved, (x, y) when only
    something cursor-sized moved in one spot, and False when anything else changed."""
    edge = int(len(a) * PANEL_EDGE)
    ys, xs = np.nonzero(np.abs(a[edge:-edge] - b[edge:-edge]) > PIXEL_DELTA)
    if len(xs) <= CURSOR_NOISE:
        return None
    cursor_sized = len(xs) <= CURSOR_PIXELS and max(np.ptp(xs), np.ptp(ys)) <= CURSOR_SPREAD * len(a[0])
    return (xs.mean(), ys.mean()) if cursor_sized else False


def pick(video, height, threshold):
    """Return the picks [(time, frame, changed_pixels_vs_previous_pick, why)], how much each
    sample changed [(time, percent of the screen, mouse moved)], and a log of every decision
    [(time, text)] for the report."""
    percent = lambda pixels: 100 * pixels / (WIDTH * height)
    picks, previous, change_started, still_since, trail = [], None, None, 0.0, []
    series, log, rest_judged = [], [], True
    for t, frame in samples(video, height):
        if not picks:
            picks.append((t, frame, None, "start of recording"))
            series.append((t, 0.0, False))
            log.append((t, "Kept: start of recording."))
            previous = frame
            continue
        step = mouse_step(frame, previous)
        series.append((t, percent(changed(frame, previous)), bool(step)))
        if step is not None:
            still_since, rest_judged = t, False
            trail.append(step)
        since_pick = changed(frame, picks[-1][1])
        if since_pick >= threshold:
            change_started = t if change_started is None else change_started
            settled = changed(frame, previous) < threshold
            if settled or t - change_started >= BURST_SECONDS:
                why = "screen changed" if settled else "screen still changing"
                picks.append((t, frame, since_pick, why))
                log.append((t, f"Kept: {percent(since_pick):.1f}% of the screen differs from the last screenshot, "
                               + ("and it was already still." if settled and t == change_started
                                  else f"and it settled after {t - change_started:.1f} s." if settled
                                  else f"and it was still changing after {BURST_SECONDS:g} s.")))
                change_started, trail, rest_judged = None if settled else t, [], True
        else:
            change_started = None
            if t - still_since >= MOUSE_REST_SECONDS and trail and not rest_judged:
                # The mouse rested, its last step was the cursor (not the mouse leaving the screen
                # while something else flickered) and it travelled far enough to mean something.
                rest_judged = True
                path = [step for step in trail if step]
                travelled = np.ptp(path, axis=0).max() / WIDTH if path else 0
                if not trail[-1]:
                    log.append((t, "Not kept: movement stopped, but the last change was bigger than a cursor, "
                                   "so it may be the mouse leaving the screen."))
                elif travelled < MOUSE_TRAVEL:
                    log.append((t, f"Not kept: the mouse stopped after moving {100 * travelled:.1f}% of the screen "
                                   f"width since the last screenshot; it must move {100 * MOUSE_TRAVEL:g}%."))
                else:
                    picks.append((t, frame, since_pick, f"mouse stopped here for {MOUSE_REST_SECONDS:g} s"))
                    log.append((t, f"Kept: the mouse moved {100 * travelled:.0f}% of the screen width and "
                                   f"stopped for {MOUSE_REST_SECONDS:g} s."))
                    trail = []
        previous = frame
    if change_started is not None:          # still changing when the recording ended
        picks.append((t, frame, changed(frame, picks[-1][1]), "screen still changing at the end"))
        log.append((t, "Kept: the screen was still changing when the recording ended."))
    return picks, series, log


def thin(picks, limit, log):
    """Drop the picks that differ least from the one before until `limit` remain."""
    picks = list(picks)
    while len(picks) > limit:
        drop = min(range(1, len(picks)), key=lambda i: picks[i][2])
        log.append((picks[drop][0], f"Dropped to keep at most {limit} screenshots: it was the most like the "
                                    "screenshot before it."))
        del picks[drop]
        if drop < len(picks):
            t, frame, _, why = picks[drop]
            picks[drop] = (t, frame, changed(frame, picks[drop - 1][1]), why)
    return picks


def changed_areas(a, b, width):
    """Boxes (x0, y0, x1, y1), in analysis pixels, around what changed between two
    screenshots, when the change is small enough to miss. Empty when it is big enough to
    see anyway, or scattered over too many places. Panels and the cursor are left out."""
    height = len(a)
    edge = int(height * PANEL_EDGE)
    mask = np.abs(a - b) > PIXEL_DELTA
    mask[:edge], mask[height - edge:] = False, False
    if not mask.any() or 100 * mask.mean() >= MARK_BELOW:
        return []
    rows, cols = height // MARK_CELL, WIDTH // MARK_CELL
    cells = mask[:rows * MARK_CELL, :cols * MARK_CELL].reshape(rows, MARK_CELL, cols, MARK_CELL).any(axis=(1, 3))
    seen, boxes, cursor = np.zeros_like(cells), [], CURSOR_SIZE * WIDTH / width
    for start in zip(*np.nonzero(cells)):
        if seen[start]:
            continue
        seen[start], stack, members = True, [start], []
        while stack:                     # flood fill one area of touching cells
            row, col = stack.pop()
            members.append((row, col))
            for near in ((row + dr, col + dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1)):
                if 0 <= near[0] < rows and 0 <= near[1] < cols and cells[near] and not seen[near]:
                    seen[near] = True
                    stack.append(near)
        member_rows, member_cols = zip(*members)
        top, left = min(member_rows) * MARK_CELL, min(member_cols) * MARK_CELL
        ys, xs = np.nonzero(mask[top:(max(member_rows) + 1) * MARK_CELL, left:(max(member_cols) + 1) * MARK_CELL])
        box = (left + xs.min(), top + ys.min(), left + xs.max() + 1, top + ys.max() + 1)
        # The cursor is small in both directions; a sliver one or two pixels thin is an edge or
        # border changing shade, such as a window title bar when focus moves.
        if max(box[2] - box[0], box[3] - box[1]) > cursor and min(box[2] - box[0], box[3] - box[1]) > 2:
            boxes.append(box)
    return boxes if len(boxes) <= MARK_MOST else []


def draw_boxes(picture, boxes, line):
    """Magenta rectangles just outside each box, which is in analysis pixels."""
    scale, draw = picture.width / WIDTH, ImageDraw.Draw(picture)
    for x0, y0, x1, y1 in boxes:
        draw.rectangle((x0 * scale - 2 * line, y0 * scale - 2 * line, x1 * scale + 2 * line, y1 * scale + 2 * line),
                       outline=MARK_COLOR, width=line)
    return picture


def labelled(picture, label, band):
    """The picture with a black band on top holding the label."""
    page = Image.new("RGB", (picture.width, picture.height + band), "black")
    page.paste(picture, (0, band))
    font = ImageFont.load_default(band * 2 // 3)
    ImageDraw.Draw(page).text((band // 3, band // 2), label, fill="white", font=font, anchor="lm")
    return page


def save(video, t, width, height, label, path, boxes):
    """Save the full-size screenshot, with boxes around small changes, and return a small
    copy for the overview grids."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    shot = Image.frombytes("RGB", (width, height), raw[:width * height * 3])
    cell = min(width, (GRID_WIDTH - GRID_GAP * (GRID_SIDE - 1)) // GRID_SIDE)
    small = draw_boxes(shot.resize((cell, round(height * cell / width)), Image.LANCZOS), boxes, 3)
    labelled(draw_boxes(shot, boxes, max(3, width // 640)), label, max(36, height // 30)).save(path, compress_level=3)
    return small


def save_grids(cells, out):
    """Lay consecutive screenshots out GRID_SIDE to a row, GRID_SIDE rows to a grid.
    The last grid keeps its empty slots. Returns the grid file names."""
    per_grid, names = GRID_SIDE * GRID_SIDE, []
    for first in range(0, len(cells), per_grid):
        chunk = cells[first:first + per_grid]
        cell_w, cell_h = chunk[0].width, chunk[0].height
        grid = Image.new("RGB", (GRID_SIDE * cell_w + (GRID_SIDE - 1) * GRID_GAP,
                                 GRID_SIDE * cell_h + (GRID_SIDE - 1) * GRID_GAP), "#808080")
        for slot, cell in enumerate(chunk):
            x, y = slot % GRID_SIDE * (cell_w + GRID_GAP), slot // GRID_SIDE * (cell_h + GRID_GAP)
            grid.paste(cell, (x, y))
        name = f"grid_{first // per_grid + 1:02d}_{first + 1:03d}-{first + len(chunk):03d}.png"
        grid.save(out / name, compress_level=3)
        names.append(name)
    return names


REPORT_STYLE = """
:root { --ink: #1f2328; --soft: #59636e; --line: #d1d9e0; --paper: #ffffff; --bar: #8c959f;
        --mark: #1f2328; --mouse: #d97706; --limit: #cf222e; }
@media (prefers-color-scheme: dark) {
  :root { --ink: #e6edf3; --soft: #9198a1; --line: #3d444d; --paper: #0d1117; --bar: #656c76;
          --mark: #e6edf3; --mouse: #f0883e; --limit: #f85149; } }
body { margin: 0; background: var(--paper); color: var(--ink); font: 16px/1.5 system-ui, sans-serif; }
main { max-width: 1200px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 24px; margin: 0 0 8px; } h2 { font-size: 18px; margin: 40px 0 12px; }
p { margin: 0 0 8px; } .soft { color: var(--soft); font-size: 14px; }
a { color: inherit; }
.chart { display: grid; grid-template-columns: 56px 1fr; margin-top: 16px; }
.ylabels { position: relative; height: 180px; margin-top: 48px; font-size: 12px; color: var(--soft); }
.ylabels span { position: absolute; right: 8px; transform: translateY(-50%); }
.marks { position: relative; height: 48px; font-size: 13px; }
.marks a { position: absolute; transform: translateX(-50%); text-decoration: none; font-weight: 600; }
.marks a.low { top: 24px; }
.plot svg { display: block; width: 100%; border: 1px solid var(--line); }
.area { height: 180px; } .mouse { height: 16px; margin-top: 4px; }
.xlabels { position: relative; height: 24px; font-size: 12px; color: var(--soft); }
.xlabels span { position: absolute; top: 4px; transform: translateX(-50%); }
.shots { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }
.shot { border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
.shot img { display: block; width: 100%; border-bottom: 1px solid var(--line); }
.shot p { margin: 8px 12px; font-size: 14px; } .shot b { font-size: 16px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
td { padding: 6px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
td:first-child { white-space: nowrap; font-variant-numeric: tabular-nums; width: 1%; }
"""


def write_report(path, video, duration, size, settings, series, log, shots, grids):
    """A page for people: what was looked at, what was kept, and why."""
    esc, span = html.escape, max(duration, 1e-6)
    x = lambda t: 100 * t / span

    def y(percent):  # log scale, 0.01% of the screen at the bottom and 100% at the top
        return 100 - 100 * (math.log10(min(max(percent, 0.01), 100)) + 2) / 4

    bars = "".join(f"M{x(t):.3f} 100V{y(p):.2f}" for t, p, _ in series if p > 0)
    mouse = "".join(f"M{x(t):.3f} 0V100" for t, _, moved in series if moved)
    kept = "".join(f"M{x(t):.3f} 0V100" for t, *_ in shots)
    stroke = 'fill="none" vector-effect="non-scaling-stroke"'
    area = (f'<svg class="area" viewBox="0 0 100 100" preserveAspectRatio="none">'
            f'<path d="{bars}" stroke="var(--bar)" stroke-width="1" {stroke}/>'
            f'<path d="{kept}" stroke="var(--mark)" stroke-width="1.5" {stroke}/>'
            f'<path d="M0 {y(settings["min_change"]):.2f}H100" stroke="var(--limit)" stroke-width="1" '
            f'stroke-dasharray="4 4" {stroke}/></svg>')
    strip = (f'<svg class="mouse" viewBox="0 0 100 100" preserveAspectRatio="none">'
             f'<path d="{mouse}" stroke="var(--mouse)" stroke-width="1" {stroke}/></svg>')
    marks = "".join(f'<a href="#shot-{n}" class="{"low" if n % 2 == 0 else ""}" style="left:{x(t):.3f}%" '
                    f'title="{n}: {clock(t)}, {esc(why)}">{n}</a>' for n, (t, _, why) in enumerate(shots, 1))
    step = next(s for s in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600) if span / s <= 10)
    ticks = "".join(f'<span style="left:{x(t):.3f}%">{clock(t)[:-2]}</span>'
                    for t in range(0, int(span) + 1, step))
    ylabels = "".join(f'<span style="top:{y(p):.1f}%">{p:g}%</span>' for p in (100, 10, 1, 0.1, 0.01))
    cards = "".join(f'<div class="shot" id="shot-{n}"><a href="{esc(name)}"><img src="{esc(name)}" loading="lazy" '
                    f'alt="Screenshot {n} at {clock(t)}"></a><p><b>{n}</b> &nbsp; {clock(t)}<br>'
                    f'{esc(why[0].upper() + why[1:])}.</p></div>' for n, (t, name, why) in enumerate(shots, 1))
    number = {t: n for n, (t, *_) in enumerate(shots, 1)}
    rows = "".join(f'<tr><td>{clock(t)}</td><td>'
                   + (f'<a href="#shot-{number[t]}">Screenshot {number[t]}</a>. ' if t in number and text.startswith("Kept") else "")
                   + f'{esc(text)}</td></tr>' for t, text in sorted(log, key=lambda entry: entry[0]))
    grid_links = ", ".join(f'<a href="{esc(g)}">{esc(g)}</a>' for g in grids)
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>skimcast: {esc(video)}</title><style>{REPORT_STYLE}</style></head><body><main>
<h1>{esc(video)}</h1>
<p>skimcast looked at {len(series)} moments of this {clock(duration)} recording ({SAMPLE_FPS} per second,
{size}) and kept {len(shots)} screenshots.</p>
<p class="soft">It keeps a moment when at least {settings["min_change"]:g}% of the screen changed and then settled,
one every {BURST_SECONDS:g} s while the screen keeps changing, and one when the mouse moves at least
{100 * MOUSE_TRAVEL:g}% of the screen width and stops for {MOUSE_REST_SECONDS:g} s. At most {settings["max"]}.
When less than {MARK_BELOW:g}% of the screen changed, a magenta box marks what changed.</p>
<h2>What it looked at</h2>
<p class="soft">Grey bars: how much of the screen changed since the moment before (log scale). Red dashed line:
the {settings["min_change"]:g}% needed to count as a change. Dark lines and numbers: screenshots kept. Orange strip
below: the mouse moving.</p>
<div class="chart"><div class="ylabels">{ylabels}</div>
<div class="plot"><div class="marks">{marks}</div>{area}{strip}<div class="xlabels">{ticks}</div></div></div>
<h2>Screenshots kept</h2>
<p class="soft">The agent gets these, and the same ones laid out in overview grids: {grid_links}.</p>
<div class="shots">{cards}</div>
<h2>Decision log</h2>
<table>{rows}</table>
</main></body></html>
"""
    path.write_text(page)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("video")
    parser.add_argument("--out", help="output folder (default: VIDEO_skimcast next to the video)")
    parser.add_argument("--min-change", type=float, default=0.1, metavar="PCT",
                        help="percent of the screen that must change to keep a screenshot (default 0.1)")
    parser.add_argument("--max", type=int, default=40, help="most screenshots to keep (default 40)")
    args = parser.parse_args()

    video = Path(args.video).expanduser().resolve()
    width, height, duration, has_sound = probe(str(video))
    small_height = round(height * WIDTH / width / 2) * 2
    threshold = max(1, int(WIDTH * small_height * args.min_change / 100))
    picks, series, log = pick(str(video), small_height, threshold)
    picks = thin(picks, max(1, args.max), log)

    out = Path(args.out).expanduser().resolve() if args.out else video.with_name(video.stem + "_skimcast")
    out.mkdir(parents=True, exist_ok=True)
    for old in [*out.glob("[0-9][0-9][0-9]_*.png"), *out.glob("grid_*.png")]:
        old.unlink()
    shots, cells = [], []
    for number, (t, frame, pixels, why) in enumerate(picks, 1):
        name = f"{number:03d}_{int(t // 60):02d}m{t % 60:04.1f}s.png"
        boxes = changed_areas(picks[number - 2][1], frame, width) if number > 1 else []
        label = f"{clock(t)} since recording start   (of {clock(duration)})   screenshot {number} of {len(picks)}"
        if boxes:
            label += "   magenta box: changed since the screenshot before"
        cells.append(labelled(save(str(video), t, width, height, label, out / name, boxes), f"{number}   {clock(t)}", 30))
        if why.startswith("screen "):
            why = f"{100 * pixels / (WIDTH * small_height):.1f}% of the screen changed" + (
                ", and it was still changing" if why.endswith("changing") else "")
        if boxes:
            why += f"; a magenta box marks {'where' if len(boxes) == 1 else f'the {len(boxes)} places'}"
        shots.append((t, name, why))
        print(f"{clock(t)}  {out / name}  ({why})")

    grids = save_grids(cells, out)
    for grid in grids:
        print(f"overview  {out / grid}")

    sound = "The video has a sound track, which skimcast does not transcribe." if has_sound else ""
    lines = [f"# {video.name}", "",
             f"Recording length {clock(duration)}, {width}x{height}. {len(picks)} screenshots, taken when the "
             f"screen changed and settled, or when the mouse stopped to point at something. The mouse cursor "
             f"shows wherever it was on this screen. When less than {MARK_BELOW:g}% of the screen changed, a "
             f"magenta box marks what changed, so small changes are not missed. Times count from the start "
             f"of the recording. {sound}".rstrip(),
             "", f"For an overview, read the grids first. Each holds {GRID_SIDE * GRID_SIDE} consecutive "
             f"screenshots, left to right then down, labelled with their number and time: {', '.join(grids)}. "
             f"Then open the single screenshots where you need detail."]
    for t, name, why in shots:
        lines += ["", f"## {clock(t)}  {name}", "", why[0].upper() + why[1:] + "."]
    (out / "index.md").write_text("\n".join(lines) + "\n")
    write_report(out / "report.html", video.name, duration, f"{width}x{height}",
                 {"min_change": args.min_change, "max": max(1, args.max)}, series, log, shots, grids)
    print(f"\n{len(picks)} screenshots and {len(grids)} overview grid{'s' * (len(grids) != 1)} from {clock(duration)} of video. "
          f"Index: {out / 'index.md'}\nReport for people: {out / 'report.html'}")


if __name__ == "__main__":
    sys.exit(main())

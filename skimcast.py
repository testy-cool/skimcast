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
import argparse, json, subprocess, sys
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
    """Return [(time, frame, changed_pixels_vs_previous_pick, why)]."""
    picks, previous, change_started, still_since, trail = [], None, None, 0.0, []
    for t, frame in samples(video, height):
        if not picks:
            picks.append((t, frame, None, "start of recording"))
            previous = frame
            continue
        step = mouse_step(frame, previous)
        if step is not None:
            still_since = t
            trail.append(step)
        since_pick = changed(frame, picks[-1][1])
        if since_pick >= threshold:
            change_started = t if change_started is None else change_started
            settled = changed(frame, previous) < threshold
            if settled or t - change_started >= BURST_SECONDS:
                picks.append((t, frame, since_pick, "screen changed"))
                change_started, trail = None if settled else t, []
        else:
            change_started = None
            # The mouse rested, its last step was the cursor (not the mouse leaving the screen
            # while something else flickered) and it travelled far enough to mean something.
            path = [step for step in trail if step]
            travelled = np.ptp(path, axis=0).max() if path else 0
            if t - still_since >= MOUSE_REST_SECONDS and trail and trail[-1] and travelled >= MOUSE_TRAVEL * WIDTH:
                picks.append((t, frame, since_pick, f"mouse stopped here for {MOUSE_REST_SECONDS:g} s"))
                trail = []
        previous = frame
    if change_started is not None:          # still changing when the recording ended
        picks.append((t, frame, changed(frame, picks[-1][1]), "screen still changing at the end"))
    return picks


def thin(picks, limit):
    """Drop the picks that differ least from the one before until `limit` remain."""
    picks = list(picks)
    while len(picks) > limit:
        drop = min(range(1, len(picks)), key=lambda i: picks[i][2])
        del picks[drop]
        if drop < len(picks):
            t, frame, _, why = picks[drop]
            picks[drop] = (t, frame, changed(frame, picks[drop - 1][1]), why)
    return picks


def labelled(picture, label, band):
    """The picture with a black band on top holding the label."""
    page = Image.new("RGB", (picture.width, picture.height + band), "black")
    page.paste(picture, (0, band))
    font = ImageFont.load_default(band * 2 // 3)
    ImageDraw.Draw(page).text((band // 3, band // 2), label, fill="white", font=font, anchor="lm")
    return page


def save(video, t, width, height, label, path):
    """Save the full-size screenshot and return a small copy for the overview grids."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    shot = Image.frombytes("RGB", (width, height), raw[:width * height * 3])
    labelled(shot, label, max(36, height // 30)).save(path, compress_level=3)
    cell = min(width, (GRID_WIDTH - GRID_GAP * (GRID_SIDE - 1)) // GRID_SIDE)
    return shot.resize((cell, round(height * cell / width)), Image.LANCZOS)


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
    picks = thin(pick(str(video), small_height, threshold), max(1, args.max))

    out = Path(args.out).expanduser().resolve() if args.out else video.with_name(video.stem + "_skimcast")
    out.mkdir(parents=True, exist_ok=True)
    for old in [*out.glob("[0-9][0-9][0-9]_*.png"), *out.glob("grid_*.png")]:
        old.unlink()
    shots, cells = [], []
    for number, (t, _, pixels, why) in enumerate(picks, 1):
        name = f"{number:03d}_{int(t // 60):02d}m{t % 60:04.1f}s.png"
        label = f"{clock(t)} since recording start   (of {clock(duration)})   screenshot {number} of {len(picks)}"
        cells.append(labelled(save(str(video), t, width, height, label, out / name), f"{number}   {clock(t)}", 30))
        if why == "screen changed":
            why = f"{100 * pixels / (WIDTH * small_height):.1f}% of the screen changed"
        shots.append((t, name, why))
        print(f"{clock(t)}  {out / name}  ({why})")

    grids = save_grids(cells, out)
    for grid in grids:
        print(f"overview  {out / grid}")

    sound = "The video has a sound track, which skimcast does not transcribe." if has_sound else ""
    lines = [f"# {video.name}", "",
             f"Recording length {clock(duration)}, {width}x{height}. {len(picks)} screenshots, taken when the "
             f"screen changed and settled, or when the mouse stopped to point at something. The mouse cursor "
             f"shows wherever it was on this screen. Times count from the start of the recording. {sound}".rstrip(),
             "", f"For an overview, read the grids first. Each holds {GRID_SIDE * GRID_SIDE} consecutive "
             f"screenshots, left to right then down, labelled with their number and time: {', '.join(grids)}. "
             f"Then open the single screenshots where you need detail."]
    for t, name, why in shots:
        lines += ["", f"## {clock(t)}  {name}", "", why[0].upper() + why[1:] + "."]
    (out / "index.md").write_text("\n".join(lines) + "\n")
    print(f"\n{len(picks)} screenshots and {len(grids)} overview grid{'s' * (len(grids) != 1)} from {clock(duration)} of video. "
          f"Index: {out / 'index.md'}")


if __name__ == "__main__":
    sys.exit(main())

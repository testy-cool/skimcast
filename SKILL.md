---
name: skimcast
description: Use when the user gives you a screen recording or another video file (.mp4, .webm, .mkv, .mov) and asks you to watch it, read it, or find what they did or what went wrong in it. Turns the video into timestamped screenshots and 3x3 overview grids with the skimcast command, which you then read.
---

# Read a screen recording with skimcast

You cannot watch a video, but you can read screenshots. `skimcast` keeps the
moments where something happened and stamps each with its time since the
recording started.

```bash
skimcast /path/to/video.mp4
```

If the command is missing, install it with
`uv tool install git+https://github.com/testy-cool/skimcast`. It needs ffmpeg.

It prints one line per screenshot (time, path, why it was kept), then the
overview grids, and writes them with an `index.md` to `<video>_skimcast/` next
to the video.

## Read it

- Read `index.md`, then the grids (`grid_01_001-009.png` and on). Each lays out
  nine consecutive screenshots, left to right then down, labelled with number
  and time. Use them to see the whole recording and decide where to look
  closer.

- Open the single screenshots where you need detail, such as text, or all of
  them when the recording is short.

- The band on top of each screenshot says how far into the recording it was
  taken. Use those times when you report what you saw.

- A magenta box on a screenshot marks a small change since the screenshot
  before, such as a menu opening or a list scrolling. Look there first.

- A screenshot is kept when the screen changed and settled, every 2 seconds
  while it keeps changing, and when the mouse travels somewhere and stops for
  1.5 seconds. A resting mouse usually points at what the user means.

- Give the user the path of `report.html`. It shows them what skimcast looked
  at and why it kept each screenshot, so they can check what you were given.

- When the user describes a moment ("when I clicked Save", "at the end"),
  match it to a screenshot rather than guessing.

## When it is not enough

- A small change is missing, such as a few typed letters: rerun with
  `--min-change 0.03`.

- Too many screenshots: `--max 20`.

- You need one exact moment:

```bash
ffmpeg -v error -ss 42.5 -i video.mp4 -frames:v 1 at-42.5s.png
```

- skimcast does not transcribe sound. `index.md` says when the video has a
  sound track. Tell the user if the sound seems to matter.

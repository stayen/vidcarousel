# vidcarousel

Turn an audio file and a handful of same-size images into a music video. A
plain-text **scenario file** says which image appears when, for how long, and
how it blends into the next one; vidcarousel works out the full timeline from
the length of the audio and renders it with `ffmpeg`.

## Features

- **Audio-driven length** – the image *loop* repeats as often as needed to fill
  the audio exactly; the last image is cut short where the audio ends.
- **Crossfades** between images using any of ffmpeg's `xfade` transitions, set
  globally or per image; hard cuts where you want them.
- **Silent tail** – optional `credits` and `end` images shown after the audio has
  finished.
- **Audio fade-out** before the silent tail, or a freeze on the last loop image
  instead of repeating.
- **Preview before rendering** – `--dry-run` prints the resolved timeline,
  `--validate-only` checks the scenario and the images without encoding.
- Clear, numbered error codes with stable exit codes for scripting.
- Output is MP4 (H.264 + AAC, `yuv420p`, fast-start) by default; codec, quality,
  frame rate and container are configurable.

## Requirements and installation

- Linux (developed for Ubuntu / Debian-like distributions), Python 3.12 or newer.
- `ffmpeg` and `ffprobe` (tested with ffmpeg 6.1.1):

  ```bash
  sudo apt install ffmpeg          # Ubuntu / Debian
  sudo dnf install ffmpeg          # RHEL 9 / AlmaLinux 9 (needs RPM Fusion or similar)
  ```

Install from a clone of the repository, preferably in a virtual environment:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install .
vidcarousel --help
```

## Quick start

```text
# scenario.txt (the images live next to it)
start,5                 intro.png
loop,4,1.5              image1.png
loop,6,2,dissolve       image2.png
loop,8                  image3.png
credits,10,1.5          credits.png
end,5,1.5               final.png
```

```bash
vidcarousel -ai song.mp3 -vo clip.mp4 -sf scenario.txt
```

All images must have identical pixel dimensions. An existing output file is
never overwritten – delete it first.

## Scenario file format

One entry per line. Blank lines and lines starting with `#` are ignored.

```
type,display_time[,xfade_time[,transition]]  filename
```

The parameters contain no spaces. They are separated from the filename by one or
more spaces or tabs; the filename is the rest of the line, so it may contain
spaces. Filenames are relative to the directory containing the scenario file.

| Field | Required | Default | Meaning |
|---|---|---|---|
| `type` | yes | – | `start`, `loop`, `credits` or `end` (lowercase). |
| `display_time` | yes | – | Seconds the image is on screen, **including** its incoming crossfade. Must be > 0. |
| `xfade_time` | no | `0` (hard cut) | Seconds over which the previous image fades into this one. Must be ≥ 0 and < `display_time`. Not allowed on `start`. |
| `transition` | no | `--transition` value (`fade`) | ffmpeg `xfade` transition name for this image's crossfade. Ignored when `xfade_time` is 0. |
| `filename` | yes | – | Image file. |

### Entry types

| Type | How many | Position | Sound during display |
|---|---|---|---|
| `start` | exactly 1 | first | yes |
| `loop` | 1 or more | after `start` | yes |
| `credits` | 0 or 1 | after all `loop` entries | **no** |
| `end` | 0 or 1 | last | **no** |

### Transition names

<!-- transitions:start -->
`fade`, `fadeblack`, `fadewhite`, `distance`, `wipeleft`, `wiperight`, `wipeup`, `wipedown`, `slideleft`, `slideright`, `slideup`, `slidedown`, `circlecrop`, `rectcrop`, `circleclose`, `circleopen`, `horzclose`, `horzopen`, `vertclose`, `vertopen`, `diagbl`, `diagbr`, `diagtl`, `diagtr`, `hlslice`, `hrslice`, `vuslice`, `vdslice`, `dissolve`, `pixelize`, `radial`, `hblur`, `wipetl`, `wipetr`, `wipebl`, `wipebr`, `squeezeh`, `squeezev`, `smoothleft`, `smoothright`, `smoothup`, `smoothdown`
<!-- transitions:end -->

This is the standard set from ffmpeg's `xfade` filter. Newer ffmpeg builds offer a
few more names (for example `zoomin` or `fadegrays`); those are not accepted yet.

### Validation rules

| Rule | Error |
|---|---|
| Exactly one `start`, and it is the first entry | `VC1001` |
| At least one `loop` entry follows `start` | `VC1002` |
| `credits` (at most one) comes after all `loop` entries | `VC1003` |
| `end` (at most one) is the last entry | `VC1004` |
| Only the four type names are accepted; every line has a filename and at most four comma-separated fields | `VC1005` |
| `display_time` is a positive number | `VC1006` |
| `xfade_time` is ≥ 0 and < `display_time` | `VC1007` |
| `start` has no crossfade | `VC1008` |
| `transition` is a known xfade name | `VC1009` |
| Every image exists and is readable | `VC2001` |
| All images have identical pixel dimensions | `VC2002` |

## How the timeline is computed

`display_time` is an image's *total* time on screen, including the crossfade
that brings it in. Each image starts `xfade_time` before the previous one ends,
so an image after the first contributes `display_time − xfade_time` to the total
length. For the quick-start scenario:

| Image | On screen | Why |
|---|---|---|
| `intro.png` | 0.0 – 5.0 | `start`, 5 s |
| `image1.png` | 3.5 – 7.5 | 4 s; starts 1.5 s before `intro` ends (the crossfade) |
| `image2.png` | 5.5 – 11.5 | 6 s; starts 2 s before `image1` ends |
| `image3.png` | 11.5 – 19.5 | 8 s; hard cut, starts exactly when `image2` ends |

One pass through the carousel therefore moves the timeline on by
(4 − 1.5) + (6 − 2) + 8 = 14.5 s.

- The **audible section** is exactly as long as the audio: `start` first, then the
  `loop` images in order, cycling from the first again as long as audio remains.
  The last image shown is cut short where the audio ends.
- With `--no-loop-repeat` the carousel runs once and the last loop image is held
  for the rest of the audio instead.
- If the audio is shorter than the `start` image, that image is shortened to the
  audio length and no loop images appear.
- The **silent tail** (`credits`, then `end`) follows. Each one fades in over the
  end of the image before it, so a `credits` crossfade overlaps the last moments
  of the audio. If the audio happens to end so soon after an image appears that
  the crossfade would be longer than that image's remaining time, the crossfade is
  shortened to fit.
- Otherwise a crossfade longer than the image it fades from (for example a
  `loop` entry with a 3 s fade after a 2 s one) is an error, `VC1007`, reported
  when the timeline is computed.

Use `--dry-run` to see the result before rendering.

## Command-line reference

```
vidcarousel -ai AUDIO -vo VIDEO -sf SCENARIO [options]
```

| Option | Default | Description |
|---|---|---|
| `-ai`, `--audio-input` | required | Input audio file (any format ffmpeg can decode). |
| `-vo`, `--video-output` | required | Output video file. Must not exist (when rendering). |
| `-sf`, `--scenario-file` | required | Scenario file. |
| `-of`, `--output-format` | – | Container format. Used when given, or when the output filename has no extension (then `mp4`); otherwise ffmpeg infers it from the extension. |
| `--codec` | `libx264` | ffmpeg video encoder. |
| `--audio-codec` | `aac` | ffmpeg audio encoder. |
| `--crf` | `23` | Constant Rate Factor (quality; lower is better). |
| `--fps` | `25` | Output frame rate. |
| `--transition` | `fade` | Default xfade transition for entries that don't name their own. |
| `--audio-fade-out` | `0` | Seconds over which the audio fades out at the end of the audible section (0 = hard cut). |
| `--no-loop-repeat` | off | Hold the last loop image instead of cycling the carousel. |
| `--dry-run` | off | Print the resolved timeline and exit. |
| `--validate-only` | off | Check the scenario, input files and image sizes, then exit. |
| `-v`, `--verbose` | off | Log at DEBUG level. |
| `--ffmpeg` | `ffmpeg` | Path to the ffmpeg binary. |
| `--ffprobe` | `ffprobe` | Path to the ffprobe binary. |

Results (`Wrote …`, the dry-run table, `OK: …`) go to **stdout**. Logs, including
the full ffmpeg command line, and error messages go to **stderr**.
`--dry-run` and `--validate-only` don't need a free output path.

## Examples

**Basic render**

```bash
vidcarousel -ai song.mp3 -vo clip.mp4 -sf scenario.txt
```

**Crossfades with a different default transition** – `image2` keeps its own
`dissolve`, every other crossfade becomes `fadeblack`:

```bash
vidcarousel -ai song.mp3 -vo clip.mp4 -sf scenario.txt --transition fadeblack
```

**Fade the audio out over 3 s, freeze on the last image, higher quality**

```bash
vidcarousel -ai song.mp3 -vo clip.mp4 -sf scenario.txt \
    --audio-fade-out 3 --no-loop-repeat --crf 18 --fps 30
```

**Preview the timeline** (30 s of audio and the quick-start scenario):

```console
$ vidcarousel -ai song.wav -vo out.mp4 -sf scenario.txt --dry-run
=== vidcarousel dry-run ===
Audio duration: 0:00:30.000
Loop carousel: 3 images, single pass 14.500 sec, repeats ~1.7x

 Timestamp    Transition         Display  File
 0:00:00.000  —                  5.000 s  intro.png
 0:00:03.500  fade 1.500 s       4.000 s  image1.png
 0:00:05.500  dissolve 2.000 s   6.000 s  image2.png
 0:00:11.500  cut                8.000 s  image3.png
 0:00:18.000  fade 1.500 s       4.000 s  image1.png   [loop 2]
 0:00:20.000  dissolve 2.000 s   6.000 s  image2.png   [loop 2]
 0:00:26.000  cut                4.000 s  image3.png   [loop 2]
 0:00:30.000  — audio ends —
 0:00:28.500  fade 1.500 s      10.000 s  credits.png  [silent]
 0:00:37.000  fade 1.500 s       5.000 s  final.png    [silent]

Total video duration: 0:00:42.000
```

The audio ends at 0:00:30; the credits fade in over its last 1.5 s, which is why
their timestamp is earlier than the `audio ends` line.

**Check everything without encoding**

```console
$ vidcarousel -ai song.mp3 -vo clip.mp4 -sf scenario.txt --validate-only
OK: 6 scenario entries, 6 images of 320x180
```

If one of the images has a different size:

```console
$ vidcarousel -ai song.mp3 -vo clip.mp4 -sf scenario.txt --validate-only
error VC2002: all images must have the same dimensions: intro.png is 320x180, but odd.png is 160x90
$ echo $?
2
```

## Errors and exit codes

Errors are printed to stderr as `error VCnnnn: message` and the process exits with
the code below. The exit codes are stable.

| Code | Exit | Category | Description |
|---|---|---|---|
| `VC1001` | 1 | Scenario | `start` entry missing or not first. |
| `VC1002` | 1 | Scenario | No `loop` entries found. |
| `VC1003` | 1 | Scenario | `credits` entry in wrong position. |
| `VC1004` | 1 | Scenario | `end` entry not last. |
| `VC1005` | 1 | Scenario | Unknown entry type. |
| `VC1006` | 1 | Scenario | `display_time` not a positive number. |
| `VC1007` | 1 | Scenario | `xfade_time` out of valid range. |
| `VC1008` | 1 | Scenario | `start` entry has non-zero `xfade_time`. |
| `VC1009` | 1 | Scenario | Unknown transition name. |
| `VC2001` | 2 | Input | Image file not found or unreadable. |
| `VC2002` | 2 | Input | Image dimension mismatch across entries. |
| `VC2003` | 2 | Input | Audio file not found or unreadable. |
| `VC2004` | 2 | Input | Scenario file not found or unreadable. |
| `VC3001` | 3 | ffmpeg | `ffmpeg` binary not found. |
| `VC3002` | 3 | ffmpeg | `ffprobe` binary not found or probe failed. |
| `VC3003` | 3 | ffmpeg | `ffmpeg` execution failed (non-zero exit). |
| `VC4001` | 4 | Output | Output path not writable or parent dir missing. |
| `VC4002` | 4 | Output | Output file already exists (no `--overwrite` flag). |

A mistake in the command-line options themselves (a missing required option, a
negative `--crf`, …) is reported by the argument parser with its usage message
and also exits with status 2.

If ffmpeg fails part-way through a render, the partially written output file is
removed.

## Known limitations

### Very long audio

The video is rendered by a single ffmpeg invocation that opens **one looped
input per timeline slot** (every appearance of an image in the carousel, so
the number of slots grows with the audio length divided by the carousel's
per-image time). ffmpeg's memory use and run time grow quickly with the
number of inputs.

Measured with ffmpeg 6.1.1 and a 3-image carousel (3 s per image, 1 s
crossfades), 64×48 images:

| Audio length | Timeline slots | Render time | Peak ffmpeg memory |
|---|---|---|---|
| 1 min | 26 | 2 s | 0.1 GB |
| 2.5 min | 64 | 7 s | 0.2 GB |
| 5 min | 129 | 14 s | 0.3 GB |
| 10 min | 257 | 39 s | 0.6 GB |
| 60 min | 1,543 | did not finish in 9 min | ~3 GB and still growing |

Roughly **2 MB of memory per slot**, and the time per slot rises with the slot
count. Up to about 10 minutes of audio is comfortable; beyond about 30
minutes expect very long renders or memory exhaustion. Longer per-image
display times mean fewer slots, so they stretch these limits proportionally.

When the filter graph becomes too long for one command-line argument (about
an hour of audio at 3 s per image), vidcarousel passes it to ffmpeg through a
temporary `-filter_complex_script` file instead, so such runs do start — but
they are subject to the limits above. (ffmpeg 7.0 may deprecate that flag in
favour of `-/filter_complex`; it is still the right choice for the 4.x–6.x
builds shipped by Debian and Ubuntu.)

For very long audio, render in sections, or use a carousel with longer
per-image display times.

### Frame rounding

Each image's on-screen time is rounded to whole frames, so the video may end
up to a few frames (about 0.05 s at 25 fps) longer than the audio-driven
timeline.

## Development

```bash
pip install -e ".[dev]"
pytest                      # unit tests (no ffmpeg needed)
pytest -m integration       # end-to-end tests with real ffmpeg / ffprobe
ruff check . && ruff format --check .
mypy src tests
```

The specification the project was built from is in [`CLAUDE.md`](CLAUDE.md).

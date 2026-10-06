# Ceonggo

A portable Windows app for making music on your own graphics card, built on
[YuE2](https://github.com/multimodal-art-projection/YuE) (songs) and
SheetSage2 (listening to a recording).

Three tabs, plus a score editor:

- **Create** — pick a sound from chips (language, genre, mood, voice, instruments, tempo),
  write lyrics with `[Verse]` / `[Chorus]` tags, press **Create song**.
- **Cover** — choose a recording, let the AI write its melody down, then give it new words and a
  new sound. **Takes** makes several versions in one go.
- **Library** — every song, with a player, search, favourites, MP3/WAV/FLAC export, and
  **↻ Load settings** to make any song again.
- **Score editor** — see and edit the melody, chords and song parts as a piano roll.

The look and structure follow EasyAI: the same dark theme with an orange accent, a separate
**Ceonggo Setup** program, and English + 繁體中文.

## For users

1. Unzip `Ceonggo-v1.0.0.zip` somewhere with about 25 GB free (for example `D:\Ceonggo`), not inside
   OneDrive or Program Files.
2. Run **Ceonggo Setup.exe**. It downloads about 16.5 GB, largest files first, showing the step,
   amount, speed, time left, the current file and a checklist. Cancel any time; running it again
   carries on from where it stopped.
3. Run **Ceonggo.exe**.

### What you need

| | |
|---|---|
| Windows | 10 or 11, 64-bit |
| Graphics card | NVIDIA, 12 GB or more (16 GB comfortable, 24 GB fastest). RTX 30xx, 40xx and 50xx are all covered, as are workstation cards like the A4000 |
| Driver | R570 or newer |
| Disk | ~25 GB |

Speed on the RTX A4000 (16 GB) it was developed on: a one-minute song takes about 80 seconds,
a three-minute song about five minutes, and writing down a melody about 80 seconds.

### Making a song

**Create** writes the song in stages, which the progress card names: it plans a score (melody and
chords), performs it, mixes the sound, then renders the audio. Under **More options**:

- **How to write it** — plan melody and chords first (best), melody only, or sing straight away.
- **Let me see and edit the score before singing** — plans the score, opens the editor, and sings
  it when you press **Sing it now**.
- **Seed** — the dice makes a new one; tick **Keep this seed** to reproduce a song exactly.
- **Takes** — 1 to 4 versions in a row, each with a different seed.
- **Follow the style** — how strictly to follow the style words.

### Making a cover

1. Drop in a recording (**▶ Listen** plays it, **■ Stop** ends it).
2. **Write down the melody** — SheetSage2 writes the melody *and chords* as a score. You can also
   **Load a score file…** instead and skip the recording entirely.
3. New style and lyrics, then **Make cover**, with its own **Takes** box.

Covers are sung from the melody without chords, so the new style can re-harmonise; the chords stay
in the editor for reference.

### The score editor

Opens from a song card's **Score** button, from **See / edit the score** on the Cover tab, or after
"plan first" on Create. It is the same editor everywhere.

- **Piano roll** with note names, a keyboard, and a faded view of the other voice. Click to add a
  note, drag to move, drag its right edge to change length, Delete to remove, ↑/↓ to change pitch,
  Shift-drag to select several. Switch between the **Vocal** and **Instrument** lines.
- **Chords and parts** — click the chord row to type a chord (checked against what the engine
  understands), right-click a part to rename it or start a new one, right-click a bar number to
  insert or delete bars.
- **Key and tempo** — changing the key moves notes and chords together, warns past ±5 semitones,
  and shows the vocal range. Songs that change key partway through (common in transcriptions) are
  shown with the change marked on the ruler, and so are bars in another metre.
- **Play** — a plain synth preview of melody, instrument line and chords, so you can check the
  notes before spending minutes on a song.
- **Words (Cantonese)** — for a Cantonese song with lyrics, a lane under the chords shows the word
  expected on each vocal note with its Jyutping. Cantonese is sung on its tones: where the melody
  moves against a word's tone (倒字) the word is heard as another one, and the cell turns red
  (amber for a looser fit), following the 0243 rule lyricists use. Click a word to select its notes
  (↑/↓ then fixes it), choose which reading it has, swap in a common word that sounds the same, or
  edit its line. The status line counts tone clashes, words without a note of their own, and notes
  without a new word. Edited words travel with the score to Create, Cover or **Sing it now**, and
  the lyrics box of the tab the editor was opened from shows them too (Ctrl+Z there brings the old
  words back); closing the editor changes nothing. To check a change by ear, a word's menu offers
  **Hear … in the song** (the phrase around the word, from the finished song: YuE2 sings a few
  seconds off the score's clock, so the first time the song is transcribed once, about half a
  minute, and the timing kept beside it as `clock.json`) and
  **Sing this line now**: YuE2 sings just that line's bars with the current words and notes, as a
  short clip kept in the cache, never the library. It takes a few minutes, and given so little
  score YuE2 tends to sing the line more than once. YuE2 places the words itself, so the lane is an estimate: it pairs syllables with notes in order, kept
  in step at the spaces and line ends of the lyrics and the rests of the melody.
- **Use it** — **Make a song with this score** (Create) or **Make a cover with this score**
  (Cover), plus **Open .abc…** / **Save as .abc…** and an **ABC text** view for hand editing.
  **Save lyrics…** keeps the words as a text file, edits included: an .abc has no place for them.
- **OpenUtau ▾** — **Export .ustx…** or **Open in OpenUtau** (set OpenUtau.exe in Settings) writes
  the vocal line as an OpenUtau project for a DiffSinger voicebank to sing. The words are the Words
  lane's: one syllable per note, `+` where a note holds the syllable before. A Cantonese song gets
  toneless Jyutping (`gam`, not `gam1`), with any reading you picked. OpenUtau's DiffSinger Jyutping
  phonemizer looks syllables up without tones, and the melody carries the tone. Opened from a
  library song, the project runs on that song's own clock (its tempo scaled to where YuE2 really
  sang), so a vocal rendered from it lines up with the song's band under **Replace vocal**. Choose
  the singer in OpenUtau.

Every score the editor sends is written in YuE2's own ABC dialect, read back and compared with what
you edited; if anything differs it refuses rather than sending something else. The parser is
vendored from YuE2's `abc_tools.py` (Apache-2.0, see `licences/`).

### Making a song again

**↻** on any song card (or ⋯ → *Load settings to make it again*) fills the tab that song came from
with everything used to make it: style, lyrics, title, planning mode, style strength, seed, the
score it was sung from, and for covers the original recording. Press Create or Make cover for a new
version, or tick **Keep this seed** to reproduce the same one. It works on older songs too, because
the settings are read from the `job.json` saved with every run.

### Replace vocal

⋯ → **Replace vocal - import a tuned vocal (.wav)…** on a song card keeps that song's band and puts
your vocal over it, as a new song beside the original (which is never changed). The vocal is taken
out with UVR's MDX-Net *Inst HQ 3* separator, run directly with ONNX Runtime on DirectML (NVIDIA or
AMD, about 7 s for a three-minute song on a Radeon AI PRO R9700). The new vocal is shifted by the song's
clock and mixed with FFmpeg to −14 LUFS: one pass measures the loudness, a second applies one
linear gain. The instrumental is kept beside the result as `instrumental.flac`. It needs the Cover
maker; older installs get the separator from Settings → Repair.

The usual round trip: open the song's **Score**, **OpenUtau ▾ → Open in OpenUtau**, pick a
Cantonese DiffSinger singer, tune, export the vocal as WAV, then **Replace vocal** on the same song.

### Extract vocals

**Tools ▾ → Extract Vocals from Audio…** splits any MP3, WAV or FLAC, not only a Ceonggo song, into
`<name> (Vocals).flac` and `<name> (Instrumental).flac` (24-bit, 44.1 kHz) in a folder you choose.
It is the same separator as Replace vocal: the model gives the instrumental, and the vocal is what
is left of the mix, so the two add back up to the original. Progress shows in its own window, which
can stay open while you keep working; Stop or closing it ends only this job. Existing files are
replaced only after you agree. It needs the Cover maker, like Replace vocal.

### Settings

Language, graphics-card mode, audio decoder, where OpenUtau is, and maintenance (repair, log
folder, clear cache).

**Graphics-card modes** decide what is traded for memory, never quality:

| Mode | What it does |
|---|---|
| Fastest | everything stays on the card (24 GB cards) |
| Balanced | the singing model is parked in system memory while the sound is mixed |
| Lowest memory | also mixes and decodes in smaller pieces (automatic below 24 GB) |

If a song still runs out of memory, Ceonggo retries it once in Lowest memory mode.

## How it is put together

```
Ceonggo.exe / Ceonggo Setup.exe   PySide6 windows (no torch inside)
runtime/yue2/                 standalone Python 3.12 + torch 2.10 cu128 + yue2-infer 0.1.6
runtime/sheetsage/            standalone Python 3.11 + torch 2.8 cu128 (SheetSage2's pinned versions)
runtime/ffmpeg/, runtime/uv/  FFmpeg 6.1 shared, uv
models/                       YuE2-3B, YuE2-Vae, SheetSage2, MERT-v2-FullSong (pinned commits)
songs/                        one plain folder per song (audio.flac, score.abc, song.json …)
app/score/                    read, edit, write and preview scores; pair lyrics with notes (stdlib only)
app/cantonese.py              Jyutping (ToJyutping) and the tone check for the editor's Words lane
workers/                      the scripts the downloaded Pythons run
```

- **Portable:** the runtimes carry no venvs, since a venv stores absolute paths. Packages install
  straight into the standalone Pythons. Every cache and temp folder (`HF_HOME`, `TORCH_HOME`,
  `TEMP` …) points inside the folder, and nothing goes to `%APPDATA%` or the registry. Proven by
  moving the runtime to another path and generating from there. (Developing in a venv is a separate
  matter and perfectly fine — see *Developing*.)
- **Workers:** the window never imports torch. Each job is
  `runtime\…\python.exe workers\<name>_worker.py <job>.json`, reporting progress as JSON lines on
  stdout (`workers/_protocol.py`). To cancel, the window creates `<job>.cancel`. The worker exits
  after each job, so graphics memory is always freed. The window checks the workers' `WORKER_VERSION`
  before starting one, so a folder that mixes files from two releases says so instead of misbehaving.
- **Setup:** `setup/catalog.json` pins every download with its URL, size and sha256. PyTorch arrives
  as individual wheels, so its 2.5 GB file gets a real progress bar, and uv then installs them
  offline. Each finished item leaves a marker, so re-runs skip completed work and `.part` downloads
  resume. Nothing is resolved on the user's machine: the Python versions, wheels and CUDA builds are
  all decided when the release is built.

### Windows and hardware fixes the code carries

| Problem | Fix |
|---|---|
| PyTorch's Windows wheels declare FlashAttention but aren't built with it, so YuE2's CUDA-graph decoder fails on the first song | `yue2_worker.py` probes it and steers `GraphAR` to cuDNN attention; if the fast path still fails, `Engine` retries once in eager mode |
| YuE2's `memory_budget_gib` is a hard ceiling, not a saving, so a "12 GB" setting caused out-of-memory on a 16 GB card | the engine always gets the whole card; modes switch on real savers (`offload_ar`, smaller VAE tiles, chunked acoustic attention, which YuE2 supports but never passes) |
| YuE2 writes JSON in the default code page (cp1252), which crashes on Chinese lyrics | workers run with `PYTHONUTF8=1` |
| A thread reading stdin deadlocks imports on Windows | the job is passed as a file; cancel is a flag file |
| `soundfile` has a pure wheel without `libsndfile.dll` | the manifest builder prefers `win_amd64` wheels |
| `pretty_midi` is source-only | built offline during Setup with the locked setuptools |
| SheetSage2's CUDA 12.6 build has no RTX 50-series (`sm_120`) code | both runtimes use CUDA 12.8 builds, which still include Ampere and Ada |

### AMD cards (ROCm)

Verified end to end on a **Radeon AI PRO R9700** (RDNA4, `gfx1201`, 32 GB) under Windows:
a 59.7 s song in 151 s, and a 3:26 recording transcribed in 49 s. Both runtimes run on
AMD's Windows ROCm wheels (`rocm.nightlies.amd.com/v2/gfx120X-all`), which cover the
`cp312` and `cp311` this project needs. `yue2-infer` is pure Python, and SheetSage2
needed no changes at all.

What the code does differently on AMD:

| | |
|---|---|
| Detection | no `nvidia-smi` ships with a Radeon driver, and WMI's `AdapterRAM` is 32-bit (a 32 GB card reads as 4 GB), so `gpu.py` reads `HardwareInformation.qwMemorySize` from the display-adapter registry |
| Driver gate | AMD's `32.0.x` is a Windows build number, not a ROCm version, so the R570 check is skipped rather than failed |
| Attention | ROCm rejects the `seqused_k` `GraphAR` passes to FlashAttention, and cuDNN attention raises "No available kernel", so `patch_attention` steers AMD to SDPA |
| Decoder path | the CUDA-graph decoder is a *pessimisation* here — 293.6 s against 151.2 s eager on the same song — so `requests.backend` defaults AMD to `torch-eager` |
| Retries | ROCm names the failing kernel rather than its family, so `RETRY_HINTS` carries `mha_varlen`, `rocm`, `miopen` and `no available kernel` |
| Playback | over Remote Desktop audio is redirected to the client and buffers ~250 ms, so the score editor draws its playhead that far behind the player's clock |

Setup installs the right one by itself. The catalogue carries both library environments —
`env:yue2` / `env:sheetsage` marked `variant: nvidia`, and `env:yue2-rocm` /
`env:sheetsage-rocm` marked `amd` — and `Catalog.items_for` picks by what `gpu.detect()`
found. Everything else (models, the Python runtimes, uv, FFmpeg) is the same on either
card and carries no variant. A machine with no readable card is treated as NVIDIA, so
existing installs are unaffected. Crucially, **Repair on a Radeon no longer drops CUDA
wheels over a working ROCm runtime** — the CUDA items are simply not in its plan.

```bash
python tools/build_manifest.py --rocm    # re-pin just the two AMD environments
```

**The catch:** AMD publishes these as dated nightlies and prunes old ones, so the pinned
URLs have a shelf life — `ROCM_BUILDS` in `build_manifest.py` names one build per
environment (the newest where that Python's torch, both ROCm SDK wheels and the `rocm`
sdist all exist together). When they 404, re-run the command above. That is the one place
this project's "pin everything, resolve nothing on the user's machine" rule is living on
borrowed time.

MIOpen's kernel cache also costs about 3x on the first decode until it warms.

## Developing

```bash
python -m venv .venv                      # once; .venv is git-ignored
.venv\Scripts\activate                    # PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements-build.txt
python -m pytest tests -q                 # 135 tests, no GPU needed
python Ceonggo.py                         # or Ceonggo.bat / CeonggoSetup.bat, which find .venv themselves
set CEONGGO_ROOT=D:\somewhere             # point either program at another portable folder
```

The venv is for developing only — PySide6, pytest and PyInstaller, nothing the engines use. The
downloaded runtimes under `runtime/` still have no venv, on purpose (see *Portable* above), and a
venv active in the shell cannot leak into them: the worker and Setup environments drop `VIRTUAL_ENV`
and every `PYTHON*` variable before starting a runtime.

Maintainer tools:

```bash
python tools/build_manifest.py            # re-pin downloads -> setup/catalog.json + setup/locks/
python tools/extract_strings.py           # translation coverage for lang/zh-Hant.json
python tools/make_icons.py                # icons (uses Ceonggo-Icon.png if present)
python tools/build_release.py             # dist/Ceonggo + release/Ceonggo-v1.0.0.zip
```

`CEONGGO_SMOKE_SHOT=<png>` makes either exe screenshot its window and quit; adding
`CEONGGO_SMOKE_CREATE=1` to `Ceonggo.exe` makes it generate one song first. Both are how the built exes
are smoke-tested.

The tests cover the score core hardest, because a wrong score is the one bug a user could not
spot: every sample score round-trips through read → edit → write → read unchanged, transposing and
back restores it exactly, and every pitch in every key spells back to itself.

## Licences

YuE2's code is Apache-2.0, and `app/score/abc_dialect.py` is adapted from its `abc_tools.py`
(notice and licence in `licences/`, shipped with the release). The score editor's Jyutping comes
from [ToJyutping](https://github.com/CanCLID/ToJyutping) (BSD-2, notice in `licences/`). The YuE2, SheetSage2 and MERT model
weights are **CC BY-NC 4.0** (non-commercial), which Setup makes users acknowledge before
downloading. Replace vocal's separator, `UVR-MDX-NET-Inst_HQ_3.onnx`, comes from the
[Ultimate Vocal Remover](https://github.com/Anjok07/ultimatevocalremovergui) project's public model
release. OpenUtau is not part of Ceonggo; it only reads the files Ceonggo writes.
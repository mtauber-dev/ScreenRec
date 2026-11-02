# ScreenRec

A Windows tray app for recording how-to videos and step-by-step guides. Press a hotkey, show how
something is done while you talk, and ScreenRec makes either:

- **a short video** with idle parts cut, zoom on clicks, click circles, shortcut keys and captions,
  small enough to send on WhatsApp, plus a ready-to-paste message; or
- **a document**: a Markdown guide with one step per click, a screenshot of each step with the
  click circled, and instructions written from your narration.

Everything runs on your PC. Writing the document can optionally use Claude (see below).

## Using it

- **Ctrl+Alt+R** starts recording the screen under the mouse; press it again to stop.
  The tray menu also has **Record an area…**.
- While recording, a floating bar has **Pause / Resume**, **Restart** and **Finish**. A recorded area
  gets a red frame around it. Neither the bar nor the frame shows up in the recording.
- When you stop, the **review window** opens: watch the video, edit the WhatsApp message, read and
  edit the document, re-make either with different options, and copy them out.
- Tray menu → **After recording, make** → *Video*, *Document* or *Video and document*.

## Install

Build the exe (see *Running from source*), then share `dist\ScreenRec.zip`: unzip it anywhere and
run `ScreenRec.exe`. It includes ffmpeg. The speech model (~460 MB) downloads the first time
something is made.
Windows SmartScreen may warn about an unknown publisher: **More info → Run anyway**.

Settings are in `%APPDATA%\ScreenRec\config.json` (tray menu → **Edit settings**).

## Writing the documents: Claude or local

In the review window, **Settings · Connect Claude**:

- **Claude** reads the screenshots and names the exact buttons. Connect it with your own Anthropic
  API key (stored encrypted with Windows DPAPI), or have Claude Code installed and signed in.
  The transcript and screenshots are sent to Anthropic.
- **Local model**: [Ollama](https://ollama.com) with `qwen2.5:3b` (`ollama pull qwen2.5:3b`).
  Nothing leaves the PC; the wording is simpler.

Without either, the guide uses your narration as the step text.

## Running from source

Needs Windows 10 (2004 or later), Python 3.13 and ffmpeg on `PATH`.

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m screenrec
```

Other commands: `python -m screenrec process <recording folder> [--output video|document|both]`,
and `python -m screenrec record --seconds 10`.

Build the exe (bundles the ffmpeg found on `PATH`):

```bash
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

## Layout

| File | What it does |
|---|---|
| `screenrec/tray.py` | Tray icon, hotkey, menu |
| `screenrec/recorder.py` | ffmpeg capture, pause/resume segments, click/key/window logging |
| `screenrec/overlay.py` | Floating control bar and the area frame |
| `screenrec/process.py` | Video: transcribe, cut idle time, zoom, overlays, compress |
| `screenrec/document.py` | Document: steps, screenshots, written guide |
| `screenrec/describe.py` | WhatsApp message |
| `screenrec/review.py`, `review.html` | Review window (local web page on 127.0.0.1) |
| `screenrec/claude_link.py` | Connecting Claude (API key or Claude Code) |

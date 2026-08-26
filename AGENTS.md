# AGENTS.md

Single-file PySide6 (Qt 6) desktop app (Python 3.11+) that downloads a queue
of videos and converts them to MP3 via `yt-dlp` + ffmpeg. No tests, no build
step, no CI. Lint/typecheck are runnable locally (ruff + mypy, both configured
in `pyproject.toml`).

## Commands

- Run the app (GUI, blocks on `app.exec()`): `python video2mp3.py`
- Install deps: `pip install -r requirements.txt` (`yt-dlp`, `PySide6`)
- Dev tools: `pip install -r requirements-dev.txt`
- Verify (no display needed): `python -m py_compile video2mp3.py`, then
  `python -m ruff check video2mp3.py`, then `python -m mypy video2mp3.py`.
  GUI checks can run headless with `QT_QPA_PLATFORM=offscreen` (see the QA
  harness under `%TEMP%\opencode\qa_pyside_test.py`).

## Setup gotchas

- `ffmpeg` must be in PATH or resolvable via `QueueManager.ffmpeg_location`.
  On Windows after `winget install ffmpeg`, the PATH may not be refreshed until
  the session restarts; the UI's "Ubicar ffmpeg.exe..." button exists for this.
  Without ffmpeg the app still runs but downloads fail (MP3 extraction needs it).
- Python 3.11+ is the floor: yt-dlp raised its recommended minimum to 3.11 and
  is dropping 3.9/3.10. Do not lower it. PySide6 6.11+ also requires 3.11+.
- Settings persist to `~/.video2mp3.json` (output dir, bitrate, ffmpeg path,
  theme, window geometry). `MainWindow._persist()` writes it;
  `load_settings()` reads it.
- PySide6 6.11 stubs only declare namespaced enums (`Qt.Orientation.Vertical`,
  `QMessageBox.StandardButton.Yes`, `QHeaderView.ResizeMode.Stretch`, etc.).
  Use those forms so mypy passes; the flat shorthands (`Qt.Vertical`) are
  runtime-only.

## Architecture (hard rules)

- `QueueManager` (video2mp3.py, class `QueueManager`) is the single
  orchestration point: queue, worker thread, all yt-dlp/ffmpeg logic, plus
  `update_ytdlp()`. The GUI must never call yt-dlp. It is NOT a QObject and
  must never be turned into a QThread.
- `DownloadItem` is the per-URL data model. Items are identified by a stable
  `uid` (monotonic counter), never by list position: `_task_queue` stores uids,
  lookups go through `QueueManager._item_by_uid`. Removing an item purges it
  from `self.items`; stale uids left in the queue are skipped by the worker.
- `MainWindow` (PySide6/Qt6, replaces the old Tkinter `App`) is presentation
  only — no business logic. Layout is `QVBoxLayout`/`QHBoxLayout`/`QSplitter`
  only; no absolute geometry as a layout mechanism; `setMinimumSize(620, 480)`.
- Worker thread → UI communication is via callbacks + a thread-safe
  `log_queue` (Queue). `MainWindow._on_item_update` is deliberately a no-op:
  never touch Qt widgets from the worker thread; refresh happens in
  `_poll_status`, driven by a `QTimer` (400 ms) that drains `log_queue` and
  refreshes table/log/status bar.
- `cancel_item(uid)` cancels an in-flight download by setting
  `DownloadItem.cancel_requested`; the `progress_hook` raises `DownloadCancelled`
  to abort yt-dlp cleanly. `retry_item(uid)` re-enqueues Error/Cancelled items.
- UI strings, status values, and comments are in Spanish — keep new user-facing
  strings in Spanish.
- Theming is centralized: every color lives in `THEMES` (a `dict` with a full
  token set per theme: `background`, `surface`, `surface_alt`, `card`, `input`,
  `border`, `text`, `text_secondary`, `text_disabled`, `primary`,
  `success`/`warning`/`error`/`info` (+ hover/pressed variants), `selection`,
  `scrollbar`, `tooltip_bg`, `menu`, `menu_hover`, `header`, `alt_row`,
  `button`, `on_colored`, ...). `get_stylesheet(theme)` renders a global QSS
  from those tokens via `string.Template` and it is applied with
  `QApplication.setStyleSheet` (not on a single window), so menus, dialogs,
  popups and tooltips are themed too. Never hardcode colors in widgets; add
  them to `THEMES`. Graphic assets (combo/spin arrows, checkbox and radio
  indicators) are generated per theme with `QPainter` (`_asset`/`_draw_asset`)
  so no icon vanishes on either theme. Contrast is validated (WCAG ≥ 4.5:1)
  by the QA harness under `%TEMP%\opencode\qa_theme_audit.py`.
- Buttons use the `&` character for Alt+ mnemonics (12 unique). Shortcuts
  Delete/Ctrl+* are `QShortcut`.

## Download tuning

- Bitrate is set from the UI combobox via `QueueManager.set_bitrate`; the
  default is "320" (MP3 max).
- The yt-dlp options in `_download_and_convert` are deliberate and tuned:
  - `player_client` order (`android`, `tv`, `ios`, `web`) mitigates
    403/signature errors; `noplaylist: False` means pasting a playlist
    downloads all videos. Don't strip these without a reason.
  - `overwrites: False` avoids clobbering existing files; `socket_timeout: 30`
    prevents a hung connection from blocking the worker.
  - ID3 metadata + embedded cover come from the `FFmpegMetadata` and
    `EmbedThumbnail` postprocessors (note: the key is `EmbedThumbnail`, not the
    removed `FFmpegEmbedThumbnail`) plus `writethumbnail: True`.
- The worker loop keeps running on an empty queue (waits for new items); it
  only stops via `stop()`.
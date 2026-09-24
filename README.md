# FilePilot — Smart File Organizer

![Python](https://img.shields.io/badge/python-3.x-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)

A desktop app that keeps your folders tidy — auto-organize files by category, find and remove duplicates, and bulk-rename files with patterns. Built with Python 3 and Tkinter only (no third-party dependencies).

## Features

1. **Auto-Organize** — pick a folder and FilePilot sorts every file into subfolders by category (Images, Videos, Documents, Audio, Archives, Code, Others) using an extension map. Category checkboxes let you choose which categories to process; directories are skipped; name collisions are resolved with `(1)`, `(2)`…
2. **Duplicate Finder** — scans the folder tree and groups identical files using chunked SHA-256 hashing. Select the redundant copies and delete them with one click (one copy is always kept).
3. **Bulk Rename** — rename every file with a pattern like `prefix_{n:03d}`, supporting `{n}` with optional `:0Nd` zero-padding. A live preview shows old → new names (extensions preserved) before you apply.
4. **Dry-Run Toggle** — when enabled, FilePilot logs every planned action without touching the filesystem.
5. **Undo Log** — every organize/rename run is appended to `undo_log.json` as a list of `{src, dst}` moves; "Undo last run" reverses the most recent run.

## Quick Start

```bash
python3 app.py
```

Note: Tkinter must be installed. On Debian/Ubuntu:

```bash
sudo apt install python3-tk
```

## Screenshots

> Screenshots go here — add screenshots here.

## Build as EXE

To ship a standalone Windows executable with PyInstaller:

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name FilePilot app.py
```

The binary lands in `dist/FilePilot.exe`.

## Safety Notes

- **Dry-run first:** turn on the dry-run toggle to preview every planned action in the log pane before changing anything.
- **Undo:** every organize/rename run is journaled to `undo_log.json` — "Undo last run" restores the previous state.
- **Duplicates:** deleting files is permanent; undo does not cover deletions. One copy per duplicate group is always kept by default.
- File operations are wrapped in `try/except` — errors are logged to the pane instead of crashing the app.

## Structure

```
filepilot/
├── app.py             # Entire application: FileEngine (logic) + FilePilotApp (Tkinter GUI)
├── requirements.txt   # Stdlib only — no pip dependencies
├── .gitignore
├── LICENSE
└── README.md
```

## Tech Stack

- Python 3
- Tkinter / ttk (standard library — no pip dependencies)

## Author

**Adil Abdullah Khan** — BS Information Technology, Thal University Bhakkar, Pakistan

- Email: adilabdullahkhan35@gmail.com
- GitHub: https://github.com/adilabdullah15

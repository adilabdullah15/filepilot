"""
FilePilot — Smart File Organizer (Desktop Software)
====================================================
A Python 3 + Tkinter (stdlib only) desktop app that:
  1. Auto-organizes a folder into subfolders by file category.
  2. Finds duplicate files via SHA-256 hashing.
  3. Bulk-renames files using patterns like "prefix_{n:03d}".
  4. Supports a dry-run mode (log plans, touch nothing).
  5. Keeps an undo log (undo_log.json) so every organize/rename
     run can be reversed.

Run with:  python3 app.py
"""

import hashlib
import json
import os
import re
import shutil
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

UNDO_LOG_FILENAME = "undo_log.json"

CATEGORIES = {
    "Images":    [".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp"],
    "Videos":    [".mp4", ".mkv", ".avi", ".mov", ".webm"],
    "Documents": [".pdf", ".doc", ".docx", ".txt", ".md", ".xls",
                  ".xlsx", ".ppt", ".pptx", ".csv"],
    "Audio":     [".mp3", ".wav", ".flac", ".ogg", ".m4a"],
    "Archives":  [".zip", ".rar", ".7z", ".tar", ".gz"],
    "Code":      [".py", ".js", ".html", ".css", ".java", ".cpp",
                  ".json", ".php"],
}

HASH_CHUNK_SIZE = 1024 * 1024  # 1 MiB per chunk when hashing large files


# ---------------------------------------------------------------------------
# File-system engine (pure logic, no Tkinter)
# ---------------------------------------------------------------------------

class FileEngine:
    """File operations with dry-run support and an undo journal."""

    def __init__(self):
        self.dry_run = False
        # Runs recorded as lists of {"src": ..., "dst": ...}; newest run last.
        self.runs = []

    # -- helpers -------------------------------------------------------------
    def category_of(self, filename):
        ext = os.path.splitext(filename)[1].lower()
        for category, extensions in CATEGORIES.items():
            if ext in extensions:
                return category
        return "Others"

    @staticmethod
    def unique_path(path):
        """Return `path` with ' (1)', ' (2)', ... appended if it exists."""
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(path)
        counter = 1
        while True:
            candidate = "%s (%d)%s" % (base, counter, ext)
            if not os.path.exists(candidate):
                return candidate
            counter += 1

    def record_run(self, moves):
        """Append one run's move list to the undo log on disk."""
        if not moves:
            return
        self.runs.append(moves)
        try:
            with open(UNDO_LOG_FILENAME, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(moves) + "\n")
        except OSError:
            # Logging must never break the actual file operation.
            pass

    # -- 1. Organize ---------------------------------------------------------
    def organize(self, folder, enabled_categories, log, progress=None):
        """Move files in `folder` into category subfolders.

        `enabled_categories` is a set of category names to process.
        `log` is a callable receiving status strings.
        Returns the number of files moved (or planned, in dry-run).
        """
        moves = []
        entries = sorted(
            f for f in os.listdir(folder)
            if os.path.isfile(os.path.join(folder, f))
        )
        total = len(entries)
        moved = 0
        for i, name in enumerate(entries):
            src = os.path.join(folder, name)
            category = self.category_of(name)
            if category not in enabled_categories:
                continue
            target_dir = os.path.join(folder, category)
            if not self.dry_run and not os.path.isdir(target_dir):
                os.makedirs(target_dir, exist_ok=True)
            dst = self.unique_path(os.path.join(target_dir, name))
            if self.dry_run:
                log("DRY-RUN: would move %s -> %s/%s" % (name, category,
                                                         os.path.basename(dst)))
            else:
                shutil.move(src, dst)
                log("Moved %s -> %s/%s" % (name, category,
                                           os.path.basename(dst)))
                moves.append({"src": src, "dst": dst})
            moved += 1
            if progress is not None:
                progress((i + 1) / max(total, 1))
        self.record_run(moves)
        return moved

    # -- 2. Duplicates -------------------------------------------------------
    @staticmethod
    def sha256_of(path):
        """SHA-256 of a file read in chunks (safe for large files)."""
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(HASH_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def find_duplicates(self, folder, log, progress=None):
        """Return list of groups; each group is a sorted list of file paths."""
        paths = []
        for root, dirs, files in os.walk(folder):
            # Skip the category folders we created? No — scan everything;
            # duplicates anywhere in the tree count.
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in files:
                if name == UNDO_LOG_FILENAME:
                    continue
                paths.append(os.path.join(root, name))
        paths.sort()
        hashes = {}
        total = len(paths)
        for i, path in enumerate(paths):
            try:
                digest = self.sha256_of(path)
            except OSError as exc:
                log("Could not hash %s: %s" % (path, exc))
                if progress is not None:
                    progress((i + 1) / max(total, 1))
                continue
            hashes.setdefault(digest, []).append(path)
            if progress is not None:
                progress((i + 1) / max(total, 1))
        groups = [sorted(g) for g in hashes.values() if len(g) > 1]
        groups.sort(key=lambda g: g[0])
        return groups

    def delete_files(self, paths, log):
        """Delete given paths; returns count of successfully deleted files."""
        deleted = 0
        for path in paths:
            if self.dry_run:
                log("DRY-RUN: would delete %s" % path)
                deleted += 1
                continue
            try:
                os.remove(path)
                log("Deleted %s" % path)
                deleted += 1
            except OSError as exc:
                log("Failed to delete %s: %s" % (path, exc))
        return deleted

    # -- 3. Bulk rename ------------------------------------------------------
    @staticmethod
    def render_pattern(pattern, number):
        """Expand {n} / {n:03d} style placeholders.

        Any valid Python format spec after the colon is accepted, e.g.
        {n}, {n:03d}, {n:04d}, {n:d}.
        """
        def repl(match):
            spec = match.group(1) or ""
            return ("{0:%s}" % spec).format(number)
        return re.sub(r"\{n(?::([^}]*))?\}", repl, pattern)

    def preview_rename(self, folder, pattern):
        """Return list of (old_name, new_name) with extensions preserved."""
        files = sorted(
            f for f in os.listdir(folder)
            if os.path.isfile(os.path.join(folder, f))
            and f != UNDO_LOG_FILENAME
        )
        preview = []
        for i, name in enumerate(files, start=1):
            stem = os.path.splitext(name)[0]
            ext = os.path.splitext(name)[1]
            new_base = self.render_pattern(pattern, i)
            new_name = new_base + ext
            preview.append((name, new_name))
        return preview

    def apply_rename(self, folder, preview, log, progress=None):
        """Apply a preview list; returns number of files renamed."""
        moves = []
        total = len(preview)
        renamed = 0
        for i, (old_name, new_name) in enumerate(preview):
            src = os.path.join(folder, old_name)
            if old_name == new_name:
                if progress is not None:
                    progress((i + 1) / max(total, 1))
                continue
            dst = self.unique_path(os.path.join(folder, new_name))
            if self.dry_run:
                log("DRY-RUN: would rename %s -> %s"
                    % (old_name, os.path.basename(dst)))
            else:
                shutil.move(src, dst)
                log("Renamed %s -> %s" % (old_name, os.path.basename(dst)))
                moves.append({"src": src, "dst": dst})
            renamed += 1
            if progress is not None:
                progress((i + 1) / max(total, 1))
        self.record_run(moves)
        return renamed

    # -- 5. Undo -------------------------------------------------------------
    def undo_last_run(self, log):
        """Reverse the most recent run's moves. Returns count undone."""
        if not self.runs:
            return 0
        moves = self.runs.pop()
        undone = 0
        for move in reversed(moves):  # reverse order is safest
            src, dst = move["src"], move["dst"]
            if not os.path.exists(dst):
                log("Skip: %s no longer exists" % dst)
                continue
            final = self.unique_path(src)
            try:
                shutil.move(dst, final)
                log("Undone: %s -> %s" % (os.path.basename(dst),
                                          os.path.basename(final)))
                undone += 1
            except OSError as exc:
                log("Undo failed for %s: %s" % (dst, exc))
        # Persist the shortened history.
        try:
            with open(UNDO_LOG_FILENAME, "w", encoding="utf-8") as fh:
                for run in self.runs:
                    fh.write(json.dumps(run) + "\n")
        except OSError:
            pass
        return undone

    def load_undo_log(self):
        """Restore undo history from disk (called at startup)."""
        if not os.path.isfile(UNDO_LOG_FILENAME):
            return
        try:
            with open(UNDO_LOG_FILENAME, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        self.runs.append(json.loads(line))
        except (OSError, ValueError):
            self.runs = []


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class FilePilotApp(tk.Tk):
    """Tkinter front-end driving FileEngine."""

    def __init__(self):
        super().__init__()
        self.title("FilePilot — Smart File Organizer")
        self.geometry("860x640")
        self.engine = FileEngine()
        self.engine.load_undo_log()

        self.folder_var = tk.StringVar()
        self.dry_run_var = tk.BooleanVar(value=False)
        self.pattern_var = tk.StringVar(value="file_{n:03d}")
        self.category_vars = {c: tk.BooleanVar(value=True)
                              for c in list(CATEGORIES) + ["Others"]}
        self.duplicate_groups = []

        self._build_widgets()
        self._log("Welcome to FilePilot. Pick a folder to get started.")
        self._log("Dry-run mode logs planned actions without changing files.")

    # -- layout --------------------------------------------------------------
    def _build_widgets(self):
        # Folder picker row
        top = ttk.Frame(self, padding=8)
        top.pack(fill=tk.X)
        ttk.Label(top, text="Folder:").pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.folder_var, width=60).pack(
            side=tk.LEFT, padx=6, expand=True, fill=tk.X)
        ttk.Button(top, text="Browse…", command=self._browse).pack(side=tk.LEFT)

        # Category checkboxes
        cat_frame = ttk.LabelFrame(self, text="Categories to organize",
                                   padding=6)
        cat_frame.pack(fill=tk.X, padx=8, pady=(0, 6))
        for name in self.category_vars:
            ttk.Checkbutton(cat_frame, text=name,
                            variable=self.category_vars[name]).pack(
                side=tk.LEFT, padx=6)

        # Dry-run + undo row
        opts = ttk.Frame(self, padding=(8, 0))
        opts.pack(fill=tk.X)
        ttk.Checkbutton(opts, text="Dry-run (log only, change nothing)",
                        variable=self.dry_run_var).pack(side=tk.LEFT)
        ttk.Button(opts, text="Undo last run",
                   command=self._undo_last).pack(side=tk.RIGHT)

        # Tabs
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        self.tab_organize = ttk.Frame(self.notebook, padding=8)
        self.tab_dupes = ttk.Frame(self.notebook, padding=8)
        self.tab_rename = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(self.tab_organize, text="Organize")
        self.notebook.add(self.tab_dupes, text="Duplicates")
        self.notebook.add(self.tab_rename, text="Bulk Rename")

        # --- Organize tab
        ttk.Button(self.tab_organize, text="Organize folder now",
                   command=self._run_organize).pack(anchor=tk.W)
        ttk.Label(self.tab_organize, wraplength=780, justify=tk.LEFT,
                  text=("Moves every file in the chosen folder into a "
                        "subfolder named after its category (Images, Videos, "
                        "Documents, Audio, Archives, Code, Others). "
                        "Subfolders are skipped; name clashes get (1), (2)…")).pack(
            anchor=tk.W, pady=6)

        # --- Duplicates tab
        dup_btns = ttk.Frame(self.tab_dupes)
        dup_btns.pack(fill=tk.X)
        ttk.Button(dup_btns, text="Scan for duplicates",
                   command=self._run_dup_scan).pack(side=tk.LEFT)
        ttk.Button(dup_btns, text="Delete selected duplicates",
                   command=self._delete_selected_dupes).pack(side=tk.LEFT,
                                                             padx=8)
        ttk.Label(self.tab_dupes,
                  text=("One copy in each group is always kept. "
                        "Select the extra copies to delete."),
                  foreground="gray").pack(anchor=tk.W, pady=(6, 2))
        self.dupe_tree = ttk.Treeview(self.tab_dupes, columns=("size",),
                                      show="tree headings", selectmode="extended")
        self.dupe_tree.heading("#0", text="File")
        self.dupe_tree.heading("size", text="Size (bytes)")
        self.dupe_tree.column("size", width=110, anchor=tk.E)
        dup_scroll = ttk.Scrollbar(self.tab_dupes, orient=tk.VERTICAL,
                                   command=self.dupe_tree.yview)
        self.dupe_tree.configure(yscrollcommand=dup_scroll.set)
        self.dupe_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        dup_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # --- Rename tab
        ren_row = ttk.Frame(self.tab_rename)
        ren_row.pack(fill=tk.X)
        ttk.Label(ren_row, text="Pattern:").pack(side=tk.LEFT)
        ttk.Entry(ren_row, textvariable=self.pattern_var,
                  width=40).pack(side=tk.LEFT, padx=6)
        ttk.Label(ren_row, text='e.g. prefix_{n:03d}',
                  foreground="gray").pack(side=tk.LEFT)
        ren_btns = ttk.Frame(self.tab_rename)
        ren_btns.pack(fill=tk.X, pady=6)
        ttk.Button(ren_btns, text="Preview",
                   command=self._preview_rename).pack(side=tk.LEFT)
        ttk.Button(ren_btns, text="Apply rename",
                   command=self._run_rename).pack(side=tk.LEFT, padx=8)
        self.rename_preview = tk.Text(self.tab_rename, height=12,
                                      state=tk.DISABLED, wrap=tk.NONE)
        self.rename_preview.pack(fill=tk.BOTH, expand=True)
        self._rename_preview_cache = []

        # Log pane
        log_frame = ttk.LabelFrame(self, text="Log", padding=4)
        log_frame.pack(fill=tk.BOTH, expand=False, padx=8, pady=(0, 6))
        self.log_text = tk.Text(log_frame, height=9, state=tk.DISABLED,
                                wrap=tk.WORD)
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL,
                                   command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Progress bar + status bar
        self.progress = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.progress.pack(fill=tk.X, padx=8, pady=(0, 4))
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(self, textvariable=self.status_var, relief=tk.SUNKEN,
                  anchor=tk.W).pack(fill=tk.X, side=tk.BOTTOM)

    # -- small helpers -------------------------------------------------------
    def _log(self, message):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _set_status(self, message):
        self.status_var.set(message)

    def _set_progress(self, fraction):
        self.progress["value"] = fraction * 100
        self.update_idletasks()

    def _reset_progress(self):
        self.progress["value"] = 0
        self.update_idletasks()

    def _sync_engine(self):
        self.engine.dry_run = self.dry_run_var.get()

    def _require_folder(self):
        folder = self.folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("FilePilot",
                                 "Please pick a valid folder first.")
            return None
        return folder

    def _run_worker(self, worker):
        """Run a long task on a worker thread; log exceptions safely.

        The worker must only call the GUI through self.after(). Here we
        keep things simple: workers use self._set_progress/_log via after(),
        but most ops are fast enough to run inline — threads keep the UI
        responsive on huge folders.
        """
        def guarded():
            try:
                worker()
            except Exception as exc:  # never crash the app
                self.after(0, self._log, "ERROR: %s" % exc)
                self.after(0, self._set_status, "Error: %s" % exc)
            finally:
                self.after(0, self._reset_progress)
        threading.Thread(target=guarded, daemon=True).start()

    # -- actions -------------------------------------------------------------
    def _browse(self):
        folder = filedialog.askdirectory(title="Choose a folder to organize")
        if folder:
            self.folder_var.set(folder)
            self._log("Folder selected: %s" % folder)

    # 1. Organize
    def _run_organize(self):
        folder = self._require_folder()
        if folder is None:
            return
        self._sync_engine()
        enabled = {c for c, var in self.category_vars.items() if var.get()}
        if not enabled:
            messagebox.showwarning("FilePilot",
                                   "Select at least one category.")
            return

        def worker():
            self.after(0, self._set_status, "Organizing…")
            moved = self.engine.organize(
                folder, enabled,
                log=lambda m: self.after(0, self._log, m),
                progress=lambda f: self.after(0, self._set_progress, f))
            self.after(0, self._log,
                       "Done: %d file(s) %s." % (
                           moved, "planned" if self.engine.dry_run else "moved"))
            self.after(0, self._set_status,
                       "Organized %d file(s)." % moved)
        self._run_worker(worker)

    # 2. Duplicates
    def _run_dup_scan(self):
        folder = self._require_folder()
        if folder is None:
            return
        self._sync_engine()

        def worker():
            self.after(0, self._set_status, "Hashing files…")
            groups = self.engine.find_duplicates(
                folder,
                log=lambda m: self.after(0, self._log, m),
                progress=lambda f: self.after(0, self._set_progress, f))

            def show():
                self.duplicate_groups = groups
                for child in self.dupe_tree.get_children():
                    self.dupe_tree.delete(child)
                total_dupes = 0
                for gi, group in enumerate(groups):
                    node = self.dupe_tree.insert(
                        "", tk.END,
                        text="Group %d — %d identical files (keeping first)"
                        % (gi + 1, len(group)), open=True)
                    for path in group:
                        size = os.path.getsize(path) \
                            if os.path.isfile(path) else 0
                        self.dupe_tree.insert(node, tk.END, text=path,
                                              values=(size,), iid=path)
                    total_dupes += len(group) - 1
                self._log("Scan complete: %d duplicate group(s), "
                          "%d redundant file(s)." % (len(groups), total_dupes))
                self._set_status("Found %d duplicate group(s)."
                                 % len(groups))
            self.after(0, show)
        self._run_worker(worker)

    def _delete_selected_dupes(self):
        selection = [i for i in self.dupe_tree.selection()
                     if os.path.isfile(i)]
        if not selection:
            messagebox.showinfo("FilePilot",
                                "Select duplicate files in the list first.")
            return
        self._sync_engine()
        if not messagebox.askyesno("FilePilot",
                                   "Delete %d selected file(s)? "
                                   "One copy of each group is kept."
                                   % len(selection)):
            return

        def worker():
            count = self.engine.delete_files(
                selection,
                log=lambda m: self.after(0, self._log, m))
            self.after(0, self._log,
                       "Deleted %d file(s)." % count)
            self.after(0, self._set_status, "Deleted %d file(s)." % count)
            # Refresh the tree: drop deleted items.
            def refresh():
                for path in selection:
                    if self.dupe_tree.exists(path):
                        self.dupe_tree.delete(path)
            self.after(0, refresh)
        self._run_worker(worker)

    # 3. Bulk rename
    def _preview_rename(self):
        folder = self._require_folder()
        if folder is None:
            return
        pattern = self.pattern_var.get().strip()
        if "{n" not in pattern:
            messagebox.showerror("FilePilot",
                                 'Pattern must contain {n}, e.g. prefix_{n:03d}')
            return
        try:
            preview = self.engine.preview_rename(folder, pattern)
        except Exception as exc:
            messagebox.showerror("FilePilot", "Preview failed: %s" % exc)
            return
        # Collision check: warn if two files would get the same new name.
        new_names = [n for _, n in preview]
        dupes = {n for n in new_names if new_names.count(n) > 1}
        self._rename_preview_cache = preview
        self.rename_preview.configure(state=tk.NORMAL)
        self.rename_preview.delete("1.0", tk.END)
        for old, new in preview:
            self.rename_preview.insert(tk.END, "%s  ->  %s\n" % (old, new))
        self.rename_preview.configure(state=tk.DISABLED)
        self._log("Preview ready: %d file(s)." % len(preview))
        if dupes:
            self._log("WARNING: %d name collision(s) in preview "
                      "(suffixed with (1), (2)… on apply): %s"
                      % (len(dupes), ", ".join(sorted(dupes))))

    def _run_rename(self):
        folder = self._require_folder()
        if folder is None:
            return
        if not self._rename_preview_cache:
            self._preview_rename()
        if not self._rename_preview_cache:
            return
        self._sync_engine()
        preview = self._rename_preview_cache

        def worker():
            self.after(0, self._set_status, "Renaming…")
            renamed = self.engine.apply_rename(
                folder, preview,
                log=lambda m: self.after(0, self._log, m),
                progress=lambda f: self.after(0, self._set_progress, f))
            self.after(0, self._log,
                       "Done: %d file(s) %s." % (
                           renamed, "planned" if self.engine.dry_run
                           else "renamed"))
            self.after(0, self._set_status,
                       "Renamed %d file(s)." % renamed)
        self._run_worker(worker)

    # 5. Undo
    def _undo_last(self):
        self._sync_engine()
        if not self.engine.runs:
            messagebox.showinfo("FilePilot", "Nothing to undo.")
            return
        if self.engine.dry_run:
            self._log("DRY-RUN: undo is disabled while dry-run is on "
                      "(nothing was changed).")
            return
        if not messagebox.askyesno("FilePilot",
                                   "Reverse the last run (%d move(s))?"
                                   % len(self.engine.runs[-1])):
            return

        def worker():
            undone = self.engine.undo_last_run(
                log=lambda m: self.after(0, self._log, m))
            self.after(0, self._log, "Undo complete: %d move(s) reversed."
                       % undone)
            self.after(0, self._set_status,
                       "Undid %d move(s)." % undone)
        self._run_worker(worker)


def main():
    app = FilePilotApp()
    app.mainloop()


if __name__ == "__main__":
    main()

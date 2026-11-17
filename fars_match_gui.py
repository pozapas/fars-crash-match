"""
fars_match_gui.py  -  a small window for fars_match.py. Start it with RUN_ME.bat or
    python fars_match_gui.py
"""
import contextlib, io, os, queue, sys, threading, traceback
import tkinter as tk
from tkinter import filedialog, ttk

import fars_match as fm

NONE = "(not in my file)"
LABELS = {
    "id": "Crash ID", "state": "State", "date": "Crash date", "time": "Crash time",
    "latitude": "Latitude", "longitude": "Longitude", "county": "County name",
    "victim_age": "Victim age", "victim_sex": "Victim sex",
}
NEEDED = {"date": "needed", "state": "needed, or pick a state below", "latitude": "Step 1",
          "longitude": "Step 1", "time": "Step 2", "county": "Step 2", "victim_age": "Step 2",
          "victim_sex": "Step 2", "id": "optional"}


class QueueWriter(io.TextIOBase):
    def __init__(self, q): self.q = q
    def write(self, s): self.q.put(s); return len(s)


class App(tk.Tk):
    def __init__(self, start_file=None):
        super().__init__()
        self.title(f"Match state crashes to FARS  v{fm.__version__}")
        self.q = queue.Queue(); self.busy = False
        pad = dict(padx=10, pady=4)

        f = ttk.LabelFrame(self, text="1. Your crash file (.xlsx or .csv), one row per crash")
        f.pack(fill="x", **pad)
        self.inp = tk.StringVar(value=start_file or "")
        ttk.Entry(f, textvariable=self.inp).pack(side="left", fill="x", expand=True, padx=6, pady=6)
        ttk.Button(f, text="Browse...", command=self.pick_input).pack(side="right", padx=6)

        f = ttk.LabelFrame(self, text="2. Which column holds what (guessed for you; change if wrong)")
        f.pack(fill="x", **pad)
        self.colvars, self.boxes = {}, {}
        for r, k in enumerate(fm.FIELDS):
            ttk.Label(f, text=LABELS[k]).grid(row=r, column=0, sticky="w", padx=6)
            v = tk.StringVar(value=NONE); self.colvars[k] = v
            b = ttk.Combobox(f, textvariable=v, values=[NONE], state="readonly", width=32)
            b.grid(row=r, column=1, sticky="w", padx=6, pady=1); self.boxes[k] = b
            ttk.Label(f, text=NEEDED[k], foreground="grey").grid(row=r, column=2, sticky="w", padx=6)
        r = len(fm.FIELDS)
        ttk.Label(f, text="Every row is in state").grid(row=r, column=0, sticky="w", padx=6, pady=(6, 2))
        self.fixed = tk.StringVar(value="(use the State column)")
        ttk.Combobox(f, textvariable=self.fixed, state="readonly", width=32,
                     values=["(use the State column)"] + [n for n, _ in fm.STATES.values()])\
            .grid(row=r, column=1, sticky="w", padx=6, pady=(6, 2))

        f = ttk.LabelFrame(self, text="3. FARS folder with FARS<year>NationalCSV.zip files (from NHTSA)")
        f.pack(fill="x", **pad)
        self.fars = tk.StringVar()
        ttk.Entry(f, textvariable=self.fars).pack(side="left", fill="x", expand=True, padx=6, pady=6)
        ttk.Button(f, text="Browse...", command=self.pick_fars).pack(side="right", padx=6)

        f = ttk.LabelFrame(self, text="4. Output file (.xlsx or .csv)"); f.pack(fill="x", **pad)
        self.out = tk.StringVar()
        ttk.Entry(f, textvariable=self.out).pack(side="left", fill="x", expand=True, padx=6, pady=6)
        ttk.Button(f, text="Browse...", command=self.pick_output).pack(side="right", padx=6)
        self.ped = tk.BooleanVar(value=True)
        ttk.Checkbutton(self, text="Pedestrian deaths only (match only to FARS cases in which a pedestrian died)",
                        variable=self.ped).pack(anchor="w", padx=16)

        b = ttk.Frame(self); b.pack(fill="x", **pad)
        self.run_btn = ttk.Button(b, text="Run", command=self.start); self.run_btn.pack(side="left")
        ttk.Button(b, text="Open output folder", command=self.open_output).pack(side="left", padx=8)
        self.status = ttk.Label(b, text="Ready."); self.status.pack(side="left", padx=8)

        self.log = tk.Text(self, height=12, wrap="word", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.write_log("Pick your crash file, check the columns, pick the FARS folder, then press Run.\n")
        if start_file: self.load_columns(start_file)
        self.update_idletasks(); self.minsize(self.winfo_reqwidth(), self.winfo_reqheight())
        self.after(200, self.poll)

    def pick_input(self):
        p = filedialog.askopenfilename(title="Crash file",
                                       filetypes=[("Excel or CSV", "*.xlsx *.xls *.csv"), ("All files", "*.*")])
        if p:
            self.inp.set(p); self.load_columns(p)
            if not self.out.get():
                self.out.set(os.path.splitext(p)[0] + "_FARS_match.xlsx")

    def load_columns(self, p):
        try:
            cols = list(fm.read_table(p).columns) if p.lower().endswith(".csv") else \
                list(fm.pd.read_excel(p, nrows=0).columns)
        except Exception as e:
            self.write_log(f"Could not read {p}: {e}\n"); return
        g = fm.guess_columns(cols)
        for k, box in self.boxes.items():
            box["values"] = [NONE] + [str(c) for c in cols]
            self.colvars[k].set(str(g[k]) if g[k] is not None else NONE)
        self.write_log(f"Read {len(cols)} columns from {os.path.basename(p)}.\n")

    def pick_fars(self):
        p = filedialog.askdirectory(title="Folder with FARS<year>NationalCSV.zip")
        if p: self.fars.set(p)

    def pick_output(self):
        p = filedialog.asksaveasfilename(title="Output file", defaultextension=".xlsx",
                                         filetypes=[("Excel", "*.xlsx"), ("CSV", "*.csv")])
        if p: self.out.set(p)

    def open_output(self):
        p = os.path.dirname(self.out.get())
        if p and os.path.isdir(p) and hasattr(os, "startfile"): os.startfile(p)

    def start(self):
        if self.busy: return
        inp, fars, out = self.inp.get().strip(), self.fars.get().strip(), self.out.get().strip()
        if not os.path.isfile(inp): self.write_log("Please choose a crash file first.\n"); return
        if not os.path.isdir(fars): self.write_log("Please choose the FARS folder.\n"); return
        if not out: self.write_log("Please choose an output file.\n"); return
        cols = {k: (None if v.get() == NONE else v.get()) for k, v in self.colvars.items()}
        fixed = None if self.fixed.get().startswith("(") else self.fixed.get()
        if not cols["date"]: self.write_log("The crash date column is needed.\n"); return
        if not cols["state"] and not fixed: self.write_log("Pick the State column or a state.\n"); return
        self.busy = True; self.run_btn.state(["disabled"]); self.status.config(text="Running...")
        self.write_log(f"\n--- run: {inp}\n")
        threading.Thread(target=self.work, args=(inp, fars, out, cols, fixed), daemon=True).start()

    def work(self, inp, fars, out, cols, fixed):
        w = QueueWriter(self.q)
        try:
            with contextlib.redirect_stdout(w), contextlib.redirect_stderr(w):
                fm.run(inp, fars, out, cols=cols, fixed_state=fixed, pedestrian_only=self.ped.get())
            self.q.put(("done", "Finished."))
        except SystemExit as e:
            self.q.put(f"\nSTOPPED: {e}\n"); self.q.put(("done", "Stopped, see message."))
        except Exception:
            self.q.put("\nERROR:\n" + traceback.format_exc()); self.q.put(("done", "Error, see message."))

    def poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item, tuple):
                    self.busy = False; self.run_btn.state(["!disabled"]); self.status.config(text=item[1])
                else:
                    self.write_log(item)
        except queue.Empty:
            pass
        self.after(200, self.poll)

    def write_log(self, s):
        self.log.insert("end", s); self.log.see("end")


if __name__ == "__main__":
    try:  # sharp text on high-resolution Windows screens
        import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App(sys.argv[1] if len(sys.argv) > 1 else None).mainloop()

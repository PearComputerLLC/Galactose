#!/usr/bin/env python3
"""
galactose_decompiler.py
=======================
Decompiles a Galactose .gcb bytecode file into human-readable .gasm assembly,
and shows the result in a tkinter GUI with syntax highlighting.

Usage:
    python galactose_decompiler.py              # open GUI file picker
    python galactose_decompiler.py prog.gcb     # open pre-loaded
"""

import struct
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

# ---------------------------------------------------------------------------
# ISA definition (must match galactose.py exactly)
# ---------------------------------------------------------------------------

OPCODES = {
    0x01: ("PUSH",      "imm32"),   # PUSH <int32>
    0x02: ("POP",       "reg"),     # POP  <reg>
    0x03: ("ADD",       ""),        # ADD
    0x04: ("JMP",       "addr32"),  # JMP  <uint32>
    0x05: ("JZ",        "addr32"),  # JZ   <uint32>
    0x06: ("HALT",      ""),        # HALT
    0x07: ("SUB",       ""),        # SUB
    0x08: ("LOAD",      "reg_addr"),# LOAD <reg> <addr32>
    0x09: ("STORE",     "addr_reg"),# STORE <addr32> <reg>
    0x0A: ("WRITE_DISK",""),        # WRITE_DISK
    0x0B: ("READ_DISK", ""),        # READ_DISK
    0x0C: ("RAM_STORE", "addr_reg"),# RAM_STORE <addr32> <reg>
    0x0D: ("PUSH_REG",  "reg"),     # PUSH_REG <reg>
}

# Well-known special register names
REGISTER_NAMES = {
    200: "FREQ",
    201: "DUR",
    202: "DISK_SEL",
    203: "IMAGE_ID",
    210: "NET_CMD",
    211: "NET_ADDR_PTR",
    212: "NET_PORT",
    213: "NET_BUF_PTR",
    214: "NET_BUF_LEN",
    215: "NET_SOCKET_ID",
    216: "NET_RESULT",
    217: "NET_STATUS",
    218: "NET_PROTO",
    219: "NET_REMOTE_PORT",
    254: "INPUT",
    255: "TIMING",
}


def reg_name(r: int) -> str:
    if r in REGISTER_NAMES:
        return f"r{r}  ; {REGISTER_NAMES[r]}"
    return f"r{r}"


def reg_name_plain(r: int) -> str:
    """No comment suffix – used inside compound operands."""
    if r in REGISTER_NAMES:
        return f"r{r}"
    return f"r{r}"


# ---------------------------------------------------------------------------
# Core decompiler
# ---------------------------------------------------------------------------

class DecompileError(Exception):
    pass


def decompile(data: bytes) -> list[dict]:
    """
    Walk the bytecode and return a list of instruction dicts:
        { offset, mnemonic, operands, raw_bytes, comment }
    Raises DecompileError on truncated operands.
    """
    instructions = []
    pc = 0
    n = len(data)

    # First pass: collect jump targets so we can emit labels
    jump_targets: set[int] = set()
    _pc = 0
    while _pc < n:
        op = data[_pc]
        _pc += 1
        fmt = OPCODES.get(op, ("???", ""))[1]
        if fmt in ("imm32", "addr32"):
            if _pc + 4 <= n:
                val = struct.unpack_from("<I", data, _pc)[0]
                if fmt == "addr32":
                    jump_targets.add(val)
            _pc += 4
        elif fmt == "reg":
            _pc += 1
        elif fmt == "reg_addr":
            _pc += 5      # 1 reg + 4 addr
        elif fmt == "addr_reg":
            _pc += 5      # 4 addr + 1 reg

    # Second pass: generate instructions
    pc = 0
    label_counter = 0
    label_map: dict[int, str] = {}
    for t in sorted(jump_targets):
        label_map[t] = f"L{label_counter}"
        label_counter += 1

    while pc < n:
        offset = pc
        op = data[pc]
        pc += 1
        raw = bytearray([op])

        mnemonic, fmt = OPCODES.get(op, ("???", ""))
        operands = ""
        comment = ""

        if fmt == "imm32":
            if pc + 4 > n:
                raise DecompileError(f"Truncated PUSH at offset {offset}")
            val_bytes = data[pc:pc+4]
            raw += val_bytes
            val = struct.unpack_from("<i", data, pc)[0]  # signed
            pc += 4
            operands = str(val)

        elif fmt == "addr32":
            if pc + 4 > n:
                raise DecompileError(f"Truncated {mnemonic} at offset {offset}")
            val_bytes = data[pc:pc+4]
            raw += val_bytes
            addr = struct.unpack_from("<I", data, pc)[0]  # unsigned
            pc += 4
            lbl = label_map.get(addr, f"0x{addr:08X}")
            operands = lbl
            comment = f"→ byte {addr}"

        elif fmt == "reg":
            if pc >= n:
                raise DecompileError(f"Truncated {mnemonic} at offset {offset}")
            r = data[pc]; raw.append(r); pc += 1
            operands = reg_name(r)

        elif fmt == "reg_addr":        # LOAD reg addr
            if pc + 5 > n:
                raise DecompileError(f"Truncated LOAD at offset {offset}")
            r = data[pc]; raw.append(r); pc += 1
            addr_bytes = data[pc:pc+4]; raw += addr_bytes
            addr = struct.unpack_from("<I", data, pc)[0]; pc += 4
            operands = f"{reg_name_plain(r)}  0x{addr:08X}"
            if r in REGISTER_NAMES:
                comment = REGISTER_NAMES[r]

        elif fmt == "addr_reg":        # STORE / RAM_STORE addr reg
            if pc + 5 > n:
                raise DecompileError(f"Truncated {mnemonic} at offset {offset}")
            addr_bytes = data[pc:pc+4]; raw += addr_bytes
            addr = struct.unpack_from("<I", data, pc)[0]; pc += 4
            r = data[pc]; raw.append(r); pc += 1
            operands = f"0x{addr:08X}  {reg_name_plain(r)}"
            if r in REGISTER_NAMES:
                comment = REGISTER_NAMES[r]

        elif mnemonic == "???":
            comment = f"unknown opcode 0x{op:02X}"

        label = label_map.get(offset, "")
        instructions.append({
            "offset":   offset,
            "label":    label,
            "mnemonic": mnemonic,
            "operands": operands,
            "raw":      raw.hex(" ").upper(),
            "comment":  comment,
        })

    return instructions


def instructions_to_gasm(instructions: list[dict]) -> str:
    lines = [
        "; Galactose Assembly (.gasm) – decompiled by galactose_decompiler.py",
        "; Columns: offset  |  label  |  mnemonic operands  ; comment",
        "",
    ]
    for ins in instructions:
        label_part = f"{ins['label']}:" if ins["label"] else ""
        if label_part:
            lines.append(f"{label_part}")
        addr_str  = f"0x{ins['offset']:08X}"
        raw_pad   = ins["raw"].ljust(17)      # 5 bytes worst case = 14 chars + spaces
        mnem_op   = f"{ins['mnemonic']}"
        if ins["operands"]:
            mnem_op += f"  {ins['operands']}"
        comment_str = f"  ; {ins['comment']}" if ins["comment"] else ""
        lines.append(f"    {addr_str}  [{raw_pad}]  {mnem_op:<35}{comment_str}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tkinter GUI
# ---------------------------------------------------------------------------

THEME = {
    "bg":        "#1e1e2e",
    "fg":        "#cdd6f4",
    "accent":    "#89b4fa",
    "muted":     "#6c7086",
    "green":     "#a6e3a1",
    "yellow":    "#f9e2af",
    "red":       "#f38ba8",
    "orange":    "#fab387",
    "pink":      "#f5c2e7",
    "panel":     "#181825",
    "border":    "#313244",
    "sel_bg":    "#313244",
    "font_mono": ("Courier New", 11),
    "font_ui":   ("Segoe UI", 10) if sys.platform == "win32" else ("Helvetica", 10),
}

MNEMONIC_COLORS = {
    # Flow control
    "JMP":  THEME["orange"],
    "JZ":   THEME["orange"],
    "HALT": THEME["red"],
    # Stack
    "PUSH":     THEME["green"],
    "POP":      THEME["green"],
    "PUSH_REG": THEME["green"],
    # Arithmetic
    "ADD": THEME["yellow"],
    "SUB": THEME["yellow"],
    # Memory / I/O
    "LOAD":       THEME["accent"],
    "STORE":      THEME["accent"],
    "RAM_STORE":  THEME["accent"],
    "WRITE_DISK": THEME["pink"],
    "READ_DISK":  THEME["pink"],
    # Unknown
    "???": THEME["red"],
}


class DecompilerApp(tk.Tk):
    def __init__(self, preload: str | None = None):
        super().__init__()
        self.title("Galactose Decompiler")
        self.configure(bg=THEME["bg"])
        self.geometry("1100x720")
        self.minsize(800, 500)

        self._instructions: list[dict] = []
        self._gasm_text: str = ""
        self._gcb_path: Path | None = None

        self._build_ui()
        self._bind_shortcuts()

        if preload:
            self._load_file(Path(preload))

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        # ── Toolbar ──────────────────────────────────────────────────────
        toolbar = tk.Frame(self, bg=THEME["panel"], pady=6, padx=10)
        toolbar.pack(side=tk.TOP, fill=tk.X)

        btn_style = dict(
            bg=THEME["accent"], fg=THEME["panel"],
            font=THEME["font_ui"], relief="flat",
            padx=10, pady=4, cursor="hand2",
            activebackground=THEME["sel_bg"],
            activeforeground=THEME["fg"],
        )

        tk.Button(toolbar, text="📂  Open .gcb", command=self._open_file, **btn_style).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(toolbar, text="💾  Save .gasm", command=self._save_gasm, **btn_style).pack(side=tk.LEFT, padx=(0, 8))

        self._status_var = tk.StringVar(value="No file loaded.")
        tk.Label(toolbar, textvariable=self._status_var,
                 bg=THEME["panel"], fg=THEME["muted"],
                 font=THEME["font_ui"]).pack(side=tk.LEFT, padx=10)

        # ── PanedWindow: table (top) + raw gasm (bottom) ────────────────
        paned = tk.PanedWindow(self, orient=tk.VERTICAL,
                               bg=THEME["border"], sashwidth=4, sashrelief="flat")
        paned.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))

        # Top: instruction table
        table_frame = tk.Frame(paned, bg=THEME["bg"])
        paned.add(table_frame, minsize=200)
        self._build_table(table_frame)

        # Bottom: raw .gasm text view
        gasm_frame = tk.Frame(paned, bg=THEME["bg"])
        paned.add(gasm_frame, minsize=120)
        self._build_gasm_view(gasm_frame)

    def _build_table(self, parent: tk.Frame):
        cols = ("offset", "label", "raw", "mnemonic", "operands", "comment")
        headers = ("Offset", "Label", "Raw Bytes", "Mnemonic", "Operands", "Comment")

        style = ttk.Style(self)
        style.theme_use("default")
        style.configure("Gal.Treeview",
                         background=THEME["panel"],
                         foreground=THEME["fg"],
                         fieldbackground=THEME["panel"],
                         borderwidth=0,
                         rowheight=22,
                         font=THEME["font_mono"])
        style.configure("Gal.Treeview.Heading",
                         background=THEME["border"],
                         foreground=THEME["accent"],
                         font=(THEME["font_ui"][0], THEME["font_ui"][1], "bold"),
                         relief="flat")
        style.map("Gal.Treeview",
                  background=[("selected", THEME["sel_bg"])],
                  foreground=[("selected", THEME["accent"])])

        frame = tk.Frame(parent, bg=THEME["bg"])
        frame.pack(fill=tk.BOTH, expand=True)

        self._tree = ttk.Treeview(frame, columns=cols, show="headings",
                                   style="Gal.Treeview", selectmode="browse")
        widths = [90, 80, 145, 90, 260, 200]
        for col, hdr, w in zip(cols, headers, widths):
            self._tree.heading(col, text=hdr)
            self._tree.column(col, width=w, minwidth=40, stretch=(col == "operands"))

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self._tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        # Tag colours for each mnemonic group
        for mnem, color in MNEMONIC_COLORS.items():
            self._tree.tag_configure(mnem, foreground=color)
        self._tree.tag_configure("label_row",
                                  foreground=THEME["yellow"],
                                  font=(THEME["font_mono"][0], THEME["font_mono"][1], "bold"))

    def _build_gasm_view(self, parent: tk.Frame):
        tk.Label(parent, text=".gasm output",
                 bg=THEME["bg"], fg=THEME["muted"],
                 font=THEME["font_ui"]).pack(anchor="w", padx=4, pady=(4, 0))

        frame = tk.Frame(parent, bg=THEME["bg"])
        frame.pack(fill=tk.BOTH, expand=True)

        self._gasm_text_widget = tk.Text(
            frame,
            bg=THEME["panel"], fg=THEME["fg"],
            font=THEME["font_mono"],
            relief="flat", borderwidth=0,
            wrap="none",
            state="disabled",
            insertbackground=THEME["accent"],
            selectbackground=THEME["sel_bg"],
        )
        vsb2 = ttk.Scrollbar(frame, orient="vertical",
                              command=self._gasm_text_widget.yview)
        hsb2 = ttk.Scrollbar(frame, orient="horizontal",
                              command=self._gasm_text_widget.xview)
        self._gasm_text_widget.configure(yscrollcommand=vsb2.set,
                                         xscrollcommand=hsb2.set)

        self._gasm_text_widget.grid(row=0, column=0, sticky="nsew")
        vsb2.grid(row=0, column=1, sticky="ns")
        hsb2.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        # Syntax highlight tags on the Text widget
        self._gasm_text_widget.tag_configure("comment",  foreground=THEME["muted"])
        self._gasm_text_widget.tag_configure("label",    foreground=THEME["yellow"],
                                              font=(THEME["font_mono"][0],
                                                    THEME["font_mono"][1], "bold"))
        self._gasm_text_widget.tag_configure("offset",   foreground=THEME["muted"])
        self._gasm_text_widget.tag_configure("raw",      foreground=THEME["border"])
        for mnem, color in MNEMONIC_COLORS.items():
            self._gasm_text_widget.tag_configure(f"mnem_{mnem}", foreground=color)

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def _open_file(self):
        path = filedialog.askopenfilename(
            title="Open Galactose bytecode",
            filetypes=[("Galactose bytecode", "*.gcb"), ("All files", "*.*")],
        )
        if path:
            self._load_file(Path(path))

    def _load_file(self, path: Path):
        try:
            data = path.read_bytes()
        except OSError as e:
            messagebox.showerror("Error", str(e))
            return

        try:
            self._instructions = decompile(data)
        except DecompileError as e:
            messagebox.showerror("Decompile Error", str(e))
            return

        self._gcb_path = path
        self._gasm_text = instructions_to_gasm(self._instructions)
        self._populate_table()
        self._populate_gasm()
        total = len(self._instructions)
        self._status_var.set(
            f"{path.name}  —  {len(data)} bytes  —  {total} instructions decoded"
        )

    def _save_gasm(self):
        if not self._gasm_text:
            messagebox.showinfo("Nothing to save", "Decompile a .gcb file first.")
            return
        default = (self._gcb_path.with_suffix(".gasm").name
                   if self._gcb_path else "output.gasm")
        path = filedialog.asksaveasfilename(
            title="Save .gasm file",
            defaultextension=".gasm",
            initialfile=default,
            filetypes=[("Galactose assembly", "*.gasm"), ("Text file", "*.txt"), ("All files", "*.*")],
        )
        if path:
            Path(path).write_text(self._gasm_text, encoding="utf-8")
            messagebox.showinfo("Saved", f"Saved to {path}")

    # ------------------------------------------------------------------
    # Populate widgets
    # ------------------------------------------------------------------

    def _populate_table(self):
        self._tree.delete(*self._tree.get_children())
        for ins in self._instructions:
            # Insert label pseudo-row
            if ins["label"]:
                self._tree.insert("", "end",
                                   values=(f"0x{ins['offset']:08X}", f"{ins['label']}:",
                                           "", "", "", ""),
                                   tags=("label_row",))
            tag = ins["mnemonic"] if ins["mnemonic"] in MNEMONIC_COLORS else ""
            self._tree.insert("", "end", values=(
                f"0x{ins['offset']:08X}",
                "",
                ins["raw"],
                ins["mnemonic"],
                ins["operands"],
                ins["comment"],
            ), tags=(tag,) if tag else ())

    def _populate_gasm(self):
        w = self._gasm_text_widget
        w.configure(state="normal")
        w.delete("1.0", "end")
        w.insert("end", self._gasm_text)
        self._apply_gasm_highlighting()
        w.configure(state="disabled")

    def _apply_gasm_highlighting(self):
        w = self._gasm_text_widget
        text = self._gasm_text

        # Comments (lines starting with ;, or inline ; comment)
        for i, line in enumerate(text.splitlines(), start=1):
            lineno = str(i)
            # Full comment lines
            stripped = line.lstrip()
            if stripped.startswith(";"):
                w.tag_add("comment", f"{lineno}.0", f"{lineno}.end")
                continue
            # Inline comment
            if ";" in line:
                col = line.index(";")
                w.tag_add("comment", f"{lineno}.{col}", f"{lineno}.end")
            # Label lines (no leading spaces, ends with ":")
            if not line.startswith(" ") and line.strip().endswith(":"):
                w.tag_add("label", f"{lineno}.0", f"{lineno}.end")
                continue
            # Highlight mnemonics
            for mnem in MNEMONIC_COLORS:
                idx = line.find(f"  {mnem}")
                if idx != -1:
                    start_col = idx + 2
                    end_col   = start_col + len(mnem)
                    w.tag_add(f"mnem_{mnem}", f"{lineno}.{start_col}", f"{lineno}.{end_col}")

    # ------------------------------------------------------------------
    # Keyboard shortcuts
    # ------------------------------------------------------------------

    def _bind_shortcuts(self):
        self.bind("<Control-o>", lambda _e: self._open_file())
        self.bind("<Control-s>", lambda _e: self._save_gasm())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    preload = sys.argv[1] if len(sys.argv) > 1 else None
    app = DecompilerApp(preload=preload)
    app.mainloop()

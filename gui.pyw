"""Janela do conversor de processos jurídicos (PJe) de PDF para Markdown.

A conversão roda em outro processo (proc2md.py), para a janela não travar e o cancelamento ser imediato;
o andamento vem das linhas que ele imprime ("[1/3] processo.pdf", "página 12/318", "Markdown gerado: ...").

Desenho: coluna única, uma ação principal, cores do ícone (bordô jurídico), tema claro ou escuro conforme o
Windows. Tudo que o mouse faz, o teclado também faz (Tab, Enter/Espaço e os atalhos em `shortcuts`).
"""
from collections import deque
import ctypes
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox
import tkinter.font as tkfont

import customtkinter as ctk
from PIL import Image
from tkinterdnd2 import DND_FILES, TkinterDnD

from procmeta import find_pdfs, process_number

HERE = Path(__file__).resolve().parent

# Cores (claro, escuro). Texto sobre fundo: contraste de pelo menos 4,5:1 nos dois temas.
BG = ("#F6F5F2", "#16171B")
SURFACE = ("#FFFFFF", "#1F2126")
SUBTLE = ("#F0EEE9", "#2A2D34")
BORDER = ("#DDDAD2", "#383B44")
TEXT = ("#1D1F24", "#E7E8EB")
MUTED = ("#5C616B", "#A3A8B3")
FAINT = ("#9A9EA6", "#6B7079")
PRIMARY = ("#7A283B", "#B84A62")  # bordô do ícone
PRIMARY_HOVER = ("#611E2E", "#C95B73")
LINK = ("#7A283B", "#E899AA")
BAR = ("#7A283B", "#D9778C")
GOOD = ("#1B7A4B", "#63C795")
BAD = ("#B3261E", "#F28B82")
RING = TEXT


def time_left(seconds: float) -> str:
    return "falta menos de 1 min" if seconds < 60 else f"faltam cerca de {round(seconds / 60)} min"


def friendly_error(msg: str) -> str:
    if "could not load document" in msg.lower() or "failed to load document" in msg.lower() or "not valid" in msg.lower():
        return "o arquivo não é um PDF válido ou está danificado"
    if "out of memory" in msg.lower():
        return "faltou memória; feche outros programas e tente de novo"
    return msg[:160]


def elide(text: str, font: tkfont.Font, width: int) -> str:
    """Corta o meio do texto com reticências para caber em `width` pixels."""
    if width <= 0 or font.measure(text) <= width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        keep = (lo + hi + 1) // 2
        head = keep * 2 // 3
        if font.measure(text[:head] + "…" + text[len(text) - (keep - head):]) <= width:
            lo = keep
        else:
            hi = keep - 1
    head = lo * 2 // 3
    return text[:head] + "…" + text[len(text) - (lo - head):] if lo else "…"


def with_focus(parent, bg, build, activate, radius: int = 10):
    """Monta um controle dentro de uma moldura que vira o anel de foco para navegação por teclado."""
    ring = ctk.CTkFrame(parent, fg_color="transparent", border_width=2, border_color=bg, corner_radius=radius + 3)
    widget = build(ring)
    widget.pack(padx=3, pady=3, fill="both", expand=True)
    canvas = widget._canvas
    canvas.configure(takefocus=1)
    canvas.bind("<FocusIn>", lambda e: ring.configure(border_color=RING), add="+")
    canvas.bind("<FocusOut>", lambda e: ring.configure(border_color=bg), add="+")
    for key in ("<Return>", "<space>"):
        canvas.bind(key, lambda e: activate(widget), add="+")
    return ring, widget


class Window(ctk.CTk, TkinterDnD.DnDWrapper):
    """Janela customtkinter que também aceita arquivos arrastados do Explorer."""

    def __init__(self) -> None:
        super().__init__(fg_color=BG)
        self.TkdndVersion = TkinterDnD._require(self)


class Row(ctk.CTkFrame):
    """Um processo da fila: nome, situação, barra de progresso enquanto converte e a ação que cabe no momento."""

    def __init__(self, parent, app: "App", pdf: Path) -> None:
        super().__init__(parent, fg_color="transparent")
        self.app, self.pdf, self.kind = app, pdf, "fila"
        self.columnconfigure(0, weight=1)
        cnj = process_number(pdf.stem)
        subtitle = f"Na fila — CNJ {cnj}" if cnj else "Na fila"
        self.name = ctk.CTkLabel(self, text=pdf.name, font=app.f_strong, text_color=TEXT, anchor="w", height=22)
        self.name.grid(row=0, column=0, sticky="ew", padx=(16, 8), pady=(10, 0))
        self.state = ctk.CTkLabel(self, text=subtitle, font=app.f_small, text_color=MUTED, anchor="w", height=18)
        self.state.grid(row=1, column=0, sticky="ew", padx=(16, 8), pady=(0, 9))
        self.ring, self.action = with_focus(
            self, SURFACE, lambda p: app.make_button(p, "Remover", self.act, "ghost", width=92, height=30),
            lambda b: b.invoke(), radius=8)
        self.ring.grid(row=0, column=1, rowspan=2, padx=(0, 8))
        self.bar = ctk.CTkProgressBar(self, height=4, corner_radius=2, progress_color=BAR, fg_color=SUBTLE)
        self.bar.set(0)
        ctk.CTkFrame(self, height=1, fg_color=BORDER).grid(row=3, column=0, columnspan=2, sticky="ew", padx=16)
        self.bind("<Configure>", self.fit_name)

    def fit_name(self, event=None) -> None:
        width = (event.width if event else self.winfo_width()) - self.app.px(16 + 8 + 110)
        self.name.configure(text=elide(self.pdf.name, self.app.measure, width))

    def set(self, kind: str, text: str, color=MUTED) -> None:
        """kind: fila | convertendo | pronto | erro | cancelado."""
        self.kind = kind
        self.state.configure(text=text, text_color=color)
        if kind == "convertendo":
            self.bar.grid(row=2, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 8))
        else:
            self.bar.grid_forget()
        label = {"pronto": "Abrir", "erro": "Detalhes"}.get(kind, "Remover")
        if kind == "convertendo":
            self.ring.grid_remove()
        else:
            self.ring.grid()
            self.action.configure(text=label)
        self.refresh()

    def refresh(self) -> None:
        locked = self.app.proc is not None and self.kind in ("fila", "cancelado")
        self.action.configure(state="disabled" if locked else "normal")

    def act(self) -> None:
        if self.kind == "pronto":
            self.app.open_md(self.pdf)
        elif self.kind == "erro":
            self.app.show_error(self.pdf)
        else:
            self.app.remove(self.pdf)


class App:
    def __init__(self, root: Window) -> None:
        self.root = root
        self.files: list[Path] = []
        self.rows: dict[Path, Row] = {}
        self.outputs: dict[Path, Path] = {}
        self.errors: dict[Path, str] = {}
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue = queue.Queue()
        self.log: deque[str] = deque(maxlen=300)
        self.batch: list[Path] = []
        self.current: Path | None = None
        self.cancelled = False
        self.first_page: tuple[float, int] | None = None
        self.outdir: Path | None = None

        families = set(tkfont.families(root))
        text_face = "Segoe UI Variable Text" if "Segoe UI Variable Text" in families else "Segoe UI"
        head_face = "Segoe UI Variable Display" if "Segoe UI Variable Display" in families else text_face
        self.f_title = ctk.CTkFont(head_face, 22, "bold")
        self.f_head = ctk.CTkFont(head_face, 16, "bold")
        self.f_body = ctk.CTkFont(text_face, 14)
        self.f_strong = ctk.CTkFont(text_face, 14, "bold")
        self.f_small = ctk.CTkFont(text_face, 12)
        self.f_button = ctk.CTkFont(text_face, 13, "bold")
        self.scale = ctk.ScalingTracker.get_widget_scaling(root)
        self.measure = tkfont.Font(root, family=text_face, size=-round(14 * self.scale), weight="bold")

        root.title("Processo PJe para Markdown")
        root.minsize(640, 580)

        # --- cabeçalho ---
        head = ctk.CTkFrame(root, fg_color="transparent")
        head.pack(fill="x", padx=28, pady=(22, 14))
        ctk.CTkLabel(head, text="Processo PJe para Markdown", font=self.f_title, text_color=TEXT, anchor="w").pack(fill="x")
        ctk.CTkLabel(
            head,
            text="Converte autos em PDF com PaddleOCR-VL-1.6, preserva folhas e IDs e elimina textos repetitivos.",
            font=self.f_body, text_color=MUTED, anchor="w",
        ).pack(fill="x")

        # --- rodapé: andamento e ação principal ---
        foot = ctk.CTkFrame(root, fg_color="transparent")
        foot.pack(side="bottom", fill="x", padx=28, pady=(6, 22))
        self.progress = ctk.CTkProgressBar(foot, height=6, corner_radius=3, progress_color=BAR, fg_color=BG)
        self.progress.set(0)
        self.progress.pack(fill="x", pady=(0, 12))
        line = ctk.CTkFrame(foot, fg_color="transparent")
        line.pack(fill="x")
        self.go_ring, self.go_btn = with_focus(
            line, BG, lambda p: self.make_button(p, "Converter", self.start_or_cancel, "primary", width=190, height=42),
            lambda b: b.invoke())
        self.go_ring.pack(side="right")
        self.show_ring, self.show_btn = with_focus(
            line, BG, lambda p: self.make_button(p, "Mostrar na pasta", self.show_in_folder, "secondary", width=150, height=42),
            lambda b: b.invoke())
        self.status_var = tk.StringVar()
        self.status = ctk.CTkLabel(
            line, textvariable=self.status_var, font=self.f_body, text_color=MUTED, anchor="w",
            justify="left", wraplength=320,
        )
        self.status.pack(side="left", fill="x", expand=True)
        line.bind("<Configure>", lambda e: self.status.configure(wraplength=max(120, (e.width - self.px(380)) / self.scale)))

        # --- opções (aparecem quando há PDFs na fila) ---
        self.opts = ctk.CTkFrame(root, fg_color=SURFACE, corner_radius=14, border_width=1, border_color=BORDER)
        self.opts.columnconfigure(1, weight=1)
        self.ocr_all_var = tk.BooleanVar(value=False)
        self.out_var = tk.StringVar()

        def label(row: int, text: str) -> ctk.CTkLabel:
            w = ctk.CTkLabel(self.opts, text=text, font=self.f_body, text_color=MUTED, anchor="w")
            w.grid(row=row, column=0, sticky="w", padx=(18, 14), pady=6)
            return w

        ctk.CTkFrame(self.opts, height=6, fg_color="transparent").grid(row=0, column=0)
        label(1, "Modo de leitura")
        ocr_ring, self.ocr_switch = with_focus(
            self.opts, SURFACE, lambda p: ctk.CTkSwitch(
                p, text="Forçar OCR visual (PaddleOCR-VL-1.6) também em páginas com texto digital",
                variable=self.ocr_all_var, font=self.f_body, text_color=TEXT, text_color_disabled=FAINT,
                progress_color=PRIMARY, fg_color=("#B5B1A8", "#555963"),
                button_color="#FFFFFF", button_hover_color="#FFFFFF",
            ),
            lambda s: s.toggle(), radius=8,
        )
        ocr_ring.grid(row=1, column=1, columnspan=2, sticky="w", pady=3)
        label(2, "Salvar em")
        self.out_label = ctk.CTkLabel(self.opts, textvariable=self.out_var, font=self.f_body, text_color=TEXT, anchor="w")
        self.out_label.grid(row=2, column=1, sticky="ew", pady=6)
        out_ring, self.out_btn = with_focus(
            self.opts, SURFACE, lambda p: self.make_button(p, "Alterar…", self.pick_outdir, "secondary", width=96, height=32),
            lambda b: b.invoke(), radius=8,
        )
        out_ring.grid(row=2, column=2, sticky="e", padx=(8, 14), pady=3)
        ctk.CTkFrame(self.opts, height=6, fg_color="transparent").grid(row=3, column=0)
        self.opts.bind("<Configure>", lambda e: self.show_outdir())

        # --- fila ---
        self.card = ctk.CTkFrame(root, fg_color=SURFACE, corner_radius=14, border_width=1, border_color=BORDER)
        self.card.pack(fill="both", expand=True, padx=28)

        self.empty = ctk.CTkFrame(self.card, fg_color="transparent")
        middle = ctk.CTkFrame(self.empty, fg_color="transparent")
        middle.place(relx=0.5, rely=0.5, anchor="center")
        try:
            icon = Image.open(HERE / "app.ico")
            icon.size = (256, 256)
            ctk.CTkLabel(middle, text="", image=ctk.CTkImage(icon.copy(), size=(72, 72))).pack(pady=(0, 14))
        except (OSError, ValueError):
            pass
        ctk.CTkLabel(middle, text="Arraste processos em PDF ou pastas para cá", font=self.f_head, text_color=TEXT).pack()
        ctk.CTkLabel(middle, text="Ou escolha os arquivos no computador.", font=self.f_body, text_color=MUTED).pack(pady=(2, 16))
        picks = ctk.CTkFrame(middle, fg_color="transparent")
        picks.pack()
        for text, cmd, kind in (("Escolher PDFs…", self.pick_files, "primary"), ("Escolher pasta…", self.pick_folder, "secondary")):
            ring, _ = with_focus(
                picks, SURFACE, lambda p, t=text, c=cmd, k=kind: self.make_button(p, t, c, k, width=150, height=38),
                lambda b: b.invoke(),
            )
            ring.pack(side="left", padx=4)

        self.filled = ctk.CTkFrame(self.card, fg_color="transparent")
        top = ctk.CTkFrame(self.filled, fg_color="transparent")
        top.pack(fill="x", padx=(16, 8), pady=(10, 2))
        self.count_var = tk.StringVar()
        ctk.CTkLabel(top, textvariable=self.count_var, font=self.f_strong, text_color=TEXT, anchor="w").pack(side="left")
        self.queue_buttons = []
        for text, cmd in (("Limpar", self.clear), ("Adicionar pasta…", self.pick_folder), ("Adicionar PDFs…", self.pick_files)):
            ring, btn = with_focus(
                top, SURFACE, lambda p, t=text, c=cmd: self.make_button(p, t, c, "ghost", height=30, width=10),
                lambda b: b.invoke(), radius=8,
            )
            ring.pack(side="right")
            self.queue_buttons.append(btn)
        self.list = ctk.CTkScrollableFrame(self.filled, fg_color="transparent", corner_radius=0)
        self.list.pack(fill="both", expand=True, padx=2, pady=(0, 8))

        root.drop_target_register(DND_FILES)
        root.dnd_bind("<<DropEnter>>", lambda e: self.drop_hint(True, e))
        root.dnd_bind("<<DropLeave>>", lambda e: self.drop_hint(False, e))
        root.dnd_bind("<<Drop>>", self.on_drop)
        shortcuts = {
            "<Control-o>": self.pick_files,
            "<Control-O>": self.pick_folder,
            "<Control-Return>": self.start,
            "<Escape>": self.cancel,
        }
        for key, action in shortcuts.items():
            root.bind(key, lambda e, a=action: a())
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh()

    # ---------- aparência ----------
    def px(self, units: float) -> int:
        return round(units * self.scale)

    def make_button(self, parent, text: str, command, kind: str, **kw) -> ctk.CTkButton:
        look = {
            "primary": dict(fg_color=PRIMARY, hover_color=PRIMARY_HOVER, text_color="#FFFFFF", border_width=0),
            "secondary": dict(fg_color="transparent", hover_color=SUBTLE, text_color=TEXT, border_width=1, border_color=BORDER),
            "danger": dict(fg_color="transparent", hover_color=SUBTLE, text_color=BAD, border_width=1, border_color=BAD),
            "ghost": dict(fg_color="transparent", hover_color=SUBTLE, text_color=LINK, border_width=0),
        }[kind]
        return ctk.CTkButton(parent, text=text, command=command, font=self.f_button, corner_radius=10,
                             text_color_disabled=FAINT, **look, **kw)

    def restyle(self, button: ctk.CTkButton, kind: str) -> None:
        probe = self.make_button(button.master, "", None, kind)
        button.configure(**{k: probe.cget(k) for k in ("fg_color", "hover_color", "text_color", "border_width", "border_color")})
        probe.destroy()

    def drop_hint(self, on: bool, event=None):
        self.card.configure(border_color=PRIMARY if on and not self.proc else BORDER, border_width=2 if on and not self.proc else 1)
        return event.action if event else None

    def on_drop(self, event):
        self.drop_hint(False)
        self.add(Path(p) for p in self.root.tk.splitlist(event.data))
        return event.action

    def show_outdir(self) -> None:
        text = str(self.outdir) if self.outdir else "Mesma pasta de cada PDF"
        self.out_var.set(elide(text, self.measure, self.out_label.winfo_width() - self.px(8)))

    # ---------- fila ----------
    def add(self, paths) -> None:
        given = list(paths)
        if self.proc or not given:
            return
        paths = [p for p in given if p.is_dir() or p.suffix.lower() == ".pdf"]
        new = [p for p in find_pdfs(paths) if p not in self.files]
        if not new:
            self.status_var.set("Esses processos já estão na fila." if paths and find_pdfs(paths)
                                else "Essa pasta não tem PDFs." if paths
                                else "Só arquivos PDF (ou pastas com PDFs) podem ser convertidos.")
            return
        for p in new:
            self.files.append(p)
            self.rows[p] = Row(self.list, self, p)
            self.rows[p].pack(fill="x")
        self.refresh()

    def pick_files(self) -> None:
        self.add(Path(p) for p in filedialog.askopenfilenames(parent=self.root, title="Escolher processos em PDF", filetypes=[("PDF", "*.pdf")]))

    def pick_folder(self) -> None:
        folder = filedialog.askdirectory(parent=self.root, title="Pasta com PDFs")
        if folder:
            self.add([Path(folder)])

    def remove(self, pdf: Path) -> None:
        if self.proc:
            return
        self.rows.pop(pdf).destroy()
        self.files.remove(pdf)
        self.outputs.pop(pdf, None)
        self.errors.pop(pdf, None)
        self.refresh()

    def clear(self) -> None:
        if self.proc:
            return
        for row in self.rows.values():
            row.destroy()
        self.rows.clear()
        self.files.clear()
        self.outputs.clear()
        self.errors.clear()
        self.refresh()

    def pick_outdir(self) -> None:
        folder = filedialog.askdirectory(parent=self.root, title="Salvar os arquivos Markdown em")
        if folder:
            self.outdir = Path(folder)
        elif self.outdir and messagebox.askyesno("Pasta de saída", "Voltar a salvar cada Markdown na pasta do próprio PDF?", parent=self.root):
            self.outdir = None
        self.show_outdir()

    def pending(self) -> list[Path]:
        return [p for p in self.files if self.rows[p].kind != "pronto"]

    def refresh(self, keep_status: bool = False) -> None:
        running, n, waiting = self.proc is not None, len(self.files), len(self.pending())
        (self.empty if n else self.filled).pack_forget()
        (self.filled if n else self.empty).pack(fill="both", expand=True, padx=2, pady=2)
        if n:
            self.opts.pack(side="bottom", fill="x", padx=28, pady=(12, 6), before=self.card)
        else:
            self.opts.pack_forget()
        self.count_var.set("1 processo" if n == 1 else f"{n} processos")

        idle = "disabled" if running else "normal"
        for w in (self.ocr_switch, self.out_btn, *self.queue_buttons):
            w.configure(state=idle)
        for row in self.rows.values():
            row.refresh()

        self.restyle(self.go_btn, "danger" if running else "primary")
        self.go_btn.configure(
            text="Cancelar" if running else "Converter" if waiting <= 1 else f"Converter {waiting} processos",
            state="normal" if running or waiting else "disabled",
        )
        self.show_ring.pack_forget()
        if not running and self.outputs:
            self.show_ring.pack(side="right", padx=(0, 4))
        self.progress.configure(fg_color=BORDER if running else BG)
        if keep_status or running:
            return
        self.status.configure(text_color=MUTED)
        if not n:
            self.status_var.set("")
        elif waiting:
            self.status_var.set("Pronto para converter." if waiting == 1 else f"{waiting} processos prontos para converter.")

    # ---------- conversão ----------
    def md_for(self, pdf: Path) -> Path:
        return (self.outdir or pdf.parent) / (pdf.stem + ".md")

    def build_command(self) -> list[str]:
        python = Path(sys.executable).with_name("python.exe")
        cmd = [str(python), "-u", str(HERE / "proc2md.py"), *map(str, self.batch)]
        if self.ocr_all_var.get():
            cmd.append("--ocr-all")
        if self.outdir:
            cmd += ["-o", str(self.outdir)]
        return cmd

    def start_or_cancel(self) -> None:
        self.cancel() if self.proc else self.start()

    def cancel(self) -> None:
        if self.proc and messagebox.askyesno(
            "Cancelar a conversão?",
            "O processo em andamento será interrompido e terá de recomeçar do início.\n\nCancelar mesmo assim?",
            parent=self.root,
        ) and self.proc:
            self.cancelled = True
            self.proc.terminate()

    def start(self) -> None:
        if self.proc or not self.pending():
            return
        self.batch = self.pending()
        existing = [p for p in self.batch if self.md_for(p).exists()]
        if existing and not messagebox.askyesno(
            "Substituir arquivos Markdown?",
            (f"“{self.md_for(existing[0]).name}” já existe e será substituído." if len(existing) == 1
             else f"{len(existing)} arquivos .md já existem e serão substituídos.") + "\n\nContinuar?",
            parent=self.root,
        ):
            return
        self.log.clear()
        self.cancelled, self.current = False, None
        for p in self.batch:
            self.errors.pop(p, None)
            self.rows[p].set("fila", "Na fila")
        try:
            self.proc = subprocess.Popen(
                self.build_command(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", cwd=HERE, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as e:
            messagebox.showerror("Não foi possível iniciar", str(e), parent=self.root)
            return
        threading.Thread(target=self.read_output, args=(self.proc,), daemon=True).start()
        self.busy(True)
        self.status.configure(text_color=MUTED)
        self.status_var.set("Iniciando…")
        self.refresh()
        self.poll()

    def busy(self, on: bool) -> None:
        self.progress.stop()
        self.progress.configure(mode="indeterminate" if on else "determinate")
        if on:
            self.progress.start()

    def read_output(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            self.lines.put(line.rstrip("\r\n"))
        self.lines.put(("exit", proc.wait()))

    def poll(self) -> None:
        try:
            while True:
                item = self.lines.get_nowait()
                if isinstance(item, tuple):
                    return self.finish(item[1])
                self.handle(item)
        except queue.Empty:
            self.root.after(100, self.poll)

    def handle(self, line: str) -> None:
        self.log.append(line)
        row = self.rows.get(self.current) if self.current else None
        prefix = f"Processo {self.batch.index(self.current) + 1} de {len(self.batch)}: " if self.current and len(self.batch) > 1 else ""
        if m := re.fullmatch(r"\[(\d+)/(\d+)\] .+", line):
            self.current = self.batch[int(m[1]) - 1]
            self.first_page = None
            self.rows[self.current].set("convertendo", "Preparando…")
        elif line == "carregando modelos…":
            self.status_var.set("Carregando PaddleOCR-VL-1.6 e modelos de layout/orientação…")
        elif (m := re.fullmatch(r"página (\d+)/(\d+)", line)) and row:
            done, total = int(m[1]), int(m[2])
            self.busy(False)
            self.progress.set(done / total)
            row.bar.set(done / total)
            row.set("convertendo", f"Página {done} de {total}")
            now, left = time.monotonic(), ""
            if self.first_page is None:
                self.first_page = (now, done)
            elif done - self.first_page[1] >= 3 and done < total:
                per_page = (now - self.first_page[0]) / (done - self.first_page[1])
                left = " — " + time_left((total - done) * per_page)
            self.status_var.set(f"{prefix}página {done} de {total}{left}")
        elif line == "montando o Markdown…" and row:
            row.set("convertendo", "Montando o Markdown…")
            self.status_var.set(f"{prefix}montando o Markdown…")
            self.busy(True)
        elif (m := re.fullmatch(r"Markdown gerado: (.+)", line)) and row:
            self.outputs[self.current] = Path(m[1])
            row.set("pronto", "Pronto", GOOD)
        elif (m := re.fullmatch(r"ERRO: (.+?): (.+)", line)) and row:
            self.errors[self.current] = friendly_error(m[2])
            row.set("erro", "Não foi convertido: " + self.errors[self.current], BAD)

    def finish(self, code: int) -> None:
        self.proc = None
        self.busy(False)
        self.progress.set(0)
        done = sum(self.rows[p].kind == "pronto" for p in self.batch)
        if self.cancelled:
            for p in self.batch:
                if self.rows[p].kind not in ("pronto", "erro"):
                    self.rows[p].set("cancelado", "Cancelado") if p == self.current else self.rows[p].set("fila", "Na fila")
            self.status.configure(text_color=MUTED)
            self.status_var.set("Conversão cancelada." + (f" {done} já estavam prontos." if done else ""))
        else:
            for p in self.batch:
                if self.rows[p].kind not in ("pronto", "erro"):
                    self.errors[p] = f"a conversão parou de forma inesperada (código {code}).\n" + "\n".join(list(self.log)[-8:])
                    self.rows[p].set("erro", "Não foi convertido: a conversão parou de forma inesperada", BAD)
            failed = len(self.batch) - done
            self.status.configure(text_color=BAD if failed else GOOD)
            if not failed:
                self.status_var.set("Pronto: 1 processo convertido." if done == 1 else f"Pronto: {done} processos convertidos.")
            else:
                self.status_var.set(f"{done} de {len(self.batch)} convertidos; {failed} com erro. Veja “Detalhes” no item.")
        self.current = None
        self.refresh(keep_status=True)

    # ---------- depois ----------
    def open_md(self, pdf: Path) -> None:
        if (md := self.outputs.get(pdf)) and md.exists():
            os.startfile(md)

    def show_in_folder(self) -> None:
        if (md := next(reversed(self.outputs.values()), None)) and md.exists():
            subprocess.Popen(["explorer", "/select,", str(md)])

    def show_error(self, pdf: Path) -> None:
        messagebox.showerror("Processo não convertido", f"{pdf.name}\n\n{self.errors.get(pdf, '')}", parent=self.root)

    def on_close(self) -> None:
        if self.proc:
            if not messagebox.askyesno("Conversão em andamento", "Cancelar a conversão e sair?", parent=self.root):
                return
            self.proc.terminate()
        self.root.destroy()


def main() -> None:
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ProcessoParaMarkdown.Conversor")
    except (AttributeError, OSError):
        pass
    ctk.set_appearance_mode("system")
    root = Window()
    try:
        root.iconbitmap(str(HERE / "app.ico"))
    except tk.TclError:
        pass
    root.geometry("760x680")
    app = App(root)
    app.add(Path(a) for a in sys.argv[1:])
    root.mainloop()


if __name__ == "__main__":
    main()

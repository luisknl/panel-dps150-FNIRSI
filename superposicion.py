"""Lecturas flotantes y transparentes encima de cualquier ventana (por ejemplo, la imagen del microscopio).

Solo se ven los numeros, con un borde oscuro para leerlos sobre cualquier fondo. El resto de la
ventana es transparente: los clics pasan a la ventana de abajo.
  - Arrastra los numeros para moverlos.
  - Clic derecho sobre los numeros: tamano, que mostrar, opacidad, bloquear y cerrar.
  - Bloqueado: ni los numeros reciben clics (todo pasa al microscopio). Se desbloquea desde el panel.
"""
import ctypes
import tkinter as tk

CLAVE = "#010101"   # color que Windows vuelve transparente (casi negro: los bordes suaves parecen sombra)
TAMANOS = {"Pequeño": 18, "Mediano": 30, "Grande": 46, "Enorme": 68}
COL = {"v": "#5ea1ff", "i": "#ffa23a", "texto": "#f2f3f5", "cc": "#ff5c61", "ok": "#4cd07d", "sombra": "#000000"}


class Superposicion:
    def __init__(self, panel, al_cerrar=None):
        self.p = panel
        self.al_cerrar = al_cerrar
        cfg = panel._leer_config().get("superposicion", {})
        self.tam = cfg.get("tam", "Mediano") if cfg.get("tam") in TAMANOS else "Mediano"
        self.ver = {"v": True, "i": True, "extra": True, **cfg.get("ver", {})}
        self.alfa = float(cfg.get("alfa", 1.0))
        self.bloqueado = False
        self.w = tk.Toplevel(panel.root)
        self.w.overrideredirect(True)
        self.w.attributes("-topmost", True)
        self.w.config(bg=CLAVE)
        try:
            self.w.attributes("-transparentcolor", CLAVE)
        except tk.TclError:
            pass
        self.w.attributes("-alpha", self.alfa)
        x, y = cfg.get("x", 60), cfg.get("y", 60)
        self.w.geometry(f"+{int(x)}+{int(y)}")
        self.c = tk.Canvas(self.w, bg=CLAVE, highlightthickness=0, bd=0)
        self.c.pack()
        self.c.bind("<ButtonPress-1>", self._agarrar)
        self.c.bind("<B1-Motion>", self._arrastrar)
        self.c.bind("<ButtonRelease-1>", lambda e: self._guardar())
        self.c.bind("<Button-3>", self._menu)
        self._dx = self._dy = 0
        self._ultimo = None
        self._refrescar()

    # --- dibujo -----------------------------------------------------------
    def _texto(self, x, y, txt, color, size, bold=True):
        """Texto con borde oscuro para leerlo sobre cualquier fondo."""
        f = ("Consolas", size, "bold" if bold else "normal")
        g = max(1, size // 14)
        for dx in (-g, 0, g):
            for dy in (-g, 0, g):
                if dx or dy:
                    self.c.create_text(x + dx, y + dy, text=txt, fill=COL["sombra"], font=f, anchor="nw")
        tid = self.c.create_text(x, y, text=txt, fill=color, font=f, anchor="nw")
        return self.c.bbox(tid)

    def _lineas(self):
        dev = self.p.dev
        if not dev:
            return [("Sin fuente conectada", COL["texto"], 0.45)]
        e = dev.lectura()
        viejo = dev.antiguedad("vout") > 3
        on = e.get("salida", False)
        modo = e.get("modo", "")
        lineas = []
        if self.ver["v"]:
            lineas.append((f"{e.get('vout', 0):6.3f} V", COL["v"] if not viejo else "#9aa0a6", 1.0))
        if self.ver["i"]:
            i = e.get("iout", 0.0)
            lineas.append((f"{i * 1000:6.1f} mA" if i < 1 else f"{i:6.3f} A",
                           COL["i"] if not viejo else "#9aa0a6", 1.0))
        if self.ver["extra"]:
            prot = e.get("proteccion")
            if prot:
                extra, col = f"¡{prot}! salida cortada", COL["cc"]
            elif viejo:
                extra, col = "sin datos de la fuente", COL["texto"]
            else:
                extra = f"{e.get('pout', 0):.2f} W · {modo if on else '--'} · {'ON' if on else 'OFF'}"
                col = COL["cc"] if (on and modo == "CC") else (COL["texto"] if on else "#b8bcc2")  # verde no: las placas son verdes
            lineas.append((extra, col, 0.42))
        return lineas or [("(nada que mostrar)", COL["texto"], 0.45)]

    def _dibujar(self, lineas):
        self.c.delete("all")
        base = TAMANOS[self.tam]
        y, ancho = 4, 0
        for txt, col, escala in lineas:
            size = max(9, int(base * escala))
            x0, y0, x1, y1 = self._texto(6, y, txt, col, size)
            ancho = max(ancho, x1 + 8)
            y = y1 + max(2, size // 6)
        self.c.config(width=ancho, height=y + 4)

    def _refrescar(self):
        try:
            lineas = self._lineas()
            if lineas != self._ultimo:          # redibujar solo si cambio algo
                self._ultimo = lineas
                self._dibujar(lineas)
        except tk.TclError:
            return
        self.w.after(150, self._refrescar)

    # --- mover y menu -----------------------------------------------------
    def _agarrar(self, e):
        self._dx, self._dy = e.x_root - self.w.winfo_x(), e.y_root - self.w.winfo_y()

    def _arrastrar(self, e):
        self.w.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _menu(self, e):
        m = tk.Menu(self.w, tearoff=0)
        sub = tk.Menu(m, tearoff=0)
        self._var_tam = tk.StringVar(value=self.tam)
        for nombre in TAMANOS:
            sub.add_radiobutton(label=nombre, value=nombre, variable=self._var_tam,
                                command=lambda n=nombre: self._poner("tam", n))
        m.add_cascade(label="Tamaño", menu=sub)
        ver = tk.Menu(m, tearoff=0)
        for k, t in (("v", "Voltaje"), ("i", "Corriente"), ("extra", "Potencia, modo y salida")):
            var = tk.BooleanVar(value=self.ver[k])
            ver.add_checkbutton(label=t, variable=var, command=lambda k=k, v=var: self._mostrar(k, v.get()))
            setattr(self, f"_var_{k}", var)                       # que no se pierda la variable
        m.add_cascade(label="Mostrar", menu=ver)
        op = tk.Menu(m, tearoff=0)
        for a in (1.0, 0.85, 0.7, 0.55):
            op.add_command(label=f"{int(a * 100)} %", command=lambda a=a: self._poner("alfa", a))
        m.add_cascade(label="Opacidad", menu=op)
        m.add_separator()
        m.add_command(label="Bloquear (los clics pasan al microscopio)", command=lambda: self.bloquear(True))
        m.add_command(label="Cerrar", command=self.cerrar)
        m.tk_popup(e.x_root, e.y_root)

    def _poner(self, clave, valor):
        if clave == "tam":
            self.tam = valor
        elif clave == "alfa":
            self.alfa = valor
            self.w.attributes("-alpha", valor)
        self._ultimo = None
        self._guardar()

    def _mostrar(self, k, valor):
        self.ver[k] = valor
        self._ultimo = None
        self._guardar()

    def _guardar(self):
        self.p._guardar_config(superposicion={"x": self.w.winfo_x(), "y": self.w.winfo_y(), "tam": self.tam,
                                              "ver": self.ver, "alfa": self.alfa})

    # --- bloqueo de clics (Windows) ---------------------------------------
    def bloquear(self, si):
        """si=True: la ventana ignora el raton por completo (todo pasa a la ventana de abajo)."""
        try:
            hwnd = int(self.w.wm_frame(), 16)
            u = ctypes.windll.user32
            estilo = u.GetWindowLongW(hwnd, -20)                  # GWL_EXSTYLE
            WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x80000, 0x20
            estilo = (estilo | WS_EX_LAYERED | WS_EX_TRANSPARENT) if si else (estilo & ~WS_EX_TRANSPARENT)
            u.SetWindowLongW(hwnd, -20, estilo)
            self.bloqueado = si
        except Exception:
            self.bloqueado = False
        if self.al_cerrar:
            self.al_cerrar(estado=True)

    def cerrar(self):
        try:
            self._guardar()
            self.w.destroy()
        except tk.TclError:
            pass
        if self.al_cerrar:
            self.al_cerrar(estado=False)

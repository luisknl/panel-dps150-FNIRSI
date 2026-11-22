"""Panel de control para la fuente FNIRSI DPS-150.

  - Lecturas en vivo (V, A, W, CV/CC, temperatura, Ah, Wh, protecciones)
  - Fijar voltaje, limite de corriente y protecciones; encender/apagar salida
  - Grafica de V e I en el tiempo
  - Registro a CSV
  - Secuencias programadas (pasos V / I / segundos, con repeticiones)
  - Barrido I-V (curva corriente contra voltaje, util para revisar placas)

Ejecutar:  python panel_dps150.py          (fuente real)
           python panel_dps150.py --sim    (fuente simulada, sin equipo)
"""
import csv
import json
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

import dps150 as P
from dps150_sim import PuertoSimulado

CARPETA = os.path.dirname(os.path.abspath(__file__))
CARPETA_REG = os.path.join(CARPETA, "registros")

AZUL = "#1f6fd1"
NARANJA = "#e07b00"
VERDE = "#1a8f3a"
ROJO = "#c62828"
GRIS = "#777777"


def num(texto):
    return float(texto.strip().replace(",", "."))


# --------------------------------------------------------------------------
class Grafica(tk.Canvas):
    """Grafica con dos ejes Y (sin matplotlib), interactiva:

      rueda = acercar/alejar donde esta el raton · arrastrar = mover · doble clic = ver todo
      pasar el raton = cursor con el valor del punto mas cercano
    Series: (lista_xy, color, eje 0|1) o (lista_xy, color, eje, {"ancho": 1, "dash": (3, 2), "puntos": False}).
    """

    FONDO, BORDE, REJILLA, TEXTO = "white", "#bbbbbb", "#eeeeee", "black"   # cambian con el tema

    def __init__(self, master, **kw):
        super().__init__(master, bg=Grafica.FONDO, highlightthickness=0, **kw)
        self.bind("<Configure>", lambda e: self.dibujar())
        self.bind("<MouseWheel>", self._rueda)
        self.bind("<ButtonPress-1>", self._agarrar)
        self.bind("<B1-Motion>", self._arrastrar)
        self.bind("<Double-Button-1>", lambda e: self.ver_todo())
        self.bind("<Motion>", self._cursor)
        self.bind("<Leave>", lambda e: self.delete("cursor"))
        self.series = []
        self.titulo_x = ""
        self.titulos_y = ("", "")
        self.colores_y = ("black", "black")
        self.rango_x = None
        self.vista = None          # (xa, xb, (a0, b0), (a1, b1)) cuando el usuario acerco o movio
        self.log_y = False         # eje izquierdo en escala logaritmica
        self.log_min = 1e-5        # piso de la escala log (en las unidades de la serie)
        self.marca = None          # (x, y, eje) punto resaltado
        self.info_punto = None     # funcion(x, y, eje) -> texto del cursor
        self._t = None             # transformacion del ultimo dibujo
        self._arr = None

    def poner(self, series, titulo_x, titulos_y, colores_y, rango_x=None):
        self.series, self.titulo_x = series, titulo_x
        self.titulos_y, self.colores_y, self.rango_x = titulos_y, colores_y, rango_x
        self.dibujar()

    def ver_todo(self):
        self.vista = None
        self.dibujar()

    @staticmethod
    def _rango(vals):
        if not vals:
            return 0.0, 1.0
        lo, hi = min(vals), max(vals)
        if hi - lo < 1e-9:
            hi = lo + 1.0 if hi <= 0 else hi * 1.1 + 0.01
            lo = min(0.0, lo)
        m = (hi - lo) * 0.08
        if lo > -0.02 * abs(hi):          # ruido de milivoltios cerca de 0: empezar el eje en 0
            lo = max(lo, 0.0)
        return (0.0 if lo >= 0 and lo - m < 0 else lo - m), hi + m

    def _ty(self, y, eje):
        """Valor -> coordenada del eje (log10 si el eje izquierdo es logaritmico)."""
        if eje == 0 and self.log_y:
            return math.log10(max(y, self.log_min))
        return y

    def _series(self):
        for s in self.series:
            yield (s[0], s[1], s[2], s[3] if len(s) > 3 else {})

    def _rangos_auto(self):
        todos_x = [x for s, _, _, _ in self._series() for x, _ in s]
        xa, xb = self.rango_x if self.rango_x else self._rango(todos_x)
        if xb - xa < 1e-9:
            xb = xa + 1
        rangos = []
        for eje in (0, 1):
            ys = [self._ty(y, eje) for s, _, e, _ in self._series() if e == eje for _, y in s]
            if eje == 0 and self.log_y and ys:
                rangos.append((math.floor(min(ys)), math.ceil(max(ys)) + (1 if max(ys) == min(ys) else 0)))
            else:
                rangos.append(self._rango(ys))
        return xa, xb, rangos[0], rangos[1]

    @staticmethod
    def _fmt(v):
        return f"{v:.4g}"

    def dibujar(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 50 or h < 50:
            return
        s = self.winfo_fpixels("1i") / 96.0
        izq, der, arr, aba = int(58 * s), int(58 * s), int(26 * s), int(40 * s)
        x0, x1, y0, y1 = izq, w - der, arr, h - aba
        xa, xb, r0, r1 = self.vista if self.vista else self._rangos_auto()
        rangos = [r0, r1]
        self._t = (x0, x1, y0, y1, xa, xb, rangos)

        def px(x):
            return x0 + (x - xa) / (xb - xa) * (x1 - x0)

        def py(y, eje):
            a, b = rangos[eje]
            return y1 - (self._ty(y, eje) - a) / (b - a) * (y1 - y0)

        self.config(bg=Grafica.FONDO)
        self.create_rectangle(x0, y0, x1, y1, outline=Grafica.BORDE)
        for k in range(6):
            fx = k / 5
            gx = x0 + fx * (x1 - x0)
            self.create_line(gx, y0, gx, y1, fill=Grafica.REJILLA)
            self.create_text(gx, y1 + 4, anchor="n", font=("Segoe UI", 8), fill=Grafica.TEXTO,
                             text=self._fmt(xa + fx * (xb - xa)))
            gy = y1 - fx * (y1 - y0)
            self.create_line(x0, gy, x1, gy, fill=Grafica.REJILLA)
            for eje, xt, anc in ((0, x0 - 4, "e"), (1, x1 + 4, "w")):
                if eje == 0 and self.log_y:
                    continue                                  # el eje log se marca en decadas abajo
                if any(e == eje for _, _, e, _ in self._series()):
                    a, b = rangos[eje]
                    self.create_text(xt, gy, anchor=anc, font=("Segoe UI", 8), fill=self.colores_y[eje],
                                     text=self._fmt(a + fx * (b - a)))
        if self.log_y and any(e == 0 for _, _, e, _ in self._series()):
            a, b = rangos[0]
            for dec in range(math.ceil(a), math.floor(b) + 1):       # 0.1, 1, 10, 100...
                gy = y1 - (dec - a) / (b - a) * (y1 - y0)
                self.create_line(x0, gy, x1, gy, fill=Grafica.REJILLA)
                self.create_text(x0 - 4, gy, anchor="e", font=("Segoe UI", 8), fill=self.colores_y[0],
                                 text=self._fmt(10 ** dec))
        self.create_text((x0 + x1) / 2, h - 4, anchor="s", font=("Segoe UI", 9), fill=Grafica.TEXTO,
                         text=self.titulo_x)
        t0 = self.titulos_y[0] + ("  (log)" if self.log_y and self.titulos_y[0] else "")
        self.create_text(4, y0 - 4, anchor="sw", font=("Segoe UI", 9, "bold"), fill=self.colores_y[0], text=t0)
        self.create_text(w - 4, y0 - 4, anchor="se", font=("Segoe UI", 9, "bold"),
                         fill=self.colores_y[1], text=self.titulos_y[1])
        if self.vista:
            self.create_text(x1 - 4, y0 + 4, anchor="ne", font=("Segoe UI", 8), fill=Grafica.TEXTO,
                             text="ampliada · doble clic para ver todo")
        for s_, color, eje, est in self._series():
            pts = [(px(x), py(y, eje)) for x, y in s_ if xa <= x <= xb]
            pts = [(cx, min(max(cy, y0 - 2), y1 + 2)) for cx, cy in pts]     # no salirse del recuadro
            if len(pts) >= 2:
                self.create_line(*[c for p in pts for c in p], fill=color, width=est.get("ancho", 2),
                                 dash=est.get("dash"))
            if est.get("puntos", True) and 1 <= len(pts) <= 80:
                for cx, cy in pts:
                    self.create_oval(cx - 2, cy - 2, cx + 2, cy + 2, fill=color, outline=color)
        if self.marca:
            mx, my, me = self.marca
            if xa <= mx <= xb:
                cx, cy = px(mx), py(my, me)
                self.create_oval(cx - 6, cy - 6, cx + 6, cy + 6, outline=Grafica.TEXTO, width=2)

    # --- interaccion ------------------------------------------------------
    def _congelar(self):
        if not self.vista and self._t:
            x0, x1, y0, y1, xa, xb, rangos = self._t
            self.vista = (xa, xb, tuple(rangos[0]), tuple(rangos[1]))

    def _rueda(self, e):
        if not self._t:
            return
        self._congelar()
        x0, x1, y0, y1, *_ = self._t
        xa, xb, r0, r1 = self.vista
        f = 0.8 if e.delta > 0 else 1.25
        fx = min(1, max(0, (e.x - x0) / max(1, x1 - x0)))
        fy = min(1, max(0, (y1 - e.y) / max(1, y1 - y0)))
        cx = xa + fx * (xb - xa)
        nuevo = [cx - fx * (xb - xa) * f, cx + (1 - fx) * (xb - xa) * f]
        rs = []
        for a, b in (r0, r1):
            cy = a + fy * (b - a)
            rs.append((cy - fy * (b - a) * f, cy + (1 - fy) * (b - a) * f))
        self.vista = (nuevo[0], nuevo[1], rs[0], rs[1])
        self.dibujar()

    def _agarrar(self, e):
        self._arr = (e.x, e.y)

    def _arrastrar(self, e):
        if not self._t or not self._arr:
            return
        self._congelar()
        x0, x1, y0, y1, *_ = self._t
        xa, xb, r0, r1 = self.vista
        dx = (e.x - self._arr[0]) / max(1, x1 - x0) * (xb - xa)
        dyf = (e.y - self._arr[1]) / max(1, y1 - y0)
        self._arr = (e.x, e.y)
        self.vista = (xa - dx, xb - dx, (r0[0] + dyf * (r0[1] - r0[0]), r0[1] + dyf * (r0[1] - r0[0])),
                      (r1[0] + dyf * (r1[1] - r1[0]), r1[1] + dyf * (r1[1] - r1[0])))
        self.dibujar()

    def _cursor(self, e):
        self.delete("cursor")
        if not self._t:
            return
        x0, x1, y0, y1, xa, xb, rangos = self._t
        if not (x0 <= e.x <= x1 and y0 <= e.y <= y1):
            return
        mejor, dist = None, 400
        for s_, color, eje, est in self._series():
            if est.get("fondo"):
                continue
            for x, y in s_:
                if not xa <= x <= xb:
                    continue
                a, b = rangos[eje]
                cx = x0 + (x - xa) / (xb - xa) * (x1 - x0)
                cy = y1 - (self._ty(y, eje) - a) / (b - a) * (y1 - y0)
                d = (cx - e.x) ** 2 + (cy - e.y) ** 2
                if d < dist:
                    mejor, dist = (x, y, eje, cx, cy, color), d
        if not mejor:
            return
        x, y, eje, cx, cy, color = mejor
        self.create_line(cx, y0, cx, y1, fill=Grafica.BORDE, dash=(2, 3), tags="cursor")
        self.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, outline=color, width=2, tags="cursor")
        texto = self.info_punto(x, y, eje) if self.info_punto else f"{self._fmt(x)} · {self._fmt(y)}"
        anc = "sw" if cx < (x0 + x1) / 2 else "se"
        tid = self.create_text(cx + (8 if anc == "sw" else -8), cy - 8, anchor=anc, text=texto,
                               font=("Consolas", 9, "bold"), fill=Grafica.TEXTO, tags="cursor")
        bx = self.bbox(tid)
        if bx:
            r = self.create_rectangle(bx[0] - 3, bx[1] - 2, bx[2] + 3, bx[3] + 2, fill=Grafica.FONDO,
                                      outline=Grafica.BORDE, tags="cursor")
            self.tag_lower(r, tid)


# --------------------------------------------------------------------------
class Medidor(tk.Canvas):
    """Medidor de corriente moderno: arco de progreso, puntero con inercia y lectura digital.

    escala: corriente a fondo de escala en A, o None para escala automatica.
    """

    ESCALAS = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0]
    INICIO, BARRIDO = 210.0, 240.0        # grados: el arco va de 210° a -30° en sentido horario
    CLARO = dict(fondo="#ffffff", pista="#e6e8ed", texto="#1d1f23", suave="#8a8f98", marca="#c3c7cf",
                 acento="#ff8a00", medio="#f5a623", alto="#e5484d", chip="#f1f2f5")
    OSCURO = dict(fondo="#202124", pista="#2f3136", texto="#f1f2f4", suave="#8f949c", marca="#4a4d54",
                  acento="#ff9d2e", medio="#ffb340", alto="#ff5c61", chip="#2a2c30")
    TEMA = CLARO

    def __init__(self, master, **kw):
        super().__init__(master, bg=Medidor.TEMA["fondo"], highlightthickness=0, **kw)
        self.escala_fija = None
        self.escala = 1.0
        self.objetivo = 0.0
        self.pos = 0.0
        self.vel = 0.0
        self.pico = 0.0
        self.mostrar_pico = False
        self.activo = False
        self._geo = None
        self.bind("<Configure>", lambda e: self._cara())
        self.bind("<Button-1>", lambda e: self.reiniciar_pico())
        self._t = time.monotonic()
        self._animar()

    # --- interfaz ---------------------------------------------------------
    def poner_escala(self, escala):
        self.escala_fija = escala
        if escala:
            self._cambiar_escala(escala)

    def valor(self, amperios, activo=True):
        self.objetivo = max(0.0, amperios)
        self.activo = activo
        if self.escala_fija is None:
            self._autoescala()
        self.pico = max(self.pico, self.objetivo)

    def reiniciar_pico(self):
        self.pico = self.objetivo
        self._dinamico()

    # --- logica -----------------------------------------------------------
    def _autoescala(self):
        i, actual = self.objetivo, self.escala
        if i > actual * 0.98:
            nueva = next((e for e in self.ESCALAS if e * 0.9 >= i), self.ESCALAS[-1])
        else:
            menores = [e for e in self.ESCALAS if e < actual and i < e * 0.75]
            nueva = menores[0] if menores else actual
        if nueva != actual:
            self._cambiar_escala(nueva)

    def _cambiar_escala(self, escala):
        if escala != self.escala:
            self.pos = self.pos * self.escala / escala
            self.escala = escala
            self.pico = self.objetivo
        self._cara()

    def _animar(self):
        ahora = time.monotonic()
        dt = min(0.05, ahora - self._t)
        self._t = ahora
        meta = min(self.objetivo / self.escala, 1.03)
        for _ in range(4):                      # inercia suave, casi sin rebote
            h = dt / 4
            acc = 120.0 * (meta - self.pos) - 19.0 * self.vel
            self.vel += acc * h
            self.pos += self.vel * h
        self.pos = max(0.0, min(1.04, self.pos))
        self._dinamico()
        self.after(25, self._animar)

    # --- dibujo -----------------------------------------------------------
    def _ang(self, frac):
        return self.INICIO - self.BARRIDO * frac

    def _punto(self, frac, r):
        cx, cy, _, _ = self._geo
        a = math.radians(self._ang(frac))
        return cx + r * math.cos(a), cy - r * math.sin(a)

    def _divisiones(self):
        d = self.escala
        cifra = round(d / 10 ** math.floor(math.log10(d)))
        if cifra == 2:
            return d / 4, 5
        return d / 5, 4 if cifra == 1 else 5

    def _ma(self):
        return self.escala < 1.0

    def _txt_escala(self, v):
        return f"{v * 1000:g}" if self._ma() else f"{v:g}"

    def _arco(self, r, frac0, frac1, color, ancho, tags=()):
        """Arco grueso con extremos redondeados."""
        cx, cy, _, _ = self._geo
        if frac1 - frac0 <= 1e-4:
            return
        self.create_arc(cx - r, cy - r, cx + r, cy + r, start=self._ang(frac0),
                        extent=-self.BARRIDO * (frac1 - frac0), style="arc", outline=color,
                        width=ancho, tags=tags)
        for f in (frac0, frac1):
            x, y = self._punto(f, r)
            self.create_oval(x - ancho / 2, y - ancho / 2, x + ancho / 2, y + ancho / 2,
                             fill=color, outline=color, tags=tags)

    def _cara(self):
        self.delete("all")
        T = Medidor.TEMA
        self.config(bg=T["fondo"])
        w, h = self.winfo_width(), self.winfo_height()
        if w < 60 or h < 60:
            self._geo = None
            return
        s = self.winfo_fpixels("1i") / 96.0
        m = 10 * s
        r = min((w - 2 * m) / 2.12, (h - 2 * m) / 1.70)
        ancho = max(6, r * 0.10)
        cx, cy = w / 2, m + ancho / 2 + r
        self._geo = (cx, cy, r, ancho)
        self._arco(r, 0, 1, T["pista"], ancho)
        paso, sub = self._divisiones()
        n_may = round(self.escala / paso)
        fuente = ("Segoe UI", max(7, int(r / s / 12)))
        total = n_may * sub
        for k in range(total + 1):
            frac = k / total
            mayor = k % sub == 0
            r0 = r - ancho / 2 - 4 * s
            r1 = r0 - (8 if mayor else 4) * s
            x0, y0 = self._punto(frac, r0)
            x1, y1 = self._punto(frac, r1)
            self.create_line(x0, y0, x1, y1, fill=T["suave"] if mayor else T["marca"],
                             width=max(1, (2 if mayor else 1) * s), capstyle="round")
            if mayor:
                tx, ty = self._punto(frac, r1 - 10 * s)
                self.create_text(tx, ty, text=self._txt_escala(frac * self.escala), font=fuente, fill=T["suave"])
        self._dinamico()

    def _dinamico(self):
        self.delete("din")
        if not self._geo:
            return
        T = Medidor.TEMA
        cx, cy, r, ancho = self._geo
        s = self.winfo_fpixels("1i") / 96.0
        fuera = self.objetivo > self.escala
        frac = min(1.0, max(0.0, self.pos))
        color = T["alto"] if (fuera or frac > 0.9) else T["medio"] if frac > 0.75 else T["acento"]
        if not self.activo:
            color = T["suave"]
        if frac > 0.003:
            self._arco(r, 0, frac, color, ancho, tags="din")
        # indicador: perilla redonda sobre el arco, en la posicion actual
        kx, ky = self._punto(min(self.pos, 1.03), r)
        kr = ancho * 0.85
        self.create_oval(kx - kr, ky - kr, kx + kr, ky + kr, fill=T["fondo"], outline=color,
                         width=max(2, 3 * s), tags="din")
        self.create_oval(kx - kr * 0.35, ky - kr * 0.35, kx + kr * 0.35, ky + kr * 0.35,
                         fill=color, outline=color, tags="din")
        # marca de pico: triangulito por fuera del arco
        if self.mostrar_pico and self.pico > 0:
            fp = min(self.pico / self.escala, 1.03)
            a = math.radians(self._ang(fp))
            px, py = self._punto(fp, r + ancho / 2 + 3 * s)
            t = 6 * s
            ux, uy = math.cos(a), -math.sin(a)          # hacia afuera
            vx, vy = -uy, ux                            # tangente
            self.create_polygon(px, py, px + ux * t * 1.6 + vx * t, py + uy * t * 1.6 + vy * t,
                                px + ux * t * 1.6 - vx * t, py + uy * t * 1.6 - vy * t,
                                fill=T["alto"], outline="", tags="din")
        # lectura digital en el centro
        if self.activo:
            v = self.objetivo * (1000 if self._ma() else 1)
            txt = (f"{v:.2f}" if v < 10 else f"{v:.1f}" if v < 100 else f"{v:.0f}") if self._ma() else f"{v:.3f}"
        else:
            txt = "--"
        unidad = "mA" if self._ma() else "A"
        self.create_text(cx, cy - r * 0.08, text=txt, fill=T["alto"] if fuera else T["texto"],
                         font=("Segoe UI Semibold", max(12, int(r / s / 3.6))), tags="din")
        self.create_text(cx, cy + r * 0.24, text=unidad, fill=T["suave"],
                         font=("Segoe UI", max(8, int(r / s / 9))), tags="din")
        # chip con la escala
        chip = "FUERA DE ESCALA" if fuera else f"{'AUTO' if self.escala_fija is None else 'ESCALA'} · " \
                                               f"{self._txt_escala(self.escala)} {unidad}"
        fy = cy + r * 0.58
        fch = ("Segoe UI", max(7, int(r / s / 13)), "bold")
        tid = self.create_text(cx, fy, text=chip, font=fch, fill=T["alto"] if fuera else T["suave"], tags="din")
        x0, y0, x1, y1 = self.bbox(tid)
        pad = 8 * s
        self._pastilla(x0 - pad, y0 - 3 * s, x1 + pad, y1 + 3 * s, T["chip"])
        self.tag_raise(tid)

    def _pastilla(self, x0, y0, x1, y1, color):
        rr = (y1 - y0) / 2
        self.create_rectangle(x0 + rr, y0, x1 - rr, y1, fill=color, outline=color, tags="din")
        self.create_oval(x0, y0, x0 + 2 * rr, y1, fill=color, outline=color, tags="din")
        self.create_oval(x1 - 2 * rr, y0, x1, y1, fill=color, outline=color, tags="din")


# --------------------------------------------------------------------------
class Panel:
    def __init__(self, root, simulado=False):
        self.root = root
        self.dev = None
        self.simulado = tk.BooleanVar(value=simulado)
        self.historia = deque(maxlen=20000)      # (t, v, i, p)
        self.t0 = time.monotonic()
        self.ui_q = queue.Queue()
        self.trabajo = None                       # hilo de secuencia / barrido
        self.parar_trabajo = threading.Event()
        self.csv_archivo = None
        self.csv_writer = None
        self.csv_filas = 0
        self.csv_ultimo = 0.0
        self.barrido = []                         # (v_fijado, v, i)
        self.esperando = None                     # Event mientras se espera la fuente

        root.title("DPS-150 - Panel de control")
        esc = root.winfo_fpixels("1i") / 96.0       # factor de escala de Windows
        root.geometry(f"{int(1240 * esc)}x{int(780 * esc)}")
        root.minsize(int(1100 * esc), int(660 * esc))
        root.protocol("WM_DELETE_WINDOW", self.al_cerrar)
        estilo = ttk.Style()
        try:
            estilo.theme_use("vista")
        except tk.TclError:
            pass
        estilo.configure("Grande.TButton", font=("Segoe UI", 13, "bold"), padding=8)

        self._armar_conexion()
        self._armar_medidas_y_control()
        self._armar_pestanas()
        self.refrescar_puertos()
        self._habilitar(False)
        self.tema, self.fg_texto = "claro", "#333333"
        if self._leer_config().get("tema") == "oscuro":
            self.aplicar_tema("oscuro")
        self.tick()
        if not simulado:
            self._esperar_y_conectar()       # conectar solo en cuanto aparezca la fuente

    # ---------------------------------------------------------------- UI --
    def _armar_conexion(self):
        f = ttk.Frame(self.root, padding=(10, 8))
        f.pack(fill="x")
        ttk.Label(f, text="Puerto:").pack(side="left")
        self.cb_puerto = ttk.Combobox(f, width=34, state="readonly")
        self.cb_puerto.pack(side="left", padx=4)
        ttk.Button(f, text="Actualizar", command=self.refrescar_puertos).pack(side="left")
        ttk.Checkbutton(f, text="Simulada (sin equipo)", variable=self.simulado).pack(side="left", padx=10)
        self.btn_tema = ttk.Button(f, text="Modo oscuro", command=self.alternar_tema)
        self.btn_tema.pack(side="right")
        ttk.Button(f, text="Guía rápida", command=self.abrir_guia).pack(side="right", padx=(0, 6))
        self.btn_flot = ttk.Button(f, text="Lecturas flotantes", command=self.alternar_flotante)
        self.btn_flot.pack(side="right", padx=(0, 6))
        self.flotante = None
        self.btn_conectar = ttk.Button(f, text="Conectar", command=self.conectar_o_desconectar)
        self.btn_conectar.pack(side="left", padx=4)
        self.lbl_info = ttk.Label(f, text="Desconectada", foreground=GRIS)
        self.lbl_info.pack(side="left", padx=10)

    def _armar_medidas_y_control(self):
        cont = ttk.Frame(self.root, padding=(10, 0))
        cont.pack(fill="x")

        med = ttk.LabelFrame(cont, text="Salida medida", padding=10)
        med.pack(side="left", fill="both", expand=True)
        grande = ("Consolas", 34, "bold")
        self.lbl_v = tk.Label(med, text="--.--- V", font=grande, fg=AZUL)
        self.lbl_i = tk.Label(med, text="-.---- A", font=grande, fg=NARANJA)
        self.lbl_p = tk.Label(med, text="--.-- W", font=("Consolas", 22), fg="#333333")
        self.lbl_v.grid(row=0, column=0, sticky="w")
        self.lbl_i.grid(row=1, column=0, sticky="w")
        self.lbl_p.grid(row=2, column=0, sticky="w")
        lado = ttk.Frame(med)
        lado.grid(row=0, column=1, rowspan=3, sticky="nsew", padx=(20, 0))
        self.lbl_modo = tk.Label(lado, text="--", font=("Segoe UI", 18, "bold"), width=4,
                                 fg="white", bg=GRIS)
        self.lbl_modo.pack(anchor="w", pady=(4, 2))
        self.lbl_estado_salida = tk.Label(lado, text="SALIDA --", font=("Segoe UI", 12, "bold"), fg=GRIS)
        self.lbl_estado_salida.pack(anchor="w")
        self.lbl_prot = tk.Label(lado, text="", font=("Segoe UI", 12, "bold"), fg=ROJO)
        self.lbl_prot.pack(anchor="w")
        self.lbl_extra = tk.Label(lado, text="", font=("Segoe UI", 10), justify="left", fg="#333333")
        self.lbl_extra.pack(anchor="w", pady=(6, 0))
        med.columnconfigure(1, weight=1)

        # amperimetro de aguja
        am = ttk.LabelFrame(cont, text="Amperímetro", padding=6)
        am.pack(side="left", fill="y", padx=(10, 0))
        esc = self.root.winfo_fpixels("1i") / 96.0
        self.medidor = Medidor(am, width=int(330 * esc), height=int(230 * esc))
        self.medidor.pack()
        fila = ttk.Frame(am)
        fila.pack(fill="x", pady=(6, 0))
        ttk.Label(fila, text="Escala:").pack(side="left")
        self.cb_escala = ttk.Combobox(fila, width=8, state="readonly", values=[
            "Auto", "10 mA", "20 mA", "50 mA", "100 mA", "200 mA", "500 mA", "1 A", "2 A", "5 A"])
        self.cb_escala.current(0)
        self.cb_escala.pack(side="left", padx=4)
        self.cb_escala.bind("<<ComboboxSelected>>", lambda e: self._escala_medidor())
        self.v_pico = tk.BooleanVar(value=False)
        ttk.Checkbutton(fila, text="Pico", variable=self.v_pico,
                        command=lambda: setattr(self.medidor, "mostrar_pico", self.v_pico.get())) \
            .pack(side="left", padx=(8, 0))
        ttk.Button(fila, text="Reiniciar", width=9, command=self.medidor.reiniciar_pico).pack(side="left", padx=4)
        self.medidor.bind("<MouseWheel>", self._rueda_escala)

        ctl = ttk.LabelFrame(cont, text="Control", padding=10)
        ctl.pack(side="left", fill="y", padx=(10, 0))
        self.e_v = self._campo(ctl, 0, "Voltaje (V)", "5.00", self.aplicar_v)
        self.e_i = self._campo(ctl, 1, "Límite de corriente (A)", "0.500", self.aplicar_i)
        self.lbl_fijado = ttk.Label(ctl, text="Programado: --", foreground=GRIS)
        self.lbl_fijado.grid(row=2, column=0, columnspan=3, sticky="w", pady=(2, 8))
        self.btn_salida = ttk.Button(ctl, text="Encender salida", style="Grande.TButton",
                                     command=self.alternar_salida)
        self.btn_salida.grid(row=3, column=0, columnspan=3, sticky="ew")
        ttk.Label(ctl, text="Atajos: Enter aplica el campo · F5 enciende/apaga\n"
                            "Ctrl+1 … Ctrl+6 usan las memorias M1 … M6",
                  foreground=GRIS, justify="left").grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.root.bind("<F5>", lambda e: self.alternar_salida())
        for n in range(1, 7):
            self.root.bind(f"<Control-Key-{n}>", lambda e, n=n: self._atajo_memoria(n))
        self.controles = [self.e_v, self.e_i, self.btn_salida]

    def _escala_medidor(self):
        t = self.cb_escala.get()
        if t == "Auto":
            self.medidor.poner_escala(None)
            self.medidor._autoescala()
            self.medidor._cara()
        else:
            n, u = t.split()
            self.medidor.poner_escala(float(n) / (1000 if u == "mA" else 1))

    def _rueda_escala(self, ev):
        """Rueda del raton sobre la aguja: girar el selector de escala."""
        vals = list(self.cb_escala["values"])
        k = vals.index(self.cb_escala.get())
        if k == 0:   # desde Auto, partir de la escala que esta mostrando
            k = 1 + Medidor.ESCALAS.index(self.medidor.escala)
        k = max(1, min(len(vals) - 1, k + (1 if ev.delta > 0 else -1)))
        self.cb_escala.current(k)
        self._escala_medidor()

    def _campo(self, master, fila, texto, valor, accion):
        ttk.Label(master, text=texto).grid(row=fila, column=0, sticky="w")
        e = ttk.Entry(master, width=9, font=("Consolas", 12))
        e.insert(0, valor)
        e.grid(row=fila, column=1, padx=6, pady=3)
        e.bind("<Return>", lambda ev: accion())
        b = ttk.Button(master, text="Aplicar", command=accion)
        b.grid(row=fila, column=2)
        self.__dict__.setdefault("botones_campo", []).append(b)
        return e

    def _armar_pestanas(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=10)

        # Grafica
        g = ttk.Frame(nb, padding=6)
        nb.add(g, text="Gráfica")
        barra = ttk.Frame(g)
        barra.pack(fill="x")
        ttk.Label(barra, text="Ventana:").pack(side="left")
        self.cb_ventana = ttk.Combobox(barra, width=8, state="readonly",
                                       values=["30 s", "60 s", "2 min", "5 min", "10 min", "Todo"])
        self.cb_ventana.current(1)
        self.cb_ventana.pack(side="left", padx=4)
        ttk.Button(barra, text="Borrar", command=self.historia.clear).pack(side="left", padx=6)
        self.v_rapido = tk.BooleanVar(value=bool(self._leer_config().get("rapido", True)))
        ttk.Checkbutton(barra, text="Muestreo rápido (~10 lecturas/s en vez de 2)", variable=self.v_rapido,
                        command=self._rapido).pack(side="left", padx=(12, 0))
        self.graf = Grafica(g)
        self.graf.pack(fill="both", expand=True, pady=(6, 0))

        # Protecciones
        pr = ttk.Frame(nb, padding=10)
        nb.add(pr, text="Protecciones")
        self.e_prot = {}
        filas = [("ovp", "OVP  sobrevoltaje (V)"), ("ocp", "OCP  sobrecorriente (A)"),
                 ("opp", "OPP  sobrepotencia (W)"), ("otp", "OTP  sobretemperatura (°C)"),
                 ("lvp", "LVP  bajo voltaje de entrada (V)")]
        for n, (clave, texto) in enumerate(filas):
            ttk.Label(pr, text=texto).grid(row=n, column=0, sticky="w", pady=3)
            e = ttk.Entry(pr, width=9, font=("Consolas", 12))
            e.grid(row=n, column=1, padx=6)
            e.bind("<Return>", lambda ev, c=clave: self.aplicar_prot(c))
            b = ttk.Button(pr, text="Aplicar", command=lambda c=clave: self.aplicar_prot(c))
            b.grid(row=n, column=2)
            self.e_prot[clave] = e
            self.controles += [e, b]
        ttk.Button(pr, text="Leer valores actuales de la fuente", command=self.cargar_protecciones) \
            .grid(row=len(filas), column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(pr, foreground=GRIS, justify="left", text=(
            "Si una protección se dispara, la fuente apaga la salida y aquí se ve en rojo.\n"
            "Para revisar placas conviene un OCP bajo, por ejemplo 0.5 A.")) \
            .grid(row=len(filas) + 1, column=0, columnspan=3, sticky="w", pady=(10, 0))

        self._armar_memorias(nb)
        self._armar_corto(nb)
        from probador import Probador, ProbadorLED
        self.probador = Probador(self, nb)
        self.probador_led = ProbadorLED(self, nb)
        from diagnostico import Celular, Inyeccion, Laptop
        self.celular = Celular(self, nb)
        self.inyeccion = Inyeccion(self, nb)
        self.laptop = Laptop(self, nb)

        self._armar_secuencia(nb)

        from barrido import BarridoIV
        self.barrido_iv = BarridoIV(self, nb)

        # Registro CSV
        rg = ttk.Frame(nb, padding=10)
        nb.add(rg, text="Registro CSV")
        ttk.Label(rg, text="Cada:").grid(row=0, column=0, sticky="w")
        self.cb_int = ttk.Combobox(rg, width=7, state="readonly",
                                   values=["0.2 s", "0.5 s", "1 s", "2 s", "5 s", "10 s", "60 s"])
        self.cb_int.current(2)
        self.cb_int.grid(row=0, column=1, sticky="w", padx=4)
        self.btn_csv = ttk.Button(rg, text="Iniciar registro", command=self.alternar_csv)
        self.btn_csv.grid(row=0, column=2, padx=10)
        ttk.Button(rg, text="Abrir carpeta de registros",
                   command=lambda: (os.makedirs(CARPETA_REG, exist_ok=True), os.startfile(CARPETA_REG))) \
            .grid(row=0, column=3)
        self.lbl_csv = ttk.Label(rg, text="Sin registrar", foreground=GRIS)
        self.lbl_csv.grid(row=1, column=0, columnspan=4, sticky="w", pady=8)

    # ----------------------------------------------------------- memorias --
    def _armar_memorias(self, nb):
        esc = self.root.winfo_fpixels("1i") / 96.0
        mf = ttk.Frame(nb, padding=10)
        nb.add(mf, text="Memorias")

        izq = ttk.LabelFrame(mf, text="Memorias de la fuente (M1 – M6)", padding=8)
        izq.pack(side="left", fill="y")
        for c, t in ((1, "Voltaje (V)"), (2, "Corriente (A)")):
            ttk.Label(izq, text=t).grid(row=0, column=c)
        self.e_mem, self.lbl_mem = [], []
        for n in range(1, 7):
            ttk.Label(izq, text=f"M{n}", font=("Segoe UI", 11, "bold")).grid(row=n, column=0, padx=(0, 6))
            ev = ttk.Entry(izq, width=7, font=("Consolas", 11))
            ei = ttk.Entry(izq, width=7, font=("Consolas", 11))
            ev.grid(row=n, column=1, padx=3, pady=2)
            ei.grid(row=n, column=2, padx=3)
            b1 = ttk.Button(izq, text="Usar", width=6, command=lambda n=n: self.usar_memoria(n))
            b2 = ttk.Button(izq, text="Guardar", width=8, command=lambda n=n: self.guardar_memoria(n))
            b3 = ttk.Button(izq, text="← Actual", width=8, command=lambda n=n: self.copiar_actual_a(n))
            b1.grid(row=n, column=3, padx=(6, 2))
            b2.grid(row=n, column=4, padx=2)
            b3.grid(row=n, column=5, padx=2)
            lb = ttk.Label(izq, text="", width=3)
            lb.grid(row=n, column=6)
            self.e_mem.append((ev, ei))
            self.lbl_mem.append(lb)
            self.controles += [ev, ei, b1, b2, b3]
        b = ttk.Button(izq, text="Leer memorias de la fuente", command=self.cargar_memorias)
        b.grid(row=7, column=0, columnspan=6, sticky="w", pady=(8, 0))
        self.controles.append(b)
        ttk.Label(izq, foreground=GRIS, justify="left", wraplength=int(400 * esc), text=(
            "Usar: aplica ese voltaje y corriente ahora.  Guardar: escribe los valores en la memoria "
            "de la fuente (quedan aunque la apagues).  ← Actual: copia lo que está programado ahora.\n"
            "Atajo: Ctrl+1 … Ctrl+6 usan M1 … M6 desde cualquier pestaña.")) \
            .grid(row=8, column=0, columnspan=7, sticky="w", pady=(8, 0))

        der = ttk.LabelFrame(mf, text="Perfiles en la PC (los que quieras, con nombre)", padding=8)
        der.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.arbol = ttk.Treeview(der, columns=("v", "i", "ocp"), show="tree headings", height=6)
        self.arbol.heading("#0", text="Nombre")
        for col, t in (("v", "V"), ("i", "A"), ("ocp", "OCP (A)")):
            self.arbol.heading(col, text=t)
            self.arbol.column(col, width=int(70 * esc), anchor="e")
        self.arbol.column("#0", width=int(220 * esc))
        self.arbol.grid(row=0, column=0, columnspan=8, sticky="nsew")
        self.arbol.bind("<<TreeviewSelect>>", lambda e: self._perfil_a_campos())
        self.arbol.bind("<Double-1>", lambda e: self.usar_perfil())
        self.e_perfil = {}
        for c, (clave, t, ancho) in enumerate((("nombre", "Nombre", 20), ("v", "V", 6),
                                               ("i", "A", 6), ("ocp", "OCP", 6))):
            ttk.Label(der, text=t).grid(row=1, column=2 * c, sticky="e", padx=(6, 2), pady=(6, 0))
            e = ttk.Entry(der, width=ancho, font=("Consolas", 10))
            e.grid(row=1, column=2 * c + 1, sticky="w", pady=(6, 0))
            self.e_perfil[clave] = e
        botones = ttk.Frame(der)
        botones.grid(row=2, column=0, columnspan=8, sticky="w", pady=(6, 0))
        for t, fn in (("Guardar perfil", self.guardar_perfil), ("Usar", self.usar_perfil),
                      ("Borrar", self.borrar_perfil), ("Copiar lo actual", self._actual_a_perfil)):
            ttk.Button(botones, text=t, command=fn).pack(side="left", padx=(0, 6))
        ttk.Label(botones, text="Pasar a:").pack(side="left", padx=(8, 2))
        self.cb_mem_dest = ttk.Combobox(botones, width=4, state="readonly", values=[f"M{n}" for n in range(1, 7)])
        self.cb_mem_dest.current(0)
        self.cb_mem_dest.pack(side="left")
        ttk.Button(botones, text="Enviar", command=self.perfil_a_memoria).pack(side="left", padx=4)
        ttk.Label(der, foreground=GRIS, text="OCP es opcional: si lo pones, se aplica junto con el perfil. "
                                             "Doble clic en un perfil para usarlo.") \
            .grid(row=3, column=0, columnspan=8, sticky="w", pady=(6, 0))
        der.columnconfigure(7, weight=1)
        der.rowconfigure(0, weight=1)
        self.perfiles = self._leer_perfiles()
        self._pintar_perfiles()

    def _confirmar_cambio(self, v, i):
        """Si la salida esta encendida, pedir confirmacion antes de cambiar."""
        if self.dev and self.dev.lectura().get("salida"):
            return messagebox.askyesno("DPS-150", f"La salida está ENCENDIDA.\n\n¿Aplicar {v:g} V / {i:g} A ahora?")
        return True

    def _aplicar_vi(self, v, i, ocp=None):
        if not self._confirmar_cambio(v, i):
            return False
        if ocp is not None and not self._seguro(self.dev.fijar_proteccion, "ocp", ocp):
            return False
        # primero la corriente, luego el voltaje: el limite nuevo ya protege al subir V
        if self._seguro(self.dev.fijar_corriente, i) and self._seguro(self.dev.fijar_voltaje, v):
            self._poner(self.e_v, f"{v:.2f}")
            self._poner(self.e_i, f"{i:.3f}")
            return True
        return False

    def _leer_mem_campos(self, n):
        ev, ei = self.e_mem[n - 1]
        try:
            return num(ev.get()), num(ei.get())
        except ValueError:
            messagebox.showwarning("Memorias", f"M{n}: escribe voltaje y corriente, por ejemplo 5 y 0.5")
            return None

    def cargar_memorias(self):
        if not self.dev:
            return
        for n, (v, i) in enumerate(self.dev.leer_memorias(), 1):
            if v is not None:
                ev, ei = self.e_mem[n - 1]
                self._poner(ev, f"{v:.2f}")
                self._poner(ei, f"{i:.3f}")
                self.lbl_mem[n - 1].config(text="")

    def _atajo_memoria(self, n):
        """Ctrl+n: usar la memoria Mn (no durante una secuencia, barrido o prueba)."""
        if not self.dev:
            return
        if not getattr(self, "_info_base", None):      # texto de conexion a restaurar despues
            self._info_base = (self.lbl_info.cget("text"), self.lbl_info.cget("foreground"))
        self._info_n = getattr(self, "_info_n", 0) + 1
        mio = self._info_n
        if self.trabajo and self.trabajo.is_alive():
            self.lbl_info.config(text=f"M{n}: espera a que termine la prueba en curso", foreground=NARANJA)
        else:
            vi = self._leer_mem_campos(n)
            if not (vi and self._aplicar_vi(*vi)):
                return
            self.lbl_info.config(text=f"M{n} aplicada: {vi[0]:g} V / {vi[1]:g} A", foreground=VERDE)
        self.root.after(3000, lambda: self._restaurar_info(mio))

    def _restaurar_info(self, n):
        if n == self._info_n and self._info_base:
            if self.dev:
                self.lbl_info.config(text=self._info_base[0], foreground=self._info_base[1])
            self._info_base = None

    def usar_memoria(self, n):
        vi = self._leer_mem_campos(n)
        if vi and self.dev:
            self._aplicar_vi(*vi)

    def guardar_memoria(self, n):
        vi = self._leer_mem_campos(n)
        if not (vi and self.dev):
            return
        if self._seguro(self.dev.fijar_memoria, n, *vi):
            self.lbl_mem[n - 1].config(text="…", foreground=GRIS)
            self.root.after(800, lambda: self._verificar_memoria(n, *vi))

    def _verificar_memoria(self, n, v, i):
        """Confirma leyendo de vuelta lo que quedo guardado en la fuente."""
        if not self.dev:
            return
        lv, li = self.dev.leer_memorias()[n - 1]
        ok = lv is not None and abs(lv - v) < 0.005 and abs(li - i) < 0.0005
        self.lbl_mem[n - 1].config(text="✓" if ok else "⚠", foreground=VERDE if ok else ROJO)
        if ok:
            ev, ei = self.e_mem[n - 1]
            self._poner(ev, f"{lv:.2f}")
            self._poner(ei, f"{li:.3f}")
        else:
            messagebox.showwarning("Memorias", f"La fuente no confirmó M{n}. Leído: "
                                   f"{lv if lv is None else round(lv, 3)} V / {li if li is None else round(li, 4)} A")

    def copiar_actual_a(self, n):
        e = self.dev.lectura() if self.dev else {}
        if e.get("vset") is None:
            return
        ev, ei = self.e_mem[n - 1]
        self._poner(ev, f"{e['vset']:.2f}")
        self._poner(ei, f"{e['iset']:.3f}")
        self.lbl_mem[n - 1].config(text="✎", foreground=NARANJA)   # falta pulsar Guardar

    # perfiles en la PC
    RUTA_PERFILES = os.path.join(CARPETA, "perfiles.json")

    def _leer_perfiles(self):
        try:
            with open(self.RUTA_PERFILES, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return [{"nombre": "Lógica 3.3 V", "v": 3.3, "i": 0.3, "ocp": None},
                    {"nombre": "Lógica 5 V", "v": 5.0, "i": 0.5, "ocp": None},
                    {"nombre": "Placa 12 V (prueba)", "v": 12.0, "i": 0.3, "ocp": 0.5}]

    def _escribir_perfiles(self):
        with open(self.RUTA_PERFILES, "w", encoding="utf-8") as f:
            json.dump(self.perfiles, f, ensure_ascii=False, indent=2)

    def _pintar_perfiles(self):
        self.arbol.delete(*self.arbol.get_children())
        for k, p in enumerate(self.perfiles):
            self.arbol.insert("", "end", iid=str(k), text=p["nombre"],
                              values=(f"{p['v']:.2f}", f"{p['i']:.3f}", "" if p.get("ocp") is None else f"{p['ocp']:.3f}"))

    def _perfil_sel(self):
        sel = self.arbol.selection()
        return self.perfiles[int(sel[0])] if sel else None

    def _perfil_a_campos(self):
        p = self._perfil_sel()
        if p:
            for clave in ("nombre", "v", "i", "ocp"):
                self._poner(self.e_perfil[clave], "" if p.get(clave) is None else str(p[clave]))

    def _actual_a_perfil(self):
        e = self.dev.lectura() if self.dev else {}
        if e.get("vset") is not None:
            self._poner(self.e_perfil["v"], f"{e['vset']:.2f}")
            self._poner(self.e_perfil["i"], f"{e['iset']:.3f}")

    def _perfil_de_campos(self):
        nombre = self.e_perfil["nombre"].get().strip()
        try:
            v, i = num(self.e_perfil["v"].get()), num(self.e_perfil["i"].get())
            t = self.e_perfil["ocp"].get().strip()
            ocp = num(t) if t else None
        except ValueError:
            messagebox.showwarning("Perfiles", "Revisa los números de V, A y OCP")
            return None
        if not nombre:
            messagebox.showwarning("Perfiles", "Ponle un nombre al perfil")
            return None
        return {"nombre": nombre, "v": v, "i": i, "ocp": ocp}

    def guardar_perfil(self):
        p = self._perfil_de_campos()
        if not p:
            return
        for k, q in enumerate(self.perfiles):
            if q["nombre"].lower() == p["nombre"].lower():
                self.perfiles[k] = p
                break
        else:
            self.perfiles.append(p)
        self._escribir_perfiles()
        self._pintar_perfiles()

    def borrar_perfil(self):
        p = self._perfil_sel()
        if p and messagebox.askyesno("Perfiles", f"¿Borrar el perfil \"{p['nombre']}\"?"):
            self.perfiles.remove(p)
            self._escribir_perfiles()
            self._pintar_perfiles()

    def usar_perfil(self):
        p = self._perfil_sel() or self._perfil_de_campos()
        if p and self.dev:
            self._aplicar_vi(p["v"], p["i"], p.get("ocp"))

    def perfil_a_memoria(self):
        p = self._perfil_sel() or self._perfil_de_campos()
        if not (p and self.dev):
            return
        n = int(self.cb_mem_dest.get()[1])
        ev, ei = self.e_mem[n - 1]
        self._poner(ev, f"{p['v']:.2f}")
        self._poner(ei, f"{p['i']:.3f}")
        self.guardar_memoria(n)

    # -------------------------------------------------------------- tema --
    RUTA_CONFIG = os.path.join(CARPETA, "panel_config.json")
    OSC = dict(bg="#1e1f22", panel="#2b2d31", campo="#313338", texto="#e6e6e6", borde="#45474d",
               sel="#3d6fd1", apagado="#6b6b6b")
    FG_OSCURO = {"#333333": "#dddddd", "black": "#e6e6e6", "#000000": "#e6e6e6", "#6a1b9a": "#c58af9",
                 "#1f6fd1": "#5ea1ff", "#777777": "#a0a0a0", "systembuttontext": "#e6e6e6",
                 "systemwindowtext": "#e6e6e6"}

    def _leer_config(self):
        try:
            with open(self.RUTA_CONFIG, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _guardar_config(self, **kv):
        cfg = self._leer_config()
        cfg.update(kv)
        try:
            with open(self.RUTA_CONFIG, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh)
        except OSError:
            pass

    def _rapido(self):
        """Pedirle lecturas a la fuente ~10 veces por segundo (ella sola manda ~2)."""
        if self.dev:
            self.dev.rapido = self.v_rapido.get()
        self._guardar_config(rapido=self.v_rapido.get())

    def alternar_flotante(self):
        """Abre las lecturas flotantes; si estan bloqueadas, las desbloquea; si no, las cierra."""
        from superposicion import Superposicion
        if self.flotante is None:
            self.flotante = Superposicion(self, al_cerrar=self._estado_flotante)
            self._estado_flotante(True)
        elif self.flotante.bloqueado:
            self.flotante.bloquear(False)
        else:
            self.flotante.cerrar()

    def _estado_flotante(self, estado):
        if not estado:
            self.flotante = None
            self.btn_flot.config(text="Lecturas flotantes")
        elif self.flotante and self.flotante.bloqueado:
            self.btn_flot.config(text="Desbloquear flotantes")
        else:
            self.btn_flot.config(text="Cerrar flotantes")

    def abrir_guia(self):
        """Abre la guia rapida (GUIA.html) en el navegador."""
        fuente = os.path.join(CARPETA, "GUIA.html")
        destino = os.path.join(CARPETA, "Guía rápida DPS-150.html")
        try:
            with open(fuente, encoding="utf-8") as fh:
                cuerpo = fh.read()
            with open(destino, "w", encoding="utf-8") as fh:
                fh.write('<!doctype html><html lang="es"><head><meta charset="utf-8">'
                         '<meta name="viewport" content="width=device-width, initial-scale=1">'
                         f'</head><body>{cuerpo}</body></html>')
            os.startfile(destino)
        except OSError as e:
            messagebox.showwarning("Guía", f"No se pudo abrir la guía: {e}")

    def alternar_tema(self):
        self.aplicar_tema("claro" if self.tema == "oscuro" else "oscuro")
        self._guardar_config(tema=self.tema)

    def aplicar_tema(self, tema):
        st = ttk.Style()
        if not hasattr(self, "_tema_base"):
            self._tema_base, self._bg_base, self._orig = st.theme_use(), self.root.cget("bg"), {}
        self.tema = tema
        o = tema == "oscuro"
        c = self.OSC
        if o:
            st.theme_use("clam")
            st.configure(".", background=c["bg"], foreground=c["texto"], fieldbackground=c["campo"],
                         bordercolor=c["borde"], lightcolor=c["bg"], darkcolor=c["bg"], troughcolor=c["panel"],
                         insertcolor=c["texto"], selectbackground=c["sel"], selectforeground="white",
                         arrowcolor=c["texto"])
            st.configure("TButton", background=c["panel"])
            st.map("TButton", background=[("disabled", c["bg"]), ("active", "#3a3d43")],
                   foreground=[("disabled", c["apagado"])])
            for w in ("TEntry", "TCombobox", "TSpinbox"):
                st.configure(w, fieldbackground=c["campo"], foreground=c["texto"], background=c["panel"])
                st.map(w, fieldbackground=[("readonly", c["campo"]), ("disabled", c["bg"])],
                       foreground=[("disabled", c["apagado"]), ("readonly", c["texto"])])
            st.configure("TNotebook", background=c["bg"])
            st.configure("TNotebook.Tab", background=c["panel"], padding=(8, 3))
            st.map("TNotebook.Tab", background=[("selected", c["campo"])])
            st.configure("Treeview", background=c["campo"], fieldbackground=c["campo"], foreground=c["texto"])
            st.configure("Treeview.Heading", background=c["panel"])
            for w in ("TCheckbutton", "TRadiobutton"):
                st.map(w, background=[("active", c["bg"])], indicatorcolor=[("selected", c["sel"]),
                                                                            ("!selected", c["campo"])])
            Grafica.FONDO, Grafica.BORDE, Grafica.REJILLA, Grafica.TEXTO = "#202124", "#55575d", "#33353a", "#cfcfcf"
            self.root.config(bg=c["bg"])
        else:
            st.theme_use(self._tema_base)
            Grafica.FONDO, Grafica.BORDE, Grafica.REJILLA, Grafica.TEXTO = "white", "#bbbbbb", "#eeeeee", "black"
            self.root.config(bg=self._bg_base)
        st.configure("Grande.TButton", font=("Segoe UI", 13, "bold"), padding=8)
        self.fg_texto = c["texto"] if o else "#333333"
        self._recolorear(self.root, o)
        for g in (self.graf, self.graf_bar, self.graf_r):
            g.dibujar()
        Medidor.TEMA = Medidor.OSCURO if o else Medidor.CLARO
        self.medidor._cara()
        self.btn_tema.config(text="Modo claro" if o else "Modo oscuro")

    def _recolorear(self, w, o):
        c = self.OSC
        clase = w.winfo_class()
        if clase in ("Label", "Text", "Listbox"):
            if w not in self._orig:
                self._orig[w] = (str(w.cget("bg")), str(w.cget("fg")))
            bg0, fg0 = self._orig[w]
            fondo_normal = bg0.lower() in (self._bg_base.lower(), "systembuttonface", "systemwindow", "white",
                                           "#ffffff", "#f0f0f0")
            if fondo_normal:
                w.config(bg=(c["campo"] if clase != "Label" else c["bg"]) if o else bg0)
            w.config(fg=self.FG_OSCURO.get(fg0.lower(), fg0) if o else fg0)
            if clase == "Text":
                w.config(insertbackground=c["texto"] if o else "black")
        for hijo in w.winfo_children():
            self._recolorear(hijo, o)

    # ------------------------------------------------------ buscar corto --
    RUTA_CORTO = os.path.join(CARPETA, "buscar_corto.json")

    def _armar_corto(self, nb):
        esc = self.root.winfo_fpixels("1i") / 96.0
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="Buscar corto")
        self.r_hist = deque(maxlen=5)          # resistencias crudas recientes (ohm)
        self.r_serie = deque(maxlen=5000)      # (t, R corregida) para la grafica
        self.r_marca = None
        self.r_actual = None
        self.r_ref = None
        self.corto_activo = False
        self.corto_modo = "medir"
        self.corto_t0 = 0.0
        self.corto_tmax = 0.0
        self.tono_on = False
        try:
            with open(self.RUTA_CORTO, encoding="utf-8") as fh:
                self.r0 = float(json.load(fh).get("r0", 0.0))
        except (OSError, ValueError, TypeError):
            self.r0 = 0.0

        cfg = ttk.LabelFrame(f, text="Prueba", padding=8)
        cfg.pack(side="left", fill="y")
        self.modo_corto = tk.StringVar(value="medir")
        ttk.Radiobutton(cfg, text="Medir resistencia (voltaje bajo)", variable=self.modo_corto,
                        value="medir", command=self._modo_corto).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(cfg, text="Inyectar corriente para calentar", variable=self.modo_corto,
                        value="inyectar", command=self._modo_corto).grid(row=1, column=0, columnspan=2, sticky="w")
        self.e_corto = {}
        for n, (k, t, v) in enumerate([("v", "Voltaje máximo (V)", "0.5"), ("i", "Corriente de prueba (A)", "0.2"),
                                       ("p", "Potencia máxima (W)", "2"), ("t", "Tiempo máximo (s)", "60")], start=2):
            ttk.Label(cfg, text=t).grid(row=n, column=0, sticky="w", pady=2)
            e = ttk.Entry(cfg, width=7, font=("Consolas", 11))
            e.insert(0, v)
            e.grid(row=n, column=1, padx=4)
            self.e_corto[k] = e
        self.btn_corto = ttk.Button(cfg, text="Iniciar prueba", style="Grande.TButton", command=self.iniciar_corto)
        self.btn_corto.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        fila = ttk.Frame(cfg)
        fila.grid(row=7, column=0, columnspan=2, sticky="w")
        ttk.Button(fila, text="Cero de puntas", command=self.cero_puntas).pack(side="left")
        ttk.Button(fila, text="Borrar cero", command=self.borrar_cero).pack(side="left", padx=4)
        fila = ttk.Frame(cfg)
        fila.grid(row=8, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(fila, text="Fijar referencia", command=self.fijar_referencia).pack(side="left")
        ttk.Button(fila, text="Quitar ref.", command=self.quitar_referencia).pack(side="left", padx=4)
        self.v_tono = tk.BooleanVar(value=False)
        ttk.Checkbutton(cfg, text="Tono: más agudo = menos resistencia", variable=self.v_tono,
                        command=self._alternar_tono).grid(row=9, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.lbl_cero = ttk.Label(cfg, foreground=GRIS, text="")
        self.lbl_cero.grid(row=10, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.lbl_ayuda_corto = ttk.Label(cfg, foreground=GRIS, justify="left", wraplength=int(270 * esc))
        self.lbl_ayuda_corto.grid(row=11, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self._mostrar_cero()
        self._modo_corto()

        med = ttk.Frame(f)
        med.pack(side="left", fill="y", padx=(12, 0))
        tk.Label(med, text="Resistencia entre las puntas", font=("Segoe UI", 10), fg=GRIS).pack(anchor="w")
        self.lbl_r = tk.Label(med, text="-- Ω", font=("Consolas", 38, "bold"), fg="#6a1b9a", width=10, anchor="w")
        self.lbl_r.pack(anchor="w")
        self.lbl_r_clase = tk.Label(med, text="Inicia la prueba", font=("Segoe UI", 13, "bold"), fg=GRIS,
                                    wraplength=int(330 * esc), justify="left")
        self.lbl_r_clase.pack(anchor="w")
        self.lbl_r_delta = tk.Label(med, text="", font=("Consolas", 13, "bold"), fg="#333333")
        self.lbl_r_delta.pack(anchor="w", pady=(4, 0))
        self.lbl_r_info = tk.Label(med, text="", font=("Segoe UI", 10), fg="#333333", justify="left")
        self.lbl_r_info.pack(anchor="w", pady=(4, 0))
        an = ttk.Frame(med)
        an.pack(anchor="w", fill="x", pady=(8, 0))
        self.e_nota = ttk.Entry(an, width=28)
        self.e_nota.pack(side="left")
        self.e_nota.insert(0, "")
        self.e_nota.bind("<Return>", lambda e: self.anotar())
        ttk.Button(an, text="Anotar", command=self.anotar).pack(side="left", padx=4)
        self.lst_notas = tk.Listbox(med, height=5, width=46, font=("Consolas", 9))
        self.lst_notas.pack(anchor="w", fill="x", pady=(4, 0))

        self.graf_r = Grafica(f)
        self.graf_r.pack(side="left", fill="both", expand=True, padx=(12, 0))

    def _modo_corto(self):
        modo = self.modo_corto.get()
        if modo == "medir":
            vals, ayuda = {"v": "0.5", "i": "0.2"}, (
                "+ al riel, − a GND, placa sin otra alimentación. Con ≤ 0.5 V los chips no "
                "conducen: lo que mides es el corto. Antes, une las puntas y pulsa «Cero».")
        else:
            vals, ayuda = {"v": "1.0", "i": "1.0"}, (
                "Calienta el componente en corto: búscalo con el dedo, alcohol isopropílico "
                "o la cámara térmica. Empieza con poca corriente y respeta la polaridad.")
        for k, v in vals.items():
            self._poner(self.e_corto[k], v)
        estado = "normal" if modo == "inyectar" else "disabled"
        self.e_corto["p"].config(state=estado)
        self.e_corto["t"].config(state=estado)
        self.lbl_ayuda_corto.config(text=ayuda)

    @staticmethod
    def _fmt_r(r):
        if r is None:
            return "-- Ω"
        if r == float("inf"):
            return "abierto"
        if abs(r) < 0.1:
            return f"{r * 1000:.1f} mΩ"
        if abs(r) < 1:
            return f"{r * 1000:.0f} mΩ"
        if abs(r) < 1000:
            return f"{r:.3g} Ω"
        return f"{r / 1000:.3g} kΩ"

    def _mostrar_cero(self):
        self.lbl_cero.config(text=f"Cero de puntas: {self._fmt_r(self.r0)} (se resta)" if self.r0
                             else "Sin cero de puntas (incluye los cables)")

    def _guardar_cero(self):
        try:
            with open(self.RUTA_CORTO, "w", encoding="utf-8") as fh:
                json.dump({"r0": self.r0}, fh)
        except OSError:
            pass

    def _r_cruda(self):
        vals = sorted(r for r in self.r_hist)
        return vals[len(vals) // 2] if vals else None

    def cero_puntas(self):
        r = self._r_cruda()
        if not self.corto_activo or r is None or r == float("inf"):
            messagebox.showinfo("Cero de puntas", "Une las puntas de la fuente entre sí, inicia la prueba "
                                "«Medir resistencia» y entonces pulsa «Cero de puntas».")
            return
        if r > 2.0:
            messagebox.showwarning("Cero de puntas", f"Mido {self._fmt_r(r)}: parece que las puntas no están unidas.")
            return
        self.r0 = r
        self._guardar_cero()
        self._mostrar_cero()

    def borrar_cero(self):
        self.r0 = 0.0
        self._guardar_cero()
        self._mostrar_cero()

    def fijar_referencia(self):
        if self.r_actual is None or self.r_actual == float("inf"):
            messagebox.showinfo("Referencia", "Primero inicia la prueba para tener una medición.")
            return
        self.r_ref = self.r_actual

    def quitar_referencia(self):
        self.r_ref = None
        self.lbl_r_delta.config(text="")

    def anotar(self):
        texto = self.e_nota.get().strip() or "(sin nota)"
        linea = f"{datetime.now():%H:%M:%S}  {self._fmt_r(self.r_actual):>10}  {texto}"
        self.lst_notas.insert(0, linea)
        self.e_nota.delete(0, "end")
        try:
            os.makedirs(CARPETA_REG, exist_ok=True)
            with open(os.path.join(CARPETA_REG, f"buscar_corto_{datetime.now():%Y%m%d}.txt"), "a",
                      encoding="utf-8") as fh:
                fh.write(f"{datetime.now():%Y-%m-%d} {linea}\n")
        except OSError:
            pass

    def _alternar_tono(self):
        self.tono_on = self.v_tono.get()
        if self.tono_on:
            threading.Thread(target=self._hilo_tono, daemon=True).start()

    def _hilo_tono(self):
        """Pitido continuo: mas agudo y mas rapido cuanto menor es la resistencia."""
        try:
            import winsound
        except ImportError:
            return
        while self.tono_on:
            r = self.r_actual if self.corto_activo else None
            if r is None or r == float("inf"):
                time.sleep(0.2)
                continue
            if self.r_ref:                      # relativo a la referencia: oir si se acerca
                frec = 800.0 * (self.r_ref / max(r, 1e-4)) ** 2
            else:                               # absoluto: 10 ohm -> 300 Hz ... 10 mohm -> 3000 Hz
                frec = 300.0 * 10 ** ((1 - math.log10(max(r, 1e-3))) / 3)
            frec = int(max(150, min(5000, frec)))
            try:
                winsound.Beep(frec, 70)
            except RuntimeError:
                pass
            time.sleep(0.03)

    def iniciar_corto(self):
        if self._detener_si_corre(self.btn_corto):
            return
        modo = self.modo_corto.get()
        try:
            v, i = num(self.e_corto["v"].get()), num(self.e_corto["i"].get())
            pmax, tmax = num(self.e_corto["p"].get()), num(self.e_corto["t"].get())
            _, imax = self.dev.limites()
            if not 0.05 <= v <= 5.0:
                raise ValueError("El voltaje máximo debe estar entre 0.05 y 5 V para buscar cortos")
            if not 0.001 <= i <= imax:
                raise ValueError(f"La corriente debe estar entre 0.001 y {imax:.2f} A")
            if modo == "inyectar" and not (0.1 <= pmax <= 30 and 1 <= tmax <= 3600):
                raise ValueError("Potencia entre 0.1 y 30 W, tiempo entre 1 y 3600 s")
        except ValueError as e:
            messagebox.showwarning("Buscar corto", str(e))
            return
        aviso = (f"Se aplicarán como máximo {v:g} V y {i:g} A.\n\n"
                 "• La placa no debe tener otra alimentación ni baterías conectadas.\n"
                 "• + de la fuente al riel en corto, − a GND (respeta la polaridad).\n")
        if modo == "inyectar":
            aviso += f"• Se apagará sola a los {tmax:g} s o si pasa de {pmax:g} W.\n"
        if not messagebox.askokcancel("Buscar corto", aviso + "\n¿Empezar?"):
            return
        e = self.dev.lectura()
        previo = (e.get("vset"), e.get("iset"), e.get("ocp"), e.get("opp"))
        self.r_hist.clear()
        self.r_serie.clear()
        self.corto_modo, self.corto_tmax = modo, tmax
        self.corto_t0 = time.monotonic()

        def trabajo():
            dev, parar = self.dev, self.parar_trabajo
            motivo, aviso_fuerte = "Prueba detenida", False
            try:
                dev.salida(False)
                dev.fijar_proteccion("ocp", min(imax, max(i * 1.3, i + 0.05)))
                from probador import encender_suave, opp_respaldo
                if modo == "inyectar":     # la propia fuente corta aunque la app se cierre
                    dev.fijar_proteccion("opp", opp_respaldo(pmax))
                # subir desde abajo: la salida nunca queda muy por encima de lo que pide el corto
                if encender_suave(dev, i, v, parar, v_ini=min(0.2, v),
                                  p_max=pmax if modo == "inyectar" else None) is None:
                    return
                self.corto_activo = True
                t0 = time.monotonic()
                i_act, visto_cc, cv_seguidos = i, False, 0
                exceso, marca_ajuste = 0, None
                while not parar.is_set():
                    time.sleep(0.25)
                    e = dev.lectura()
                    if not e.get("salida"):
                        motivo, aviso_fuerte = f"La fuente apagó la salida ({e.get('proteccion') or 'protección'}).", True
                        break
                    if modo != "inyectar" or dev.antiguedad("vout") > 1.0:
                        continue
                    if time.monotonic() - t0 >= tmax:
                        motivo = f"Tiempo terminado ({tmax:g} s). Salida apagada."
                        break
                    pw = e.get("vout", 0.0) * e.get("iout", 0.0)
                    marca = dev.marca("vout")
                    exceso = exceso + 1 if pw > pmax * 1.5 else 0
                    if exceso >= 6:                            # ~1.5 s muy por encima: algo va mal
                        motivo, aviso_fuerte = f"La potencia siguió en {pw:.1f} W (límite {pmax:g} W). Apagado.", True
                        break
                    # bajar la corriente para respetar el limite, solo con una lectura nueva
                    if pw > pmax * 1.05 and marca != marca_ajuste:
                        i_act = i_act * math.sqrt(pmax / pw) * 0.97   # en un corto P = I²·R
                        if i_act < 0.005:
                            motivo, aviso_fuerte = "No se puede respetar la potencia máxima. Apagado.", True
                            break
                        dev.fijar_corriente(i_act)
                        marca_ajuste = dev.marca("vout")
                        dev.esperar("vout", 1.0, despues_de=marca_ajuste)
                        continue
                    if e.get("modo") == "CC":
                        visto_cc, cv_seguidos = True, 0
                    elif visto_cc:
                        cv_seguidos += 1
                        if cv_seguidos >= 3:
                            motivo, aviso_fuerte = ("La fuente pasó de CC a CV: ya no hay corto. "
                                                    "¿Se quemó o se abrió algún componente? Salida apagada."), True
                            break
            except Exception as ex:
                motivo, aviso_fuerte = f"Prueba detenida: {ex}", True
            finally:
                self.corto_activo = False
                try:
                    dev.salida(False)
                except Exception:
                    pass
                try:                     # dejar la fuente como estaba (con la salida apagada)
                    if previo[2] is not None:
                        dev.fijar_proteccion("ocp", previo[2])
                    if previo[3] is not None:
                        dev.fijar_proteccion("opp", previo[3])
                    if previo[1] is not None:
                        dev.fijar_corriente(previo[1])
                    if previo[0] is not None:
                        dev.fijar_voltaje(previo[0])
                except Exception:
                    pass
                self.ui_q.put(("aviso_corto" if aviso_fuerte else "corto_info", motivo))
                self.ui_q.put(("fin_trabajo", self.btn_corto))

        self._lanzar(trabajo, self.btn_corto, "Detener prueba")

    def _actualizar_corto(self, e):
        marca = self.dev.marca("vout") if self.dev else None
        if self.corto_activo and e.get("salida") and marca != self.r_marca:
            self.r_marca = marca
            vo, io = e.get("vout", 0.0), e.get("iout", 0.0)
            self.r_hist.append(vo / io if io > 0.002 else float("inf"))
        r = self._r_cruda() if self.corto_activo else None
        if r is None:
            self.r_actual = None
            if not self.corto_activo:
                self.lbl_r_info.config(text="")
            return
        rc = r if r == float("inf") else max(0.0, r - self.r0)
        self.r_actual = rc
        t = time.monotonic() - self.corto_t0
        if rc != float("inf") and (not self.r_serie or t - self.r_serie[-1][0] >= 0.2):
            self.r_serie.append((t, rc))
        vo, io, iset = e.get("vout", 0.0), e.get("iout", 0.0), e.get("iset", 0.0) or 0.0
        if rc == float("inf"):
            self.lbl_r.config(text=f"> {vo / 0.002:.0f} Ω" if vo > 0 else "abierto")
            clase, color = "Sin corto a este voltaje (casi no pasa corriente)", VERDE
        else:
            self.lbl_r.config(text=self._fmt_r(rc))
            if rc < 0.5:
                clase, color = "CORTO FRANCO", ROJO
            elif rc < 5:
                clase, color = "Resistencia muy baja: probable corto", NARANJA
            elif rc < 50:
                clase, color = "Baja: corto parcial o consumo normal de la placa", "#b8860b"
            else:
                clase, color = "Sin corto a este voltaje", VERDE
        self.lbl_r_clase.config(text=clase, fg=color)
        if self.r_ref is not None and rc != float("inf"):
            d = rc - self.r_ref
            self.lbl_r_delta.config(text=f"Δ {d * 1000:+.1f} mΩ respecto a la referencia",
                                    fg=VERDE if d < 0 else self.fg_texto)
        res = 0.001 / iset if iset > 0 else 0
        info = (f"{vo:.3f} V · {io:.4f} A · {vo * io:.2f} W · {e.get('modo', '')}\n"
                f"Resolución ≈ {self._fmt_r(res)} (sube la corriente para afinar)")
        if self.corto_modo == "inyectar":
            info += f"\nTiempo {t:.0f} / {self.corto_tmax:g} s"
        self.lbl_r_info.config(text=info)
        if self.r_serie:
            fin = self.r_serie[-1][0]
            ini = max(0.0, fin - 120)
            datos = [(a, b) for a, b in self.r_serie if a >= ini]
            self.graf_r.poner([(datos, "#6a1b9a", 0)], "Tiempo (s)", ("Resistencia (Ω)", ""),
                              ("#6a1b9a", "black"), rango_x=(ini, max(fin, ini + 30)))

    # ---------------------------------------------------------- conexion --
    AUTO = "Automático: buscar la DPS-150"

    def refrescar_puertos(self):
        actual = self.cb_puerto.get()
        valores = [self.AUTO] + [f"{d}  -  {desc}" for d, desc in P.buscar_puertos()]
        self.cb_puerto["values"] = valores
        self.cb_puerto.set(actual if actual in valores else self.AUTO)

    def conectar_o_desconectar(self):
        if self.dev:
            self.desconectar()
            return
        if self.esperando is not None:          # cancelar la espera
            self.esperando.set()
            return
        if self.simulado.get():
            self._conectar_a("SIM", PuertoSimulado(resistencia=10.0))
        elif self.cb_puerto.get() == self.AUTO:
            self._esperar_y_conectar()
        else:
            self._conectar_a(self.cb_puerto.get().split()[0], None)

    def _conectar_a(self, puerto, serie):
        self.lbl_info.config(text=f"Conectando a {puerto}...", foreground=GRIS)
        self.btn_conectar.config(state="disabled")

        def hilo():
            dev = P.DPS150(puerto, serie=serie)
            try:
                dev.abrir()
                self.ui_q.put(("conectado", dev))
            except Exception as e:
                self.ui_q.put(("error_conexion", str(e)))
        threading.Thread(target=hilo, daemon=True).start()

    def _esperar_y_conectar(self, aviso=None):
        """La DPS-150 se suelta del USB a los ~4 s si nadie abre su puerto,
        así que se vigila el USB y se abre el puerto en cuanto aparece."""
        cancelar = threading.Event()
        self.esperando = cancelar
        self.btn_conectar.config(text="Cancelar", state="normal")
        self.lbl_info.config(foreground=NARANJA, text=(aviso or "") +
                             "Esperando la DPS-150: desconecta y vuelve a conectar su cable USB")

        def hilo():
            while not cancelar.is_set():
                puerto = P.esperar_dps150(timeout=3600, parar=cancelar)
                if puerto is None:
                    break
                self.ui_q.put(("info", f"Conectando a {puerto}..."))
                dev = P.DPS150(puerto)
                try:
                    dev.abrir()
                except Exception as e:
                    self.ui_q.put(("info", f"Falló ({e}). Sigo esperando..."))
                    time.sleep(1.0)
                    continue
                if cancelar.is_set():
                    dev.cerrar()
                    break
                self.ui_q.put(("conectado", dev))
                return
            self.ui_q.put(("espera_cancelada", None))
        threading.Thread(target=hilo, daemon=True).start()

    def _al_conectar(self, dev):
        self.esperando = None
        self.dev = dev
        dev.rapido = self.v_rapido.get()
        self._hist_ult = 0.0
        e = dev.lectura()
        self.lbl_info.config(text=f"{dev.puerto}: {e.get('modelo', 'DPS-150')}  "
                                  f"HW {e.get('hw', '?')}  FW {e.get('fw', '?')}", foreground=VERDE)
        self.btn_conectar.config(text="Desconectar", state="normal")
        self._habilitar(True)
        if e.get("vset") is not None:
            self._poner(self.e_v, f"{e['vset']:.2f}")
            self._poner(self.e_i, f"{e['iset']:.3f}")
        self.cargar_protecciones()
        self.cargar_memorias()
        self.historia.clear()
        self.t0 = time.monotonic()

    def desconectar(self, preguntar=True):
        if self.dev is None:
            return
        self.parar_trabajo.set()
        if preguntar and self.dev.conectado and self.dev.lectura().get("salida"):
            r = messagebox.askyesnocancel("DPS-150", "La salida sigue encendida.\n¿Apagarla antes de desconectar?")
            if r is None:
                return
            if r:
                self._seguro(self.dev.salida, False)
        self.parar_csv()
        dev, self.dev = self.dev, None
        threading.Thread(target=dev.cerrar, daemon=True).start()
        self.btn_conectar.config(text="Conectar", state="normal")
        self.lbl_info.config(text="Desconectada", foreground=GRIS)
        self._habilitar(False)

    def _habilitar(self, si):
        estado = "normal" if si else "disabled"
        for w in self.controles + self.botones_campo + [self.btn_seq, self.btn_bar, self.btn_csv, self.btn_corto]:
            w.config(state=estado)

    # ---------------------------------------------------------- acciones --
    def _seguro(self, fn, *args):
        """Llama a la fuente y muestra el error en lugar de romper el panel."""
        try:
            fn(*args)
            return True
        except ValueError as e:
            messagebox.showwarning("DPS-150", str(e))
        except P.ErrorDPS as e:
            messagebox.showerror("DPS-150", str(e))
        return False

    @staticmethod
    def _poner(entry, texto):
        entry.delete(0, "end")
        entry.insert(0, texto)

    def aplicar_v(self):
        if not self.dev:
            return
        try:
            v = num(self.e_v.get())
        except ValueError:
            messagebox.showwarning("DPS-150", "Escribe un número de voltaje, por ejemplo 3.3")
            return
        e = self.dev.lectura()
        actual = e.get("vset") or 0.0
        # con la salida encendida, un error de dedo (12 en vez de 1.2) llega directo a la placa
        if e.get("salida") and v > actual + max(0.5, 0.25 * actual):
            if not messagebox.askyesno("DPS-150", f"La salida está ENCENDIDA.\n\n¿Subir de {actual:g} V a {v:g} V?\n"
                                                  "Revisa que no sea un error de dedo."):
                self._poner(self.e_v, f"{actual:.2f}")
                return
        self._seguro(self.dev.fijar_voltaje, v)

    def aplicar_i(self):
        if not self.dev:
            return
        try:
            i = num(self.e_i.get())
        except ValueError:
            messagebox.showwarning("DPS-150", "Escribe un número de corriente, por ejemplo 0.5")
            return
        self._seguro(self.dev.fijar_corriente, i)

    def alternar_salida(self):
        if not self.dev:
            return
        self._seguro(self.dev.salida, not self.dev.lectura().get("salida", False))

    def aplicar_prot(self, clave):
        if not self.dev:
            return
        try:
            val = num(self.e_prot[clave].get())
        except ValueError:
            messagebox.showwarning("DPS-150", f"Valor no válido para {clave.upper()}")
            return
        self._seguro(self.dev.fijar_proteccion, clave, val)

    def cargar_protecciones(self):
        if not self.dev:
            return
        e = self.dev.lectura()
        for clave, ent in self.e_prot.items():
            if e.get(clave) is not None:
                self._poner(ent, f"{e[clave]:.2f}")

    # ---------------------------------------------------------- secuencia --
    # ---------------------------------------------------------- secuencia --
    EJEMPLOS_SEQ = {
        "Encender y apagar (ciclos de arranque)": (
            "Enciende la placa 5 s y la apaga 5 s, muchas veces: sirve para cazar fallas que solo salen al arrancar.",
            [(5.0, 0.5, 5, False), (0.0, 0.5, 5, True)], 50),
        "Tolerancia de 12 V (10.5 / 12 / 13.8 V)": (
            "Prueba si un equipo de 12 V funciona con batería baja (10.5 V), normal y cargando (13.8 V).",
            [(10.5, 1.0, 30, False), (12.0, 1.0, 30, False), (13.8, 1.0, 30, False)], 1),
        "Tolerancia de 5 V USB (4.5 / 5 / 5.25 V)": (
            "Prueba un equipo USB en el mínimo, normal y máximo que permite la norma.",
            [(4.5, 0.5, 30, False), (5.0, 0.5, 30, False), (5.25, 0.5, 30, False)], 1),
        "Tolerancia de 3.3 V (3.0 / 3.3 / 3.6 V)": (
            "Prueba un circuito de 3.3 V en sus límites habituales.",
            [(3.0, 0.3, 30, False), (3.3, 0.3, 30, False), (3.6, 0.3, 30, False)], 1),
        "Subida suave 0 → 12 V": (
            "Sube de 2 en 2 V cada 3 s y luego deja 12 V un minuto: para encender una placa reparada sin golpe.",
            [(float(v), 0.5, 3, False) for v in range(2, 12, 2)] + [(12.0, 0.5, 60, False)], 1),
        "Prueba larga: 12 V durante 1 hora": (
            "Deja la placa trabajando una hora. Activa el Registro CSV para guardar su consumo.",
            [(12.0, 1.0, 3600, False)], 1),
    }

    @staticmethod
    def _seg(texto):
        """'30' -> 30 s, '2m' -> 120 s, '1h' -> 3600 s."""
        t = texto.strip().lower().replace(",", ".").replace(" ", "")
        mult = 1
        for suf, m in (("min", 60), ("h", 3600), ("m", 60), ("s", 1)):
            if t.endswith(suf):
                t, mult = t[: -len(suf)], m
                break
        return float(t) * mult

    @staticmethod
    def _fmt_seg(s):
        s = round(s)
        if s < 60:
            return f"{s} s"
        if s < 3600:
            return f"{s // 60} min {s % 60} s" if s % 60 else f"{s // 60} min"
        return f"{s // 3600} h {s % 3600 // 60} min" if s % 3600 else f"{s // 3600} h"

    def _armar_secuencia(self, nb):
        esc = self.root.winfo_fpixels("1i") / 96.0
        sq = ttk.Frame(nb, padding=8)
        nb.add(sq, text="Secuencia")
        self.seq_pasos = []

        izq = ttk.Frame(sq)
        izq.pack(side="left", fill="both", expand=True)
        arriba = ttk.Frame(izq)
        arriba.pack(fill="x")
        ttk.Label(arriba, text="Ejemplo:").pack(side="left")
        self.cb_ejemplo = ttk.Combobox(arriba, width=40, state="readonly", postcommand=self._seq_lista)
        self.cb_ejemplo.pack(side="left", padx=4)
        ttk.Button(arriba, text="Cargar", command=self._cargar_ejemplo).pack(side="left")
        ttk.Button(arriba, text="Guardar como…", command=self._seq_guardar).pack(side="left", padx=(12, 4))
        ttk.Button(arriba, text="Abrir archivo…", command=self._seq_abrir).pack(side="left")
        self.lbl_ejemplo = ttk.Label(izq, text="La fuente seguirá los pasos de la tabla, uno tras otro. "
                                               "Elige un ejemplo o arma los tuyos abajo.",
                                     foreground=GRIS, wraplength=int(560 * esc), justify="left")
        self.lbl_ejemplo.pack(anchor="w", pady=(4, 4))
        self.arbol_seq = ttk.Treeview(izq, columns=("v", "i", "t", "sal"), show="headings", height=7,
                                      selectmode="browse")
        for col, t, w in (("v", "Voltaje", 90), ("i", "Corriente máx.", 110), ("t", "Duración", 110),
                          ("sal", "Salida", 90)):
            self.arbol_seq.heading(col, text=t)
            self.arbol_seq.column(col, width=int(w * esc), anchor="center")
        self.arbol_seq.pack(fill="both", expand=True)
        self.arbol_seq.bind("<<TreeviewSelect>>", lambda e: self._seq_a_campos())

        ed = ttk.Frame(izq)
        ed.pack(fill="x", pady=(6, 0))
        self.e_seq = {}
        for clave, t, val in (("v", "Voltaje (V)", "5"), ("i", "Corriente máx. (A)", "0.5"),
                              ("t", "Duración (s, 2m, 1h)", "10")):
            ttk.Label(ed, text=t).pack(side="left", padx=(0, 3))
            e = ttk.Entry(ed, width=7, font=("Consolas", 10))
            e.insert(0, val)
            e.pack(side="left", padx=(0, 10))
            self.e_seq[clave] = e
        self.v_seq_off = tk.BooleanVar(value=False)
        ttk.Checkbutton(ed, text="Salida apagada en este paso", variable=self.v_seq_off).pack(side="left")
        bot = ttk.Frame(izq)
        bot.pack(fill="x", pady=(6, 0))
        self.botones_seq = []
        for t, fn in (("Agregar paso", self._seq_agregar), ("Cambiar el seleccionado", self._seq_cambiar),
                      ("Quitar", self._seq_quitar), ("↑ Subir", lambda: self._seq_mover(-1)),
                      ("↓ Bajar", lambda: self._seq_mover(1)), ("Vaciar", self._seq_vaciar)):
            b = ttk.Button(bot, text=t, command=fn)
            b.pack(side="left", padx=(0, 4))
            self.botones_seq.append(b)

        der = ttk.LabelFrame(sq, text="Ejecutar", padding=8)
        der.pack(side="left", fill="y", padx=(10, 0))
        fila = ttk.Frame(der)
        fila.pack(anchor="w")
        ttk.Label(fila, text="Repetir la tabla").pack(side="left")
        self.sp_rep = ttk.Spinbox(fila, from_=1, to=100000, width=6, command=self._seq_resumen)
        self.sp_rep.set(1)
        self.sp_rep.pack(side="left", padx=4)
        self.sp_rep.bind("<KeyRelease>", lambda e: self._seq_resumen())
        ttk.Label(fila, text="veces").pack(side="left")
        self.apagar_seq = tk.BooleanVar(value=True)
        ttk.Checkbutton(der, text="Apagar la salida al terminar", variable=self.apagar_seq) \
            .pack(anchor="w", pady=(6, 0))
        self.lbl_seq_res = ttk.Label(der, text="", font=("Segoe UI", 10, "bold"))
        self.lbl_seq_res.pack(anchor="w", pady=(8, 0))
        self.btn_seq = ttk.Button(der, text="Iniciar secuencia", style="Grande.TButton",
                                  command=self.iniciar_secuencia)
        self.btn_seq.pack(fill="x", pady=(10, 4))
        self.pb_seq = ttk.Progressbar(der, length=int(240 * esc), maximum=100)
        self.pb_seq.pack(fill="x")
        self.lbl_seq = ttk.Label(der, text="", foreground=GRIS, wraplength=int(250 * esc), justify="left")
        self.lbl_seq.pack(anchor="w", pady=(6, 0))
        self._cargar_ejemplo(list(self.EJEMPLOS_SEQ)[1])

    CARPETA_SEQ = os.path.join(CARPETA, "secuencias")
    MIA = "★ "

    def _seq_lista(self):
        """Ejemplos + las secuencias guardadas por el usuario (★)."""
        mias = []
        try:
            mias = sorted(f[:-5] for f in os.listdir(self.CARPETA_SEQ) if f.lower().endswith(".json"))
        except OSError:
            pass
        self.cb_ejemplo["values"] = [self.MIA + m for m in mias] + list(self.EJEMPLOS_SEQ)

    def _seq_guardar(self):
        if not self.seq_pasos:
            return messagebox.showinfo("Secuencia", "La tabla está vacía: no hay nada que guardar.")
        os.makedirs(self.CARPETA_SEQ, exist_ok=True)
        actual = self.cb_ejemplo.get()
        sugerido = actual[len(self.MIA):] if actual.startswith(self.MIA) else ""
        ruta = filedialog.asksaveasfilename(initialdir=self.CARPETA_SEQ, initialfile=sugerido,
                                            defaultextension=".json", filetypes=[("Secuencia", "*.json")],
                                            title="Guardar secuencia")
        if not ruta:
            return
        try:
            reps = max(1, int(self.sp_rep.get()))
        except ValueError:
            reps = 1
        with open(ruta, "w", encoding="utf-8") as fh:
            json.dump({"pasos": self.seq_pasos, "repeticiones": reps,
                       "apagar_al_terminar": self.apagar_seq.get()}, fh, ensure_ascii=False, indent=1)
        nombre = os.path.splitext(os.path.basename(ruta))[0]
        self._seq_lista()
        if os.path.normcase(os.path.dirname(os.path.abspath(ruta))) == os.path.normcase(os.path.abspath(self.CARPETA_SEQ)):
            self.cb_ejemplo.set(self.MIA + nombre)
        self.lbl_ejemplo.config(text=f"Guardada: {nombre}. Aparece con ★ en la lista de arriba.")

    def _seq_abrir(self, ruta=None):
        ruta = ruta or filedialog.askopenfilename(initialdir=self.CARPETA_SEQ if os.path.isdir(self.CARPETA_SEQ)
                                                  else CARPETA, filetypes=[("Secuencia", "*.json")],
                                                  title="Abrir secuencia")
        if not ruta:
            return
        try:
            with open(ruta, encoding="utf-8") as fh:
                d = json.load(fh)
            pasos = [[float(v), float(i), float(s), bool(off)] for v, i, s, off in d["pasos"]]
            if not pasos or any(s <= 0 or i <= 0 or v < 0 for v, i, s, _ in pasos):
                raise ValueError("pasos no válidos")
        except (OSError, ValueError, KeyError, TypeError) as e:
            return messagebox.showwarning("Secuencia", f"No se pudo abrir el archivo:\n{e}")
        self.seq_pasos = pasos
        self.sp_rep.set(int(d.get("repeticiones", 1)))
        self.apagar_seq.set(bool(d.get("apagar_al_terminar", True)))
        nombre = os.path.splitext(os.path.basename(ruta))[0]
        self.lbl_ejemplo.config(text=f"Secuencia «{nombre}». Puedes cambiar cualquier paso antes de iniciar.")
        self._seq_pintar()

    def _cargar_ejemplo(self, nombre=None):
        nombre = nombre or self.cb_ejemplo.get()
        if nombre.startswith(self.MIA):
            return self._seq_abrir(os.path.join(self.CARPETA_SEQ, nombre[len(self.MIA):] + ".json"))
        if nombre not in self.EJEMPLOS_SEQ:
            return
        desc, pasos, reps = self.EJEMPLOS_SEQ[nombre]
        self.cb_ejemplo.set(nombre)
        self.seq_pasos = [list(p) for p in pasos]
        self.sp_rep.set(reps)
        self.lbl_ejemplo.config(text=desc + "  Puedes cambiar cualquier paso antes de iniciar.")
        self._seq_pintar()

    def _seq_pintar(self, sel=None):
        self.arbol_seq.delete(*self.arbol_seq.get_children())
        for k, (v, i, s, off) in enumerate(self.seq_pasos):
            self.arbol_seq.insert("", "end", iid=str(k), values=(
                "—" if off else f"{v:g} V", "—" if off else f"{i:g} A", self._fmt_seg(s),
                "APAGADA" if off else "encendida"))
        if sel is not None and 0 <= sel < len(self.seq_pasos):
            self.arbol_seq.selection_set(str(sel))
            self.arbol_seq.see(str(sel))
        self._seq_resumen()

    def _seq_resumen(self):
        try:
            reps = max(1, int(self.sp_rep.get()))
        except ValueError:
            reps = 1
        vuelta = sum(p[2] for p in self.seq_pasos)
        self.lbl_seq_res.config(text=f"{len(self.seq_pasos)} pasos · {self._fmt_seg(vuelta)} por vuelta\n"
                                     f"Total: {self._fmt_seg(vuelta * reps)}" if self.seq_pasos else
                                "La tabla está vacía")

    def _seq_sel(self):
        s = self.arbol_seq.selection()
        return int(s[0]) if s else None

    def _seq_a_campos(self):
        k = self._seq_sel()
        if k is None:
            return
        v, i, s, off = self.seq_pasos[k]
        self._poner(self.e_seq["v"], f"{v:g}")
        self._poner(self.e_seq["i"], f"{i:g}")
        self._poner(self.e_seq["t"], f"{s:g}")
        self.v_seq_off.set(off)

    def _seq_de_campos(self):
        try:
            v, i, s = num(self.e_seq["v"].get()), num(self.e_seq["i"].get()), self._seg(self.e_seq["t"].get())
        except ValueError:
            messagebox.showwarning("Secuencia", "Revisa los números: voltaje, corriente y duración (ej. 30, 2m, 1h)")
            return None
        if s <= 0 or v < 0 or i <= 0:
            messagebox.showwarning("Secuencia", "La duración y la corriente deben ser mayores que 0")
            return None
        return [v, i, s, self.v_seq_off.get()]

    def _seq_agregar(self):
        p = self._seq_de_campos()
        if p:
            k = self._seq_sel()
            pos = len(self.seq_pasos) if k is None else k + 1
            self.seq_pasos.insert(pos, p)
            self._seq_pintar(pos)

    def _seq_cambiar(self):
        k, p = self._seq_sel(), self._seq_de_campos()
        if k is not None and p:
            self.seq_pasos[k] = p
            self._seq_pintar(k)

    def _seq_quitar(self):
        k = self._seq_sel()
        if k is not None:
            del self.seq_pasos[k]
            self._seq_pintar(min(k, len(self.seq_pasos) - 1))

    def _seq_mover(self, d):
        k = self._seq_sel()
        if k is not None and 0 <= k + d < len(self.seq_pasos):
            self.seq_pasos[k], self.seq_pasos[k + d] = self.seq_pasos[k + d], self.seq_pasos[k]
            self._seq_pintar(k + d)

    def _seq_vaciar(self):
        if self.seq_pasos and messagebox.askyesno("Secuencia", "¿Borrar todos los pasos?"):
            self.seq_pasos = []
            self._seq_pintar()

    def _leer_pasos(self):
        if not self.seq_pasos:
            raise ValueError("La tabla está vacía: agrega pasos o carga un ejemplo")
        return [tuple(p) for p in self.seq_pasos]

    def iniciar_secuencia(self):
        if self._detener_si_corre(self.btn_seq):
            return
        try:
            pasos = self._leer_pasos()
            reps = int(self.sp_rep.get())
            vmax, imax = self.dev.limites()
            for v, i, _, off in pasos:
                if not off and not (0 <= v <= vmax and 0 <= i <= imax):
                    raise ValueError(f"Paso fuera de rango: {v} V / {i} A (máx. {vmax:.1f} V / {imax:.2f} A)")
        except ValueError as e:
            messagebox.showwarning("Secuencia", str(e))
            return
        apagar = self.apagar_seq.get()
        total = sum(p[2] for p in pasos) * reps
        for w in self.botones_seq:
            w.config(state="disabled")

        def trabajo():
            dev, parar = self.dev, self.parar_trabajo
            hecho = 0.0
            try:
                for r in range(reps):
                    for k, (v, i, s, off) in enumerate(pasos, 1):
                        if parar.is_set():
                            return
                        if off:
                            dev.salida(False)
                        else:
                            dev.fijar_corriente(i)
                            dev.fijar_voltaje(v)
                            if not dev.lectura().get("salida"):
                                dev.salida(True)
                        fin = time.monotonic() + s
                        while time.monotonic() < fin:
                            if parar.is_set():
                                return
                            falta = fin - time.monotonic()
                            que = "salida apagada" if off else f"{v:g} V, {i:g} A"
                            self.ui_q.put(("seq_avance", (k - 1, 100 * (hecho + s - falta) / total,
                                           f"Vuelta {r + 1} de {reps} · paso {k} de {len(pasos)}: {que}\n"
                                           f"Faltan {self._fmt_seg(falta)} de este paso · "
                                           f"{self._fmt_seg(total - hecho - s + falta)} en total")))
                            time.sleep(0.25)
                        hecho += s
                self.ui_q.put(("seq", "Secuencia terminada"))
            except Exception as e:
                self.ui_q.put(("aviso", f"Secuencia detenida: {e}"))
            finally:
                if apagar:
                    try:
                        dev.salida(False)
                    except Exception:
                        pass
                if parar.is_set():
                    self.ui_q.put(("seq", "Secuencia detenida"))
                self.ui_q.put(("fin_trabajo", self.btn_seq))

        self._lanzar(trabajo, self.btn_seq, "Detener secuencia")

    # ------------------------------------------------------ hilos de apoyo --
    def _detener_si_corre(self, boton):
        if self.trabajo and self.trabajo.is_alive():
            self.parar_trabajo.set()
            boton.config(text="Deteniendo...")
            return True
        return False

    def _lanzar(self, fn, boton, texto):
        if self.trabajo and self.trabajo.is_alive():
            messagebox.showinfo("DPS-150", "Ya hay una secuencia o un barrido en marcha.")
            return
        self.parar_trabajo.clear()
        self._boton_trabajo = (boton, boton.cget("text"))
        boton.config(text=texto)
        for b in (self.btn_seq, self.btn_bar, self.btn_corto):
            if b is not boton:
                b.config(state="disabled")
        self.trabajo = threading.Thread(target=fn, daemon=True)
        self.trabajo.start()

    # --------------------------------------------------------------- CSV --
    def alternar_csv(self):
        if self.csv_archivo:
            self.parar_csv()
            return
        os.makedirs(CARPETA_REG, exist_ok=True)
        ruta = os.path.join(CARPETA_REG, f"dps150_{datetime.now():%Y%m%d_%H%M%S}.csv")
        self.csv_archivo = open(ruta, "w", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_archivo)
        self.csv_writer.writerow(["fecha_hora", "t_s", "v_programado", "i_programado", "v_salida", "i_salida",
                                  "p_salida", "modo", "salida", "proteccion", "temp_c", "v_entrada", "ah", "wh"])
        self.csv_filas, self.csv_ultimo, self.csv_t0 = 0, 0.0, time.monotonic()
        self.csv_ruta = ruta
        self.btn_csv.config(text="Detener registro")

    def parar_csv(self):
        if self.csv_archivo:
            self.csv_archivo.close()
            self.lbl_csv.config(text=f"Guardado: {self.csv_ruta}  ({self.csv_filas} filas)")
        self.csv_archivo = self.csv_writer = None
        self.btn_csv.config(text="Iniciar registro")

    def _escribir_csv(self, e):
        intervalo = float(self.cb_int.get().split()[0])
        ahora = time.monotonic()
        if ahora - self.csv_ultimo < intervalo - 0.05:
            return
        self.csv_ultimo = ahora
        g = lambda k, f="{:.4f}": f.format(e[k]) if e.get(k) is not None else ""
        self.csv_writer.writerow([
            datetime.now().isoformat(timespec="milliseconds"), f"{ahora - self.csv_t0:.2f}",
            g("vset"), g("iset"), g("vout"), g("iout"), g("pout"), e.get("modo", ""),
            "ON" if e.get("salida") else "OFF", e.get("proteccion", ""), g("temp", "{:.1f}"),
            g("vin", "{:.2f}"), g("ah", "{:.5f}"), g("wh", "{:.5f}")])
        self.csv_filas += 1
        if self.csv_filas % 20 == 0:
            self.csv_archivo.flush()
        self.lbl_csv.config(text=f"Registrando en {self.csv_ruta}  ·  {self.csv_filas} filas")

    # -------------------------------------------------------------- ciclo --
    def tick(self):
        self._procesar_cola()
        dev = self.dev
        if dev is not None:
            if dev.error:
                self.desconectar(preguntar=False)
                if self.cb_puerto.get() == self.AUTO and not self.simulado.get():
                    self._esperar_y_conectar("Se perdió la conexión. ")
                else:
                    self.lbl_info.config(text="Se perdió la conexión: revisa el cable USB", foreground=ROJO)
            else:
                self._mostrar(dev.lectura(), dev.antiguedad("vout"))
        self.root.after(150, self.tick)

    def _procesar_cola(self):
        while True:
            try:
                tipo, dato = self.ui_q.get_nowait()
            except queue.Empty:
                return
            if tipo == "conectado":
                self._al_conectar(dato)
            elif tipo == "error_conexion":
                self.btn_conectar.config(state="normal")
                self.lbl_info.config(text="No se pudo conectar", foreground=ROJO)
                messagebox.showerror("DPS-150", dato)
            elif tipo == "info":
                self.lbl_info.config(text=dato, foreground=NARANJA)
            elif tipo == "espera_cancelada":
                self.esperando = None
                if not self.dev:
                    self.btn_conectar.config(text="Conectar", state="normal")
                    self.lbl_info.config(text="Desconectada", foreground=GRIS)
            elif tipo == "seq":
                self.lbl_seq.config(text=dato)
                self.pb_seq["value"] = 100 if "terminada" in dato else self.pb_seq["value"]
            elif tipo == "seq_avance":
                k, pct, texto = dato
                self.lbl_seq.config(text=texto)
                self.pb_seq["value"] = pct
                if self.arbol_seq.selection() != (str(k),):
                    self.arbol_seq.selection_set(str(k))
                    self.arbol_seq.see(str(k))
            elif tipo == "aviso":
                messagebox.showwarning("DPS-150", dato)
            elif tipo == "aviso_corto":
                self.lbl_r_clase.config(text=dato, fg=ROJO)
                messagebox.showwarning("Buscar corto", dato)
            elif tipo == "llamar":
                dato()
            elif tipo == "corto_info":
                self.lbl_r_clase.config(text=dato, fg=GRIS)
            elif tipo == "fin_trabajo":
                boton, texto = self._boton_trabajo
                boton.config(text=texto)
                for w in self.botones_seq:
                    w.config(state="normal")
                if self.dev:
                    for b in (self.btn_seq, self.btn_bar, self.btn_corto):
                        b.config(state="normal")

    def _mostrar(self, e, edad):
        viejo = edad > 3
        if "vout" in e:
            # todas las lecturas que llegaron desde el ultimo dibujo (hasta ~10 por segundo en modo rapido)
            ult = getattr(self, "_hist_ult", 0.0)
            for tt, v, i, pw in list(self.dev.historial)[-300:]:
                if tt > ult:
                    self.historia.append((tt - self.t0, v, i, pw))
                    ult = tt
            self._hist_ult = ult
            self.lbl_v.config(text=f"{e['vout']:6.3f} V", fg=GRIS if viejo else AZUL)
            self.lbl_i.config(text=f"{e['iout']:6.4f} A", fg=GRIS if viejo else NARANJA)
            self.lbl_p.config(text=f"{e.get('pout', 0):6.2f} W")
            self.medidor.valor(e["iout"] if not viejo else 0.0, activo=not viejo)
        modo = e.get("modo", "--")
        on = e.get("salida", False)
        self.lbl_modo.config(text=modo if on else "--",
                             bg=(ROJO if modo == "CC" else VERDE) if on else GRIS)
        self.lbl_estado_salida.config(text="SALIDA ENCENDIDA" if on else "SALIDA APAGADA",
                                      fg=VERDE if on else GRIS)
        self.btn_salida.config(text="Apagar salida" if on else "Encender salida")
        prot = e.get("proteccion", "")
        self.lbl_prot.config(text=f"¡Protección {prot} activada!" if prot else
                             ("Sin datos de la fuente" if viejo else ""))
        partes = []
        for k, fmt in (("vin", "Entrada {:.2f} V"), ("temp", "Temperatura {:.1f} °C"),
                       ("ah", "{:.4f} Ah"), ("wh", "{:.4f} Wh")):
            if e.get(k) is not None:
                if k == "temp" and not -5 <= e[k] <= 150:
                    partes.append("Temperatura: sin dato válido")   # la DPS-150 FW 1.2 manda ~-16 °C
                    continue
                partes.append(fmt.format(e[k]))
        if e.get("v_max"):
            partes.append(f"Máx. {e['v_max']:.1f} V / {e.get('i_max', 0):.2f} A")
        self.lbl_extra.config(text="\n".join(partes))
        if e.get("vset") is not None:
            self.lbl_fijado.config(text=f"Programado: {e['vset']:.2f} V  /  {e['iset']:.3f} A")
        if self.csv_writer:
            self._escribir_csv(e)
        self._dibujar_grafica()
        self._actualizar_corto(e)

    def _dibujar_grafica(self):
        if not self.historia:
            self.graf.poner([], "Tiempo (s)", ("Voltaje (V)", "Corriente (A)"), (AZUL, NARANJA))
            return
        sel = self.cb_ventana.get()
        ventana = {"30 s": 30, "60 s": 60, "2 min": 120, "5 min": 300, "10 min": 600}.get(sel)
        t_fin = self.historia[-1][0]
        t_ini = 0.0 if ventana is None else max(0.0, t_fin - ventana)
        datos = [h for h in self.historia if h[0] >= t_ini]
        paso = max(1, len(datos) // 1500)
        datos = datos[::paso]
        self.graf.poner([([(t, v) for t, v, _, _ in datos], AZUL, 0),
                         ([(t, i) for t, _, i, _ in datos], NARANJA, 1)],
                        "Tiempo (s)", ("Voltaje (V)", "Corriente (A)"), (AZUL, NARANJA),
                        rango_x=(t_ini, max(t_fin, t_ini + (ventana or 1))))

    def al_cerrar(self):
        self.tono_on = False
        if self.esperando is not None:
            self.esperando.set()
        if self.dev:
            if self.dev.conectado and self.dev.lectura().get("salida"):
                r = messagebox.askyesnocancel("DPS-150", "La salida sigue encendida.\n¿Apagarla antes de salir?")
                if r is None:
                    return
                if r:
                    self._seguro(self.dev.salida, False)
            self.parar_trabajo.set()
            self.parar_csv()
            self.dev.cerrar()
            self.dev = None
        self.root.destroy()


def main():
    try:   # texto nitido en pantallas con escala >100 %
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    Panel(root, simulado="--sim" in sys.argv)
    root.mainloop()


if __name__ == "__main__":
    main()

"""Pestaña Barrido I-V: sube el voltaje por pasos y dibuja la corriente.

  - Grafica interactiva (rueda = zoom, arrastrar = mover, doble clic = ver todo, cursor con V, I y R).
  - Resumen automatico: donde empieza a conducir, resistencia equivalente, maximos, si llego al limite
    y un veredicto (corto/resistencia, codo tipo diodo o regulador, abierto).
  - Tabla de puntos; clic en una fila la marca en la grafica.
  - Comparar con una "curva buena" guardada por modelo y con las ultimas 3 curvas.
  - Siempre empieza desde 0 V y puede detenerse al llegar al limite de corriente.
"""
import csv
import os
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

import diagnostico as D

GRIS, ROJO, VERDE, NARANJA, AMBAR = "#777777", "#c62828", "#1a8f3a", "#e07b00", "#b8860b"
TENUES = ["#8ab4f8", "#c58af9", "#81c995"]


def _ma(a):
    return f"{a * 1000:.1f} mA" if abs(a) < 1 else f"{a:.3f} A"


def _ohm(r):
    if r == float("inf"):
        return "∞"
    return f"{r * 1000:.0f} mΩ" if r < 1 else (f"{r:.3g} Ω" if r < 1000 else f"{r / 1000:.3g} kΩ")


def _interp(curva, v):
    """Corriente de una curva [(v, i)] en el voltaje v (interpolacion lineal), o None fuera de rango."""
    pts = sorted(curva)
    for (va, ia), (vb, ib) in zip(pts, pts[1:]):
        if va <= v <= vb and vb > va:
            return ia + (ib - ia) * (v - va) / (vb - va)
    return None


def analizar_iv(puntos, ilim):
    """puntos = [(v_prog, v, i, modo)]. Devuelve (veredicto, color, [lineas de resumen])."""
    vi = [(v, i) for _, v, i, _ in puntos]
    imax = max(i for _, i in vi)
    vfin = vi[-1][0]
    pmax = max(v * i for v, i in vi)
    lineas = [f"Corriente máxima: {_ma(imax)}", f"Potencia máxima: {pmax:.2f} W"]
    limite = next(((v, i) for v, i in vi if i >= 0.95 * ilim), None)
    if limite:
        lineas.append(f"Llegó al límite de {_ma(ilim)} a {limite[0]:.2f} V")
    # la DPS-150 no distingue menos de ~1-2 mA: por debajo de eso es ruido
    umbral = max(0.002, 0.05 * imax)
    inicio = next(((v, i) for v, i in vi if i > umbral), None)
    if imax < 0.003:
        return ("Casi plana: abierto o sin consumo", AMBAR,
                lineas + [f"Menos de 3 mA hasta {vfin:.2f} V (la fuente no ve menos de 1–2 mA)."])
    cond = [(v, i) for v, i in vi if i > umbral and v > 0.02]
    if inicio:
        lineas.append(f"Empieza a conducir a ~{inicio[0]:.2f} V")
    r_ini = cond[0][0] / cond[0][1] if cond else float("inf")
    lineas.append(f"Resistencia al principio: {_ohm(r_ini)}")
    if len(cond) >= 3:
        (va, ia), (vb, ib) = cond[-3], cond[-1]
        r_din = (vb - va) / (ib - ia) if ib - ia > 1e-6 else float("inf")
        lineas.append(f"Resistencia al final (dinámica): {_ohm(r_din)}")
    # recta desde 0 V: la corriente crece en proporcion al voltaje (R constante)
    rs = [v / i for v, i in cond if i > 0]
    if len(rs) >= 3 and max(rs) / min(rs) < 1.35:        # R casi constante en todo el barrido
        r = sorted(rs)[len(rs) // 2]
        desde0 = inicio and inicio[0] < max(0.25, 0.1 * vfin)
        if r < 5:
            return (f"✗ {'Recta desde 0 V' if desde0 else 'Resistiva'}: CORTO de ~{_ohm(r)}", ROJO, lineas)
        return (f"{'Recta desde 0 V' if desde0 else 'Resistiva'}: ~{_ohm(r)} constante", AMBAR,
                lineas + ["Consumo resistivo: una resistencia, una carga o un corto parcial."])
    # codo brusco (diodo, LED, regulador): pasa del 10 % al 90 % de la corriente en muy poco voltaje
    v10 = next((v for v, i in vi if i >= 0.1 * imax), None)
    v90 = next((v for v, i in vi if i >= 0.9 * imax), None)
    brusco = v10 is not None and v90 is not None and (v90 - v10) <= max(0.15, 0.3 * v10)
    if inicio and inicio[0] >= 0.25 and brusco:
        return (f"Codo a ~{inicio[0]:.2f} V", VERDE,
                lineas + ["Casi no consume hasta ese voltaje y después sube de golpe: se comporta como un diodo, "
                          "un LED o un regulador que arranca."])
    return ("Sube poco a poco, no lineal", NARANJA,
            lineas + ["La corriente crece gradualmente y no como una resistencia simple: fuga o corto parcial "
                      "con semiconductores en paralelo, o una carga que arranca de a poco. Compara con una placa buena."])


class BarridoIV:
    def __init__(self, panel, nb):
        self.p = panel
        esc = panel.root.winfo_fpixels("1i") / 96.0
        self.puntos, self.anteriores = [], []          # anteriores: hasta 3 curvas [(v, i)]
        bw = ttk.Frame(nb, padding=8)
        nb.add(bw, text="Barrido I-V")

        izq = ttk.Frame(bw)
        izq.pack(side="left", fill="y")
        self.e = {}
        for n, (k, t, v) in enumerate([("v0", "Voltaje inicial (V)", "0.0"), ("v1", "Voltaje final (V)", "3.3"),
                                       ("paso", "Paso (V)", "0.1"), ("espera", "Espera por punto (s)", "0.5"),
                                       ("ilim", "Límite de corriente (A)", "0.5")]):
            ttk.Label(izq, text=t).grid(row=n, column=0, sticky="w", pady=2)
            e = ttk.Entry(izq, width=8, font=("Consolas", 11))
            e.insert(0, v)
            e.grid(row=n, column=1, padx=6)
            self.e[k] = e
        self.v_detener = tk.BooleanVar(value=True)
        ttk.Checkbutton(izq, text="Detener al llegar al límite", variable=self.v_detener) \
            .grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.v_apagar = tk.BooleanVar(value=True)
        ttk.Checkbutton(izq, text="Apagar salida al terminar", variable=self.v_apagar) \
            .grid(row=6, column=0, columnspan=2, sticky="w")
        self.v_log = tk.BooleanVar(value=False)
        ttk.Checkbutton(izq, text="Escala logarítmica (corriente)", variable=self.v_log,
                        command=self._log).grid(row=7, column=0, columnspan=2, sticky="w")
        self.btn = ttk.Button(izq, text="Iniciar barrido", style="Grande.TButton", command=self.iniciar)
        self.btn.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        ttk.Label(izq, text="Modelo / placa").grid(row=9, column=0, sticky="w", pady=(6, 0))
        self.cb_mod = ttk.Combobox(izq, width=18, postcommand=lambda: self.cb_mod.configure(
            values=D.modelos_ref("barrido")))
        self.cb_mod.grid(row=9, column=1, sticky="w", pady=(6, 0))
        self.cb_mod.bind("<<ComboboxSelected>>", lambda e: self._pintar())
        fila = ttk.Frame(izq)
        fila.grid(row=10, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(fila, text="Guardar como curva buena", command=self.guardar_buena).pack(side="left")
        ttk.Button(fila, text="CSV", width=5, command=self.guardar_csv).pack(side="left", padx=4)
        ttk.Button(izq, text="Borrar curvas anteriores", command=self._borrar_ant) \
            .grid(row=11, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.lbl_estado = ttk.Label(izq, text="", foreground=GRIS, wraplength=int(250 * esc), justify="left")
        self.lbl_estado.grid(row=12, column=0, columnspan=2, sticky="w", pady=(6, 0))
        panel.controles.append(self.btn)

        self.graf = type(panel.graf)(bw)
        self.graf.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.graf.info_punto = self._info

        der = ttk.Frame(bw)
        der.pack(side="left", fill="y", padx=(10, 0))
        self.lbl_ver = tk.Label(der, text="Sin datos todavía", font=("Segoe UI", 12, "bold"), fg=GRIS,
                                justify="left", wraplength=int(300 * esc), anchor="w")
        self.lbl_ver.pack(anchor="w", fill="x")
        self.lbl_res = tk.Label(der, text="", font=("Segoe UI", 9), fg="#333333", justify="left",
                                wraplength=int(300 * esc), anchor="w")
        self.lbl_res.pack(anchor="w", fill="x", pady=(4, 6))
        self.tabla = ttk.Treeview(der, columns=("vp", "v", "i", "r", "p", "m"), show="headings", height=8)
        for col, t, w in (("vp", "V prog.", 56), ("v", "V", 56), ("i", "I", 70), ("r", "R", 64),
                          ("p", "P (W)", 52), ("m", "", 30)):
            self.tabla.heading(col, text=t)
            self.tabla.column(col, width=int(w * esc), anchor="e")
        self.tabla.pack(fill="both", expand=True)
        self.tabla.bind("<<TreeviewSelect>>", lambda e: self._marcar())

        # para que el resto del panel siga encontrando estos elementos
        panel.btn_bar, panel.graf_bar = self.btn, self.graf
        self._pintar()

    # --- grafica y datos --------------------------------------------------
    def _usa_ma(self):
        todas = [i for _, _, i, _ in self.puntos] + [i for c in self.anteriores for _, i in c]
        return not todas or max(todas) < 1.0

    def _info(self, x, y, eje):
        i = y / 1000 if self._usa_ma() else y
        r = _ohm(x / i) if i > 1e-6 else "∞"
        return f"{x:.3f} V · {_ma(i)} · {r}"

    def _log(self):
        self.graf.log_y = self.v_log.get()
        self.graf.log_min = 0.1 if self._usa_ma() else 1e-4     # la fuente no ve menos de ~0.1-1 mA
        self.graf.vista = None
        self._pintar()

    def _pintar(self):
        k = 1000 if self._usa_ma() else 1
        series = []
        modelo = self.cb_mod.get().strip()
        ref = D.leer_ref("barrido", modelo) if modelo else None
        if ref:
            series.append(([(v, i * k) for v, i in ref["curva"]], "#9aa0a6", 0, {"ancho": 3, "puntos": False}))
        for n, c in enumerate(self.anteriores):
            series.append(([(v, i * k) for v, i in c], TENUES[n % 3], 0, {"ancho": 1, "dash": (4, 3),
                                                                            "puntos": False, "fondo": True}))
        series.append(([(v, i * k) for _, v, i, _ in self.puntos], NARANJA, 0))
        self.graf.log_min = 0.1 if k == 1000 else 1e-4
        self.graf.poner(series, "Voltaje medido (V) · rueda = zoom · arrastrar = mover · doble clic = todo",
                        ("Corriente (mA)" if k == 1000 else "Corriente (A)", ""), (NARANJA, "black"))

    def _llenar_tabla(self):
        self.tabla.delete(*self.tabla.get_children())
        for n, (vp, v, i, modo) in enumerate(self.puntos):
            self.tabla.insert("", "end", iid=str(n), values=(
                f"{vp:.2f}", f"{v:.3f}", _ma(i), _ohm(v / i) if i > 1e-6 else "∞", f"{v * i:.3f}", modo))

    def _marcar(self):
        sel = self.tabla.selection()
        if not sel:
            return
        vp, v, i, _ = self.puntos[int(sel[0])]
        self.graf.marca = (v, i * (1000 if self._usa_ma() else 1), 0)
        self.graf.dibujar()

    def _analizar(self, ilim):
        if not self.puntos:
            return
        texto, color, lineas = analizar_iv(self.puntos, ilim)
        modelo = self.cb_mod.get().strip()
        ref = D.leer_ref("barrido", modelo) if modelo else None
        if ref:
            difs = [(v, i, _interp(ref["curva"], v)) for _, v, i, _ in self.puntos]
            difs = [(v, i, b) for v, i, b in difs if b is not None]
            if difs:
                v, i, b = max(difs, key=lambda x: abs(x[1] - x[2]))
                lineas.append(f"Contra la curva buena de «{modelo}»: la mayor diferencia es a {v:.2f} V "
                              f"({_ma(i)} contra {_ma(b)})")
        self.lbl_ver.config(text=texto, fg=color)
        self.lbl_res.config(text="\n".join("• " + l for l in lineas), fg=self.p.fg_texto)

    # --- barrido ----------------------------------------------------------
    def iniciar(self):
        if self.p._detener_si_corre(self.btn) or not self.p.dev:
            return
        try:
            v0, v1, paso, espera, ilim = (D._num(self.e[k].get()) for k in ("v0", "v1", "paso", "espera", "ilim"))
            vmax, imax = self.p.dev.limites()
            if paso <= 0 or espera < 0:
                raise ValueError("El paso debe ser mayor que 0 y la espera no puede ser negativa")
            if not (0 <= v0 <= vmax and 0 <= v1 <= vmax and 0 < ilim <= imax):
                raise ValueError(f"Fuera de rango (máx. {vmax:.1f} V / {imax:.2f} A)")
        except ValueError as ex:
            return messagebox.showwarning("Barrido I-V", str(ex))
        signo = 1 if v1 >= v0 else -1
        n = int(abs(v1 - v0) / paso + 1e-9) + 1
        voltajes = [round(v0 + signo * k * paso, 4) for k in range(n)]
        if abs(voltajes[-1] - v1) > 1e-6:
            voltajes.append(v1)
        # llegar a v0 desde 0 V por pasos, sin registrar
        previos = [round(k * paso, 4) for k in range(1, int(v0 / paso + 1e-9) + 1) if k * paso < v0 - 1e-6]
        if self.puntos:
            self.anteriores = ([[(v, i) for _, v, i, _ in self.puntos]] + self.anteriores)[:3]
        self.puntos = []
        self.graf.vista, self.graf.marca = None, None
        self._llenar_tabla()
        self._pintar()
        e = self.p.dev.lectura()
        previo = (e.get("vset"), e.get("iset"), e.get("salida"))
        apagar, detener = self.v_apagar.get(), self.v_detener.get()

        def trabajo():
            dev, parar = self.p.dev, self.p.parar_trabajo
            motivo = None
            try:
                dev.salida(False)
                dev.fijar_corriente(ilim)
                dev.fijar_voltaje(0.0)
                dev.salida(True)
                for v in previos:
                    if parar.is_set():
                        break
                    dev.fijar_voltaje(v)
                    time.sleep(0.15)
                for k, v in enumerate(voltajes, 1):
                    if parar.is_set():
                        break
                    dev.fijar_voltaje(v)
                    r = dev.medir_estable(espera, parar)
                    if r is None:
                        break
                    if not r.get("salida", True):
                        motivo = f"La fuente apagó la salida ({r.get('proteccion') or 'protección'})"
                        break
                    punto = (v, r.get("vout", 0.0), r.get("iout", 0.0), r.get("modo", ""))
                    self.p.ui_q.put(("llamar", lambda pt=punto, k=k: self._nuevo_punto(pt, k, len(voltajes))))
                    if detener and punto[2] >= 0.95 * ilim:
                        motivo = f"Se detuvo al llegar al límite de {_ma(ilim)} a {punto[1]:.2f} V"
                        break
            except Exception as ex:
                motivo = f"Barrido detenido: {ex}"
            finally:
                try:
                    if apagar or not previo[2]:
                        dev.salida(False)
                    if previo[1] is not None:
                        dev.fijar_corriente(previo[1])
                        dev.fijar_voltaje(previo[0])
                except Exception:
                    pass
                m = motivo or ("Barrido detenido" if parar.is_set() else f"Barrido terminado: {len(voltajes)} puntos")
                self.p.ui_q.put(("llamar", lambda: (self.lbl_estado.config(text=m), self._analizar(ilim))))
                self.p.ui_q.put(("fin_trabajo", self.btn))

        self.lbl_estado.config(text="Subiendo desde 0 V…")
        self.p._lanzar(trabajo, self.btn, "Detener barrido")

    def _nuevo_punto(self, punto, k, n):
        self.puntos.append(punto)
        vp, v, i, modo = punto
        self.tabla.insert("", "end", iid=str(len(self.puntos) - 1), values=(
            f"{vp:.2f}", f"{v:.3f}", _ma(i), _ohm(v / i) if i > 1e-6 else "∞", f"{v * i:.3f}", modo))
        self.tabla.see(str(len(self.puntos) - 1))
        self.lbl_estado.config(text=f"Punto {k} de {n}: {vp:g} V → {_ma(i)}")
        self._pintar()

    def _borrar_ant(self):
        self.anteriores = []
        self._pintar()

    def guardar_buena(self):
        modelo = self.cb_mod.get().strip()
        if not modelo or len(self.puntos) < 3:
            return messagebox.showinfo("Barrido I-V", "Escribe el modelo y mide primero la curva de una placa BUENA.")
        D.guardar_ref("barrido", modelo, {"curva": [(v, i) for _, v, i, _ in self.puntos],
                                          "fecha": time.strftime("%Y-%m-%d %H:%M")})
        self._pintar()
        self.lbl_estado.config(text=f"Guardada como curva buena de «{modelo}». Se dibuja en gris para comparar.")

    def guardar_csv(self):
        if not self.puntos:
            return messagebox.showinfo("Barrido I-V", "Todavía no hay datos de barrido.")
        carpeta = os.path.join(D.CARPETA, "registros")
        os.makedirs(carpeta, exist_ok=True)
        ruta = filedialog.asksaveasfilename(initialdir=carpeta, defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv")],
                                            initialfile=f"barrido_IV_{datetime.now():%Y%m%d_%H%M%S}.csv")
        if not ruta:
            return
        with open(ruta, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["v_programado", "v_medido", "i_medido", "resistencia_ohm", "potencia_w", "modo"])
            for vp, v, i, modo in self.puntos:
                w.writerow([f"{vp:.4f}", f"{v:.4f}", f"{i:.5f}", f"{v / i:.3f}" if i > 1e-4 else "",
                            f"{v * i:.4f}", modo])
        self.lbl_estado.config(text=f"Guardado en {ruta}")

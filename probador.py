"""Probador de componentes para el panel DPS-150.

La fuente solo tiene dos puntas (+ y -), asi que el probador guia paso a paso:
dice donde va cada punta, mide con la corriente limitada y al final deduce
que pieza es, su patillaje y si esta danada.

Principio: en CC (corriente constante) el voltaje que marca la fuente es la
caida de la pieza a esa corriente (Vf de un diodo, Vz de un zener, Vgs de un
MOSFET con compuerta unida al drenador...). Si queda en CV casi sin
corriente, la pieza no conduce en esa direccion.
"""
import time
import tkinter as tk
from tkinter import messagebox, ttk

GRIS, ROJO, VERDE, NARANJA = "#777777", "#c62828", "#1a8f3a", "#e07b00"

PIEZAS = ["Diodo / LED", "Zener", "Transistor BJT", "MOSFET", "Capacitor"]
PARES = [(1, 2), (2, 1), (1, 3), (3, 1), (2, 3), (3, 2)]


def conduce(m):
    """m = (i_limite, v, i, modo). True si la pieza deja pasar la corriente de prueba."""
    il, v, i, modo = m
    # solo por la corriente: el modo CV/CC llega en otro registro y puede venir atrasado
    return i >= 0.6 * il


def subir_hasta_cc(dev, i_lim, v_tope, parar, v_desde, p_max=None):
    """Sube el voltaje poco a poco (la salida ya encendida) hasta que la pieza toma la
    corriente pedida (CC) o se llega a v_tope. Devuelve la ultima lectura o None si se paro.

    Asi la salida nunca queda muy por encima del voltaje de la pieza: si quedara alta,
    el capacitor de salida de la fuente descargaria un golpe de corriente en ella
    (eso es lo que quema LEDs aunque haya limite de corriente).
    """
    v = v_desde
    while not parar.is_set():
        r = dev.medir_estable(0.1, parar)
        if r is None:
            return None
        i = r.get("iout", 0.0)
        # no usar el modo CC/CV: llega en otro registro y puede ser el del paso anterior
        if i >= 0.85 * i_lim or v >= v_tope - 1e-6:
            return r
        if p_max and r.get("vout", 0.0) * i >= 0.9 * p_max:      # no pasar de la potencia maxima
            return r
        paso = 0.04 if i > 0.05 * i_lim else max(0.1, 0.08 * v)
        v = min(v_tope, v + paso)
        dev.fijar_voltaje(v)
    return None


def encender_suave(dev, i_lim, v_tope, parar, v_ini=1.0, p_max=None):
    """Enciende la salida a voltaje bajo con el limite puesto y sube hasta que conduzca."""
    dev.salida(False)
    dev.fijar_corriente(i_lim)
    v0 = min(v_ini, v_tope)
    dev.fijar_voltaje(v0)
    dev.salida(True)
    return subir_hasta_cc(dev, i_lim, v_tope, parar, v0, p_max)


def opp_respaldo(p_max):
    """Proteccion de potencia dentro de la fuente: respaldo si la app falla, con margen para no cortar."""
    return p_max * 1.5 + 0.5


def tipo_por_vf(vf):
    if vf < 0.12:
        return "EN CORTO", ROJO
    if vf < 0.45:
        return "diodo Schottky o de germanio", VERDE
    if vf < 0.95:
        return "diodo de silicio (rectificador o de señal)", VERDE
    if vf < 1.55:
        return "LED infrarrojo o dos diodos en serie", VERDE
    if vf < 2.25:
        return "LED rojo, naranja o amarillo", VERDE
    if vf < 2.6:
        return "LED verde-amarillo", VERDE
    if vf < 3.8:
        return "LED verde, azul, blanco o UV", VERDE
    return "varios diodos/LEDs en serie, o un zener", NARANJA


class Probador:
    def __init__(self, panel, nb):
        self.p = panel
        self.pasos, self.k, self.res, self.bjt = [], 0, {}, None
        self.confirmado = False
        esc = panel.root.winfo_fpixels("1i") / 96.0
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="Probador")

        izq = ttk.LabelFrame(f, text="Pieza", padding=8)
        izq.pack(side="left", fill="y")
        self.v_pieza = tk.StringVar(value=PIEZAS[0])
        for n, t in enumerate(PIEZAS):
            ttk.Radiobutton(izq, text=t, value=t, variable=self.v_pieza, command=self.reiniciar) \
                .grid(row=n, column=0, columnspan=2, sticky="w")
        # opciones de cada pieza: solo se ve la de la pieza elegida
        self.opciones = {}
        op = ttk.LabelFrame(izq, text="Opciones", padding=6)
        op.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.cb_canal = ttk.Combobox(op, width=4, state="readonly", values=["N", "P"])
        self.cb_canal.current(0)
        self.cb_canal.bind("<<ComboboxSelected>>", lambda e: self.reiniciar())
        self.e_rb = ttk.Entry(op, width=7, font=("Consolas", 11))
        self.e_rb.insert(0, "47")
        self.e_vcap = ttk.Entry(op, width=7, font=("Consolas", 11))
        self.e_vcap.insert(0, "16")
        self.e_vcap.bind("<KeyRelease>", lambda e: self._mostrar_paso())
        for pieza, texto, campo in (("MOSFET", "Canal del MOSFET", self.cb_canal),
                                    ("Transistor BJT", "Resistencia de base\npara hFE (kΩ)", self.e_rb),
                                    ("Capacitor", "Voltaje del capacitor (V)\n(el que dice su cuerpo)", self.e_vcap)):
            lbl = ttk.Label(op, text=texto, justify="left")
            self.opciones[pieza] = (lbl, campo)
        self.lbl_sin_op = ttk.Label(op, text="Esta pieza no necesita opciones.", foreground=GRIS)
        ttk.Button(izq, text="Reiniciar", command=self.reiniciar).grid(row=9, column=0, columnspan=2,
                                                                         sticky="ew", pady=(10, 0))
        ttk.Label(izq, foreground=GRIS, wraplength=int(190 * esc), justify="left", text=(
            "Prueba la pieza fuera de la placa. La corriente siempre va limitada "
            "(20 mA o menos), así que no se daña.")).grid(row=10, column=0, columnspan=2, sticky="w", pady=(8, 0))

        mid = ttk.LabelFrame(f, text="Paso", padding=8)
        mid.pack(side="left", fill="both", padx=(10, 0))
        self.lbl_paso = ttk.Label(mid, text="", font=("Segoe UI", 10, "bold"))
        self.lbl_paso.pack(anchor="w")
        self.lbl_instr = tk.Label(mid, text="", font=("Segoe UI", 14), justify="left",
                                  wraplength=int(360 * esc), anchor="w")
        self.lbl_instr.pack(anchor="w", fill="x", pady=(6, 10))
        fila = ttk.Frame(mid)
        fila.pack(anchor="w")
        self.btn_medir = ttk.Button(fila, text="Medir", style="Grande.TButton", command=self.medir)
        self.btn_medir.pack(side="left")
        self.btn_atras = ttk.Button(fila, text="Repetir anterior", command=self.atras)
        self.btn_atras.pack(side="left", padx=6)
        self.btn_hfe = ttk.Button(mid, text="Medir ganancia hFE", command=self.medir_hfe)
        self.lbl_med = ttk.Label(mid, text="", foreground=GRIS)
        self.lbl_med.pack(anchor="w", pady=(8, 0))
        panel.controles += [self.btn_medir, self.btn_atras, self.btn_hfe]

        der = ttk.LabelFrame(f, text="Resultado", padding=8)
        der.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.txt = tk.Text(der, height=10, width=40, font=("Consolas", 10), wrap="word",
                           relief="flat", borderwidth=0)
        self.txt.pack(fill="both", expand=True)
        self.txt.tag_configure("ok", foreground=VERDE, font=("Segoe UI", 12, "bold"))
        self.txt.tag_configure("mal", foreground=ROJO, font=("Segoe UI", 12, "bold"))
        self.txt.tag_configure("aviso", foreground=NARANJA, font=("Segoe UI", 11, "bold"))
        self.reiniciar()

    # ------------------------------------------------------------ pasos --
    def _hacer_pasos(self):
        pieza = self.v_pieza.get()
        vmax = self._vmax()
        if pieza == "Diodo / LED":
            return [dict(t="+ al ÁNODO, − al CÁTODO (la banda o la pata corta del LED).",
                         clave="dir", v=min(6.0, vmax), i=[0.005, 0.010, 0.020], suave=True),
                    dict(t="⟲ VOLTEA LA PIEZA\nAhora + al CÁTODO, − al ÁNODO.", clave="inv", v=4.0, i=[0.005],
                         suave=True, voltear_de="dir")]
        if pieza == "Zener":
            return [dict(t="+ al CÁTODO (la banda), − al ÁNODO.", clave="zen", v=vmax, i=[0.005, 0.010],
                         suave=True),
                    dict(t="⟲ VOLTEA LA PIEZA\nAhora + al ÁNODO, − al CÁTODO.", clave="dir", v=3.0, i=[0.005],
                         suave=True, voltear_de="zen")]
        if pieza == "Transistor BJT":
            return [dict(t=f"Numera las patas 1-2-3 de izquierda a derecha.\n+ a la pata {a}, − a la pata {b}. "
                           "La tercera, al aire.", clave=(a, b), v=3.0, i=[0.005], suave=True) for a, b in PARES]
        if pieza == "MOSFET":
            n = self.cb_canal.get() == "N"
            pos, neg = ("D", "S") if n else ("S", "D")
            return [dict(t=f"Toca G con S un segundo para descargarlo.\n+ a {pos}, − a {neg}, G al aire.",
                         clave="ds", v=3.0, i=[0.010], suave=True),
                    dict(t=f"+ a {neg}, − a {pos} (diodo interno).", clave="diodo", v=3.0, i=[0.010], suave=True),
                    dict(t=f"Une G con D con un cable.\n+ a {pos}, − a {neg}.", clave="vth",
                         v=min(10.0, vmax), i=[0.002, 0.005, 0.010], suave=True)]
        return [dict(t="Capacitor DESCARGADO. + a la pata positiva, − a la negativa (la de la franja).",
                     clave="cap", v=None, i=None)]

    def _v_prueba_cap(self):
        """Voltaje al que se prueba el capacitor: 90 % del suyo, sin pasar del maximo de la fuente."""
        try:
            vn = float(self.e_vcap.get().replace(",", "."))
        except ValueError:
            return None, None
        return vn, min(vn * 0.9, self._vmax())

    def _mostrar_opciones(self):
        pieza = self.v_pieza.get()
        for lbl, campo in self.opciones.values():
            lbl.grid_remove()
            campo.grid_remove()
        self.lbl_sin_op.grid_remove()
        if pieza in self.opciones:
            lbl, campo = self.opciones[pieza]
            lbl.grid(row=0, column=0, sticky="w")
            campo.grid(row=0, column=1, sticky="w", padx=(6, 0))
        else:
            self.lbl_sin_op.grid(row=0, column=0, sticky="w")

    def _vmax(self):
        vm, _ = self.p.dev.limites() if self.p.dev else (19.0, 5.0)
        return round(vm - 0.3, 1)

    def reiniciar(self):
        self.pasos, self.k, self.res, self.bjt = self._hacer_pasos(), 0, {}, None
        self._insistio = False
        self.btn_hfe.pack_forget()
        self._mostrar_opciones()
        self.txt.delete("1.0", "end")
        self.lbl_med.config(text="")
        self._mostrar_paso()

    def _mostrar_paso(self):
        if self.k < len(self.pasos):
            self.lbl_paso.config(text=f"{self.v_pieza.get()} · paso {self.k + 1} de {len(self.pasos)}")
            texto = self.pasos[self.k]["t"]
            if self.pasos[self.k]["clave"] == "cap":
                vn, vt = self._v_prueba_cap()
                if vn and vt > 0:
                    texto += f"\n\nCapacitor de {vn:g} V → se probará a {vt:.1f} V con 20 mA máximo."
                    if vt < vn * 0.85:
                        texto += (f"\nOJO: la fuente solo llega a {vt:.1f} V ({vt / vn * 100:.0f} % de {vn:g} V): "
                                  "detecta cortos y fugas grandes, pero no lo prueba a su voltaje completo.")
                else:
                    texto += "\n\nEscribe a la izquierda, en Opciones, el voltaje del capacitor."
            self.lbl_instr.config(text=texto, fg=getattr(self.p, "fg_texto", "#333333"))
            self.btn_medir.config(text="Medir")
        else:
            self.lbl_paso.config(text=f"{self.v_pieza.get()} · terminado")
            self.lbl_instr.config(text="Listo. Pulsa Reiniciar para probar otra pieza.",
                                  fg=getattr(self.p, "fg_texto", "#333333"))
            self.btn_medir.config(text="Medir")

    def atras(self):
        if self.k > 0:
            self.k -= 1
            self._mostrar_paso()

    # ---------------------------------------------------------- medicion --
    def _confirmar(self):
        if self.confirmado:
            return True
        self.confirmado = messagebox.askokcancel(
            "Probador", "La pieza debe estar fuera de la placa (o al menos sin alimentación).\n"
                        "La fuente encenderá la salida unos segundos en cada paso, con la corriente limitada.\n\n"
                        "¿Continuar?")
        return self.confirmado

    def medir(self):
        if self.p._detener_si_corre(self.btn_medir):
            return
        if not self.p.dev or self.k >= len(self.pasos) or not self._confirmar():
            return
        paso = self.pasos[self.k]
        if paso["clave"] == "cap":
            return self._medir_capacitor()
        self.p._lanzar(lambda: self._trabajo(paso["v"], paso["i"], lambda r: self._paso_listo(paso, r),
                                             suave=paso.get("suave", False)), self.btn_medir, "Midiendo…")

    def _trabajo(self, v, corrientes, al_terminar, espera=1.3, suave=False):
        """Hilo: enciende la salida, mide a cada corriente, apaga y restaura la fuente.

        suave=True (diodos, LEDs, zener): el voltaje sube desde abajo hasta que la pieza
        conduce, en vez de encender directo al voltaje maximo.
        """
        dev, parar = self.p.dev, self.p.parar_trabajo
        e = dev.lectura()
        previo = (e.get("vset"), e.get("iset"))
        res, error = [], None
        try:
            if suave:
                r = encender_suave(dev, corrientes[0], v, parar)
                for n, il in enumerate(corrientes):
                    if r is None:
                        break
                    if n:
                        dev.fijar_corriente(il)
                        r = subir_hasta_cc(dev, il, v, parar, r.get("vset", r.get("vout", 0.0)))
                        if r is None:
                            break
                    r = dev.medir_estable(0.5, parar) or r
                    res.append((il, r.get("vout", 0.0), r.get("iout", 0.0), r.get("modo", "")))
                    if not conduce(res[-1]):
                        break           # no conduce ni al maximo: no tiene caso seguir subiendo
            else:
                dev.salida(False)
                dev.fijar_corriente(corrientes[0])
                dev.fijar_voltaje(v)
                dev.salida(True)
                for n, il in enumerate(corrientes):
                    if n:
                        dev.fijar_corriente(il)
                    r = dev.medir_estable(espera, parar)
                    if r is None:
                        break
                    res.append((il, r.get("vout", 0.0), r.get("iout", 0.0), r.get("modo", "")))
        except Exception as ex:
            error = str(ex)
        finally:
            try:
                dev.salida(False)
                if previo[1] is not None:
                    dev.fijar_corriente(previo[1])
                    dev.fijar_voltaje(previo[0])
            except Exception:
                pass
            self.p.ui_q.put(("llamar", (lambda: messagebox.showwarning("Probador", error)) if error
                             else (lambda: al_terminar(res))))
            self.p.ui_q.put(("fin_trabajo", self.btn_medir))

    def _paso_listo(self, paso, res):
        if not res:
            return
        self.lbl_med.config(text="Medido: " + "   ".join(
            f"{il * 1000:g} mA → {v:.3f} V ({modo}, {i * 1000:.1f} mA)" for il, v, i, modo in res))
        # ¿de verdad volteo la pieza? si mide lo mismo que el paso anterior, no la volteo
        previo = self.res.get(paso.get("voltear_de")) if paso.get("voltear_de") else None
        if (previo and conduce(res[0]) and conduce(previo[0]) and abs(res[0][1] - previo[0][1]) < 0.03
                and not self._insistio):
            self._insistio = True
            self.lbl_instr.config(fg=ROJO, text=(
                f"⚠ Mediste lo mismo que en el paso anterior ({res[0][1]:.3f} V).\n"
                "Parece que NO volteaste la pieza. Voltéala y pulsa Medir otra vez.\n"
                "(Si de verdad la volteaste, pulsa Medir: puede ser una resistencia o una pieza en corto.)"))
            return
        self._insistio = False
        self.res[paso["clave"]] = res
        self.k += 1
        self._mostrar_paso()
        if self.k >= len(self.pasos):
            self._analizar()

    # ---------------------------------------------------------- analisis --
    def _escribir(self, texto, tag=None):
        self.txt.insert("end", texto + "\n", tag)

    def _analizar(self):
        self.txt.delete("1.0", "end")
        pieza = self.v_pieza.get()
        {"Diodo / LED": self._an_diodo, "Zener": self._an_zener,
         "Transistor BJT": self._an_bjt, "MOSFET": self._an_mosfet}[pieza]()

    def _an_diodo(self):
        d, inv = self.res["dir"], self.res["inv"][0]
        if not conduce(d[-1]):
            if conduce(inv):
                self._escribir("Conduce al revés: lo conectaste invertido.", "aviso")
                self._escribir(f"Vf ≈ {inv[1]:.3f} V a 5 mA. Repite con las puntas al revés.")
            else:
                self._escribir("ABIERTO: no conduce en ningún sentido.", "mal")
                self._escribir(f"(O su Vf es mayor que {d[-1][1]:.1f} V.)")
            return
        vf = d[-1][1]
        # una resistencia: el voltaje crece en proporcion a la corriente (un diodo casi no cambia)
        if len(d) >= 3 and d[0][1] > 0.05 and d[-1][1] / d[0][1] > 2.5:
            r = d[-1][1] / max(d[-1][2], 1e-6)
            self._escribir(f"Parece una RESISTENCIA de ~{r:.0f} Ω, no un diodo.", "aviso")
            self._escribir("El voltaje sube en proporción a la corriente y mide igual en los dos sentidos.")
            return
        tipo, color = tipo_por_vf(vf)
        if color == ROJO:
            self._escribir("EN CORTO: conduce casi sin caída de voltaje.", "mal")
            return
        if conduce(inv) and abs(inv[1] - d[0][1]) < 0.03:
            self._escribir("¿Invertiste las puntas en el paso 2?", "aviso")
            self._escribir(f"Al revés se midió lo mismo que en directo ({inv[1]:.3f} V): parece que la pieza "
                           "quedó igual. Pulsa «Repetir anterior» y mide el paso 2 con las puntas al revés.")
        elif conduce(inv) and inv[1] < 4.0:
            self._escribir("CON FUGA o en corto: conduce también al revés.", "mal")
        else:
            self._escribir("BUENO ✓", "ok")
        self._escribir(f"Parece: {tipo}")
        for il, v, i, modo in d:
            self._escribir(f"  Vf a {il * 1000:>4g} mA = {v:.3f} V")
        if len(d) >= 2 and d[-1][2] > d[0][2]:
            rd = (d[-1][1] - d[0][1]) / (d[-1][2] - d[0][2])
            self._escribir(f"  Resistencia dinámica ≈ {rd:.1f} Ω")
        if vf >= 1.55:
            self._escribir("¿Encendió el LED? Si no encendió, está dañado o es IR (míralo con la cámara del celular).")

    def _an_zener(self):
        z, d = self.res["zen"], self.res["dir"][0]
        if not conduce(z[0]):
            self._escribir(f"No hay ruptura hasta {z[0][1]:.1f} V.", "aviso")
            self._escribir("Su voltaje zener es mayor que eso, o la pieza está abierta.")
        elif z[0][1] < 0.12:
            self._escribir("EN CORTO.", "mal")
            return
        else:
            self._escribir(f"Vz ≈ {z[0][1]:.2f} V a 5 mA ✓", "ok")
            if len(z) > 1 and z[1][2] > z[0][2]:
                self._escribir(f"  Vz a 10 mA = {z[1][1]:.2f} V · Rz ≈ {(z[1][1] - z[0][1]) / (z[1][2] - z[0][2]):.1f} Ω")
        if conduce(d) and conduce(z[0]) and abs(d[1] - z[0][1]) < 0.03:
            self._escribir(f"En el paso 2 se midió lo mismo que en el 1 ({d[1]:.3f} V): ¿invertiste las puntas? "
                           "Pulsa «Repetir anterior» y repite el paso 2.", "aviso")
        elif conduce(d):
            self._escribir(f"Directo: Vf = {d[1]:.3f} V ✓")
        else:
            self._escribir("Directo: no conduce. Si no volteaste la pieza en el paso 2, pulsa «Repetir anterior» "
                           "y mídelo volteada. Si la volteaste, la pieza está abierta.", "aviso")

    def _an_bjt(self):
        c = {par: r[0] for par, r in self.res.items()}
        cortos = [par for par, m in c.items() if conduce(m) and m[1] < 0.12]
        si = [par for par, m in c.items() if conduce(m)]
        for par in PARES:
            m = c[par]
            self._escribir(f"  +{par[0]} −{par[1]}: " + (f"conduce {m[1]:.3f} V" if conduce(m) else "no conduce"))
        if cortos:
            self._escribir(f"EN CORTO entre las patas {cortos[0][0]} y {cortos[0][1]}.", "mal")
            return
        if len(si) == 2:
            (a1, b1), (a2, b2) = si
            if a1 == a2 or b1 == b2:
                npn = a1 == a2
                base = a1 if npn else b1
                otras = [b1, b2] if npn else [a1, a2]
                vf = {otras[0]: c[si[0]][1], otras[1]: c[si[1]][1]}
                emisor = max(otras, key=lambda x: vf[x])     # la union B-E cae un poco mas que B-C
                colector = [x for x in otras if x != emisor][0]
                self.bjt = dict(npn=npn, b=base, e=emisor, c=colector)
                self._escribir(f"BUENO ✓  {'NPN' if npn else 'PNP'}", "ok")
                self._escribir(f"Base = pata {base} · Emisor = pata {emisor} · Colector = pata {colector}")
                self._escribir(f"Vbe = {vf[emisor]:.3f} V · Vbc = {vf[colector]:.3f} V")
                if abs(vf[emisor] - vf[colector]) < 0.008:
                    self._escribir("E y C caen casi igual: confirma E/C con la hoja de datos.", "aviso")
                self.btn_hfe.pack(anchor="w", pady=(8, 0))
                return
        if len(si) == 1:
            a, b = si[0]
            self._escribir(f"Solo conduce +{a} −{b}.", "aviso")
            self._escribir("Puede ser un MOSFET (diodo interno) o un BJT con una unión abierta.")
        elif not si:
            self._escribir("ABIERTO: no conduce entre ningún par de patas.", "mal")
        else:
            self._escribir("Conducción anormal entre varias patas: dañado, Darlington "
                           "o con resistencias/diodo internos.", "mal")

    def medir_hfe(self):
        if not self.bjt or self.p._detener_si_corre(self.btn_medir):
            return
        try:
            rb = float(self.e_rb.get().replace(",", ".")) * 1000
        except ValueError:
            return messagebox.showwarning("Probador", "Escribe la resistencia de base en kΩ")
        b = self.bjt
        if b["npn"]:
            msg = (f"Pon una resistencia de {rb / 1000:g} kΩ entre la pata {b['b']} (base) y la pata {b['c']} (colector).\n"
                   f"+ a la pata {b['c']} (colector), − a la pata {b['e']} (emisor).")
        else:
            msg = (f"Pon una resistencia de {rb / 1000:g} kΩ entre la pata {b['b']} (base) y la pata {b['c']} (colector).\n"
                   f"+ a la pata {b['e']} (emisor), − a la pata {b['c']} (colector).")
        if not messagebox.askokcancel("Ganancia hFE", msg + "\n\n¿Listo para medir?"):
            return
        self.p._lanzar(lambda: self._trabajo(3.0, [0.05], lambda r: self._hfe_listo(r, rb), suave=True),
                       self.btn_medir, "Midiendo…")

    def _hfe_listo(self, res, rb):
        if not res:
            return
        il, v, i, modo = res[0]
        ib = max(1e-7, (v - 0.65) / rb)
        beta = i / ib - 1
        if v < 0.7:
            self._escribir("hFE: no se pudo medir (voltaje muy bajo: ¿transistor en corto?).", "mal")
        elif i >= 0.95 * il:
            self._escribir(f"hFE > {beta:.0f} (llegó al límite de 50 mA). Usa una R de base mayor.", "aviso")
        else:
            self._escribir(f"hFE ≈ {beta:.0f}  (Ib = {ib * 1e6:.0f} µA, Ic ≈ {(i - ib) * 1000:.1f} mA)", "ok")
            if i < 0.003:
                self._escribir("Corriente muy baja para la resolución de la fuente: usa una R de base menor.", "aviso")

    def _an_mosfet(self):
        ds, di, vth = self.res["ds"][0], self.res["diodo"][0], self.res["vth"]
        ok = True
        if conduce(ds):
            ok = False
            self._escribir(f"D-S CONDUCE ({ds[1]:.3f} V a 10 mA): en corto, o la compuerta estaba cargada. "
                           "Descarga G-S y repite el paso 1.", "mal")
        else:
            self._escribir("D-S no conduce con la compuerta descargada ✓")
        if conduce(di) and di[1] > 0.12:
            self._escribir(f"Diodo interno: Vf = {di[1]:.3f} V ✓")
        else:
            ok = False
            self._escribir("Diodo interno: " + ("en corto" if conduce(di) else "no conduce (abierto o es de canal P)"),
                           "mal")
        if conduce(vth[0]) and vth[0][1] > 0.3:
            self._escribir(f"Umbral Vgs(th) ≈ {vth[0][1]:.2f} V (a 2 mA)")
            for il, v, i, modo in vth[1:]:
                self._escribir(f"  Vgs a {il * 1000:g} mA = {v:.2f} V")
        else:
            ok = False
            self._escribir(f"No enciende con la compuerta a {vth[0][1]:.1f} V: compuerta dañada, "
                           "o el canal es el contrario.", "mal")
        self.txt.insert("1.0", ("BUENO ✓\n" if ok else "DAÑADO o mal conectado\n"), "ok" if ok else "mal")
        self._escribir("Al terminar, descarga G-S otra vez.")

    # -------------------------------------------------------- capacitor --
    def _medir_capacitor(self):
        try:
            vn = float(self.e_vcap.get().replace(",", "."))
        except ValueError:
            return messagebox.showwarning("Probador", "Escribe el voltaje del capacitor")
        vt = min(vn * 0.9, self._vmax())
        if vt <= 0:
            return
        self.lbl_med.config(text=f"Cargando a {vt:.1f} V con 20 mA máximo, durante 20 s…")
        corrientes = [0.02] * 15
        self.p._lanzar(lambda: self._trabajo(vt, corrientes, lambda r: self._cap_listo(r, vt), espera=0.6),
                       self.btn_medir, "Midiendo…")

    def _cap_listo(self, res, vt):
        self.k = len(self.pasos)
        self._mostrar_paso()
        self.txt.delete("1.0", "end")
        if not res:
            return
        il, v, i, modo = res[-1]
        if v < 0.3 and i >= 0.9 * il:
            self._escribir("EN CORTO: no sube el voltaje.", "mal")
        elif i > 0.002:
            self._escribir(f"FUGA ALTA: sigue pasando {i * 1000:.1f} mA a {v:.1f} V.", "mal")
        elif v < vt * 0.95:
            self._escribir(f"No llegó al voltaje de prueba ({v:.1f} de {vt:.1f} V): fuga alta.", "mal")
        else:
            self._escribir(f"Sin fuga detectable a {v:.1f} V ✓", "ok")
            self._escribir("La fuente no detecta fugas menores a ~1–2 mA.")
            vn, _ = self._v_prueba_cap()
            if vn and vt < vn * 0.85:
                self._escribir(f"OJO: solo se probó a {vt:.1f} V de sus {vn:g} V ({vt / vn * 100:.0f} %). "
                               "Descarta cortos y fugas grandes, pero no garantiza que aguante su voltaje "
                               "completo.", "aviso")
        self._escribir("  " + " · ".join(f"{x[2] * 1000:.0f}" for x in res) + "  mA")
        self._escribir("¡DESCARGA el capacitor con una resistencia antes de tocarlo!", "aviso")


# ----------------------------------------------------------------------------
E24 = [1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0, 3.3, 3.6, 3.9, 4.3, 4.7, 5.1,
       5.6, 6.2, 6.8, 7.5, 8.2, 9.1]


def r_comercial(r):
    """Siguiente valor comercial E24 igual o mayor que r."""
    if r <= 0:
        return 0.0
    dec = 10 ** int(f"{r:e}".split("e")[1])
    for base in E24 + [10.0]:
        if base * dec >= r * 0.999:
            return round(base * dec, 6)
    return 10 * dec


def fmt_ohm(r):
    return f"{r / 1000:g} kΩ" if r >= 1000 else f"{r:g} Ω"


class ProbadorLED:
    """LED encendido en vivo: ajustas la corriente y ves su Vf y la resistencia que necesita."""

    def __init__(self, panel, nb):
        self.p = panel
        self.activo = False
        self.i_pedida = 0.010
        self.i_enviada = None
        self.vf = None
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="LEDs")

        izq = ttk.LabelFrame(f, text="Probar LED", padding=8)
        izq.pack(side="left", fill="y")
        ttk.Label(izq, text="+ al ánodo (pata larga), − al cátodo (pata corta, lado plano).",
                  foreground=GRIS).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(izq, text="Corriente (mA)").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.v_ma = tk.DoubleVar(value=10.0)
        self.esc = ttk.Scale(izq, from_=1, to=30, variable=self.v_ma, length=260, command=self._cambio)
        self.esc.grid(row=1, column=1, sticky="w", pady=(8, 0))
        self.lbl_ma = ttk.Label(izq, text="10 mA", width=7, font=("Consolas", 11, "bold"))
        self.lbl_ma.grid(row=1, column=2, sticky="w", pady=(8, 0))
        rap = ttk.Frame(izq)
        rap.grid(row=2, column=1, columnspan=2, sticky="w")
        for ma in (2, 5, 10, 20, 30):
            ttk.Button(rap, text=f"{ma}", width=4, command=lambda m=ma: self._poner_ma(m)).pack(side="left", padx=1)
        ttk.Label(izq, text="Tope de voltaje (V)").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.e_vmax = ttk.Entry(izq, width=6)
        self.e_vmax.insert(0, "4")
        self.e_vmax.grid(row=3, column=1, sticky="w", pady=(8, 0))
        self.btn = ttk.Button(izq, text="Encender LED", style="Grande.TButton", command=self.alternar)
        self.btn.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ttk.Label(izq, foreground=GRIS, justify="left", text=(
            "El voltaje sube solo, poco a poco, hasta que el LED conduce, y se queda\n"
            "apenas encima de su Vf. El tope es solo el límite de esa subida:\n"
            "4 V para un LED suelto; para varios en serie, la suma de sus Vf + 1 V.")) \
            .grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(izq, foreground=ROJO, font=("Segoe UI", 10, "bold"), text=(
            "Conecta el LED ANTES de encender. Si lo quitas, la salida se apaga sola.")) \
            .grid(row=6, column=0, columnspan=3, sticky="w", pady=(6, 0))
        panel.controles += [self.btn]

        mid = ttk.Frame(f)
        mid.pack(side="left", fill="y", padx=(14, 0))
        tk.Label(mid, text="Voltaje del LED (Vf)", font=("Segoe UI", 10), fg=GRIS).pack(anchor="w")
        self.lbl_vf = tk.Label(mid, text="-.--- V", font=("Consolas", 34, "bold"), fg="#1f6fd1")
        self.lbl_vf.pack(anchor="w")
        self.lbl_tipo = tk.Label(mid, text="", font=("Segoe UI", 12, "bold"), fg=GRIS)
        self.lbl_tipo.pack(anchor="w")
        self.lbl_det = tk.Label(mid, text="", font=("Segoe UI", 10), fg="#333333", justify="left")
        self.lbl_det.pack(anchor="w", pady=(4, 0))

        der = ttk.LabelFrame(f, text="Resistencia en serie", padding=8)
        der.pack(side="left", fill="y", padx=(14, 0))
        ttk.Label(der, text="Lo vas a alimentar con (V):").grid(row=0, column=0, sticky="w")
        self.e_vs = ttk.Entry(der, width=6)
        self.e_vs.insert(0, "5")
        self.e_vs.grid(row=0, column=1, padx=4)
        self.e_vs.bind("<KeyRelease>", lambda e: self._calc())
        rap = ttk.Frame(der)
        rap.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        for vs in (3.3, 5, 9, 12, 24):
            ttk.Button(rap, text=f"{vs:g}", width=4, command=lambda v=vs: self._poner_vs(v)).pack(side="left", padx=1)
        self.lbl_r = tk.Label(der, text="", font=("Consolas", 13, "bold"), justify="left", fg="#6a1b9a")
        self.lbl_r.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._calc()
        self._refrescar()

    def _poner_vs(self, v):
        self.p._poner(self.e_vs, f"{v:g}")
        self._calc()

    def _poner_ma(self, ma):
        self.v_ma.set(ma)
        self._cambio()

    def _cambio(self, *_):
        ma = round(self.v_ma.get())
        self.lbl_ma.config(text=f"{ma} mA")
        self.i_pedida = ma / 1000
        self._calc()

    def alternar(self):
        if self.p._detener_si_corre(self.btn) or not self.p.dev:
            return
        try:
            vmax = float(self.e_vmax.get().replace(",", "."))
            vlim, _ = self.p.dev.limites()
            if not 1.5 <= vmax <= vlim:
                raise ValueError
        except ValueError:
            return messagebox.showwarning("LEDs", "Tope de voltaje no válido")
        self.lbl_tipo.config(text="Subiendo el voltaje poco a poco…", fg=GRIS)
        self.p._lanzar(lambda: self._trabajo(vmax), self.btn, "Apagar LED")

    MARGEN = 0.25   # voltios por encima del Vf: lo justo para que la fuente regule en CC

    def _trabajo(self, vmax):
        dev, parar = self.p.dev, self.p.parar_trabajo
        e = dev.lectura()
        previo = (e.get("vset"), e.get("iset"))
        aviso = None
        try:
            self.i_enviada = self.i_pedida
            r = encender_suave(dev, self.i_enviada, vmax, parar, v_ini=1.2)
            if r is None:
                return
            if not conduce((self.i_enviada, r.get("vout", 0.0), r.get("iout", 0.0), r.get("modo", ""))):
                aviso = (f"No conduce ni a {vmax:g} V: LED invertido, abierto o dañado "
                         "(o son varios en serie: sube el tope).")
                return
            vset = min(vmax, r.get("vout", 0.0) + self.MARGEN)
            dev.fijar_voltaje(vset)
            self.activo = True
            marca = dev.marca()
            while not parar.is_set():
                time.sleep(0.05)
                if abs(self.i_pedida - self.i_enviada) > 1e-6:
                    subir = self.i_pedida > self.i_enviada
                    self.i_enviada = self.i_pedida
                    dev.fijar_corriente(self.i_enviada)
                    if subir:       # a mas corriente el Vf sube un poco: dar el margen justo
                        r = subir_hasta_cc(dev, self.i_enviada, vmax, parar, vset)
                        if r is None:
                            break
                        vset = min(vmax, r.get("vout", 0.0) + self.MARGEN)
                        dev.fijar_voltaje(vset)
                    marca = dev.marca()
                    continue
                if dev.marca() == marca:
                    continue
                marca = dev.marca()
                r = dev.lectura()
                if not r.get("salida"):
                    break
                v, i = r.get("vout", 0.0), r.get("iout", 0.0)
                if i < 0.3 * self.i_enviada:
                    aviso = "Se desconectó el LED: salida apagada por seguridad. Conecta el siguiente y vuelve a encender."
                    break
                if i >= 0.85 * self.i_enviada and vset - v > self.MARGEN + 0.15:
                    vset = min(vmax, v + self.MARGEN)      # mantener el margen pequeno
                    dev.fijar_voltaje(vset)
        except Exception as ex:
            aviso = str(ex)
        finally:
            self.activo = False
            try:
                dev.salida(False)
                dev.fijar_voltaje(0.0)
            except Exception:
                pass
            if aviso:
                self.p.ui_q.put(("llamar", lambda: self._avisar(aviso)))
            try:
                if previo[1] is not None:
                    dev.fijar_corriente(previo[1])
                    dev.fijar_voltaje(previo[0])
            except Exception:
                pass
            self.p.ui_q.put(("fin_trabajo", self.btn))

    def _avisar(self, texto):
        self.lbl_tipo.config(text=texto, fg=ROJO, wraplength=int(300 * self.p.root.winfo_fpixels("1i") / 96))
        self.lbl_vf.config(text="-.--- V")
        self.vf = None
        self._calc()

    def _refrescar(self):
        """Actualiza las lecturas en pantalla mientras el LED esta encendido."""
        try:
            if self.activo and self.p.dev and self.p.dev.antiguedad("vout") < 2:
                e = self.p.dev.lectura()
                v, i, modo = e.get("vout", 0.0), e.get("iout", 0.0), e.get("modo", "")
                if i >= 0.6 * self.i_pedida:
                    tipo, color = tipo_por_vf(v)
                    self.vf = v
                    self.lbl_vf.config(text=f"{v:.3f} V")
                    self.lbl_tipo.config(text=tipo, fg=color)
                else:
                    self.vf = None
                    self.lbl_vf.config(text="-.--- V")
                    self.lbl_tipo.config(text="No conduce: invertido, abierto o Vf mayor que el máximo", fg=ROJO)
                self.lbl_det.config(text=f"{i * 1000:.1f} mA · {v * i * 1000:.0f} mW · {modo}")
                self._calc()
        finally:
            self.p.root.after(300, self._refrescar)

    def _calc(self):
        if self.vf is None:
            self.lbl_r.config(text="Enciende el LED\npara calcular")
            return
        try:
            vs = float(self.e_vs.get().replace(",", "."))
        except ValueError:
            return
        i = self.i_pedida
        if vs <= self.vf:
            self.lbl_r.config(text=f"Con {vs:g} V no alcanza\n(el LED necesita {self.vf:.2f} V)")
            return
        r = (vs - self.vf) / i
        rc = r_comercial(r)
        pr = (vs - self.vf) ** 2 / rc
        pot = "1/4 W" if pr < 0.125 else "1/2 W" if pr < 0.25 else "1 W" if pr < 0.5 else f"{pr * 2:.1f} W"
        self.lbl_r.config(text=f"Exacta: {fmt_ohm(round(r, 1))}\nComercial: {fmt_ohm(rc)}  ({pot})\n"
                               f"Corriente real: {(vs - self.vf) / rc * 1000:.1f} mA")

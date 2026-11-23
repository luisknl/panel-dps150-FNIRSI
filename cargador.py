"""Pestaña Cargador: carga de baterías con corte automático, capacidad medida e historial por batería.

  - Li-ion/LiPo, LiFePO4 y plomo-ácido: CC/CV (corriente fija y luego voltaje fijo). Termina cuando, en CV,
    la corriente se queda bajo el corte 10 s seguidos.
  - NiMH/NiCd: carga lenta a 0.1C, termina al llegar al 150 % de la capacidad nominal (o por tiempo).
  - Li-ion y LiFePO4 muy descargadas: precarga suave a 0.1C hasta que la celda se recupera.
  - Paros de emergencia: protección de la fuente, voltaje por encima del objetivo, salida apagada desde fuera,
    sin lecturas o USB perdido, no pasa corriente, la corriente cae de golpe (batería suelta o su BMS cortó),
    entra mucha más carga de la que cabe, tiempo máximo.
  - Respaldo en la fuente: programa OVP y OCP justo encima de lo pedido y al terminar los deja como estaban.
  - mAh y Wh medidos por la app (sumando cada lectura), curva en CSV e historial por batería (baterias.json).

Idea del cargador tomada de pazyuk/dps-150 (GPL-3.0); este código es propio, no copia el suyo.
"""
import csv
import json
import os
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

import diagnostico as D

GRIS, ROJO, VERDE, NARANJA, AZUL = "#777777", "#c62828", "#1a8f3a", "#e07b00", "#1565c0"
ARCHIVO_BATERIAS = os.path.join(D.CARPETA, "baterias.json")

# perfiles: (nombre, voltaje por celda). c_* en múltiplos de la capacidad (C).
QUIMICAS = {
    "Li-ion / LiPo": dict(
        sim="liion", celdas=(1, 6), nominal=3.7, modo="cccv",
        perfiles=[("Carga completa · 4.20 V/celda", 4.20), ("Carga suave · 4.10 V/celda (más vida)", 4.10),
                  ("Almacenamiento · 3.80 V/celda", 3.80)],
        c_carga=0.5, c_max=1.0, c_corte=0.05, v_peligro=2.5, v_precarga=3.0, v_vacia=3.3,
        nota="Sin balanceo: para packs de 2 celdas o más, cada celda debe estar pareja (±0.05 V)."),
    "LiFePO4": dict(
        sim="lifepo4", celdas=(1, 8), nominal=3.2, modo="cccv",
        perfiles=[("Carga completa · 3.60 V/celda", 3.60), ("Carga suave · 3.45 V/celda", 3.45)],
        c_carga=0.5, c_max=1.0, c_corte=0.05, v_peligro=2.0, v_precarga=2.5, v_vacia=3.0,
        nota="Sin balanceo: para packs de 2 celdas o más, cada celda debe estar pareja."),
    "Plomo-ácido (AGM / inundada)": dict(
        sim="plomo", celdas=(3, 6), nominal=2.0, modo="cccv", celdas_fijas=(3, 6),
        perfiles=[("Carga de ciclo · 2.40 V/celda (14.4 V en 12 V)", 2.40),
                  ("Flotación · 2.275 V/celda (13.65 V en 12 V)", 2.275)],
        c_carga=0.1, c_max=0.25, c_corte=0.02, v_peligro=1.75, v_vacia=1.95,
        nota="3 celdas = batería de 6 V, 6 celdas = 12 V. Experimental: probado solo con el simulador."),
    "Plomo-ácido gel": dict(
        sim="plomo", celdas=(3, 6), nominal=2.0, modo="cccv", celdas_fijas=(3, 6),
        perfiles=[("Carga gel · 2.35 V/celda (14.1 V en 12 V)", 2.35),
                  ("Flotación · 2.25 V/celda (13.5 V en 12 V)", 2.25)],
        c_carga=0.1, c_max=0.2, c_corte=0.02, v_peligro=1.75, v_vacia=1.95,
        nota="El gel no aguanta más de 14.1 V. Experimental: probado solo con el simulador."),
    "NiMH / NiCd": dict(
        sim="nimh", celdas=(1, 12), nominal=1.2, modo="tiempo",
        perfiles=[("Carga lenta 0.1C · hasta 150 % de la capacidad", 1.60)],
        c_carga=0.1, c_max=0.1, c_corte=0.0, v_peligro=0.8, v_vacia=1.1,
        nota="Carga lenta segura (sin −ΔV). Pon la capacidad real de la pila. Experimental: probado solo con "
             "el simulador."),
}


def _ma(a):
    return f"{a * 1000:.0f} mA" if abs(a) < 1 else f"{a:.2f} A"


def _dur(seg):
    s = int(max(0, seg))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def valores_auto(quimica, cap_mah):
    """Corriente de carga (A), corte (A) y tiempo máximo (h) sugeridos para la capacidad."""
    q = QUIMICAS[quimica]
    cap = cap_mah / 1000.0
    i = round(min(5.0, max(0.01, q["c_carga"] * cap)), 3)
    corte = round(max(0.010, q["c_corte"] * cap), 3) if q["modo"] == "cccv" else 0.0
    if q["modo"] == "tiempo":
        horas = 1.1 * 1.5 * cap / i + 1.0      # el 150 % con margen: normalmente termina por capacidad
    else:
        horas = 1.5 * cap / i + (2.0 if "Plomo" in quimica else 1.0)
    return i, corte, round(horas, 1)


def revisar(quimica, celdas, objetivo, vbat, e):
    """Revisión antes de cargar. Devuelve (bloqueos, preguntas, precarga) — listas de textos y un bool."""
    q = QUIMICAS[quimica]
    bloqueos, preguntas, precarga = [], [], False
    vin, vmax = e.get("vin") or 0.0, e.get("v_max") or 30.0
    if e.get("proteccion") == "LVP" or vin < objetivo + 1.0 or vmax < objetivo:
        bloqueos.append(f"La fuente no tiene entrada suficiente (entrada {vin:.1f} V, máximo de salida "
                        f"{vmax:.1f} V; hace falta más de {objetivo + 1.0:.1f} V). Conecta el adaptador al "
                        "USB-C. Sin él, la fuente se alimenta de la batería y marca LVP.")
    por_celda = vbat / celdas
    if vbat < 0.2:
        preguntas.append("No veo voltaje de batería (0 V). Puede estar desconectada, con la polaridad al revés "
                         "o con su protección (BMS) cortada.\nSi sigues, la app arranca con corriente baja y se "
                         "detiene sola si no pasa corriente.")
        precarga = q["modo"] == "cccv" and "v_precarga" in q
        return bloqueos, preguntas, precarga
    if vbat > objetivo + 0.05:
        bloqueos.append(f"La batería ya tiene {vbat:.2f} V, más que el objetivo de {objetivo:.2f} V. "
                        "Revisa la química, el número de celdas y el perfil.")
    # ¿el voltaje cuadra con el número de celdas elegido?
    lo, hi = q["v_peligro"] * 0.6, max(v for _, v in q["perfiles"]) + 0.1
    if not lo <= por_celda <= hi:
        posibles = [n for n in range(1, 13) if lo <= vbat / n <= hi]
        extra = f" Por el voltaje parecen {posibles[0]} celda(s)." if posibles else ""
        if por_celda < lo:
            extra += " O la batería está casi muerta: no la cargues sin revisarla."
        precarga = "v_precarga" in q and por_celda < q["v_precarga"]
        preguntas.append(f"{vbat:.2f} V no cuadra con {celdas} celda(s) de {quimica} "
                         f"({por_celda:.2f} V por celda).{extra}")
    elif por_celda < q["v_peligro"]:
        preguntas.append(f"La batería está MUY descargada ({por_celda:.2f} V por celda, mínimo seguro "
                         f"{q['v_peligro']:.2f} V). Puede estar dañada; una celda de litio así puede calentarse "
                         "al cargarla. Lo prudente es no cargarla. Si sigues, empieza con precarga suave y "
                         "vigílala.")
        precarga = "v_precarga" in q
    elif "v_precarga" in q and por_celda < q["v_precarga"]:
        precarga = True
    if q["modo"] == "tiempo" and por_celda > 1.42:
        preguntas.append(f"La pila ya tiene {por_celda:.2f} V por celda: parece cargada. La carga por tiempo "
                         "le metería el 150 % de su capacidad otra vez.")
    return bloqueos, preguntas, precarga


class Carga:
    """Hace una carga completa con la fuente. Sin Tk: se puede probar sola con el simulador.

    cfg: quimica, celdas, objetivo (V del pack), corriente, corte (A), tmax_s, cap_mah, precarga (bool),
         respaldo (bool). escala = tiempo acelerado del simulador (1 con la fuente real).
    avisar(texto, datos) se llama ~2 veces por segundo; datos = dict con fase, v, i, mah, wh, t.
    """

    ASENTAR = 2.0          # s reales para que la salida se estabilice
    SIN_LECTURAS = 5.0     # s sin lecturas = algo anda mal
    PRECARGA_MIN = 180.0   # s de carga (tiempo de la batería) que dura como mínimo la precarga
    PRECARGA_SOSTENER = 20.0   # s reales con el voltaje estimado en reposo ya recuperado

    def __init__(self, dev, cfg, parar, avisar=lambda t, d: None, escala=1.0):
        self.dev, self.cfg, self.parar, self.avisar, self.escala = dev, cfg, parar, avisar, escala
        self.q = QUIMICAS[cfg["quimica"]]
        self.mah = self.wh = 0.0
        self.curva = []            # (t_s escalado, v, i, mah)
        self.fase = "precarga" if cfg.get("precarga") else ("CC" if self.q["modo"] == "cccv" else "carga lenta")
        self.v_ini = self.v_fin = None
        self.t_carga = 0.0

    # --- decisiones (una por lectura) -------------------------------------
    def _evaluar(self, t, v, i, e):
        """Devuelve None para seguir, o (mensaje, estado) con estado 'completa' | 'falla'."""
        c = self.cfg
        real = t - self.t0
        el = real * self.escala
        if real < self.ASENTAR:
            return None
        if e.get("proteccion"):
            return f"Saltó la protección {e['proteccion']} de la fuente: carga detenida.", "falla"
        if not e.get("salida", True) and real > self.ASENTAR + 2:
            return "Alguien apagó la salida: carga interrumpida.", "falla"
        if v > c["objetivo"] + 0.15:
            return f"Voltaje {v:.2f} V por encima del objetivo {c['objetivo']:.2f} V: paro de emergencia.", "falla"
        if el > c["tmax_s"]:
            if self.q["modo"] == "tiempo" and self.mah >= c["cap_mah"]:
                return f"Carga completa por tiempo: {self.mah:.0f} mAh en {_dur(el)}.", "completa"
            return f"Se cumplió el tiempo máximo ({_dur(c['tmax_s'])}) sin terminar: carga detenida.", "falla"
        self.pico = max(self.pico, i)
        # corriente que cae de golpe: se soltó un cable o el BMS de la batería cortó
        if i < 0.004 and self.i_prev > max(0.015, 3 * c["corte"]) and t - self.t_prev < 3:
            return ("La corriente cayó de golpe a 0: se soltó la batería o cortó su protección (BMS).",
                    "falla")
        # nunca pasó corriente
        if i < 0.005 and self.pico < 0.01:
            self.sin_i = self.sin_i or t
            if t - self.sin_i > 10:
                return "No pasa corriente: la batería no está conectada, está abierta o ya está llena.", "falla"
        else:
            self.sin_i = None
        if self.q["modo"] == "tiempo":
            if self.mah >= 1.5 * c["cap_mah"]:
                return f"Carga completa: {self.mah:.0f} mAh (150 % de la capacidad).", "completa"
            return None
        if self.mah > 1.3 * c["cap_mah"]:
            return (f"Ya entraron {self.mah:.0f} mAh, más del 130 % de la capacidad puesta "
                    f"({c['cap_mah']:.0f} mAh): capacidad mal puesta o batería con fuga. Detenida.", "falla")
        if self.fase == "precarga":
            v_ok = self.q["v_precarga"] * c["celdas"]
            # Con corriente, la fuente lee la celda MAS ALTA que en reposo (resistencia interna + cables).
            # Se estima esa resistencia con el primer salto (reposo -> con corriente) y se descuenta.
            if self.r_est is None and i > 0.005:
                if self.v_ini and self.v_ini > 0.5:
                    self.r_est = min(1.0 * c["celdas"], max(0.0, (v - self.v_ini) / i))
                else:                                   # sin voltaje inicial (BMS cortado): valor prudente
                    self.r_est = 0.3 * c["celdas"]
            self.v_est = v - i * (self.r_est or 0.0)
            if self.v_est >= v_ok and el >= self.PRECARGA_MIN:
                self.ok_pre = self.ok_pre or t
                if t - self.ok_pre >= self.PRECARGA_SOSTENER:
                    self.fase = "CC"
                    self.dev.fijar_corriente(c["corriente"])
                    self.t_prev, self.i_prev = t, 0.0     # la corriente sube: no confundir con un corte
            else:
                self.ok_pre = None
                if el > 30 * 60:
                    return ("La precarga no recuperó la celda en 30 min: batería dañada, no la cargues.",
                            "falla")
            return None
        cv = e.get("modo") == "CV" or v >= c["objetivo"] - 0.05
        if cv:
            self.fase = "CV"
        if cv and i < c["corte"] and self.pico >= 0.01:
            self.bajo = self.bajo or t
            if t - self.bajo >= 10:
                return (f"Carga completa: {self.mah:.0f} mAh en {_dur(el)} "
                        f"(la corriente bajó de {_ma(c['corte'])}).", "completa")
        else:
            self.bajo = None
        return None

    # --- ciclo completo ---------------------------------------------------
    def correr(self):
        """Devuelve (mensaje, estado). estado: 'completa' | 'falla' | 'detenida' | 'usb'."""
        dev, c = self.dev, self.cfg
        e = dev.lectura()
        previo = {k: e.get(k) for k in ("vset", "iset", "ovp", "ocp")}
        if self.v_ini is None:
            self.v_ini = e.get("vout")
        self.pico, self.i_prev, self.t_prev = 0.0, 0.0, 0.0
        self.sin_i = self.bajo = self.ok_pre = None
        self.r_est = self.v_est = None
        resultado = None
        try:
            dev.salida(False)
            if c.get("respaldo", True):
                dev.fijar_proteccion("ovp", c["objetivo"] + 0.3)
                dev.fijar_proteccion("ocp", min(5.2, c["corriente"] * 1.2 + 0.05))
            i0 = max(0.01, round(0.1 * c["cap_mah"] / 1000, 3)) if self.fase == "precarga" else c["corriente"]
            dev.fijar_corriente(min(i0, c["corriente"]))
            dev.fijar_voltaje(c["objetivo"])
            dev.salida(True)
            self.t0 = ult = time.monotonic()
            ult_aviso = ult_curva = 0.0
            while resultado is None:
                if self.parar.is_set():
                    resultado = ("Carga detenida por ti.", "detenida")
                    break
                if dev.error or not dev.conectado:
                    resultado = ("Se perdió la conexión USB: la fuente puede seguir con la salida ENCENDIDA "
                                 "(con el límite puesto). Apágala a mano y desconecta la batería.", "usb")
                    break
                if dev.antiguedad("vout") > self.SIN_LECTURAS:
                    resultado = ("La fuente dejó de mandar lecturas: carga detenida por seguridad.", "falla")
                    break
                e = dev.lectura()
                nuevas = [h for h in list(dev.historial)[-400:] if h[0] > ult]
                for tt, v, i, p in nuevas:
                    dt = (tt - ult) * self.escala if tt - ult < 3 else 0.0   # huecos largos no se suman
                    ult = tt
                    if tt > self.t0:
                        self.mah += i * dt / 3.6
                        self.wh += p * dt / 3600
                    self.v_fin = v
                    resultado = self._evaluar(tt, v, i, e)
                    self.i_prev, self.t_prev = i, tt
                    if resultado:
                        break
                    if tt - ult_curva >= 1.0:
                        ult_curva = tt
                        self.curva.append(((tt - self.t0) * self.escala, v, i, self.mah))
                ahora = time.monotonic()
                self.t_carga = (ahora - self.t0) * self.escala
                if nuevas and ahora - ult_aviso > 0.5 and resultado is None:
                    ult_aviso = ahora
                    _, v, i, _ = nuevas[-1]
                    extra = (f" · en reposo ≈ {self.v_est:.2f} V (meta {self.q['v_precarga'] * c['celdas']:.2f})"
                             if self.fase == "precarga" and self.v_est is not None else "")
                    self.avisar(f"{self.fase} · {_dur(self.t_carga)} · {self.mah:.0f} mAh{extra}",
                                dict(fase=self.fase, v=v, i=i, mah=self.mah, wh=self.wh, t=self.t_carga))
                time.sleep(0.2)
        except Exception as ex:
            resultado = (f"Carga detenida por un error: {ex}", "falla")
        finally:
            if not dev.error:
                try:
                    dev.salida(False)
                except Exception:
                    pass
                try:
                    if c.get("respaldo", True):
                        for k in ("ovp", "ocp"):
                            if previo[k] is not None:
                                dev.fijar_proteccion(k, previo[k])
                    if previo["iset"] is not None:
                        dev.fijar_corriente(previo["iset"])
                        dev.fijar_voltaje(previo["vset"])
                except Exception:
                    pass
        return resultado


# --------------------------------------------------------------------------
def _leer_baterias():
    try:
        with open(ARCHIVO_BATERIAS, encoding="utf-8") as fh:
            d = json.load(fh)
            return {"baterias": d.get("baterias", {}), "cargas": d.get("cargas", [])}
    except (OSError, ValueError):
        return {"baterias": {}, "cargas": []}


def _guardar_baterias(d):
    with open(ARCHIVO_BATERIAS, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=1)


def _mantener_despierta(si):
    """Evita que Windows suspenda la PC mientras carga."""
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if si else 0))
    except Exception:
        pass


class Cargador:
    def __init__(self, panel, nb):
        self.p = panel
        esc = panel.root.winfo_fpixels("1i") / 96.0
        self.curva, self.carga = [], None
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="Cargador")

        izq = ttk.Frame(f)
        izq.pack(side="left", fill="y")
        fila = 0

        def etiqueta(t):
            nonlocal fila
            ttk.Label(izq, text=t).grid(row=fila, column=0, sticky="w", pady=2)

        etiqueta("Batería (nombre)")
        self.cb_bat = ttk.Combobox(izq, width=22, postcommand=lambda: self.cb_bat.configure(
            values=sorted(_leer_baterias()["baterias"])))
        self.cb_bat.grid(row=fila, column=1, sticky="w", padx=6)
        self.cb_bat.bind("<<ComboboxSelected>>", lambda e: self._elegir_bateria())
        fila += 1
        etiqueta("Química")
        self.cb_q = ttk.Combobox(izq, width=22, state="readonly", values=list(QUIMICAS))
        self.cb_q.current(0)
        self.cb_q.grid(row=fila, column=1, sticky="w", padx=6)
        self.cb_q.bind("<<ComboboxSelected>>", lambda e: self._cambio_quimica())
        fila += 1
        etiqueta("Perfil")
        self.cb_perfil = ttk.Combobox(izq, width=34, state="readonly")
        self.cb_perfil.grid(row=fila, column=1, sticky="w", padx=6)
        self.cb_perfil.bind("<<ComboboxSelected>>", lambda e: self._resumen())
        fila += 1
        etiqueta("Celdas en serie")
        self.sp_celdas = ttk.Spinbox(izq, from_=1, to=6, width=5, command=self._resumen)
        self.sp_celdas.set(1)
        self.sp_celdas.grid(row=fila, column=1, sticky="w", padx=6)
        self.sp_celdas.bind("<KeyRelease>", lambda e: self._resumen())
        fila += 1
        self.e = {}
        for k, t, v in (("cap", "Capacidad nominal (mAh)", "2000"), ("i", "Corriente de carga (A)", ""),
                        ("corte", "Corte al final (mA)", ""), ("horas", "Tiempo máximo (h)", "")):
            etiqueta(t)
            en = ttk.Entry(izq, width=9, font=("Consolas", 11))
            en.insert(0, v)
            en.grid(row=fila, column=1, sticky="w", padx=6)
            en.bind("<KeyRelease>", lambda ev: self._resumen())
            self.e[k] = en
            fila += 1
        self.e["cap"].bind("<FocusOut>", lambda ev: self._auto())
        self.e["cap"].bind("<Return>", lambda ev: self._auto())
        ttk.Button(izq, text="Sugeridos", width=10, command=self._auto).grid(row=fila - 4, column=1, sticky="e",
                                                                             padx=6)
        self.v_respaldo = tk.BooleanVar(value=True)
        ttk.Checkbutton(izq, text="Programar OVP y OCP en la fuente como respaldo", variable=self.v_respaldo) \
            .grid(row=fila, column=0, columnspan=2, sticky="w", pady=(4, 0))
        fila += 1
        self.lbl_resumen = tk.Label(izq, text="", font=("Segoe UI", 9), fg=GRIS, justify="left", anchor="w",
                                    wraplength=int(330 * esc))
        self.lbl_resumen.grid(row=fila, column=0, columnspan=2, sticky="w", pady=(6, 0))
        fila += 1
        self.btn = ttk.Button(izq, text="Iniciar carga", style="Grande.TButton", command=self.iniciar)
        self.btn.grid(row=fila, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        panel.controles.append(self.btn)

        centro = ttk.Frame(f)
        centro.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.lbl_estado = tk.Label(centro, text="Sin carga en marcha", font=("Segoe UI", 13, "bold"), fg=GRIS,
                                   anchor="w", justify="left", wraplength=int(520 * esc))
        self.lbl_estado.pack(fill="x")
        self.lbl_datos = tk.Label(centro, text="", font=("Consolas", 12), fg="#333333", anchor="w")
        self.lbl_datos.pack(fill="x", pady=(2, 4))
        ttk.Label(centro, foreground=GRIS, justify="left", wraplength=int(520 * esc), text=(
            "⚠ No dejes una batería cargando sola. Si la app o el USB fallan, solo la OVP/OCP de la fuente "
            "la protegen. Revisa química, celdas y perfil: uno equivocado sobrecarga la batería.")) \
            .pack(side="bottom", fill="x", pady=(4, 0))
        self.graf = type(panel.graf)(centro)
        self.graf.pack(fill="both", expand=True)
        panel.graf_carga = self.graf

        der = ttk.Frame(f)
        der.pack(side="right", fill="y", padx=(10, 0), before=centro)   # el historial no se recorta
        ttk.Label(der, text="Historial de esta batería", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.tabla = ttk.Treeview(der, columns=("f", "mah", "pct", "wh", "t", "r"), show="headings", height=12)
        for col, t, w in (("f", "Fecha", 78), ("mah", "mAh", 44), ("pct", "% nom.", 48), ("wh", "Wh", 40),
                          ("t", "Tiempo", 56), ("r", "Resultado", 66)):
            self.tabla.heading(col, text=t)
            self.tabla.column(col, width=int(w * esc), anchor="e" if col != "r" else "w")
        self.tabla.pack(fill="both", expand=True)
        self.lbl_hist = tk.Label(der, text="", font=("Segoe UI", 9), fg=GRIS, justify="left", anchor="w",
                                 wraplength=int(320 * esc))
        self.lbl_hist.pack(fill="x", pady=(4, 0))

        self._cambio_quimica()
        self._pintar()

    # --- formulario -------------------------------------------------------
    def _q(self):
        return QUIMICAS[self.cb_q.get()]

    def _cambio_quimica(self):
        q = self._q()
        self.cb_perfil.configure(values=[n for n, _ in q["perfiles"]])
        self.cb_perfil.current(0)
        lo, hi = q["celdas"]
        self.sp_celdas.configure(from_=lo, to=hi, increment=3 if q.get("celdas_fijas") else 1)
        try:
            n = int(self.sp_celdas.get())
        except ValueError:
            n = lo
        if q.get("celdas_fijas"):
            n = 6 if n > 3 else 3
        self.sp_celdas.set(min(hi, max(lo, n)))
        self._auto()

    def _auto(self):
        try:
            cap = D._num(self.e["cap"].get())
        except ValueError:
            return self._resumen()
        if cap > 0:
            i, corte, horas = valores_auto(self.cb_q.get(), cap)
            for k, v in (("i", f"{i:g}"), ("corte", f"{corte * 1000:.0f}"), ("horas", f"{horas:g}")):
                self.e[k].delete(0, "end")
                self.e[k].insert(0, v)
        self._resumen()

    def _leer(self):
        """Lee el formulario. Devuelve cfg o lanza ValueError con el motivo."""
        q, quimica = self._q(), self.cb_q.get()
        celdas = int(self.sp_celdas.get())
        lo, hi = q["celdas"]
        if not lo <= celdas <= hi or (q.get("celdas_fijas") and celdas not in q["celdas_fijas"]):
            raise ValueError(f"{quimica}: celdas válidas {q.get('celdas_fijas') or f'{lo} a {hi}'}")
        v_celda = q["perfiles"][max(0, self.cb_perfil.current())][1]
        cap = D._num(self.e["cap"].get())
        i = D._num(self.e["i"].get())
        corte = D._num(self.e["corte"].get() or "0") / 1000
        horas = D._num(self.e["horas"].get())
        if cap <= 0 or i <= 0 or horas <= 0:
            raise ValueError("La capacidad, la corriente y el tiempo máximo deben ser mayores que 0.")
        if i > q["c_max"] * cap / 1000 + 1e-9:
            raise ValueError(f"{_ma(i)} es demasiado para {cap:.0f} mAh de {quimica}: máximo "
                             f"{q['c_max']:g}C = {_ma(q['c_max'] * cap / 1000)}.")
        if q["modo"] == "cccv" and not 0.005 <= corte < i:
            raise ValueError("El corte debe ser de al menos 5 mA y menor que la corriente de carga.")
        return dict(quimica=quimica, celdas=celdas, objetivo=round(v_celda * celdas, 3), corriente=i,
                    corte=corte, tmax_s=horas * 3600, cap_mah=cap, respaldo=self.v_respaldo.get(),
                    perfil=self.cb_perfil.get())

    def _resumen(self):
        q = self._q()
        try:
            c = self._leer()
        except ValueError as ex:
            self.lbl_resumen.config(text=str(ex), fg=ROJO)
            return
        if q["modo"] == "tiempo":
            txt = (f"Corriente fija {_ma(c['corriente'])} con tope de {c['objetivo']:.2f} V; termina al meter "
                   f"{1.5 * c['cap_mah']:.0f} mAh (150 %) o a las {c['tmax_s'] / 3600:g} h.")
        else:
            txt = (f"Carga a {_ma(c['corriente'])} hasta {c['objetivo']:.2f} V, luego mantiene el voltaje y "
                   f"termina cuando la corriente baja de {_ma(c['corte'])}. Tope {c['tmax_s'] / 3600:g} h.")
        if c["respaldo"]:
            txt += (f"\nRespaldo en la fuente: OVP {c['objetivo'] + 0.3:.2f} V · "
                    f"OCP {min(5.2, c['corriente'] * 1.2 + 0.05):.2f} A.")
        self.lbl_resumen.config(text=txt + "\n" + q["nota"], fg=getattr(self.p, "fg_texto", "#333333"))

    def _elegir_bateria(self):
        b = _leer_baterias()["baterias"].get(self.cb_bat.get().strip())
        if b:
            if b.get("quimica") in QUIMICAS:
                self.cb_q.set(b["quimica"])
                self._cambio_quimica()
            nombres = [n for n, _ in self._q()["perfiles"]]
            if b.get("perfil") in nombres:
                self.cb_perfil.current(nombres.index(b["perfil"]))
            self.sp_celdas.set(b.get("celdas", 1))
            self.e["cap"].delete(0, "end")
            self.e["cap"].insert(0, f"{b.get('cap_mah', 2000):g}")
            self._auto()
        self._historial()

    def _historial(self):
        nombre = self.cb_bat.get().strip()
        self.tabla.delete(*self.tabla.get_children())
        cargas = [c for c in _leer_baterias()["cargas"] if c.get("bateria") == nombre]
        for n, c in enumerate(reversed(cargas)):
            pct = f"{c['pct']:.0f} %" if c.get("pct") is not None else "—"
            self.tabla.insert("", "end", iid=str(n), values=(
                f"{c['fecha'][8:10]}/{c['fecha'][5:7]} {c['fecha'][11:16]}", f"{c['mah']:.0f}", pct, f"{c['wh']:.2f}", _dur(c["seg"]), c["resultado"]))
        llenas = [c for c in cargas if c.get("pct") is not None]
        if llenas:
            ult = llenas[-1]
            txt = (f"Última carga completa desde vacía: {ult['mah']:.0f} mAh = {ult['pct']:.0f} % de la nominal."
                   + ("  ✓ en buen estado" if ult["pct"] >= 80 else "  ▼ capacidad baja: batería gastada"))
        elif cargas:
            txt = "Para medir la capacidad real, carga la batería empezando VACÍA (la columna «% nom.»)."
        else:
            txt = "Escribe un nombre para guardar cada carga de esta batería y ver su capacidad con el tiempo."
        self.lbl_hist.config(text=txt)

    # --- gráfica ----------------------------------------------------------
    def _pintar(self):
        pts = self.curva[:: max(1, len(self.curva) // 1500)]
        mins = [(t / 60, v, i) for t, v, i, _ in pts]
        self.graf.poner([([(t, v) for t, v, _ in mins], AZUL, 0), ([(t, i) for t, _, i in mins], NARANJA, 1)],
                        "Tiempo (min)", ("Voltaje (V)", "Corriente (A)"), (AZUL, NARANJA))

    # --- carga ------------------------------------------------------------
    def iniciar(self):
        if self.p._detener_si_corre(self.btn) or not self.p.dev:
            return
        dev = self.p.dev
        try:
            c = self._leer()
        except ValueError as ex:
            return messagebox.showwarning("Cargador", str(ex))
        e = dev.lectura()
        if e.get("salida"):
            if not messagebox.askokcancel("Cargador", "La salida está encendida. Voy a apagarla para medir la "
                                                      "batería antes de cargar."):
                return
        simulada = dev.puerto == "SIM"
        if simulada:      # en el simulador, conectar una batería falsa medio descargada
            from dps150_sim import BateriaSim
            q = self._q()
            self._bat_sim = BateriaSim(q["sim"], c["celdas"], c["cap_mah"], soc=0.15,
                                       acelerar=300.0 if q["modo"] == "tiempo" else 60.0)
            self._modelo_prev = dev._ser.modelo
            dev._ser.modelo = self._bat_sim
        try:
            dev.salida(False)
            e = dev.medir_estable(1.2) or dev.lectura()
        except Exception as ex:
            return messagebox.showerror("Cargador", f"No se pudo medir la batería: {ex}")
        vbat = e.get("vout") or 0.0
        bloqueos, preguntas, precarga = revisar(c["quimica"], c["celdas"], c["objetivo"], vbat, e)
        if bloqueos:
            self._soltar_sim()
            return messagebox.showerror("Cargador", "No se puede cargar:\n\n" + "\n\n".join(bloqueos))
        for txt in preguntas:
            if not messagebox.askyesno("Cargador", txt + "\n\n¿Seguir de todos modos?", icon="warning",
                                       default="no"):
                self._soltar_sim()
                return
        c["precarga"] = precarga
        if not messagebox.askokcancel("Cargador", (
                f"Batería: {vbat:.2f} V medidos · {c['celdas']} celda(s) de {c['quimica']}\n"
                f"Objetivo: {c['objetivo']:.2f} V · {_ma(c['corriente'])}"
                + (" (empieza con precarga suave)" if precarga else "") +
                "\n\nRevisa la polaridad. Quédate cerca mientras carga.\n¿Empezar?")):
            self._soltar_sim()
            return
        nombre = self.cb_bat.get().strip()
        if nombre:
            d = _leer_baterias()
            d["baterias"][nombre] = {k: c[k] for k in ("quimica", "perfil", "celdas", "cap_mah")}
            _guardar_baterias(d)
        self.curva = []
        self.graf.vista = None
        self._pintar()
        escala = self._bat_sim.acelerar if simulada else 1.0
        self.carga = carga = Carga(dev, c, self.p.parar_trabajo, self._aviso_hilo, escala=escala)
        carga.v_ini = vbat

        def trabajo():
            _mantener_despierta(True)
            try:
                msg, estado = carga.correr()
            finally:
                _mantener_despierta(False)
            self.p.ui_q.put(("llamar", lambda: self._terminar(carga, c, nombre, msg, estado, vbat)))
            self.p.ui_q.put(("fin_trabajo", self.btn))

        self.lbl_estado.config(text=("Precarga suave…" if precarga else "Cargando…")
                               + ("  (simulador: tiempo ×%g)" % escala if simulada else ""), fg=NARANJA)
        self.p._lanzar(trabajo, self.btn, "Detener carga")

    def _soltar_sim(self):
        if getattr(self, "_bat_sim", None) is not None and self.p.dev and self.p.dev.puerto == "SIM":
            self.p.dev._ser.modelo = self._modelo_prev
        self._bat_sim = None

    def _aviso_hilo(self, texto, d):
        self.p.ui_q.put(("llamar", lambda: self._progreso(texto, d)))

    def _progreso(self, texto, d):
        if not self.carga:
            return
        self.curva = list(self.carga.curva)
        self.lbl_estado.config(text=texto, fg=NARANJA)
        self.lbl_datos.config(text=f"{d['v']:6.3f} V   {d['i'] * 1000:7.1f} mA   {d['mah']:6.0f} mAh   "
                                   f"{d['wh']:6.2f} Wh   {_dur(d['t'])}", fg=getattr(self.p, "fg_texto", "#333333"))
        self._pintar()

    def _terminar(self, carga, c, nombre, msg, estado, vbat):
        self._soltar_sim()
        self.curva = list(carga.curva)
        self._pintar()
        color = {"completa": VERDE, "detenida": GRIS}.get(estado, ROJO)
        self.lbl_estado.config(text=msg, fg=color)
        q = QUIMICAS[c["quimica"]]
        desde_vacia = vbat / c["celdas"] <= q["v_vacia"]
        pct = 100 * carga.mah / c["cap_mah"] if estado == "completa" and desde_vacia and q["modo"] == "cccv" \
            else None
        # curva en CSV
        ruta = None
        if carga.curva:
            carpeta = os.path.join(D.CARPETA, "registros")
            os.makedirs(carpeta, exist_ok=True)
            seguro = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (nombre or "bateria"))
            ruta = os.path.join(carpeta, f"carga_{seguro}_{datetime.now():%Y%m%d_%H%M%S}.csv")
            with open(ruta, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["t_s", "v", "i_a", "mah"])
                for t, v, i, m in carga.curva:
                    w.writerow([f"{t:.1f}", f"{v:.4f}", f"{i:.5f}", f"{m:.1f}"])
        if nombre and carga.mah > 1:
            d = _leer_baterias()
            d["cargas"].append(dict(
                bateria=nombre, fecha=datetime.now().isoformat(timespec="seconds"), quimica=c["quimica"],
                perfil=c["perfil"], celdas=c["celdas"], cap_mah=c["cap_mah"], v_ini=round(vbat, 3),
                v_fin=round(carga.v_fin or 0, 3), mah=round(carga.mah, 1), wh=round(carga.wh, 3),
                seg=round(carga.t_carga), resultado=estado, pct=None if pct is None else round(pct, 1),
                csv=os.path.basename(ruta) if ruta else None, simulada=self.p.dev is not None
                and self.p.dev.puerto == "SIM"))
            _guardar_baterias(d)
            self._historial()
        self.lbl_datos.config(text=f"Entraron {carga.mah:.0f} mAh · {carga.wh:.2f} Wh en {_dur(carga.t_carga)}"
                                   + (f" · {pct:.0f} % de la nominal" if pct is not None else ""),
                              fg=getattr(self.p, "fg_texto", "#333333"))
        try:
            import winsound
            for frec in ((880, 1175, 1568) if estado == "completa" else (440, 330, 440, 330)):
                winsound.Beep(frec, 160)
        except Exception:
            pass
        if estado in ("falla", "usb"):
            messagebox.showwarning("Cargador", msg)
        elif estado == "completa":
            messagebox.showinfo("Cargador", msg + (f"\n\nCurva guardada en {ruta}" if ruta else ""))


class HistorialBaterias:
    """Pestaña Baterías › Historial: todas las baterías con nombre y todas sus cargas."""

    def __init__(self, panel, nb):
        self.p = panel
        esc = panel.root.winfo_fpixels("1i") / 96.0
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="Historial")
        izq = ttk.Frame(f)
        izq.pack(side="left", fill="y")
        ttk.Label(izq, text="Baterías", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.t_bat = ttk.Treeview(izq, columns=("q", "c", "cap", "n", "salud"), show="tree headings", height=14)
        self.t_bat.heading("#0", text="Nombre")
        self.t_bat.column("#0", width=int(120 * esc))
        for col, t, w in (("q", "Química", 92), ("c", "Celdas", 46), ("cap", "mAh nom.", 62), ("n", "Cargas", 48),
                          ("salud", "Salud", 50)):
            self.t_bat.heading(col, text=t)
            self.t_bat.column(col, width=int(w * esc), anchor="e" if col != "q" else "w")
        self.t_bat.pack(fill="y", expand=True)
        self.t_bat.bind("<<TreeviewSelect>>", lambda e: self._cargas())
        fila = ttk.Frame(izq)
        fila.pack(fill="x", pady=(6, 0))
        ttk.Button(fila, text="Actualizar", command=self.refrescar).pack(side="left")
        ttk.Button(fila, text="Cargar esta batería →", command=self._ir_a_cargar).pack(side="left", padx=4)
        ttk.Button(fila, text="Borrar", command=self._borrar).pack(side="left")

        der = ttk.Frame(f)
        der.pack(side="left", fill="both", expand=True, padx=(12, 0))
        self.lbl = ttk.Label(der, text="Elige una batería", font=("Segoe UI", 10, "bold"))
        self.lbl.pack(anchor="w")
        cols = (("f", "Fecha", 110), ("vi", "V inicio", 60), ("vf", "V final", 60), ("mah", "mAh", 56),
                ("pct", "% nom.", 54), ("wh", "Wh", 50), ("t", "Tiempo", 64), ("r", "Resultado", 80),
                ("s", "", 34))
        self.t_car = ttk.Treeview(der, columns=[c for c, _, _ in cols], show="headings", height=14)
        for col, t, w in cols:
            self.t_car.heading(col, text=t)
            self.t_car.column(col, width=int(w * esc), anchor="w" if col in ("f", "r") else "e")
        self.t_car.pack(fill="both", expand=True)
        self.t_car.bind("<Double-1>", lambda e: self._abrir_csv())
        self.lbl_info = ttk.Label(der, foreground=GRIS, justify="left", wraplength=int(560 * esc), text=(
            "Doble clic: abre la curva (CSV). Salud = % de la nominal en la última carga completa desde vacía "
            "(80 % o más: bien). sim = simulador."))
        self.lbl_info.pack(anchor="w", pady=(6, 0))
        f.bind("<Map>", lambda e: self.refrescar())
        self.cargas_sel = []

    def refrescar(self):
        d = _leer_baterias()
        sel = self.t_bat.selection()
        self.t_bat.delete(*self.t_bat.get_children())
        for nombre in sorted(d["baterias"]):
            b = d["baterias"][nombre]
            cargas = [c for c in d["cargas"] if c.get("bateria") == nombre]
            llenas = [c for c in cargas if c.get("pct") is not None]
            salud = f"{llenas[-1]['pct']:.0f} %" if llenas else "—"
            self.t_bat.insert("", "end", iid=nombre, text=nombre, values=(
                b.get("quimica", "?"), b.get("celdas", "?"), f"{b.get('cap_mah', 0):g}", len(cargas), salud))
        if sel and self.t_bat.exists(sel[0]):
            self.t_bat.selection_set(sel[0])
        self._cargas()

    def _cargas(self):
        sel = self.t_bat.selection()
        self.t_car.delete(*self.t_car.get_children())
        if not sel:
            self.lbl.config(text="Elige una batería")
            return
        nombre = sel[0]
        self.cargas_sel = [c for c in _leer_baterias()["cargas"] if c.get("bateria") == nombre][::-1]
        self.lbl.config(text=f"Cargas de «{nombre}» ({len(self.cargas_sel)})")
        for n, c in enumerate(self.cargas_sel):
            fecha = f"{c['fecha'][8:10]}/{c['fecha'][5:7]}/{c['fecha'][2:4]} {c['fecha'][11:16]}"
            self.t_car.insert("", "end", iid=str(n), values=(
                fecha, f"{c.get('v_ini', 0):.2f}", f"{c.get('v_fin', 0):.2f}", f"{c['mah']:.0f}",
                f"{c['pct']:.0f} %" if c.get("pct") is not None else "—", f"{c['wh']:.2f}", _dur(c["seg"]),
                c["resultado"], "sim" if c.get("simulada") else ""))

    def _abrir_csv(self):
        sel = self.t_car.selection()
        if not sel:
            return
        c = self.cargas_sel[int(sel[0])]
        ruta = os.path.join(D.CARPETA, "registros", c.get("csv") or "")
        if c.get("csv") and os.path.exists(ruta):
            os.startfile(ruta)
        else:
            messagebox.showinfo("Historial", "Esa carga no tiene curva guardada (o se borró el archivo).")

    def _ir_a_cargar(self):
        sel = self.t_bat.selection()
        if not sel:
            return
        car = self.p.cargador
        car.cb_bat.set(sel[0])
        car._elegir_bateria()
        self.p.ir_a(car.btn)

    def _borrar(self):
        sel = self.t_bat.selection()
        if not sel:
            return
        nombre = sel[0]
        d = _leer_baterias()
        n = len([c for c in d["cargas"] if c.get("bateria") == nombre])
        if not messagebox.askyesno("Historial", f"¿Borrar «{nombre}» y sus {n} carga(s) del historial?\n\n"
                                                "No se puede deshacer (las curvas CSV no se borran).",
                                   icon="warning", default="no"):
            return
        d["baterias"].pop(nombre, None)
        d["cargas"] = [c for c in d["cargas"] if c.get("bateria") != nombre]
        _guardar_baterias(d)
        self.refrescar()

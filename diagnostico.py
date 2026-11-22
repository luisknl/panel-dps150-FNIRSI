"""Diagnostico de placas al estilo de los talleres profesionales.

  - Celular: alimentar por el conector de bateria (~4.2 V), medir la fuga ANTES de
    presionar Power y grabar la "ola" de arranque con un veredicto automatico.
  - Inyeccion: buscar un corto inyectando corriente en escalones (0.2 -> 0.5 -> 1 -> 2 A),
    sin pasar nunca del voltaje del riel, mientras se mira con camara, alcohol o colofonia.
  - Laptop: subir el voltaje despacio mirando la corriente; veredicto automatico
    (corto en la linea principal, consumo en reposo normal...) y comparacion con una placa buena.

Las curvas de placas buenas se guardan en la carpeta "referencias" para compararlas despues.
Nota: la DPS-150 manda unas 2 lecturas por segundo; los cambios de segundos se ven bien,
los picos de milisegundos no.
"""
import json
import math
import os
import re
import sys
import statistics
import time
import tkinter as tk
from tkinter import messagebox, ttk

from probador import encender_suave, opp_respaldo, subir_hasta_cc

GRIS, ROJO, VERDE, NARANJA, AZUL, AMBAR = "#777777", "#c62828", "#1a8f3a", "#e07b00", "#1f6fd1", "#b8860b"
# en el .exe, los datos del usuario van junto al ejecutable (no en la carpeta temporal)
CARPETA = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
           else os.path.dirname(os.path.abspath(__file__)))
CARPETA_REF = os.path.join(CARPETA, "referencias")


def _num(t):
    return float(str(t).strip().replace(",", "."))


def _ma(a):
    return f"{a * 1000:.0f} mA" if a < 0.9995 else f"{a:.2f} A"


def _archivo(nombre):
    return re.sub(r'[\\/:*?"<>|]+', "_", nombre.strip()) or "sin_nombre"


def guardar_ref(tipo, modelo, datos):
    os.makedirs(os.path.join(CARPETA_REF, tipo), exist_ok=True)
    with open(os.path.join(CARPETA_REF, tipo, _archivo(modelo) + ".json"), "w", encoding="utf-8") as fh:
        json.dump(datos, fh)


def leer_ref(tipo, modelo):
    try:
        with open(os.path.join(CARPETA_REF, tipo, _archivo(modelo) + ".json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def modelos_ref(tipo):
    try:
        return sorted(f[:-5] for f in os.listdir(os.path.join(CARPETA_REF, tipo)) if f.endswith(".json"))
    except OSError:
        return []


class _Base:
    """Lo comun: panel, grafica, caja de veredicto y hilo de trabajo."""

    def __init__(self, panel, nb, titulo):
        self.p = panel
        self.esc = panel.root.winfo_fpixels("1i") / 96.0
        self.f = ttk.Frame(nb, padding=8)
        nb.add(self.f, text=titulo)
        self.Grafica = type(panel.graf)

    def _veredicto(self, master):
        self.lbl_ver = tk.Label(master, text="", font=("Segoe UI", 14, "bold"), fg=GRIS, justify="left",
                                wraplength=int(330 * self.esc), anchor="w")
        self.lbl_ver.pack(anchor="w", fill="x")
        self.lbl_det = tk.Label(master, text="", font=("Segoe UI", 10), fg="#333333", justify="left",
                                wraplength=int(330 * self.esc), anchor="w")
        self.lbl_det.pack(anchor="w", fill="x", pady=(4, 0))

    def _ver(self, texto, color, detalle=""):
        self.lbl_ver.config(text=texto, fg=color)
        self.lbl_det.config(text=detalle)

    def _modelo(self, master, tipo, fila):
        ttk.Label(master, text="Modelo").grid(row=fila, column=0, sticky="w", pady=(8, 0))
        cb = ttk.Combobox(master, width=22, postcommand=lambda: cb.configure(values=modelos_ref(tipo)))
        cb.grid(row=fila, column=1, sticky="w", pady=(8, 0))
        return cb

    def _llamar(self, fn):
        self.p.ui_q.put(("llamar", fn))

    def _previo(self):
        e = self.p.dev.lectura()
        return e.get("vset"), e.get("iset"), e.get("ovp"), e.get("ocp"), e.get("opp")

    def _restaurar(self, previo):
        dev = self.p.dev
        try:
            dev.salida(False)
            v, i, ovp, ocp, opp = previo
            for nombre, val in (("ovp", ovp), ("ocp", ocp), ("opp", opp)):
                if val is not None:
                    dev.fijar_proteccion(nombre, val)
            if i is not None:
                dev.fijar_corriente(i)
                dev.fijar_voltaje(v)
        except Exception:
            pass


# ============================================================================
class Celular(_Base):
    """Diagnostico de celular alimentado por el conector de bateria."""

    def __init__(self, panel, nb):
        super().__init__(panel, nb, "Celular")
        self.ola, self.t0, self.grabando, self.encendido = [], 0.0, False, False
        izq = ttk.LabelFrame(self.f, text="Alimentación por el conector de batería", padding=8)
        izq.pack(side="left", fill="y")
        self.e = {}
        for n, (k, t, v) in enumerate((("v", "Voltaje de batería (V)", "4.20"),
                                       ("i", "Límite de corriente (A)", "1.0"),
                                       ("t", "Grabar arranque (s)", "20"))):
            ttk.Label(izq, text=t).grid(row=n, column=0, sticky="w", pady=2)
            e = ttk.Entry(izq, width=7, font=("Consolas", 11))
            e.insert(0, v)
            e.grid(row=n, column=1, sticky="w")
            self.e[k] = e
        self.cb_mod = self._modelo(izq, "celular", 3)
        self.btn_on = ttk.Button(izq, text="1 · Encender y medir fuga", style="Grande.TButton", command=self.fuga)
        self.btn_on.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 4))
        self.btn_ola = ttk.Button(izq, text="2 · Grabar arranque (presiona Power)", command=self.grabar)
        self.btn_ola.grid(row=5, column=0, columnspan=2, sticky="ew")
        fila = ttk.Frame(izq)
        fila.grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(fila, text="Guardar como ola buena", command=self.guardar_buena).pack(side="left")
        self.btn_off = ttk.Button(fila, text="Apagar", command=self.apagar)
        self.btn_off.pack(side="left", padx=4)
        ttk.Label(izq, foreground=GRIS, justify="left", wraplength=int(270 * self.esc), text=(
            "Conecta la fuente al conector de batería del celular (cable de arranque DC), "
            "respetando + y −. El voltaje nunca pasa de 4.45 V y la fuente queda con "
            "protección de sobrevoltaje.\n\n1) Mide la fuga sin tocar nada. 2) Pulsa «Grabar» "
            "y presiona Power en el celular.")).grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))
        panel.controles += [self.btn_on, self.btn_ola, self.btn_off]

        mid = ttk.Frame(self.f)
        mid.pack(side="left", fill="y", padx=(12, 0))
        tk.Label(mid, text="Corriente", font=("Segoe UI", 10), fg=GRIS).pack(anchor="w")
        self.lbl_i = tk.Label(mid, text="-- mA", font=("Consolas", 30, "bold"), fg=NARANJA)
        self.lbl_i.pack(anchor="w")
        self._veredicto(mid)
        self.graf = self.Grafica(self.f)
        self.graf.pack(side="left", fill="both", expand=True, padx=(12, 0))
        self._refrescar()

    # --- acciones ---------------------------------------------------------
    def _params(self):
        v, i, t = _num(self.e["v"].get()), _num(self.e["i"].get()), _num(self.e["t"].get())
        if not 3.0 <= v <= 4.45:
            raise ValueError("El voltaje de batería debe estar entre 3.0 y 4.45 V")
        if not 0.01 <= i <= 5.0:
            raise ValueError("El límite de corriente debe estar entre 0.01 y 5 A")
        if not 3 <= t <= 120:
            raise ValueError("Graba entre 3 y 120 s")
        return v, i, t

    def fuga(self):
        if self.p._detener_si_corre(self.btn_on) or not self.p.dev:
            return
        try:
            v, i, _ = self._params()
        except ValueError as ex:
            return messagebox.showwarning("Celular", str(ex))
        if not messagebox.askokcancel("Celular", f"Se darán {v:.2f} V con límite de {_ma(i)} al conector de batería.\n"
                                                 "Revisa la polaridad: + al positivo de la batería.\n\n¿Encender?"):
            return
        if not self.encendido:               # guardar como estaba la fuente solo la primera vez
            self.previo = self._previo()
        self._ver("Encendiendo…", GRIS)

        def trabajo():
            dev, parar = self.p.dev, self.p.parar_trabajo
            try:
                dev.salida(False)
                dev.fijar_proteccion("ovp", min(4.6, v + 0.3))
                dev.fijar_corriente(i)
                dev.fijar_voltaje(v)
                dev.salida(True)
                self.encendido = True
                dev.medir_estable(1.5, parar)          # dejar pasar la carga de los capacitores
                muestras, fin, marca = [], time.monotonic() + 4, None
                while time.monotonic() < fin and not parar.is_set():
                    if dev.marca() != marca:
                        marca = dev.marca()
                        muestras.append(dev.lectura().get("iout", 0.0))
                    time.sleep(0.05)
                if muestras:
                    self._llamar(lambda: self._veredicto_fuga(statistics.median(muestras), i))
            except Exception as ex:
                msg = str(ex)
                self._llamar(lambda: self._ver(f"Error: {msg}", ROJO))
            finally:
                self.p.ui_q.put(("fin_trabajo", self.btn_on))

        self.p._lanzar(trabajo, self.btn_on, "Midiendo fuga…")

    def _veredicto_fuga(self, ia, ilim):
        if ia >= 0.9 * ilim or ia >= 0.5:
            self._ver(f"✗ CORTO: {_ma(ia)} sin presionar Power", ROJO,
                      "Corto en la línea principal (VCC_MAIN). Usa la pestaña Inyección con el riel "
                      "«VCC_MAIN de celular» para encontrar el componente que se calienta.")
        elif ia >= 0.15:
            self._ver(f"✗ Consumo alto sin arrancar: {_ma(ia)}", ROJO,
                      "Corto parcial o algo que arranca solo. Busca con Inyección o con la cámara térmica.")
        elif ia >= 0.02:
            self._ver(f"⚠ FUGA: {_ma(ia)} sin presionar Power", NARANJA,
                      "Fuga o medio corto (20–150 mA). Busca el componente tibio con alcohol, colofonia o cámara.")
        elif ia >= 0.005:
            self._ver(f"⚠ Consumo pequeño: {_ma(ia)}", AMBAR,
                      "Puede ser normal en algunos modelos o una fuga leve. Compara con un equipo bueno.")
        else:
            self._ver("✓ Sin fuga: 0 mA antes de Power", VERDE,
                      "Normal. Ahora pulsa «2 · Grabar arranque» y presiona Power en el celular.")

    def grabar(self):
        if self.p._detener_si_corre(self.btn_ola) or not self.p.dev:
            return
        if not self.encendido or not self.p.dev.lectura().get("salida"):
            return messagebox.showinfo("Celular", "Primero pulsa «1 · Encender y medir fuga».")
        try:
            _, _, dur = self._params()
        except ValueError as ex:
            return messagebox.showwarning("Celular", str(ex))
        self.ola, self.t0, self.grabando = [], time.monotonic(), True
        self._ver("● Grabando: presiona Power en el celular", ROJO)

        def trabajo():
            dev, parar = self.p.dev, self.p.parar_trabajo
            antes = dev.rapido
            dev.rapido = True                    # ~10 lecturas por segundo durante la grabacion
            visto = time.monotonic()
            try:
                while time.monotonic() - self.t0 < dur and not parar.is_set():
                    for tt, v, i, _ in list(dev.historial)[-50:]:
                        if tt > visto:
                            self.ola.append((tt - self.t0, i))
                            visto = tt
                    if not dev.lectura().get("salida"):
                        break
                    time.sleep(0.03)
            finally:
                dev.rapido = antes
                self.grabando = False
                ilim = dev.lectura().get("iset", 1.0) or 1.0
                self._llamar(lambda: self._analizar_ola(list(self.ola), ilim))
                self.p.ui_q.put(("fin_trabajo", self.btn_ola))

        self.p._lanzar(trabajo, self.btn_ola, "Detener grabación")

    def _analizar_ola(self, ola, ilim):
        self._pintar(ola)
        if len(ola) < 4:
            return self._ver("Grabación muy corta", GRIS)
        corr = [i for _, i in ola]
        imax = max(corr)
        texto, color, det = analizar_ola(ola, ilim)
        ref = leer_ref("celular", self.cb_mod.get()) if self.cb_mod.get().strip() else None
        if ref:
            imax_b = max(i for _, i in ref["ola"]) or 1e-6
            det += (f"\n\nComparado con la ola buena de «{self.cb_mod.get()}»: pico {_ma(imax)} contra "
                    f"{_ma(imax_b)} ({imax / imax_b * 100:.0f} %).")
        self._ver(texto, color, det)

    def guardar_buena(self):
        modelo = self.cb_mod.get().strip()
        if not modelo:
            return messagebox.showinfo("Celular", "Escribe el modelo (por ejemplo «iPhone 11») antes de guardar.")
        if len(self.ola) < 4:
            return messagebox.showinfo("Celular", "Primero graba el arranque de un celular BUENO.")
        guardar_ref("celular", modelo, {"ola": self.ola, "fecha": time.strftime("%Y-%m-%d %H:%M")})
        self._pintar(self.ola)
        self.lbl_det.config(text=f"Guardada como ola buena de «{modelo}». La próxima vez se dibuja en gris para comparar.")

    def apagar(self):
        self.p.parar_trabajo.set()
        if self.p.dev and self.encendido:
            self._restaurar(self.previo)
        self.encendido = False
        self._ver("Salida apagada", GRIS)

    # --- pantalla ---------------------------------------------------------
    def _pintar(self, ola):
        series = []
        ref = leer_ref("celular", self.cb_mod.get()) if self.cb_mod.get().strip() else None
        if ref:
            series.append(([(t, i * 1000) for t, i in ref["ola"]], "#9aa0a6", 0))
        series.append(([(t, i * 1000) for t, i in ola], NARANJA, 0))
        fin = max([t for s, _, _ in series for t, _ in s] + [5])
        self.graf.poner(series, "Tiempo desde que pulsaste Grabar (s)", ("Corriente (mA)", ""),
                        (NARANJA, "black"), rango_x=(0, fin))

    def _refrescar(self):
        try:
            if self.p.dev and self.encendido:
                e = self.p.dev.lectura()
                self.lbl_i.config(text=_ma(e.get("iout", 0.0)) if e.get("salida") else "salida apagada")
                if self.grabando:
                    self._pintar(list(self.ola))
                if not e.get("salida") and not (self.p.trabajo and self.p.trabajo.is_alive()):
                    self.encendido = False
                    if e.get("proteccion"):
                        self._ver(f"La fuente cortó por {e['proteccion']}", ROJO)
        finally:
            self.p.root.after(250, self._refrescar)


def analizar_ola(ola, ilim):
    """Veredicto de la ola de arranque. Devuelve (texto, color, detalle)."""
    corr = [i for _, i in ola]
    imax = max(corr)
    if imax >= 0.95 * ilim:
        return (f"✗ Llega al límite de corriente ({_ma(ilim)})", ROJO,
                "Consume todo lo que la fuente le deja: corto que aparece al arrancar, o el límite es muy bajo. "
                "Prueba con más límite; si sigue igual, busca el corto con Inyección.")
    if imax < 0.005:
        return ("✗ No reacciona al botón Power", ROJO,
                "La corriente no se movió. Revisa el botón o su flex, la línea de encendido y el circuito de "
                "carga/PMIC. Asegúrate de haber presionado Power mientras grababa.")
    umbral = max(0.005, 0.25 * imax)
    # vueltas: veces que la corriente sube por encima del umbral y despues cae casi a cero
    vueltas, arriba = 0, False
    for i in corr:
        if i > umbral:
            arriba = True
        elif arriba and i < 0.004:
            vueltas, arriba = vueltas + 1, False
    cola = [i for t, i in ola if t >= ola[-1][0] - 5]           # ultimos 5 s
    final = statistics.median(cola)
    variacion = (statistics.pstdev(cola) / final) if final > 0.003 else 0
    if vueltas >= 2:
        return (f"✗ BUCLE DE ARRANQUE: {vueltas} vueltas", ROJO,
                f"Sube hasta ~{_ma(imax)}, cae a 0 y vuelve a empezar. Típico de fallas de NAND, CPU, "
                "software o de un sensor que reinicia el equipo. Compara el pico con un equipo bueno.")
    if vueltas == 1 and final < 0.004:
        return ("✗ Arranca y se apaga", ROJO,
                f"Sube hasta ~{_ma(imax)} y cae a 0 una sola vez. Revisa si se apaga por protección "
                "(batería, temperatura) o por falla de arranque.")
    if final > 0.004 and variacion < 0.04:
        return (f"✗ SE QUEDA FIJA en ~{_ma(final)}", ROJO,
                "Un arranque sano nunca se queda en un número fijo. Quedarse pegado suele indicar CPU, RAM, "
                "NAND, reloj o una línea que no arranca. Anota el valor: es la pista para buscar el caso del modelo.")
    return (f"✓ Fluctúa: patrón de arranque normal (pico {_ma(imax)})", VERDE,
            "La corriente sube y baja como en un arranque sano. Si no da imagen, revisa la parte de pantalla, "
            "retroiluminación o el conector del display.")


# ============================================================================
RIELES = {
    "VCC_MAIN de celular (máx. 4.0 V)": 4.0,
    "Riel de 5 V (máx. 4.5 V)": 4.5,
    "Riel de 3.3 V (máx. 3.0 V)": 3.0,
    "Riel de 1.8 V (máx. 1.6 V)": 1.6,
    "Riel de CPU / RAM ~1 V (máx. 0.9 V)": 0.9,
    "Línea principal de laptop 19 V (máx. 3.0 V)": 3.0,
    "Personalizado": None,
}
CONSEJOS = ["Toca con el dedo o mira con la cámara térmica.",
            "Pon una gota de alcohol isopropílico: se evapora primero sobre el componente caliente.",
            "Con colofonia (humo de resina) encima, se derrite primero donde está el corto.",
            "Enfría la placa con spray congelante: el componente en corto se descongela primero."]


class Inyeccion(_Base):
    """Inyeccion escalonada para encontrar cortos (metodo de 0.2 -> 0.5 -> 1 -> 2 A)."""

    def __init__(self, panel, nb):
        super().__init__(panel, nb, "Inyección")
        self.estado = {}
        self.saltar = False
        izq = ttk.LabelFrame(self.f, text="Inyección escalonada", padding=8)
        izq.pack(side="left", fill="y")
        ttk.Label(izq, text="Riel en corto").grid(row=0, column=0, sticky="w")
        self.cb_riel = ttk.Combobox(izq, width=34, state="readonly", values=list(RIELES))
        self.cb_riel.current(0)
        self.cb_riel.grid(row=1, column=0, columnspan=2, sticky="w")
        self.cb_riel.bind("<<ComboboxSelected>>", lambda e: self._riel())
        self.e = {}
        for n, (k, t, v) in enumerate((("v", "Voltaje máximo (V)", "4.0"),
                                       ("i", "Escalones de corriente (A)", "0.2, 0.5, 1, 2"),
                                       ("t", "Segundos por escalón", "30"),
                                       ("p", "Potencia máxima (W)", "6")), start=2):
            ttk.Label(izq, text=t).grid(row=n, column=0, sticky="w", pady=2)
            e = ttk.Entry(izq, width=14 if k == "i" else 7, font=("Consolas", 10))
            e.insert(0, v)
            e.grid(row=n, column=1, sticky="w")
            self.e[k] = e
        self.btn = ttk.Button(izq, text="Iniciar inyección", style="Grande.TButton", command=self.iniciar)
        self.btn.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(10, 4))
        fila = ttk.Frame(izq)
        fila.grid(row=7, column=0, columnspan=2, sticky="w")
        self.btn_sig = ttk.Button(fila, text="Siguiente escalón ▶", command=self._siguiente)
        self.btn_sig.pack(side="left")
        self.btn_ok = ttk.Button(fila, text="¡Lo encontré! (apagar)", command=self._encontrado)
        self.btn_ok.pack(side="left", padx=4)
        lat = ttk.LabelFrame(izq, text="Latido", padding=6)
        lat.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(lat, text="Periodo (s)").grid(row=0, column=0, sticky="w")
        self.cb_periodo = ttk.Combobox(lat, width=5, state="readonly", values=["1", "1.5", "2", "3", "4"])
        self.cb_periodo.set("2")
        self.cb_periodo.grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(lat, text="Forma").grid(row=0, column=2, sticky="w", padx=(8, 0))
        self.cb_forma = ttk.Combobox(lat, width=10, state="readonly", values=["Cuadrada", "Corazón"])
        self.cb_forma.set("Cuadrada")
        self.cb_forma.grid(row=0, column=3, sticky="w", padx=4)
        ttk.Label(lat, text="Duración máx. (s)").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.e_tlat = ttk.Entry(lat, width=6)
        self.e_tlat.insert(0, "120")
        self.e_tlat.grid(row=1, column=1, sticky="w", padx=4, pady=(4, 0))
        self.btn_lat = ttk.Button(lat, text="♥ Latir en este escalón", command=self._latir)
        self.btn_lat.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        ttk.Label(izq, foreground=GRIS, justify="left", wraplength=int(300 * self.esc), text=(
            "+ al riel en corto, − a GND, placa sin otra alimentación. En cada escalón el voltaje sube "
            "despacio hasta que pasa esa corriente, sin pasar nunca del voltaje máximo del riel. "
            "Si nada se calienta, pasa al siguiente escalón. Cuando algo brille, pulsa «Latir»: "
            "la corriente pulsa y el componente parpadea en la cámara.")).grid(row=9, column=0, columnspan=2,
                                                                                 sticky="w", pady=(8, 0))
        self.latir = False
        panel.controles += [self.btn, self.btn_sig, self.btn_ok, self.btn_lat]

        mid = ttk.Frame(self.f)
        mid.pack(side="left", fill="both", expand=True, padx=(12, 0))
        self.lbl_esc = tk.Label(mid, text="", font=("Segoe UI", 12, "bold"), fg=GRIS, anchor="w")
        self.lbl_esc.pack(anchor="w")
        self.lbl_med = tk.Label(mid, text="", font=("Consolas", 22, "bold"), fg=NARANJA, anchor="w")
        self.lbl_med.pack(anchor="w", pady=(4, 0))
        self.pb = ttk.Progressbar(mid, maximum=100, length=int(320 * self.esc))
        self.pb.pack(anchor="w", pady=(6, 6))
        self._veredicto(mid)
        self._refrescar()

    def _riel(self):
        v = RIELES[self.cb_riel.get()]
        if v is not None:
            self.p._poner(self.e["v"], f"{v:g}")

    def _siguiente(self):
        self.saltar = True

    def _latir(self):
        """Activa o detiene el latido en el escalon actual (solo durante la inyeccion)."""
        if not (self.p.trabajo and self.p.trabajo.is_alive() and self.estado):
            return messagebox.showinfo("Latido", "Primero inicia la inyección. Cuando veas brillar algo, "
                                                 "pulsa «Latir en este escalón».")
        if self.latir:
            self.latir = False
            return
        try:
            self.lat_cfg = (float(self.cb_periodo.get()), self.cb_forma.get(), _num(self.e_tlat.get()))
            if not 5 <= self.lat_cfg[2] <= 1800:
                raise ValueError
        except ValueError:
            return messagebox.showwarning("Latido", "Duración máxima entre 5 y 1800 s")
        self.latir = True
        self.btn_lat.config(text="■ Detener latido")

    def _pulsar(self, dev, parar, alto, pmax):
        """Hilo: alterna el limite de corriente entre alto y bajo con el ritmo elegido."""
        periodo, forma, tmax = self.lat_cfg
        bajo = max(0.005, 0.05 * alto)
        fases = ([(True, 0.5), (False, 0.5)] if forma == "Cuadrada"
                 else [(True, 0.15), (False, 0.10), (True, 0.15), (False, 0.60)])   # pum-pum ... pausa
        inicio = time.monotonic()
        fin, ciclos = inicio + tmax, 0
        while time.monotonic() < fin and not parar.is_set() and self.latir:
            t_fase = inicio + ciclos * periodo          # reloj fijo: los envios no acumulan retraso
            for es_alto, frac in fases:
                dev.fijar_corriente(alto if es_alto else bajo, refrescar=False)
                t_fase += frac * periodo
                while time.monotonic() < t_fase:
                    if parar.is_set() or not self.latir or time.monotonic() >= fin:
                        break
                    time.sleep(0.01)
            ciclos += 1
            e = dev.lectura()
            if not e.get("salida"):
                raise RuntimeError(f"La fuente cortó la salida ({e.get('proteccion') or 'protección'})")
            pw = e.get("vout", 0.0) * e.get("iout", 0.0)
            if pw > pmax * 1.05 and alto > 0.02:               # respetar la potencia tambien al latir
                alto = alto * math.sqrt(pmax / pw) * 0.97
                bajo = max(0.005, 0.05 * alto)
            falta = max(0, fin - time.monotonic())
            self.estado["msg"] = (f"♥ latiendo ({forma.lower()}, {periodo:g} s): alto {_ma(alto)} / bajo {_ma(bajo)}"
                                  f" · {ciclos} latidos · quedan {falta:.0f} s")
        dev.fijar_corriente(alto)
        return ciclos

    def _encontrado(self):
        if self.p.trabajo and self.p.trabajo.is_alive() and self.estado:
            self.estado["encontrado"] = True
            self.p.parar_trabajo.set()

    def iniciar(self):
        if self.p._detener_si_corre(self.btn) or not self.p.dev:
            return
        try:
            vmax = _num(self.e["v"].get())
            pasos = [_num(x) for x in self.e["i"].get().replace(";", ",").split(",") if x.strip()]
            tpaso, pmax = _num(self.e["t"].get()), _num(self.e["p"].get())
            _, imax = self.p.dev.limites()
            if not 0.3 <= vmax <= 5.0:
                raise ValueError("El voltaje máximo debe estar entre 0.3 y 5 V")
            if not pasos or any(not 0.01 <= x <= imax for x in pasos):
                raise ValueError(f"Escalones entre 0.01 y {imax:.1f} A, separados por comas")
            if not (3 <= tpaso <= 600 and 0.5 <= pmax <= 30):
                raise ValueError("Segundos por escalón entre 3 y 600; potencia entre 0.5 y 30 W")
        except ValueError as ex:
            return messagebox.showwarning("Inyección", str(ex))
        if not messagebox.askokcancel("Inyección", f"Se inyectará como máximo {vmax:g} V, en escalones de "
                                      f"{', '.join(_ma(x) for x in pasos)}, sin pasar de {pmax:g} W.\n\n"
                                      "• + al riel en corto, − a GND.\n• Sin batería ni cargador conectados.\n\n¿Empezar?"):
            return
        previo = self._previo()
        self.estado = {"k": 0, "n": len(pasos), "il": pasos[0], "fin": 0, "tpaso": tpaso, "msg": ""}
        self._ver("Inyectando…", GRIS)

        def trabajo():
            dev, parar = self.p.dev, self.p.parar_trabajo
            motivo, color, det = "Inyección detenida", GRIS, ""
            try:
                dev.fijar_proteccion("ovp", min(30.0, vmax + 0.5))
                # protecciones dentro de la fuente: cortan aunque la app se cierre o se suelte el USB
                dev.fijar_proteccion("ocp", min(imax, max(pasos) * 1.25 + 0.05))
                dev.fijar_proteccion("opp", opp_respaldo(pmax))
                vset = 0.0
                for k, il in enumerate(pasos):
                    self.estado.update(k=k, il=il, fin=0, msg="subiendo el voltaje…")
                    if k == 0:
                        r = encender_suave(dev, il, vmax, parar, v_ini=min(0.3, vmax), p_max=pmax)
                    else:
                        dev.fijar_corriente(il)
                        r = subir_hasta_cc(dev, il, vmax, parar, vset, p_max=pmax)
                    if r is None:
                        break
                    vset = r.get("vset", vmax) or vmax
                    if r.get("iout", 0.0) < 0.5 * il:
                        self.estado["msg"] = (f"a {vmax:g} V solo pasan {_ma(r.get('iout', 0))}: "
                                              f"resistencia > {vmax / max(il, 1e-3):.1f} Ω")
                    else:
                        self.estado["msg"] = CONSEJOS[k % len(CONSEJOS)]
                    self.saltar = False
                    self.estado["fin"] = time.monotonic() + tpaso
                    i_act, visto = il, r.get("iout", 0.0) >= 0.85 * il
                    while time.monotonic() < self.estado["fin"] and not parar.is_set() and not self.saltar:
                        if self.latir:
                            ciclos = self._pulsar(dev, parar, i_act, pmax)
                            self.latir = False
                            if self.estado.get("encontrado"):
                                motivo, color = "✓ Encontrado: salida apagada", VERDE
                                det = "Cambia o levanta el componente y vuelve a medir la fuga o la resistencia del riel."
                            elif not parar.is_set():
                                motivo, color = f"Latido terminado ({ciclos} latidos)", GRIS
                                det = "Si viste parpadear un componente, ese es el corto. Pulsa Iniciar para repetir."
                            return
                        time.sleep(0.25)
                        e = dev.lectura()
                        if not e.get("salida"):
                            raise RuntimeError(f"La fuente cortó la salida ({e.get('proteccion') or 'protección'})")
                        v, i = e.get("vout", 0.0), e.get("iout", 0.0)
                        if i >= 0.85 * i_act:
                            visto = True
                        elif visto and i < 0.4 * i_act:     # en CC el voltaje solo sube al programado
                            motivo, color = "⚠ La corriente cayó de golpe", NARANJA
                            det = ("Algo se abrió o se quemó: el corto ya no está, o se fue el componente "
                                   "en corto. Revisa la zona y mide de nuevo.")
                            return
                        if v * i > pmax * 1.05 and i_act > 0.02:
                            i_act = i_act * math.sqrt(pmax / (v * i)) * 0.97
                            dev.fijar_corriente(i_act)
                            self.estado["msg"] = f"potencia limitada a {pmax:g} W: corriente bajada a {_ma(i_act)}"
                if self.estado.get("encontrado"):
                    motivo, color = "✓ Encontrado: salida apagada", VERDE
                    det = "Cambia o levanta el componente y vuelve a medir la fuga o la resistencia del riel."
                elif not parar.is_set():
                    motivo, color = "Terminaron los escalones", GRIS
                    det = ("Si nada se calentó, el corto puede estar debajo de un chip (BGA) o necesitar más corriente. "
                           "Prueba la cámara térmica o agrega un escalón mayor.")
            except Exception as ex:
                motivo, color, det = f"Detenida: {ex}", ROJO, ""
            finally:
                self._restaurar(previo)
                self.estado = {}
                self.latir = False
                m, c, d = motivo, color, det
                self._llamar(lambda: (self._ver(m, c, d), self.btn_lat.config(text="♥ Latir en este escalón")))
                self.p.ui_q.put(("fin_trabajo", self.btn))

        self.p._lanzar(trabajo, self.btn, "Detener inyección")

    def _refrescar(self):
        try:
            st = self.estado
            if st and self.p.dev:
                e = self.p.dev.lectura()
                v, i = e.get("vout", 0.0), e.get("iout", 0.0)
                self.lbl_esc.config(text=f"Escalón {st['k'] + 1} de {st['n']}: {_ma(st['il'])} · {st['msg']}",
                                    fg=self.p.fg_texto)
                r = f"{v / i:.3f} Ω" if i > 0.002 else "—"
                self.lbl_med.config(text=f"{v:.3f} V · {_ma(i)} · {v * i:.2f} W · R {r}")
                if st.get("fin"):
                    falta = max(0.0, st["fin"] - time.monotonic())
                    self.pb["value"] = 100 * (1 - falta / st["tpaso"])
            elif not (self.p.trabajo and self.p.trabajo.is_alive()):
                self.pb["value"] = 0
        finally:
            self.p.root.after(300, self._refrescar)


# ============================================================================
class Laptop(_Base):
    """Subir el voltaje despacio mirando la corriente, con veredicto y comparacion."""

    def __init__(self, panel, nb):
        super().__init__(panel, nb, "Laptop")
        self.curva = []
        izq = ttk.LabelFrame(self.f, text="Curva de la línea principal", padding=8)
        izq.pack(side="left", fill="y")
        self.e = {}
        for n, (k, t, v) in enumerate((("v", "Voltaje final (V)", "19"), ("i", "Límite de corriente (A)", "0.5"),
                                       ("paso", "Paso (V)", "0.5")), start=0):
            ttk.Label(izq, text=t).grid(row=n, column=0, sticky="w", pady=2)
            e = ttk.Entry(izq, width=7, font=("Consolas", 11))
            e.insert(0, v)
            e.grid(row=n, column=1, sticky="w")
            self.e[k] = e
        self.cb_mod = self._modelo(izq, "laptop", 3)
        self.btn = ttk.Button(izq, text="Medir curva", style="Grande.TButton", command=self.iniciar)
        self.btn.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 4))
        ttk.Button(izq, text="Guardar como curva buena", command=self.guardar_buena) \
            .grid(row=5, column=0, columnspan=2, sticky="ew")
        self.lbl_lim = ttk.Label(izq, foreground=GRIS, justify="left", wraplength=int(270 * self.esc))
        self.lbl_lim.grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(izq, foreground=GRIS, justify="left", wraplength=int(270 * self.esc), text=(
            "+ a la entrada de la placa (después del conector de carga) o a la línea principal, − a GND. "
            "Sin batería. El voltaje sube por pasos; si la corriente llega al límite, se detiene.")) \
            .grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))
        panel.controles += [self.btn]
        mid = ttk.Frame(self.f)
        mid.pack(side="left", fill="y", padx=(12, 0))
        self._veredicto(mid)
        self.graf = self.Grafica(self.f)
        self.graf.pack(side="left", fill="both", expand=True, padx=(12, 0))
        self.p.root.after(1500, self._limite)

    def _limite(self):
        if self.p.dev:
            vmax, _ = self.p.dev.limites()
            self.lbl_lim.config(text=f"Tu fuente llega hasta {vmax:.1f} V con el adaptador actual "
                                     f"(para 19–20 V necesitas un adaptador de más de 20 V).")
        self.p.root.after(5000, self._limite)

    def iniciar(self):
        if self.p._detener_si_corre(self.btn) or not self.p.dev:
            return
        try:
            vfin, ilim, paso = _num(self.e["v"].get()), _num(self.e["i"].get()), _num(self.e["paso"].get())
            vmax, imax = self.p.dev.limites()
            if not 1 <= vfin <= vmax:
                raise ValueError(f"Voltaje final entre 1 y {vmax:.1f} V (lo que permite tu adaptador)")
            if not 0.01 <= ilim <= imax or not 0.05 <= paso <= 2:
                raise ValueError("Revisa el límite de corriente y el paso")
        except ValueError as ex:
            return messagebox.showwarning("Laptop", str(ex))
        if not messagebox.askokcancel("Laptop", f"El voltaje subirá de 0 a {vfin:g} V en pasos de {paso:g} V, "
                                                f"con límite de {_ma(ilim)}.\nSin batería conectada.\n\n¿Empezar?"):
            return
        previo = self._previo()
        self.curva = []
        self._ver("Midiendo…", GRIS)

        def trabajo():
            dev, parar = self.p.dev, self.p.parar_trabajo
            self.corte = None
            try:
                dev.salida(False)
                dev.fijar_proteccion("ovp", min(30.0, vfin + 1.0))
                dev.fijar_proteccion("ocp", min(imax, ilim * 1.5 + 0.05))
                dev.fijar_corriente(ilim)
                dev.fijar_voltaje(0.0)
                dev.salida(True)
                n = int(round(vfin / paso))
                for k in range(1, n + 1):
                    if parar.is_set():
                        break
                    vs = min(vfin, k * paso)
                    dev.fijar_voltaje(vs)
                    e = dev.medir_estable(0.25, parar)
                    if e is None:
                        break
                    if not e.get("salida"):
                        self.corte = e.get("proteccion") or "protección"
                        break
                    self.curva.append((vs, e.get("vout", 0.0), e.get("iout", 0.0)))
                    self._llamar(self._pintar)
                    if e.get("iout", 0.0) >= 0.9 * ilim:
                        break
            except Exception as ex:
                msg = str(ex)
                self._llamar(lambda: self._ver(f"Error: {msg}", ROJO))
            finally:
                self._restaurar(previo)
                self._llamar(lambda: self._analizar(ilim))
                self.p.ui_q.put(("fin_trabajo", self.btn))

        self.p._lanzar(trabajo, self.btn, "Detener")

    def _pintar(self):
        series = []
        ref = leer_ref("laptop", self.cb_mod.get()) if self.cb_mod.get().strip() else None
        if ref:
            series.append(([(v, i * 1000) for _, v, i in ref["curva"]], "#9aa0a6", 0))
        series.append(([(v, i * 1000) for _, v, i in self.curva], NARANJA, 0))
        self.graf.poner(series, "Voltaje (V)", ("Corriente (mA)", ""), (NARANJA, "black"))

    def _analizar(self, ilim):
        self._pintar()
        if getattr(self, "corte", None):
            return self._ver(f"✗ La fuente cortó la salida ({self.corte})", ROJO,
                             "Saltó una protección de la fuente. Si fue OCP, la placa pidió más corriente de golpe: "
                             "probable corto.")
        llego_al_limite = bool(self.curva) and self.curva[-1][2] >= 0.9 * ilim
        if len(self.curva) < 3 and not llego_al_limite:
            return self._ver("Curva muy corta", GRIS)
        texto, color, det = analizar_curva(self.curva, ilim)
        ref = leer_ref("laptop", self.cb_mod.get()) if self.cb_mod.get().strip() else None
        if ref:
            ib = {round(vs, 2): i for vs, _, i in ref["curva"]}
            difs = [(vs, i, ib[round(vs, 2)]) for vs, _, i in self.curva if round(vs, 2) in ib]
            if difs:
                vs, i, b = max(difs, key=lambda x: abs(x[1] - x[2]))
                det += (f"\n\nContra la curva buena de «{self.cb_mod.get()}»: la mayor diferencia es a {vs:g} V "
                        f"({_ma(i)} contra {_ma(b)}).")
        self._ver(texto, color, det)

    def guardar_buena(self):
        modelo = self.cb_mod.get().strip()
        if not modelo or len(self.curva) < 3:
            return messagebox.showinfo("Laptop", "Escribe el modelo y mide primero la curva de una placa BUENA.")
        guardar_ref("laptop", modelo, {"curva": self.curva, "fecha": time.strftime("%Y-%m-%d %H:%M")})
        self._pintar()
        self.lbl_det.config(text=f"Guardada como curva buena de «{modelo}».")


def analizar_curva(curva, ilim):
    """curva = [(v_programado, v_medido, i)]. Devuelve (texto, color, detalle)."""
    vfin, vmed, ifin = curva[-1]
    if ifin >= 0.9 * ilim:
        r = vmed / ifin if ifin > 0 else 0
        return (f"✗ CORTO en la línea principal (llegó a {_ma(ilim)} a {vmed:.2f} V)", ROJO,
                f"Resistencia aproximada {r:.2f} Ω. La corriente se dispara al subir el voltaje. "
                "Busca el componente con la pestaña Inyección (riel «Línea principal de laptop»).")
    bajos = [(v, i) for _, v, i in curva if v <= 2.0 and i > 0.002]
    if bajos and max(i for _, i in bajos) > 0.03:
        v, i = max(bajos, key=lambda x: x[1])
        return (f"⚠ Consume desde voltaje muy bajo ({_ma(i)} a {v:.1f} V)", NARANJA,
                f"≈ {v / i:.1f} Ω: corto parcial o fuga en la entrada. A ese voltaje nada debería arrancar.")
    inicio = next(((v, i) for _, v, i in curva if i > 0.005), None)
    if ifin < 0.005:
        return (f"Sin consumo hasta {vmed:.1f} V", AMBAR,
                "No hay corto, pero la placa no toma corriente: revisa el conector de carga, el fusible de entrada, "
                "los MOSFET de entrada o el circuito de carga.")
    det = f"Empieza a consumir desde ~{inicio[0]:.1f} V (arrancan los reguladores)." if inicio else ""
    return (f"✓ Sin corto en la línea principal · {_ma(ifin)} a {vmed:.1f} V", VERDE,
            det + " Un consumo pequeño y estable es el de reposo (standby). Compara con una placa buena del mismo modelo.")

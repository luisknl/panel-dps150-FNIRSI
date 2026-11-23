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
import threading
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
def activar_bateria(dev, parar, v=4.20, ilim=0.3, pulso=3.0, intentos=3, avisar=lambda t: None):
    """Despierta una bateria de celular nueva cuya proteccion (PCM/BMS) viene cortada (0 V en el conector).

    Da pulsos de `v` voltios con el limite `ilim` y mide antes y despues con la salida apagada
    (la DPS-150 muestra el voltaje de lo que tiene conectado con la salida apagada).
    Se usa 4.20 V tambien para las de alto voltaje (4.35/4.40 V): basta para despertarlas y no las sobrecarga.
    Devuelve (texto, color, detalle). No restaura la fuente: eso lo hace quien llama.
    """
    def medir_apagada(espera):
        dev.salida(False)
        r = dev.medir_estable(espera, parar)
        return (r or dev.lectura()).get("vout", 0.0) or 0.0

    v0 = medir_apagada(1.0)
    if v0 >= 2.5:
        estado = (f"La batería ya marca {v0:.2f} V: está activa, no hace falta activarla.", VERDE,
                  "Si el celular igual no enciende, la falla no es de la batería dormida.")
        if v0 > v + 0.1:
            estado = (f"⚠ La batería marca {v0:.2f} V", NARANJA,
                      "Más que un pulso de 4.20 V: probablemente es de alto voltaje (4.35/4.40 V) y ya está activa.")
        return estado
    dev.fijar_proteccion("ovp", min(4.6, v + 0.25))
    dev.fijar_proteccion("ocp", min(5.2, ilim + 0.15))
    dev.fijar_corriente(ilim)
    dev.fijar_voltaje(v)
    acepto = False                       # en algun pulso entro corriente
    for n in range(1, intentos + 1):
        if parar.is_set():
            return "Activación detenida", GRIS, ""
        avisar(f"Pulso {n} de {intentos}: {v:.2f} V con límite de {_ma(ilim)}…")
        dev.salida(True)
        t0, marca, muestras = time.monotonic(), None, []
        while time.monotonic() - t0 < pulso + 0.8 and not parar.is_set():
            if dev.marca() != marca:
                marca = dev.marca()
                e = dev.lectura()
                if time.monotonic() - t0 > 0.8:            # la salida tarda ~0.8 s en estabilizarse
                    muestras.append((e.get("vout", 0.0), e.get("iout", 0.0), e.get("modo")))
                    vv, ii = muestras[-1][:2]
                    if ii >= 0.9 * ilim and vv < 1.0:      # corriente al limite con voltaje casi 0: corto
                        dev.salida(False)
                        return (f"✗ CORTO: {_ma(ii)} con solo {vv:.2f} V", ROJO,
                                "La batería (o lo que tocan las puntas) está en corto. No la instales. "
                                "Revisa que no estés tocando dos pines juntos.")
                if e.get("proteccion"):
                    dev.salida(False)
                    return (f"✗ Saltó la protección {e['proteccion']} de la fuente", ROJO,
                            "La batería pidió más de lo normal o subió el voltaje. No la instales.")
            time.sleep(0.03)
        con_i = [s for s in muestras if s[1] >= 0.02]
        if muestras and len(con_i) >= max(1, len(muestras) // 2):
            acepto = True
        v1 = medir_apagada(1.5)
        avisar(f"Pulso {n}: después marca {v1:.2f} V")
        if v1 >= 2.5 and acepto:
            i_med = statistics.median(s[1] for s in con_i)
            if v1 < 3.0:
                return (f"⚠ Despertó, pero está muy baja: {v1:.2f} V", NARANJA,
                        f"Aceptó {_ma(i_med)} en el pulso {n}. Cárgala antes de instalarla (Cargador, Li-ion, "
                        "0.1C) y vigílala: una celda tan descargada puede estar dañada.")
            pct = max(0, min(100, round((v1 - 3.3) / (4.2 - 3.3) * 100)))
            return (f"✓ Batería activada: {v1:.2f} V", VERDE,
                    f"Despertó en el pulso {n} (aceptó {_ma(i_med)}). Carga aproximada {pct} %. "
                    "Ya la puedes instalar." + (" Está baja: cárgala en el celular." if v1 < 3.6 else ""))
        if v1 >= 2.5 and not acepto:
            return (f"? Marca {v1:.2f} V, pero no entró corriente", AMBAR,
                    "No se puede confirmar: o ya estaba llena, o lo que se ve es la carga que quedó en la "
                    "salida de la fuente. Mídela con el multímetro sin las puntas de la fuente.")
        time.sleep(1.0)
    if acepto:
        return ("✗ Acepta corriente pero no despierta", ROJO,
                f"Entró corriente en los pulsos, pero después sigue en {v1:.2f} V: su protección vuelve a cortar. "
                "Las celdas están demasiado descargadas o la protección está dañada. Reclámala.")
    return ("✗ No despertó", ROJO,
            f"Después de {intentos} pulsos no entró corriente y sigue en {v1:.2f} V. Revisa que tocas + y − "
            "(los pines de los extremos, no los del centro) con buen contacto. Si está bien, la batería está "
            "defectuosa: reclámala.")


class Celular(_Base):
    """Diagnostico de celular alimentado por el conector de bateria."""

    def __init__(self, panel, nb):
        super().__init__(panel, nb, "Celular")
        self.ola, self.t0, self.grabando, self.encendido = [], 0.0, False, False
        col = ttk.Frame(self.f)
        col.pack(side="left", fill="y")
        izq = ttk.LabelFrame(col, text="Alimentación por el conector de batería", padding=8)
        izq.pack(fill="x")
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
            "Fuente al conector de batería del celular (cable de arranque DC), respetando + y −. "
            "Nunca pasa de 4.45 V. 1) Mide la fuga sin tocar nada. 2) Pulsa «Grabar» y presiona Power.")) \
            .grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))
        panel.controles += [self.btn_on, self.btn_ola, self.btn_off]

        mid = ttk.Frame(self.f)
        mid.pack(side="left", fill="y", padx=(12, 0))
        ttk.Button(mid, text="¿Batería nueva en 0 V? Activarla →",
                   command=lambda: panel.ir_a(panel.activar_bat.f)).pack(side="bottom", fill="x", pady=(8, 0))

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


class ActivarBateria(_Base):
    """Pestaña Baterías › Activar batería: despierta baterías de 1 celda con la protección cortada (0 V)."""

    def __init__(self, panel, nb):
        super().__init__(panel, nb, "Activar batería")
        self.e = {}
        izq = ttk.LabelFrame(self.f, text="Batería nueva que marca 0 V", padding=10)
        izq.pack(side="left", fill="y")
        for n, (k, t, v) in enumerate((("ai", "Límite de corriente (A)", "0.3"), ("ap", "Duración del pulso (s)", "3"))):
            ttk.Label(izq, text=t).grid(row=n, column=0, sticky="w", pady=2)
            e = ttk.Entry(izq, width=7, font=("Consolas", 11))
            e.insert(0, v)
            e.grid(row=n, column=1, sticky="w", padx=6)
            self.e[k] = e
        self.btn_act = ttk.Button(izq, text="Activar batería (4.20 V)", style="Grande.TButton", command=self.activar)
        self.btn_act.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 6))
        panel.controles.append(self.btn_act)
        ttk.Label(izq, foreground=GRIS, justify="left", wraplength=int(300 * self.esc), text=(
            "Para baterías de 1 celda: celular, tablet, 18650 protegida, reloj, audífonos.\n\n"
            "1. Saca la batería del equipo.\n"
            "2. Punta roja en + y negra en − del conector flex: los pines de los EXTREMOS. No toques los del "
            "centro (datos y sensor de temperatura).\n"
            "3. Sujétalas firmes (mejor con pinzas) y pulsa el botón.\n\n"
            "Da hasta 3 pulsos de 4.20 V y mide antes y después con la salida apagada. 4.20 V sirve también "
            "para las de alto voltaje (4.35/4.40 V).\n\n"
            "Audífonos y relojes: baja el límite a 0.1 A.")).grid(row=3, column=0, columnspan=2, sticky="w")
        der = ttk.Frame(self.f)
        der.pack(side="left", fill="both", expand=True, padx=(16, 0))
        self._veredicto(der)
        self.lbl_ver.config(wraplength=int(520 * self.esc))
        self.lbl_det.config(wraplength=int(520 * self.esc))
        ttk.Label(der, text="Pasos", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(14, 2))
        self.lista = tk.Listbox(der, height=10, font=("Consolas", 10), activestyle="none")
        self.lista.pack(fill="both", expand=True)

    def _paso(self, texto):
        self.lista.insert("end", f"{time.strftime('%H:%M:%S')}  {texto}")
        self.lista.see("end")
        self._ver(texto, NARANJA)

    def activar(self):
        if self.p._detener_si_corre(self.btn_act) or not self.p.dev:
            return
        try:
            ilim, pulso = _num(self.e["ai"].get()), _num(self.e["ap"].get())
            if not 0.05 <= ilim <= 0.5:
                raise ValueError("Para activar, el límite debe estar entre 0.05 y 0.5 A (0.3 A es lo normal).")
            if not 1 <= pulso <= 10:
                raise ValueError("El pulso debe durar entre 1 y 10 s.")
        except ValueError as ex:
            return messagebox.showwarning("Activar batería", str(ex))
        dev = self.p.dev
        cel = getattr(self.p, "celular", None)
        if (cel and cel.encendido) or dev.lectura().get("salida"):
            if not messagebox.askokcancel("Activar batería", "La salida está encendida. Voy a apagarla primero."):
                return
            if cel and cel.encendido:
                cel._restaurar(cel.previo)
                cel.encendido = False
        if not messagebox.askokcancel("Activar batería", (
                f"Se darán pulsos de 4.20 V con límite de {_ma(ilim)} a la batería, {pulso:g} s cada uno.\n\n"
                "• La batería debe estar FUERA del celular.\n"
                "• Punta roja en + y negra en − del conector, firmes (mejor con pinzas).\n"
                "• No toques los pines del centro (datos y sensor de temperatura).\n\n¿Empezar?")):
            return
        previo = self._previo()
        sim = dev.puerto == "SIM"
        if sim:      # en el simulador: una bateria nueva dormida
            from dps150_sim import BateriaSim
            modelo_prev, dev._ser.modelo = dev._ser.modelo, BateriaSim("liion", 1, 3000, soc=0.55, dormida=True)
        self.lista.delete(0, "end")
        self._paso("Midiendo la batería con la salida apagada…")

        def trabajo():
            parar = self.p.parar_trabajo
            try:
                texto, color, det = activar_bateria(dev, parar, 4.20, ilim, pulso,
                                                    avisar=lambda t: self._llamar(lambda: self._paso(t)))
            except Exception as ex:
                texto, color, det = f"Error: {ex}", ROJO, ""
            finally:
                self._restaurar(previo)
                if sim and dev._ser is not None:
                    dev._ser.modelo = modelo_prev
            self._llamar(lambda: (self.lista.insert("end", f"{time.strftime('%H:%M:%S')}  → {texto}"),
                                  self._ver(texto, color, det)))
            self.p.ui_q.put(("fin_trabajo", self.btn_act))

        self.p._lanzar(trabajo, self.btn_act, "Detener")


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
        mid = ttk.Frame(self.f)
        lat = ttk.LabelFrame(mid, text="Latido: cuando algo caliente, haz que «parpadee»", padding=6)
        lat.pack(side="bottom", anchor="w", pady=(8, 0))
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
        ttk.Label(mid, foreground=GRIS, justify="left", wraplength=int(420 * self.esc), text=(
            "+ al riel en corto, − a GND, placa sin otra alimentación. En cada escalón el voltaje sube "
            "despacio hasta que pasa esa corriente, sin pasar nunca del voltaje máximo del riel. "
            "Si nada se calienta, pasa al siguiente escalón. Cuando algo brille, pulsa «Latir»: "
            "la corriente pulsa y el componente parpadea en la cámara.")).pack(side="bottom", anchor="w",
                                                                                 pady=(8, 0))
        self.latir = False
        panel.controles += [self.btn, self.btn_sig, self.btn_ok, self.btn_lat]

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
        arr = ttk.LabelFrame(mid, text="Arranque por el jack DC: presiona Power", padding=8)
        arr.pack(side="bottom", fill="x", pady=(8, 0))
        for n, (k, t, v) in enumerate((("av", "Voltaje del cargador (V)", "19"), ("ai", "Límite de corriente (A)", "3.0"),
                                       ("at", "Grabar (s)", "60"))):
            ttk.Label(arr, text=t).grid(row=n, column=0, sticky="w", pady=2)
            e = ttk.Entry(arr, width=7, font=("Consolas", 11))
            e.insert(0, v)
            e.grid(row=n, column=1, sticky="w")
            self.e[k] = e
        self.btn_arr = ttk.Button(arr, text="Encender y grabar arranque", command=self.arranque)
        self.btn_arr.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(arr, text="Guardar como arranque bueno", command=self.guardar_arranque) \
            .grid(row=4, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        panel.controles.append(self.btn_arr)
        self.ola = []
        self._veredicto(mid)
        self.lbl_det.config(font=("Segoe UI", 9))      # el detalle del arranque es largo: que quepa sobre el recuadro
        self.graf = self.Grafica(self.f)
        self.graf.pack(side="left", fill="both", expand=True, padx=(12, 0))
        self.p.root.after(1500, self._limite)

    # --- arranque por el jack DC -------------------------------------------
    def arranque(self):
        if self.p._detener_si_corre(self.btn_arr) or not self.p.dev:
            return
        dev = self.p.dev
        try:
            v, ilim, dur = _num(self.e["av"].get()), _num(self.e["ai"].get()), _num(self.e["at"].get())
            vmax, imax = dev.limites()
            if not 5 <= v <= 30:
                raise ValueError("El voltaje del cargador debe estar entre 5 y 30 V (lo normal: 19 o 20 V).")
            if v > vmax + 0.05:
                raise ValueError(f"Tu fuente solo llega a {vmax:.1f} V con el adaptador que tiene. Para {v:g} V "
                                 f"conecta al USB-C un adaptador de más de {v + 0.5:g} V (cargador USB-C de 20 V).")
            if not 0.1 <= ilim <= imax:
                raise ValueError(f"Límite entre 0.1 y {imax:.1f} A (3 A suele sobrar para arrancar).")
            if v * ilim > 150:
                raise ValueError(f"{v:g} V × {ilim:g} A pasa de los 150 W de la fuente: baja el límite.")
            if not 10 <= dur <= 180:
                raise ValueError("Graba entre 10 y 180 s (las laptops tardan de 20 a 60 s en hacer POST).")
        except ValueError as ex:
            return messagebox.showwarning("Laptop", str(ex))
        if not messagebox.askokcancel("Laptop", (
                f"Se darán {v:g} V con límite de {_ma(ilim)} por el jack DC.\n\n"
                "• Quita la batería de la laptop.\n• Revisa la polaridad: + al centro del jack en la mayoría.\n"
                "• Usa el voltaje de SU cargador (dice OUTPUT en la etiqueta).\n\n¿Encender?")):
            return
        previo = self._previo()
        self.ola = []
        sim = dev.puerto == "SIM"
        if sim:      # en el simulador: una laptop que arranca normal
            from dps150_sim import LaptopSim
            modelo_prev, dev._ser.modelo = dev._ser.modelo, LaptopSim("normal")
        self._ver("Subiendo el voltaje…", GRIS)

        def trabajo():
            parar = self.p.parar_trabajo
            antes = dev.rapido
            resultado = None
            try:
                dev.salida(False)
                dev.fijar_proteccion("ovp", min(31.0, v + 1.0))
                dev.fijar_proteccion("ocp", min(imax + 0.1, ilim + 0.3))
                dev.fijar_corriente(ilim)
                dev.fijar_voltaje(min(5.0, v))
                dev.salida(True)
                # subida por pasos: si a voltaje bajo ya llega al limite, es un corto en la entrada
                vs = 5.0
                while not parar.is_set():
                    e = dev.medir_estable(0.4, parar)
                    if e is None:
                        break
                    if not e.get("salida"):
                        resultado = (f"✗ La fuente cortó por {e.get('proteccion') or 'protección'} al subir", ROJO,
                                     "La placa pidió demasiado antes de presionar Power: corto en la entrada. "
                                     "Usa «Medir curva» con límite bajo e Inyección.")
                        break
                    if e.get("iout", 0.0) >= 0.9 * ilim:
                        resultado = (f"✗ CORTO en la entrada: llega a {_ma(ilim)} con {e.get('vout', 0):.1f} V", ROJO,
                                     "Antes de presionar Power ya consume todo el límite. Busca el corto en la línea "
                                     "principal con «Medir curva» (límite bajo) e Inyección.")
                        break
                    if vs >= v:
                        break
                    vs = min(v, vs + 3.0)
                    dev.fijar_voltaje(vs)
                if resultado is None and not parar.is_set():
                    e = dev.medir_estable(1.0, parar)
                    muestras, fin, marca = [], time.monotonic() + 3, None
                    while time.monotonic() < fin and not parar.is_set():
                        if dev.marca() != marca:
                            marca = dev.marca()
                            r = dev.lectura()
                            muestras.append(r.get("vout", 0.0) * r.get("iout", 0.0))
                        time.sleep(0.05)
                    p_rep = statistics.median(muestras) if muestras else 0.0
                    txt, col = reposo_laptop(p_rep, v)
                    self._llamar(lambda: self._ver("● PRESIONA POWER AHORA", ROJO, txt))
                    dev.rapido = True
                    t0 = visto = time.monotonic()
                    if sim:
                        modelo = dev._ser.modelo
                        threading.Timer(1.5, modelo.presionar).start()   # en el simulador, "presionar" solo
                    ult_pinta = 0.0
                    while time.monotonic() - t0 < dur and not parar.is_set():
                        for tt, vv, ii, _ in list(dev.historial)[-60:]:
                            if tt > visto:
                                self.ola.append((tt - t0, vv, ii))
                                visto = tt
                        r = dev.lectura()
                        if not r.get("salida"):
                            break
                        if time.monotonic() - ult_pinta > 0.5:
                            ult_pinta = time.monotonic()
                            seg = int(dur - (ult_pinta - t0))
                            self._llamar(lambda s=seg, rr=r: (self._pintar_ola(), self.lbl_det.config(
                                text=f"{txt}\nAhora: {_w(rr.get('vout', 0) * rr.get('iout', 0), v)} · "
                                     f"faltan {s} s")))
                        time.sleep(0.03)
                    corte = None if dev.lectura().get("salida") else (dev.lectura().get("proteccion") or "")
                    resultado = analizar_arranque_laptop(list(self.ola), p_rep, ilim)
                    if corte:
                        resultado = (f"✗ La fuente cortó por {corte} durante el arranque", ROJO,
                                     "Pidió más de lo programado de golpe: un riel en corto al encender." +
                                     resultado[2][resultado[2].find("\n\n"):])
                    elif parar.is_set():
                        resultado = ("Grabación detenida · " + resultado[0], resultado[1], resultado[2])
            except Exception as ex:
                resultado = (f"Error: {ex}", ROJO, "")
            finally:
                dev.rapido = antes
                self._restaurar(previo)
                if sim and dev._ser is not None:
                    dev._ser.modelo = modelo_prev
                if resultado is None:
                    resultado = ("Detenido", GRIS, "")
                self._llamar(lambda: (self._pintar_ola(), self._ver_arranque(resultado)))
                self.p.ui_q.put(("fin_trabajo", self.btn_arr))

        self.p._lanzar(trabajo, self.btn_arr, "Detener")

    def _ver_arranque(self, res):
        texto, color, det = res
        ref = leer_ref("laptop_arranque", self.cb_mod.get()) if self.cb_mod.get().strip() else None
        if ref and self.ola:
            pb = max(v * i for _, v, i in ref["ola"]) or 1e-6
            pm = max(v * i for _, v, i in self.ola)
            det += (f"\n\nContra el arranque bueno de «{self.cb_mod.get()}»: pico {pm:.1f} W contra {pb:.1f} W "
                    f"({pm / pb * 100:.0f} %).")
        self._ver(texto, color, det)

    def _pintar_ola(self):
        series = []
        ref = leer_ref("laptop_arranque", self.cb_mod.get()) if self.cb_mod.get().strip() else None
        if ref:
            series.append(([(t, v * i) for t, v, i in ref["ola"]], "#9aa0a6", 0))
        series.append(([(t, v * i) for t, v, i in self.ola], NARANJA, 0))
        fin = max([t for s, _, _ in series for t, _ in s] + [10])
        self.graf.poner(series, "Tiempo desde «Presiona Power» (s)", ("Potencia (W)", ""), (NARANJA, "black"),
                        rango_x=(0, fin))

    def guardar_arranque(self):
        modelo = self.cb_mod.get().strip()
        if not modelo:
            return messagebox.showinfo("Laptop", "Escribe el modelo (arriba, en «Modelo») antes de guardar.")
        if len(self.ola) < 10:
            return messagebox.showinfo("Laptop", "Primero graba el arranque de una laptop BUENA.")
        guardar_ref("laptop_arranque", modelo, {"ola": self.ola, "fecha": time.strftime("%Y-%m-%d %H:%M")})
        self._pintar_ola()
        self.lbl_det.config(text=f"Guardado como arranque bueno de «{modelo}». Se dibuja en gris para comparar.")

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


def _w(p, v):
    return f"{p:.1f} W ({_ma(p / v) if v > 0.5 else '—'})"


def reposo_laptop(p, v):
    """Veredicto del consumo antes de presionar Power. Devuelve (texto, color)."""
    if p < 0.05:
        return f"Reposo: casi 0 ({_w(p, v)})", AMBAR
    if p < 1.2:
        return f"Reposo normal: {_w(p, v)} (EC y rieles de reposo vivos)", VERDE
    if p < 6:
        return f"Reposo ALTO: {_w(p, v)}", NARANJA
    return f"Reposo MUY ALTO: {_w(p, v)}", ROJO


def analizar_arranque_laptop(ola, p_reposo, ilim):
    """ola = [(t, v, i)] desde que se pidió presionar Power. Clasifica por potencia (W) para que sirva a
    19 o 20 V. Devuelve (texto, color, detalle)."""
    if len(ola) < 4:
        return "Grabación muy corta", GRIS, ""
    v_med = statistics.median(v for _, v, _ in ola)
    ps = [(t, v * i) for t, v, i in ola]
    imax = max(i for _, _, i in ola)
    pmax = max(p for _, p in ps)
    rep_txt, _ = reposo_laptop(p_reposo, v_med)
    pie = f"\n\n{rep_txt}. Pico {_w(pmax, v_med)}."
    if imax >= 0.95 * ilim:
        return (f"✗ Llega al límite de corriente ({_ma(ilim)}) al encender", ROJO,
                "Al presionar Power se enciende un riel en corto (VCORE del CPU, GPU, RAM) o el límite es muy bajo "
                "para esta laptop. Con 3 A suele sobrar; si sigue igual, busca el corto en los rieles con "
                "Inyección y la cámara térmica." + pie)
    if pmax < p_reposo + 0.3 and pmax < 1.5:
        if p_reposo < 0.05:
            return ("✗ No reacciona y no tiene consumo de reposo", ROJO,
                    "La placa ni siquiera ve el adaptador. Revisa el conector DC, el fusible y los MOSFET de "
                    "entrada, el circuito de carga (charger IC) y los rieles siempre encendidos (3V/5V ALW) que "
                    "alimentan al EC." + pie)
        return ("✗ No reacciona al botón Power", ROJO,
                "Hay consumo de reposo (el EC vive), pero no arranca. Revisa el botón y su flex, la señal de "
                "encendido hacia el EC, y el BIOS/EC (firmware dañado: reprogramarlo)." + pie)
    # "volver al reposo" = bajar cerca del consumo de reposo y quedarse ahi al menos 0.8 s
    # (un POST sano tambien tiene bajones, pero no hasta el reposo)
    base = p_reposo + max(0.5, 0.1 * pmax)
    umbral = max(3.0, 0.4 * pmax)
    vueltas, arriba, inicios, bajo_desde = 0, False, [], None
    for t, p in ps:
        if p > umbral:
            bajo_desde = None
            if not arriba:
                arriba = True
                inicios.append(t)
        elif arriba and p < base:
            bajo_desde = t if bajo_desde is None else bajo_desde
            if t - bajo_desde >= 0.8:
                vueltas, arriba, bajo_desde = vueltas + 1, False, None
        else:
            bajo_desde = None
    reaccion = next((t for t, p in ps if p > p_reposo + 0.5), None)
    if reaccion is not None and reaccion > 8:
        pie += f" Tardó {reaccion:.0f} s en reaccionar (¿presionaste Power al momento?)."
    cola = [p for t, p in ps if t >= ps[-1][0] - 5]
    final = statistics.median(cola)
    var = statistics.pstdev(cola) / final if final > 0.1 else 0
    if vueltas >= 2:
        periodo = (inicios[-1] - inicios[0]) / (len(inicios) - 1) if len(inicios) >= 2 else 0
        return (f"✗ REINICIA EN BUCLE: {vueltas} vueltas" + (f", cada ~{periodo:.0f} s" if periodo else ""), ROJO,
                f"Sube a ~{pmax:.0f} W y vuelve al reposo una y otra vez. Típico de RAM (prueba otro módulo o "
                "limpia los contactos), BIOS dañado, o un VRM que se protege y reinicia." + pie)
    if vueltas == 1 and final < base:
        apaga = next((t for t, p in ps if inicios and t > inicios[0] and p < base), 0)
        return (f"✗ Arranca y se apaga a los ~{apaga:.0f} s", ROJO,
                f"Sube a ~{pmax:.0f} W y vuelve al reposo una sola vez. Revisa protección por temperatura "
                "(ventilador, pasta térmica), un VRM que se protege, el BIOS o el circuito de carga." + pie)
    if final < 6 and var < 0.05:
        return (f"✗ SE QUEDA EN BAJO: ~{_w(final, v_med)}", ROJO,
                "Arrancan los rieles de reposo (S3/S5) pero el CPU no recibe su voltaje. Revisa la secuencia de "
                "encendido (RSMRST#, SLP_S3#/S4#, PWROK), el VRM del CPU (VCORE) y el BIOS." + pie)
    if var < 0.03:
        return (f"✗ SE QUEDA FIJA en ~{_w(final, v_med)}", ROJO,
                "El CPU tiene energía pero no hace POST (un arranque sano fluctúa). Revisa RAM, BIOS, CPU o "
                "chipset. Anota el valor: es la pista para buscar el caso de ese modelo." + pie)
    return (f"✓ Fluctúa: arranque normal (pico {pmax:.0f} W)", VERDE,
            "La potencia sube y varía como en un POST sano. Si no da imagen, revisa la pantalla y su flex "
            "(eDP/LVDS), la retroiluminación (fusible, voltaje del backlight) o la GPU." + pie)


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

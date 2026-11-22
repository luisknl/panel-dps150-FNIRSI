"""Control de la fuente FNIRSI DPS-150 por USB (puerto serie virtual).

FNIRSI no publica el protocolo. Este modulo sigue el protocolo descifrado
por el proyecto abierto cho45/fnirsi-dps-150 (WebSerial):

  Envio (PC -> fuente):   F1 <cmd> <registro> <largo> <datos...> <checksum>
  Recibo (fuente -> PC):  F0 A1 <registro> <largo> <datos...> <checksum>
  checksum = (registro + largo + suma(datos)) & 0xFF
  Los numeros con decimales van como float32 little-endian.

Uso rapido:
    from dps150 import DPS150, buscar_puertos
    fuente = DPS150(buscar_puertos()[0][0])
    fuente.abrir()
    fuente.fijar_voltaje(5.0)
    fuente.fijar_corriente(0.2)
    fuente.salida(True)
    print(fuente.lectura())
    fuente.cerrar()
"""
import collections
import struct
import threading
import time

import serial
from serial.tools import list_ports

# --- Constantes del protocolo -------------------------------------------
HDR_IN = 0xF0
HDR_OUT = 0xF1

CMD_GET = 0xA1
CMD_BAUD = 0xB0
CMD_SET = 0xB1
CMD_SESION = 0xC1

REG_V_ENTRADA = 192   # float   voltaje de entrada (USB-C / adaptador)
REG_V_FIJADO = 193    # float   voltaje programado
REG_I_FIJADO = 194    # float   limite de corriente programado
REG_SALIDA_MED = 195  # 3 float voltaje, corriente y potencia de salida
REG_TEMP = 196        # float   temperatura interna
REG_M1_V = 197        # float   memorias: M1 V=197 I=198, M2 V=199 I=200 ... M6 V=207 I=208
REG_OVP = 209         # float   proteccion de sobrevoltaje
REG_OCP = 210         # float   proteccion de sobrecorriente
REG_OPP = 211         # float   proteccion de sobrepotencia
REG_OTP = 212         # float   proteccion de sobretemperatura
REG_LVP = 213         # float   proteccion de bajo voltaje de entrada
REG_BRILLO = 214      # byte
REG_VOLUMEN = 215     # byte
REG_MEDICION = 216    # byte    1 = enviar mediciones
REG_CAPACIDAD = 217   # float   Ah acumulados
REG_ENERGIA = 218     # float   Wh acumulados
REG_SALIDA_ON = 219   # byte    1 = salida encendida
REG_PROTECCION = 220  # byte    indice en PROTECCIONES
REG_MODO = 221        # byte    0 = CC, otro = CV
REG_MODELO = 222      # texto
REG_HW = 223          # texto
REG_FW = 224          # texto
REG_V_MAX = 226       # float   voltaje maximo posible (depende de la entrada)
REG_I_MAX = 227       # float   corriente maxima posible
REG_TODO = 255        # bloque con todo el estado

PROTECCIONES = ["", "OVP", "OCP", "OPP", "OTP", "LVP", "REP"]
REG_PROTECCION_POR_NOMBRE = {
    "ovp": REG_OVP, "ocp": REG_OCP, "opp": REG_OPP, "otp": REG_OTP, "lvp": REG_LVP,
}
BAUDIOS = [9600, 19200, 38400, 57600, 115200]


def armar_paquete(cmd, registro, datos):
    """Arma un paquete PC -> fuente. `datos` puede ser int (1 byte) o bytes."""
    if isinstance(datos, int):
        datos = bytes([datos & 0xFF])
    datos = bytes(datos)
    checksum = (registro + len(datos) + sum(datos)) & 0xFF
    return bytes([HDR_OUT, cmd, registro, len(datos)]) + datos + bytes([checksum])


class LectorTramas:
    """Separa el flujo de bytes en tramas validas (registro, datos).

    Tolera basura, tramas partidas entre lecturas y checksums malos.
    """

    def __init__(self):
        self.buf = bytearray()

    def alimentar(self, data):
        self.buf += data
        tramas = []
        while True:
            i = self.buf.find(b"\xf0\xa1")
            if i < 0:
                # conservar un 0xF0 suelto al final: puede ser el inicio de una trama
                if self.buf[-1:] == b"\xf0":
                    del self.buf[:-1]
                else:
                    self.buf.clear()
                break
            if i:
                del self.buf[:i]
            if len(self.buf) < 4:
                break
            registro, largo = self.buf[2], self.buf[3]
            if len(self.buf) < 5 + largo:
                break
            datos = bytes(self.buf[4:4 + largo])
            if (registro + largo + sum(datos)) & 0xFF != self.buf[4 + largo]:
                del self.buf[:1]   # trama falsa: saltar y volver a sincronizar
                continue
            del self.buf[:5 + largo]
            tramas.append((registro, datos))
        return tramas


def _f(p, o=0):
    return struct.unpack_from("<f", p, o)[0]


def _texto(p):
    return p.decode("ascii", errors="replace").strip("\x00 ")


def decodificar(registro, p):
    """Convierte una trama recibida en un diccionario de valores."""
    try:
        if registro == REG_V_ENTRADA:
            return {"vin": _f(p)}
        if registro == REG_SALIDA_MED:
            return {"vout": _f(p, 0), "iout": _f(p, 4), "pout": _f(p, 8)}
        if registro == REG_TEMP:
            return {"temp": _f(p)}
        if registro == REG_CAPACIDAD:
            return {"ah": _f(p)}
        if registro == REG_ENERGIA:
            return {"wh": _f(p)}
        if registro == REG_SALIDA_ON:
            return {"salida": p[0] == 1}
        if registro == REG_PROTECCION:
            return {"proteccion": PROTECCIONES[p[0]] if p[0] < len(PROTECCIONES) else f"?{p[0]}"}
        if registro == REG_MODO:
            return {"modo": "CC" if p[0] == 0 else "CV"}
        if registro == REG_MODELO:
            return {"modelo": _texto(p)}
        if registro == REG_HW:
            return {"hw": _texto(p)}
        if registro == REG_FW:
            return {"fw": _texto(p)}
        if registro == REG_V_MAX:
            return {"v_max": _f(p)}
        if registro == REG_I_MAX:
            return {"i_max": _f(p)}
        if registro == REG_TODO and len(p) >= 119:
            prot = p[108]
            d = {
                "vin": _f(p, 0), "vset": _f(p, 4), "iset": _f(p, 8),
                "vout": _f(p, 12), "iout": _f(p, 16), "pout": _f(p, 20),
                "temp": _f(p, 24),
                "ovp": _f(p, 76), "ocp": _f(p, 80), "opp": _f(p, 84),
                "otp": _f(p, 88), "lvp": _f(p, 92),
                "brillo": p[96], "volumen": p[97], "medicion": p[98] != 0,
                "ah": _f(p, 99), "wh": _f(p, 103),
                "salida": p[107] == 1,
                "proteccion": PROTECCIONES[prot] if prot < len(PROTECCIONES) else f"?{prot}",
                "modo": "CC" if p[109] == 0 else "CV",
                "v_max": _f(p, 111), "i_max": _f(p, 115),
            }
            for n in range(6):
                d[f"m{n + 1}_v"] = _f(p, 28 + 8 * n)
                d[f"m{n + 1}_i"] = _f(p, 32 + 8 * n)
            return d
    except (struct.error, IndexError):
        pass
    return {}


VID_PID_DPS150 = "2E3C:5740"   # chip Artery AT32 "Virtual Com Port" de la DPS-150


def buscar_puertos():
    """Lista de (puerto, descripcion), los mas probables primero.

    La DPS-150 se reconoce por su VID:PID. Se descartan los puertos Bluetooth
    y el de Intel AMT, que nunca son la fuente.
    """
    dps, probables, otros = [], [], []
    for p in list_ports.comports():
        desc = p.description or ""
        hwid = (p.hwid or "").upper()
        if VID_PID_DPS150 in hwid:
            dps.append((p.device, "DPS-150"))
            continue
        if "BTHENUM" in hwid or "BLUETOOTH" in desc.upper() or "SOL" in desc.upper() and "INTEL" in desc.upper():
            continue
        (probables if "USB" in hwid else otros).append((p.device, desc))
    return dps + probables + otros


def puerto_dps150():
    """Nombre del puerto de la DPS-150 si esta presente ahora, si no None."""
    for p in list_ports.comports():
        if VID_PID_DPS150 in (p.hwid or "").upper():
            return p.device
    return None


def esperar_dps150(timeout=60.0, parar=None):
    """Espera a que aparezca la DPS-150 y devuelve su puerto (o None).

    Hace falta porque la fuente se desconecta del USB a los pocos segundos
    si ningun programa abre su puerto: hay que abrirlo en cuanto aparece.
    """
    fin = time.monotonic() + timeout
    while time.monotonic() < fin:
        if parar is not None and parar.is_set():
            return None
        puerto = puerto_dps150()
        if puerto:
            return puerto
        time.sleep(0.05)
    return None


class ErrorDPS(Exception):
    pass


class DPS150:
    """Conexion con una DPS-150. Un hilo de fondo lee lo que manda la fuente."""

    def __init__(self, puerto, rtscts=False, serie=None):
        self.puerto = puerto
        self.rtscts = rtscts
        self._ser = serie            # permite inyectar un simulador
        self.estado = {}
        self.actualizado = {}        # clave -> time.monotonic() de la ultima vez
        self.error = None
        self.conectado = False
        self._lock = threading.Lock()
        self._wlock = threading.Lock()
        self._parar = threading.Event()
        self._hilo = None
        self._ultimo_pedido = 0.0
        # cada lectura que llega: (t monotonic, vout, iout, pout). La fuente manda sola ~2 por s;
        # con rapido=True se le piden ~10 por s (medido: mide unas 9 distintas por segundo).
        self.historial = collections.deque(maxlen=200000)
        self.rapido = False

    # --- conexion ---------------------------------------------------------
    def abrir(self, espera=2.0):
        if self._ser is None:
            # recien enchufada, Windows tarda un instante en dejar abrir el puerto
            fin = time.monotonic() + 1.5
            while True:
                try:
                    self._ser = serial.Serial(self.puerto, 115200, timeout=0.05,
                                              write_timeout=1, rtscts=self.rtscts)
                    break
                except serial.SerialException as e:
                    if time.monotonic() > fin:
                        raise ErrorDPS(f"No se pudo abrir {self.puerto}: {e}") from e
                    time.sleep(0.05)
        for attr in ("dtr", "rts"):
            try:
                setattr(self._ser, attr, True)
            except Exception:
                pass
        try:
            self._ser.reset_input_buffer()
        except Exception:
            pass
        self._parar.clear()
        self._hilo = threading.Thread(target=self._leer, name="DPS150-lector", daemon=True)
        self._hilo.start()

        self._enviar(CMD_SESION, 0, 1)
        self._enviar(CMD_BAUD, 0, BAUDIOS.index(115200) + 1)
        for reg in (REG_MODELO, REG_HW, REG_FW):
            self._enviar(CMD_GET, reg, 0)
        self._enviar(CMD_SET, REG_MEDICION, 1)
        self.pedir_todo()

        if not self.esperar("vout", espera):
            self.cerrar()
            raise ErrorDPS(f"{self.puerto} se abrio pero la fuente no respondio. "
                           "Revisa que este encendida y que sea el puerto correcto.")
        self.conectado = True

    def cerrar(self):
        if self._ser is not None and self.error is None:
            try:
                self._enviar(CMD_SESION, 0, 0)
            except Exception:
                pass
        self._parar.set()
        if self._hilo and self._hilo is not threading.current_thread():
            self._hilo.join(timeout=1)
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None
        self.conectado = False

    def esperar(self, clave, timeout=2.0, despues_de=None):
        """Espera a que llegue `clave` (y que sea mas nueva que `despues_de`)."""
        fin = time.monotonic() + timeout
        while time.monotonic() < fin:
            with self._lock:
                t = self.actualizado.get(clave)
            if t is not None and (despues_de is None or t > despues_de):
                return True
            if self.error:
                return False
            time.sleep(0.02)
        return False

    # --- comunicacion -----------------------------------------------------
    def _enviar(self, cmd, registro, datos):
        if self._ser is None:
            raise ErrorDPS("No hay conexion con la fuente")
        paquete = armar_paquete(cmd, registro, datos)
        with self._wlock:
            try:
                self._ser.write(paquete)
            except (serial.SerialException, OSError) as e:
                self.error = str(e)
                self.conectado = False
                raise ErrorDPS(f"Error al escribir en la fuente: {e}") from e
            time.sleep(0.05)   # la fuente necesita una pausa entre comandos

    def _leer(self):
        lector = LectorTramas()
        while not self._parar.is_set():
            try:
                n = self._ser.in_waiting
                data = self._ser.read(n or 1)
            except (serial.SerialException, OSError, AttributeError, TypeError) as e:
                if not self._parar.is_set():
                    self.error = f"Se perdio la conexion: {e}"
                    self.conectado = False
                break
            if data:
                for reg, datos in lector.alimentar(data):
                    d = decodificar(reg, datos)
                    if d:
                        ahora = time.monotonic()
                        with self._lock:
                            self.estado.update(d)
                            for k in d:
                                self.actualizado[k] = ahora
                        if "vout" in d:
                            self.historial.append((ahora, d["vout"], d.get("iout", 0.0), d.get("pout", 0.0)))
            self._mantener_lecturas()

    def _mantener_lecturas(self):
        """Si la fuente deja de mandar mediciones, se las pide."""
        ahora = time.monotonic()
        with self._lock:
            t = self.actualizado.get("vout", 0.0)
        if self.rapido:
            intervalo = 0.09                      # ~10 lecturas por segundo
        else:
            intervalo = 1.0 if ahora - t > 0.8 else 3.0
        if ahora - self._ultimo_pedido > intervalo:
            self._ultimo_pedido = ahora
            try:
                self.pedir_todo()
            except ErrorDPS:
                pass

    # --- lectura ----------------------------------------------------------
    def pedir_todo(self):
        self._enviar(CMD_GET, REG_TODO, 0)

    def lectura(self):
        """Copia del estado actual (diccionario)."""
        with self._lock:
            return dict(self.estado)

    def antiguedad(self, clave="vout"):
        """Segundos desde la ultima vez que llego `clave` (inf si nunca)."""
        with self._lock:
            t = self.actualizado.get(clave)
        return float("inf") if t is None else time.monotonic() - t

    def marca(self, clave="vout"):
        """time.monotonic() de la ultima vez que llego `clave` (None si nunca)."""
        with self._lock:
            return self.actualizado.get(clave)

    def limites(self):
        e = self.lectura()
        return (e.get("v_max") or 30.0), (e.get("i_max") or 5.1)

    # --- control ----------------------------------------------------------
    def fijar_voltaje(self, v):
        v = float(v)
        vmax, _ = self.limites()
        if not 0 <= v <= vmax + 1e-6:
            raise ValueError(f"El voltaje debe estar entre 0 y {vmax:.2f} V")
        self._enviar(CMD_SET, REG_V_FIJADO, struct.pack("<f", v))
        self.pedir_todo()

    def fijar_corriente(self, i, refrescar=True):
        """refrescar=False no pide el estado despues (mas rapido, para pulsos)."""
        i = float(i)
        _, imax = self.limites()
        if not 0 <= i <= imax + 1e-6:
            raise ValueError(f"La corriente debe estar entre 0 y {imax:.3f} A")
        self._enviar(CMD_SET, REG_I_FIJADO, struct.pack("<f", i))
        if refrescar:
            self.pedir_todo()

    def fijar_proteccion(self, nombre, valor):
        """nombre: 'ovp', 'ocp', 'opp', 'otp' o 'lvp'."""
        reg = REG_PROTECCION_POR_NOMBRE[nombre.lower()]
        self._enviar(CMD_SET, reg, struct.pack("<f", float(valor)))
        self.pedir_todo()

    def fijar_memoria(self, n, v, i):
        """Guarda voltaje y corriente en la memoria M1..M6 de la fuente."""
        if not 1 <= n <= 6:
            raise ValueError("La memoria debe ser de M1 a M6")
        v, i = float(v), float(i)
        vmax, imax = self.limites()
        if not (0 <= v <= vmax + 1e-6 and 0 <= i <= imax + 1e-6):
            raise ValueError(f"M{n}: fuera de rango (máx. {vmax:.2f} V / {imax:.3f} A)")
        reg = REG_M1_V + 2 * (n - 1)
        self._enviar(CMD_SET, reg, struct.pack("<f", v))
        self._enviar(CMD_SET, reg + 1, struct.pack("<f", i))
        self.pedir_todo()

    def leer_memorias(self):
        """[(v, i)] de M1..M6 segun la ultima lectura (None si aun no llegan)."""
        e = self.lectura()
        return [(e.get(f"m{n}_v"), e.get(f"m{n}_i")) for n in range(1, 7)]

    def salida(self, encender):
        self._enviar(CMD_SET, REG_SALIDA_ON, 1 if encender else 0)
        self.pedir_todo()

    def fijar_brillo(self, nivel):
        self._enviar(CMD_SET, REG_BRILLO, int(nivel))

    def fijar_volumen(self, nivel):
        self._enviar(CMD_SET, REG_VOLUMEN, int(nivel))

    def medir_estable(self, espera, parar=None):
        """Espera `espera` s y devuelve una lectura que llego despues de la espera."""
        fin = time.monotonic() + espera
        while time.monotonic() < fin:
            if parar is not None and parar.is_set():
                return None
            time.sleep(0.05)
        marca = time.monotonic()
        self.pedir_todo()
        self.esperar("vout", 1.5, despues_de=marca)
        return self.lectura()


if __name__ == "__main__":
    # Prueba rapida por consola: python dps150.py [COMx]
    import sys
    puertos = buscar_puertos()
    puerto = sys.argv[1] if len(sys.argv) > 1 else (puertos[0][0] if puertos else None)
    if not puerto:
        print("No se encontro ningun puerto. Conecta y enciende la DPS-150.")
        sys.exit(1)
    f = DPS150(puerto)
    f.abrir()
    e = f.lectura()
    print(f"Conectado en {puerto}: {e.get('modelo', '?')}  HW {e.get('hw', '?')}  FW {e.get('fw', '?')}")
    try:
        while True:
            e = f.lectura()
            print(f"\r{e.get('vout', 0):6.3f} V  {e.get('iout', 0):6.3f} A  {e.get('pout', 0):7.3f} W  "
                  f"{e.get('modo', '--')}  salida={'ON ' if e.get('salida') else 'OFF'}  "
                  f"{e.get('temp', 0):4.1f} C   (Ctrl+C para salir)", end="", flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
    finally:
        f.cerrar()

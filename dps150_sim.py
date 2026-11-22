"""Simulador de una DPS-150 con una carga resistiva, para probar sin el equipo.

Imita la interfaz de pyserial (write, read, in_waiting, close) y habla el
mismo protocolo que la fuente real. Uso:
    from dps150 import DPS150
    from dps150_sim import PuertoSimulado
    f = DPS150("SIM", serie=PuertoSimulado(resistencia=10))
"""
import math
import random
import struct
import threading
import time

import dps150 as P


def diodo(vf20=0.65, n=1.8):
    """Modelo de diodo: conduce 20 mA a `vf20` voltios."""
    vt = n * 0.02585
    i_s = 0.02 / (math.exp(vf20 / vt) - 1)
    return lambda v: i_s * (math.exp(min(v, 40 * vt * 3) / vt) - 1) if v > 0 else 0.0


def zener(vz, rz=5.0):
    """Zener polarizado en inversa: no conduce hasta `vz`."""
    return lambda v: max(0.0, (v - vz) / rz)


def abierto():
    return lambda v: 0.0


def suma(*modelos):
    return lambda v: sum(m(v) for m in modelos)


class PuertoSimulado:
    def __init__(self, resistencia=10.0, vin=20.0):
        self.r = resistencia
        self.modelo = None       # funcion V -> I para simular piezas (ver diodo(), zener())
        self.vin = vin
        self.vset, self.iset = 5.0, 1.0
        self.ovp, self.ocp, self.opp, self.otp, self.lvp = 31.0, 5.2, 105.0, 80.0, 4.5
        self.on = False
        self.prot = 0
        self.mem = [[3.3, 0.5], [5.0, 1.0], [9.0, 1.0], [12.0, 1.0], [15.0, 2.0], [19.0, 2.0]]
        self.sesion = False
        self.medicion = False
        self.ah = self.wh = 0.0
        self.temp = 28.0
        self._salida = bytearray()
        self._lock = threading.Lock()
        self._ultimo_envio = 0.0
        self._ultimo_calculo = time.monotonic()
        self.dtr = self.rts = False
        self.cerrado = False

    # --- modelo electrico -------------------------------------------------
    def _medir(self):
        if not self.on:
            return 0.0, 0.0, 0.0, 1
        f = self.modelo or (lambda x: x / self.r)     # carga: corriente en funcion del voltaje
        v, i, modo = self.vset, f(self.vset), 1        # CV
        if i > self.iset:                              # CC: buscar V donde f(V) = Iset
            lo, hi = 0.0, self.vset
            for _ in range(50):
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if f(mid) < self.iset else (lo, mid)
            v, i, modo = (lo + hi) / 2, self.iset, 0
        v += random.gauss(0, 0.002)
        i = max(0.0, i + random.gauss(0, 0.0005))
        return v, i, v * i, modo

    def _proteger(self, v, i, p):
        for idx, cond in ((1, v > self.ovp), (2, i > self.ocp), (3, p > self.opp)):
            if cond:
                self.on, self.prot = False, idx
                return

    def _avanzar(self):
        ahora = time.monotonic()
        dt = ahora - self._ultimo_calculo
        self._ultimo_calculo = ahora
        v, i, p, _ = self._medir()
        self._proteger(v, i, p)
        self.ah += i * dt / 3600
        self.wh += p * dt / 3600
        self.temp += ((28 + p * 0.4) - self.temp) * min(1.0, dt / 20)

    # --- respuestas -------------------------------------------------------
    def _responder(self, reg, datos):
        trama = bytes([P.HDR_IN, P.CMD_GET, reg, len(datos)]) + datos
        trama += bytes([(reg + len(datos) + sum(datos)) & 0xFF])
        self._salida += trama

    def _todo(self):
        v, i, p, modo = self._medir()
        f = lambda x: struct.pack("<f", x)
        b = bytearray()
        b += f(self.vin) + f(self.vset) + f(self.iset) + f(v) + f(i) + f(p) + f(self.temp)
        for mv, mi in self.mem:
            b += f(mv) + f(mi)
        b += f(self.ovp) + f(self.ocp) + f(self.opp) + f(self.otp) + f(self.lvp)
        b += bytes([4, 2, 1])
        b += f(self.ah) + f(self.wh)
        b += bytes([1 if self.on else 0, self.prot, modo, 0])
        b += f(self.vin - 1.0) + f(5.1)
        b += f(0) * 5
        return bytes(b)

    def _procesar(self, paquete):
        cmd, reg, datos = paquete[1], paquete[2], paquete[4:-1]
        fl = struct.unpack("<f", datos)[0] if len(datos) == 4 else None
        if cmd == P.CMD_SESION:
            self.sesion = datos[0] == 1
        elif cmd == P.CMD_GET:
            if reg == P.REG_TODO:
                self._responder(reg, self._todo())
            elif reg == P.REG_MODELO:
                self._responder(reg, b"DPS-150 (simulado)")
            elif reg == P.REG_HW:
                self._responder(reg, b"V1.0")
            elif reg == P.REG_FW:
                self._responder(reg, b"SIM")
        elif cmd == P.CMD_SET:
            if reg == P.REG_V_FIJADO:
                self.vset = fl
            elif reg == P.REG_I_FIJADO:
                self.iset = fl
            elif reg == P.REG_SALIDA_ON:
                self.on = datos[0] == 1
                if self.on:
                    self.prot = 0
            elif 197 <= reg <= 208:
                self.mem[(reg - 197) // 2][(reg - 197) % 2] = fl
            elif reg == P.REG_MEDICION:
                self.medicion = datos[0] == 1
            elif reg in (P.REG_OVP, P.REG_OCP, P.REG_OPP, P.REG_OTP, P.REG_LVP):
                setattr(self, {P.REG_OVP: "ovp", P.REG_OCP: "ocp", P.REG_OPP: "opp",
                               P.REG_OTP: "otp", P.REG_LVP: "lvp"}[reg], fl)

    # --- interfaz tipo pyserial -------------------------------------------
    def write(self, paquete):
        if self.cerrado:
            raise OSError("puerto cerrado")
        with self._lock:
            self._avanzar()
            self._procesar(bytes(paquete))
        return len(paquete)

    @property
    def in_waiting(self):
        with self._lock:
            self._empujar()
            return len(self._salida)

    def _empujar(self):
        ahora = time.monotonic()
        if self.sesion and self.medicion and ahora - self._ultimo_envio > 0.2:
            self._ultimo_envio = ahora
            self._avanzar()
            v, i, p, modo = self._medir()
            self._responder(P.REG_SALIDA_MED, struct.pack("<fff", v, i, p))
            self._responder(P.REG_MODO, bytes([modo]))
            self._responder(P.REG_SALIDA_ON, bytes([1 if self.on else 0]))
            self._responder(P.REG_PROTECCION, bytes([self.prot]))
            self._responder(P.REG_TEMP, struct.pack("<f", self.temp))

    def read(self, n=1):
        if self.cerrado:
            raise OSError("puerto cerrado")
        with self._lock:
            self._empujar()
            if not self._salida:
                pass
            else:
                datos = bytes(self._salida[:n])
                del self._salida[:n]
                return datos
        time.sleep(0.02)
        return b""

    def reset_input_buffer(self):
        with self._lock:
            self._salida.clear()

    def close(self):
        self.cerrado = True

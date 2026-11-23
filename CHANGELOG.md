# Cambios

## v1.1 — 23/11/2026

### Nuevo
- **Cargador de baterías** (Baterías › Cargador): Li-ion/LiPo (4.20 / 4.10 / 3.80 V por celda), LiFePO4 (3.60 / 3.45 V), plomo-ácido de 6 y 12 V, gel y NiMH/NiCd.
  - CC/CV con corte automático; NiMH por capacidad (150 %).
  - Corriente, corte y tiempo máximo calculados según la capacidad, con un tope seguro por química.
  - Precarga suave para litio muy descargado: estima el voltaje en reposo descontando la resistencia de la celda y los cables, y dura al menos 3 min.
  - Revisión antes de cargar: entrada USB-C suficiente, batería ya cargada, voltaje que no cuadra con las celdas, batería muy descargada.
  - OVP y OCP de respaldo en la fuente; al terminar se restauran.
  - Paros de emergencia: protección, sobrevoltaje, salida apagada, sin lecturas, USB perdido, sin corriente, corriente que cae de golpe, más del 130 % de la capacidad, tiempo máximo.
  - mAh y Wh medidos, curva en CSV, la PC no se suspende mientras carga.
- **Historial de baterías** (Baterías › Historial): salud (% de la nominal) y todas las cargas de cada batería.
- **Activar batería** (Baterías › Activar batería): despierta baterías nuevas de 1 celda que marcan 0 V. Da pulsos de 4.20 V con límite de corriente y da su veredicto: activada, muy baja, no despierta, protección dañada o corto.
- **Arranque de laptop por el jack DC** (Placas › Laptop): reposo antes de Power, grabación del arranque y veredicto en watts con dónde buscar. Guarda el arranque bueno por modelo.
- Simulador: batería (incluso «dormida») y laptop con 8 comportamientos, para probar todo sin equipo.

### Cambiado
- **Interfaz reorganizada**: barra lateral con 5 grupos (Placas, Piezas, Baterías, Monitoreo, Fuente) en vez de 13 pestañas sueltas.
  - «Buscar corto» e «Inyección» se unen en **Cortos**, con dos modos.
  - **● REC** arriba (antes, la pestaña Registro CSV).
  - Memorias **M1–M6** con su voltaje y la línea **OVP · OCP · OPP**, siempre visibles bajo Control.
  - Pestaña **Fuente › Ajustes**: datos del equipo, brillo, volumen, intervalo del CSV y atajos.
  - Recuerda el grupo y la pestaña, y marca con ● la que tiene algo en marcha.
  - Atajos: **Alt+1…5** para los grupos y **Ctrl+Tab** para las pestañas.

### Probado
- Con la fuente real (FW V1.2): carga de Li-ion 18650 de precarga a CV (4.10 V) y lectura del voltaje de la batería con la salida apagada. Falta una carga completa con corte automático.
- Solo con el simulador: plomo, NiMH, activación de baterías y arranque de laptop.

## v1.0 — 22/11/2026
Primera versión publicada.

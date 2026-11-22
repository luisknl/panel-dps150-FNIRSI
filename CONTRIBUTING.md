# Cómo colaborar

¡Gracias por querer mejorar el Panel DPS-150! Tus aportes son bienvenidos: arreglos, funciones nuevas, traducciones o casos de reparación.

## Antes de empezar

- **Prueba siempre con la fuente simulada:** `python panel_dps150.py --sim`. `dps150_sim.py` imita la fuente y permite cargas de prueba: resistencia (`resistencia=`), diodo (`diodo(vf)`), zener (`zener(vz)`) o cualquier función de voltaje a corriente (`modelo = lambda v: ...`).
- **Después, con la fuente real**, empieza con voltajes bajos, corriente limitada y piezas que no te importe dañar.
- Anota en el pull request con qué firmware lo probaste (por ejemplo, V1.2).

## Reglas de seguridad para el código

Estas reglas salieron de componentes quemados de verdad. Respétalas en cualquier función que encienda la salida:

1. **Nunca encender directo al voltaje máximo** cuando la pieza puede conducir (diodos, LEDs, rieles en corto). Usa `encender_suave()` o `subir_hasta_cc()` de `probador.py`: suben el voltaje desde abajo y se detienen cuando pasa la corriente pedida.
2. **No decidir con el modo CC/CV**, porque llega en otro registro y puede venir atrasado. Decide con la **corriente medida** (`iout`).
3. Si la prueba inyecta potencia, programa **OCP/OPP dentro de la fuente** como respaldo (`opp_respaldo()`).
4. **Al terminar, apaga la salida y devuelve la fuente a como estaba** (voltaje, corriente y protecciones), también si hubo un error. Usa `try/finally`.
5. Las cosas largas corren en un hilo (`panel._lanzar(...)`). La interfaz se toca solo desde el hilo principal: usa `panel.ui_q.put(("llamar", funcion))`.

## Estructura

| Archivo | Para qué |
|---|---|
| `dps150.py` | Protocolo: `DPS150.abrir()`, `fijar_voltaje()`, `fijar_corriente()`, `salida()`, `lectura()`, `historial`, `rapido` |
| `dps150_sim.py` | Simulador con modelos de carga |
| `panel_dps150.py` | Ventana principal, clase `Grafica` (zoom y cursor), `Medidor`, memorias, Buscar corto, secuencias y temas |
| `probador.py` | Probador de piezas, LEDs en vivo, `encender_suave()` |
| `diagnostico.py` | Celular, Inyección (con latido) y Laptop |
| `barrido.py` | Barrido I-V |
| `superposicion.py` | Lecturas flotantes |

## Ideas abiertas

- Libreta de casos de reparación (modelo, síntoma y lectura → qué era la falla).
- Probador: resistencias en mΩ, Rds(on) del MOSFET con una pila de 9 V en la compuerta, patillaje automático del MOSFET.
- Corriente recomendada para LEDs según su resistencia dinámica, y prueba de calentamiento.
- Probar y adaptar el panel a Linux y macOS.
- Traducción de la interfaz al inglés.

## Cómo enviar un cambio

1. Haz un *fork* del repositorio y crea una rama.
2. Haz tus cambios y pruébalos con `--sim`, y con la fuente real si puedes.
3. Abre un *pull request* explicando qué cambia, cómo lo probaste y con qué firmware.

# Punk AutoTrade

Bot que lee las señales de un canal de Telegram y las opera automáticamente en **Bitunix Futures (USDT-M)**, con SL, TP y Break Even gestionados por el propio bot, y un panel web para seguir todo en tiempo real.

```
Canal de Telegram  →  lectura de la señal  →  validación y riesgo  →  orden en Bitunix
                                                                          ↓
              Panel web  ←  conciliación  ←  Break Even  ←  SL y TP confirmados
```

> ⚠️ **Software que opera con dinero real.** Puede generar pérdidas. Usalo bajo tu propia responsabilidad, empezá con montos chicos y con una API key **sin permiso de retiro**. Nada de este repositorio es asesoramiento financiero.

## Qué hace

- **Lee la señal del canal**: par, dirección (Long/Short), entrada, Stop Loss, objetivos y apalancamiento (`Apalancamiento: x20`).
- **Entra a mercado o con orden límite** según qué tan cerca esté el precio de la entrada de la señal.
- **El SL viaja pegado a la orden de entrada**: la posición nunca queda sin protección.
- **Take Profit** en el último objetivo (modo `LAST`) o repartido en hasta 5 parciales (modo `SPLIT`).
- **Break Even automático** al tocar el TP1 o con 1 % de movimiento a favor (20 % de ROI a x20). El SL pasa a la entrada más las comisiones ya pagadas (apertura y financiación, leídas de Bitunix), la comisión de cierre y una pequeña ganancia. Un cierre en Break Even cuenta como empate, no como pérdida.
- **Lee los mensajes de seguimiento del canal** ("TP1 alcanzado", "mover a BE") y avisa si no coinciden con tu cuenta.
- **Cantidad de operaciones según tu saldo**: abre tantas como permita el margen disponible.
- **Notificaciones a tu Telegram personal** cuando abre, protege, mueve a BE o cierra.
- **Panel web** con posiciones abiertas, historial, resultados por período (1 día, 7 días, 1 mes o rango personalizado) y por sesión de mercado (Asia, Londres, Nueva York, Australia).

## Seguridad por diseño

- **Nunca reenvía una orden.** Si Bitunix no responde, la operación queda como "desconocida" y se verifica consultando la cuenta, en lugar de arriesgar una posición duplicada.
- **Cada orden lleva un ID propio** que no se reutiliza.
- **Una respuesta OK no alcanza:** el bot relee Bitunix y confirma que la posición, el SL y el TP existen.
- **Si queda algo sin conciliar, no abre nada nuevo** hasta resolverlo.
- **El modo real no se activa con una variable de entorno.** Requiere armar el automático por consola con una frase de confirmación escrita.
- **Solo opera en margen aislado** y respeta los límites de pérdida, exposición y apalancamiento que configures.

## Requisitos

- Windows 10/11 o Linux
- Python 3.11
- Cuenta de Telegram con acceso al canal de señales y credenciales de API de Telegram (https://my.telegram.org)
- Cuenta de Bitunix Futures y una API key con permiso de **trading** (nunca de retiro)

## Instalación

```bash
git clone https://github.com/ServicioProgramador/PunkAutoTrade.git
cd PunkAutoTrade
pip install -r requirements.txt
```

Copiá `.env.example` a `.env` y completalo:

| Variable | Para qué |
| --- | --- |
| `PA_TG_API_ID` / `PA_TG_API_HASH` | Credenciales de Telegram (my.telegram.org) |
| `PA_CHANNELS` | ID numérico del canal de señales |
| `PA_BITUNIX_API_KEY` / `PA_BITUNIX_API_SECRET` | API de Bitunix Futures |
| `PA_NOTIFY_BOT_TOKEN` / `PA_NOTIFY_CHAT_ID` | Opcional: avisos a tu Telegram mediante tu bot de @BotFather |
| `PA_LIVE_FEE_RATE` | Opcional: comisión usada para el Break Even (por defecto 0.0006) |
| `PA_LIVE_BE_MOVE_PCT` | Opcional: % de movimiento a favor que activa el Break Even (por defecto 1) |
| `PA_LIVE_BE_PROFIT_PCT` | Opcional: ganancia extra que asegura el Break Even, sobre las comisiones (por defecto 0.1 %) |

**El `.env` nunca se sube ni se comparte.** Ya está excluido en el `.gitignore`.

## Primeros pasos

```bash
# 1. Iniciar sesión en Telegram (solo la primera vez)
python run.py --telegram-login

# 2. Buscar el ID del canal y ponerlo en PA_CHANNELS
python run.py --telegram-channels

# 3. Verificar la conexión con Bitunix (solo lectura, no opera)
python run.py --live-check BTCUSDT

# 4. Opcional: configurar y probar las notificaciones
python run.py --notify-setup
python run.py --notify-test
```

## Uso

**Armar el automático** (pide una frase de confirmación escrita):

```bash
# Modo prueba: decide pero no envía órdenes
python run.py --live-arm SHADOW

# Modo real: cantidad de operaciones según saldo, sin límite diario
python run.py --live-arm LIVE --max-open AUTO --max-day AUTO
```

**Iniciar el bot con el panel:**

```bash
python run.py --telegram --telegram-paper --bitunix-read --market-feed --market-feed-paper --live-supervise --live-auto
```

El panel queda en **http://127.0.0.1:8766**.

**Comandos útiles:**

| Comando | Qué hace |
| --- | --- |
| `python run.py --live-auto-status` | Estado del automático y sus últimas decisiones |
| `python run.py --live-status` | Operaciones registradas |
| `python run.py --live-disarm` | Desarma el automático (las posiciones abiertas siguen protegidas) |
| `python run.py --live-reconcile` | Concilia con Bitunix y completa protecciones pendientes |
| `python run.py --help` | Lista completa de opciones |

## Operar a mano con el sistema

**Abrir una señal a mano, validada por el sistema.** Si el automático no abrió una señal (por ejemplo, porque la pérdida hasta el SL superaba tu límite) y todavía estás a tiempo:

```bash
python run.py --live-open 734 --max-loss 6
```

El número es el del mensaje del canal. El sistema hace los mismos chequeos que el automático (precio todavía entre el SL y el TP1, margen, exposición, margen aislado) y decide si entra a mercado o con orden límite. `--max-loss` sube el tope de pérdida solo para esa operación. Pide confirmación escrita antes de enviar.

**Posiciones abiertas a mano en Bitunix.** El bot las detecta y las gestiona como propias: usa tu SL y tu TP si los pusiste, o los de la última señal del canal para ese par, y mueve el SL a Break Even igual que en las operaciones automáticas. No toca posiciones con varias órdenes de SL/TP, ni inventa un SL si no hay señal. Se desactiva con `PA_LIVE_ADOPT_MANUAL=NO`.

## Configuración del automático

| Opción | Por defecto | Qué controla |
| --- | --- | --- |
| `--max-open` | `2` | Operaciones simultáneas (`AUTO` = según saldo) |
| `--max-day` | `3` | Entradas por día (`AUTO` = sin límite) |
| `--entry-policy` | `AUTO` | `AUTO`: a mercado si el precio está cerca de la entrada; si no, límite |
| `--max-dev` | `0.3` | % de desvío máximo para entrar a mercado |
| `--tp-mode` | `LAST` | `LAST`: un TP en el último objetivo; `SPLIT`: hasta 5 parciales |

## Por qué una señal puede no abrirse

El bot registra el motivo de cada señal descartada. Lo ves en la pestaña **Señales** del panel, con `--live-auto-status` o en las notificaciones. Los más comunes:

- El par está en margen cruzado (pasalo a Aislado en Bitunix).
- El precio ya pasó la entrada o el TP1.
- La pérdida hasta el SL supera tu límite configurado.
- La señal llegó con más de 120 segundos de demora.
- No hay margen disponible.

## Licencia

Uso personal. Software provisto "tal cual", sin garantías de ningún tipo.

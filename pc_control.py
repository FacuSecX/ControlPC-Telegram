#Bot Telegram para controlar un pc remotamente
#github: https://github.com/FacuSecX



import asyncio
import ctypes
import html
import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

import GPUtil
import mss
import mss.tools
import psutil
import pyautogui
import pyperclip
import requests

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# =========================================================
# CONFIGURACIÓN
# =========================================================

# Carga el archivo .env ubicado en la misma carpeta que este script.
# El .env contiene la configuración privada y no debe publicarse..



ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)

# Configuración sensible: se carga desde .env / variables de entorno.
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

try:
    ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0").strip())
except ValueError:
    ALLOWED_USER_ID = 0

PLAYLIST_FOLDER = os.environ.get(
    "PLAYLIST_FOLDER",
    str(Path.home() / "Desktop" / "Scripts"),
)
VLC_PATH = os.environ.get(
    "VLC_PATH",
    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
)
VLC_PASSWORD = os.environ.get("VLC_PASSWORD", "")

SOUNDVOLUMEVIEW_PATH = os.environ.get(
    "SOUNDVOLUMEVIEW_PATH",
    str(Path(PLAYLIST_FOLDER) / "SoundVolumeView.exe"),
)
SETVOL_PATH = os.environ.get(
    "SETVOL_PATH",
    str(Path(PLAYLIST_FOLDER) / "SetVol.exe"),
)

SEND_ONLINE_NOTIFICATION = True
COMMAND_TIMEOUT_SECONDS = 30
COMMAND_HISTORY_LIMIT = 20
VERSION = "3.0"
ADMIN_SESSION_MINUTES = 10
# Opcional: si colocas un PIN (ej. "2580"), se pedira al activar modo administrador.
# Vacio = activacion por doble confirmacion desde tu cuenta autorizada.
ADMIN_PIN = os.environ.get("ADMIN_PIN", "")
FILE_PAGE_SIZE = 8
WINDOW_PAGE_SIZE = 8
MAX_SEND_FILE_MB = 45
MAX_RECEIVE_FILE_MB = 20

BASE_DIR = Path(__file__).resolve().parent
LOG_PATH = BASE_DIR / "pc_control.log"
COMMAND_HISTORY_PATH = BASE_DIR / "cmd_history.jsonl"
MACROS_PATH = BASE_DIR / "macros.json"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

USER_HOME = Path.home()
FILE_ROOTS = [
    ("🖥 Escritorio", USER_HOME / "Desktop"),
    ("📥 Descargas", USER_HOME / "Downloads"),
    ("📄 Documentos", USER_HOME / "Documents"),
    ("🧰 Scripts", Path(PLAYLIST_FOLDER)),
]

PANEL_MESSAGE_ID = "panel_message_id"
STATE_KEY = "input_state"
PENDING_CMD_KEY = "pending_cmd"
PROCESS_RETURN_KEY = "process_return"
FILE_CURRENT_DIR_KEY = "file_current_dir"
FILE_CACHE_KEY = "file_cache"
FILE_PAGE_KEY = "file_page"
FILE_SELECTED_KEY = "file_selected"
WINDOW_CACHE_KEY = "window_cache"
WINDOW_PAGE_KEY = "window_page"
ADMIN_UNTIL_KEY = "admin_until"
ADMIN_PENDING_KEY = "admin_pending"
LAST_ACTION = {"name": "Inicio del bot", "timestamp": time.time()}

# Estados de entrada de texto.
STATE_CMD = "cmd"
STATE_CLIPBOARD_COPY = "clipboard_copy"
STATE_NOTIFICATION = "notification"
STATE_ADMIN_PIN = "admin_pin"
STATE_FILE_UPLOAD = "file_upload"

# Cachés no sensibles y temporales.
audio_devices_cache = {}
playlist_cache = {}

BOT_STARTED_AT = time.time()


# =========================================================
# LOGS
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("pc-control-bot")


# =========================================================
# UTILIDADES TELEGRAM / NAVEGACIÓN
# =========================================================

def autorizado(update: Update) -> bool:
    usuario = update.effective_user
    return bool(usuario and usuario.id == ALLOWED_USER_ID)


def boton(texto: str, callback_data: str, style: str | None = None):
    """Usa colores si la versión de Telegram/PTB los admite."""
    kwargs = {
        "text": texto,
        "callback_data": callback_data,
    }
    if style:
        kwargs["style"] = style

    try:
        return InlineKeyboardButton(**kwargs)
    except TypeError:
        kwargs.pop("style", None)
        return InlineKeyboardButton(**kwargs)


async def borrar_mensaje_seguro(bot, chat_id: int, message_id: int | None):
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError:
        pass


async def mostrar_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    texto: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = "HTML",
    nuevo: bool = False,
):
    """
    Mantiene un único panel de navegación.

    Siempre que sea posible edita el panel actual. Si debe crear uno nuevo,
    elimina el panel anterior para evitar acumulación de ventanas.
    """
    chat = update.effective_chat
    if chat is None:
        return None

    chat_id = chat.id
    panel_id = context.user_data.get(PANEL_MESSAGE_ID)
    query = update.callback_query

    # En callbacks, el mensaje pulsado suele ser el panel actual.
    if query and query.message and not nuevo:
        try:
            mensaje = await query.edit_message_text(
                text=texto,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
            context.user_data[PANEL_MESSAGE_ID] = mensaje.message_id
            return mensaje
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return query.message
        except TelegramError:
            pass

    # Desde mensajes de texto del usuario intentamos editar el panel guardado.
    if panel_id and not nuevo:
        try:
            mensaje = await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=panel_id,
                text=texto,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
            context.user_data[PANEL_MESSAGE_ID] = mensaje.message_id
            return mensaje
        except TelegramError:
            pass

    mensaje = await context.bot.send_message(
        chat_id=chat_id,
        text=texto,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
    )

    if panel_id and panel_id != mensaje.message_id:
        await borrar_mensaje_seguro(context.bot, chat_id, panel_id)

    context.user_data[PANEL_MESSAGE_ID] = mensaje.message_id
    return mensaje


async def eliminar_panel_actual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat is None:
        return
    panel_id = context.user_data.pop(PANEL_MESSAGE_ID, None)
    await borrar_mensaje_seguro(context.bot, chat.id, panel_id)


def limpiar_estado_entrada(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop(STATE_KEY, None)
    context.user_data.pop(PENDING_CMD_KEY, None)


def registrar_accion(nombre: str):
    LAST_ACTION["name"] = nombre
    LAST_ACTION["timestamp"] = time.time()


def admin_activo(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return float(context.user_data.get(ADMIN_UNTIL_KEY, 0) or 0) > time.time()


def admin_restante(context: ContextTypes.DEFAULT_TYPE) -> int:
    restante = int(float(context.user_data.get(ADMIN_UNTIL_KEY, 0) or 0) - time.time())
    return max(0, restante)


def requiere_admin(data: str) -> bool:
    return (
        data == "cmd_confirm"
        or data.startswith("process_confirm::")
        or data.startswith("file_delete_confirm::")
        or data.startswith("window_close_confirm::")
        or data.startswith("power_confirm::restart")
        or data.startswith("power_confirm::shutdown")
        or data == "bot_restart_confirm"
    )


def ruta_autorizada(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except Exception:
        return False
    for _, root in FILE_ROOTS:
        try:
            root_resolved = root.resolve()
            resolved.relative_to(root_resolved)
            return True
        except Exception:
            continue
    return False


def root_para_ruta(path: Path):
    try:
        resolved = path.resolve()
    except Exception:
        return None
    for nombre, root in FILE_ROOTS:
        try:
            rr = root.resolve()
            resolved.relative_to(rr)
            return nombre, rr
        except Exception:
            continue
    return None


# =========================================================
# FORMATO / STATUS
# =========================================================

def formato_bytes(numero: float) -> str:
    numero = float(numero)
    for unidad in ("B", "KB", "MB", "GB", "TB"):
        if abs(numero) < 1024.0 or unidad == "TB":
            return f"{numero:.1f} {unidad}"
        numero /= 1024.0
    return f"{numero:.1f} TB"


def formato_duracion(segundos: float) -> str:
    segundos = max(0, int(segundos))
    dias, resto = divmod(segundos, 86400)
    horas, resto = divmod(resto, 3600)
    minutos, _ = divmod(resto, 60)

    partes = []
    if dias:
        partes.append(f"{dias}d")
    if horas or dias:
        partes.append(f"{horas}h")
    partes.append(f"{minutos}m")
    return " ".join(partes)


def _red_mbps(delta_bytes: int, segundos: float) -> float:
    if segundos <= 0:
        return 0.0
    return (delta_bytes * 8) / segundos / 1_000_000


def get_status() -> str:
    cpu = psutil.cpu_percent(interval=0.35)
    memoria = psutil.virtual_memory()

    # Disco del sistema Windows.
    unidad = os.environ.get("SystemDrive", "C:") + "\\"
    try:
        disco = psutil.disk_usage(unidad)
        disco_texto = (
            f"💽 <b>Disco {html.escape(unidad)}</b>\n"
            f"Usado: {formato_bytes(disco.used)} / {formato_bytes(disco.total)} "
            f"({disco.percent:.0f}%)\n"
            f"Libre: {formato_bytes(disco.free)}"
        )
    except Exception:
        disco_texto = "💽 <b>Disco</b>\nNo disponible"

    # Velocidad instantánea aproximada de red.
    red_1 = psutil.net_io_counters()
    inicio = time.monotonic()
    time.sleep(0.35)
    red_2 = psutil.net_io_counters()
    duracion = time.monotonic() - inicio
    bajada = _red_mbps(red_2.bytes_recv - red_1.bytes_recv, duracion)
    subida = _red_mbps(red_2.bytes_sent - red_1.bytes_sent, duracion)

    gpu_texto = "🎮 <b>GPU</b>\nNo detectada"
    try:
        gpus = GPUtil.getGPUs()
        if gpus:
            gpu = gpus[0]
            temp = "N/D" if gpu.temperature is None else f"{gpu.temperature:.0f} °C"
            gpu_texto = (
                f"🎮 <b>GPU</b>\n"
                f"{html.escape(gpu.name)}\n"
                f"Uso: {gpu.load * 100:.0f}%\n"
                f"VRAM: {gpu.memoryUsed:.0f} / {gpu.memoryTotal:.0f} MB\n"
                f"Temperatura: {temp}"
            )
    except Exception:
        pass

    try:
        windows_uptime = time.time() - psutil.boot_time()
    except Exception:
        windows_uptime = 0

    os_text = platform.platform(terse=True)

    return (
        "🖥 <b>ESTADO DEL PC</b>\n\n"
        f"🧠 <b>CPU</b>\nUso: {cpu:.0f}%\n\n"
        f"💾 <b>RAM</b>\n"
        f"Uso: {formato_bytes(memoria.used)} / {formato_bytes(memoria.total)} "
        f"({memoria.percent:.0f}%)\n\n"
        f"{gpu_texto}\n\n"
        f"{disco_texto}\n\n"
        "🌐 <b>Red</b>\n"
        f"↓ {bajada:.2f} Mbps   ↑ {subida:.2f} Mbps\n\n"
        "⏱ <b>Tiempo encendido</b>\n"
        f"PC: {formato_duracion(windows_uptime)}\n"
        f"Bot: {formato_duracion(time.time() - BOT_STARTED_AT)}\n\n"
        f"🪟 {html.escape(os_text)}"
    )


# =========================================================
# AUDIO
# =========================================================

def _normalize_audio_key(key):
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _audio_field(item, *names):
    normalized = {_normalize_audio_key(k): v for k, v in item.items()}
    for name in names:
        value = normalized.get(_normalize_audio_key(name))
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _audio_display_name(name, device_name):
    name = (name or "").strip()
    device_name = (device_name or "").strip()
    if not name:
        return device_name or "Dispositivo de audio"
    if not device_name or device_name.lower() in name.lower():
        return name
    if name.lower() in device_name.lower():
        return device_name
    return f"{name} ({device_name})"


def get_audio_output_devices():
    if not os.path.isfile(SOUNDVOLUMEVIEW_PATH):
        return [], f"No se encontró SoundVolumeView.exe en:\n{SOUNDVOLUMEVIEW_PATH}"

    temp_path = ""
    try:
        fd, temp_path = tempfile.mkstemp(prefix="telegram_audio_", suffix=".json")
        os.close(fd)
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass

        columns = (
            "Name,Device Name,Command-Line Friendly ID,Item ID,"
            "Type,Direction,Device State,Default"
        )
        subprocess.run(
            [
                SOUNDVOLUMEVIEW_PATH,
                "/SaveFileEncoding", "3",
                "/sjson", temp_path,
                "/Columns", columns,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=CREATE_NO_WINDOW,
        )

        if not os.path.isfile(temp_path):
            return [], "SoundVolumeView no creó la lista de dispositivos."

        with open(temp_path, "r", encoding="utf-8-sig") as archivo:
            raw_items = json.load(archivo)
        if isinstance(raw_items, dict):
            raw_items = [raw_items]

        devices = []
        seen_ids = set()
        for item in raw_items:
            if not isinstance(item, dict):
                continue

            item_type = _audio_field(item, "Type")
            direction = _audio_field(item, "Direction")
            state = _audio_field(item, "Device State", "State")
            if item_type and "device" not in item_type.lower():
                continue
            if direction and "render" not in direction.lower():
                continue
            if state and "active" not in state.lower():
                continue

            name = _audio_field(item, "Name")
            device_name = _audio_field(item, "Device Name")
            command_id = _audio_field(item, "Command-Line Friendly ID", "Item ID")
            default_value = _audio_field(item, "Default")
            is_default = bool(
                default_value
                and default_value.casefold() not in {"no", "false", "0"}
            )

            if not command_id:
                continue
            key = command_id.casefold()
            if key in seen_ids:
                continue
            seen_ids.add(key)
            devices.append({
                "display_name": _audio_display_name(name, device_name),
                "command_id": command_id,
                "default": is_default,
            })

        devices.sort(key=lambda d: (not d["default"], d["display_name"].casefold()))
        if not devices:
            return [], "No se encontraron salidas de audio activas."
        return devices, ""

    except subprocess.TimeoutExpired:
        return [], "SoundVolumeView tardó demasiado en responder."
    except subprocess.CalledProcessError as error:
        detalle = (error.stderr or error.stdout or str(error)).strip()
        return [], f"Error ejecutando SoundVolumeView:\n{detalle}"
    except Exception as error:
        return [], f"Error detectando dispositivos:\n{error}"
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass


def set_default_audio_device(command_id):
    if not os.path.isfile(SOUNDVOLUMEVIEW_PATH):
        return False, f"No se encontró SoundVolumeView.exe en:\n{SOUNDVOLUMEVIEW_PATH}"
    try:
        result = subprocess.run(
            [SOUNDVOLUMEVIEW_PATH, "/SetDefault", command_id, "all"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
        return True, result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "SoundVolumeView tardó demasiado en responder."
    except subprocess.CalledProcessError as error:
        detalle = (error.stderr or error.stdout or str(error)).strip()
        return False, detalle
    except Exception as error:
        return False, str(error)


def set_volume(volume):
    try:
        volume = int(volume)
        if not 0 <= volume <= 100:
            return False, "El volumen debe estar entre 0 y 100."
        if not os.path.isfile(SETVOL_PATH):
            return False, f"No se encontró SetVol.exe en:\n{SETVOL_PATH}"

        result = subprocess.run(
            [SETVOL_PATH, str(volume)],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        return True, result.stdout.strip()
    except subprocess.CalledProcessError as error:
        detalle = (error.stderr or error.stdout or str(error)).strip()
        return False, detalle
    except Exception as error:
        return False, str(error)


# =========================================================
# CAPTURAS
# =========================================================

def obtener_monitores():
    with mss.MSS() as sct:
        return len(sct.monitors) - 1


def screenshot(monitor_index: int = 0):
    """
    monitor_index 0 = todos los monitores.
    1..N = monitor específico.
    """
    with mss.MSS() as sct:
        if monitor_index < 0 or monitor_index >= len(sct.monitors):
            raise ValueError("Monitor no válido")
        monitor = sct.monitors[monitor_index]
        fd, ruta = tempfile.mkstemp(prefix="pc_screen_", suffix=".png")
        os.close(fd)
        imagen = sct.grab(monitor)
        mss.tools.to_png(imagen.rgb, imagen.size, output=ruta)
        return ruta


# =========================================================
# CONTROL WINDOWS
# =========================================================

def screen_off():
    ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)


def screen_on():
    ctypes.windll.user32.keybd_event(0, 0, 0, 0)
    ctypes.windll.user32.keybd_event(0, 0, 2, 0)


def switch_display(mode):
    try:
        displayswitch_path = r"C:\Windows\System32\DisplaySwitch.exe"
        subprocess.run(
            [displayswitch_path, f"/{mode}"],
            check=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
        return True
    except Exception:
        logger.exception("Error cambiando configuración de monitores")
        return False


def lock_pc():
    ctypes.windll.user32.LockWorkStation()


def suspend_pc():
    resultado = ctypes.windll.PowrProf.SetSuspendState(False, True, False)
    if resultado == 0:
        raise OSError("Windows rechazó la orden de suspensión")


def restart_pc():
    subprocess.Popen(["shutdown", "/r", "/t", "5"], creationflags=CREATE_NO_WINDOW)


def shutdown_pc():
    subprocess.Popen(["shutdown", "/s", "/t", "5"], creationflags=CREATE_NO_WINDOW)


# =========================================================
# VLC / MÚSICA
# =========================================================

def vlc_status():
    try:
        respuesta = requests.get(
            "http://127.0.0.1:8080/requests/status.json",
            auth=("", VLC_PASSWORD),
            timeout=2,
        )
        respuesta.raise_for_status()
        return respuesta.json(), ""
    except Exception as error:
        return {}, str(error)


def vlc_command(command):
    try:
        respuesta = requests.get(
            f"http://127.0.0.1:8080/requests/status.json?command={command}",
            auth=("", VLC_PASSWORD),
            timeout=2,
        )
        respuesta.raise_for_status()
        return True
    except Exception:
        logger.exception("Error enviando comando a VLC: %s", command)
        return False


def play_playlist(path):
    subprocess.Popen(
        [
            VLC_PATH,
            "--extraintf", "http",
            "--http-password", VLC_PASSWORD,
            "--one-instance",
            "--qt-start-minimized",
            path,
        ],
        creationflags=CREATE_NO_WINDOW,
    )


def pause_music():
    return vlc_command("pl_pause")


def next_music():
    return vlc_command("pl_next")


def prev_music():
    return vlc_command("pl_previous")


def stop_music():
    vlc_command("pl_stop")
    subprocess.run(
        ["taskkill", "/F", "/IM", "vlc.exe"],
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
    )


def music_status_text():
    estado, error = vlc_status()
    if not estado:
        return (
            "🎵 <b>MÚSICA</b>\n\n"
            "VLC no está respondiendo por la interfaz HTTP.\n"
            f"{html.escape(error[:300])}"
        )

    meta = (
        estado.get("information", {})
        .get("category", {})
        .get("meta", {})
    )
    titulo = meta.get("title") or meta.get("filename") or "Sin título"
    artista = meta.get("artist") or ""
    state = estado.get("state", "stopped")
    tiempo = int(estado.get("time") or 0)
    duracion = int(estado.get("length") or 0)

    def mmss(segundos):
        return f"{segundos // 60:02d}:{segundos % 60:02d}"

    estado_texto = {
        "playing": "▶ Reproduciendo",
        "paused": "⏸ Pausado",
        "stopped": "⏹ Detenido",
    }.get(state, state)

    lineas = [
        "🎵 <b>MÚSICA</b>",
        "",
        f"{estado_texto}",
        f"🎶 {html.escape(str(titulo))}",
    ]
    if artista:
        lineas.append(f"👤 {html.escape(str(artista))}")
    lineas.append(f"⏱ {mmss(tiempo)} / {mmss(duracion)}")
    return "\n".join(lineas)


# =========================================================
# CLIPBOARD
# =========================================================

def pegar_clipboard(presionar_enter=False):
    pyautogui.hotkey("ctrl", "v")
    if presionar_enter:
        time.sleep(0.1)
        pyautogui.press("enter")


# =========================================================
# PROCESOS
# =========================================================

def top_procesos(orden="cpu", limite=8):
    procesos = []
    objetos = []

    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            proc.cpu_percent(None)
            objetos.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if orden == "cpu":
        time.sleep(0.45)

    for proc in objetos:
        try:
            info = proc.as_dict(attrs=["pid", "name", "memory_info"])
            cpu = proc.cpu_percent(None) if orden == "cpu" else 0.0
            memoria = info.get("memory_info")
            ram = memoria.rss if memoria else 0
            procesos.append({
                "pid": info["pid"],
                "name": info.get("name") or "Proceso",
                "cpu": cpu,
                "ram": ram,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    key = (lambda p: p["cpu"]) if orden == "cpu" else (lambda p: p["ram"])
    return sorted(procesos, key=key, reverse=True)[:limite]


def detalle_proceso(pid: int):
    proc = psutil.Process(pid)
    with proc.oneshot():
        nombre = proc.name()
        estado = proc.status()
        memoria = proc.memory_info().rss
        creado = datetime.fromtimestamp(proc.create_time()).strftime("%d-%m-%Y %H:%M:%S")
        try:
            exe = proc.exe()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            exe = "No disponible"
        try:
            usuario = proc.username()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            usuario = "No disponible"

    return {
        "pid": pid,
        "nombre": nombre,
        "estado": estado,
        "ram": memoria,
        "creado": creado,
        "exe": exe,
        "usuario": usuario,
    }


def terminar_proceso(pid: int):
    if pid in {0, 4, os.getpid()}:
        return False, "Este proceso está protegido y no se cerrará desde el bot."

    proc = psutil.Process(pid)
    nombre = proc.name()
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except psutil.TimeoutExpired:
        return False, f"{nombre} recibió la orden de cierre, pero sigue activo."
    return True, f"{nombre} finalizado."


# =========================================================
# CMD / HISTORIAL
# =========================================================

def guardar_historial_cmd(comando: str, resultado: str, ok: bool):
    registro = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "command": comando,
        "ok": ok,
        "result_preview": resultado[:1200],
    }
    with open(COMMAND_HISTORY_PATH, "a", encoding="utf-8") as archivo:
        archivo.write(json.dumps(registro, ensure_ascii=False) + "\n")


def leer_historial_cmd(limite=10):
    if not COMMAND_HISTORY_PATH.exists():
        return []
    try:
        lineas = COMMAND_HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    registros = []
    for linea in lineas[-max(1, limite):]:
        try:
            registros.append(json.loads(linea))
        except json.JSONDecodeError:
            continue
    return registros


def ejecutar_cmd(comando: str):
    try:
        resultado = subprocess.check_output(
            comando,
            shell=True,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=COMMAND_TIMEOUT_SECONDS,
            creationflags=CREATE_NO_WINDOW,
        )
        if not resultado.strip():
            resultado = "Comando ejecutado sin salida."
        ok = True
    except subprocess.CalledProcessError as error:
        resultado = error.output or str(error)
        ok = False
    except subprocess.TimeoutExpired:
        resultado = f"Tiempo agotado después de {COMMAND_TIMEOUT_SECONDS} segundos."
        ok = False
    except Exception as error:
        resultado = str(error)
        ok = False

    guardar_historial_cmd(comando, resultado, ok)
    logger.info("CMD ejecutado: %s | ok=%s", comando, ok)
    return ok, resultado



# =========================================================
# RED
# =========================================================

def _ssid_wifi():
    try:
        r = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            creationflags=CREATE_NO_WINDOW,
        )
        match = re.search(r"^\s*SSID\s*:\s*(.+)$", r.stdout, re.MULTILINE)
        return match.group(1).strip() if match else "No detectado"
    except Exception:
        return "No disponible"


def network_status_text():
    local_ip = "No disponible"
    public_ip = "No disponible"
    ping_text = "No disponible"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            local_ip = sock.getsockname()[0]
    except Exception:
        pass

    try:
        r = requests.get("https://api.ipify.org?format=json", timeout=3)
        r.raise_for_status()
        public_ip = r.json().get("ip", "No disponible")
    except Exception:
        pass

    try:
        inicio = time.monotonic()
        proc = subprocess.run(
            ["ping", "-n", "1", "-w", "1500", "1.1.1.1"],
            capture_output=True,
            text=True,
            timeout=4,
            creationflags=CREATE_NO_WINDOW,
        )
        if proc.returncode == 0:
            ping_text = f"{(time.monotonic() - inicio) * 1000:.0f} ms aprox."
    except Exception:
        pass

    io1 = psutil.net_io_counters()
    t1 = time.monotonic()
    time.sleep(0.45)
    io2 = psutil.net_io_counters()
    dt = max(0.001, time.monotonic() - t1)
    down = _red_mbps(io2.bytes_recv - io1.bytes_recv, dt)
    up = _red_mbps(io2.bytes_sent - io1.bytes_sent, dt)
    activos = [name for name, st in psutil.net_if_stats().items() if st.isup]
    wifi = _ssid_wifi()
    return (
        "🌐 <b>RED</b>\n\n"
        f"🏠 IP local: <code>{html.escape(local_ip)}</code>\n"
        f"🌍 IP pública: <code>{html.escape(public_ip)}</code>\n"
        f"📶 Wi‑Fi: {html.escape(wifi)}\n"
        f"🏓 Ping: {html.escape(ping_text)}\n\n"
        f"⚡ Ahora: ⬇ {down:.2f} Mbps · ⬆ {up:.2f} Mbps\n"
        f"⬇ Recibido desde inicio: {formato_bytes(io2.bytes_recv)}\n"
        f"⬆ Enviado desde inicio: {formato_bytes(io2.bytes_sent)}\n\n"
        f"🔌 Interfaces activas: {len(activos)}"
    )


def network_interfaces_text():
    addrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    lines = ["🌐 <b>INTERFACES DE RED</b>", ""]
    for name, st in stats.items():
        if not st.isup:
            continue
        ips = []
        for addr in addrs.get(name, []):
            if getattr(addr.family, "name", "") in {"AF_INET", "AF_INET6"}:
                ips.append(addr.address.split("%", 1)[0])
        lines.append(f"✅ <b>{html.escape(name)}</b>")
        if ips:
            lines.append("   " + html.escape(" · ".join(ips[:3])))
        if st.speed:
            lines.append(f"   Velocidad enlace: {st.speed} Mbps")
    if len(lines) == 2:
        lines.append("No se detectaron interfaces activas.")
    return "\n".join(lines)


# =========================================================
# GPU AVANZADA
# =========================================================

def _nvidia_smi_path():
    path = shutil.which("nvidia-smi")
    if path:
        return path
    candidate = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
    return str(candidate) if candidate.exists() else None


def gpu_advanced_text():
    smi = _nvidia_smi_path()
    lines = ["🎮 <b>GPU AVANZADA</b>", ""]
    if smi:
        try:
            query = (
                "name,utilization.gpu,memory.used,memory.total,temperature.gpu,"
                "power.draw,power.limit,clocks.sm,clocks.mem"
            )
            r = subprocess.run(
                [smi, f"--query-gpu={query}", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=CREATE_NO_WINDOW,
            )
            if r.returncode == 0 and r.stdout.strip():
                for idx, row in enumerate(r.stdout.strip().splitlines(), 1):
                    parts = [p.strip() for p in row.split(",")]
                    if len(parts) >= 9:
                        name, util, memu, memt, temp, power, plimit, csm, cmem = parts[:9]
                        lines += [
                            f"<b>GPU {idx}: {html.escape(name)}</b>",
                            f"Uso: {html.escape(util)}%",
                            f"VRAM: {html.escape(memu)} / {html.escape(memt)} MB",
                            f"Temperatura: {html.escape(temp)} °C",
                            f"Potencia: {html.escape(power)} / {html.escape(plimit)} W",
                            f"Clock core: {html.escape(csm)} MHz",
                            f"Clock memoria: {html.escape(cmem)} MHz",
                            "",
                        ]
            else:
                raise RuntimeError(r.stderr.strip() or "nvidia-smi sin salida")
        except Exception as error:
            lines.append(f"nvidia-smi no respondió: {html.escape(str(error))}")
    else:
        try:
            gpus = GPUtil.getGPUs()
            for idx, gpu in enumerate(gpus, 1):
                lines += [
                    f"<b>GPU {idx}: {html.escape(gpu.name)}</b>",
                    f"Uso: {gpu.load * 100:.0f}%",
                    f"VRAM: {gpu.memoryUsed:.0f}/{gpu.memoryTotal:.0f} MB",
                    f"Temperatura: {gpu.temperature:.0f} °C",
                    "",
                ]
        except Exception as error:
            lines.append(f"GPU no disponible: {html.escape(str(error))}")
    return "\n".join(lines).rstrip()


def gpu_processes_text():
    smi = _nvidia_smi_path()
    if not smi:
        return "🎮 <b>PROCESOS GPU</b>\n\nnvidia-smi no está disponible."
    try:
        r = subprocess.run(
            [
                smi,
                "--query-compute-apps=pid,process_name,used_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=CREATE_NO_WINDOW,
        )
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip())
        rows = [x.strip() for x in r.stdout.splitlines() if x.strip()]
        lines = ["🎮 <b>PROCESOS GPU</b>", ""]
        if not rows:
            lines.append("No hay procesos de cómputo GPU detectados.")
        for row in rows[:15]:
            parts = [p.strip() for p in row.split(",", 2)]
            if len(parts) == 3:
                pid, name, mem = parts
                lines.append(f"• <code>{html.escape(pid)}</code> · {html.escape(Path(name).name)} · {html.escape(mem)} MB")
        return "\n".join(lines)
    except Exception as error:
        return f"🎮 <b>PROCESOS GPU</b>\n\n❌ {html.escape(str(error))}"


# =========================================================
# VENTANAS ABIERTAS
# =========================================================

def listar_ventanas():
    user32 = ctypes.windll.user32
    windows = []
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        if title:
            windows.append({"hwnd": int(hwnd), "title": title})
        return True

    user32.EnumWindows(EnumProc(callback), 0)
    return windows


def window_action(hwnd: int, action: str):
    user32 = ctypes.windll.user32
    if not user32.IsWindow(hwnd):
        return False, "La ventana ya no existe."
    if action == "minimize":
        user32.ShowWindow(hwnd, 6)
    elif action == "maximize":
        user32.ShowWindow(hwnd, 3)
    elif action == "focus":
        user32.ShowWindow(hwnd, 9)
        ok = bool(user32.SetForegroundWindow(hwnd))
        if not ok:
            return False, "Windows bloqueó el cambio de foco."
    elif action == "close":
        user32.PostMessageW(hwnd, 0x0010, 0, 0)
    else:
        return False, "Acción no válida."
    return True, "Acción enviada."


# =========================================================
# NOTIFICACIONES DE WINDOWS
# =========================================================

def windows_notification(texto: str):
    texto = texto.strip()[:500]
    if not texto:
        return False, "Texto vacío."
    safe = texto.replace("'", "''")
    script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$textNodes = $template.GetElementsByTagName('text')
$textNodes.Item(0).AppendChild($template.CreateTextNode('PC Control')) > $null
$textNodes.Item(1).AppendChild($template.CreateTextNode('{safe}')) > $null
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Windows PowerShell')
$notifier.Show($toast)
"""
    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        return True, "Notificación enviada."
    except Exception as error:
        return False, str(error)


# =========================================================
# CONTROL RÁPIDO
# =========================================================

def quick_key(action: str):
    actions = {
        "playpause": lambda: pyautogui.press("playpause"),
        "nexttrack": lambda: pyautogui.press("nexttrack"),
        "prevtrack": lambda: pyautogui.press("prevtrack"),
        "esc": lambda: pyautogui.press("esc"),
        "enter": lambda: pyautogui.press("enter"),
        "left": lambda: pyautogui.press("left"),
        "right": lambda: pyautogui.press("right"),
        "up": lambda: pyautogui.press("up"),
        "down": lambda: pyautogui.press("down"),
        "alttab": lambda: pyautogui.hotkey("alt", "tab"),
        "desktop": lambda: pyautogui.hotkey("win", "d"),
    }
    fn = actions.get(action)
    if not fn:
        raise ValueError("Acción rápida no válida")
    fn()


# =========================================================
# ARCHIVOS (SOLO CARPETAS AUTORIZADAS)
# =========================================================

def listar_archivos(path: Path):
    if not ruta_autorizada(path):
        raise PermissionError("Ruta fuera de las carpetas autorizadas")
    entries = []
    for entry in path.iterdir():
        try:
            is_dir = entry.is_dir()
            size = 0 if is_dir else entry.stat().st_size
            entries.append({"path": str(entry), "name": entry.name, "dir": is_dir, "size": size})
        except OSError:
            continue
    entries.sort(key=lambda e: (not e["dir"], e["name"].casefold()))
    return entries


def texto_ruta_archivos(path: Path):
    root = root_para_ruta(path)
    root_name = root[0] if root else "Archivos"
    return f"📁 <b>{html.escape(root_name)}</b>\n\n<code>{html.escape(str(path))}</code>"


# =========================================================
# MACROS
# =========================================================

def crear_macros_por_defecto():
    if MACROS_PATH.exists():
        return
    data = [
        {
            "name": "🖥 Modo trabajo",
            "actions": [
                {"type": "monitor", "mode": "extend"},
                {"type": "volume", "value": 50}
            ]
        },
        {
            "name": "🌙 Modo noche",
            "actions": [
                {"type": "volume", "value": 25}
            ]
        },
        {
            "name": "🎧 Modo música",
            "actions": [
                {"type": "volume", "value": 50},
                {"type": "key", "action": "playpause"}
            ]
        }
    ]
    MACROS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def cargar_macros():
    crear_macros_por_defecto()
    try:
        data = json.loads(MACROS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        logger.exception("No se pudo leer macros.json")
        return []


def ejecutar_macro(macro: dict):
    resultados = []
    for action in macro.get("actions", []):
        tipo = action.get("type")
        try:
            if tipo == "volume":
                ok, detail = set_volume(int(action.get("value", 50)))
                if not ok:
                    raise RuntimeError(detail)
            elif tipo == "monitor":
                if not switch_display(str(action.get("mode", "extend"))):
                    raise RuntimeError("No se pudo cambiar el modo de monitores")
            elif tipo == "key":
                quick_key(str(action.get("action", "")))
            elif tipo == "delay":
                time.sleep(max(0, min(float(action.get("seconds", 1)), 30)))
            elif tipo == "notification":
                ok, detail = windows_notification(str(action.get("text", "Macro ejecutada")))
                if not ok:
                    raise RuntimeError(detail)
            elif tipo == "screen_off":
                screen_off()
            elif tipo == "lock":
                lock_pc()
            else:
                raise RuntimeError(f"Tipo no soportado: {tipo}")
            resultados.append(f"✅ {tipo}")
        except Exception as error:
            resultados.append(f"❌ {tipo}: {error}")
    return resultados


# =========================================================
# BOT / REINICIO
# =========================================================

def bot_status_text(context: ContextTypes.DEFAULT_TYPE):
    boot = psutil.boot_time()
    last_age = formato_duracion(time.time() - LAST_ACTION["timestamp"])
    admin = admin_restante(context)
    return (
        "ℹ️ <b>ESTADO DEL BOT</b>\n\n"
        f"Versión: <b>{VERSION}</b>\n"
        f"PID: <code>{os.getpid()}</code>\n"
        f"Bot activo: {formato_duracion(time.time() - BOT_STARTED_AT)}\n"
        f"PC activo: {formato_duracion(time.time() - boot)}\n"
        f"Última acción: {html.escape(str(LAST_ACTION['name']))}\n"
        f"Hace: {last_age}\n"
        f"Telegram: 🟢 conectado\n"
        f"Modo admin: {'🟢 activo (' + formato_duracion(admin) + ')' if admin else '🔒 inactivo'}"
    )


def reiniciar_bot_proceso():
    os.execv(sys.executable, [sys.executable] + sys.argv)


# =========================================================
# MENÚS
# =========================================================

def menu_principal():
    return InlineKeyboardMarkup([
        [boton("📊 Estado", "status", "primary"), boton("🎮 GPU", "gpu")],
        [boton("📸 Capturas", "shots"), boton("🌐 Red", "network")],
        [boton("🎵 Música", "music"), boton("🔊 Volumen", "volume_menu")],
        [boton("🔊 Cambiar audio", "audio_menu"), boton("🖥 Monitores", "monitors")],
        [boton("⚙️ Procesos", "processes"), boton("🪟 Ventanas", "windows")],
        [boton("📁 Archivos", "files"), boton("⭐ Macros", "macros")],
        [boton("📋 Clipboard", "clipboard"), boton("⌨ CMD", "cmd")],
        [boton("⌨ Control rápido", "quick"), boton("🔔 Notificación", "notify")],
        [boton("💻 Notepad", "notepad"), boton("⚡ Energía", "power")],
        [boton("🔐 Admin", "admin"), boton("ℹ️ Bot", "bot_status")],
    ])


def status_menu():
    return InlineKeyboardMarkup([
        [boton("🔄 Actualizar", "status")],
        [boton("‹ Menú principal", "back")],
    ])


def screenshots_menu():
    filas = []
    try:
        cantidad = obtener_monitores()
    except Exception:
        cantidad = 1

    filas.append([boton("🖥 Todos los monitores", "shot::0")])
    for indice in range(1, cantidad + 1):
        filas.append([boton(f"🖥 Monitor {indice}", f"shot::{indice}")])
    filas.append([boton("‹ Menú principal", "back")])
    return InlineKeyboardMarkup(filas)


def playlists_menu(user_id: int):
    archivos = []
    try:
        archivos = sorted(
            [f for f in os.listdir(PLAYLIST_FOLDER) if f.lower().endswith(".m3u")],
            key=str.casefold,
        )
    except OSError:
        pass

    playlist_cache[user_id] = archivos
    filas = []
    for indice, nombre in enumerate(archivos):
        visible = nombre if len(nombre) <= 45 else nombre[:42] + "..."
        filas.append([boton(f"🎵 {visible}", f"playlist::{indice}")])

    if not filas:
        filas.append([boton("Sin playlists .m3u", "noop")])
    filas.append([boton("‹ Menú principal", "back")])
    return InlineKeyboardMarkup(filas)


def music_home_menu():
    return InlineKeyboardMarkup([
        [boton("🎛 Abrir reproductor", "music_player", "primary")],
        [boton("📂 Elegir playlist", "music_playlists")],
        [boton("‹ Menú principal", "back")],
    ])


def music_menu():
    return InlineKeyboardMarkup([
        [
            boton("⏮", "music_prev"),
            boton("⏯", "music_pause", "primary"),
            boton("⏭", "music_next"),
        ],
        [boton("🔄 Actualizar info", "music_refresh")],
        [boton("📂 Elegir playlist", "music_playlists")],
        [boton("⏹ Detener", "music_stop", "danger")],
        [boton("‹ Música", "music")],
    ])


def monitor_menu():
    return InlineKeyboardMarkup([
        [boton("🖥 Solo Monitor 1", "monitor_1")],
        [boton("🖥 Solo Monitor 2", "monitor_2")],
        [boton("📺 Duplicar", "monitor_clone")],
        [boton("↔ Extender", "monitor_extend")],
        [boton("‹ Menú principal", "back")],
    ])


def volume_menu():
    return InlineKeyboardMarkup([
        [boton("🔇 0%", "volume::0"), boton("🔈 25%", "volume::25")],
        [boton("🔉 50%", "volume::50"), boton("🔊 75%", "volume::75")],
        [boton("🔊 100%", "volume::100")],
        [boton("‹ Menú principal", "back")],
    ])


def audio_devices_menu(devices):
    filas = []
    for indice, device in enumerate(devices):
        nombre = device["display_name"]
        if len(nombre) > 42:
            nombre = nombre[:39] + "..."
        icono = "✅" if device.get("default") else "🔊"
        filas.append([boton(f"{icono} {nombre}", f"audio_device::{indice}")])
    filas.append([boton("🔄 Actualizar dispositivos", "audio_refresh")])
    filas.append([boton("‹ Menú principal", "back")])
    return InlineKeyboardMarkup(filas)


def clipboard_menu():
    return InlineKeyboardMarkup([
        [boton("📥 Ver clipboard del PC", "clipboard_view")],
        [boton("📤 Copiar texto al PC", "clipboard_copy")],
        [boton("⌨ Pegar clipboard", "clipboard_paste")],
        [boton("⏎ Pegar + Enter", "clipboard_paste_enter", "danger")],
        [boton("‹ Menú principal", "back")],
    ])


def power_menu():
    return InlineKeyboardMarkup([
        [boton("🔒 Bloquear PC", "power_lock")],
        [
            boton("📴 Pantalla OFF", "off"),
            boton("🔆 Pantalla ON", "on"),
        ],
        [boton("🌙 Suspender", "power_suspend")],
        [boton("🔄 Reiniciar PC", "power_restart", "danger")],
        [boton("🔌 Apagar PC", "power_shutdown", "danger")],
        [boton("‹ Menú principal", "back")],
    ])


def confirm_power_menu(action: str):
    textos = {
        "suspend": "🌙 Sí, suspender",
        "restart": "🔄 Sí, reiniciar",
        "shutdown": "🔌 Sí, apagar",
    }
    return InlineKeyboardMarkup([
        [boton(textos[action], f"power_confirm::{action}", "danger")],
        [boton("❌ Cancelar", "power")],
    ])


def processes_menu():
    return InlineKeyboardMarkup([
        [boton("🔥 Mayor uso CPU", "processes::cpu")],
        [boton("🧠 Mayor uso RAM", "processes::ram")],
        [boton("‹ Menú principal", "back")],
    ])


def process_list_menu(procesos, orden):
    filas = []
    for proc in procesos:
        nombre = proc["name"]
        if len(nombre) > 28:
            nombre = nombre[:25] + "..."
        if orden == "cpu":
            dato = f"{proc['cpu']:.1f}%"
        else:
            dato = formato_bytes(proc["ram"])
        filas.append([boton(f"{nombre} · {dato}", f"process::{proc['pid']}")])
    filas.append([boton("🔄 Actualizar", f"processes::{orden}")])
    filas.append([boton("‹ Procesos", "processes")])
    return InlineKeyboardMarkup(filas)


def process_detail_menu(pid: int):
    return InlineKeyboardMarkup([
        [boton("❌ Terminar proceso", f"process_terminate::{pid}", "danger")],
        [boton("‹ Procesos", "processes")],
    ])


def process_confirm_menu(pid: int):
    return InlineKeyboardMarkup([
        [boton("❌ Sí, terminar", f"process_confirm::{pid}", "danger")],
        [boton("Cancelar", f"process::{pid}")],
    ])


def cmd_menu():
    return InlineKeyboardMarkup([
        [boton("⌨ Ejecutar comando", "cmd_new")],
        [boton("📜 Historial CMD", "cmd_history")],
        [boton("‹ Menú principal", "back")],
    ])


def cmd_confirm_menu():
    return InlineKeyboardMarkup([
        [boton("▶ Ejecutar", "cmd_confirm", "danger")],
        [boton("❌ Cancelar", "cmd")],
    ])


def network_menu():
    return InlineKeyboardMarkup([
        [boton("🔄 Actualizar", "network")],
        [boton("🔌 Ver interfaces", "network_interfaces")],
        [boton("‹ Menú principal", "back")],
    ])


def gpu_menu():
    return InlineKeyboardMarkup([
        [boton("🔄 Actualizar", "gpu")],
        [boton("⚙️ Procesos GPU", "gpu_processes")],
        [boton("‹ Menú principal", "back")],
    ])


def windows_menu(windows, page=0):
    total_pages = max(1, (len(windows) + WINDOW_PAGE_SIZE - 1) // WINDOW_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * WINDOW_PAGE_SIZE
    filas = []
    for idx in range(start, min(start + WINDOW_PAGE_SIZE, len(windows))):
        title = windows[idx]["title"]
        visible = title if len(title) <= 38 else title[:35] + "..."
        filas.append([boton(f"🪟 {visible}", f"window::{idx}")])
    nav = []
    if page > 0:
        nav.append(boton("‹", f"windows_page::{page-1}"))
    if page + 1 < total_pages:
        nav.append(boton("›", f"windows_page::{page+1}"))
    if nav:
        filas.append(nav)
    filas.append([boton("🔄 Actualizar", "windows")])
    filas.append([boton("‹ Menú principal", "back")])
    return InlineKeyboardMarkup(filas)


def window_detail_menu(hwnd: int):
    return InlineKeyboardMarkup([
        [boton("👁 Traer al frente", f"window_action::{hwnd}::focus")],
        [boton("➖ Minimizar", f"window_action::{hwnd}::minimize"), boton("⬜ Maximizar", f"window_action::{hwnd}::maximize")],
        [boton("❌ Cerrar ventana", f"window_close::{hwnd}", "danger")],
        [boton("‹ Ventanas", "windows")],
    ])


def file_roots_menu():
    filas = []
    for idx, (name, root) in enumerate(FILE_ROOTS):
        if root.exists():
            filas.append([boton(name, f"files_root::{idx}")])
    filas.append([boton("‹ Menú principal", "back")])
    return InlineKeyboardMarkup(filas)


def file_browser_menu(entries, path: Path, page=0):
    total_pages = max(1, (len(entries) + FILE_PAGE_SIZE - 1) // FILE_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * FILE_PAGE_SIZE
    filas = []
    for idx in range(start, min(start + FILE_PAGE_SIZE, len(entries))):
        item = entries[idx]
        icon = "📁" if item["dir"] else "📄"
        visible = item["name"] if len(item["name"]) <= 34 else item["name"][:31] + "..."
        filas.append([boton(f"{icon} {visible}", f"file_item::{idx}")])
    nav = []
    if page > 0:
        nav.append(boton("‹", f"files_page::{page-1}"))
    if page + 1 < total_pages:
        nav.append(boton("›", f"files_page::{page+1}"))
    if nav:
        filas.append(nav)
    root = root_para_ruta(path)
    if root and path.resolve() != root[1]:
        filas.append([boton("⬆ Carpeta superior", "file_up")])
    filas.append([boton("📤 Subir archivo aquí", "file_upload")])
    filas.append([boton("‹ Carpetas", "files")])
    return InlineKeyboardMarkup(filas)


def file_detail_menu(index: int):
    return InlineKeyboardMarkup([
        [boton("📥 Enviar a Telegram", f"file_send::{index}")],
        [boton("🗑 Eliminar", f"file_delete::{index}", "danger")],
        [boton("‹ Carpeta", "file_return")],
    ])


def quick_menu():
    return InlineKeyboardMarkup([
        [boton("⏮", "quick::prevtrack"), boton("⏯", "quick::playpause", "primary"), boton("⏭", "quick::nexttrack")],
        [boton("Alt+Tab", "quick::alttab"), boton("🖥 Escritorio", "quick::desktop")],
        [boton("Esc", "quick::esc"), boton("Enter", "quick::enter")],
        [boton("⬅", "quick::left"), boton("⬆", "quick::up"), boton("⬇", "quick::down"), boton("➡", "quick::right")],
        [boton("‹ Menú principal", "back")],
    ])


def admin_menu(context):
    activo = admin_activo(context)
    filas = []
    if activo:
        filas.append([boton("🔒 Desactivar modo administrador", "admin_disable", "danger")])
    else:
        filas.append([boton("🔓 Activar por 10 minutos", "admin_activate", "danger")])
    filas.append([boton("‹ Menú principal", "back")])
    return InlineKeyboardMarkup(filas)


def admin_confirm_menu():
    return InlineKeyboardMarkup([
        [boton("🔓 Sí, activar 10 minutos", "admin_confirm", "danger")],
        [boton("❌ Cancelar", "admin")],
    ])


def bot_info_menu():
    return InlineKeyboardMarkup([
        [boton("🔄 Actualizar", "bot_status")],
        [boton("🔄 Reiniciar PCControl", "bot_restart", "danger")],
        [boton("‹ Menú principal", "back")],
    ])


def macros_menu(macros):
    filas = []
    for idx, macro in enumerate(macros[:20]):
        name = str(macro.get("name", f"Macro {idx+1}"))
        visible = name if len(name) <= 40 else name[:37] + "..."
        filas.append([boton(visible, f"macro::{idx}")])
    if not filas:
        filas.append([boton("Sin macros configuradas", "noop")])
    filas.append([boton("🔄 Recargar macros", "macros")])
    filas.append([boton("‹ Menú principal", "back")])
    return InlineKeyboardMarkup(filas)


# =========================================================
# TEXTOS DE MENÚ
# =========================================================

def texto_menu_principal():
    return (
        "🖥 <b>CONTROL REMOTO DEL PC</b>\n\n"
        "Selecciona una función:"
    )


# =========================================================
# HANDLERS
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    limpiar_estado_entrada(context)
    estado = await asyncio.to_thread(get_status)
    await mostrar_panel(
        update,
        context,
        f"{estado}\n\nSelecciona una función:",
        menu_principal(),
        nuevo=True,
    )


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q is None:
        return
    await q.answer()

    if not autorizado(update):
        return

    data = q.data or ""
    user_id = q.from_user.id
    registrar_accion(data or "callback")

    if data == "noop":
        return

    if requiere_admin(data) and not admin_activo(context):
        context.user_data[ADMIN_PENDING_KEY] = data
        await mostrar_panel(
            update,
            context,
            "🔐 <b>MODO ADMINISTRADOR REQUERIDO</b>\n\n"
            "Esta acción está protegida. Activa el modo administrador temporal y vuelve a intentarlo.",
            admin_menu(context),
        )
        return

    # Menú principal.
    if data == "back":
        limpiar_estado_entrada(context)
        await mostrar_panel(update, context, texto_menu_principal(), menu_principal())
        return

    # STATUS.
    if data == "status":
        await mostrar_panel(update, context, "📊 Obteniendo estado del PC…", status_menu())
        estado = await asyncio.to_thread(get_status)
        await mostrar_panel(update, context, estado, status_menu())
        return

    # CAPTURAS.
    if data == "shots":
        await mostrar_panel(
            update,
            context,
            "📸 <b>CAPTURAS DE PANTALLA</b>\n\nSelecciona qué monitor capturar:",
            screenshots_menu(),
        )
        return

    if data.startswith("shot::"):
        try:
            indice = int(data.split("::", 1)[1])
        except ValueError:
            return

        ruta = ""
        try:
            ruta = await asyncio.to_thread(screenshot, indice)
            await eliminar_panel_actual(update, context)
            etiqueta = "Todos los monitores" if indice == 0 else f"Monitor {indice}"
            with open(ruta, "rb") as archivo:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=archivo,
                    caption=f"📸 {etiqueta}",
                )
            await mostrar_panel(
                update,
                context,
                "📸 Captura enviada.",
                screenshots_menu(),
                nuevo=True,
            )
        except Exception as error:
            logger.exception("Error tomando screenshot")
            await mostrar_panel(
                update,
                context,
                f"❌ No se pudo realizar la captura.\n\n{html.escape(str(error))}",
                screenshots_menu(),
                nuevo=True,
            )
        finally:
            if ruta:
                try:
                    os.remove(ruta)
                except OSError:
                    pass
        return

    # NOTEPAD.
    if data == "notepad":
        try:
            subprocess.Popen(["notepad.exe"])
            texto = "✅ Notepad abierto."
        except Exception as error:
            texto = f"❌ Error abriendo Notepad:\n{html.escape(str(error))}"
        await mostrar_panel(update, context, texto, menu_principal())
        return

    # MÚSICA.
    if data == "music":
        estado, _ = await asyncio.to_thread(vlc_status)
        if estado:
            state = estado.get("state", "stopped")
            resumen = "Hay música disponible para controlar." if state in {"playing", "paused"} else "VLC está abierto, pero no está reproduciendo."
        else:
            resumen = "No se detectó una sesión VLC controlable en este momento."
        await mostrar_panel(
            update,
            context,
            "🎵 <b>MÚSICA</b>\n\n" + resumen + "\n\nPuedes abrir el reproductor actual o elegir otra playlist.",
            music_home_menu(),
        )
        return

    if data == "music_player":
        texto = await asyncio.to_thread(music_status_text)
        await mostrar_panel(update, context, texto, music_menu())
        return

    if data == "music_playlists":
        await mostrar_panel(
            update,
            context,
            "🎵 <b>PLAYLISTS DISPONIBLES</b>\n\nSelecciona una playlist:",
            playlists_menu(user_id),
        )
        return

    if data.startswith("playlist::"):
        try:
            indice = int(data.split("::", 1)[1])
            archivos = playlist_cache.get(user_id, [])
            nombre = archivos[indice]
            full_path = os.path.join(PLAYLIST_FOLDER, nombre)
            await asyncio.to_thread(play_playlist, full_path)
            await asyncio.sleep(1.0)
            texto = await asyncio.to_thread(music_status_text)
            await mostrar_panel(update, context, texto, music_menu())
        except (ValueError, IndexError):
            await mostrar_panel(
                update,
                context,
                "❌ La lista de playlists cambió. Vuelve a elegir.",
                playlists_menu(user_id),
            )
        except Exception as error:
            logger.exception("Error reproduciendo playlist")
            await mostrar_panel(
                update,
                context,
                f"❌ Error reproduciendo playlist:\n{html.escape(str(error))}",
                playlists_menu(user_id),
            )
        return

    if data in {"music_refresh", "music_pause", "music_next", "music_prev", "music_stop"}:
        if data == "music_pause":
            await asyncio.to_thread(pause_music)
        elif data == "music_next":
            await asyncio.to_thread(next_music)
        elif data == "music_prev":
            await asyncio.to_thread(prev_music)
        elif data == "music_stop":
            await asyncio.to_thread(stop_music)
        await asyncio.sleep(0.25)
        texto = await asyncio.to_thread(music_status_text)
        await mostrar_panel(update, context, texto, music_menu())
        return

    # RED.
    if data == "network":
        await mostrar_panel(update, context, "🌐 Consultando red…", network_menu())
        texto = await asyncio.to_thread(network_status_text)
        await mostrar_panel(update, context, texto, network_menu())
        return

    if data == "network_interfaces":
        texto = await asyncio.to_thread(network_interfaces_text)
        await mostrar_panel(update, context, texto, network_menu())
        return

    # GPU.
    if data == "gpu":
        await mostrar_panel(update, context, "🎮 Consultando GPU…", gpu_menu())
        texto = await asyncio.to_thread(gpu_advanced_text)
        await mostrar_panel(update, context, texto, gpu_menu())
        return

    if data == "gpu_processes":
        texto = await asyncio.to_thread(gpu_processes_text)
        await mostrar_panel(update, context, texto, gpu_menu())
        return

    # VENTANAS ABIERTAS.
    if data == "windows":
        ventanas = await asyncio.to_thread(listar_ventanas)
        context.user_data[WINDOW_CACHE_KEY] = ventanas
        context.user_data[WINDOW_PAGE_KEY] = 0
        await mostrar_panel(
            update, context,
            f"🪟 <b>VENTANAS ABIERTAS</b>\n\nDetectadas: <b>{len(ventanas)}</b>",
            windows_menu(ventanas, 0),
        )
        return

    if data.startswith("windows_page::"):
        page = int(data.split("::", 1)[1])
        ventanas = context.user_data.get(WINDOW_CACHE_KEY, [])
        context.user_data[WINDOW_PAGE_KEY] = page
        await mostrar_panel(update, context, "🪟 <b>VENTANAS ABIERTAS</b>", windows_menu(ventanas, page))
        return

    if data.startswith("window::"):
        idx = int(data.split("::", 1)[1])
        ventanas = context.user_data.get(WINDOW_CACHE_KEY, [])
        try:
            item = ventanas[idx]
        except IndexError:
            await mostrar_panel(update, context, "ℹ️ La lista de ventanas cambió.", windows_menu(ventanas, 0))
            return
        await mostrar_panel(
            update, context,
            f"🪟 <b>VENTANA</b>\n\n{html.escape(item['title'])}",
            window_detail_menu(int(item["hwnd"])),
        )
        return

    if data.startswith("window_action::"):
        _, hwnd, action = data.split("::", 2)
        ok, detail = await asyncio.to_thread(window_action, int(hwnd), action)
        await mostrar_panel(update, context, ("✅ " if ok else "⚠️ ") + html.escape(detail), window_detail_menu(int(hwnd)))
        return

    if data.startswith("window_close::"):
        hwnd = int(data.split("::", 1)[1])
        await mostrar_panel(
            update, context,
            "⚠️ <b>CERRAR VENTANA</b>\n\nEsto puede hacer que la aplicación pregunte si desea guardar cambios.",
            InlineKeyboardMarkup([
                [boton("❌ Sí, cerrar ventana", f"window_close_confirm::{hwnd}", "danger")],
                [boton("Cancelar", "windows")],
            ]),
        )
        return

    if data.startswith("window_close_confirm::"):
        hwnd = int(data.split("::", 1)[1])
        ok, detail = await asyncio.to_thread(window_action, hwnd, "close")
        await mostrar_panel(update, context, ("✅ " if ok else "⚠️ ") + html.escape(detail), windows_menu(await asyncio.to_thread(listar_ventanas), 0))
        return

    # ARCHIVOS.
    if data == "files":
        limpiar_estado_entrada(context)
        await mostrar_panel(
            update, context,
            "📁 <b>ARCHIVOS</b>\n\nSolo se muestran carpetas autorizadas:",
            file_roots_menu(),
        )
        return

    if data.startswith("files_root::"):
        idx = int(data.split("::", 1)[1])
        try:
            _, path = FILE_ROOTS[idx]
        except IndexError:
            return
        entries = await asyncio.to_thread(listar_archivos, path)
        context.user_data[FILE_CURRENT_DIR_KEY] = str(path)
        context.user_data[FILE_CACHE_KEY] = entries
        context.user_data[FILE_PAGE_KEY] = 0
        await mostrar_panel(update, context, texto_ruta_archivos(path), file_browser_menu(entries, path, 0))
        return

    if data.startswith("files_page::"):
        page = int(data.split("::", 1)[1])
        path = Path(context.user_data.get(FILE_CURRENT_DIR_KEY, ""))
        entries = context.user_data.get(FILE_CACHE_KEY, [])
        context.user_data[FILE_PAGE_KEY] = page
        await mostrar_panel(update, context, texto_ruta_archivos(path), file_browser_menu(entries, path, page))
        return

    if data == "file_up":
        path = Path(context.user_data.get(FILE_CURRENT_DIR_KEY, ""))
        root = root_para_ruta(path)
        if not root:
            await mostrar_panel(update, context, "❌ Ruta no autorizada.", file_roots_menu())
            return
        parent = path.parent
        if not ruta_autorizada(parent):
            parent = root[1]
        entries = await asyncio.to_thread(listar_archivos, parent)
        context.user_data[FILE_CURRENT_DIR_KEY] = str(parent)
        context.user_data[FILE_CACHE_KEY] = entries
        context.user_data[FILE_PAGE_KEY] = 0
        await mostrar_panel(update, context, texto_ruta_archivos(parent), file_browser_menu(entries, parent, 0))
        return

    if data.startswith("file_item::"):
        idx = int(data.split("::", 1)[1])
        entries = context.user_data.get(FILE_CACHE_KEY, [])
        try:
            item = entries[idx]
        except IndexError:
            await mostrar_panel(update, context, "ℹ️ La carpeta cambió. Vuelve a cargarla.", file_roots_menu())
            return
        path = Path(item["path"])
        if item["dir"]:
            new_entries = await asyncio.to_thread(listar_archivos, path)
            context.user_data[FILE_CURRENT_DIR_KEY] = str(path)
            context.user_data[FILE_CACHE_KEY] = new_entries
            context.user_data[FILE_PAGE_KEY] = 0
            await mostrar_panel(update, context, texto_ruta_archivos(path), file_browser_menu(new_entries, path, 0))
        else:
            context.user_data[FILE_SELECTED_KEY] = idx
            await mostrar_panel(
                update, context,
                f"📄 <b>{html.escape(item['name'])}</b>\n\nTamaño: {formato_bytes(item['size'])}",
                file_detail_menu(idx),
            )
        return

    if data == "file_return":
        context.user_data.pop(STATE_KEY, None)
        path = Path(context.user_data.get(FILE_CURRENT_DIR_KEY, ""))
        entries = context.user_data.get(FILE_CACHE_KEY, [])
        page = int(context.user_data.get(FILE_PAGE_KEY, 0) or 0)
        await mostrar_panel(update, context, texto_ruta_archivos(path), file_browser_menu(entries, path, page))
        return

    if data.startswith("file_send::"):
        idx = int(data.split("::", 1)[1])
        entries = context.user_data.get(FILE_CACHE_KEY, [])
        try:
            item = entries[idx]
            path = Path(item["path"])
        except Exception:
            return
        if not ruta_autorizada(path) or not path.is_file():
            await mostrar_panel(update, context, "❌ Archivo no disponible.", file_roots_menu())
            return
        size_mb = path.stat().st_size / 1024 / 1024
        if size_mb > MAX_SEND_FILE_MB:
            await mostrar_panel(update, context, f"⚠️ Archivo demasiado grande ({size_mb:.1f} MB). Límite configurado: {MAX_SEND_FILE_MB} MB.", file_detail_menu(idx))
            return
        await eliminar_panel_actual(update, context)
        try:
            with open(path, "rb") as f:
                await context.bot.send_document(chat_id=update.effective_chat.id, document=f, filename=path.name)
            await mostrar_panel(update, context, "✅ Archivo enviado.", file_detail_menu(idx), nuevo=True)
        except Exception as error:
            await mostrar_panel(update, context, f"❌ No se pudo enviar.\n\n{html.escape(str(error))}", file_detail_menu(idx), nuevo=True)
        return

    if data.startswith("file_delete::"):
        idx = int(data.split("::", 1)[1])
        entries = context.user_data.get(FILE_CACHE_KEY, [])
        try:
            item = entries[idx]
        except IndexError:
            return
        await mostrar_panel(
            update, context,
            f"⚠️ <b>ELIMINAR ARCHIVO</b>\n\n{html.escape(item['name'])}\n\nEsta acción no envía el archivo a la Papelera.",
            InlineKeyboardMarkup([
                [boton("🗑 Sí, eliminar", f"file_delete_confirm::{idx}", "danger")],
                [boton("Cancelar", f"file_item::{idx}")],
            ]),
        )
        return

    if data.startswith("file_delete_confirm::"):
        idx = int(data.split("::", 1)[1])
        entries = context.user_data.get(FILE_CACHE_KEY, [])
        try:
            item = entries[idx]
            path = Path(item["path"])
            if item["dir"] or not ruta_autorizada(path):
                raise PermissionError("Solo se pueden eliminar archivos autorizados")
            await asyncio.to_thread(path.unlink)
            current = Path(context.user_data.get(FILE_CURRENT_DIR_KEY, ""))
            entries = await asyncio.to_thread(listar_archivos, current)
            context.user_data[FILE_CACHE_KEY] = entries
            await mostrar_panel(update, context, "✅ Archivo eliminado.", file_browser_menu(entries, current, 0))
        except Exception as error:
            await mostrar_panel(update, context, f"❌ No se pudo eliminar.\n\n{html.escape(str(error))}", file_roots_menu())
        return

    if data == "file_upload":
        path = Path(context.user_data.get(FILE_CURRENT_DIR_KEY, ""))
        if not ruta_autorizada(path):
            return
        context.user_data[STATE_KEY] = STATE_FILE_UPLOAD
        await mostrar_panel(
            update, context,
            f"📤 <b>SUBIR ARCHIVO</b>\n\nEnvía un documento de Telegram y se guardará en:\n<code>{html.escape(str(path))}</code>",
            InlineKeyboardMarkup([[boton("❌ Cancelar", "file_return")]]),
        )
        return

    # MACROS.
    if data == "macros":
        macros = await asyncio.to_thread(cargar_macros)
        context.user_data["macros_cache"] = macros
        await mostrar_panel(update, context, "⭐ <b>MACROS</b>\n\nSelecciona una rutina:", macros_menu(macros))
        return

    if data.startswith("macro::"):
        idx = int(data.split("::", 1)[1])
        macros = context.user_data.get("macros_cache") or await asyncio.to_thread(cargar_macros)
        try:
            macro = macros[idx]
        except IndexError:
            await mostrar_panel(update, context, "ℹ️ macros.json cambió. Recarga la lista.", macros_menu(macros))
            return
        await mostrar_panel(update, context, f"⭐ Ejecutando {html.escape(str(macro.get('name', 'macro')))}…", macros_menu(macros))
        results = await asyncio.to_thread(ejecutar_macro, macro)
        await mostrar_panel(update, context, "⭐ <b>MACRO COMPLETADA</b>\n\n" + "\n".join(html.escape(x) for x in results), macros_menu(macros))
        return

    # CONTROL RÁPIDO.
    if data == "quick":
        await mostrar_panel(update, context, "⌨ <b>CONTROL RÁPIDO</b>\n\nEnvía teclas multimedia y de navegación al PC.", quick_menu())
        return

    if data.startswith("quick::"):
        action = data.split("::", 1)[1]
        try:
            await asyncio.to_thread(quick_key, action)
            texto = "✅ Acción enviada al PC."
        except Exception as error:
            texto = f"❌ No se pudo enviar la tecla.\n\n{html.escape(str(error))}"
        await mostrar_panel(update, context, texto, quick_menu())
        return

    # NOTIFICACIÓN.
    if data == "notify":
        context.user_data[STATE_KEY] = STATE_NOTIFICATION
        await mostrar_panel(
            update, context,
            "🔔 <b>NOTIFICACIÓN DE WINDOWS</b>\n\nEscribe el texto que quieres mostrar en el PC.",
            InlineKeyboardMarkup([[boton("❌ Cancelar", "back")]]),
        )
        return

    # ADMIN.
    if data == "admin":
        estado_admin = "🟢 Activo" if admin_activo(context) else "🔒 Inactivo"
        texto = f"🔐 <b>MODO ADMINISTRADOR</b>\n\nEstado: {estado_admin}"
        if admin_activo(context):
            texto += f"\nTiempo restante: {formato_duracion(admin_restante(context))}"
        texto += "\n\nProtege CMD, cierre de procesos, eliminación de archivos y acciones críticas."
        await mostrar_panel(update, context, texto, admin_menu(context))
        return

    if data == "admin_activate":
        if ADMIN_PIN:
            context.user_data[STATE_KEY] = STATE_ADMIN_PIN
            await mostrar_panel(update, context, "🔐 Escribe el PIN de administrador:", InlineKeyboardMarkup([[boton("❌ Cancelar", "admin")]]))
        else:
            await mostrar_panel(update, context, "⚠️ <b>ACTIVAR MODO ADMINISTRADOR</b>\n\nQuedará habilitado durante 10 minutos.", admin_confirm_menu())
        return

    if data == "admin_confirm":
        context.user_data[ADMIN_UNTIL_KEY] = time.time() + ADMIN_SESSION_MINUTES * 60
        await mostrar_panel(update, context, "🔓 <b>Modo administrador activo</b> durante 10 minutos.", admin_menu(context))
        return

    if data == "admin_disable":
        context.user_data.pop(ADMIN_UNTIL_KEY, None)
        await mostrar_panel(update, context, "🔒 Modo administrador desactivado.", admin_menu(context))
        return

    # ESTADO / REINICIO DEL BOT.
    if data == "bot_status":
        await mostrar_panel(update, context, bot_status_text(context), bot_info_menu())
        return

    if data == "bot_restart":
        await mostrar_panel(
            update, context,
            "⚠️ <b>REINICIAR PCCONTROL</b>\n\nEl proceso Python se reemplazará por una nueva instancia del mismo bot.",
            InlineKeyboardMarkup([
                [boton("🔄 Sí, reiniciar bot", "bot_restart_confirm", "danger")],
                [boton("❌ Cancelar", "bot_status")],
            ]),
        )
        return

    if data == "bot_restart_confirm":
        await mostrar_panel(update, context, "🔄 Reiniciando PCControl…", None)
        await asyncio.sleep(0.8)
        reiniciar_bot_proceso()
        return

    # VOLUMEN.
    if data == "volume_menu":
        await mostrar_panel(
            update,
            context,
            "🔊 <b>CONTROL DE VOLUMEN</b>\n\nSelecciona el nivel:",
            volume_menu(),
        )
        return

    if data.startswith("volume::"):
        try:
            volume = int(data.split("::", 1)[1])
            success, detail = await asyncio.to_thread(set_volume, volume)
            if success:
                icon = "🔇" if volume == 0 else "🔊"
                texto = f"{icon} Volumen ajustado al <b>{volume}%</b>."
            else:
                texto = f"❌ No se pudo ajustar el volumen.\n\n{html.escape(detail)}"
        except Exception as error:
            texto = f"❌ Error ajustando volumen:\n{html.escape(str(error))}"
        await mostrar_panel(update, context, texto, volume_menu())
        return

    # AUDIO.
    if data in {"audio_menu", "audio_refresh"}:
        await mostrar_panel(update, context, "🔊 Detectando salidas de audio…", menu_principal())
        devices, error = await asyncio.to_thread(get_audio_output_devices)
        audio_devices_cache[user_id] = devices
        if not devices:
            await mostrar_panel(
                update,
                context,
                f"❌ No se pudieron detectar salidas de audio.\n\n{html.escape(error)}",
                menu_principal(),
            )
        else:
            listado = "\n".join(
                f"{'✅' if d.get('default') else '🔊'} {html.escape(d['display_name'])}"
                for d in devices
            )
            await mostrar_panel(
                update,
                context,
                f"🔊 <b>SALIDAS DE AUDIO</b>\n\n{listado}\n\nSelecciona una:",
                audio_devices_menu(devices),
            )
        return

    if data.startswith("audio_device::"):
        try:
            indice = int(data.split("::", 1)[1])
            devices = audio_devices_cache.get(user_id, [])
            selected = devices[indice]
            success, detail = await asyncio.to_thread(
                set_default_audio_device,
                selected["command_id"],
            )
            if success:
                for device in devices:
                    device["default"] = device["command_id"] == selected["command_id"]
                texto = f"✅ Salida cambiada a:\n🔊 <b>{html.escape(selected['display_name'])}</b>"
            else:
                texto = f"❌ No se pudo cambiar la salida.\n\n{html.escape(detail)}"
            await mostrar_panel(update, context, texto, audio_devices_menu(devices))
        except Exception as error:
            await mostrar_panel(
                update,
                context,
                f"❌ Selección de audio inválida.\n\n{html.escape(str(error))}",
                menu_principal(),
            )
        return

    # MONITORES.
    if data == "monitors":
        await mostrar_panel(
            update,
            context,
            "🖥 <b>CONFIGURACIÓN DE MONITORES</b>\n\nSelecciona el modo:",
            monitor_menu(),
        )
        return

    monitor_modes = {
        "monitor_1": ("internal", "🖥 Usando solo Monitor 1."),
        "monitor_2": ("external", "🖥 Usando solo Monitor 2."),
        "monitor_clone": ("clone", "📺 Monitores duplicados."),
        "monitor_extend": ("extend", "↔ Modo extendido activado."),
    }
    if data in monitor_modes:
        modo, ok_text = monitor_modes[data]
        ok = await asyncio.to_thread(switch_display, modo)
        await mostrar_panel(
            update,
            context,
            ok_text if ok else "❌ No se pudo cambiar el modo de pantalla.",
            monitor_menu(),
        )
        return

    # CLIPBOARD.
    if data == "clipboard":
        limpiar_estado_entrada(context)
        await mostrar_panel(
            update,
            context,
            "📋 <b>CLIPBOARD</b>\n\nSelecciona una acción:",
            clipboard_menu(),
        )
        return

    if data == "clipboard_view":
        try:
            contenido = await asyncio.to_thread(pyperclip.paste)
            contenido = str(contenido)
            if len(contenido) > 2500:
                contenido = contenido[:2500] + "…"
            texto = f"📥 <b>Clipboard actual</b>\n\n<pre>{html.escape(contenido or '(vacío)')}</pre>"
        except Exception as error:
            texto = f"❌ No se pudo leer el clipboard.\n\n{html.escape(str(error))}"
        await mostrar_panel(update, context, texto, clipboard_menu())
        return

    if data == "clipboard_copy":
        context.user_data[STATE_KEY] = STATE_CLIPBOARD_COPY
        await mostrar_panel(
            update,
            context,
            "📤 <b>Copiar texto al PC</b>\n\nEscribe el texto que quieres dejar en el clipboard de Windows.",
            InlineKeyboardMarkup([[boton("❌ Cancelar", "clipboard")]]),
        )
        return

    if data in {"clipboard_paste", "clipboard_paste_enter"}:
        try:
            await asyncio.to_thread(pegar_clipboard, data == "clipboard_paste_enter")
            texto = "✅ Clipboard pegado."
            if data == "clipboard_paste_enter":
                texto += " También se presionó Enter."
        except Exception as error:
            texto = f"❌ Error pegando clipboard.\n\n{html.escape(str(error))}"
        await mostrar_panel(update, context, texto, clipboard_menu())
        return

    # PROCESOS.
    if data == "processes":
        await mostrar_panel(
            update,
            context,
            "⚙️ <b>PROCESOS</b>\n\n¿Cómo quieres ordenarlos?",
            processes_menu(),
        )
        return

    if data.startswith("processes::"):
        orden = data.split("::", 1)[1]
        if orden not in {"cpu", "ram"}:
            return
        await mostrar_panel(update, context, "⚙️ Analizando procesos…", processes_menu())
        procesos = await asyncio.to_thread(top_procesos, orden, 8)
        context.user_data[PROCESS_RETURN_KEY] = orden
        lineas = []
        for i, proc in enumerate(procesos, 1):
            dato = f"{proc['cpu']:.1f}% CPU" if orden == "cpu" else f"{formato_bytes(proc['ram'])} RAM"
            lineas.append(f"{i}. {html.escape(proc['name'])} · {dato}")
        titulo = "🔥 CPU" if orden == "cpu" else "🧠 RAM"
        await mostrar_panel(
            update,
            context,
            f"⚙️ <b>TOP PROCESOS — {titulo}</b>\n\n" + "\n".join(lineas),
            process_list_menu(procesos, orden),
        )
        return

    if data.startswith("process::"):
        try:
            pid = int(data.split("::", 1)[1])
            detalle = await asyncio.to_thread(detalle_proceso, pid)
            texto = (
                f"⚙️ <b>{html.escape(detalle['nombre'])}</b>\n\n"
                f"PID: <code>{pid}</code>\n"
                f"Estado: {html.escape(detalle['estado'])}\n"
                f"RAM: {formato_bytes(detalle['ram'])}\n"
                f"Usuario: {html.escape(detalle['usuario'])}\n"
                f"Iniciado: {detalle['creado']}\n\n"
                f"Ruta:\n<code>{html.escape(detalle['exe'])}</code>"
            )
            await mostrar_panel(update, context, texto, process_detail_menu(pid))
        except (psutil.NoSuchProcess, ProcessLookupError):
            await mostrar_panel(update, context, "ℹ️ El proceso ya terminó.", processes_menu())
        except Exception as error:
            await mostrar_panel(
                update,
                context,
                f"❌ No se pudo consultar el proceso.\n\n{html.escape(str(error))}",
                processes_menu(),
            )
        return

    if data.startswith("process_terminate::"):
        pid = int(data.split("::", 1)[1])
        try:
            detalle = await asyncio.to_thread(detalle_proceso, pid)
            texto = (
                "⚠️ <b>TERMINAR PROCESO</b>\n\n"
                f"{html.escape(detalle['nombre'])}\nPID: {pid}\n\n"
                "¿Seguro que quieres cerrarlo?"
            )
            await mostrar_panel(update, context, texto, process_confirm_menu(pid))
        except Exception:
            await mostrar_panel(update, context, "ℹ️ El proceso ya no está activo.", processes_menu())
        return

    if data.startswith("process_confirm::"):
        pid = int(data.split("::", 1)[1])
        try:
            ok, detalle = await asyncio.to_thread(terminar_proceso, pid)
            texto = ("✅ " if ok else "⚠️ ") + html.escape(detalle)
        except Exception as error:
            texto = f"❌ No se pudo terminar el proceso.\n\n{html.escape(str(error))}"
        await mostrar_panel(update, context, texto, processes_menu())
        return

    # CMD.
    if data == "cmd":
        limpiar_estado_entrada(context)
        await mostrar_panel(update, context, "⌨ <b>CMD</b>\n\nSelecciona una opción:", cmd_menu())
        return

    if data == "cmd_new":
        context.user_data[STATE_KEY] = STATE_CMD
        await mostrar_panel(
            update,
            context,
            "⌨ <b>NUEVO COMANDO</b>\n\nEscribe el comando CMD que quieres preparar.",
            InlineKeyboardMarkup([[boton("❌ Cancelar", "cmd")]]),
        )
        return

    if data == "cmd_confirm":
        comando = context.user_data.pop(PENDING_CMD_KEY, None)
        if not comando:
            await mostrar_panel(update, context, "ℹ️ No hay un comando pendiente.", cmd_menu())
            return
        await mostrar_panel(update, context, "⌨ Ejecutando comando…", cmd_menu())
        ok, resultado = await asyncio.to_thread(ejecutar_cmd, comando)
        if len(resultado) > 3000:
            resultado = resultado[:3000] + "…"
        icono = "✅" if ok else "❌"
        texto = (
            f"{icono} <b>CMD RESULTADO</b>\n\n"
            f"Comando:\n<code>{html.escape(comando)}</code>\n\n"
            f"Salida:\n<pre>{html.escape(resultado)}</pre>"
        )
        await mostrar_panel(update, context, texto, cmd_menu())
        return

    if data == "cmd_history":
        registros = leer_historial_cmd(10)
        if not registros:
            texto = "📜 <b>HISTORIAL CMD</b>\n\nTodavía no hay comandos registrados."
        else:
            lineas = ["📜 <b>ÚLTIMOS COMANDOS CMD</b>", ""]
            for reg in reversed(registros):
                icono = "✅" if reg.get("ok") else "❌"
                comando = html.escape(str(reg.get("command", "")))
                fecha = html.escape(str(reg.get("timestamp", "")))
                if len(comando) > 100:
                    comando = comando[:97] + "..."
                lineas.append(f"{icono} <code>{comando}</code>\n   {fecha}")
            texto = "\n".join(lineas)
        await mostrar_panel(update, context, texto, cmd_menu())
        return

    # ENERGÍA.
    if data == "power":
        await mostrar_panel(
            update,
            context,
            "⚡ <b>ENERGÍA</b>\n\nSelecciona una acción:",
            power_menu(),
        )
        return

    if data == "power_lock":
        try:
            await asyncio.to_thread(lock_pc)
            texto = "🔒 PC bloqueada."
        except Exception as error:
            texto = f"❌ No se pudo bloquear el PC.\n\n{html.escape(str(error))}"
        await mostrar_panel(update, context, texto, power_menu())
        return

    power_request = {
        "power_suspend": ("suspend", "🌙 Suspender el PC"),
        "power_restart": ("restart", "🔄 Reiniciar el PC"),
        "power_shutdown": ("shutdown", "🔌 Apagar el PC"),
    }
    if data in power_request:
        action, descripcion = power_request[data]
        await mostrar_panel(
            update,
            context,
            f"⚠️ <b>CONFIRMACIÓN</b>\n\n{descripcion}\n\n¿Seguro?",
            confirm_power_menu(action),
        )
        return

    if data.startswith("power_confirm::"):
        action = data.split("::", 1)[1]
        await mostrar_panel(update, context, "⚡ Orden enviada al PC…", None)
        try:
            if action == "suspend":
                await asyncio.to_thread(suspend_pc)
            elif action == "restart":
                try:
                    await context.bot.send_message(chat_id=update.effective_chat.id, text="🟠 PC REINICIANDO · el bot quedará offline unos instantes.")
                except TelegramError:
                    pass
                await asyncio.to_thread(restart_pc)
            elif action == "shutdown":
                try:
                    await context.bot.send_message(chat_id=update.effective_chat.id, text="🔴 PC APAGÁNDOSE · el bot quedará offline.")
                except TelegramError:
                    pass
                await asyncio.to_thread(shutdown_pc)
            else:
                return
        except Exception as error:
            logger.exception("Error ejecutando acción de energía: %s", action)
            await mostrar_panel(
                update,
                context,
                f"❌ Windows rechazó la orden.\n\n{html.escape(str(error))}",
                power_menu(),
            )
        return

    # Pantalla OFF/ON disponibles como callbacks internos por compatibilidad.
    if data == "off":
        await asyncio.to_thread(screen_off)
        await mostrar_panel(update, context, "📴 Pantalla apagada.", menu_principal())
        return
    if data == "on":
        await asyncio.to_thread(screen_on)
        await mostrar_panel(update, context, "🔆 Pantalla encendida.", menu_principal())
        return


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return

    mensaje = update.effective_message
    chat = update.effective_chat
    if mensaje is None or chat is None:
        return

    estado = context.user_data.get(STATE_KEY)
    if not estado:
        return

    texto = mensaje.text or ""
    await borrar_mensaje_seguro(context.bot, chat.id, mensaje.message_id)

    if estado == STATE_FILE_UPLOAD:
        await mostrar_panel(
            update, context,
            "📤 Esperando un <b>documento/archivo</b> de Telegram, no texto.\n\nUsa el icono de adjuntar y envíalo como archivo.",
            InlineKeyboardMarkup([[boton("❌ Cancelar", "file_return")]]),
        )
        return

    if estado == STATE_NOTIFICATION:
        context.user_data.pop(STATE_KEY, None)
        ok, detail = await asyncio.to_thread(windows_notification, texto)
        await mostrar_panel(
            update, context,
            ("✅ " if ok else "❌ ") + html.escape(detail),
            menu_principal(),
        )
        return

    if estado == STATE_ADMIN_PIN:
        context.user_data.pop(STATE_KEY, None)
        if texto.strip() == ADMIN_PIN:
            context.user_data[ADMIN_UNTIL_KEY] = time.time() + ADMIN_SESSION_MINUTES * 60
            respuesta = "🔓 Modo administrador activo durante 10 minutos."
        else:
            respuesta = "❌ PIN incorrecto."
        await mostrar_panel(update, context, respuesta, admin_menu(context))
        return

    if estado == STATE_CLIPBOARD_COPY:
        context.user_data.pop(STATE_KEY, None)
        try:
            await asyncio.to_thread(pyperclip.copy, texto)
            visible = texto if len(texto) <= 300 else texto[:300] + "…"
            respuesta = (
                "✅ Texto copiado al clipboard de Windows.\n\n"
                f"<pre>{html.escape(visible)}</pre>"
            )
        except Exception as error:
            respuesta = f"❌ Error copiando al clipboard.\n\n{html.escape(str(error))}"
        await mostrar_panel(update, context, respuesta, clipboard_menu())
        return

    if estado == STATE_CMD:
        context.user_data.pop(STATE_KEY, None)
        comando = texto.strip()
        if not comando:
            await mostrar_panel(update, context, "❌ El comando está vacío.", cmd_menu())
            return
        context.user_data[PENDING_CMD_KEY] = comando
        await mostrar_panel(
            update,
            context,
            "⚠️ <b>CONFIRMAR CMD</b>\n\n"
            f"Comando:\n<code>{html.escape(comando)}</code>\n\n"
            "El comando todavía NO se ejecutó.",
            cmd_confirm_menu(),
        )
        return


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    if context.user_data.get(STATE_KEY) != STATE_FILE_UPLOAD:
        return
    mensaje = update.effective_message
    if mensaje is None or mensaje.document is None:
        return

    target_dir = Path(context.user_data.get(FILE_CURRENT_DIR_KEY, ""))
    if not ruta_autorizada(target_dir):
        context.user_data.pop(STATE_KEY, None)
        return

    document = mensaje.document
    if document.file_size and document.file_size > MAX_RECEIVE_FILE_MB * 1024 * 1024:
        await mensaje.reply_text(f"⚠️ Archivo demasiado grande. Límite configurado: {MAX_RECEIVE_FILE_MB} MB.")
        return

    name = Path(document.file_name or f"telegram_{document.file_unique_id}").name
    target = target_dir / name
    if not ruta_autorizada(target):
        await mensaje.reply_text("❌ Nombre de archivo no permitido.")
        return

    if target.exists():
        stem = target.stem
        suffix = target.suffix
        counter = 2
        while target.exists():
            target = target_dir / f"{stem}_{counter}{suffix}"
            counter += 1

    try:
        tg_file = await context.bot.get_file(document.file_id)
        await tg_file.download_to_drive(custom_path=str(target))
        context.user_data.pop(STATE_KEY, None)
        entries = await asyncio.to_thread(listar_archivos, target_dir)
        context.user_data[FILE_CACHE_KEY] = entries
        await borrar_mensaje_seguro(context.bot, update.effective_chat.id, mensaje.message_id)
        await mostrar_panel(
            update, context,
            f"✅ Archivo guardado en:\n<code>{html.escape(str(target))}</code>",
            file_browser_menu(entries, target_dir, 0),
        )
    except Exception as error:
        logger.exception("Error subiendo archivo al PC")
        await mensaje.reply_text(f"❌ No se pudo guardar el archivo: {error}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    if error is not None:
        logger.error(
            "Error no controlado durante una actualización",
            exc_info=(type(error), error, error.__traceback__),
        )
    else:
        logger.error("Error no controlado durante una actualización")

    # Evita generar bucles de errores intentando contestar siempre.
    try:
        if isinstance(update, Update) and autorizado(update):
            await mostrar_panel(
                update,
                context,
                "⚠️ Ocurrió un error interno. Quedó registrado en <code>pc_control.log</code>.",
                menu_principal(),
                nuevo=True,
            )
    except Exception:
        logger.exception("No se pudo informar el error por Telegram")


async def post_stop(application: Application):
    logger.info("Bot detenido de forma controlada")
    if SEND_ONLINE_NOTIFICATION:
        try:
            await application.bot.send_message(
                chat_id=ALLOWED_USER_ID,
                text="🔴 <b>PCControl OFFLINE</b>\n\nEl bot se detuvo de forma controlada.",
                parse_mode="HTML",
            )
        except TelegramError:
            pass


async def post_init(application: Application):
    logger.info("Bot iniciado")

    try:
        crear_macros_por_defecto()
    except Exception:
        logger.exception("No se pudo crear macros.json")

    # El menú principal aparece automáticamente cada vez que
    # PCControl inicia o se reinicia. No hace falta escribir /menu.
    try:
        estado = await asyncio.to_thread(get_status)

        if SEND_ONLINE_NOTIFICATION:
            texto_inicio = (
                "🟢 <b>PC ONLINE</b>\n\n"
                f"{estado}\n\n"
                "Selecciona una función:"
            )
        else:
            texto_inicio = (
                f"{estado}\n\n"
                "Selecciona una función:"
            )

        await application.bot.send_message(
            chat_id=ALLOWED_USER_ID,
            text=texto_inicio,
            parse_mode="HTML",
            reply_markup=menu_principal(),
        )

    except TelegramError:
        logger.exception(
            "No se pudo enviar el menú automático de inicio"
        )
    except Exception:
        logger.exception(
            "Error preparando el menú automático de inicio"
        )


# =========================================================
# RUN
# =========================================================

def main():
    if not TOKEN:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en el archivo .env")
    if ALLOWED_USER_ID <= 0:
        raise RuntimeError("Falta o es inválido ALLOWED_USER_ID en el archivo .env")

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", start))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Iniciando polling")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()

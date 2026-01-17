import os
import re
import asyncio
import traceback
import time
import json
from datetime import datetime, timezone, timedelta
from urllib.parse import unquote
from concurrent.futures import TimeoutError as FutureTimeoutError 
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto
from telethon.errors.rpcerrorlist import UserBlockedError

# --- Configuración y Variables de Entorno ---
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://consulta-pe-bot.up.railway.app").rstrip("/")
SESSION_STRING = os.getenv("SESSION_STRING", None)
PORT = int(os.getenv("PORT", 8080))

# --- Configuración Interna ---
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

LEDERDATA_BOT_ID = "@LEDERDATA_OFC_BOT" 
LEDERDATA_BACKUP_BOT_ID = "@lederdata_publico_bot"
ALL_BOT_IDS = [LEDERDATA_BOT_ID, LEDERDATA_BACKUP_BOT_ID]

TIMEOUT_FAILOVER = 15 
TIMEOUT_TOTAL = 50 

# --- Trackeo de Fallos de Bots ---
bot_fail_tracker = {}
BOT_FAIL_TIMEOUT_HOURS = 6 

def is_bot_blocked(bot_id: str) -> bool:
    """Verifica si el bot está temporalmente bloqueado por fallos previos."""
    last_fail_time = bot_fail_tracker.get(bot_id)
    if not last_fail_time:
        return False

    now = datetime.now()
    six_hours_ago = now - timedelta(hours=BOT_FAIL_TIMEOUT_HOURS)

    if last_fail_time > six_hours_ago:
        return True
    
    print(f"✅ Bot {bot_id} ha cumplido su tiempo de bloqueo. Desbloqueado.")
    bot_fail_tracker.pop(bot_id, None)
    return False

def record_bot_failure(bot_id: str):
    """Registra la hora actual como la última hora de fallo del bot."""
    print(f"🚨 Bot {bot_id} ha fallado y será BLOQUEADO por {BOT_FAIL_TIMEOUT_HOURS} horas.")
    bot_fail_tracker[bot_id] = datetime.now()

# --- Aplicación Flask ---
app = Flask(__name__)
CORS(app)

# --- Cache para almacenar temporalmente las respuestas ---
response_cache = {}
_cache_lock = asyncio.Lock()

# --- Lógica de Limpieza y Extracción de Datos ---
def clean_and_extract(raw_text: str):
    if not raw_text:
        return {"text": "", "fields": {}}

    text = raw_text
    
    # 1. Reemplazar la marca LEDER_BOT por CONSULTA PE
    text = re.sub(r"^\[\#LEDER\_BOT\]", "[CONSULTA PE]", text, flags=re.IGNORECASE | re.DOTALL)
    
    # 2. Eliminar cabecera
    header_pattern = r"^\[.*?\]\s*→\s*.*?\[.*?\](\r?\n){1,2}"
    text = re.sub(header_pattern, "", text, flags=re.IGNORECASE | re.DOTALL)
    
    # 3. Eliminar pie de página
    footer_pattern = r"((\r?\n){1,2}\[|Página\s*\d+\/\d+.*|(\r?\n){1,2}Por favor, usa el formato correcto.*|↞ Anterior|Siguiente ↠.*|Credits\s*:.+|Wanted for\s*:.+|\s*@lederdata.*|(\r?\n){1,2}\s*Marca\s*@lederdata.*|(\r?\n){1,2}\s*Créditos\s*:\s*\d+)"
    text = re.sub(footer_pattern, "", text, flags=re.IGNORECASE | re.DOTALL)
    
    # 4. Limpiar separador
    text = re.sub(r"\-{3,}", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = text.strip()

    # 5. Extraer datos clave
    fields = {}
    dni_match = re.search(r"DNI\s*:\s*(\d{8})", text, re.IGNORECASE)
    if dni_match: fields["dni"] = dni_match.group(1)
    
    ruc_match = re.search(r"RUC\s*:\s*(\d{11})", text, re.IGNORECASE)
    if ruc_match: fields["ruc"] = ruc_match.group(1)

    photo_type_match = re.search(r"Foto\s*:\s*(rostro|huella|firma|adverso|reverso).*", text, re.IGNORECASE)
    if photo_type_match: fields["photo_type"] = photo_type_match.group(1).lower()
    
    # 6. Manejo de mensajes de no encontrado
    not_found_pattern = r"\[⚠️\]\s*(no se encontro información|no se han encontrado resultados|no se encontró una|no hay resultados|no tenemos datos|no se encontraron registros)"
    if re.search(not_found_pattern, text, re.IGNORECASE | re.DOTALL):
         fields["not_found"] = True

    return {"text": text, "fields": fields}

# --- Función Principal para Conexión On-Demand ---
async def send_telegram_command(command: str):
    """
    Función on-demand que:
    1. Crea un nuevo cliente Telethon
    2. Se conecta
    3. Envía el comando
    4. Espera la respuesta
    5. Procesa el resultado
    6. Se desconecta
    7. Limpia los archivos descargados
    """
    client = None
    try:
        # 1. Crear el cliente
        if API_ID == 0 or not API_HASH:
            raise Exception("API_ID o API_HASH no configurados.")
        
        if not SESSION_STRING or not SESSION_STRING.strip():
            raise Exception("SESSION_STRING no configurada. Se requiere sesión válida.")
        
        session = StringSession(SESSION_STRING)
        client = TelegramClient(session, API_ID, API_HASH)
        
        # 2. Conectar
        await client.connect()
        
        if not await client.is_user_authorized():
            raise Exception("Cliente no autorizado. La sesión puede haber expirado.")
        
        # 3. Extraer DNI para tracking
        dni_match = re.search(r"/\w+\s+(\d{8})", command)
        dni = dni_match.group(1) if dni_match else None
        
        # 4. Determinar bots a usar
        bots_to_try = []
        for bot_id in ALL_BOT_IDS:
            if not is_bot_blocked(bot_id):
                bots_to_try.append(bot_id)
        
        if not bots_to_try:
            raise Exception("Todos los bots están temporalmente bloqueados.")
        
        # Variables para almacenar respuestas
        received_messages = []
        message_event = asyncio.Event()
        
        # 5. Handler temporal para capturar respuestas
        @client.on(events.NewMessage(incoming=True))
        async def temp_handler(event):
            try:
                # Verificar si el mensaje viene de uno de los bots
                sender_is_bot = False
                for bot_name in ALL_BOT_IDS:
                    try:
                        entity = await client.get_entity(bot_name)
                        if event.sender_id == entity.id:
                            sender_is_bot = True
                            break
                    except:
                        continue
                
                if not sender_is_bot:
                    return
                
                raw_text = event.raw_text or ""
                cleaned = clean_and_extract(raw_text)
                
                # Verificar match de DNI si existe
                if dni and cleaned["fields"].get("dni") != dni:
                    return  # Ignorar si el DNI no coincide
                
                msg_urls = []
                
                # Manejar archivos adjuntos
                if getattr(event, "message", None) and getattr(event.message, "media", None):
                    media_list = []
                    
                    if isinstance(event.message.media, (MessageMediaDocument, MessageMediaPhoto)):
                        media_list.append(event.message.media)
                    
                    if media_list:
                        timestamp_str = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
                        
                        for i, media in enumerate(media_list):
                            file_ext = '.file'
                            is_photo = False
                            
                            if hasattr(media, 'document') and hasattr(media.document, 'attributes'):
                                file_ext = os.path.splitext(getattr(media.document, 'file_name', 'file'))[1]
                            elif isinstance(media, MessageMediaPhoto) or (hasattr(media, 'photo') and media.photo):
                                file_ext = '.jpg'
                                is_photo = True
                                
                            dni_part = f"_{cleaned['fields'].get('dni')}" if cleaned["fields"].get("dni") else ""
                            type_part = f"_{cleaned['fields'].get('photo_type')}" if cleaned['fields'].get('photo_type') else ""
                            unique_filename = f"{timestamp_str}_{event.message.id}{dni_part}{type_part}_{i}{file_ext}"
                            
                            saved_path = await client.download_media(event.message, file=os.path.join(DOWNLOAD_DIR, unique_filename))
                            
                            url_obj = {
                                "url": f"{PUBLIC_URL}/files/{os.path.basename(saved_path)}", 
                                "type": cleaned['fields'].get('photo_type', 'image' if is_photo else 'document'),
                                "text_context": raw_text.split('\n')[0].strip()
                            }
                            msg_urls.append(url_obj)
                
                msg_obj = {
                    "chat_id": getattr(event, "chat_id", None),
                    "from_id": event.sender_id,
                    "date": event.message.date.isoformat() if getattr(event, "message", None) else datetime.utcnow().isoformat(),
                    "message": cleaned["text"],
                    "fields": cleaned["fields"],
                    "urls": msg_urls 
                }
                
                received_messages.append(msg_obj)
                
                # Detectar si es una respuesta final
                is_final = (
                    "Por favor, usa el formato correcto" in msg_obj["message"] or 
                    msg_obj["fields"].get("not_found", False) or
                    len(received_messages) >= 3  # Límite de mensajes por consulta
                )
                
                if is_final:
                    message_event.set()
                    
            except Exception as e:
                print(f"Error en handler temporal: {e}")
        
        # 6. Intentar con cada bot disponible
        final_result = None
        for attempt, current_bot_id in enumerate(bots_to_try, 1):
            print(f"📡 Enviando comando (Intento {attempt}) a {current_bot_id}: {command}")
            
            try:
                # Resetear variables para cada intento
                received_messages = []
                message_event.clear()
                
                # Enviar comando
                await client.send_message(current_bot_id, command)
                
                # Esperar respuesta con timeout
                try:
                    await asyncio.wait_for(message_event.wait(), timeout=TIMEOUT_FAILOVER if attempt == 1 else TIMEOUT_TOTAL)
                except asyncio.TimeoutError:
                    if attempt == 1 and len(bots_to_try) > 1:
                        print(f"⌛ Timeout de {current_bot_id}. Intentando con siguiente bot...")
                        continue
                    else:
                        raise Exception(f"Tiempo de espera agotado ({TIMEOUT_FAILOVER if attempt == 1 else TIMEOUT_TOTAL}s)")
                
                # Procesar respuestas recibidas
                if received_messages:
                    # Si recibimos error de formato
                    if "Por favor, usa el formato correcto" in received_messages[0]["message"]:
                        return {
                            "status": "error_bot_format", 
                            "message": "Formato de consulta incorrecto. " + received_messages[0]["message"],
                            "bot_used": current_bot_id
                        }
                    
                    # Si es "no encontrado"
                    if received_messages[0]["fields"].get("not_found", False):
                        return {
                            "status": "error_not_found", 
                            "message": "No se encontraron resultados para dicha consulta. Intenta con otro dato.",
                            "bot_used": current_bot_id
                        }
                    
                    # Consolidar múltiples mensajes
                    final_msg = received_messages[0].copy()
                    if len(received_messages) > 1:
                        final_msg["message"] = "\n---\n".join([msg["message"] for msg in received_messages])
                        
                        consolidated_urls = {}
                        type_map = {"rostro": "ROSTRO", "huella": "HUELLA", "firma": "FIRMA", 
                                   "adverso": "ADVERSO", "reverso": "REVERSO"}
                        
                        for msg in received_messages:
                            for url_obj in msg.get("urls", []):
                                key_type = url_obj["type"].lower()
                                key = type_map.get(key_type)
                                
                                if key:
                                    if key not in consolidated_urls:
                                        consolidated_urls[key] = url_obj["url"]
                                else:
                                    base_key = url_obj["type"].upper()
                                    i = 1
                                    key_name = base_key
                                    if key_name in consolidated_urls:
                                        while f"{base_key}_{i}" in consolidated_urls: 
                                            i += 1
                                        key_name = f"{base_key}_{i}"
                                    consolidated_urls[key_name] = url_obj["url"]
                        
                        final_msg["urls"] = consolidated_urls
                    
                    # Formatear respuesta final
                    final_json = {
                        "message": final_msg["message"],
                        "fields": final_msg["fields"],
                        "urls": final_msg.get("urls", {}),
                        "status": "ok"
                    }
                    
                    # Mover DNI/RUC al nivel superior si existen
                    dni_val = final_json["fields"].get("dni")
                    ruc_val = final_json["fields"].get("ruc")
                    
                    if dni_val:
                        final_json["dni"] = dni_val
                        final_json["fields"].pop("dni", None)
                    if ruc_val:
                        final_json["ruc"] = ruc_val
                        final_json["fields"].pop("ruc", None)
                    
                    return final_json
                    
            except UserBlockedError:
                print(f"❌ Bot {current_bot_id} bloqueado. Registrando fallo...")
                record_bot_failure(current_bot_id)
                if attempt < len(bots_to_try):
                    continue
                else:
                    raise Exception("Todos los bots están bloqueados temporalmente.")
                    
            except Exception as e:
                print(f"❌ Error con bot {current_bot_id}: {e}")
                if attempt < len(bots_to_try):
                    continue
                else:
                    raise e
        
        raise Exception("No se pudo obtener respuesta de ningún bot")
        
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error al procesar comando: {str(e)}"
        }
        
    finally:
        # 7. Limpiar siempre
        if client:
            try:
                await client.disconnect()
            except:
                pass
        
        # Limpiar archivos descargados más antiguos de 5 minutos
        try:
            now = time.time()
            for filename in os.listdir(DOWNLOAD_DIR):
                filepath = os.path.join(DOWNLOAD_DIR, filename)
                if os.path.isfile(filepath):
                    if now - os.path.getmtime(filepath) > 300:  # 5 minutos
                        os.remove(filepath)
        except Exception as e:
            print(f"⚠️ Error limpiando archivos: {e}")

# --- Wrapper síncrono para Flask ---
def run_telegram_command(command: str):
    """Ejecuta la función asíncrona desde Flask (síncrono)"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(send_telegram_command(command))
    finally:
        loop.close()

# --- Rutas HTTP ---

@app.route("/")
def root():
    return jsonify({
        "status": "ok",
        "message": "Gateway API para LEDER DATA Bot activo (Modo Serverless).",
        "mode": "serverless",
        "cost_optimized": True
    })

@app.route("/status")
def status():
    bot_status = {}
    for bot_id in ALL_BOT_IDS:
        is_blocked = is_bot_blocked(bot_id)
        bot_status[bot_id] = {
            "blocked": is_blocked,
            "last_fail": bot_fail_tracker.get(bot_id).isoformat() if bot_fail_tracker.get(bot_id) else None
        }
    
    return jsonify({
        "status": "ready",
        "session_loaded": bool(SESSION_STRING and SESSION_STRING.strip()),
        "api_credentials_ok": API_ID != 0 and bool(API_HASH),
        "bot_status": bot_status,
        "mode": "on-demand",
        "instructions": "Telethon se conecta solo cuando llega una consulta y se desconecta después"
    })

@app.route("/files/<path:filename>")
def files(filename):
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=True)

# --- Helper para determinar comando ---
def get_command_and_param(path, request_args):
    command_name_path = path.lstrip('/') 
    command_name = "sun" if command_name_path in ["sunat", "sun"] else command_name_path
    
    # Comandos que requieren un DNI de 8 dígitos
    dni_required_commands = [
        "dni", "dnif", "dnidb", "dnifdb", "c4", "dnivaz", "dnivam", "dnivel", "dniveln", 
        "fa", "fadb", "fb", "fbdb", "cnv", "cdef", "antpen", "antpol", "antjud", 
        "actancc", "actamcc", "actadcc", "tra", "sue", "cla", "sune", "cun", "colp", 
        "mine", "afp", "antpenv", "dend", "meta", "fis", "det", "rqh", "agv", "agvp",
        "fam", "fam2", "migrapdf", "con", "exd", "dir"
    ]
    
    # Comandos que aceptan varios tipos de consulta
    query_required_commands = [
        "tel", "telp", "cor", "nmv", "tremp", "fisdet",
        "dence", "denpas", "denci", "denp", "denar", "dencl", "cedula",
    ]
    
    optional_commands = ["osiptel", "claro", "entel", "pro", "sen", "sbs", "pasaporte", "seeker", "bdir"]
    
    param = ""

    if command_name == "sun":
        param = request_args.get("dni_o_ruc") or request_args.get("query")
        if not param or not param.isdigit() or len(param) not in [8, 11]:
            return None, f"Parámetro 'dni_o_ruc' o 'query' es requerido y debe ser un DNI (8 dígitos) o RUC (11 dígitos) para /{command_name_path}."
    
    elif command_name in dni_required_commands:
        param = request_args.get("dni")
        if not param or not param.isdigit() or len(param) != 8:
            return None, f"Parámetro 'dni' es requerido y debe ser un número de 8 dígitos para /{command_name_path}."
    
    elif command_name in query_required_commands:
        param_value = None
        
        if command_name == "fisdet":
            param_value = request_args.get("caso") or request_args.get("distritojudicial") or request_args.get("query")
            if not param_value:
                dni_val = request_args.get("dni")
                det_val = request_args.get("detalle")
                if dni_val and det_val:
                    param_value = f"{dni_val}|{det_val}"
                elif dni_val:
                    param_value = dni_val
        
        elif command_name == "dence": 
            param_value = request_args.get("carnet_extranjeria")
        elif command_name == "denpas": 
            param_value = request_args.get("pasaporte")
        elif command_name == "denci": 
            param_value = request_args.get("cedula_identidad")
        elif command_name == "denp": 
            param_value = request_args.get("placa")
        elif command_name == "denar": 
            param_value = request_args.get("serie_armamento")
        elif command_name == "dencl": 
            param_value = request_args.get("clave_denuncia")
        elif command_name == "cedula": 
            param_value = request_args.get("cedula")
        
        elif command_name in ["telp", "cor"]:
            param_value = request_args.get("dni_o_telefono") or request_args.get("dni_o_correo") or request_args.get("query")

        param = param_value or request_args.get("dni") or request_args.get("query")
             
        if not param:
            return None, f"Parámetro de consulta es requerido para /{command_name_path}."
    
    elif command_name in optional_commands:
        param_dni = request_args.get("dni")
        param_query = request_args.get("query")
        param_pasaporte = request_args.get("pasaporte") if command_name == "pasaporte" else None
        
        param = param_dni or param_query or param_pasaporte or ""
        
    else:
        param = request_args.get("dni") or request_args.get("query") or ""

    return f"/{command_name} {param}".strip(), None

# --- Definición de TODAS las rutas ---
ROUTES = [
    "/sunat", "/sun", "/dni", "/dnif", "/dnidb", "/dnifdb", "/c4", "/dnivaz", "/dnivam",
    "/dnivel", "/dniveln", "/fa", "/fadb", "/fb", "/fbdb", "/cnv", "/cdef", "/antpen",
    "/antpol", "/antjud", "/actancc", "/actamcc", "/actadcc", "/osiptel", "/claro",
    "/entel", "/pro", "/sen", "/sbs", "/tra", "/tremp", "/sue", "/cla", "/sune",
    "/cun", "/colp", "/mine", "/pasaporte", "/seeker", "/afp", "/bdir", "/meta",
    "/fis", "/fisdet", "/det", "/rqh", "/antpenv", "/dend", "/dence", "/denpas",
    "/denci", "/denp", "/denar", "/dencl", "/agv", "/agvp", "/cedula", "/telp",
    "/fam", "/fam2", "/migrapdf", "/con", "/exd", "/cor", "/dir"
]

# --- Función generadora de endpoints ---
def create_endpoint(endpoint_path):
    def endpoint_handler():
        command, error = get_command_and_param(endpoint_path, request.args)
        if error:
            return jsonify({"status": "error", "message": error}), 400
        
        if not command:
            return jsonify({"status": "error", "message": "Comando no válido"}), 400
        
        try:
            result = run_telegram_command(command)
            
            if result.get("status", "").startswith("error"):
                status_code = 500
                if result.get("status") == "error_bot_format":
                    status_code = 400
                elif result.get("status") == "error_not_found":
                    status_code = 404
                elif "timeout" in result.get("message", "").lower():
                    status_code = 504
                return jsonify(result), status_code
                
            return jsonify(result)
            
        except FutureTimeoutError:
            return jsonify({
                "status": "error", 
                "message": f"Error interno: Timeout excedido ({TIMEOUT_TOTAL}s)."
            }), 504
        except Exception as e:
            return jsonify({
                "status": "error", 
                "message": f"Error interno: {str(e)}"
            }), 500
    
    return endpoint_handler

# --- Registrar todas las rutas ---
for route in ROUTES:
    app.route(route, methods=["GET"])(create_endpoint(route))

# --- Rutas especiales ---
@app.route("/dni_nombres", methods=["GET"])
def api_dni_nombres():
    nombres = unquote(request.args.get("nombres", "")).strip()
    ape_paterno = unquote(request.args.get("apepaterno", "")).strip()
    ape_materno = unquote(request.args.get("apematerno", "")).strip()

    if not ape_paterno or not ape_materno:
        return jsonify({
            "status": "error", 
            "message": "Faltan parámetros: 'apepaterno' y 'apematerno' son obligatorios."
        }), 400

    formatted_nombres = nombres.replace(" ", ",")
    formatted_apepaterno = ape_paterno.replace(" ", "+")
    formatted_apematerno = ape_materno.replace(" ", "+")

    command = f"/nm {formatted_nombres}|{formatted_apepaterno}|{formatted_apematerno}"
    
    try:
        result = run_telegram_command(command)
        if result.get("status", "").startswith("error"):
            status_code = 500
            if result.get("status") == "error_bot_format":
                status_code = 400
            elif result.get("status") == "error_not_found":
                status_code = 404
            elif "timeout" in result.get("message", "").lower():
                status_code = 504
            return jsonify(result), status_code
            
        return jsonify(result)
        
    except FutureTimeoutError:
        return jsonify({
            "status": "error", 
            "message": f"Error interno: Timeout excedido ({TIMEOUT_TOTAL}s)."
        }), 504
    except Exception as e:
        return jsonify({
            "status": "error", 
            "message": f"Error interno: {str(e)}"
        }), 500

@app.route("/venezolanos_nombres", methods=["GET"])
def api_venezolanos_nombres():
    query = unquote(request.args.get("query", "")).strip()
    
    if not query:
        return jsonify({
            "status": "error", 
            "message": "Parámetro 'query' (nombres_apellidos) es requerido para /venezolanos_nombres."
        }), 400

    command = f"/nmv {query}"
    
    try:
        result = run_telegram_command(command)
        if result.get("status", "").startswith("error"):
            status_code = 500
            if result.get("status") == "error_bot_format":
                status_code = 400
            elif result.get("status") == "error_not_found":
                status_code = 404
            elif "timeout" in result.get("message", "").lower():
                status_code = 504
            return jsonify(result), status_code
            
        return jsonify(result)
        
    except FutureTimeoutError:
        return jsonify({
            "status": "error", 
            "message": f"Error interno: Timeout excedido ({TIMEOUT_TOTAL}s)."
        }), 504
    except Exception as e:
        return jsonify({
            "status": "error", 
            "message": f"Error interno: {str(e)}"
        }), 500

# --- Endpoints de mantenimiento (login ya no es necesario con SESSION_STRING) ---
@app.route("/login", methods=["GET"])
def login_info():
    return jsonify({
        "status": "info",
        "message": "El sistema ahora usa SESSION_STRING. No se requiere login manual.",
        "instruction": "Configure la variable de entorno SESSION_STRING con la sesión previamente generada."
    })

@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({
        "status": "healthy",
        "mode": "serverless",
        "timestamp": datetime.utcnow().isoformat(),
        "session_configured": bool(SESSION_STRING and SESSION_STRING.strip())
    })

# --- Archivo Procfile (para Railway) ---
"""
Crear un archivo llamado "Procfile" (sin extensión) con este contenido:

web: gunicorn app:app --workers 1 --threads 1 --timeout 60 --bind 0.0.0.0:${PORT}

"""

# --- Variables de entorno requeridas ---
"""
Variables de entorno REQUERIDAS en Railway:

API_ID=tu_api_id
API_HASH=tu_api_hash
SESSION_STRING=tu_session_string_generada_anteriormente
PUBLIC_URL=https://tu-app.up.railway.app
PORT=8080

"""

if __name__ == "__main__":
    print("🚀 Iniciando backend en modo SERVERLESS (on-demand)")
    print("📊 Modo optimizado para costos (<5 USD/mes)")
    print("🔗 Telethon se conecta solo cuando recibe consultas")
    app.run(host="0.0.0.0", port=PORT, debug=False)

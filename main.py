import os
import re
import asyncio
import traceback
import time
import json
import mimetypes
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

# TIMEOUTS ACTUALIZADOS SEGÚN TUS INSTRUCCIONES
TIMEOUT_PRIMARY = 35  # ~35 segundos para bot principal
TIMEOUT_BACKUP = 50   # 50 segundos para bot de respaldo
BOT_BLOCK_HOURS = 4   # 4 horas de bloqueo si falla

# --- Trackeo de Fallos de Bots (ACTUALIZADO) ---
bot_fail_tracker = {}

def is_bot_blocked(bot_id: str) -> bool:
    """Verifica si el bot está bloqueado por fallos recientes."""
    last_fail_time = bot_fail_tracker.get(bot_id)
    if not last_fail_time:
        return False

    now = datetime.now()
    block_time_ago = now - timedelta(hours=BOT_BLOCK_HOURS)

    if last_fail_time > block_time_ago:
        return True
    
    print(f"✅ Bot {bot_id} ha cumplido su tiempo de bloqueo ({BOT_BLOCK_HOURS}h). Desbloqueado.")
    bot_fail_tracker.pop(bot_id, None)
    return False

def record_bot_failure(bot_id: str):
    """Registra la hora actual como la última hora de fallo del bot."""
    print(f"🚨 Bot {bot_id} ha fallado y será BLOQUEADO por {BOT_BLOCK_HOURS} horas.")
    bot_fail_tracker[bot_id] = datetime.now()

# --- Lógica de Limpieza y Extracción de Datos (MEJORADA) ---
def clean_and_extract(raw_text: str):
    if not raw_text:
        return {"text": "", "fields": {}}

    text = raw_text
    
    # 1. Eliminar completamente cualquier mención de LEDER_BOT
    text = re.sub(r"\[#?LEDER_BOT\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[CONSULTA PE\]", "", text, flags=re.IGNORECASE)
    
    # 2. Eliminar cabecera completa
    header_pattern = r"^\[.*?\]\s*→\s*.*?\[.*?\](\r?\n){1,2}"
    text = re.sub(header_pattern, "", text, flags=re.IGNORECASE | re.DOTALL)
    
    # 3. Eliminar pie de página
    footer_pattern = r"((\r?\n){1,2}\[|Página\s*\d+\/\d+.*|(\r?\n){1,2}Por favor, usa el formato correcto.*|↞ Anterior|Siguiente ↠.*|Credits\s*:.+|Wanted for\s*:.+|\s*@lederdata.*|(\r?\n){1,2}\s*Marca\s*@lederdata.*|(\r?\n){1,2}\s*Créditos\s*:\s*\d+)"
    text = re.sub(footer_pattern, "", text, flags=re.IGNORECASE | re.DOTALL)
    
    # 4. Limpiar separadores y espacios extra
    text = re.sub(r"\-{3,}", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\s+", " ", text)  # Normalizar espacios
    text = text.strip()

    # 5. Extracción dinámica de campos estándar
    fields = {}
    
    # Patrones de extracción mejorados
    patterns = {
        "dni": r"DNI\s*:\s*(\d{8})",
        "ruc": r"RUC\s*:\s*(\d{11})",
        "apellido_paterno": r"APELLIDO\s+PATERNO\s*:\s*(.*?)(?:\n|$)",
        "apellido_materno": r"APELLIDO\s+MATERNO\s*:\s*(.*?)(?:\n|$)", 
        "nombres": r"NOMBRES\s*:\s*(.*?)(?:\n|$)",
        "estado": r"ESTADO\s*:\s*(.*?)(?:\n|$)",
        "fecha_nacimiento": r"(?:FECHA\s+DE\s+NACIMIENTO|F\.?NAC\.?)\s*:\s*(.*?)(?:\n|$)",
        "genero": r"(?:GÉNERO|SEXO)\s*:\s*(.*?)(?:\n|$)",
        "direccion": r"(?:DIRECCIÓN|DOMICILIO)\s*:\s*(.*?)(?:\n|$)",
        "ubigeo": r"UBIGEO\s*:\s*(.*?)(?:\n|$)",
        "departamento": r"DEPARTAMENTO\s*:\s*(.*?)(?:\n|$)",
        "provincia": r"PROVINCIA\s*:\s*(.*?)(?:\n|$)",
        "distrito": r"DISTRITO\s*:\s*(TODO\s+EL\s+DISTRITO|.*?)(?:\n|$)",
    }
    
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if match:
            fields[key] = match.group(1).strip()
            # Limpiar el campo del texto principal
            text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.DOTALL)
    
    # Extraer tipo de foto si existe
    photo_type_match = re.search(r"Foto\s*:\s*(rostro|huella|firma|adverso|reverso).*", text, re.IGNORECASE)
    if photo_type_match: 
        fields["photo_type"] = photo_type_match.group(1).lower()
    
    # 6. Manejo de mensajes de no encontrado
    not_found_pattern = r"\[⚠️\]\s*(no se encontro información|no se han encontrado resultados|no se encontró una|no hay resultados|no tenemos datos|no se encontraron registros)"
    if re.search(not_found_pattern, text, re.IGNORECASE | re.DOTALL):
         fields["not_found"] = True
    
    # Limpiar texto final
    text = re.sub(r"\n\s*\n", "\n", text)  # Eliminar líneas vacías múltiples
    text = text.strip()
    
    return {"text": text, "fields": fields}

# --- Función Principal para Conexión On-Demand (MEJORADA para manejo de failover) ---
async def send_telegram_command(command: str, consulta_id: str = None, endpoint_path: str = None):
    """
    Función on-demand con manejo CORREGIDO de duplicación:
    - Envía comando UNA SOLA VEZ al bot principal
    - Solo si recibe "ANTI-SPAM" o timeout, intenta UNA SOLA VEZ con el bot de respaldo
    """
    client = None
    handler_removed = False
    
    try:
        # 1. Verificar credenciales
        if API_ID == 0 or not API_HASH:
            raise Exception("API_ID o API_HASH no configurados.")
        
        if not SESSION_STRING or not SESSION_STRING.strip():
            raise Exception("SESSION_STRING no configurada. Se requiere sesión válida.")
        
        # 2. Crear cliente
        session = StringSession(SESSION_STRING)
        client = TelegramClient(session, API_ID, API_HASH)
        
        # 3. Conectar
        await client.connect()
        
        if not await client.is_user_authorized():
            raise Exception("Cliente no autorizado. La sesión puede haber expirado.")
        
        # 4. Extraer DNI para tracking
        dni_match = re.search(r"/\w+\s+(\d{8,11})", command)
        dni = dni_match.group(1) if dni_match else None
        
        # 5. Generar consulta_id si no se proporciona
        if not consulta_id:
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            tipo_consulta = determinar_tipo_consulta_por_comando(endpoint_path) if endpoint_path else "general"
            consulta_id = f"{tipo_consulta}_{dni or 'unknown'}_{timestamp}" if dni else f"{tipo_consulta}_{timestamp}"
        
        # 6. Determinar qué bot usar primero
        primary_blocked = is_bot_blocked(LEDERDATA_BOT_ID)
        backup_blocked = is_bot_blocked(LEDERDATA_BACKUP_BOT_ID)
        
        # Decidir qué bot usar primero según bloqueos
        bot_to_use_first = None
        bot_to_use_backup = None
        
        if not primary_blocked:
            bot_to_use_first = LEDERDATA_BOT_ID
            if not backup_blocked:
                bot_to_use_backup = LEDERDATA_BACKUP_BOT_ID
        elif not backup_blocked:
            bot_to_use_first = LEDERDATA_BACKUP_BOT_ID
        else:
            raise Exception("Todos los bots están temporalmente bloqueados.")
        
        print(f"🤖 Bot principal seleccionado: {bot_to_use_first}")
        if bot_to_use_backup:
            print(f"🤖 Bot de respaldo disponible: {bot_to_use_backup}")
        
        # 7. Variables para capturar respuestas
        all_received_messages = []
        all_files_data = []
        stop_collecting = asyncio.Event()
        
        # Variable para trackear última actividad
        last_message_time = [time.time()]
        
        # Bandera para saber de qué bot estamos recibiendo mensajes
        current_bot_id = None
        
        # 8. Handler temporal para capturar respuestas
        @client.on(events.NewMessage(incoming=True))
        async def temp_handler(event):
            if stop_collecting.is_set():
                return
                
            try:
                # Verificar si el mensaje viene del bot que estamos usando actualmente
                if not current_bot_id:
                    return  # Aún no hemos definido qué bot estamos usando
                    
                try:
                    entity = await client.get_entity(current_bot_id)
                    if event.sender_id != entity.id:
                        return  # Ignorar mensajes de otros bots/usuarios
                except:
                    return
                
                # Actualizar tiempo de última actividad
                last_message_time[0] = time.time()
                
                raw_text = event.raw_text or ""
                cleaned = clean_and_extract(raw_text)
                
                # Verificar match de DNI si existe
                if dni and cleaned["fields"].get("dni") != dni:
                    return  # Ignorar si el DNI no coincide
                
                msg_obj = {
                    "chat_id": getattr(event, "chat_id", None),
                    "from_id": event.sender_id,
                    "date": event.message.date.isoformat() if getattr(event, "message", None) else datetime.utcnow().isoformat(),
                    "message": cleaned["text"],
                    "fields": cleaned["fields"],
                    "urls": [],
                    "bot_id": entity.id,
                    "event_message": event.message
                }
                
                all_received_messages.append(msg_obj)
                print(f"📥 Mensaje recibido de bot {'(respaldo)' if current_bot_id == LEDERDATA_BACKUP_BOT_ID else '(principal)'}: {len(msg_obj['message'])} chars")
                
            except Exception as e:
                print(f"Error en handler temporal: {e}")
        
        # 9. INTENTO CON BOT PRINCIPAL (SI DISPONIBLE)
        if bot_to_use_first == LEDERDATA_BOT_ID:
            print(f"\n🎯 INTENTANDO CON BOT PRINCIPAL: {bot_to_use_first}")
            print(f"   Comando: {command}")
            print(f"   Timeout: {TIMEOUT_PRIMARY}s")
            
            # Resetear para este intento
            all_received_messages = []
            all_files_data = []
            stop_collecting.clear()
            last_message_time[0] = time.time()
            current_bot_id = LEDERDATA_BOT_ID
            
            try:
                # Enviar comando UNA SOLA VEZ al bot principal
                await client.send_message(bot_to_use_first, command)
                print(f"✅ Comando enviado UNA VEZ al bot principal")
                
                # Timer para múltiples mensajes
                start_time = time.time()
                
                # --- LÓGICA DE ESPERA PARA BOT PRINCIPAL ---
                while True:
                    elapsed_total = time.time() - start_time
                    silence_duration = time.time() - last_message_time[0]
                    
                    # Si ya recibimos algo, esperamos un silencio de 4 segundos para cerrar
                    if len(all_received_messages) > 0:
                        if silence_duration > 4.0: 
                            print(f"✅ Silencio detectado ({silence_duration:.1f}s). Total mensajes: {len(all_received_messages)}")
                            break
                    
                    # Si no ha llegado nada y pasamos el timeout total
                    if elapsed_total > TIMEOUT_PRIMARY:
                        if len(all_received_messages) == 0:
                            print(f"⏰ TIMEOUT: Bot principal no respondió en {TIMEOUT_PRIMARY}s")
                            # Bot principal está caído/lageado
                            record_bot_failure(LEDERDATA_BOT_ID)
                            break  # Salir para intentar con bot de respaldo
                        else:
                            break  # Cerramos con lo que tengamos
                            
                    await asyncio.sleep(0.5)
                
            except UserBlockedError:
                print(f"❌ Bot principal bloqueado por el usuario")
                record_bot_failure(LEDERDATA_BOT_ID)
                all_received_messages = []  # Limpiar mensajes
            except Exception as e:
                print(f"❌ Error con bot principal: {str(e)[:100]}")
                if "blocked" in str(e).lower():
                    record_bot_failure(LEDERDATA_BOT_ID)
            
            # 10. ANALIZAR RESPUESTA DEL BOT PRINCIPAL
            if all_received_messages:
                print(f"✅ Bot principal respondió con {len(all_received_messages)} mensajes")
                
                # Verificar si hay mensaje ANTI-SPAM
                anti_spam_detected = False
                for msg in all_received_messages:
                    if "⛔ ANTI-SPAM" in msg.get("message", "") or "ANTI-SPAM" in msg.get("message", ""):
                        anti_spam_detected = True
                        break
                
                if anti_spam_detected:
                    print("🔄 Bot principal respondió con ANTI-SPAM, intentando con bot de respaldo...")
                    
                    # Esperar 5 segundos como indica el mensaje
                    print("⏳ Esperando 5 segundos antes de intentar con bot de respaldo...")
                    await asyncio.sleep(5)
                    
                    # Continuar con la lógica del bot de respaldo
                    # (se procesará después de este bloque)
                else:
                    # Bot principal respondió normalmente, procesar respuesta
                    stop_collecting.set()
                    return await process_bot_response(
                        client, temp_handler, all_received_messages, 
                        all_files_data, handler_removed, consulta_id
                    )
        
        # 11. SI NECESITAMOS USAR BOT DE RESPALDO
        # (porque: 1) bot principal no respondió, 2) bot principal respondió ANTI-SPAM, 
        # 3) bot principal está bloqueado, o 4) estamos usando directamente el bot de respaldo como principal)
        
        # Verificar si necesitamos usar el bot de respaldo
        use_backup = False
        reason = ""
        
        if not all_received_messages and bot_to_use_backup:
            use_backup = True
            reason = "bot principal no respondió"
        elif anti_spam_detected and bot_to_use_backup:
            use_backup = True
            reason = "ANTI-SPAM detectado"
        elif bot_to_use_first == LEDERDATA_BACKUP_BOT_ID:
            use_backup = True
            reason = "usando bot de respaldo como principal"
        
        if use_backup and bot_to_use_backup:
            print(f"\n🔄 INTENTANDO CON BOT DE RESPALDO: {bot_to_use_backup} (Razón: {reason})")
            print(f"   Comando: {command}")
            print(f"   Timeout: {TIMEOUT_BACKUP}s")
            
            # Resetear para intento con bot de respaldo
            all_received_messages = []
            all_files_data = []
            stop_collecting.clear()
            last_message_time[0] = time.time()
            current_bot_id = LEDERDATA_BACKUP_BOT_ID
            
            # Enviar comando UNA SOLA VEZ al bot de respaldo
            await client.send_message(bot_to_use_backup, command)
            print(f"✅ Comando enviado UNA VEZ al bot de respaldo")
            
            # Timer para bot de respaldo
            start_time = time.time()
            
            # --- LÓGICA DE ESPERA PARA BOT DE RESPALDO ---
            while True:
                elapsed_total = time.time() - start_time
                silence_duration = time.time() - last_message_time[0]
                
                # Si ya recibimos algo, esperamos un silencio de 4 segundos para cerrar
                if len(all_received_messages) > 0:
                    if silence_duration > 4.0: 
                        print(f"✅ Silencio detectado ({silence_duration:.1f}s). Total mensajes: {len(all_received_messages)}")
                        break
                
                # Si no ha llegado nada y pasamos el timeout total
                if elapsed_total > TIMEOUT_BACKUP:
                    if len(all_received_messages) == 0:
                        print(f"⏰ TIMEOUT: Bot de respaldo no respondió en {TIMEOUT_BACKUP}s")
                        record_bot_failure(LEDERDATA_BACKUP_BOT_ID)
                        raise Exception("Bot de respaldo no respondió a tiempo")
                    else:
                        break
                        
                await asyncio.sleep(0.5)
            
            # 12. PROCESAR RESPUESTA DEL BOT DE RESPALDO
            if all_received_messages:
                print(f"✅ Bot de respaldo respondió con {len(all_received_messages)} mensajes")
                stop_collecting.set()
                return await process_bot_response(
                    client, temp_handler, all_received_messages, 
                    all_files_data, handler_removed, consulta_id
                )
            else:
                raise Exception("No se recibieron mensajes del bot de respaldo")
        
        # 13. SI LLEGAMOS AQUÍ, NO HAY RESPUESTA VÁLIDA
        if not all_received_messages:
            raise Exception("No se recibió respuesta de ningún bot")
                
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error al procesar comando: {str(e)}"
        }
        
    finally:
        # 14. Limpieza final
        if client:
            try:
                # Asegurarnos de que el handler ya fue removido
                if not handler_removed:
                    try:
                        client.remove_event_handler(temp_handler)
                    except:
                        pass
                
                await client.disconnect()
                print("🔌 Cliente desconectado exitosamente")
            except Exception as e:
                print(f"⚠️ Error desconectando cliente: {e}")
        
        # Limpiar archivos descargados más antiguos de 5 minutos
        try:
            now = time.time()
            cleaned_count = 0
            for filename in os.listdir(DOWNLOAD_DIR):
                filepath = os.path.join(DOWNLOAD_DIR, filename)
                if os.path.isfile(filepath):
                    if now - os.path.getmtime(filepath) > 300:  # 5 minutos
                        os.remove(filepath)
                        cleaned_count += 1
            if cleaned_count > 0:
                print(f"🧹 Limpiados {cleaned_count} archivos antiguos")
        except Exception as e:
            print(f"⚠️ Error limpiando archivos: {e}")

# --- Función para procesar respuesta del bot ---
async def process_bot_response(client, temp_handler, all_received_messages, all_files_data, handler_removed, consulta_id):
    """Procesa la respuesta del bot (compartida para principal y respaldo)"""
    try:
        # Verificar si hay error de formato en cualquier mensaje
        format_error_detected = False
        for msg in all_received_messages:
            if "Por favor, usa el formato correcto" in msg.get("message", ""):
                format_error_detected = True
                break
        
        if format_error_detected:
            # Remover handler ANTES de desconectar
            if client and not handler_removed:
                client.remove_event_handler(temp_handler)
                handler_removed = True
            return {
                "status": "error",
                "message": "Formato de consulta incorrecto. Verifica los parámetros enviados."
            }
        
        # Verificar si es "no encontrado" en cualquier mensaje
        not_found_detected = False
        for msg in all_received_messages:
            if msg.get("fields", {}).get("not_found", False):
                not_found_detected = True
                break
        
        if not_found_detected:
            # Remover handler ANTES de desconectar
            if client and not handler_removed:
                client.remove_event_handler(temp_handler)
                handler_removed = True
            return {
                "status": "error",
                "message": "No se encontraron resultados para dicha consulta. Intenta con otro dato."
            }
        
        # --- DESCARGAR ARCHIVOS ANTES DE CERRAR CONEXIÓN ---
        print(f"📥 Iniciando descarga de archivos multimedia...")
        
        for idx, msg in enumerate(all_received_messages):
            try:
                event_msg = msg.get("event_message")
                if event_msg and getattr(event_msg, "media", None):
                    media_list = []
                    
                    if isinstance(event_msg.media, (MessageMediaDocument, MessageMediaPhoto)):
                        media_list.append(event_msg.media)
                    
                    for i, media in enumerate(media_list):
                        try:
                            file_ext = '.file'
                            content_type = None
                            
                            if hasattr(media, 'document') and hasattr(media.document, 'attributes'):
                                file_name = getattr(media.document, 'file_name', 'file')
                                file_ext = os.path.splitext(file_name)[1]
                                # Determinar content_type
                                if 'pdf' in file_name.lower() or file_ext == '.pdf':
                                    content_type = 'application/pdf'
                                elif 'jpg' in file_name.lower() or 'jpeg' in file_name.lower() or file_ext in ['.jpg', '.jpeg']:
                                    content_type = 'image/jpeg'
                                elif 'png' in file_name.lower() or file_ext == '.png':
                                    content_type = 'image/png'
                                else:
                                    content_type = 'application/octet-stream'
                            elif isinstance(media, MessageMediaPhoto) or (hasattr(media, 'photo') and media.photo):
                                file_ext = '.jpg'
                                content_type = 'image/jpeg'
                                
                            dni_part = f"_{msg['fields'].get('dni')}" if msg['fields'].get('dni') else ""
                            type_part = f"_{msg['fields'].get('photo_type')}" if msg['fields'].get('photo_type') else ""
                            timestamp_str = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
                            unique_filename = f"{timestamp_str}_{event_msg.id}{dni_part}{type_part}_{i}{file_ext}"
                            
                            # DESCARGAR ARCHIVO SÍNCRONO
                            print(f"   Descargando archivo {i+1} del mensaje {idx+1}...")
                            
                            # Verificar que el cliente siga conectado
                            if not client.is_connected():
                                print("⚠️ Cliente desconectado durante descarga, reconectando...")
                                await client.connect()
                            
                            saved_path = await client.download_media(
                                event_msg, 
                                file=os.path.join(DOWNLOAD_DIR, unique_filename)
                            )
                            
                            # Leer contenido para archivos locales
                            if saved_path and os.path.exists(saved_path):
                                with open(saved_path, 'rb') as f:
                                    file_content = f.read()
                                
                                # Guardar para posible uso futuro
                                all_files_data.append((unique_filename, file_content, content_type))
                                
                                # URL local temporal
                                msg_url = {
                                    "url": f"{PUBLIC_URL}/files/{os.path.basename(saved_path)}", 
                                    "type": "document",
                                }
                                if "urls" not in msg:
                                    msg["urls"] = []
                                msg["urls"].append(msg_url)
                            
                            print(f"   ✅ Archivo descargado: {unique_filename}")
                            
                        except Exception as e:
                            print(f"❌ Error procesando archivo {i} del mensaje {idx}: {e}")
                            continue
            except Exception as e:
                print(f"❌ Error procesando mensaje {idx} para archivos: {e}")
                continue
        
        print(f"📊 Total de archivos descargados: {len(all_files_data)}")
        
        # Remover handler ANTES de cualquier posible desconexión
        if client and not handler_removed:
            client.remove_event_handler(temp_handler)
            handler_removed = True
        
        # --- CONSOLIDAR CAMPOS DE TODOS LOS MENSAJES ---
        final_fields = {}
        urls_temporales = []
        
        for msg in all_received_messages:
            # Unificar fields de todos los mensajes (sin sobrescribir)
            if msg.get("fields"):
                for key, value in msg["fields"].items():
                    if key not in final_fields:
                        final_fields[key] = value
            
            # Extraer URLs temporales
            if isinstance(msg.get("urls"), list):
                for url_obj in msg["urls"]:
                    urls_temporales.append({
                        "type": url_obj.get("type", "document"),
                        "url": url_obj.get("url")
                    })
        
        # 13. CONSTRUIR RESPUESTA LIMPIA
        response_data = {}
        
        # Agregar campos extraídos al nivel raíz
        for key, value in final_fields.items():
            if value:  # Solo agregar si tiene valor
                response_data[key] = value
        
        # Agregar metadatos
        response_data["total_files"] = len(urls_temporales)
        response_data["total_messages"] = len(all_received_messages)
        
        if urls_temporales:
            response_data["urls"] = urls_temporales
        
        return response_data
        
    except Exception as e:
        print(f"❌ Error procesando respuesta del bot: {e}")
        return {
            "status": "error",
            "message": f"Error procesando respuesta: {str(e)}"
        }

# --- Wrapper síncrono para Flask ---
def run_telegram_command(command: str, consulta_id: str = None, endpoint_path: str = None):
    """Ejecuta la función asíncrona desde Flask (síncrono)"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(send_telegram_command(command, consulta_id, endpoint_path))
    finally:
        loop.close()

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

def determinar_tipo_consulta_por_comando(comando_path: str):
    """Determina el tipo de consulta basado en el endpoint (solo para organización)"""
    comando = comando_path.lstrip('/').split('/')[0]
    
    # Mapa de comandos a tipos de consulta
    tipo_por_comando = {
        'dni': 'DNI_virtual',
        'dnif': 'DNI_virtual',
        'dnidb': 'DNI_virtual',
        'dnifdb': 'DNI_virtual',
        'dnivaz': 'DNI_virtual',
        'dnivam': 'DNI_virtual',
        'dnivel': 'DNI_virtual',
        'dniveln': 'DNI_virtual',
        'sbs': 'SBS',
        'denuncia': 'Denuncias',
        'dence': 'Denuncias',
        'denpas': 'Denuncias',
        'denci': 'Denuncias',
        'denp': 'Denuncias',
        'denar': 'Denuncias',
        'dencl': 'Denuncias',
        'migrapdf': 'Migraciones',
        'sunat': 'SUNAT',
        'sun': 'SUNAT',
        'antpen': 'Antecedentes',
        'antpol': 'Antecedentes',
        'antjud': 'Antecedentes'
    }
    
    return tipo_por_comando.get(comando, 'general')

# --- Aplicación Flask ---
app = Flask(__name__)
CORS(app)

# --- Rutas HTTP ---

@app.route("/")
def root():
    return jsonify({
        "status": "ok",
        "message": "Gateway API para LEDER DATA Bot activo (Modo Serverless).",
        "mode": "serverless",
        "cost_optimized": True,
        "version": "4.4 - Duplicación CORREGIDA (1 comando por bot)"
    })

@app.route("/status")
def status():
    bot_status = {}
    for bot_id in ALL_BOT_IDS:
        is_blocked = is_bot_blocked(bot_id)
        last_fail = bot_fail_tracker.get(bot_id)
        bot_status[bot_id] = {
            "blocked": is_blocked,
            "last_fail": last_fail.isoformat() if last_fail else None,
            "block_hours": BOT_BLOCK_HOURS if is_blocked else 0
        }
    
    return jsonify({
        "status": "ready",
        "session_loaded": bool(SESSION_STRING and SESSION_STRING.strip()),
        "api_credentials_ok": API_ID != 0 and bool(API_HASH),
        "bot_status": bot_status,
        "mode": "on-demand",
        "storage_pe": "removed",
        "timeouts": {
            "primary_bot": TIMEOUT_PRIMARY,
            "backup_bot": TIMEOUT_BACKUP,
            "block_hours": BOT_BLOCK_HOURS
        }
    })

@app.route("/files/<path:filename>")
def files(filename):
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=True)

# --- Función para manejar endpoints ---
def handle_api_endpoint(endpoint_path):
    """Manejador genérico para todos los endpoints de API"""
    command, error = get_command_and_param(endpoint_path, request.args)
    if error:
        return jsonify({"status": "error", "message": error}), 400
    
    if not command:
        return jsonify({"status": "error", "message": "Comando no válido"}), 400
    
    try:
        # Generar ID único para esta consulta
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        command_name = endpoint_path.lstrip('/').split('/')[0]
        dni_match = re.search(r"/(\d{8,11})", command)
        dni = dni_match.group(1) if dni_match else "unknown"
        
        consulta_id = f"{command_name}_{dni}_{timestamp}"
        
        # Ejecutar comando
        result = run_telegram_command(command, consulta_id, endpoint_path)
        
        # Si la respuesta tiene status="error", mantener formato antiguo
        if result.get("status") == "error":
            status_code = 500
            if "Formato de consulta incorrecto" in result.get("message", ""):
                status_code = 400
            elif "No se encontraron resultados" in result.get("message", ""):
                status_code = 404
            return jsonify(result), status_code
        
        # Si no tiene status, es el nuevo formato limpio
        return jsonify(result)
        
    except FutureTimeoutError:
        return jsonify({
            "status": "error", 
            "message": f"Error interno: Timeout excedido ({TIMEOUT_BACKUP}s)."
        }), 504
    except Exception as e:
        return jsonify({
            "status": "error", 
            "message": f"Error interno: {str(e)}"
        }), 500

# --- DEFINICIÓN DE TODAS LAS RUTAS (TODAS MANTENIDAS) ---

@app.route("/sunat", methods=["GET"])
def sunat():
    return handle_api_endpoint("/sunat")

@app.route("/sun", methods=["GET"])
def sun():
    return handle_api_endpoint("/sun")

@app.route("/dni", methods=["GET"])
def dni():
    return handle_api_endpoint("/dni")

@app.route("/dnif", methods=["GET"])
def dnif():
    return handle_api_endpoint("/dnif")

@app.route("/dnidb", methods=["GET"])
def dnidb():
    return handle_api_endpoint("/dnidb")

@app.route("/dnifdb", methods=["GET"])
def dnifdb():
    return handle_api_endpoint("/dnifdb")

@app.route("/c4", methods=["GET"])
def c4():
    return handle_api_endpoint("/c4")

@app.route("/dnivaz", methods=["GET"])
def dnivaz():
    return handle_api_endpoint("/dnivaz")

@app.route("/dnivam", methods=["GET"])
def dnivam():
    return handle_api_endpoint("/dnivam")

@app.route("/dnivel", methods=["GET"])
def dnivel():
    return handle_api_endpoint("/dnivel")

@app.route("/dniveln", methods=["GET"])
def dniveln():
    return handle_api_endpoint("/dniveln")

@app.route("/fa", methods=["GET"])
def fa():
    return handle_api_endpoint("/fa")

@app.route("/fadb", methods=["GET"])
def fadb():
    return handle_api_endpoint("/fadb")

@app.route("/fb", methods=["GET"])
def fb():
    return handle_api_endpoint("/fb")

@app.route("/fbdb", methods=["GET"])
def fbdb():
    return handle_api_endpoint("/fbdb")

@app.route("/cnv", methods=["GET"])
def cnv():
    return handle_api_endpoint("/cnv")

@app.route("/cdef", methods=["GET"])
def cdef():
    return handle_api_endpoint("/cdef")

@app.route("/antpen", methods=["GET"])
def antpen():
    return handle_api_endpoint("/antpen")

@app.route("/antpol", methods=["GET"])
def antpol():
    return handle_api_endpoint("/antpol")

@app.route("/antjud", methods=["GET"])
def antjud():
    return handle_api_endpoint("/antjud")

@app.route("/actancc", methods=["GET"])
def actancc():
    return handle_api_endpoint("/actancc")

@app.route("/actamcc", methods=["GET"])
def actamcc():
    return handle_api_endpoint("/actamcc")

@app.route("/actadcc", methods=["GET"])
def actadcc():
    return handle_api_endpoint("/actadcc")

@app.route("/osiptel", methods=["GET"])
def osiptel():
    return handle_api_endpoint("/osiptel")

@app.route("/claro", methods=["GET"])
def claro():
    return handle_api_endpoint("/claro")

@app.route("/entel", methods=["GET"])
def entel():
    return handle_api_endpoint("/entel")

@app.route("/pro", methods=["GET"])
def pro():
    return handle_api_endpoint("/pro")

@app.route("/sen", methods=["GET"])
def sen():
    return handle_api_endpoint("/sen")

@app.route("/sbs", methods=["GET"])
def sbs():
    return handle_api_endpoint("/sbs")

@app.route("/tra", methods=["GET"])
def tra():
    return handle_api_endpoint("/tra")

@app.route("/tremp", methods=["GET"])
def tremp():
    return handle_api_endpoint("/tremp")

@app.route("/sue", methods=["GET"])
def sue():
    return handle_api_endpoint("/sue")

@app.route("/cla", methods=["GET"])
def cla():
    return handle_api_endpoint("/cla")

@app.route("/sune", methods=["GET"])
def sune():
    return handle_api_endpoint("/sune")

@app.route("/cun", methods=["GET"])
def cun():
    return handle_api_endpoint("/cun")

@app.route("/colp", methods=["GET"])
def colp():
    return handle_api_endpoint("/colp")

@app.route("/mine", methods=["GET"])
def mine():
    return handle_api_endpoint("/mine")

@app.route("/pasaporte", methods=["GET"])
def pasaporte():
    return handle_api_endpoint("/pasaporte")

@app.route("/seeker", methods=["GET"])
def seeker():
    return handle_api_endpoint("/seeker")

@app.route("/afp", methods=["GET"])
def afp():
    return handle_api_endpoint("/afp")

@app.route("/bdir", methods=["GET"])
def bdir():
    return handle_api_endpoint("/bdir")

@app.route("/meta", methods=["GET"])
def meta():
    return handle_api_endpoint("/meta")

@app.route("/fis", methods=["GET"])
def fis():
    return handle_api_endpoint("/fis")

@app.route("/fisdet", methods=["GET"])
def fisdet():
    return handle_api_endpoint("/fisdet")

@app.route("/det", methods=["GET"])
def det():
    return handle_api_endpoint("/det")

@app.route("/rqh", methods=["GET"])
def rqh():
    return handle_api_endpoint("/rqh")

@app.route("/antpenv", methods=["GET"])
def antpenv():
    return handle_api_endpoint("/antpenv")

@app.route("/dend", methods=["GET"])
def dend():
    return handle_api_endpoint("/dend")

@app.route("/dence", methods=["GET"])
def dence():
    return handle_api_endpoint("/dence")

@app.route("/denpas", methods=["GET"])
def denpas():
    return handle_api_endpoint("/denpas")

@app.route("/denci", methods=["GET"])
def denci():
    return handle_api_endpoint("/denci")

@app.route("/denp", methods=["GET"])
def denp():
    return handle_api_endpoint("/denp")

@app.route("/denar", methods=["GET"])
def denar():
    return handle_api_endpoint("/denar")

@app.route("/dencl", methods=["GET"])
def dencl():
    return handle_api_endpoint("/dencl")

@app.route("/agv", methods=["GET"])
def agv():
    return handle_api_endpoint("/agv")

@app.route("/agvp", methods=["GET"])
def agvp():
    return handle_api_endpoint("/agvp")

@app.route("/cedula", methods=["GET"])
def cedula():
    return handle_api_endpoint("/cedula")

@app.route("/telp", methods=["GET"])
def telp():
    return handle_api_endpoint("/telp")

@app.route("/fam", methods=["GET"])
def fam():
    return handle_api_endpoint("/fam")

@app.route("/fam2", methods=["GET"])
def fam2():
    return handle_api_endpoint("/fam2")

@app.route("/migrapdf", methods=["GET"])
def migrapdf():
    return handle_api_endpoint("/migrapdf")

@app.route("/con", methods=["GET"])
def con():
    return handle_api_endpoint("/con")

@app.route("/exd", methods=["GET"])
def exd():
    return handle_api_endpoint("/exd")

@app.route("/cor", methods=["GET"])
def cor():
    return handle_api_endpoint("/cor")

@app.route("/dir", methods=["GET"])
def dir():
    return handle_api_endpoint("/dir")

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
        # Generar ID único para esta consulta
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        consulta_id = f"dni_nombres_{timestamp}"
        
        result = run_telegram_command(command, consulta_id, "/dni_nombres")
        
        # Manejar errores
        if result.get("status") == "error":
            status_code = 500
            if "Formato de consulta incorrecto" in result.get("message", ""):
                status_code = 400
            elif "No se encontraron resultados" in result.get("message", ""):
                status_code = 404
            return jsonify(result), status_code
            
        return jsonify(result)
        
    except FutureTimeoutError:
        return jsonify({
            "status": "error", 
            "message": f"Error interno: Timeout excedido ({TIMEOUT_BACKUP}s)."
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
        # Generar ID único para esta consulta
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        consulta_id = f"venezolanos_nombres_{timestamp}"
        
        result = run_telegram_command(command, consulta_id, "/venezolanos_nombres")
        
        # Manejar errores
        if result.get("status") == "error":
            status_code = 500
            if "Formato de consulta incorrecto" in result.get("message", ""):
                status_code = 400
            elif "No se encontraron resultados" in result.get("message", ""):
                status_code = 404
            return jsonify(result), status_code
            
        return jsonify(result)
        
    except FutureTimeoutError:
        return jsonify({
            "status": "error", 
            "message": f"Error interno: Timeout excedido ({TIMEOUT_BACKUP}s)."
        }), 504
    except Exception as e:
        return jsonify({
            "status": "error", 
            "message": f"Error interno: {str(e)}"
        }), 500

# --- Endpoints de mantenimiento ---
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
        "storage_pe": "removed",
        "timestamp": datetime.utcnow().isoformat(),
        "session_configured": bool(SESSION_STRING and SESSION_STRING.strip()),
        "features": {
            "multiple_messages": True,
            "idle_timeout": True,
            "clean_json": True,
            "field_extraction": True,
            "bot_failover": True,
            "bot_blocking": True
        }
    })

@app.route("/debug/bots", methods=["GET"])
def debug_bots():
    """Endpoint de depuración para ver estado de bots"""
    return jsonify({
        "primary_bot": {
            "id": LEDERDATA_BOT_ID,
            "blocked": is_bot_blocked(LEDERDATA_BOT_ID),
            "last_fail": bot_fail_tracker.get(LEDERDATA_BOT_ID, {}),
            "timeout": TIMEOUT_PRIMARY
        },
        "backup_bot": {
            "id": LEDERDATA_BACKUP_BOT_ID,
            "blocked": is_bot_blocked(LEDERDATA_BACKUP_BOT_ID),
            "last_fail": bot_fail_tracker.get(LEDERDATA_BACKUP_BOT_ID, {}),
            "timeout": TIMEOUT_BACKUP
        },
        "block_hours": BOT_BLOCK_HOURS,
        "storage_pe": "removed"
    })

if __name__ == "__main__":
    print("🚀 Iniciando backend en modo SERVERLESS (on-demand)")
    print("📊 Modo optimizado para costos (<5 USD/mes)")
    print("🔗 Telethon se conecta solo cuando recibe consultas")
    print(f"⏰ Timeouts: Principal={TIMEOUT_PRIMARY}s, Respaldo={TIMEOUT_BACKUP}s")
    print(f"🔒 Bloqueo bot fallado: {BOT_BLOCK_HOURS} horas")
    print("✨ MEJORAS IMPLEMENTADAS:")
    print("   ✓ CORREGIDO: Problema de duplicación de comandos")
    print("   ✓ FIX: Cada comando se envía UNA SOLA VEZ por bot")
    print("   ✓ FIX: Handler único para evitar mensajes duplicados")
    print("   ✓ Sistema: Envía al bot principal → Si ANTI-SPAM → Envía al bot de respaldo")
    print("   ✓ Sistema: Si bot principal no responde → Usa solo bot de respaldo")
    print("   ✓ Captura TODOS los mensajes del bot (2, 5, 20+ mensajes)")
    print("   ✓ JSON LIMPIO sin marcas LEDERDATA")
    print("   ✓ Campos extraídos al nivel raíz (dni, nombres, apellidos, etc.)")
    app.run(host="0.0.0.0", port=PORT, debug=False)

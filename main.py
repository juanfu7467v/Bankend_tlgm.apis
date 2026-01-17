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

# --- Configuración de Firebase ---
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "consulta-pe-abf99")
FIREBASE_CLIENT_EMAIL = os.getenv("FIREBASE_CLIENT_EMAIL", "firebase-adminsdk-fbsvc@consulta-pe-abf99.iam.gserviceaccount.com")
FIREBASE_PRIVATE_KEY = os.getenv("FIREBASE_PRIVATE_KEY", "").replace("\\n", "\n")
FIREBASE_STORAGE_BUCKET = os.getenv("FIREBASE_STORAGE_BUCKET", "consulta-pe-abf99.appspot.com")

# Importación condicional de Firebase
try:
    from google.cloud import storage
    firebase_available = True
    print("✅ Firebase SDK disponible")
except ImportError:
    print("⚠️ Firebase SDK no disponible. Instala: pip install google-cloud-storage")
    firebase_available = False
    storage = None

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

# --- Funciones de Firebase Storage ---
def get_storage_client():
    """Obtiene el cliente de Firebase Storage"""
    if not firebase_available:
        return None
    try:
        client = storage.Client()
        return client
    except Exception as e:
        print(f"❌ Error al conectar con Firebase Storage: {e}")
        return None

def archivos_existentes_en_storage(consulta_id: str, tipo_consulta: str = None):
    """
    Revisa si los archivos ya existen en Firebase Storage
    """
    if not firebase_available:
        return None
    
    try:
        client = get_storage_client()
        if not client:
            return None
        
        bucket = client.bucket(FIREBASE_STORAGE_BUCKET)
        
        # Construir el prefijo según tipo de consulta
        if tipo_consulta:
            prefix = f"resultados/{tipo_consulta}/{consulta_id}/"
        else:
            # Buscar en cualquier tipo de consulta
            prefixes = [f"resultados/{tipo}/{consulta_id}/" for tipo in ["DNI_virtual", "SBS", "Denuncias", "general"]]
            all_blobs = []
            for prefix in prefixes:
                blobs = list(bucket.list_blobs(prefix=prefix))
                if blobs:
                    all_blobs.extend(blobs)
            
            if all_blobs:
                return [blob.public_url for blob in all_blobs]
            return None
        
        blobs = list(bucket.list_blobs(prefix=prefix))
        if blobs:
            urls = []
            for blob in blobs:
                # Asegurar que la URL sea pública
                if not blob.public_url:
                    blob.make_public()
                urls.append(blob.public_url)
            return urls
        return None
        
    except Exception as e:
        print(f"⚠️ Error al verificar archivos en Storage: {e}")
        return None

def subir_archivos_a_storage(files_data, consulta_id: str, tipo_consulta: str = None):
    """
    Sube todos los archivos recibidos a Firebase Storage
    files_data: lista de tuplas (filename, file_content, content_type)
    """
    if not firebase_available:
        return None
    
    try:
        client = get_storage_client()
        if not client:
            return None
        
        bucket = client.bucket(FIREBASE_STORAGE_BUCKET)
        urls = []
        
        # Determinar tipo de consulta para organización
        if not tipo_consulta:
            tipo_consulta = determinar_tipo_consulta_por_comando(request.path)
        
        for idx, (filename, file_content, content_type) in enumerate(files_data):
            try:
                # Obtener extensión del archivo
                file_ext = os.path.splitext(filename)[1] if filename else '.jpg'
                if not file_ext or file_ext == '.':
                    # Determinar extensión basada en content_type
                    if content_type:
                        if 'pdf' in content_type.lower():
                            file_ext = '.pdf'
                        elif 'png' in content_type.lower():
                            file_ext = '.png'
                        elif 'jpeg' in content_type.lower() or 'jpg' in content_type.lower():
                            file_ext = '.jpg'
                        else:
                            file_ext = '.bin'
                
                # Nombre único por archivo
                safe_filename = re.sub(r'[^\w\-\.]', '_', filename or f"file_{idx}")
                unique_filename = f"{consulta_id}_{idx}_{safe_filename}"
                
                # Ruta organizada en Storage
                storage_path = f"resultados/{tipo_consulta}/{consulta_id}/{unique_filename}"
                
                # Subir archivo
                blob = bucket.blob(storage_path)
                
                # Configurar metadata
                blob.content_type = content_type or mimetypes.guess_type(filename)[0] if filename else 'application/octet-stream'
                
                # Subir contenido
                blob.upload_from_string(file_content)
                
                # Hacer público
                blob.make_public()
                
                urls.append({
                    "url": blob.public_url,
                    "filename": unique_filename,
                    "type": content_type or "unknown"
                })
                
                print(f"✅ Archivo subido a Firebase: {storage_path}")
                
            except Exception as e:
                print(f"⚠️ Error subiendo archivo {idx}: {e}")
                continue
        
        return urls
        
    except Exception as e:
        print(f"❌ Error general subiendo archivos a Storage: {e}")
        return None

def determinar_tipo_consulta_por_comando(comando_path: str):
    """Determina el tipo de consulta basado en el endpoint"""
    comando = comando_path.lstrip('/').split('/')[0]
    
    # Mapeo de comandos a tipos de consulta
    tipo_por_comando = {
        'dni': 'DNI_virtual',
        'dnif': 'DNI_virtual',
        'dnidb': 'DNI_virtual',
        'dnifdb': 'DNI_virtual',
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

# --- Función Principal para Conexión On-Demand (MEJORADA para capturar TODOS los mensajes) ---
async def send_telegram_command(command: str, consulta_id: str = None, endpoint_path: str = None):
    """
    Función on-demand con soporte para múltiples archivos y Firebase Storage
    MEJORADA: Captura TODOS los mensajes y archivos del bot
    """
    client = None
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
        
        # 5. Determinar tipo de consulta para Firebase
        tipo_consulta = determinar_tipo_consulta_por_comando(endpoint_path) if endpoint_path else "general"
        
        # 6. Generar consulta_id si no se proporciona
        if not consulta_id:
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            consulta_id = f"{tipo_consulta}_{dni or 'unknown'}_{timestamp}" if dni else f"{tipo_consulta}_{timestamp}"
        
        # 7. Verificar si ya existen archivos en Firebase Storage
        urls_existentes = archivos_existentes_en_storage(consulta_id, tipo_consulta)
        if urls_existentes and firebase_available:
            print(f"✅ Archivos encontrados en Firebase Storage para consulta {consulta_id}")
            # Retornar estructura similar a la que espera el bot
            return {
                "status": "ok",
                "message": "Consulta recuperada de cache",
                "fields": {"dni": dni} if dni else {},
                "urls": {f"file_{i}": url for i, url in enumerate(urls_existentes)},
                "from_cache": True,
                "consulta_id": consulta_id
            }
        
        # 8. Determinar orden de bots según bloqueos
        bots_order = []
        
        if not is_bot_blocked(LEDERDATA_BOT_ID):
            bots_order.append(LEDERDATA_BOT_ID)
        
        if not is_bot_blocked(LEDERDATA_BACKUP_BOT_ID):
            bots_order.append(LEDERDATA_BACKUP_BOT_ID)
        
        if not bots_order:
            raise Exception("Todos los bots están temporalmente bloqueados.")
        
        print(f"🔍 Orden de intentos: {bots_order}")
        
        # 9. Variables para capturar respuestas
        all_received_messages = []  # Para múltiples mensajes del MISMO bot
        all_files_data = []  # Para almacenar archivos para Firebase
        stop_collecting = asyncio.Event()
        
        # Variable para trackear última actividad
        last_message_time = [time.time()]  # Usamos lista para poder modificar desde el handler
        
        # 10. Handler temporal para capturar respuestas (MEJORADO)
        @client.on(events.NewMessage(incoming=True))
        async def temp_handler(event):
            # Si ya tenemos respuesta completa, ignorar nuevos mensajes
            if stop_collecting.is_set():
                return
                
            try:
                # Verificar si el mensaje viene de un bot que estamos probando
                sender_is_current_bot = False
                current_bot_entity = None
                
                for bot_id in bots_order:
                    try:
                        entity = await client.get_entity(bot_id)
                        if event.sender_id == entity.id:
                            sender_is_current_bot = True
                            current_bot_entity = entity
                            break
                    except:
                        continue
                
                if not sender_is_current_bot:
                    return
                
                # Actualizar tiempo de última actividad
                last_message_time[0] = time.time()
                
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
                            try:
                                file_ext = '.file'
                                is_photo = False
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
                                    is_photo = True
                                    content_type = 'image/jpeg'
                                    
                                dni_part = f"_{cleaned['fields'].get('dni')}" if cleaned["fields"].get("dni") else ""
                                type_part = f"_{cleaned['fields'].get('photo_type')}" if cleaned['fields'].get('photo_type') else ""
                                unique_filename = f"{timestamp_str}_{event.message.id}{dni_part}{type_part}_{i}{file_ext}"
                                
                                # Descargar archivo
                                saved_path = await client.download_media(event.message, file=os.path.join(DOWNLOAD_DIR, unique_filename))
                                
                                # Leer contenido para Firebase
                                if os.path.exists(saved_path):
                                    with open(saved_path, 'rb') as f:
                                        file_content = f.read()
                                    
                                    # Guardar para subir a Firebase
                                    all_files_data.append((unique_filename, file_content, content_type))
                                
                                # URL local
                                url_obj = {
                                    "url": f"{PUBLIC_URL}/files/{os.path.basename(saved_path)}", 
                                    "type": cleaned['fields'].get('photo_type', 'image' if is_photo else 'document'),
                                    "text_context": raw_text.split('\n')[0].strip()
                                }
                                msg_urls.append(url_obj)
                                
                            except Exception as e:
                                print(f"Error procesando archivo {i}: {e}")
                                continue
                
                msg_obj = {
                    "chat_id": getattr(event, "chat_id", None),
                    "from_id": event.sender_id,
                    "date": event.message.date.isoformat() if getattr(event, "message", None) else datetime.utcnow().isoformat(),
                    "message": cleaned["text"],
                    "fields": cleaned["fields"],
                    "urls": msg_urls,
                    "bot_id": current_bot_entity.id if current_bot_entity else None
                }
                
                all_received_messages.append(msg_obj)
                print(f"📥 Mensaje recibido de bot: {len(msg_obj['message'])} chars, {len(msg_urls)} archivos")
                
                # Verificar si es error de formato, pero NO detener inmediatamente
                # El bot podría seguir enviando más mensajes
                if ("Por favor, usa el formato correcto" in msg_obj["message"]):
                    print("⚠️ Error de formato detectado, pero continuamos escuchando por si hay más")
                
            except Exception as e:
                print(f"Error en handler temporal: {e}")
        
        # 11. Intentar SECUENCIALMENTE con cada bot
        for attempt, current_bot_id in enumerate(bots_order, 1):
            print(f"\n🎯 Intento {attempt}: Enviando a {current_bot_id}")
            print(f"   Comando: {command}")
            
            # Resetear para este intento
            all_received_messages = []
            all_files_data = []
            stop_collecting.clear()
            last_message_time[0] = time.time()
            
            try:
                # Determinar timeout según bot
                timeout = TIMEOUT_PRIMARY if current_bot_id == LEDERDATA_BOT_ID else TIMEOUT_BACKUP
                print(f"   Timeout configurado: {timeout}s")
                
                # Enviar comando
                await client.send_message(current_bot_id, command)
                
                # Timer para múltiples mensajes
                start_time = time.time()
                
                # --- LÓGICA DE ESPERA MEJORADA (Idle Timeout) ---
                try:
                    while True:
                        elapsed_total = time.time() - start_time
                        silence_duration = time.time() - last_message_time[0]
                        
                        # Si ya recibimos algo, esperamos un silencio de 4 segundos para cerrar
                        if len(all_received_messages) > 0:
                            if silence_duration > 4.0: 
                                print(f"✅ Silencio detectado ({silence_duration:.1f}s). Total mensajes: {len(all_received_messages)}")
                                break
                        
                        # Si no ha llegado nada y pasamos el timeout total
                        if elapsed_total > timeout:
                            if len(all_received_messages) == 0:
                                raise asyncio.TimeoutError("El bot no respondió a tiempo")
                            else:
                                break  # Cerramos con lo que tengamos
                                
                        await asyncio.sleep(0.5)
                
                except asyncio.TimeoutError:
                    # TIMEOUT - este bot no respondió
                    print(f"⏰ TIMEOUT: {current_bot_id} no respondió en {timeout}s")
                    
                    # Si es el bot principal, bloquearlo por 4 horas
                    if current_bot_id == LEDERDATA_BOT_ID:
                        record_bot_failure(LEDERDATA_BOT_ID)
                        print(f"🔒 Bot principal bloqueado por {BOT_BLOCK_HOURS} horas")
                    
                    # Si hay más bots para probar, continuar
                    if attempt < len(bots_order):
                        print(f"🔄 Pasando al siguiente bot...")
                        continue
                    else:
                        # No hay más bots, lanzar error
                        raise Exception(f"Ningún bot respondió. Último timeout: {timeout}s")
                
                # 12. SI LLEGAMOS AQUÍ, EL BOT RESPONDIÓ
                print(f"✅ {current_bot_id} respondió con {len(all_received_messages)} mensajes")
                
                # Marcar para detener cualquier espera futura
                stop_collecting.set()
                
                # Procesar respuestas recibidas
                if all_received_messages:
                    # Verificar si hay error de formato en cualquier mensaje
                    format_error_detected = False
                    for msg in all_received_messages:
                        if "Por favor, usa el formato correcto" in msg.get("message", ""):
                            format_error_detected = True
                            break
                    
                    if format_error_detected:
                        return {
                            "status": "error_bot_format", 
                            "message": "Formato de consulta incorrecto. Verifica los parámetros enviados.",
                            "bot_used": current_bot_id
                        }
                    
                    # Verificar si es "no encontrado" en cualquier mensaje
                    not_found_detected = False
                    for msg in all_received_messages:
                        if msg.get("fields", {}).get("not_found", False):
                            not_found_detected = True
                            break
                    
                    if not_found_detected:
                        return {
                            "status": "error_not_found", 
                            "message": "No se encontraron resultados para dicha consulta. Intenta con otro dato.",
                            "bot_used": current_bot_id
                        }
                    
                    # --- CONSOLIDAR TODO EL CONTENIDO (MEJORADO) ---
                    final_text = []
                    final_urls = []  # Cambiar a lista para no perder nada
                    all_fields = {}
                    
                    for msg in all_received_messages:
                        # Consolidar texto
                        if msg.get("message"):
                            final_text.append(msg["message"])
                        
                        # Consolidar fields (sin sobrescribir)
                        if msg.get("fields"):
                            for key, value in msg["fields"].items():
                                if key not in all_fields:
                                    all_fields[key] = value
                        
                        # Extraer TODAS las URLs de este mensaje
                        if isinstance(msg.get("urls"), list):
                            for url_obj in msg["urls"]:
                                # Agregar URL a la lista (no sobrescribir)
                                final_urls.append({
                                    "url": url_obj.get("url"),
                                    "type": url_obj.get("type", "unknown"),
                                    "text_context": url_obj.get("text_context", "")
                                })
                    
                    # Crear JSON de respuesta final
                    final_json = {
                        "status": "ok",
                        "message": "\n\n---\n\n".join(final_text) if final_text else "Consulta procesada",
                        "fields": all_fields,
                        "urls": final_urls,  # Ahora es una lista completa de todos los archivos
                        "consulta_id": consulta_id,
                        "bot_used": current_bot_id,
                        "total_messages": len(all_received_messages),
                        "total_files": len(final_urls)
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
                    
                    # 13. SUBIR ARCHIVOS A FIREBASE STORAGE
                    if all_files_data and firebase_available:
                        try:
                            firebase_urls = subir_archivos_a_storage(all_files_data, consulta_id, tipo_consulta)
                            if firebase_urls:
                                # Agregar URLs de Firebase a la respuesta
                                firebase_url_list = []
                                for url_info in firebase_urls:
                                    firebase_url_list.append({
                                        "url": url_info["url"],
                                        "type": "firebase_storage",
                                        "filename": url_info["filename"]
                                    })
                                
                                # Combinar URLs locales con Firebase
                                final_json["urls"].extend(firebase_url_list)
                                final_json["firebase_uploaded"] = True
                                final_json["firebase_files"] = len(firebase_urls)
                                print(f"✅ {len(firebase_urls)} archivos subidos a Firebase Storage")
                        except Exception as e:
                            print(f"⚠️ Error subiendo a Firebase (no crítico): {e}")
                            final_json["firebase_uploaded"] = False
                    
                    # LIMPIAR handler de eventos para evitar fugas
                    client.remove_event_handler(temp_handler)
                    
                    return final_json
                else:
                    # Caso raro: sin mensajes recibidos
                    raise Exception("No se recibieron mensajes del bot")
                    
            except UserBlockedError:
                print(f"❌ {current_bot_id} bloqueado por el usuario")
                record_bot_failure(current_bot_id)
                
                if attempt < len(bots_order):
                    continue
                else:
                    raise Exception("Todos los bots están bloqueados")
                    
            except Exception as e:
                print(f"❌ Error con {current_bot_id}: {str(e)[:100]}")
                
                # Si es error grave y es el bot principal, bloquearlo
                if "blocked" in str(e).lower() and current_bot_id == LEDERDATA_BOT_ID:
                    record_bot_failure(LEDERDATA_BOT_ID)
                
                if attempt < len(bots_order):
                    print(f"🔄 Intentando con siguiente bot...")
                    continue
                else:
                    raise e
        
        # No deberíamos llegar aquí
        raise Exception("Flujo inesperado - no se obtuvo respuesta")
        
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error al procesar comando: {str(e)}"
        }
        
    finally:
        # 14. Limpieza final
        if client:
            try:
                await client.disconnect()
                print("🔌 Cliente desconectado")
            except:
                pass
        
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

# --- Rutas HTTP ---

@app.route("/")
def root():
    return jsonify({
        "status": "ok",
        "message": "Gateway API para LEDER DATA Bot activo (Modo Serverless con Firebase).",
        "mode": "serverless",
        "firebase_available": firebase_available,
        "cost_optimized": True,
        "version": "3.0 - Captura múltiple mejorada"
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
        "firebase_available": firebase_available,
        "firebase_bucket": FIREBASE_STORAGE_BUCKET if firebase_available else "no configurado",
        "bot_status": bot_status,
        "mode": "on-demand",
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
        "firebase": firebase_available,
        "timestamp": datetime.utcnow().isoformat(),
        "session_configured": bool(SESSION_STRING and SESSION_STRING.strip()),
        "features": {
            "multiple_messages": True,
            "idle_timeout": True,
            "firebase_storage": firebase_available
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
        "firebase": {
            "available": firebase_available,
            "bucket": FIREBASE_STORAGE_BUCKET if firebase_available else None
        }
    })

# --- Nuevos endpoints para Firebase ---
@app.route("/firebase/test", methods=["GET"])
def test_firebase():
    """Endpoint para probar la conexión a Firebase"""
    if not firebase_available:
        return jsonify({
            "status": "error",
            "message": "Firebase SDK no disponible. Instala: pip install google-cloud-storage"
        }), 500
    
    try:
        client = get_storage_client()
        if not client:
            return jsonify({
                "status": "error",
                "message": "No se pudo crear cliente de Firebase Storage"
            }), 500
        
        # Intentar listar buckets
        buckets = list(client.list_buckets())
        
        return jsonify({
            "status": "ok",
            "message": "Conexión a Firebase Storage exitosa",
            "project": client.project,
            "buckets_count": len(buckets),
            "configured_bucket": FIREBASE_STORAGE_BUCKET
        })
        
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"Error conectando a Firebase Storage: {str(e)}"
        }), 500

@app.route("/firebase/files/<consulta_id>", methods=["GET"])
def get_firebase_files(consulta_id):
    """Obtener archivos de una consulta específica desde Firebase"""
    if not firebase_available:
        return jsonify({
            "status": "error",
            "message": "Firebase no disponible"
        }), 500
    
    try:
        urls = archivos_existentes_en_storage(consulta_id)
        if urls:
            return jsonify({
                "status": "ok",
                "consulta_id": consulta_id,
                "files_count": len(urls),
                "urls": urls
            })
        else:
            return jsonify({
                "status": "not_found",
                "message": f"No se encontraron archivos para la consulta {consulta_id}"
            }), 404
            
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"Error obteniendo archivos: {str(e)}"
        }), 500

if __name__ == "__main__":
    print("🚀 Iniciando backend en modo SERVERLESS (on-demand) con Firebase")
    print("📊 Modo optimizado para costos (<5 USD/mes)")
    print("🔗 Telethon se conecta solo cuando recibe consultas")
    print(f"⏰ Timeouts: Principal={TIMEOUT_PRIMARY}s, Respaldo={TIMEOUT_BACKUP}s")
    print(f"🔒 Bloqueo bot fallado: {BOT_BLOCK_HOURS} horas")
    print(f"🔥 Firebase Storage: {'CONECTADO' if firebase_available else 'NO DISPONIBLE'}")
    if firebase_available:
        print(f"   Bucket: {FIREBASE_STORAGE_BUCKET}")
    print("✨ MEJORAS IMPLEMENTADAS:")
    print("   ✓ Captura TODOS los mensajes del bot (2, 5, 20+ mensajes)")
    print("   ✓ Lógica de espera con idle timeout (4 segundos de silencio)")
    print("   ✓ URLs como lista (no se sobrescriben archivos del mismo tipo)")
    print("   ✓ Handler más permisivo (no se detiene inmediatamente con errores)")
    print("   ✓ Consolidación completa de texto y archivos")
    print("   ✓ Todos los comandos preservados")
    app.run(host="0.0.0.0", port=PORT, debug=False)

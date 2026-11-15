"""
handlers/upload.py — Orquestador de subidas BitZero.

Mejoras:
  - Si LOG_CHANNEL_ID está configurado, envía un mensaje de log al canal
    por cada subida completada (con URL BitZero, usuario, revista, tamaño).
  - Cache de uploaders por revista con invalidación.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Dict

from pyrogram.errors import RPCError

from config import config
from database import db
from storage import storage
from uploader import RevistaUploader
from url_generator import URLGenerator
from utils import format_size
from plans import PLANS, get_active_plan

logger = logging.getLogger(__name__)

# Cache de uploaders por revista
_journal_uploaders: Dict[str, RevistaUploader] = {}


def invalidate_uploader(revista_id: str) -> None:
    """Invalida el uploader cacheado para que se recree con la nueva config."""
    _journal_uploaders.pop(revista_id, None)


async def get_or_create_uploader(revista: Dict) -> RevistaUploader:
    """Devuelve uploader cacheado o crea uno nuevo con la config actual."""
    rev_id = revista['rev_id']
    if rev_id in _journal_uploaders:
        return _journal_uploaders[rev_id]
    uploader = RevistaUploader(
        username=revista['username'],
        password=revista['password'],
        submission_id=revista['submission_id'],
        base_url=revista['base_url'],
        contexto=revista['contexto'],
        bitzero_mode=revista.get('bitzero_mode', 0),
        encryption_key=revista.get('encryption_key'),
    )
    _journal_uploaders[rev_id] = uploader
    return uploader


async def _log_upload_to_channel(client, user_id: int, revista: Dict,
                                  uploader: RevistaUploader,
                                  uploaded_count: int, total_files: int,
                                  all_uploaded: list, bitzero_url: str,
                                  is_multi: bool, original_names_list: list,
                                  status: str) -> None:
    """Envía un mensaje de log al canal de log con el resumen de la subida."""
    if not config.LOG_CHANNEL_ID:
        return
    try:
        user_display = original_names_list[0] if original_names_list else "N/A"
        text = (
            f"📤 **Subida BitZero — {status.upper()}**\n\n"
            f"👤 **Usuario ID:** `{user_id}`\n"
            f"📚 **Revista:** {revista['nombre']} (`{revista['rev_id']}`)\n"
            f"🆔 **Submission:** `{revista['submission_id']}`\n"
            f"📦 **Archivos:** {uploaded_count}/{total_files}"
            + (f" (empaquetado .tar de {len(original_names_list)})" if is_multi else "")
            + f"\n🔗 **Partes:** {len(all_uploaded)}\n"
            f"🔢 **Modo BitZero:** {uploader.bitzero_mode}\n"
            f"📅 **Fecha:** {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        total_orig = sum(f.get('original_size', f.get('size', 0)) for f in all_uploaded)
        total_up = sum(f.get('size', 0) for f in all_uploaded)
        text += f"💾 **Tamaño original:** {format_size(total_orig)}\n"
        text += f"📤 **Tamaño subido:** {format_size(total_up)}\n"
        if bitzero_url:
            text += f"\n🔗 **URL BitZero:**\n`{bitzero_url}`\n"
        if uploader.encryption_key:
            text += f"\n🔑 **Clave:** `{uploader.encryption_key}`"
        await client.send_message(
            chat_id=config.LOG_CHANNEL_ID,
            text=text,
            disable_web_page_preview=True
        )
    except RPCError as e:
        logger.warning(f"No se pudo enviar log de subida al canal: {e}")
    except Exception as e:
        logger.warning(f"Error inesperado en log de subida: {e}")


async def perform_upload_with_bitzero(client, message, uploader: RevistaUploader,
                                       revista: Dict, user_id: int,
                                       file_paths: list[str] | None = None) -> None:
    """Ejecuta la subida completa de los archivos del usuario a la revista.
    Genera URL BitZero al final y actualiza el historial en DB.
    """
    # Aplicar límites del plan antes de iniciar cualquier subida.
    user_record = await db.get_user(user_id) or {"plan": "basico"}
    plan_key = get_active_plan(user_record)
    plan = PLANS[plan_key]
    if file_paths is not None:
        requested_paths = file_paths
    else:
        requested_paths = [f['path'] for f in storage.list_user_files(user_id, sort_by='modified_desc')]
    if not requested_paths:
        await message.edit_text("📭 **No tienes archivos para subir.**")
        return
    max_size = plan["max_file_bytes"]
    oversized = [(os.path.basename(p), os.path.getsize(p)) for p in requested_paths
                 if os.path.getsize(p) > max_size] if max_size else []
    if oversized:
        name, size = oversized[0]
        await message.edit_text(
            f"❌ **Límite del plan {plan['name']} superado.**\n"
            f"Archivo: `{name}` ({format_size(size)}).\n"
            f"Máximo permitido: {format_size(max_size)} por archivo.")
        return
    used_today = await db.get_daily_uploads(user_id)
    daily_limit = plan["daily_uploads"]
    if daily_limit is not None and used_today + len(requested_paths) > daily_limit:
        await message.edit_text(
            f"❌ **Has alcanzado el límite diario del plan {plan['name']}.**\n"
            f"Usadas: {used_today}/{daily_limit}. Archivos solicitados: {len(requested_paths)}.")
        return

    if not uploader.ensure_logged_in():
        await message.edit_text("❌ **Error de autenticación.** No se pudo iniciar sesión.")
        await db.update_revista_login_status(revista['rev_id'], False)
        return
    await db.update_revista_login_status(revista['rev_id'], True)

    uploader.uploaded_files = []

    # Listar archivos del usuario (aislamiento)
    files_to_upload = requested_paths

    if not files_to_upload:
        await message.edit_text("📭 **No tienes archivos para subir.**")
        return

    total_files = len(files_to_upload)
    uploaded_count = 0
    all_uploaded: list = []

    # ── Múltiples archivos → empaquetar en .tar ─────────────────────
    if total_files > 1:
        await message.edit_text(
            f"📦 **Empaquetando {total_files} archivos**\n\n"
            f"⏳ Generando .tar..."
        )
        tar_path = storage.package_multiple_files(files_to_upload, user_id)
        files_to_upload_processed = [tar_path]
        original_names_list = [os.path.basename(f) for f in files_to_upload]
        is_multi = True
    else:
        files_to_upload_processed = files_to_upload
        original_names_list = [os.path.basename(files_to_upload[0])]
        is_multi = False
        tar_path = None

    # ── Subir cada archivo ──────────────────────────────────────────
    for idx, file_path in enumerate(files_to_upload_processed, 1):
        file_name = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)

        bitzero_info = {
            0: '❌', 1: '🖼️ PNG', 2: '🌐 HTML', 3: '📦 ZIP'
        }.get(uploader.bitzero_mode, '❌')

        await message.edit_text(
            f"📤 **Subiendo**\n\n"
            f"📂 Progreso: {idx}/{len(files_to_upload_processed)}\n"
            f"📄 {file_name}\n"
            f"💾 {format_size(file_size)}\n"
            f"🔐 BitZero: {bitzero_info}\n"
            f"⏳ Procesando..."
        )

        if file_size > uploader.chunk_size:
            uploaded = uploader.upload_chunked_file(file_path, user_id)
        else:
            result = uploader.upload_file(file_path, user_id=user_id)
            uploaded = [result] if result else []

        if uploaded:
            uploaded_count += 1
            all_uploaded.extend(uploaded)
            await message.edit_text(
                f"✅ **Subido**\n\n"
                f"📂 Progreso: {idx}/{len(files_to_upload_processed)}\n"
                f"📄 {file_name}\n"
                f"🔗 Partes: {len(uploaded)}"
            )
        else:
            await message.edit_text(
                f"⚠️ **Error subiendo** {file_name}\n"
                f"⏳ Continuando con el siguiente..."
            )
        await asyncio.sleep(1)

    # ── Limpiar tar temporal si se creó ─────────────────────────────
    if tar_path and os.path.exists(tar_path):
        try:
            os.remove(tar_path)
        except OSError:
            pass

    if not all_uploaded:
        await message.edit_text("❌ **Subida fallida.** No se subió ningún archivo.")
        await db.log_upload(
            user_id=user_id, revista_id=revista['rev_id'],
            submission_id=revista['submission_id'],
            original_name=original_names_list[0] if original_names_list else "unknown",
            original_size=0, uploaded_size=0,
            file_ids=[], bitzero_mode=uploader.bitzero_mode,
            bitzero_url=None, encryption_key=uploader.encryption_key,
            status='failed'
        )
        await _log_upload_to_channel(
            client, user_id, revista, uploader, 0, total_files,
            [], "", is_multi, original_names_list, "failed"
        )
        return

    # ── Generar URL BitZero ────────────────────────────────────────
    bitzero_url = ""
    if uploader.bitzero_mode > 0:
        total_original_size = sum(
            f.get('original_size', f.get('size', 0)) for f in all_uploaded
        )
        if is_multi:
            original_name = URLGenerator.build_multi_filename(original_names_list)
        else:
            original_name = original_names_list[0]

        bitzero_url = uploader.generate_bitzero_url(
            original_name=original_name,
            file_size=total_original_size,
        )

    # ── Registrar en DB ─────────────────────────────────────────────
    total_uploaded_size = sum(f.get('size', 0) for f in all_uploaded)
    total_original_size_db = sum(
        f.get('original_size', f.get('size', 0)) for f in all_uploaded
    )
    status = 'success' if uploaded_count == len(files_to_upload_processed) else 'partial'
    await db.log_upload(
        user_id=user_id, revista_id=revista['rev_id'],
        submission_id=revista['submission_id'],
        original_name=original_names_list[0] if original_names_list else "multi",
        original_size=total_original_size_db,
        uploaded_size=total_uploaded_size,
        file_ids=[str(f['id']) for f in all_uploaded],
        bitzero_mode=uploader.bitzero_mode,
        bitzero_url=bitzero_url,
        encryption_key=uploader.encryption_key,
        status=status
    )

    # Cada archivo original subido cuenta como una subida diaria.
    await db.increment_daily_uploads(user_id, uploaded_count if not is_multi else len(original_names_list))

    # ── Enviar al canal de LOG ─────────────────────────────────────
    await _log_upload_to_channel(
        client, user_id, revista, uploader, uploaded_count, total_files,
        all_uploaded, bitzero_url, is_multi, original_names_list, status
    )

    # ── Mensaje final ───────────────────────────────────────────────
    text = f"✅ **Subida Completada**\n\n"
    text += f"📚 **Revista:** {revista['nombre']}\n"
    text += f"📦 **Archivos procesados:** {uploaded_count}/{len(files_to_upload_processed)}\n"
    text += f"🔗 **Partes subidas:** {len(all_uploaded)}\n"
    if is_multi:
        text += f"🗃️ **Empaquetado en .tar** ({total_files} archivos)\n"

    if uploader.bitzero_mode > 0:
        text += f"🔐 **BitZero:** ✅ Activado (Modo {uploader.bitzero_mode})\n"
        if uploader.encryption_key:
            text += f"🔑 **Encriptación:** ✅ Clave configurada\n"
        if is_multi:
            text += f"📝 **Manifiesto:** {len(original_names_list)} archivos en .tar\n"
    else:
        text += f"🔐 **BitZero:** ❌ Desactivado\n"
    text += "\n"

    if bitzero_url:
        text += f"🔗 **URL BitZero:**\n`{bitzero_url}`\n\n"
        text += "📥 **Descargar con:**\n"
        text += "```bash\npython3 bitzero.py \"URL\"\n```\n"
        if uploader.bitzero_mode == 3 and uploader.encryption_key:
            text += f"🔐 **Contraseña ZIP:** `{uploader.encryption_key}`\n"

    text += f"\n👨‍💻 **Desarrollador:** {config.DEVELOPER_HANDLE}"
    await message.edit_text(text)

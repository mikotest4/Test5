import os
import re
import time
import shutil
import asyncio
import logging
from datetime import datetime
from PIL import Image
from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import InputMediaDocument, Message
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser
from plugins.antinsfw import check_anti_nsfw
from helper.utils import progress_for_pyrogram, humanbytes, convert
from helper.database import DARKXSIDE78
from config import Config
import random
import string
import aiohttp
from datetime import datetime, timedelta
import pytz
from asyncio import Semaphore

renaming_operations = {}
active_sequences = {}
message_ids = {}
USER_SEMAPHORES = {}
USER_LIMITS = {}

# Function to detect video quality from filename
def detect_quality(file_name):
    quality_order = {"480p": 1, "720p": 2, "1080p": 3}
    match = re.search(r"(480p|720p|1080p)", file_name)
    return quality_order.get(match.group(1), 4) if match else 4  # Default priority = 4

@Client.on_message(filters.command("ssequence") & filters.private)
async def start_sequence(client, message: Message):
    user_id = message.from_user.id
    if user_id in active_sequences:
        await message.reply_text("A sequence is already active! Use /esequence to end it.")
    else:
        active_sequences[user_id] = []
        message_ids[user_id] = []
        msg = await message.reply_text("Sequence started! Send your files.")
        message_ids[user_id].append(msg.id)

@Client.on_message(filters.command("esequence") & filters.private)
async def end_sequence(client, message: Message):
    user_id = message.from_user.id
    if user_id not in active_sequences:
        await message.reply_text("No active sequence found!")
        return

    file_list = active_sequences.pop(user_id, [])
    delete_messages = message_ids.pop(user_id, [])

    if not file_list:
        await message.reply_text("No files were sent in this sequence!")
        return

    # Sorting files based on quality
    sorted_files = sorted(file_list, key=lambda f: (
        detect_quality(f["file_name"]) if "file_name" in f else 4,
        f["file_name"] if "file_name" in f else ""
    ))

    await message.reply_text(f"Sequence ended! Sending {len(sorted_files)} files back...")

    # Sending sorted files
    for file in sorted_files:
        await client.send_document(message.chat.id, file["file_id"], caption=f"**{file.get('file_name', '')}**",)

    # Deleting old messages (file added messages)
    try:
        await client.delete_messages(chat_id=message.chat.id, message_ids=delete_messages)
    except Exception as e:
        print(f"Error deleting messages: {e}")
        
# Pattern 1: S01E02 or S01EP02
pattern1 = re.compile(r'S(\d+)(?:E|EP)(\d+)')
# Pattern 2: S01 E02 or S01 EP02 or S01 - E01 or S01 - EP02
pattern2 = re.compile(r'S(\d+)\s*(?:E|EP|-\s*EP)(\d+)')
# Pattern 3: Episode Number After "E" or "EP"
pattern3 = re.compile(r'(?:[([<{]?\s*(?:E|EP)\s*(\d+)\s*[)\]>}]?)')
# Pattern 3_2: episode number after - [hyphen]
pattern3_2 = re.compile(r'(?:\s*-\s*(\d+)\s*)')
# Pattern 4: S2 09 ex.
pattern4 = re.compile(r'S(\d+)[^\d]*(\d+)', re.IGNORECASE)
# Pattern X: Standalone Episode Number
patternX = re.compile(r'(\d+)')
#QUALITY PATTERNS 
# Pattern 5: 3-4 digits before 'p' as quality
pattern5 = re.compile(r'\b(?:.*?(\d{3,4}[^\dp]*p).*?|.*?(\d{3,4}p))\b', re.IGNORECASE)
# Pattern 6: Find 4k in brackets or parentheses
pattern6 = re.compile(r'[([<{]?\s*4k\s*[)\]>}]?', re.IGNORECASE)
# Pattern 7: Find 2k in brackets or parentheses
pattern7 = re.compile(r'[([<{]?\s*2k\s*[)\]>}]?', re.IGNORECASE)
# Pattern 8: Find HdRip without spaces
pattern8 = re.compile(r'[([<{]?\s*HdRip\s*[)\]>}]?|\bHdRip\b', re.IGNORECASE)
# Pattern 9: Find 4kX264 in brackets or parentheses
pattern9 = re.compile(r'[([<{]?\s*4kX264\s*[)\]>}]?', re.IGNORECASE)
# Pattern 10: Find 4kx265 in brackets or parentheses
pattern10 = re.compile(r'[([<{]?\s*4kx265\s*[)\]>}]?', re.IGNORECASE)

def extract_quality(filename):
    # Try Quality Patterns
    match5 = re.search(pattern5, filename)
    if match5:
        quality5 = match5.group(1) or match5.group(2)  # Extracted quality from both patterns
        return quality5

    match6 = re.search(pattern6, filename)
    if match6:
        quality6 = "4k"
        return quality6

    match7 = re.search(pattern7, filename)
    if match7:
        quality7 = "2k"
        return quality7

    match8 = re.search(pattern8, filename)
    if match8:
        quality8 = "HdRip"
        return quality8

    match9 = re.search(pattern9, filename)
    if match9:
        quality9 = "4kX264"
        return quality9

    match10 = re.search(pattern10, filename)
    if match10:
        quality10 = "4kx265"
        return quality10    

    # Return "Unknown" if no pattern matches
    unknown_quality = "Unknown"
    return unknown_quality
    

def extract_episode_number(filename):    
    # Try Pattern 1
    match = re.search(pattern1, filename)
    if match:
        return match.group(2)  # Extracted episode number
    
    # Try Pattern 2
    match = re.search(pattern2, filename)
    if match:
        return match.group(2)  # Extracted episode number

    # Try Pattern 3
    match = re.search(pattern3, filename)
    if match:
        return match.group(1)  # Extracted episode number

    # Try Pattern 3_2
    match = re.search(pattern3_2, filename)
    if match:
        return match.group(1)  # Extracted episode number
        
    # Try Pattern 4
    match = re.search(pattern4, filename)
    if match:
        return match.group(2)  # Extracted episode number

    # Try Pattern X
    match = re.search(patternX, filename)
    if match:
        return match.group(1)  # Extracted episode number
        
    # Return None if no pattern matches
    return None

@Client.on_message(filters.private & (filters.document | filters.video | filters.audio))
async def auto_rename_files(client, message: Message):
    user_id = message.from_user.id
    user = message.from_user

    # Initialize file_id and file_name early
    file_id = None
    file_name = None

    # Check if the user is an admin.
    is_admin = False
    if hasattr(Config, "ADMINS") and user_id in Config.ADMINS:
        is_admin = True

    # Check premium status
    user_data = await DARKXSIDE78.col.find_one({"_id": int(user_id)})  
    is_premium = user_data.get("is_premium", False) if user_data else False
    premium_expiry = user_data.get("premium_expiry")
    if is_premium and premium_expiry:
        if datetime.now() < premium_expiry:
            is_premium = True
        else:
            await DARKXSIDE78.col.update_one(
                {"_id": user_id},
                {"$set": {"is_premium": False, "premium_expiry": None}}
            )
            is_premium = False

    if not is_premium:
        current_tokens = user_data.get("token", 69) if user_data else 69
        if current_tokens <= 0:
            await message.reply_text(
                "❌ You've run out of tokens!\n\n"
                "Generate more tokens by completing short links using /gentoken command.",
            )
            return

        # Deduct token
        new_tokens = current_tokens - 1
        await DARKXSIDE78.col.update_one(
            {"_id": user_id},
            {"$set": {"token": new_tokens}}
        )

    concurrency_limit = 3 if (is_admin or is_premium) else 3
    if user_id in USER_LIMITS:
        if USER_LIMITS[user_id] != concurrency_limit:
            USER_SEMAPHORES[user_id] = asyncio.Semaphore(concurrency_limit)
            USER_LIMITS[user_id] = concurrency_limit
    else:
        USER_LIMITS[user_id] = concurrency_limit
        USER_SEMAPHORES[user_id] = asyncio.Semaphore(concurrency_limit)

    semaphore = USER_SEMAPHORES[user_id]

    async with semaphore:
        if user_id in active_sequences:
            # Ensure file_id and file_name are defined
            if message.document:
                file_id = message.document.file_id
                file_name = message.document.file_name
            elif message.video:
                file_id = message.video.file_id
                file_name = getattr(message.video, 'file_name', None) or f"video_{file_id[:8]}.mp4"
            elif message.audio:
                file_id = message.audio.file_id
                file_name = getattr(message.audio, 'file_name', None) or f"audio_{file_id[:8]}.mp3"

            file_info = {
                "file_id": file_id,
                "file_name": file_name if file_name else "Unknown"
            }
            active_sequences[user_id].append(file_info)
            await message.reply_text(f"File received in sequence...")
            return

        # Auto-Rename Logic (Runs only when not in sequence mode)
        format_template = await DARKXSIDE78.get_format_template(user_id)
        media_preference = await DARKXSIDE78.get_media_preference(user_id)

        if not format_template:
            return await message.reply_text(
                "Please Set An Auto Rename Format First Using /autorename"
            )

        if message.document:
            file_id = message.document.file_id
            file_name = message.document.file_name or f"document_{file_id[:8]}"
            media_type = media_preference or "document"
        elif message.video:
            file_id = message.video.file_id
            file_name = getattr(message.video, 'file_name', None) or f"video_{file_id[:8]}.mp4"
            media_type = media_preference or "video"
        elif message.audio:
            file_id = message.audio.file_id
            file_name = getattr(message.audio, 'file_name', None) or f"audio_{file_id[:8]}.mp3"
            media_type = media_preference or "document"
        else:
            return await message.reply_text("Unsupported File Type")

        if file_id in renaming_operations:
            elapsed_time = (datetime.now() - renaming_operations[file_id]).seconds
            if elapsed_time < 10:
                return

        renaming_operations[file_id] = datetime.now()

        episode_number = extract_episode_number(file_name)
        if episode_number:
            placeholders = ["episode", "Episode", "EPISODE", "{episode}"]
            for placeholder in placeholders:
                format_template = format_template.replace(placeholder, str(episode_number), 1)

            # Add extracted qualities to the format template
            quality_placeholders = ["quality", "Quality", "QUALITY", "{quality}"]
            for quality_placeholder in quality_placeholders:
                if quality_placeholder in format_template:
                    extracted_qualities = extract_quality(file_name)
                    if extracted_qualities == "Unknown":
                        await message.reply_text("**__I Was Not Able To Extract The Quality Properly. Renaming As 'Unknown'...__**")
                        # Continue with renaming instead of returning
                
                    format_template = format_template.replace(quality_placeholder, str(extracted_qualities))

        _, file_extension = os.path.splitext(file_name)
        renamed_file_name = f"{format_template}{file_extension}"
        
        # Create directories if they don't exist
        os.makedirs("downloads", exist_ok=True)
        os.makedirs("Metadata", exist_ok=True)
        
        renamed_file_path = f"downloads/{renamed_file_name}"
        metadata_file_path = f"Metadata/{renamed_file_name}"

        download_msg = await message.reply_text("**__Downloading...__**")

        try:
            path = await client.download_media(
                message,
                file_name=renamed_file_path,
                progress=progress_for_pyrogram,
                progress_args=("Download Started...", download_msg, time.time()),
            )
        except Exception as e:
            del renaming_operations[file_id]
            return await download_msg.edit(f"**Download Error:** {e}")

        await download_msg.edit("**__Processing and Adding Metadata...__**")

        try:
            # Check if metadata is enabled
            metadata_enabled = await DARKXSIDE78.get_metadata(user_id)
            
            if metadata_enabled == "On":
                # Prepare metadata command
                ffmpeg_cmd = shutil.which('ffmpeg')
                if ffmpeg_cmd:
                    metadata_command = [
                        ffmpeg_cmd,
                        '-i', path,
                        '-metadata', f'title={await DARKXSIDE78.get_title(user_id)}',
                        '-metadata', f'artist={await DARKXSIDE78.get_artist(user_id)}',
                        '-metadata', f'author={await DARKXSIDE78.get_author(user_id)}',
                        '-metadata:s:v', f'title={await DARKXSIDE78.get_video(user_id)}',
                        '-metadata:s:a', f'title={await DARKXSIDE78.get_audio(user_id)}',
                        '-metadata:s:s', f'title={await DARKXSIDE78.get_subtitle(user_id)}',
                        '-c', 'copy',
                        metadata_file_path
                    ]
                    
                    # Execute FFmpeg command
                    process = await asyncio.create_subprocess_exec(
                        *metadata_command,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, stderr = await process.communicate()
                    
                    if process.returncode == 0:
                        # Use metadata file
                        path = metadata_file_path
                    else:
                        logging.error(f"FFmpeg error: {stderr.decode()}")
                        # Continue with original file
                        pass
                else:
                    logging.warning("FFmpeg not found, skipping metadata addition")

        except Exception as e:
            logging.error(f"Metadata processing error: {e}")
            # Continue with original file

        await download_msg.edit("**__Uploading...__**")

        # Get thumbnail and caption
        thumbnail = await DARKXSIDE78.get_thumbnail(user_id)
        caption = await DARKXSIDE78.get_caption(user_id)
        
        # Format caption if provided
        if caption:
            formatted_caption = caption.format(
                filename=renamed_file_name,
                filesize=humanbytes(os.path.getsize(path)) if os.path.exists(path) else "Unknown"
            )
        else:
            formatted_caption = f"**{renamed_file_name}**"

        try:
            # Upload based on media preference
            if media_type == "video":
                await client.send_video(
                    chat_id=message.chat.id,
                    video=path,
                    caption=formatted_caption,
                    thumb=thumbnail,
                    progress=progress_for_pyrogram,
                    progress_args=("Upload Started...", download_msg, time.time())
                )
            else:
                await client.send_document(
                    chat_id=message.chat.id,
                    document=path,
                    caption=formatted_caption,
                    thumb=thumbnail,
                    progress=progress_for_pyrogram,
                    progress_args=("Upload Started...", download_msg, time.time())
                )

            await download_msg.delete()

        except Exception as e:
            await download_msg.edit(f"**Upload Error:** {e}")

        finally:
            # Cleanup files
            try:
                if os.path.exists(path):
                    os.remove(path)
                if os.path.exists(metadata_file_path) and metadata_file_path != path:
                    os.remove(metadata_file_path)
            except Exception as e:
                logging.error(f"Cleanup error: {e}")
            
            # Remove from operations
            if file_id in renaming_operations:
                del renaming_operations[file_id]

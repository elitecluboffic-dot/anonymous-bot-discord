import discord
from discord.ext import commands, tasks
import asyncio
import random
import logging
import time
import os
from typing import Optional
from collections import defaultdict
from dotenv import load_dotenv

# ─────────────────────────────────────────────
#  LOAD TOKEN DARI .env
# ─────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise ValueError("DISCORD_TOKEN tidak ditemukan! Pastikan file .env sudah dibuat.")

# ─────────────────────────────────────────────
#  KONFIGURASI
# ─────────────────────────────────────────────
QUEUE_TIMEOUT_SECONDS = 300   # 5 menit
CLEANUP_INTERVAL_SECONDS = 60
COOLDOWN_SECONDS = 3

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  INTENTS & BOT
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ─────────────────────────────────────────────
#  STATE MANAGEMENT
# ─────────────────────────────────────────────
active_pairs: dict[int, int] = {}
waiting_queue: dict[int, float] = {}
command_cooldown: dict[int, float] = defaultdict(float)


# ─────────────────────────────────────────────
#  HELPER FUNCTIONS
# ─────────────────────────────────────────────
def is_in_pair(user_id: int) -> bool:
    return user_id in active_pairs

def is_in_queue(user_id: int) -> bool:
    return user_id in waiting_queue

def get_partner_id(user_id: int) -> Optional[int]:
    return active_pairs.get(user_id)

def remove_from_queue(user_id: int):
    waiting_queue.pop(user_id, None)

def remove_pair(user_id: int):
    partner_id = active_pairs.pop(user_id, None)
    if partner_id:
        active_pairs.pop(partner_id, None)
    return partner_id

def check_cooldown(user_id: int) -> float:
    elapsed = time.time() - command_cooldown[user_id]
    return max(0.0, COOLDOWN_SECONDS - elapsed)

def update_cooldown(user_id: int):
    command_cooldown[user_id] = time.time()

async def safe_send(user: discord.User, content: str) -> bool:
    try:
        await user.send(content)
        return True
    except discord.Forbidden:
        log.warning(f"Tidak bisa DM ke {user.id} — DM mungkin ditutup.")
        return False
    except discord.HTTPException as e:
        log.error(f"HTTP error DM ke {user.id}: {e}")
        return False
    except Exception as e:
        log.error(f"Error DM ke {user.id}: {e}")
        return False

async def get_user_safe(user_id: int) -> Optional[discord.User]:
    try:
        user = bot.get_user(user_id)
        if user is None:
            user = await bot.fetch_user(user_id)
        return user
    except discord.NotFound:
        log.warning(f"User {user_id} tidak ditemukan.")
        return None
    except Exception as e:
        log.error(f"Error mengambil user {user_id}: {e}")
        return None

async def disconnect_pair(user_id: int, reason: str = "Percakapan diakhiri."):
    partner_id = remove_pair(user_id)
    user = await get_user_safe(user_id)
    if user:
        await safe_send(user, f"🔴 {reason}\nKetik `!start` untuk mencari orang baru.")
    if partner_id:
        partner = await get_user_safe(partner_id)
        if partner:
            await safe_send(partner, "🔴 Orang asing telah meninggalkan percakapan.\nKetik `!start` untuk mencari orang baru.")


# ─────────────────────────────────────────────
#  BACKGROUND TASK: CLEANUP QUEUE TIMEOUT
# ─────────────────────────────────────────────
@tasks.loop(seconds=CLEANUP_INTERVAL_SECONDS)
async def cleanup_stale_queue():
    now = time.time()
    stale_users = [uid for uid, ts in list(waiting_queue.items()) if now - ts > QUEUE_TIMEOUT_SECONDS]
    for uid in stale_users:
        remove_from_queue(uid)
        user = await get_user_safe(uid)
        if user:
            await safe_send(user, f"⏰ Kamu sudah menunggu terlalu lama ({QUEUE_TIMEOUT_SECONDS // 60} menit) dan dikeluarkan dari antrian.\nKetik `!start` untuk mencoba lagi.")
        log.info(f"User {uid} timeout dari queue.")
    if stale_users:
        log.info(f"Cleanup: {len(stale_users)} user dihapus dari queue.")

@cleanup_stale_queue.before_loop
async def before_cleanup():
    await bot.wait_until_ready()


# ─────────────────────────────────────────────
#  EVENTS
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    log.info(f"Bot online sebagai {bot.user} (ID: {bot.user.id})")
    cleanup_stale_queue.start()
    try:
        synced = await bot.tree.sync()
        log.info(f"Slash commands synced: {len(synced)}")
    except Exception as e:
        log.warning(f"Gagal sync slash commands: {e}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    await bot.process_commands(message)
    if not isinstance(message.channel, discord.DMChannel):
        return
    if message.content.startswith("!"):
        return

    user_id = message.author.id
    if not is_in_pair(user_id):
        return

    partner_id = get_partner_id(user_id)
    if not partner_id:
        return

    partner = await get_user_safe(partner_id)
    if not partner:
        await disconnect_pair(user_id, "Koneksi terputus karena orang asing tidak dapat ditemukan.")
        return

    content = message.content.strip()
    if not content and not message.attachments:
        return

    if len(content) > 2000:
        await safe_send(message.author, "⚠️ Pesan terlalu panjang (maks 2000 karakter).")
        return

    forwarded = f"👤 **Orang asing:** {content}" if content else ""
    if message.attachments:
        attachment_links = "\n".join(a.url for a in message.attachments)
        forwarded += f"\n📎 {attachment_links}" if forwarded else f"👤 **Orang asing:** 📎 {attachment_links}"

    success = await safe_send(partner, forwarded)
    if not success:
        await disconnect_pair(user_id, "Koneksi terputus karena orang asing tidak dapat menerima pesan.")

@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingPermissions):
        await safe_send(ctx.author, "❌ Kamu tidak memiliki izin untuk itu.")
    elif isinstance(error, commands.BotMissingPermissions):
        await safe_send(ctx.author, "❌ Bot tidak memiliki izin yang dibutuhkan.")
    elif isinstance(error, commands.CommandOnCooldown):
        await safe_send(ctx.author, f"⏳ Tunggu {error.retry_after:.1f} detik lagi.")
    else:
        log.error(f"Unhandled error pada command '{ctx.command}': {error}", exc_info=True)
        await safe_send(ctx.author, "❌ Terjadi error. Coba lagi nanti.")


# ─────────────────────────────────────────────
#  COMMANDS
# ─────────────────────────────────────────────
@bot.command(name="start")
async def cmd_start(ctx: commands.Context):
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("📩 Command ini hanya bisa dipakai lewat **DM** ke bot!")
        return

    user = ctx.author
    user_id = user.id

    remaining = check_cooldown(user_id)
    if remaining > 0:
        await safe_send(user, f"⏳ Tunggu {remaining:.1f} detik sebelum menggunakan command lagi.")
        return
    update_cooldown(user_id)

    if is_in_pair(user_id):
        await safe_send(user, "💬 Kamu sedang dalam percakapan. Ketik `!cancel` untuk mengakhirinya dulu.")
        return

    if is_in_queue(user_id):
        await safe_send(user, "🔍 Kamu sudah dalam antrian pencarian. Mohon tunggu...")
        return

    available = [uid for uid in list(waiting_queue.keys()) if uid != user_id]

    if available:
        partner_id = random.choice(available)
        remove_from_queue(partner_id)
        active_pairs[user_id] = partner_id
        active_pairs[partner_id] = user_id

        partner = await get_user_safe(partner_id)
        if not partner:
            del active_pairs[user_id]
            del active_pairs[partner_id]
            waiting_queue[user_id] = time.time()
            await safe_send(user, "🔍 Menambahkan ke antrian... menunggu orang asing.")
            return

        await safe_send(user, "✅ Terhubung dengan orang asing! Mulai ngobrol sekarang.\nKetik `!next` untuk ganti orang, `!cancel` untuk berhenti.")
        await safe_send(partner, "✅ Terhubung dengan orang asing! Mulai ngobrol sekarang.\nKetik `!next` untuk ganti orang, `!cancel` untuk berhenti.")
        log.info(f"Match berhasil: {user_id} <-> {partner_id}")
    else:
        waiting_queue[user_id] = time.time()
        await safe_send(user, "🔍 Mencari orang asing... Mohon tunggu.\nKetik `!cancel` untuk membatalkan pencarian.")
        log.info(f"User {user_id} masuk antrian. Total queue: {len(waiting_queue)}")


@bot.command(name="next")
async def cmd_next(ctx: commands.Context):
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("📩 Command ini hanya bisa dipakai lewat **DM** ke bot!")
        return

    user = ctx.author
    user_id = user.id

    remaining = check_cooldown(user_id)
    if remaining > 0:
        await safe_send(user, f"⏳ Tunggu {remaining:.1f} detik sebelum menggunakan command lagi.")
        return
    update_cooldown(user_id)

    if not is_in_pair(user_id) and not is_in_queue(user_id):
        await safe_send(user, "❓ Kamu tidak sedang dalam percakapan atau antrian. Ketik `!start` untuk mulai.")
        return

    if is_in_pair(user_id):
        partner_id = get_partner_id(user_id)
        remove_pair(user_id)
        if partner_id:
            partner = await get_user_safe(partner_id)
            if partner:
                await safe_send(partner, "🔄 Orang asing melewati percakapan ini.\nKetik `!start` untuk mencari orang baru.")
        await safe_send(user, "🔄 Percakapan diakhiri. Mencari orang baru...")
    else:
        remove_from_queue(user_id)
        await safe_send(user, "🔄 Mencari ulang...")

    available = [uid for uid in list(waiting_queue.keys()) if uid != user_id]

    if available:
        partner_id = random.choice(available)
        remove_from_queue(partner_id)
        active_pairs[user_id] = partner_id
        active_pairs[partner_id] = user_id

        partner = await get_user_safe(partner_id)
        if not partner:
            del active_pairs[user_id]
            del active_pairs[partner_id]
            waiting_queue[user_id] = time.time()
            await safe_send(user, "🔍 Menambahkan ke antrian... menunggu orang asing.")
            return

        await safe_send(user, "✅ Terhubung dengan orang asing baru!\nKetik `!next` untuk ganti orang, `!cancel` untuk berhenti.")
        await safe_send(partner, "✅ Terhubung dengan orang asing! Mulai ngobrol sekarang.\nKetik `!next` untuk ganti orang, `!cancel` untuk berhenti.")
        log.info(f"Next match: {user_id} <-> {partner_id}")
    else:
        waiting_queue[user_id] = time.time()
        await safe_send(user, "🔍 Belum ada yang menunggu. Kamu masuk antrian...\nKetik `!cancel` untuk membatalkan.")
        log.info(f"User {user_id} masuk antrian via !next. Total queue: {len(waiting_queue)}")


@bot.command(name="cancel")
async def cmd_cancel(ctx: commands.Context):
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("📩 Command ini hanya bisa dipakai lewat **DM** ke bot!")
        return

    user = ctx.author
    user_id = user.id

    remaining = check_cooldown(user_id)
    if remaining > 0:
        await safe_send(user, f"⏳ Tunggu {remaining:.1f} detik sebelum menggunakan command lagi.")
        return
    update_cooldown(user_id)

    if not is_in_pair(user_id) and not is_in_queue(user_id):
        await safe_send(user, "❓ Kamu tidak sedang dalam percakapan atau antrian.")
        return

    if is_in_pair(user_id):
        await disconnect_pair(user_id, "Percakapan diakhiri oleh orang asing.")
        await safe_send(user, "🔴 Percakapan diakhiri. Sampai jumpa!\nKetik `!start` kalau mau ngobrol lagi.")
    elif is_in_queue(user_id):
        remove_from_queue(user_id)
        await safe_send(user, "🔴 Pencarian dibatalkan. Ketik `!start` untuk mencari lagi.")

    log.info(f"User {user_id} menggunakan !cancel.")


@bot.command(name="status")
async def cmd_status(ctx: commands.Context):
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("📩 Command ini hanya bisa dipakai lewat **DM** ke bot!")
        return

    user_id = ctx.author.id
    if is_in_pair(user_id):
        await safe_send(ctx.author, "💬 **Status:** Sedang dalam percakapan.\n`!next` → ganti orang | `!cancel` → berhenti")
    elif is_in_queue(user_id):
        wait_time = int(time.time() - waiting_queue[user_id])
        await safe_send(ctx.author, f"🔍 **Status:** Menunggu di antrian ({wait_time} detik).\n`!cancel` → batalkan pencarian")
    else:
        await safe_send(ctx.author, "😴 **Status:** Tidak aktif.\n`!start` → mulai mencari")


@bot.command(name="help")
async def cmd_help(ctx: commands.Context):
    msg = (
        "**📖 Panduan Anonymous Chat Bot**\n\n"
        "`!start` — Mulai mencari orang asing\n"
        "`!next` — Skip ke orang berikutnya\n"
        "`!cancel` — Akhiri percakapan / batalkan pencarian\n"
        "`!status` — Cek status kamu sekarang\n"
        "`!help` — Tampilkan pesan ini\n\n"
        "⚠️ **Semua command harus dikirim lewat DM ke bot ini.**\n"
        "Identitas kamu 100% anonim — orang asing tidak tahu siapa kamu."
    )
    if isinstance(ctx.channel, discord.DMChannel):
        await safe_send(ctx.author, msg)
    else:
        await ctx.send(msg)


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
async def main():
    async with bot:
        try:
            await bot.start(TOKEN)
        except discord.LoginFailure:
            log.critical("TOKEN tidak valid! Cek nilai DISCORD_TOKEN di file .env atau Railway Variables.")
        except discord.PrivilegedIntentsRequired:
            log.critical("Bot membutuhkan Privileged Intents (Message Content). Aktifkan di Discord Developer Portal.")
        except KeyboardInterrupt:
            log.info("Bot dihentikan oleh user.")
        except Exception as e:
            log.critical(f"Error fatal: {e}", exc_info=True)

if __name__ == "__main__":
    asyncio.run(main())

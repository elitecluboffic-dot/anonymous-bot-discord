import discord
from discord.ext import commands, tasks
import asyncio
import random
import logging
import time
import os
import json
import uuid
from datetime import datetime, timedelta
from typing import Optional
from collections import defaultdict
from dotenv import load_dotenv

# ─────────────────────────────────────────────
#  LOAD TOKEN
# ─────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")  # Isi di .env: ADMIN_IDS=123456789,987654321

ADMIN_IDS: set[int] = set()
for aid in ADMIN_IDS_RAW.split(","):
    aid = aid.strip()
    if aid.isdigit():
        ADMIN_IDS.add(int(aid))

if not TOKEN:
    raise ValueError("DISCORD_TOKEN tidak ditemukan!")

# ─────────────────────────────────────────────
#  KONFIGURASI
# ─────────────────────────────────────────────
QUEUE_TIMEOUT_SECONDS = 300        # Free: 5 menit
QUEUE_TIMEOUT_PREMIUM = 99999      # Premium: tidak timeout
CLEANUP_INTERVAL_SECONDS = 60
COOLDOWN_SECONDS = 3

PREMIUM_PLANS = {
    "7":  {"days": 7,  "price": 30000,  "label": "7 Hari"},
    "30": {"days": 30, "price": 190000, "label": "30 Hari"},
}

KEYS_FILE = "premium_keys.json"
USERS_FILE = "premium_users.json"

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
waiting_queue: dict[int, float] = {}          # Free queue
premium_queue: dict[int, float] = {}          # Premium priority queue
command_cooldown: dict[int, float] = defaultdict(float)

# user_info: { user_id: { "gender": "pria/wanita", ... } }
user_info: dict[int, dict] = {}


# ─────────────────────────────────────────────
#  PREMIUM KEY STORAGE
# ─────────────────────────────────────────────
def load_keys() -> dict:
    try:
        with open(KEYS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_keys(data: dict):
    with open(KEYS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_users() -> dict:
    try:
        with open(USERS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_users(data: dict):
    with open(USERS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def generate_key(days: int) -> str:
    prefix = "PREM7" if days == 7 else "PREM30"
    return f"{prefix}-{uuid.uuid4().hex[:6].upper()}-{uuid.uuid4().hex[:6].upper()}"

def is_premium(user_id: int) -> bool:
    users = load_users()
    uid = str(user_id)
    if uid not in users:
        return False
    expiry = users[uid].get("expiry")
    if not expiry:
        return False
    return datetime.fromisoformat(expiry) > datetime.now()

def get_premium_expiry(user_id: int) -> Optional[str]:
    users = load_users()
    uid = str(user_id)
    if uid not in users:
        return None
    return users[uid].get("expiry")

def activate_key(user_id: int, key: str) -> tuple[bool, str]:
    """Return (success, message)"""
    keys = load_keys()
    users = load_users()
    uid = str(user_id)

    if key not in keys:
        return False, "❌ Key tidak valid atau tidak ditemukan."

    key_data = keys[key]

    if key_data.get("used"):
        return False, "❌ Key ini sudah pernah digunakan."

    days = key_data["days"]
    now = datetime.now()

    # Kalau masih premium, perpanjang dari expiry yang ada
    current_expiry = None
    if uid in users and users[uid].get("expiry"):
        current_exp = datetime.fromisoformat(users[uid]["expiry"])
        if current_exp > now:
            current_expiry = current_exp

    start = current_expiry if current_expiry else now
    new_expiry = start + timedelta(days=days)

    # Update user
    users[uid] = {
        "user_id": user_id,
        "expiry": new_expiry.isoformat(),
        "plan": f"{days} hari",
        "activated_at": now.isoformat(),
    }

    # Tandai key sudah dipakai
    keys[key]["used"] = True
    keys[key]["used_by"] = user_id
    keys[key]["used_at"] = now.isoformat()

    save_users(users)
    save_keys(keys)

    label = "7 Hari" if days == 7 else "30 Hari"
    expiry_str = new_expiry.strftime("%d %B %Y %H:%M")
    return True, f"✅ **Premium {label} aktif!**\nBerlaku hingga: `{expiry_str}`"


# ─────────────────────────────────────────────
#  HELPER FUNCTIONS
# ─────────────────────────────────────────────
def is_in_pair(user_id: int) -> bool:
    return user_id in active_pairs

def is_in_queue(user_id: int) -> bool:
    return user_id in waiting_queue or user_id in premium_queue

def get_partner_id(user_id: int) -> Optional[int]:
    return active_pairs.get(user_id)

def remove_from_queue(user_id: int):
    waiting_queue.pop(user_id, None)
    premium_queue.pop(user_id, None)

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

def get_available_partners(user_id: int) -> list[int]:
    """Prioritas: ambil dari premium_queue dulu, lalu waiting_queue."""
    premium = [uid for uid in premium_queue if uid != user_id]
    free = [uid for uid in waiting_queue if uid != user_id]
    return premium + free  # premium diprioritaskan

async def safe_send(user: discord.User, content: str = "", embed=None) -> bool:
    try:
        if embed:
            await user.send(content=content, embed=embed)
        else:
            await user.send(content)
        return True
    except discord.Forbidden:
        log.warning(f"Tidak bisa DM ke {user.id} — DM ditutup.")
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
        return None
    except Exception as e:
        log.error(f"Error mengambil user {user_id}: {e}")
        return None

def build_match_embed(partner_id: int, show_info: bool) -> discord.Embed:
    """Buat embed notif match. Kalau premium, tampilkan info lawan."""
    embed = discord.Embed(
        title="✅ Terhubung!",
        description="Kamu terhubung dengan orang asing. Mulai ngobrol sekarang!",
        color=0x2ecc71
    )
    if show_info and partner_id in user_info:
        info = user_info[partner_id]
        gender = info.get("gender", "?")
        embed.add_field(name="👤 Info Lawan", value=f"Gender: **{gender}**", inline=False)
    embed.add_field(name="Commands", value="`!next` → ganti orang\n`!cancel` → berhenti", inline=False)
    if show_info:
        embed.set_footer(text="⭐ Premium — kamu bisa lihat info lawan")
    return embed

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
#  BACKGROUND TASK: CLEANUP
# ─────────────────────────────────────────────
@tasks.loop(seconds=CLEANUP_INTERVAL_SECONDS)
async def cleanup_stale_queue():
    now = time.time()

    # Free queue timeout
    stale = [uid for uid, ts in list(waiting_queue.items()) if now - ts > QUEUE_TIMEOUT_SECONDS]
    for uid in stale:
        remove_from_queue(uid)
        user = await get_user_safe(uid)
        if user:
            await safe_send(user, f"⏰ Kamu sudah menunggu terlalu lama dan dikeluarkan dari antrian.\nKetik `!start` untuk mencoba lagi.")
        log.info(f"User {uid} timeout dari queue.")

    # Cek expiry premium (hapus dari premium_queue kalau sudah expired)
    expired_premium = [uid for uid in list(premium_queue.keys()) if not is_premium(uid)]
    for uid in expired_premium:
        premium_queue.pop(uid, None)
        waiting_queue[uid] = time.time()  # pindah ke free queue
        user = await get_user_safe(uid)
        if user:
            await safe_send(user, "⚠️ Premium kamu habis! Kamu dipindahkan ke antrian biasa.")

    if stale:
        log.info(f"Cleanup: {len(stale)} user free timeout.")

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
        log.warning(f"Gagal sync: {e}")

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
        await disconnect_pair(user_id, "Koneksi terputus.")
        return

    content = message.content.strip()
    if not content and not message.attachments:
        return

    if len(content) > 2000:
        await safe_send(message.author, "⚠️ Pesan terlalu panjang (maks 2000 karakter).")
        return

    forwarded = f"👤 **Orang asing:** {content}" if content else ""
    if message.attachments:
        links = "\n".join(a.url for a in message.attachments)
        forwarded += f"\n📎 {links}" if forwarded else f"👤 **Orang asing:** 📎 {links}"

    success = await safe_send(partner, forwarded)
    if not success:
        await disconnect_pair(user_id, "Koneksi terputus karena orang asing tidak dapat menerima pesan.")

@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingPermissions):
        await safe_send(ctx.author, "❌ Kamu tidak memiliki izin.")
    elif isinstance(error, commands.CommandOnCooldown):
        await safe_send(ctx.author, f"⏳ Tunggu {error.retry_after:.1f} detik lagi.")
    else:
        log.error(f"Error command '{ctx.command}': {error}", exc_info=True)
        await safe_send(ctx.author, "❌ Terjadi error. Coba lagi nanti.")


# ─────────────────────────────────────────────
#  MATCH LOGIC (dipakai !start dan !next)
# ─────────────────────────────────────────────
async def do_match(user: discord.User):
    user_id = user.id
    prem = is_premium(user_id)

    available = get_available_partners(user_id)

    if available:
        partner_id = available[0]  # Premium partner diprioritaskan (ada di depan list)
        remove_from_queue(partner_id)

        active_pairs[user_id] = partner_id
        active_pairs[partner_id] = user_id

        partner = await get_user_safe(partner_id)
        if not partner:
            del active_pairs[user_id]
            del active_pairs[partner_id]
            # Masuk queue
            if prem:
                premium_queue[user_id] = time.time()
            else:
                waiting_queue[user_id] = time.time()
            await safe_send(user, "🔍 Menambahkan ke antrian... menunggu orang asing.")
            return

        partner_prem = is_premium(partner_id)

        # Kirim embed ke user
        embed_user = build_match_embed(partner_id, show_info=prem)
        await safe_send(user, embed=embed_user)

        # Kirim embed ke partner
        embed_partner = build_match_embed(user_id, show_info=partner_prem)
        await safe_send(partner, embed=embed_partner)

        log.info(f"Match: {user_id}{'⭐' if prem else ''} <-> {partner_id}{'⭐' if partner_prem else ''}")
    else:
        if prem:
            premium_queue[user_id] = time.time()
            await safe_send(user, "⭐ **[Premium]** Kamu masuk antrian prioritas!\nKetik `!cancel` untuk membatalkan.")
        else:
            waiting_queue[user_id] = time.time()
            await safe_send(user, "🔍 Mencari orang asing... Mohon tunggu.\nKetik `!cancel` untuk membatalkan pencarian.")
        log.info(f"User {user_id} masuk antrian {'premium' if prem else 'free'}.")


# ─────────────────────────────────────────────
#  COMMANDS — CHAT
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
        await safe_send(user, f"⏳ Tunggu {remaining:.1f} detik.")
        return
    update_cooldown(user_id)

    if is_in_pair(user_id):
        await safe_send(user, "💬 Kamu sedang dalam percakapan. Ketik `!cancel` untuk mengakhirinya dulu.")
        return

    if is_in_queue(user_id):
        await safe_send(user, "🔍 Kamu sudah dalam antrian. Mohon tunggu...")
        return

    # Tanya gender kalau belum ada info
    if user_id not in user_info:
        await safe_send(user,
            "👋 Sebelum mulai, **pilih gendermu** dengan mengetik:\n"
            "`!setgender pria` atau `!setgender wanita`\n\n"
            "Setelah itu ketik `!start` lagi."
        )
        return

    await do_match(user)


@bot.command(name="next")
async def cmd_next(ctx: commands.Context):
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("📩 Command ini hanya bisa dipakai lewat **DM** ke bot!")
        return

    user = ctx.author
    user_id = user.id

    remaining = check_cooldown(user_id)
    if remaining > 0:
        await safe_send(user, f"⏳ Tunggu {remaining:.1f} detik.")
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
        await safe_send(user, "🔄 Mencari orang baru...")
    else:
        remove_from_queue(user_id)

    await do_match(user)


@bot.command(name="cancel")
async def cmd_cancel(ctx: commands.Context):
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("📩 Command ini hanya bisa dipakai lewat **DM** ke bot!")
        return

    user = ctx.author
    user_id = user.id

    remaining = check_cooldown(user_id)
    if remaining > 0:
        await safe_send(user, f"⏳ Tunggu {remaining:.1f} detik.")
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

    log.info(f"User {user_id} cancel.")


@bot.command(name="setgender")
async def cmd_setgender(ctx: commands.Context, gender: str = None):
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("📩 Command ini hanya bisa dipakai lewat **DM** ke bot!")
        return

    if not gender or gender.lower() not in ["pria", "wanita"]:
        await safe_send(ctx.author, "❌ Pilihan tidak valid.\nKetik `!setgender pria` atau `!setgender wanita`")
        return

    user_info[ctx.author.id] = {"gender": gender.lower()}
    await safe_send(ctx.author, f"✅ Gender kamu diset ke **{gender.lower()}**.\nSekarang ketik `!start` untuk mulai mencari!")


@bot.command(name="status")
async def cmd_status(ctx: commands.Context):
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("📩 Command ini hanya bisa dipakai lewat **DM** ke bot!")
        return

    user_id = ctx.author.id
    prem = is_premium(user_id)
    expiry = get_premium_expiry(user_id)

    if is_in_pair(user_id):
        status = "💬 Sedang dalam percakapan"
    elif is_in_queue(user_id):
        q = "⭐ Antrian Premium" if user_id in premium_queue else "🔍 Antrian Biasa"
        wait = int(time.time() - (premium_queue.get(user_id) or waiting_queue.get(user_id, time.time())))
        status = f"{q} ({wait} detik)"
    else:
        status = "😴 Tidak aktif"

    embed = discord.Embed(title="📊 Status Kamu", color=0xf1c40f if prem else 0x95a5a6)
    embed.add_field(name="Status Chat", value=status, inline=False)
    embed.add_field(
        name="Akun",
        value=("⭐ **Premium** — berlaku hingga **" + datetime.fromisoformat(expiry).strftime('%d %b %Y') + "**")
              if prem and expiry else "🆓 Free",
        inline=False
    )
    gender = user_info.get(user_id, {}).get("gender", "belum diset")
    embed.add_field(name="Gender", value=gender, inline=True)
    await ctx.author.send(embed=embed)


@bot.command(name="anonymous")
async def cmd_help(ctx: commands.Context):
    msg = (
        "**📖 Panduan Anonymous Chat Bot**\n\n"
        "`!start` — Mulai mencari orang asing\n"
        "`!next` — Skip ke orang berikutnya\n"
        "`!cancel` — Akhiri percakapan / batalkan pencarian\n"
        "`!setgender` — Set gender kamu (pria/wanita)\n"
        "`!status` — Cek status & info premium\n"
        "`!redeem <key>` — Aktifkan key premium\n"
        "`!premium` — Info harga & fitur premium\n\n"
        "⚠️ Semua command lewat **DM** ke bot.\n"
        "Identitas kamu 100% anonim."
    )
    if isinstance(ctx.channel, discord.DMChannel):
        await safe_send(ctx.author, msg)
    else:
        await ctx.send(msg)


# ─────────────────────────────────────────────
#  COMMANDS — PREMIUM
# ─────────────────────────────────────────────
@bot.command(name="premium")
async def cmd_premium(ctx: commands.Context):
    embed = discord.Embed(
        title="⭐ Anonymous Chat Premium",
        description="Upgrade ke Premium dan nikmati fitur eksklusif!",
        color=0xf1c40f
    )
    embed.add_field(
        name="🎁 Fitur Premium",
        value=(
            "✅ **Prioritas match** — antrian premium didahulukan\n"
            "✅ **Lihat info lawan** — gender orang asing terlihat\n"
            "✅ **Tidak ada timeout** antrian\n"
        ),
        inline=False
    )
    embed.add_field(
        name="💰 Harga",
        value=(
            "**7 Hari** → Rp 30.000\n"
            "**30 Hari** → Rp 190.000\n"
        ),
        inline=False
    )
    embed.add_field(
        name="📲 Cara Beli",
        value=(
            "1. Hubungi admin untuk pembayaran\n"
            "2. Setelah bayar, kamu dapat key unik\n"
            "3. Ketik `!redeem <key>` untuk aktivasi\n"
        ),
        inline=False
    )
    embed.set_footer(text="Ketik !redeem <key> untuk aktivasi premium")
    if isinstance(ctx.channel, discord.DMChannel):
        await ctx.author.send(embed=embed)
    else:
        await ctx.send(embed=embed)


@bot.command(name="redeem")
async def cmd_redeem(ctx: commands.Context, key: str = None):
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("📩 Command ini hanya bisa dipakai lewat **DM** ke bot!")
        return

    if not key:
        await safe_send(ctx.author, "❌ Masukkan key premium.\nContoh: `!redeem PREM7-XXXXXX-XXXXXX`")
        return

    success, message = activate_key(ctx.author.id, key.strip().upper())
    await safe_send(ctx.author, message)

    if success:
        log.info(f"User {ctx.author.id} aktivasi key: {key}")


# ─────────────────────────────────────────────
#  COMMANDS — ADMIN
# ─────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


@bot.command(name="genkey")
async def cmd_genkey(ctx: commands.Context, plan: str = None):
    """Admin only: generate premium key. Contoh: !genkey 7 atau !genkey 30"""
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("📩 Command ini hanya bisa dipakai lewat **DM** ke bot!")
        return

    if not is_admin(ctx.author.id):
        await safe_send(ctx.author, "❌ Kamu tidak memiliki akses admin.")
        return

    if plan not in PREMIUM_PLANS:
        await safe_send(ctx.author, "❌ Plan tidak valid. Gunakan `!genkey 7` atau `!genkey 30`")
        return

    plan_data = PREMIUM_PLANS[plan]
    key = generate_key(plan_data["days"])

    keys = load_keys()
    keys[key] = {
        "days": plan_data["days"],
        "plan": plan_data["label"],
        "price": plan_data["price"],
        "used": False,
        "created_at": datetime.now().isoformat(),
        "created_by": ctx.author.id,
    }
    save_keys(keys)

    embed = discord.Embed(title="🔑 Key Premium Generated", color=0x2ecc71)
    embed.add_field(name="Key", value=f"`{key}`", inline=False)
    embed.add_field(name="Plan", value=plan_data["label"], inline=True)
    embed.add_field(name="Harga", value=f"Rp {plan_data['price']:,}", inline=True)
    embed.set_footer(text="Kirim key ini ke user setelah pembayaran dikonfirmasi")
    await ctx.author.send(embed=embed)
    log.info(f"Admin {ctx.author.id} generate key: {key} ({plan_data['label']})")


@bot.command(name="listkeys")
async def cmd_listkeys(ctx: commands.Context):
    """Admin only: lihat semua key."""
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("📩 DM only!")
        return

    if not is_admin(ctx.author.id):
        await safe_send(ctx.author, "❌ Akses ditolak.")
        return

    keys = load_keys()
    if not keys:
        await safe_send(ctx.author, "📭 Belum ada key yang dibuat.")
        return

    lines = []
    for k, v in list(keys.items())[-20:]:  # Tampilkan 20 key terakhir
        status = "✅ Terpakai" if v.get("used") else "🟢 Tersedia"
        lines.append(f"`{k}` — {v['plan']} — {status}")

    await safe_send(ctx.author, "**🔑 Daftar Key (20 terakhir):**\n" + "\n".join(lines))


@bot.command(name="cekuser")
async def cmd_cekuser(ctx: commands.Context, user_id: str = None):
    """Admin only: cek status premium user."""
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("📩 DM only!")
        return

    if not is_admin(ctx.author.id):
        await safe_send(ctx.author, "❌ Akses ditolak.")
        return

    if not user_id or not user_id.isdigit():
        await safe_send(ctx.author, "❌ Masukkan user ID. Contoh: `!cekuser 123456789`")
        return

    uid = int(user_id)
    prem = is_premium(uid)
    expiry = get_premium_expiry(uid)

    user = await get_user_safe(uid)
    name = str(user) if user else f"ID: {uid}"

    status = f"⭐ Premium hingga `{datetime.fromisoformat(expiry).strftime('%d %b %Y %H:%M')}`" if prem and expiry else "🆓 Free"
    await safe_send(ctx.author, f"**👤 {name}**\nStatus: {status}")


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
async def main():
    async with bot:
        try:
            await bot.start(TOKEN)
        except discord.LoginFailure:
            log.critical("TOKEN tidak valid!")
        except discord.PrivilegedIntentsRequired:
            log.critical("Aktifkan Message Content Intent di Discord Developer Portal.")
        except KeyboardInterrupt:
            log.info("Bot dihentikan.")
        except Exception as e:
            log.critical(f"Error fatal: {e}", exc_info=True)

if __name__ == "__main__":
    asyncio.run(main())
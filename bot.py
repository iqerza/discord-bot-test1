import asyncio
import os
import random
import re
import sqlite3
import threading
import time
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from flask import Flask, jsonify
from PIL import Image, ImageDraw, ImageFont, ImageOps
from discord.ext import commands, tasks


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

DATABASE_PATH = os.getenv("LEVELS_DATABASE", "levels.sqlite3")
XP_COOLDOWN_SECONDS = 60
MIN_MESSAGE_XP = 15
MAX_MESSAGE_XP = 25
MAX_TIMEOUT_SECONDS = 28 * 24 * 60 * 60
VOICE_XP_PER_MINUTE = 10
LEVEL_UP_CHANNEL_ID = 1474234820971462747
LEVEL_CARD_SIZE = (736, 230)
LEVEL_CARD_BACKGROUND = (
    Path(__file__).resolve().parent
    / "attached_assets"
    / "background.png"
)
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
WATERMARK_FONT_PATH = (
    Path(__file__).resolve().parent / "attached_assets" / "NotoSansMath.otf"
)
SERVER_WATERMARK = "𝙽𝚘𝚒𝚛 𝙱𝚕𝚘𝚘𝚖"
KEEP_ALIVE_HOST = "0.0.0.0"
KEEP_ALIVE_PORT = int(os.getenv("PORT", "8080"))

database = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
database.row_factory = sqlite3.Row
database.execute("PRAGMA journal_mode=WAL")
database.executescript(
    """
    CREATE TABLE IF NOT EXISTS member_levels (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        xp INTEGER NOT NULL DEFAULT 0,
        chat_xp INTEGER NOT NULL DEFAULT 0,
        voice_xp INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS member_warnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        moderator_id INTEGER NOT NULL,
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """
)
level_columns = {
    row["name"]
    for row in database.execute("PRAGMA table_info(member_levels)").fetchall()
}
if "chat_xp" not in level_columns:
    database.execute(
        "ALTER TABLE member_levels ADD COLUMN chat_xp INTEGER NOT NULL DEFAULT 0"
    )
    database.execute(
        "UPDATE member_levels SET chat_xp = xp WHERE chat_xp = 0 AND xp > 0"
    )
if "voice_xp" not in level_columns:
    database.execute(
        "ALTER TABLE member_levels ADD COLUMN voice_xp INTEGER NOT NULL DEFAULT 0"
    )
database.commit()

xp_cooldowns: dict[tuple[int, int], float] = {}
slash_commands_synced = False

keep_alive_app = Flask(__name__)


@keep_alive_app.get("/")
def keep_alive_home():
    return jsonify(status="ok", service="discord-bot")


@keep_alive_app.get("/health")
def keep_alive_health():
    return jsonify(status="healthy", discord_bot="running")


def run_keep_alive_server() -> None:
    """يشغل Endpoint بسيطًا يمكن لخدمة مراقبة خارجية فحصه."""
    keep_alive_app.run(
        host=KEEP_ALIVE_HOST,
        port=KEEP_ALIVE_PORT,
        debug=False,
        use_reloader=False,
    )


def start_keep_alive_server() -> None:
    """يشغل Flask في Thread منفصل حتى لا يحجب اتصال Discord."""
    server_thread = threading.Thread(
        target=run_keep_alive_server,
        name="keep-alive-server",
        daemon=True,
    )
    server_thread.start()
    print(f"Keep Alive server listening on port {KEEP_ALIVE_PORT}")


def level_from_xp(xp: int) -> int:
    """كل مستوى جديد يحتاج إلى مربع المستوى × 100 نقطة خبرة."""
    return int((xp / 100) ** 0.5)


def progress_for_xp(xp: int) -> tuple[int, int, int]:
    """يعيد المستوى الحالي وXP المستوى التالي ونسبة التقدم."""
    current_level = level_from_xp(xp)
    current_level_xp = current_level**2 * 100
    next_level_xp = (current_level + 1) ** 2 * 100
    required_xp = max(1, next_level_xp - current_level_xp)
    progress_percent = min(
        100, max(0, int(((xp - current_level_xp) / required_xp) * 100))
    )
    return current_level, next_level_xp, progress_percent


def get_member_xp_breakdown(guild_id: int, user_id: int) -> tuple[int, int]:
    row = database.execute(
        """
        SELECT chat_xp, voice_xp, xp
        FROM member_levels
        WHERE guild_id = ? AND user_id = ?
        """,
        (guild_id, user_id),
    ).fetchone()
    if not row:
        return 0, 0
    chat_xp = int(row["chat_xp"])
    voice_xp = int(row["voice_xp"])
    # يحافظ على بيانات الإصدارات القديمة في حال كانت الهجرة قديمة أو جزئية.
    if chat_xp == 0 and voice_xp == 0 and int(row["xp"]) > 0:
        chat_xp = int(row["xp"])
    return chat_xp, voice_xp


def get_member_xp(guild_id: int, user_id: int) -> int:
    chat_xp, voice_xp = get_member_xp_breakdown(guild_id, user_id)
    return chat_xp + voice_xp


def add_member_xp(
    guild_id: int, user_id: int, amount: int, source: str = "chat"
) -> tuple[int, int]:
    chat_xp, voice_xp = get_member_xp_breakdown(guild_id, user_id)
    old_xp = chat_xp + voice_xp
    if source == "chat":
        chat_xp += amount
    elif source == "voice":
        voice_xp += amount
    elif source == "both":
        chat_xp += amount
        voice_xp += amount
    else:
        raise ValueError(f"مصدر XP غير معروف: {source}")
    new_xp = chat_xp + voice_xp
    database.execute(
        """
        INSERT INTO member_levels (guild_id, user_id, xp, chat_xp, voice_xp)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id)
        DO UPDATE SET
            xp = excluded.xp,
            chat_xp = excluded.chat_xp,
            voice_xp = excluded.voice_xp
        """,
        (guild_id, user_id, new_xp, chat_xp, voice_xp),
    )
    database.commit()
    return old_xp, new_xp


def add_warning(guild_id: int, user_id: int, moderator_id: int, reason: str) -> int:
    cursor = database.execute(
        """
        INSERT INTO member_warnings (guild_id, user_id, moderator_id, reason)
        VALUES (?, ?, ?, ?)
        """,
        (guild_id, user_id, moderator_id, reason),
    )
    database.commit()
    return int(cursor.lastrowid)


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(BOLD_FONT_PATH if bold else FONT_PATH, size)


def fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> str:
    """يختصر الاسم إذا كان أطول من المساحة المتاحة في البطاقة."""
    if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
        return text
    shortened = text
    while shortened and draw.textbbox((0, 0), f"{shortened}…", font=font)[2] > max_width:
        shortened = shortened[:-1]
    return f"{shortened}…"


def render_level_card(
    *,
    display_name: str,
    avatar_bytes: bytes,
    level: int,
    xp: int,
    next_level_xp: int,
    chat_xp: int = 0,
    voice_xp: int = 0,
) -> BytesIO:
    """يرسم بطاقة المستوى ويعيدها كملف PNG جاهز للإرسال في Discord."""
    if not LEVEL_CARD_BACKGROUND.exists():
        raise FileNotFoundError(f"خلفية بطاقة الليفل غير موجودة: {LEVEL_CARD_BACKGROUND}")

    canvas = Image.open(LEVEL_CARD_BACKGROUND).convert("RGBA").resize(
        LEVEL_CARD_SIZE, Image.Resampling.LANCZOS
    )

    # تعتيم تدريجي خفيف من الجهة اليسرى، من دون إنشاء صندوق حول المعلومات.
    dark_overlay = Image.new("RGBA", LEVEL_CARD_SIZE, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(dark_overlay)
    for x in range(500):
        alpha = max(0, 145 - int(x * 145 / 500))
        overlay_draw.line((x, 0, x, LEVEL_CARD_SIZE[1]), fill=(3, 4, 8, alpha))
    canvas = Image.alpha_composite(canvas, dark_overlay)

    draw = ImageDraw.Draw(canvas)

    avatar = Image.open(BytesIO(avatar_bytes)).convert("RGBA")
    avatar = ImageOps.fit(avatar, (104, 104), centering=(0.5, 0.5))
    avatar_mask = Image.new("L", avatar.size, 0)
    ImageDraw.Draw(avatar_mask).ellipse((0, 0, 103, 103), fill=255)
    avatar_x, avatar_y = 42, 53
    canvas.paste(avatar, (avatar_x, avatar_y), avatar_mask)
    draw.ellipse(
        (avatar_x - 3, avatar_y - 3, avatar_x + 107, avatar_y + 107),
        outline=(240, 240, 245, 235),
        width=3,
    )

    name_font = load_font(25, bold=True)
    label_font = load_font(14, bold=True)
    small_font = load_font(12)
    name = fit_text(draw, display_name, name_font, 280)
    draw.text((172, 32), name, font=name_font, fill=(255, 255, 255, 255))
    draw.text(
        (172, 67),
        f"TOTAL LEVEL {level}",
        font=label_font,
        fill=(220, 220, 230, 245),
    )

    draw.text(
        (172, 88),
        f"TOTAL XP  {xp:,} / {next_level_xp:,}",
        font=small_font,
        fill=(225, 225, 235, 235),
    )

    chat_level, chat_next_xp, chat_percent = progress_for_xp(chat_xp)
    voice_level, voice_next_xp, voice_percent = progress_for_xp(voice_xp)

    def draw_progress_bar(
        label: str,
        current_level: int,
        current_xp: int,
        next_xp: int,
        percent: int,
        label_y: int,
        bar_y: int,
        fill_color: tuple[int, int, int, int],
    ) -> None:
        draw.text(
            (172, label_y),
            f"{label}  •  LEVEL {current_level}  •  {percent}%",
            font=small_font,
            fill=(225, 225, 235, 255),
        )
        bar_x, bar_width, bar_height = 172, 270, 11
        draw.rounded_rectangle(
            (bar_x, bar_y, bar_x + bar_width, bar_y + bar_height),
            radius=6,
            fill=(75, 78, 92, 255),
        )
        filled_width = int(bar_width * percent / 100)
        if filled_width:
            draw.rounded_rectangle(
                (bar_x, bar_y, bar_x + filled_width, bar_y + bar_height),
                radius=6,
                fill=fill_color,
            )
        draw.text(
            (452, label_y),
            f"{current_xp:,}/{next_xp:,}",
            font=small_font,
            fill=(190, 190, 202, 255),
        )

    draw_progress_bar(
        "CHAT XP",
        chat_level,
        chat_xp,
        chat_next_xp,
        chat_percent,
        label_y=106,
        bar_y=124,
        fill_color=(226, 230, 255, 255),
    )
    draw_progress_bar(
        "VOICE XP",
        voice_level,
        voice_xp,
        voice_next_xp,
        voice_percent,
        label_y=151,
        bar_y=169,
        fill_color=(238, 238, 238, 255),
    )

    # العلامة المائية في الزاوية السفلى اليمنى، بعيدًا عن مساحة البيانات.
    watermark_font = ImageFont.truetype(str(WATERMARK_FONT_PATH), 12)
    watermark_bbox = draw.textbbox((0, 0), SERVER_WATERMARK, font=watermark_font)
    watermark_width = watermark_bbox[2] - watermark_bbox[0]
    draw.text(
        (LEVEL_CARD_SIZE[0] - watermark_width - 24, 207),
        SERVER_WATERMARK,
        font=watermark_font,
        fill=(190, 190, 198, 255),
    )

    output = BytesIO()
    canvas.convert("RGB").save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


def parse_timeout_duration(argument: str) -> timedelta:
    """يحوّل 30s أو 10m أو 2h أو 1d إلى مدة صالحة للتايم أوت."""
    match = re.fullmatch(r"(?i)(\d+)(s|m|h|d)", argument.strip())
    if not match:
        raise ValueError(
            "المدة يجب أن تكون مثل `30s` أو `10m` أو `2h` أو `1d`."
        )

    amount = int(match.group(1))
    unit = match.group(2).lower()
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    seconds = amount * multipliers[unit]
    if seconds < 1 or seconds > MAX_TIMEOUT_SECONDS:
        raise ValueError("مدة التايم أوت يجب أن تكون بين ثانية و28 يومًا.")
    return timedelta(seconds=seconds)


class TimeoutDuration(commands.Converter):
    """محول مدة أمر Prefix القديم."""

    async def convert(self, ctx: commands.Context, argument: str) -> timedelta:
        try:
            return parse_timeout_duration(argument)
        except ValueError as error:
            raise commands.BadArgument(str(error)) from error


def moderation_target_error(
    guild: discord.Guild, moderator: discord.Member, member: discord.Member
) -> Optional[str]:
    """يعيد سبب رفض الإجراء حسب ترتيب الأدوار، أو None إذا كان مسموحًا."""
    if member == moderator:
        return "لا يمكنك استخدام هذا الأمر على نفسك."
    if member == guild.owner:
        return "لا يمكن تنفيذ هذا الأمر على مالك السيرفر."

    bot_member = guild.me
    if bot_member is None or member.top_role >= bot_member.top_role:
        return "رتبة البوت يجب أن تكون أعلى من رتبة العضو المستهدف."
    if moderator != guild.owner and member.top_role >= moderator.top_role:
        return "لا يمكنك إدارة عضو رتبته مساوية أو أعلى من رتبتك."
    return None


async def send_level_up_message(
    member: discord.Member,
    new_level: int,
    source: str,
    visual_test: bool = False,
) -> None:
    """يرسل إشعار رفع المستوى إلى قناة الإشعارات الثابتة."""
    channel = bot.get_channel(LEVEL_UP_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(LEVEL_UP_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    source_labels = {
        "chat": "الشات",
        "voice": "الرومات الصوتية",
        "both": "اختبار XP",
    }
    source_label = source_labels.get(source, "XP")
    try:
        title = "🎨 تجربة ترقية بصرية" if visual_test else "🎉 ترقية جديدة!"
        description = (
            f"تجربة بصرية لـ {member.mention}!\n"
            f"المستوى الوهمي: **{new_level}**.\n"
            "لم يتم تغيير XP الحقيقي."
            if visual_test
            else f"مبروك {member.mention}!\nوصلت إلى المستوى **{new_level}**."
        )
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.gold(),
        )
        embed.add_field(name="مصدر الترقية", value=source_label, inline=True)
        embed.set_thumbnail(url=member.display_avatar.url)
        await channel.send(content=member.mention, embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        return


@tasks.loop(minutes=1)
async def voice_xp_loop() -> None:
    """يمنح Voice XP لكل عضو موجود في روم صوتي مرة كل دقيقة."""
    for guild in bot.guilds:
        for voice_channel in guild.voice_channels:
            for member in voice_channel.members:
                if member.bot:
                    continue

                old_xp, new_xp = add_member_xp(
                    guild.id,
                    member.id,
                    VOICE_XP_PER_MINUTE,
                    source="voice",
                )
                old_level = level_from_xp(old_xp)
                new_level = level_from_xp(new_xp)
                if new_level > old_level:
                    await send_level_up_message(member, new_level, "voice")


@voice_xp_loop.before_loop
async def before_voice_xp_loop() -> None:
    await bot.wait_until_ready()
    await asyncio.sleep(60)


async def sync_application_commands() -> None:
    """يسجل أوامر السلاش في سيرفر قناة الليفل، مع احتياط للمزامنة العامة."""
    global slash_commands_synced
    if slash_commands_synced:
        return

    target_guild: Optional[discord.Guild] = None
    level_channel = bot.get_channel(LEVEL_UP_CHANNEL_ID)
    if level_channel is None:
        try:
            level_channel = await bot.fetch_channel(LEVEL_UP_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            level_channel = None

    if level_channel is not None:
        target_guild = getattr(level_channel, "guild", None)
    if target_guild is None and len(bot.guilds) == 1:
        target_guild = bot.guilds[0]

    if target_guild is not None:
        bot.tree.copy_global_to(guild=target_guild)
        synced_commands = await bot.tree.sync(guild=target_guild)
        print(
            f"تمت مزامنة {len(synced_commands)} أمر Slash مع السيرفر "
            f"{target_guild.name} ({target_guild.id})"
        )
    else:
        synced_commands = await bot.tree.sync()
        print(f"تمت المزامنة العامة لـ {len(synced_commands)} أمر Slash")

    slash_commands_synced = True


@bot.event
async def on_ready() -> None:
    print(f"تم تسجيل الدخول باسم {bot.user}")
    try:
        await sync_application_commands()
    except discord.HTTPException as error:
        print(f"فشلت مزامنة أوامر Slash: {error}")
    if not voice_xp_loop.is_running():
        voice_xp_loop.start()


@bot.event
async def on_message(message: discord.Message) -> None:
    if not message.author.bot and message.guild is not None:
        cooldown_key = (message.guild.id, message.author.id)
        now = time.monotonic()
        last_award = xp_cooldowns.get(cooldown_key, 0)

        if now - last_award >= XP_COOLDOWN_SECONDS:
            xp_cooldowns[cooldown_key] = now
            old_xp, new_xp = add_member_xp(
                message.guild.id,
                message.author.id,
                random.randint(MIN_MESSAGE_XP, MAX_MESSAGE_XP),
                source="chat",
            )
            old_level = level_from_xp(old_xp)
            new_level = level_from_xp(new_xp)
            if new_level > old_level:
                await send_level_up_message(message.author, new_level, "chat")

    await bot.process_commands(message)


def recommended_test_xp(chat_xp: int, voice_xp: int) -> int:
    """يضمن أن اختبار !test level يعبر مستوى الشات والصوت والمستوى الإجمالي."""
    total_xp = chat_xp + voice_xp
    _, total_next_xp, _ = progress_for_xp(total_xp)
    _, chat_next_xp, _ = progress_for_xp(chat_xp)
    _, voice_next_xp, _ = progress_for_xp(voice_xp)
    return max(
        500,
        total_next_xp - total_xp + 1,
        chat_next_xp - chat_xp + 1,
        voice_next_xp - voice_xp + 1,
    )


def random_visual_test_stats() -> tuple[int, int, int, int]:
    """ينشئ إحصاءات وهمية لأمر اختبار البطاقة دون لمس قاعدة البيانات."""
    fake_chat_xp = random.randint(1_000, 20_000)
    fake_voice_xp = random.randint(1_000, 20_000)
    fake_total_xp = fake_chat_xp + fake_voice_xp
    fake_level = level_from_xp(fake_total_xp)
    fake_next_level_xp = (fake_level + 1) ** 2 * 100
    return fake_chat_xp, fake_voice_xp, fake_level, fake_next_level_xp


@bot.group(name="test", invoke_without_command=True)
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def test_group(ctx: commands.Context) -> None:
    """أوامر اختبار الإدارة."""
    if ctx.invoked_subcommand is None:
        await ctx.send("الاستخدام: `!test level`")


@test_group.command(name="level")
async def test_level(ctx: commands.Context) -> None:
    """يعرض بطاقة وترقية وهميتين للاختبار دون تعديل XP الحقيقي."""
    chat_xp, voice_xp, fake_level, next_level_xp = random_visual_test_stats()
    fake_total_xp = chat_xp + voice_xp
    avatar_bytes = await ctx.author.display_avatar.read()
    card = render_level_card(
        display_name=ctx.author.display_name,
        avatar_bytes=avatar_bytes,
        level=fake_level,
        xp=fake_total_xp,
        next_level_xp=next_level_xp,
        chat_xp=chat_xp,
        voice_xp=voice_xp,
    )
    await ctx.send(
        "🎨 تم إنشاء بطاقة ليفل تجريبية بأرقام عشوائية. "
        "لم يتم تعديل XP الحقيقي في قاعدة البيانات.\n"
        f"المستوى الوهمي: **{fake_level}** | "
        f"Chat XP: **{chat_xp:,}** | Voice XP: **{voice_xp:,}**",
        file=discord.File(card, filename="visual-level-test.png"),
    )
    await send_level_up_message(
        ctx.author,
        fake_level,
        "both",
        visual_test=True,
    )


@bot.tree.command(name="ping", description="يرد برسالة Pong")
async def ping_slash(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("Pong!")


@bot.tree.command(name="clear", description="مسح عدد من الرسائل")
@app_commands.describe(amount="عدد الرسائل من 1 إلى 100")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.checks.bot_has_permissions(
    manage_messages=True, read_message_history=True
)
async def clear_slash(interaction: discord.Interaction, amount: int) -> None:
    if amount < 1 or amount > 100:
        await interaction.response.send_message(
            "عدد الرسائل يجب أن يكون بين 1 و100.", ephemeral=True
        )
        return
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message(
            "لا يمكن مسح الرسائل في هذه القناة.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    deleted_messages = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(
        f"تم مسح **{len(deleted_messages)}** رسالة.", ephemeral=True
    )


@bot.tree.command(name="avatar", description="عرض الصورة الشخصية")
@app_commands.describe(member="العضو المطلوب عرض صورته")
@app_commands.guild_only()
async def avatar_slash(
    interaction: discord.Interaction, member: Optional[discord.Member] = None
) -> None:
    target = member or interaction.user
    embed = discord.Embed(
        title=f"الصورة الشخصية لـ {target.display_name}",
        color=discord.Color.blurple(),
    )
    embed.set_image(url=target.display_avatar.url)
    embed.set_footer(text=f"معرّف العضو: {target.id}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="userinfo", description="عرض معلومات الحساب والعضوية")
@app_commands.describe(member="العضو المطلوب عرض معلوماته")
@app_commands.guild_only()
async def userinfo_slash(
    interaction: discord.Interaction, member: Optional[discord.Member] = None
) -> None:
    target = member or interaction.user
    roles = [role.mention for role in reversed(target.roles[1:])]
    roles_text = ", ".join(roles) if roles else "لا توجد أدوار إضافية"
    if len(roles_text) > 1024:
        roles_text = f"{roles_text[:1021]}..."

    embed = discord.Embed(
        title=f"معلومات {target.display_name}",
        color=discord.Color.blurple(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="اسم المستخدم", value=str(target), inline=True)
    embed.add_field(name="معرّف الحساب", value=str(target.id), inline=True)
    embed.add_field(
        name="تاريخ إنشاء الحساب",
        value=discord.utils.format_dt(target.created_at, style="F"),
        inline=False,
    )
    if target.joined_at is not None:
        embed.add_field(
            name="تاريخ الانضمام للسيرفر",
            value=discord.utils.format_dt(target.joined_at, style="F"),
            inline=False,
        )
    embed.add_field(name="أعلى رتبة", value=target.top_role.mention, inline=True)
    embed.add_field(name="الأدوار", value=roles_text, inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="kick", description="طرد عضو من السيرفر")
@app_commands.describe(member="العضو المطلوب طرده", reason="سبب الطرد")
@app_commands.guild_only()
@app_commands.default_permissions(kick_members=True)
@app_commands.checks.has_permissions(kick_members=True)
@app_commands.checks.bot_has_permissions(kick_members=True)
async def kick_slash(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "بدون سبب",
) -> None:
    failure = moderation_target_error(interaction.guild, interaction.user, member)
    if failure:
        await interaction.response.send_message(failure, ephemeral=True)
        return
    await interaction.response.defer()
    await member.kick(reason=f"{interaction.user}: {reason}")
    await interaction.followup.send(f"تم طرد {member.mention}. السبب: {reason}")


@bot.tree.command(name="ban", description="حظر عضو من السيرفر")
@app_commands.describe(member="العضو المطلوب حظره", reason="سبب الحظر")
@app_commands.guild_only()
@app_commands.default_permissions(ban_members=True)
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.checks.bot_has_permissions(ban_members=True)
async def ban_slash(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "بدون سبب",
) -> None:
    failure = moderation_target_error(interaction.guild, interaction.user, member)
    if failure:
        await interaction.response.send_message(failure, ephemeral=True)
        return
    await interaction.response.defer()
    await member.ban(reason=f"{interaction.user}: {reason}")
    await interaction.followup.send(f"تم تبنيد {member.mention}. السبب: {reason}")


@bot.tree.command(name="timeout", description="وضع عضو في تايم أوت")
@app_commands.describe(
    member="العضو المطلوب تقييده",
    duration="المدة مثل 30s أو 10m أو 2h أو 1d",
    reason="سبب التايم أوت",
)
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.checks.bot_has_permissions(moderate_members=True)
async def timeout_slash(
    interaction: discord.Interaction,
    member: discord.Member,
    duration: str,
    reason: str = "بدون سبب",
) -> None:
    try:
        timeout_duration = parse_timeout_duration(duration)
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return

    failure = moderation_target_error(interaction.guild, interaction.user, member)
    if failure:
        await interaction.response.send_message(failure, ephemeral=True)
        return
    await interaction.response.defer()
    await member.timeout(timeout_duration, reason=f"{interaction.user}: {reason}")
    await interaction.followup.send(
        f"تم وضع {member.mention} في تايم أوت لمدة **{duration}**. السبب: {reason}"
    )


@bot.tree.command(name="warn", description="تسجيل تحذير لعضو")
@app_commands.describe(member="العضو المطلوب تحذيره", reason="سبب التحذير")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
@app_commands.checks.has_permissions(manage_messages=True)
async def warn_slash(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "بدون سبب",
) -> None:
    failure = moderation_target_error(interaction.guild, interaction.user, member)
    if failure:
        await interaction.response.send_message(failure, ephemeral=True)
        return

    warning_id = add_warning(
        interaction.guild.id, member.id, interaction.user.id, reason
    )
    try:
        await member.send(
            f"تم تحذيرك في سيرفر **{interaction.guild.name}**. السبب: {reason}"
        )
    except discord.Forbidden:
        pass
    await interaction.response.send_message(
        f"تم تحذير {member.mention}. رقم التحذير: **#{warning_id}**. السبب: {reason}"
    )


@bot.tree.command(name="addxp", description="إضافة XP للشات والصوت للاختبار")
@app_commands.describe(
    amount="مقدار XP من 1 إلى 100000",
    member="العضو المستهدف، أو اتركه فارغًا لنفسك",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def addxp_slash(
    interaction: discord.Interaction,
    amount: int,
    member: Optional[discord.Member] = None,
) -> None:
    if amount < 1 or amount > 100_000:
        await interaction.response.send_message(
            "مقدار XP يجب أن يكون بين 1 و100000.", ephemeral=True
        )
        return

    target = member or interaction.user
    old_xp, new_xp = add_member_xp(
        interaction.guild.id, target.id, amount, source="both"
    )
    await interaction.response.send_message(
        f"تمت إضافة **{amount:,} XP** إلى الشات والصوت للعضو {target.mention}."
    )
    if level_from_xp(new_xp) > level_from_xp(old_xp):
        await send_level_up_message(target, level_from_xp(new_xp), "both")


@bot.tree.command(name="level", description="عرض بطاقة المستوى")
@app_commands.describe(member="العضو المطلوب عرض مستواه")
@app_commands.guild_only()
async def level_slash(
    interaction: discord.Interaction, member: Optional[discord.Member] = None
) -> None:
    target = member or interaction.user
    xp = get_member_xp(interaction.guild.id, target.id)
    chat_xp, voice_xp = get_member_xp_breakdown(interaction.guild.id, target.id)
    current_level = level_from_xp(xp)
    next_level_xp = (current_level + 1) ** 2 * 100
    avatar_bytes = await target.display_avatar.read()
    card = render_level_card(
        display_name=target.display_name,
        avatar_bytes=avatar_bytes,
        level=current_level,
        xp=xp,
        next_level_xp=next_level_xp,
        chat_xp=chat_xp,
        voice_xp=voice_xp,
    )
    await interaction.response.send_message(
        file=discord.File(card, filename="level-card.png")
    )


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    original_error = getattr(error, "original", error)
    if isinstance(error, app_commands.MissingPermissions):
        message = "ليس لديك الصلاحية المطلوبة لاستخدام هذا الأمر."
    elif isinstance(error, app_commands.BotMissingPermissions):
        message = "البوت لا يملك الصلاحيات المطلوبة لتنفيذ هذا الأمر."
    elif isinstance(error, app_commands.TransformerError):
        message = "تعذر قراءة أحد المدخلات. تحقق من العضو أو القيمة المدخلة."
    elif isinstance(original_error, discord.Forbidden):
        message = "رفض Discord تنفيذ العملية. تحقق من الصلاحيات وترتيب الأدوار."
    elif isinstance(
        original_error, (FileNotFoundError, OSError, ValueError, discord.HTTPException)
    ):
        message = "تعذر تنفيذ الأمر حاليًا. تحقق من الإعدادات والصلاحيات."
    else:
        message = "حدث خطأ غير متوقع أثناء تنفيذ الأمر."

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


@bot.event
async def on_command_error(
    ctx: commands.Context, error: commands.CommandError
) -> None:
    original_error = getattr(error, "original", error)

    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.send("هذا الأمر متاح داخل السيرفر فقط.")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("ليس لديك الصلاحية المطلوبة لاستخدام هذا الأمر.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("البوت لا يملك الصلاحيات المطلوبة لتنفيذ هذا الأمر.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"بيانات ناقصة. الاستخدام الصحيح: `!{ctx.command.name} ...`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(str(error))
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"انتظر {error.retry_after:.1f} ثانية قبل المحاولة مجددًا.")
    elif isinstance(original_error, discord.Forbidden):
        await ctx.send("رفض Discord تنفيذ العملية. تحقق من الصلاحيات وترتيب الأدوار.")
    elif isinstance(
        original_error, (FileNotFoundError, OSError, ValueError, discord.HTTPException)
    ):
        await ctx.send("تعذر إنشاء بطاقة الليفل حاليًا. تحقق من خلفية البطاقة وصورة العضو.")
    else:
        raise error


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "لم يتم العثور على DISCORD_TOKEN. أضف توكن البوت كمتغير بيئة قبل التشغيل."
        )

    start_keep_alive_server()
    bot.run(token)

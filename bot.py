import asyncio
import os
import random
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
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
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

DATABASE_PATH = os.getenv("LEVELS_DATABASE", "levels.sqlite3")
XP_COOLDOWN_SECONDS = 60
MIN_MESSAGE_XP = 15
MAX_MESSAGE_XP = 25
MAX_TIMEOUT_SECONDS = 28 * 24 * 60 * 60
VOICE_XP_PER_MINUTE = 10
LEVEL_CARD_SIZE = (736, 230)
LEVEL_CARD_BACKGROUND = (
    Path(__file__).resolve().parent
    / "attached_assets"
    / "background.png"
)
PROFILE_CARD_SIZE = (500, 500)
PROFILE_CARD_BACKGROUND = (
    Path(__file__).resolve().parent
    / "attached_assets"
    / "profile_background.png"
)
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
SERVER_WATERMARK = "Noir Bloom"
KEEP_ALIVE_HOST = "0.0.0.0"
KEEP_ALIVE_PORT = int(os.getenv("PORT", "8080"))

# ---------------------------------------------------------------------------
# إعدادات نظام Petals (العملة الخاصة بالسيرفر)
# ---------------------------------------------------------------------------
PETALS_EMOJI = "<a:white:1537914794080862339>"
TOP_TITLE_EMOJI = "<a:White:1537914412688736410>"
TOP_TEXT_EMOJI = "<a:message:1548210084742434867>"
TOP_VOICE_EMOJI = "<a:mic:1548210304557654028>"
THEME_COLOR = discord.Color.from_rgb(60, 60, 66)
DAILY_COOLDOWN_HOURS = 24
DAILY_MIN_AMOUNT = 100
DAILY_MAX_AMOUNT = 500
MAX_ADMIN_GRANT = 10_000_000

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

    CREATE TABLE IF NOT EXISTS member_economy (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        petals INTEGER NOT NULL DEFAULT 0,
        last_daily TEXT,
        PRIMARY KEY (guild_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id INTEGER PRIMARY KEY,
        commands_channel_id INTEGER,
        level_channel_id INTEGER,
        join_role_id INTEGER
    );

    CREATE TABLE IF NOT EXISTS level_role_rewards (
        guild_id INTEGER NOT NULL,
        level INTEGER NOT NULL,
        role_id INTEGER NOT NULL,
        PRIMARY KEY (guild_id, level)
    );

    CREATE TABLE IF NOT EXISTS guild_command_channels (
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        PRIMARY KEY (guild_id, channel_id)
    );

    CREATE TABLE IF NOT EXISTS member_period_xp (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        period_type TEXT NOT NULL,
        period_key TEXT NOT NULL,
        chat_xp INTEGER NOT NULL DEFAULT 0,
        voice_xp INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id, period_type, period_key)
    );

    CREATE TABLE IF NOT EXISTS member_points (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        points INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS member_reputation (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        points INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS reputation_cooldowns (
        guild_id INTEGER NOT NULL,
        giver_id INTEGER NOT NULL,
        last_given TEXT NOT NULL,
        PRIMARY KEY (guild_id, giver_id)
    );

    CREATE TABLE IF NOT EXISTS guild_mute_roles (
        guild_id INTEGER PRIMARY KEY,
        role_id INTEGER NOT NULL
    );
    """
)

# هجرة بسيطة: نقل الروم القديم (لو كان محددًا من قبل) للجدول الجديد.
_legacy_channels = database.execute(
    "SELECT guild_id, commands_channel_id FROM guild_settings WHERE commands_channel_id IS NOT NULL"
).fetchall()
for _row in _legacy_channels:
    database.execute(
        "INSERT OR IGNORE INTO guild_command_channels (guild_id, channel_id) VALUES (?, ?)",
        (_row["guild_id"], _row["commands_channel_id"]),
    )
database.commit()
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

guild_settings_columns = {
    row["name"]
    for row in database.execute("PRAGMA table_info(guild_settings)").fetchall()
}
if "level_channel_id" not in guild_settings_columns:
    database.execute("ALTER TABLE guild_settings ADD COLUMN level_channel_id INTEGER")
if "join_role_id" not in guild_settings_columns:
    database.execute("ALTER TABLE guild_settings ADD COLUMN join_role_id INTEGER")
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
    add_period_xp(guild_id, user_id, amount, source)
    return old_xp, new_xp


def get_level_leaderboard(guild_id: int, limit: int = 10) -> list[sqlite3.Row]:
    return database.execute(
        """
        SELECT user_id, xp FROM member_levels
        WHERE guild_id = ?
        ORDER BY xp DESC
        LIMIT ?
        """,
        (guild_id, limit),
    ).fetchall()


def current_period_key(period_type: str) -> str:
    """يحسب مفتاح الفترة الحالية (يوم/أسبوع/شهر) بتوقيت UTC."""
    now = datetime.now(timezone.utc)
    if period_type == "day":
        return now.strftime("%Y-%m-%d")
    if period_type == "week":
        iso_year, iso_week, _ = now.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if period_type == "month":
        return now.strftime("%Y-%m")
    raise ValueError(f"نوع فترة غير معروف: {period_type}")


def add_period_xp(guild_id: int, user_id: int, amount: int, source: str) -> None:
    """يحدّث إحصائيات الفترات الثلاث (يومي/أسبوعي/شهري) دفعة وحدة."""
    if source == "chat":
        chat_add, voice_add = amount, 0
    elif source == "voice":
        chat_add, voice_add = 0, amount
    else:
        chat_add, voice_add = amount, amount

    for period_type in ("day", "week", "month"):
        period_key = current_period_key(period_type)
        database.execute(
            """
            INSERT INTO member_period_xp
                (guild_id, user_id, period_type, period_key, chat_xp, voice_xp)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id, period_type, period_key)
            DO UPDATE SET
                chat_xp = chat_xp + excluded.chat_xp,
                voice_xp = voice_xp + excluded.voice_xp
            """,
            (guild_id, user_id, period_type, period_key, chat_add, voice_add),
        )
    database.commit()


def get_period_chat_leaderboard(
    guild_id: int, period_type: str, limit: int = 5, offset: int = 0
) -> list[sqlite3.Row]:
    period_key = current_period_key(period_type)
    return database.execute(
        """
        SELECT user_id, chat_xp FROM member_period_xp
        WHERE guild_id = ? AND period_type = ? AND period_key = ?
        ORDER BY chat_xp DESC
        LIMIT ? OFFSET ?
        """,
        (guild_id, period_type, period_key, limit, offset),
    ).fetchall()


def get_period_voice_leaderboard(
    guild_id: int, period_type: str, limit: int = 5, offset: int = 0
) -> list[sqlite3.Row]:
    period_key = current_period_key(period_type)
    return database.execute(
        """
        SELECT user_id, voice_xp FROM member_period_xp
        WHERE guild_id = ? AND period_type = ? AND period_key = ?
        ORDER BY voice_xp DESC
        LIMIT ? OFFSET ?
        """,
        (guild_id, period_type, period_key, limit, offset),
    ).fetchall()


def get_chat_leaderboard(guild_id: int, limit: int = 5, offset: int = 0) -> list[sqlite3.Row]:
    return database.execute(
        """
        SELECT user_id, chat_xp FROM member_levels
        WHERE guild_id = ?
        ORDER BY chat_xp DESC
        LIMIT ? OFFSET ?
        """,
        (guild_id, limit, offset),
    ).fetchall()


def get_voice_leaderboard(guild_id: int, limit: int = 5, offset: int = 0) -> list[sqlite3.Row]:
    return database.execute(
        """
        SELECT user_id, voice_xp FROM member_levels
        WHERE guild_id = ?
        ORDER BY voice_xp DESC
        LIMIT ? OFFSET ?
        """,
        (guild_id, limit, offset),
    ).fetchall()


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


def get_member_warnings(guild_id: int, user_id: int) -> list[sqlite3.Row]:
    return database.execute(
        """
        SELECT id, moderator_id, reason, created_at FROM member_warnings
        WHERE guild_id = ? AND user_id = ?
        ORDER BY id DESC
        """,
        (guild_id, user_id),
    ).fetchall()


def get_guild_warnings(guild_id: int, limit: int = 15) -> list[sqlite3.Row]:
    return database.execute(
        """
        SELECT id, user_id, moderator_id, reason, created_at FROM member_warnings
        WHERE guild_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (guild_id, limit),
    ).fetchall()


def remove_warning(guild_id: int, warning_id: int) -> bool:
    cursor = database.execute(
        "DELETE FROM member_warnings WHERE guild_id = ? AND id = ?",
        (guild_id, warning_id),
    )
    database.commit()
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# دوال نظام النقاط (Points) — نظام مستقل عن Petals وXP، يُستخدم للفعاليات
# ---------------------------------------------------------------------------
def get_points(guild_id: int, user_id: int) -> int:
    row = database.execute(
        "SELECT points FROM member_points WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    return int(row["points"]) if row else 0


def set_points(guild_id: int, user_id: int, amount: int) -> None:
    database.execute(
        """
        INSERT INTO member_points (guild_id, user_id, points)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id, user_id)
        DO UPDATE SET points = excluded.points
        """,
        (guild_id, user_id, amount),
    )
    database.commit()


def add_points(guild_id: int, user_id: int, amount: int) -> int:
    new_amount = max(0, get_points(guild_id, user_id) + amount)
    set_points(guild_id, user_id, new_amount)
    return new_amount


def get_points_list(guild_id: int) -> list[sqlite3.Row]:
    return database.execute(
        """
        SELECT user_id, points FROM member_points
        WHERE guild_id = ? AND points != 0
        ORDER BY points DESC
        """,
        (guild_id,),
    ).fetchall()


def reset_points(guild_id: int, user_id: Optional[int] = None) -> None:
    if user_id is None:
        database.execute("DELETE FROM member_points WHERE guild_id = ?", (guild_id,))
    else:
        database.execute(
            "DELETE FROM member_points WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
    database.commit()


# ---------------------------------------------------------------------------
# دوال نظام نقاط السمعة (Reputation)
# ---------------------------------------------------------------------------
def get_reputation(guild_id: int, user_id: int) -> int:
    row = database.execute(
        "SELECT points FROM member_reputation WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    return int(row["points"]) if row else 0


def add_reputation(guild_id: int, user_id: int, amount: int = 1) -> int:
    current = get_reputation(guild_id, user_id)
    new_amount = max(0, current + amount)
    database.execute(
        """
        INSERT INTO member_reputation (guild_id, user_id, points)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id, user_id)
        DO UPDATE SET points = excluded.points
        """,
        (guild_id, user_id, new_amount),
    )
    database.commit()
    return new_amount


def get_reputation_cooldown(guild_id: int, giver_id: int) -> Optional[datetime]:
    row = database.execute(
        "SELECT last_given FROM reputation_cooldowns WHERE guild_id = ? AND giver_id = ?",
        (guild_id, giver_id),
    ).fetchone()
    if not row:
        return None
    try:
        return datetime.fromisoformat(row["last_given"])
    except ValueError:
        return None


def set_reputation_cooldown(guild_id: int, giver_id: int, when: datetime) -> None:
    database.execute(
        """
        INSERT INTO reputation_cooldowns (guild_id, giver_id, last_given)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id, giver_id)
        DO UPDATE SET last_given = excluded.last_given
        """,
        (guild_id, giver_id, when.isoformat()),
    )
    database.commit()


# ---------------------------------------------------------------------------
# دوال إدارة رول الكتم النصي
# ---------------------------------------------------------------------------
def get_mute_role_id(guild_id: int) -> Optional[int]:
    row = database.execute(
        "SELECT role_id FROM guild_mute_roles WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    return int(row["role_id"]) if row else None


def set_mute_role_id(guild_id: int, role_id: int) -> None:
    database.execute(
        """
        INSERT INTO guild_mute_roles (guild_id, role_id)
        VALUES (?, ?)
        ON CONFLICT(guild_id)
        DO UPDATE SET role_id = excluded.role_id
        """,
        (guild_id, role_id),
    )
    database.commit()


async def get_or_create_mute_role(guild: discord.Guild) -> discord.Role:
    """يجيب رول الكتم النصي الخاص بالسيرفر، أو ينشئه لو ما كان موجود."""
    role_id = get_mute_role_id(guild.id)
    if role_id is not None:
        role = guild.get_role(role_id)
        if role is not None:
            return role

    role = discord.utils.get(guild.roles, name="🔇 مكتوم نصيًا")
    if role is None:
        role = await guild.create_role(
            name="🔇 مكتوم نصيًا", reason="إنشاء رول الكتم النصي التلقائي"
        )
        for channel in guild.text_channels:
            try:
                await channel.set_permissions(
                    role, send_messages=False, add_reactions=False
                )
            except (discord.Forbidden, discord.HTTPException):
                continue

    set_mute_role_id(guild.id, role.id)
    return role


# ---------------------------------------------------------------------------
# دوال إعدادات روم إشعارات الرفع ورتبة الدخول
# ---------------------------------------------------------------------------
def get_level_channel_id(guild_id: int) -> Optional[int]:
    row = database.execute(
        "SELECT level_channel_id FROM guild_settings WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()
    if not row or row["level_channel_id"] is None:
        return None
    return int(row["level_channel_id"])


def set_level_channel_id(guild_id: int, channel_id: int) -> None:
    database.execute(
        """
        INSERT INTO guild_settings (guild_id, level_channel_id)
        VALUES (?, ?)
        ON CONFLICT(guild_id)
        DO UPDATE SET level_channel_id = excluded.level_channel_id
        """,
        (guild_id, channel_id),
    )
    database.commit()


def get_join_role_id(guild_id: int) -> Optional[int]:
    row = database.execute(
        "SELECT join_role_id FROM guild_settings WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()
    if not row or row["join_role_id"] is None:
        return None
    return int(row["join_role_id"])


def set_join_role_id(guild_id: int, role_id: Optional[int]) -> None:
    database.execute(
        """
        INSERT INTO guild_settings (guild_id, join_role_id)
        VALUES (?, ?)
        ON CONFLICT(guild_id)
        DO UPDATE SET join_role_id = excluded.join_role_id
        """,
        (guild_id, role_id),
    )
    database.commit()


# ---------------------------------------------------------------------------
# دوال رتب المستويات التلقائية
# ---------------------------------------------------------------------------
def get_level_role_rewards(guild_id: int) -> list[sqlite3.Row]:
    return database.execute(
        "SELECT level, role_id FROM level_role_rewards WHERE guild_id = ? ORDER BY level ASC",
        (guild_id,),
    ).fetchall()


def set_level_role_reward(guild_id: int, level: int, role_id: int) -> None:
    database.execute(
        """
        INSERT INTO level_role_rewards (guild_id, level, role_id)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id, level)
        DO UPDATE SET role_id = excluded.role_id
        """,
        (guild_id, level, role_id),
    )
    database.commit()


def remove_level_role_reward(guild_id: int, level: int) -> bool:
    cursor = database.execute(
        "DELETE FROM level_role_rewards WHERE guild_id = ? AND level = ?",
        (guild_id, level),
    )
    database.commit()
    return cursor.rowcount > 0


async def apply_level_role_rewards(member: discord.Member, new_level: int) -> None:
    """يعطي العضو كل رتب المستويات المستحقة (حتى مستواه الحالي) لو ما كانت عنده."""
    rewards = get_level_role_rewards(member.guild.id)
    roles_to_add = []
    for row in rewards:
        if row["level"] <= new_level:
            role = member.guild.get_role(row["role_id"])
            if role is not None and role not in member.roles:
                roles_to_add.append(role)
    if roles_to_add:
        try:
            await member.add_roles(*roles_to_add, reason="مكافأة رتبة مستوى تلقائية")
        except (discord.Forbidden, discord.HTTPException):
            pass


# ---------------------------------------------------------------------------
# دوال نظام Petals
# ---------------------------------------------------------------------------
def get_petals(guild_id: int, user_id: int) -> int:
    row = database.execute(
        "SELECT petals FROM member_economy WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    return int(row["petals"]) if row else 0


def set_petals(guild_id: int, user_id: int, amount: int) -> None:
    database.execute(
        """
        INSERT INTO member_economy (guild_id, user_id, petals)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id, user_id)
        DO UPDATE SET petals = excluded.petals
        """,
        (guild_id, user_id, amount),
    )
    database.commit()


def add_petals(guild_id: int, user_id: int, amount: int) -> int:
    """يضيف (أو يطرح لو كان العدد سالب) بتلات ويعيد الرصيد الجديد."""
    current = get_petals(guild_id, user_id)
    new_amount = max(0, current + amount)
    set_petals(guild_id, user_id, new_amount)
    return new_amount


def get_last_daily(guild_id: int, user_id: int) -> Optional[datetime]:
    row = database.execute(
        "SELECT last_daily FROM member_economy WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    if not row or not row["last_daily"]:
        return None
    try:
        return datetime.fromisoformat(row["last_daily"])
    except ValueError:
        return None


def set_last_daily(guild_id: int, user_id: int, when: datetime) -> None:
    database.execute(
        """
        INSERT INTO member_economy (guild_id, user_id, petals, last_daily)
        VALUES (?, ?, 0, ?)
        ON CONFLICT(guild_id, user_id)
        DO UPDATE SET last_daily = excluded.last_daily
        """,
        (guild_id, user_id, when.isoformat()),
    )
    database.commit()


def get_commands_channels(guild_id: int) -> list[int]:
    rows = database.execute(
        "SELECT channel_id FROM guild_command_channels WHERE guild_id = ?",
        (guild_id,),
    ).fetchall()
    return [int(row["channel_id"]) for row in rows]


def add_commands_channel(guild_id: int, channel_id: int) -> None:
    database.execute(
        "INSERT OR IGNORE INTO guild_command_channels (guild_id, channel_id) VALUES (?, ?)",
        (guild_id, channel_id),
    )
    database.commit()


def remove_commands_channel(guild_id: int, channel_id: int) -> bool:
    cursor = database.execute(
        "DELETE FROM guild_command_channels WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id),
    )
    database.commit()
    return cursor.rowcount > 0


def get_petals_leaderboard(guild_id: int, limit: int = 10, offset: int = 0) -> list[sqlite3.Row]:
    return database.execute(
        """
        SELECT user_id, petals FROM member_economy
        WHERE guild_id = ?
        ORDER BY petals DESC
        LIMIT ? OFFSET ?
        """,
        (guild_id, limit, offset),
    ).fetchall()


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

    watermark_font = load_font(12, bold=False)
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


def render_profile_card(
    *,
    display_name: str,
    avatar_bytes: bytes,
    level: int,
    petals: int,
    petals_rank: Optional[int],
    current_xp: int,
    next_level_xp: int,
) -> BytesIO:
    """يرسم بطاقة بروفايل مصورة بأسلوب ProBot (أفاتار، إحصائيات، شريط XP)."""
    if not PROFILE_CARD_BACKGROUND.exists():
        raise FileNotFoundError(
            f"خلفية بطاقة البروفايل غير موجودة: {PROFILE_CARD_BACKGROUND}"
        )

    canvas = Image.open(PROFILE_CARD_BACKGROUND).convert("RGBA").resize(
        PROFILE_CARD_SIZE, Image.Resampling.LANCZOS
    )

    # تعتيم خفيف عام حتى تكون النصوص واضحة فوق أي جزء من الخلفية.
    dark_overlay = Image.new("RGBA", PROFILE_CARD_SIZE, (5, 3, 8, 110))
    canvas = Image.alpha_composite(canvas, dark_overlay)

    draw = ImageDraw.Draw(canvas)

    # الأفاتار أعلى اليسار بحدود زهرية.
    avatar = Image.open(BytesIO(avatar_bytes)).convert("RGBA")
    avatar = ImageOps.fit(avatar, (116, 116), centering=(0.5, 0.5))
    avatar_mask = Image.new("L", avatar.size, 0)
    ImageDraw.Draw(avatar_mask).ellipse((0, 0, 115, 115), fill=255)
    avatar_x, avatar_y = 28, 26
    canvas.paste(avatar, (avatar_x, avatar_y), avatar_mask)
    draw.ellipse(
        (avatar_x - 4, avatar_y - 4, avatar_x + 120, avatar_y + 120),
        outline=(255, 210, 230, 245),
        width=4,
    )

    name_font = load_font(30, bold=True)
    stat_label_font = load_font(15, bold=True)
    stat_value_font = load_font(34, bold=True)
    small_font = load_font(14)

    # اسم العضو يمين الأفاتار.
    name_x = avatar_x + 116 + 24
    name = fit_text(draw, display_name, name_font, PROFILE_CARD_SIZE[0] - name_x - 24)
    draw.text((name_x, avatar_y + 38), name, font=name_font, fill=(255, 255, 255, 255))

    # عمود الإحصائيات (LEVEL, PETALS, RANK) تحت الأفاتار، عمودي زي ProBot.
    stats = [
        ("LEVEL", str(level)),
        ("PETALS", f"{petals:,}"),
        ("RANK", f"#{petals_rank}" if petals_rank else "—"),
    ]
    stat_start_y = avatar_y + 116 + 34
    stat_gap = 78
    for index, (stat_label, stat_value) in enumerate(stats):
        y = stat_start_y + index * stat_gap
        draw.text((avatar_x, y), stat_label, font=stat_label_font, fill=(255, 195, 220, 255))
        draw.text((avatar_x, y + 20), stat_value, font=stat_value_font, fill=(255, 255, 255, 255))

    # شريط تقدم XP أسفل البطاقة.
    bar_x = 28
    bar_width = PROFILE_CARD_SIZE[0] - (bar_x * 2)
    bar_y = PROFILE_CARD_SIZE[1] - 56
    bar_height = 16
    progress_percent = min(100, max(0, int((current_xp / max(1, next_level_xp)) * 100)))

    draw.rounded_rectangle(
        (bar_x, bar_y, bar_x + bar_width, bar_y + bar_height),
        radius=8,
        fill=(45, 32, 42, 220),
    )
    filled_width = int(bar_width * progress_percent / 100)
    if filled_width:
        draw.rounded_rectangle(
            (bar_x, bar_y, bar_x + filled_width, bar_y + bar_height),
            radius=8,
            fill=(255, 200, 225, 255),
        )
    fraction_text = f"{current_xp:,} / {next_level_xp:,}"
    fraction_bbox = draw.textbbox((0, 0), fraction_text, font=small_font)
    fraction_width = fraction_bbox[2] - fraction_bbox[0]
    draw.text(
        (bar_x + bar_width - fraction_width, bar_y - 20),
        fraction_text,
        font=small_font,
        fill=(230, 220, 228, 255),
    )
    draw.text(
        (bar_x, bar_y - 20),
        f"TOTAL XP: {current_xp:,}",
        font=small_font,
        fill=(230, 220, 228, 255),
    )

    # العلامة المائية في الزاوية السفلى اليمنى.
    watermark_font = load_font(12, bold=False)
    watermark_bbox = draw.textbbox((0, 0), SERVER_WATERMARK, font=watermark_font)
    watermark_width = watermark_bbox[2] - watermark_bbox[0]
    draw.text(
        (PROFILE_CARD_SIZE[0] - watermark_width - 20, PROFILE_CARD_SIZE[1] - 20),
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
        return "لا يمكن تنفيذ هذا الأمر على مالك الخادم."

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
    """يرسل إشعار رفع المستوى إلى الروم المحدد، ويطبّق رتب المستويات التلقائية."""
    if not visual_test:
        await apply_level_role_rewards(member, new_level)

    level_channel_id = get_level_channel_id(member.guild.id)
    if level_channel_id is None:
        return

    channel = bot.get_channel(level_channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(level_channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    source_level_labels = {
        "chat": "الكتابي",
        "voice": "الصوتي",
        "both": "الكتابي والصوتي",
    }
    level_label = source_level_labels.get(source, "الكتابي")
    try:
        display_name = member.display_name
        title = f"　　. ︶ {display_name} leveled up　𖹭.ᐟ"
        if visual_test:
            title = f"🎨 (تجربة) {title}"

        description = (
            f"੭੭　  ݂  　 تهانيناً أيّتها الجميلة {member.mention} 　 ݂ "
            f"<a:white:1481419152198598830>  ꒱\n"
            f"　　✧　. 　<a:white:1481419279961292850>  "
            f"مستواكِ {level_label} حاليًا {new_level}   ㅤ.ㅤ  ౨౿"
        )

        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.from_rgb(255, 255, 255),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        guild = member.guild
        embed.set_footer(
            text=guild.name,
            icon_url=guild.icon.url if guild.icon else None,
        )
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
    """يسجل أوامر السلاش بكل سيرفر البوت موجود فيه (مزامنة فورية لكل سيرفر)."""
    global slash_commands_synced
    if slash_commands_synced:
        return

    for target_guild in bot.guilds:
        bot.tree.copy_global_to(guild=target_guild)
        synced_commands = await bot.tree.sync(guild=target_guild)
        print(
            f"تمت مزامنة {len(synced_commands)} أمر Slash مع السيرفر "
            f"{target_guild.name} ({target_guild.id})"
        )

    if not bot.guilds:
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
async def on_member_join(member: discord.Member) -> None:
    """يعطي رتبة الدخول التلقائية للعضو الجديد (لو كانت محددة)."""
    role_id = get_join_role_id(member.guild.id)
    if role_id is None:
        return
    role = member.guild.get_role(role_id)
    if role is None:
        return
    try:
        await member.add_roles(role, reason="رتبة الدخول التلقائية")
    except (discord.Forbidden, discord.HTTPException):
        pass


# ---------------------------------------------------------------------------
# اختصارات بدون علامة ! قبلها — حرف واحد أو كلمة قصيرة (زي pt)
# ---------------------------------------------------------------------------
BARE_SHORTCUTS = {"r", "p", "g", "d", "t", "a", "c", "pt"}


async def handle_single_letter_shortcut(message: discord.Message) -> bool:
    """يتحقق هل الرسالة اختصار مسموح، وينفذه إذا كان كذلك. يعيد True لو نُفذ."""
    if message.guild is None or message.author.bot:
        return False

    content = message.content.strip()
    if not content:
        return False

    parts = content.split()
    trigger = parts[0].lower()
    if trigger not in BARE_SHORTCUTS:
        return False

    ctx = await bot.get_context(message)
    args = parts[1:]

    try:
        if trigger == "r":
            await run_rank(ctx, ctx.author)
        elif trigger == "p":
            await run_profile(ctx, ctx.author)
        elif trigger == "a":
            target = message.mentions[0] if message.mentions else ctx.author
            await run_avatar(ctx, target)
        elif trigger in ("c", "pt"):
            target = message.mentions[0] if message.mentions else ctx.author
            amount = None
            if args:
                last = args[-1]
                if last.isdigit():
                    amount = int(last)
            if message.mentions and amount is not None:
                await run_give(ctx, target, amount)
            else:
                await run_balance(ctx, target)
        elif trigger == "t":
            period_arg = args[0].lower() if args else "all"
            period = PERIOD_WORDS.get(period_arg, "all")
            board_type = "level" if period_arg in PERIOD_WORDS else "petals"
            await run_top(ctx, board_type, period)
        elif trigger == "d":
            await run_daily(ctx)
        elif trigger == "g":
            if not message.mentions:
                await ctx.send(
                    f"طريقة الاستخدام: `g @العضو المبلغ` — مثال: `g @اسم_العضو 100`"
                )
                return True
            amount_text = args[-1] if args else ""
            if not amount_text.isdigit():
                await ctx.send("يجب تحديد مبلغ صحيح، مثال: `g @اسم_العضو 100`")
                return True
            await run_give(ctx, message.mentions[0], int(amount_text))
    except FileNotFoundError:
        await ctx.send("تعذر إنشاء البطاقة حاليًا، يرجى التحقق من ملفات الخلفية والخط.")
    except (OSError, ValueError, discord.HTTPException):
        await ctx.send("تعذر تنفيذ الأمر حاليًا، يرجى المحاولة مرة أخرى.")

    return True


# ---------------------------------------------------------------------------
# نظام الردود التلقائية الذكية
# ---------------------------------------------------------------------------
NAME_TRIGGERS = [
    "عايشة", "عائشة", "عيوش", "عووش", "عواش",
    "اش", "آش", "عواشه", "عايشه", "عائشه",
]
NAME_RESPONSES = ["عيونها", "اسطورة السيرفر"]

EXAM_TRIGGERS = ["قدرات", "تحصيلي", "قياس"]
EXAM_RESPONSE = "أعوذ بالله"

PRAISE_TRIGGERS = ["كفو", "اسطورتي"]
PRAISE_RESPONSES = ["ابشر بييي", "ازهلني", "عيوني"]

GENERIC_MENTION_RESPONSES = ["هلا", "عيوني"]


_WORD_PATTERN = re.compile(r"[\u0621-\u064A\u0660-\u0669A-Za-z0-9]+")


def _message_words(text: str) -> set[str]:
    """يستخرج كلمات الرسالة كاملة (بدون علامات ترقيم) للمطابقة الدقيقة."""
    return set(_WORD_PATTERN.findall(text))


def _contains_any(text: str, triggers: list[str]) -> bool:
    """يتحقق من وجود إحدى الكلمات بالضبط (كلمة كاملة، مو جزء من كلمة أخرى)."""
    words = _message_words(text)
    return any(trigger in words for trigger in triggers)


async def handle_smart_replies(message: discord.Message) -> None:
    """يرد تلقائيًا (كـ Reply على رسالة الشخص) على نداء الاسم، كلمات الاختبارات، المدح، والمنشن العام."""
    content = message.content

    if _contains_any(content, NAME_TRIGGERS):
        await message.reply(random.choice(NAME_RESPONSES), mention_author=True)
        return

    if _contains_any(content, EXAM_TRIGGERS):
        await message.reply(EXAM_RESPONSE, mention_author=True)
        return

    is_reply_to_bot = False
    if message.reference is not None:
        try:
            replied_message = message.reference.resolved
            if replied_message is None:
                replied_message = await message.channel.fetch_message(
                    message.reference.message_id
                )
            is_reply_to_bot = replied_message.author.id == bot.user.id
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            is_reply_to_bot = False

    # الرد على رسالة البوت: يرد بس لو فيه كلمة مدح، وإلا ما يرد بشي إطلاقًا.
    if is_reply_to_bot:
        if _contains_any(content, PRAISE_TRIGGERS):
            await message.reply(random.choice(PRAISE_RESPONSES), mention_author=True)
        return

    # منشن عادي (بدون رد على رسالة): يرد تحية عامة دايمًا، بغض النظر عن المحتوى.
    if bot.user in message.mentions:
        await message.reply(random.choice(GENERIC_MENTION_RESPONSES), mention_author=True)


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

    if not message.author.bot and message.guild is not None:
        await handle_smart_replies(message)

    handled = await handle_single_letter_shortcut(message)
    if handled:
        return

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


# ---------------------------------------------------------------------------
# دوال منطقية مشتركة (يستدعيها كل من أمر السلاش وأمر ! والاختصار الحرفي)
# ---------------------------------------------------------------------------
async def run_ping(ctx_or_interaction) -> None:
    await _reply(ctx_or_interaction, "Pong!")


async def run_avatar(ctx_or_interaction, target: discord.Member) -> None:
    if not await ensure_correct_channel(ctx_or_interaction):
        return
    embed = discord.Embed(
        title=f"الصورة الشخصية لـ {target.display_name}",
        color=THEME_COLOR,
    )
    embed.set_image(url=target.display_avatar.url)
    embed.set_footer(text=f"معرّف العضو: {target.id}")
    await _reply(ctx_or_interaction, embed=embed)


async def run_userinfo(ctx_or_interaction, target: discord.Member) -> None:
    if not await ensure_correct_channel(ctx_or_interaction):
        return
    embed = discord.Embed(
        title=target.display_name,
        color=discord.Color.from_rgb(0, 0, 0),
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(
        name="Joined Discord",
        value=discord.utils.format_dt(target.created_at, style="R"),
        inline=True,
    )
    if target.joined_at is not None:
        embed.add_field(
            name="Joined Server",
            value=discord.utils.format_dt(target.joined_at, style="R"),
            inline=True,
        )
    await _reply(ctx_or_interaction, embed=embed)


async def run_rank(ctx_or_interaction, target: discord.Member) -> None:
    if not await ensure_correct_channel(ctx_or_interaction):
        return
    guild_id = target.guild.id
    xp = get_member_xp(guild_id, target.id)
    chat_xp, voice_xp = get_member_xp_breakdown(guild_id, target.id)
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
    await _reply(ctx_or_interaction, file=discord.File(card, filename="rank-card.png"))


async def run_profile(ctx_or_interaction, target: discord.Member) -> None:
    if not await ensure_correct_channel(ctx_or_interaction):
        return
    guild_id = target.guild.id
    xp = get_member_xp(guild_id, target.id)
    level, next_level_xp, _ = progress_for_xp(xp)
    petals = get_petals(guild_id, target.id)

    leaderboard = get_petals_leaderboard(guild_id, limit=1000)
    petals_rank = next(
        (i + 1 for i, row in enumerate(leaderboard) if row["user_id"] == target.id),
        None,
    )

    avatar_bytes = await target.display_avatar.read()
    card = render_profile_card(
        display_name=target.display_name,
        avatar_bytes=avatar_bytes,
        level=level,
        petals=petals,
        petals_rank=petals_rank,
        current_xp=xp,
        next_level_xp=next_level_xp,
    )
    await _reply(ctx_or_interaction, file=discord.File(card, filename="profile-card.png"))


async def run_balance(ctx_or_interaction, target: discord.Member) -> None:
    if not await ensure_correct_channel(ctx_or_interaction):
        return
    petals = get_petals(target.guild.id, target.id)
    await _reply(
        ctx_or_interaction,
        f"{PETALS_EMOJI} رصيد {target.mention}: **{petals:,}** Petals",
    )


async def run_daily(ctx_or_interaction) -> None:
    if not await ensure_correct_channel(ctx_or_interaction):
        return
    author = _get_author(ctx_or_interaction)
    guild_id = author.guild.id
    last_claim = get_last_daily(guild_id, author.id)
    now = datetime.now(timezone.utc)

    if last_claim is not None:
        elapsed = now - last_claim
        remaining = timedelta(hours=DAILY_COOLDOWN_HOURS) - elapsed
        if remaining.total_seconds() > 0:
            hours, remainder = divmod(int(remaining.total_seconds()), 3600)
            minutes = remainder // 60
            await _reply(
                ctx_or_interaction,
                f"⏳ تم استلام المكافأة اليومية مسبقًا. "
                f"يرجى العودة بعد **{hours} ساعة و{minutes} دقيقة**.",
            )
            return

    amount = random.randint(DAILY_MIN_AMOUNT, DAILY_MAX_AMOUNT)
    new_balance = add_petals(guild_id, author.id, amount)
    set_last_daily(guild_id, author.id, now)

    await _reply(
        ctx_or_interaction,
        f"{PETALS_EMOJI} تم استلام **{amount:,}** من رصيد Petals كمكافأة يومية.\n"
        f"الرصيد الحالي: **{new_balance:,}** {PETALS_EMOJI}",
    )


async def run_give(
    ctx_or_interaction, target: discord.Member, amount: int
) -> None:
    if not await ensure_correct_channel(ctx_or_interaction):
        return
    author = _get_author(ctx_or_interaction)
    guild_id = author.guild.id

    if amount <= 0:
        await _reply(ctx_or_interaction, "يجب أن يكون المبلغ أكبر من صفر.")
        return
    if target.bot:
        await _reply(ctx_or_interaction, "لا يمكن تحويل العملة إلى حساب بوت.")
        return
    if target.id == author.id:
        await _reply(ctx_or_interaction, "لا يمكن تحويل العملة إلى النفس.")
        return

    sender_balance = get_petals(guild_id, author.id)
    if sender_balance < amount:
        await _reply(
            ctx_or_interaction,
            f"الرصيد الحالي **{sender_balance:,}** {PETALS_EMOJI} غير كافٍ.",
        )
        return

    add_petals(guild_id, author.id, -amount)
    new_receiver_balance = add_petals(guild_id, target.id, amount)

    await _reply(
        ctx_or_interaction,
        f"<a:white:1481419152198598830>┆تم تحويل ``{amount:,}`` Petals لـ {target.mention} !",
    )


PERIOD_LABELS = {
    "day": "اليوم",
    "week": "هذا الأسبوع",
    "month": "هذا الشهر",
    "all": "منذ الأبد",
}
LEADERBOARD_PAGE_SIZE = 5
LEADERBOARD_MAX_PAGES = 5  # يعني حتى المركز 25


def _leaderboard_lines(rows, xp_key: str, start_rank: int, unit: str) -> list[str]:
    """يبني الأسطر لأصحاب البيانات فقط، بدون صفوف فاضية لمن ما عنده شي."""
    lines = []
    for index, row in enumerate(rows, start=start_rank):
        value = row[xp_key]
        if not value:
            continue
        lines.append(f"#{index} <@{row['user_id']}> — {value:,} {unit}")
    return lines


def build_leaderboard_embed(
    guild: discord.Guild, board_type: str, period: str, page: int
) -> discord.Embed:
    period_label = PERIOD_LABELS.get(period, "") if board_type == "level" else ""
    title = f"أفضل المتفاعلين في {guild.name} {TOP_TITLE_EMOJI}"
    if period_label:
        title += f" — {period_label}"

    embed = discord.Embed(title=title, color=THEME_COLOR)
    offset = page * LEADERBOARD_PAGE_SIZE
    start_rank = offset + 1

    if board_type == "level":
        if period == "all":
            chat_rows = get_chat_leaderboard(guild.id, limit=LEADERBOARD_PAGE_SIZE, offset=offset)
            voice_rows = get_voice_leaderboard(guild.id, limit=LEADERBOARD_PAGE_SIZE, offset=offset)
        else:
            chat_rows = get_period_chat_leaderboard(
                guild.id, period, limit=LEADERBOARD_PAGE_SIZE, offset=offset
            )
            voice_rows = get_period_voice_leaderboard(
                guild.id, period, limit=LEADERBOARD_PAGE_SIZE, offset=offset
            )

        chat_lines = _leaderboard_lines(chat_rows, "chat_xp", start_rank, "XP")
        voice_lines = _leaderboard_lines(voice_rows, "voice_xp", start_rank, "XP")

        if chat_lines:
            embed.add_field(
                name=f"{TOP_TEXT_EMOJI} TOP TEXT",
                value="\n".join(chat_lines),
                inline=False,
            )
        if voice_lines:
            embed.add_field(
                name=f"{TOP_VOICE_EMOJI} TOP VOICE",
                value="\n".join(voice_lines),
                inline=False,
            )
        if not chat_lines and not voice_lines:
            embed.description = "لا يوجد أي تفاعل مسجل خلال هذه الفترة حتى الآن."
    else:
        rows = get_petals_leaderboard(guild.id, limit=LEADERBOARD_PAGE_SIZE, offset=offset)
        lines = _leaderboard_lines(rows, "petals", start_rank, str(PETALS_EMOJI))
        if lines:
            embed.add_field(
                name=f"{TOP_TITLE_EMOJI} TOP PETALS",
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.description = "لا يوجد أي رصيد من عملة Petals مسجل خلال هذه الفترة حتى الآن."

    embed.set_footer(text=f"{SERVER_WATERMARK} • صفحة {page + 1} من {LEADERBOARD_MAX_PAGES}")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


class LeaderboardView(discord.ui.View):
    """أزرار التنقل بين صفحات قائمة المتصدرين (بعد التوب 5)."""

    def __init__(self, guild_id: int, board_type: str, period: str):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.board_type = board_type
        self.period = period
        self.page = 0

    @discord.ui.button(label="◀ السابق", style=discord.ButtonStyle.secondary)
    async def previous_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.page > 0:
            self.page -= 1
        embed = build_leaderboard_embed(interaction.guild, self.board_type, self.period, self.page)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="التالي ▶", style=discord.ButtonStyle.secondary)
    async def next_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.page < LEADERBOARD_MAX_PAGES - 1:
            self.page += 1
        embed = build_leaderboard_embed(interaction.guild, self.board_type, self.period, self.page)
        await interaction.response.edit_message(embed=embed, view=self)


async def run_top(
    ctx_or_interaction, board_type: str = "petals", period: str = "all"
) -> None:
    if not await ensure_correct_channel(ctx_or_interaction):
        return
    author = _get_author(ctx_or_interaction)
    guild = author.guild

    if period not in ("day", "week", "month", "all"):
        period = "all"

    has_data = get_petals_leaderboard(guild.id, limit=1) or get_chat_leaderboard(guild.id, limit=1)
    if not has_data:
        await _reply(ctx_or_interaction, "لا تتوفر بيانات كافية لعرض القائمة حاليًا.")
        return

    embed = build_leaderboard_embed(guild, board_type, period, page=0)
    view = LeaderboardView(guild.id, board_type, period)
    await _reply(ctx_or_interaction, embed=embed, view=view)


async def run_addpetals(
    ctx_or_interaction, target: discord.Member, amount: int
) -> None:
    if not await ensure_correct_channel(ctx_or_interaction):
        return
    if amount <= 0 or amount > MAX_ADMIN_GRANT:
        await _reply(
            ctx_or_interaction,
            f"المبلغ يجب أن يكون بين 1 و{MAX_ADMIN_GRANT:,}.",
        )
        return
    new_balance = add_petals(target.guild.id, target.id, amount)
    await _reply(
        ctx_or_interaction,
        f"{PETALS_EMOJI} تمت إضافة **{amount:,}** Petals لـ {target.mention}.\n"
        f"الرصيد الجديد: **{new_balance:,}** {PETALS_EMOJI}",
    )


async def run_warnings(ctx_or_interaction, target: Optional[discord.Member]) -> None:
    author = _get_author(ctx_or_interaction)
    guild = author.guild

    if target is None:
        warnings = get_guild_warnings(guild.id, limit=15)
        if not warnings:
            await _reply(ctx_or_interaction, "لا توجد أي تحذيرات مسجلة في هذا الخادم.")
            return

        lines = []
        for row in warnings:
            member_text = f"<@{row['user_id']}>"
            lines.append(
                f"**#{row['id']}** {member_text} — {row['reason']}\n"
                f"بواسطة: <@{row['moderator_id']}> • {row['created_at']}"
            )

        embed = discord.Embed(
            title=f"⚠️ آخر تحذيرات {guild.name}",
            description="\n\n".join(lines),
            color=THEME_COLOR,
        )
        embed.set_footer(text=f"{SERVER_WATERMARK} • آخر 15 تحذير")
        await _reply(ctx_or_interaction, embed=embed)
        return

    warnings = get_member_warnings(guild.id, target.id)
    if not warnings:
        await _reply(ctx_or_interaction, f"لا توجد تحذيرات مسجلة لـ {target.mention}.")
        return

    lines = []
    for row in warnings:
        moderator = guild.get_member(row["moderator_id"])
        moderator_name = moderator.display_name if moderator else f"<@{row['moderator_id']}>"
        lines.append(
            f"**#{row['id']}** — {row['reason']}\n"
            f"بواسطة: {moderator_name} • {row['created_at']}"
        )

    embed = discord.Embed(
        title=f"⚠️ تحذيرات {target.display_name}",
        description="\n\n".join(lines),
        color=THEME_COLOR,
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.set_footer(text=SERVER_WATERMARK)
    await _reply(ctx_or_interaction, embed=embed)


async def run_removewarn(ctx_or_interaction, warning_id: int) -> None:
    author = _get_author(ctx_or_interaction)
    success = remove_warning(author.guild.id, warning_id)
    if success:
        await _reply(ctx_or_interaction, f"✅ تم حذف التحذير رقم **#{warning_id}**.")
    else:
        await _reply(ctx_or_interaction, f"لم يتم العثور على تحذير بالرقم **#{warning_id}** في هذا الخادم.")


# ---------------------------------------------------------------------------
# دوال منطقية: معلومات الخادم
# ---------------------------------------------------------------------------
async def run_server_info(ctx_or_interaction) -> None:
    author = _get_author(ctx_or_interaction)
    guild = author.guild

    embed = discord.Embed(
        title=guild.name,
        color=THEME_COLOR,
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="المالك", value=str(guild.owner) if guild.owner else "—", inline=True)
    embed.add_field(name="عدد الأعضاء", value=f"{guild.member_count:,}", inline=True)
    embed.add_field(name="عدد الأدوار", value=str(len(guild.roles)), inline=True)
    embed.add_field(
        name="القنوات النصية", value=str(len(guild.text_channels)), inline=True
    )
    embed.add_field(
        name="القنوات الصوتية", value=str(len(guild.voice_channels)), inline=True
    )
    embed.add_field(
        name="مستوى التعزيز (Boost)",
        value=f"المستوى {guild.premium_tier} ({guild.premium_subscription_count} تعزيز)",
        inline=True,
    )
    embed.add_field(
        name="تاريخ الإنشاء",
        value=discord.utils.format_dt(guild.created_at, style="D"),
        inline=False,
    )
    embed.set_footer(text=f"معرّف الخادم: {guild.id}")
    await _reply(ctx_or_interaction, embed=embed)


# ---------------------------------------------------------------------------
# دوال منطقية: السمعة (Reputation)
# ---------------------------------------------------------------------------
REPUTATION_COOLDOWN_HOURS = 24


async def run_rep(ctx_or_interaction, target: discord.Member) -> None:
    giver = _get_author(ctx_or_interaction)
    guild_id = giver.guild.id

    if target.bot:
        await _reply(ctx_or_interaction, "لا يمكن منح نقطة سمعة لحساب بوت.")
        return
    if target.id == giver.id:
        await _reply(ctx_or_interaction, "لا يمكن منح نقطة سمعة للنفس.")
        return

    last_given = get_reputation_cooldown(guild_id, giver.id)
    now = datetime.now(timezone.utc)
    if last_given is not None:
        elapsed = now - last_given
        remaining = timedelta(hours=REPUTATION_COOLDOWN_HOURS) - elapsed
        if remaining.total_seconds() > 0:
            hours, remainder = divmod(int(remaining.total_seconds()), 3600)
            minutes = remainder // 60
            await _reply(
                ctx_or_interaction,
                f"⏳ يمكنك منح نقطة سمعة أخرى بعد **{hours} ساعة و{minutes} دقيقة**.",
            )
            return

    new_total = add_reputation(guild_id, target.id, 1)
    set_reputation_cooldown(guild_id, giver.id, now)
    await _reply(
        ctx_or_interaction,
        f"⭐ حصل {target.mention} على نقطة سمعة من {giver.mention}!\n"
        f"رصيد السمعة الحالي: **{new_total:,}**",
    )


# ---------------------------------------------------------------------------
# دوال منطقية: الأدوار (Roles)
# ---------------------------------------------------------------------------
async def run_role_give(ctx_or_interaction, target: discord.Member, role: discord.Role) -> None:
    try:
        await target.add_roles(role, reason="أمر role give")
    except discord.Forbidden:
        await _reply(ctx_or_interaction, "لا تملك صلاحية كافية لإضافة هذا الدور.")
        return
    await _reply(ctx_or_interaction, f"✅ تمت إضافة الدور {role.mention} إلى {target.mention}.")


async def run_role_remove(ctx_or_interaction, target: discord.Member, role: discord.Role) -> None:
    try:
        await target.remove_roles(role, reason="أمر role remove")
    except discord.Forbidden:
        await _reply(ctx_or_interaction, "لا تملك صلاحية كافية لحذف هذا الدور.")
        return
    await _reply(ctx_or_interaction, f"✅ تم حذف الدور {role.mention} من {target.mention}.")


def _extract_member_ids(text: str) -> list[int]:
    return [int(match) for match in re.findall(r"\d{15,25}", text)]


async def run_role_multiple(
    ctx_or_interaction, action: str, role: discord.Role, members_text: str
) -> None:
    author = _get_author(ctx_or_interaction)
    guild = author.guild
    member_ids = _extract_member_ids(members_text)

    if not member_ids:
        await _reply(
            ctx_or_interaction,
            "يرجى منشنة الأعضاء المطلوبين ضمن الأمر، مثال: `@عضو1 @عضو2 @عضو3`",
        )
        return

    succeeded = []
    failed = []
    for member_id in member_ids:
        member = guild.get_member(member_id)
        if member is None:
            failed.append(f"<@{member_id}>")
            continue
        try:
            if action == "give":
                await member.add_roles(role, reason="أمر role multiple")
            else:
                await member.remove_roles(role, reason="أمر role multiple")
            succeeded.append(member.mention)
        except discord.Forbidden:
            failed.append(member.mention)

    action_label = "إضافة" if action == "give" else "حذف"
    lines = [f"✅ تمت عملية {action_label} الدور {role.mention} للأعضاء التالين:"]
    if succeeded:
        lines.append("، ".join(succeeded))
    if failed:
        lines.append(f"\n⚠️ تعذّر تنفيذ العملية على: {'، '.join(failed)}")
    await _reply(ctx_or_interaction, "\n".join(lines))


async def run_roles_list(ctx_or_interaction) -> None:
    author = _get_author(ctx_or_interaction)
    guild = author.guild

    roles = [role for role in guild.roles if role.name != "@everyone"]
    roles.sort(key=lambda role: role.position, reverse=True)

    lines = [f"{role.mention} — {len(role.members):,} عضو" for role in roles]
    description = "\n".join(lines) if lines else "لا توجد أدوار في هذا الخادم."

    embed = discord.Embed(
        title=f"أدوار {guild.name}",
        description=description[:4000],
        color=THEME_COLOR,
    )
    embed.set_footer(text=f"إجمالي الأدوار: {len(roles)}")
    await _reply(ctx_or_interaction, embed=embed)


# ---------------------------------------------------------------------------
# دوال منطقية: نظام النقاط (Points)
# ---------------------------------------------------------------------------
async def run_points_increase(ctx_or_interaction, target: discord.Member, amount: int) -> None:
    if amount <= 0:
        await _reply(ctx_or_interaction, "يجب أن يكون المقدار أكبر من صفر.")
        return
    new_total = add_points(target.guild.id, target.id, amount)
    await _reply(
        ctx_or_interaction,
        f"✅ تمت إضافة **{amount:,}** نقطة لـ {target.mention}.\nالرصيد الحالي: **{new_total:,}**",
    )


async def run_points_decrease(ctx_or_interaction, target: discord.Member, amount: int) -> None:
    if amount <= 0:
        await _reply(ctx_or_interaction, "يجب أن يكون المقدار أكبر من صفر.")
        return
    new_total = add_points(target.guild.id, target.id, -amount)
    await _reply(
        ctx_or_interaction,
        f"✅ تم خصم **{amount:,}** نقطة من {target.mention}.\nالرصيد الحالي: **{new_total:,}**",
    )


async def run_points_set(ctx_or_interaction, target: discord.Member, amount: int) -> None:
    if amount < 0:
        await _reply(ctx_or_interaction, "لا يمكن أن يكون المقدار أقل من صفر.")
        return
    set_points(target.guild.id, target.id, amount)
    await _reply(
        ctx_or_interaction,
        f"✅ تم تحديد رصيد {target.mention} بـ **{amount:,}** نقطة.",
    )


async def run_points_list(ctx_or_interaction) -> None:
    author = _get_author(ctx_or_interaction)
    guild = author.guild
    rows = get_points_list(guild.id)

    if not rows:
        await _reply(ctx_or_interaction, "لا توجد نقاط مسجلة لأي عضو حاليًا.")
        return

    lines = [f"<@{row['user_id']}> — **{row['points']:,}** نقطة" for row in rows[:25]]
    embed = discord.Embed(
        title=f"نقاط أعضاء {guild.name}",
        description="\n".join(lines),
        color=THEME_COLOR,
    )
    await _reply(ctx_or_interaction, embed=embed)


async def run_points_reset(ctx_or_interaction, target: Optional[discord.Member]) -> None:
    author = _get_author(ctx_or_interaction)
    reset_points(author.guild.id, target.id if target else None)
    if target:
        await _reply(ctx_or_interaction, f"✅ تم تصفير نقاط {target.mention}.")
    else:
        await _reply(ctx_or_interaction, "✅ تم تصفير نقاط جميع الأعضاء.")


# ---------------------------------------------------------------------------
# دوال منطقية: تصفير XP
# ---------------------------------------------------------------------------
async def run_reset_xp(
    ctx_or_interaction, reset_type: str, target: Optional[discord.Member]
) -> None:
    author = _get_author(ctx_or_interaction)
    guild_id = author.guild.id

    if target is not None:
        chat_xp, voice_xp = get_member_xp_breakdown(guild_id, target.id)
        new_chat = 0 if reset_type in ("text", "all") else chat_xp
        new_voice = 0 if reset_type in ("voice", "all") else voice_xp
        database.execute(
            """
            UPDATE member_levels
            SET chat_xp = ?, voice_xp = ?, xp = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (new_chat, new_voice, new_chat + new_voice, guild_id, target.id),
        )
        database.commit()
        await _reply(ctx_or_interaction, f"✅ تم تصفير نقاط الخبرة الخاصة بـ {target.mention}.")
        return

    if reset_type == "text":
        database.execute(
            "UPDATE member_levels SET chat_xp = 0, xp = voice_xp WHERE guild_id = ?",
            (guild_id,),
        )
    elif reset_type == "voice":
        database.execute(
            "UPDATE member_levels SET voice_xp = 0, xp = chat_xp WHERE guild_id = ?",
            (guild_id,),
        )
    else:
        database.execute(
            "UPDATE member_levels SET chat_xp = 0, voice_xp = 0, xp = 0 WHERE guild_id = ?",
            (guild_id,),
        )
    database.commit()
    await _reply(ctx_or_interaction, "✅ تم تصفير نقاط الخبرة لجميع أعضاء الخادم.")


# ---------------------------------------------------------------------------
# دوال منطقية: الرومات الصوتية
# ---------------------------------------------------------------------------
async def run_move_all(ctx_or_interaction, source_channel: Optional[discord.VoiceChannel]) -> None:
    author = _get_author(ctx_or_interaction)
    if author.voice is None or author.voice.channel is None:
        await _reply(ctx_or_interaction, "يجب أن تكون متصلًا برومٍ صوتي أولًا.")
        return

    destination = author.voice.channel
    members_to_move = (
        list(source_channel.members)
        if source_channel is not None
        else [
            member
            for channel in author.guild.voice_channels
            if channel != destination
            for member in channel.members
        ]
    )

    moved = 0
    for member in members_to_move:
        try:
            await member.move_to(destination, reason="أمر move all")
            moved += 1
        except (discord.Forbidden, discord.HTTPException):
            continue

    await _reply(ctx_or_interaction, f"✅ تم نقل **{moved}** عضوًا إلى {destination.mention}.")


async def run_move_user(
    ctx_or_interaction, target: discord.Member, channel: discord.VoiceChannel
) -> None:
    if target.voice is None or target.voice.channel is None:
        await _reply(ctx_or_interaction, f"{target.mention} غير متصل بأي روم صوتي حاليًا.")
        return
    try:
        await target.move_to(channel, reason="أمر move user")
    except (discord.Forbidden, discord.HTTPException):
        await _reply(ctx_or_interaction, "تعذّر نقل العضو، يرجى التحقق من الصلاحيات.")
        return
    await _reply(ctx_or_interaction, f"✅ تم نقل {target.mention} إلى {channel.mention}.")


async def run_moveme(ctx_or_interaction, channel: discord.VoiceChannel) -> None:
    author = _get_author(ctx_or_interaction)
    if author.voice is None or author.voice.channel is None:
        await _reply(ctx_or_interaction, "يجب أن تكون متصلًا برومٍ صوتي أولًا.")
        return
    try:
        await author.move_to(channel, reason="أمر moveme")
    except (discord.Forbidden, discord.HTTPException):
        await _reply(ctx_or_interaction, "تعذّر نقلك، يرجى التحقق من الصلاحيات.")
        return
    await _reply(ctx_or_interaction, f"✅ تم نقلك إلى {channel.mention}.")


async def run_mute_voice(ctx_or_interaction, target: discord.Member) -> None:
    try:
        new_state = not target.voice.mute if target.voice else True
        await target.edit(mute=new_state, reason="أمر mute voice")
    except (discord.Forbidden, discord.HTTPException):
        await _reply(ctx_or_interaction, "تعذّر تنفيذ الأمر، يرجى التحقق من الصلاحيات.")
        return
    status_text = "تم كتم" if new_state else "تم رفع الكتم الصوتي عن"
    await _reply(ctx_or_interaction, f"✅ {status_text} {target.mention}.")


async def run_mute_text(ctx_or_interaction, target: discord.Member) -> None:
    guild = target.guild
    role = await get_or_create_mute_role(guild)

    if role in target.roles:
        try:
            await target.remove_roles(role, reason="أمر mute text - رفع الكتم")
        except discord.Forbidden:
            await _reply(ctx_or_interaction, "لا تملك صلاحية كافية لتنفيذ هذا الأمر.")
            return
        await _reply(ctx_or_interaction, f"✅ تم رفع الكتم النصي عن {target.mention}.")
    else:
        try:
            await target.add_roles(role, reason="أمر mute text")
        except discord.Forbidden:
            await _reply(ctx_or_interaction, "لا تملك صلاحية كافية لتنفيذ هذا الأمر.")
            return
        await _reply(ctx_or_interaction, f"✅ تم كتم {target.mention} نصيًا.")


# ---------------------------------------------------------------------------
# أدوات مساعدة للرد سواء كان المصدر Context (أمر !) أو Interaction (أمر /)
# ---------------------------------------------------------------------------
def _get_author(ctx_or_interaction) -> discord.Member:
    if isinstance(ctx_or_interaction, discord.Interaction):
        return ctx_or_interaction.user
    return ctx_or_interaction.author


def _get_channel_id(ctx_or_interaction) -> int:
    if isinstance(ctx_or_interaction, discord.Interaction):
        return ctx_or_interaction.channel_id
    return ctx_or_interaction.channel.id


async def ensure_correct_channel(ctx_or_interaction) -> bool:
    """يتأكد إن الأمر يُستخدم بأحد الرومات المسموحة (لو تم تحديد رومات أصلاً).
    يعيد True لو مسموح بالتنفيذ، و False لو تم رفضه (مع إرسال رسالة توضيحية)."""
    author = _get_author(ctx_or_interaction)
    guild_id = author.guild.id
    allowed_channels = get_commands_channels(guild_id)

    if not allowed_channels:
        return True

    if _get_channel_id(ctx_or_interaction) in allowed_channels:
        return True

    channels_text = "، ".join(f"<#{cid}>" for cid in allowed_channels)
    await _reply(
        ctx_or_interaction,
        f"⚠️ لا يمكن استخدام هذا الأمر إلا في القنوات التالية: {channels_text}",
    )
    return False


async def _reply(ctx_or_interaction, content: str = None, **kwargs) -> None:
    if isinstance(ctx_or_interaction, discord.Interaction):
        interaction = ctx_or_interaction
        if interaction.response.is_done():
            await interaction.followup.send(content, **kwargs)
        else:
            await interaction.response.send_message(content, **kwargs)
    else:
        await ctx_or_interaction.send(content, **kwargs)


# ---------------------------------------------------------------------------
# أوامر Slash (/)
# ---------------------------------------------------------------------------
@bot.tree.command(name="ping", description="يرد برسالة Pong")
async def ping_slash(interaction: discord.Interaction) -> None:
    await run_ping(interaction)


@bot.tree.command(name="clear", description="مسح عدد من الرسائل")
@app_commands.describe(amount="عدد الرسائل من 1 إلى 100")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
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
    await run_avatar(interaction, member or interaction.user)


@bot.tree.command(name="user", description="عرض معلومات الحساب والعضوية")
@app_commands.describe(member="العضو المطلوب عرض معلوماته")
@app_commands.guild_only()
async def userinfo_slash(
    interaction: discord.Interaction, member: Optional[discord.Member] = None
) -> None:
    await run_userinfo(interaction, member or interaction.user)


@bot.tree.command(name="kick", description="طرد عضو من الخادم")
@app_commands.describe(member="العضو المطلوب طرده", reason="سبب الطرد")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
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


@bot.tree.command(name="ban", description="حظر عضو من الخادم")
@app_commands.describe(member="العضو المطلوب حظره", reason="سبب الحظر")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
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
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
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
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
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
            f"تم تحذيرك في الخادم **{interaction.guild.name}**. السبب: {reason}"
        )
    except discord.Forbidden:
        pass
    await interaction.response.send_message(
        f"تم تحذير {member.mention}. رقم التحذير: **#{warning_id}**. السبب: {reason}"
    )


@bot.tree.command(name="warnings", description="عرض تحذيرات عضو معين أو جميع تحذيرات الخادم")
@app_commands.describe(member="اتركه فارغًا لعرض جميع تحذيرات الخادم")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
@app_commands.checks.has_permissions(manage_messages=True)
async def warnings_slash(
    interaction: discord.Interaction, member: Optional[discord.Member] = None
) -> None:
    await run_warnings(interaction, member)


remove_group = app_commands.Group(name="remove", description="أوامر الحذف")
add_group = app_commands.Group(name="add", description="أوامر الإضافة")


@remove_group.command(name="warn", description="حذف تحذير معين برقمه")
@app_commands.describe(رقم_التحذير="رقم التحذير المطلوب حذفه")
@app_commands.checks.has_permissions(administrator=True)
async def removewarn_slash(
    interaction: discord.Interaction, رقم_التحذير: int
) -> None:
    await run_removewarn(interaction, رقم_التحذير)


@add_group.command(name="xp", description="إضافة XP للدردشة النصية والصوت للاختبار")
@app_commands.describe(
    amount="مقدار XP من 1 إلى 100000",
    member="العضو المستهدف، أو اتركه فارغًا لنفسك",
)
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
        f"تمت إضافة **{amount:,} XP** إلى الدردشة النصية والصوت للعضو {target.mention}."
    )
    if level_from_xp(new_xp) > level_from_xp(old_xp):
        await send_level_up_message(target, level_from_xp(new_xp), "both")


@bot.tree.command(name="rank", description="عرض بطاقة المستوى (رانك)")
@app_commands.describe(member="العضو المطلوب عرض رتبته")
@app_commands.guild_only()
async def rank_slash(
    interaction: discord.Interaction, member: Optional[discord.Member] = None
) -> None:
    await run_rank(interaction, member or interaction.user)


@bot.tree.command(name="profile", description="عرض بروفايلك الكامل")
@app_commands.describe(member="العضو المطلوب عرض بروفايله")
@app_commands.guild_only()
async def profile_slash(
    interaction: discord.Interaction, member: Optional[discord.Member] = None
) -> None:
    await run_profile(interaction, member or interaction.user)


@bot.tree.command(name="daily", description="استلام مكافأة Petals اليومية")
@app_commands.guild_only()
async def daily_slash(interaction: discord.Interaction) -> None:
    await run_daily(interaction)


@bot.tree.command(name="balance", description="عرض رصيدك من Petals")
@app_commands.describe(member="العضو المطلوب عرض رصيده")
@app_commands.guild_only()
async def balance_slash(
    interaction: discord.Interaction, member: Optional[discord.Member] = None
) -> None:
    await run_balance(interaction, member or interaction.user)


@bot.tree.command(name="give", description="تحويل Petals لعضو ثاني")
@app_commands.describe(member="العضو المستلم", amount="عدد الـ Petals")
@app_commands.guild_only()
async def give_slash(
    interaction: discord.Interaction, member: discord.Member, amount: int
) -> None:
    await run_give(interaction, member, amount)


@bot.tree.command(name="top", description="عرض قائمة المتصدرين")
@app_commands.describe(نوع="اختاري بتلز أو مستوى", الفترة="اختاري الفترة الزمنية (للمستوى فقط)")
@app_commands.choices(
    نوع=[
        app_commands.Choice(name="Petals", value="petals"),
        app_commands.Choice(name="المستوى", value="level"),
    ],
    الفترة=[
        app_commands.Choice(name="اليوم", value="day"),
        app_commands.Choice(name="هذا الأسبوع", value="week"),
        app_commands.Choice(name="هذا الشهر", value="month"),
        app_commands.Choice(name="منذ الأبد", value="all"),
    ],
)
@app_commands.guild_only()
async def top_slash(
    interaction: discord.Interaction,
    نوع: app_commands.Choice[str] = None,
    الفترة: app_commands.Choice[str] = None,
) -> None:
    board_type = نوع.value if نوع else "petals"
    period = الفترة.value if الفترة else "all"
    await run_top(interaction, board_type, period)


set_group = app_commands.Group(name="set", description="أوامر الإعداد")
set_commands_subgroup = app_commands.Group(
    name="commands", description="إعداد قنوات الأوامر", parent=set_group
)
set_level_subgroup = app_commands.Group(
    name="level", description="إعداد إشعارات رفع المستوى", parent=set_group
)
set_join_subgroup = app_commands.Group(
    name="join", description="إعداد رتبة الدخول التلقائية", parent=set_group
)
remove_commands_subgroup = app_commands.Group(
    name="commands", description="إعداد قنوات الأوامر", parent=remove_group
)
levelrole_group = app_commands.Group(
    name="levelrole", description="[إدارة فقط] أوامر رتب المستويات التلقائية"
)


@set_level_subgroup.command(
    name="channel", description="[إدارة فقط] تحديد روم إشعارات رفع المستوى"
)
@app_commands.describe(channel="الروم المطلوب تحديده")
@app_commands.checks.has_permissions(administrator=True)
async def set_level_channel_slash(
    interaction: discord.Interaction, channel: discord.TextChannel
) -> None:
    set_level_channel_id(interaction.guild.id, channel.id)
    await interaction.response.send_message(
        f"✅ تم تحديد {channel.mention} كروم لإشعارات رفع المستوى."
    )


@set_join_subgroup.command(
    name="role", description="[إدارة فقط] تحديد رتبة تُعطى تلقائيًا للأعضاء الجدد"
)
@app_commands.describe(role="الرتبة المطلوب تفعيلها، أو اتركها فارغة لإلغاء الرتبة التلقائية")
@app_commands.checks.has_permissions(administrator=True)
async def set_join_role_slash(
    interaction: discord.Interaction, role: Optional[discord.Role] = None
) -> None:
    set_join_role_id(interaction.guild.id, role.id if role else None)
    if role:
        await interaction.response.send_message(f"✅ تم تحديد {role.mention} كرتبة دخول تلقائية.")
    else:
        await interaction.response.send_message("✅ تم إلغاء رتبة الدخول التلقائية.")


@levelrole_group.command(name="add", description="[إدارة فقط] إضافة رتبة تُعطى عند الوصول لمستوى معين")
@app_commands.describe(المستوى="المستوى المطلوب", role="الرتبة المستحقة")
@app_commands.checks.has_permissions(administrator=True)
async def levelrole_add_slash(
    interaction: discord.Interaction, المستوى: int, role: discord.Role
) -> None:
    set_level_role_reward(interaction.guild.id, المستوى, role.id)
    await interaction.response.send_message(
        f"✅ عند الوصول للمستوى **{المستوى}**، بيحصل العضو تلقائيًا على {role.mention}."
    )


@levelrole_group.command(name="remove", description="[إدارة فقط] حذف رتبة مستوى معين")
@app_commands.describe(المستوى="المستوى المطلوب حذف مكافأته")
@app_commands.checks.has_permissions(administrator=True)
async def levelrole_remove_slash(interaction: discord.Interaction, المستوى: int) -> None:
    success = remove_level_role_reward(interaction.guild.id, المستوى)
    if success:
        await interaction.response.send_message(f"✅ تم حذف مكافأة المستوى **{المستوى}**.")
    else:
        await interaction.response.send_message(f"لا توجد مكافأة مسجلة للمستوى **{المستوى}**.")


@levelrole_group.command(name="list", description="[إدارة فقط] عرض كل رتب المستويات المفعّلة")
async def levelrole_list_slash(interaction: discord.Interaction) -> None:
    rewards = get_level_role_rewards(interaction.guild.id)
    if not rewards:
        await interaction.response.send_message("ما فيه أي رتب مستويات مفعّلة حاليًا.")
        return
    lines = [f"المستوى **{row['level']}** ← <@&{row['role_id']}>" for row in rewards]
    await interaction.response.send_message("\n".join(lines))


@set_commands_subgroup.command(
    name="channel", description="[إدارة فقط] إضافة قناة لقائمة قنوات الأوامر المسموحة"
)
@app_commands.describe(channel="القناة المطلوب إضافتها")
@app_commands.checks.has_permissions(administrator=True)
async def setcommandschannel_slash(
    interaction: discord.Interaction, channel: discord.TextChannel
) -> None:
    add_commands_channel(interaction.guild.id, channel.id)
    allowed_channels = get_commands_channels(interaction.guild.id)
    channels_text = "، ".join(f"<#{cid}>" for cid in allowed_channels)
    await interaction.response.send_message(
        f"✅ تمت إضافة القناة {channel.mention} إلى قائمة القنوات المسموح فيها بتنفيذ الأوامر.\n"
        f"القنوات المسموحة حاليًا: {channels_text}"
    )


@remove_commands_subgroup.command(
    name="channel", description="[إدارة فقط] حذف قناة من قائمة قنوات الأوامر المسموحة"
)
@app_commands.describe(channel="القناة المطلوب حذفها من القائمة")
@app_commands.checks.has_permissions(administrator=True)
async def removecommandschannel_slash(
    interaction: discord.Interaction, channel: discord.TextChannel
) -> None:
    success = remove_commands_channel(interaction.guild.id, channel.id)
    if success:
        await interaction.response.send_message(f"✅ تم حذف القناة {channel.mention} من قائمة القنوات المسموح فيها بتنفيذ الأوامر.")
    else:
        await interaction.response.send_message(f"القناة {channel.mention} غير موجودة أساسًا ضمن القائمة.")


@add_group.command(name="petals", description="[إدارة فقط] إضافة Petals لعضو")
@app_commands.describe(
    amount="عدد الـ Petals المضافة",
    member="العضو المستهدف، أو اتركه فارغًا لنفسك",
)
@app_commands.checks.has_permissions(administrator=True)
async def addpetals_slash(
    interaction: discord.Interaction,
    amount: int,
    member: Optional[discord.Member] = None,
) -> None:
    await run_addpetals(interaction, member or interaction.user, amount)


@bot.tree.command(name="server", description="عرض معلومات عن الخادم")
@app_commands.guild_only()
async def server_slash(interaction: discord.Interaction) -> None:
    await run_server_info(interaction)


@bot.tree.command(name="rep", description="منح نقطة سمعة لعضو")
@app_commands.describe(member="العضو المطلوب منحه نقطة سمعة")
@app_commands.guild_only()
async def rep_slash(interaction: discord.Interaction, member: discord.Member) -> None:
    await run_rep(interaction, member)


role_group = app_commands.Group(name="role", description="أوامر إدارة الأدوار")


@role_group.command(name="give", description="إضافة دور لعضو")
@app_commands.describe(member="العضو المستهدف", role="الدور المطلوب إضافته")
@app_commands.checks.has_permissions(manage_roles=True)
async def role_give_slash(
    interaction: discord.Interaction, member: discord.Member, role: discord.Role
) -> None:
    await run_role_give(interaction, member, role)


@role_group.command(name="remove", description="حذف دور من عضو")
@app_commands.describe(member="العضو المستهدف", role="الدور المطلوب حذفه")
@app_commands.checks.has_permissions(manage_roles=True)
async def role_remove_slash(
    interaction: discord.Interaction, member: discord.Member, role: discord.Role
) -> None:
    await run_role_remove(interaction, member, role)


@role_group.command(name="multiple", description="[إدارة فقط] إضافة/حذف دور لعدة أعضاء دفعة واحدة")
@app_commands.describe(
    الإجراء="إضافة أو حذف",
    role="الدور المطلوب",
    الأعضاء="منشنة الأعضاء المطلوبين (مفصولين بمسافة)",
)
@app_commands.choices(
    الإجراء=[
        app_commands.Choice(name="إضافة", value="give"),
        app_commands.Choice(name="حذف", value="remove"),
    ]
)
@app_commands.checks.has_permissions(administrator=True)
async def role_multiple_slash(
    interaction: discord.Interaction,
    الإجراء: app_commands.Choice[str],
    role: discord.Role,
    الأعضاء: str,
) -> None:
    await run_role_multiple(interaction, الإجراء.value, role, الأعضاء)


@bot.tree.command(name="roles", description="عرض قائمة أدوار الخادم وعدد الأعضاء بكل دور")
@app_commands.guild_only()
async def roles_slash(interaction: discord.Interaction) -> None:
    await run_roles_list(interaction)


points_group = app_commands.Group(name="points", description="أوامر نظام النقاط")


@points_group.command(name="increase", description="[إدارة الفعاليات فقط] زيادة نقاط عضو")
@app_commands.describe(member="العضو المستهدف", amount="عدد النقاط")
@app_commands.checks.has_permissions(manage_events=True)
async def points_increase_slash(
    interaction: discord.Interaction, member: discord.Member, amount: int
) -> None:
    await run_points_increase(interaction, member, amount)


@points_group.command(name="decrease", description="[إدارة الفعاليات فقط] إنقاص نقاط عضو")
@app_commands.describe(member="العضو المستهدف", amount="عدد النقاط")
@app_commands.checks.has_permissions(manage_events=True)
async def points_decrease_slash(
    interaction: discord.Interaction, member: discord.Member, amount: int
) -> None:
    await run_points_decrease(interaction, member, amount)


@points_group.command(name="set", description="[إدارة الفعاليات فقط] تحديد نقاط عضو بقيمة معينة")
@app_commands.describe(member="العضو المستهدف", amount="القيمة الجديدة")
@app_commands.checks.has_permissions(manage_events=True)
async def points_set_slash(
    interaction: discord.Interaction, member: discord.Member, amount: int
) -> None:
    await run_points_set(interaction, member, amount)


@points_group.command(name="list", description="[إدارة الفعاليات فقط] عرض نقاط جميع الأعضاء")
@app_commands.checks.has_permissions(manage_events=True)
async def points_list_slash(interaction: discord.Interaction) -> None:
    await run_points_list(interaction)


@points_group.command(name="reset", description="[إدارة الفعاليات فقط] تصفير نقاط عضو أو الجميع")
@app_commands.describe(member="اتركه فارغًا لتصفير نقاط جميع الأعضاء")
@app_commands.checks.has_permissions(manage_events=True)
async def points_reset_slash(
    interaction: discord.Interaction, member: Optional[discord.Member] = None
) -> None:
    await run_points_reset(interaction, member)


@bot.tree.command(name="reset", description="[إدارة فقط] تصفير نقاط الخبرة (كتابي/صوتي/الكل)")
@app_commands.describe(
    النوع="نوع النقاط المطلوب تصفيرها",
    member="اتركه فارغًا لتصفير نقاط جميع الأعضاء",
)
@app_commands.choices(
    النوع=[
        app_commands.Choice(name="كتابي", value="text"),
        app_commands.Choice(name="صوتي", value="voice"),
        app_commands.Choice(name="الكل", value="all"),
    ]
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def reset_slash(
    interaction: discord.Interaction,
    النوع: app_commands.Choice[str],
    member: Optional[discord.Member] = None,
) -> None:
    await run_reset_xp(interaction, النوع.value, member)


move_group = app_commands.Group(name="move", description="أوامر نقل الأعضاء بين الرومات الصوتية")


@move_group.command(name="all", description="نقل كل الأعضاء إلى الروم الصوتي الحالي لك")
@app_commands.describe(المصدر="اختياري: انقل من روم معين فقط")
@app_commands.checks.has_permissions(move_members=True)
async def move_all_slash(
    interaction: discord.Interaction, المصدر: Optional[discord.VoiceChannel] = None
) -> None:
    await run_move_all(interaction, المصدر)


@move_group.command(name="user", description="نقل عضو لروم صوتي آخر")
@app_commands.describe(member="العضو المطلوب نقله", channel="الروم الصوتي الهدف")
@app_commands.checks.has_permissions(move_members=True)
async def move_user_slash(
    interaction: discord.Interaction, member: discord.Member, channel: discord.VoiceChannel
) -> None:
    await run_move_user(interaction, member, channel)


@bot.tree.command(name="moveme", description="نقل نفسك لروم صوتي آخر")
@app_commands.describe(channel="الروم الصوتي الهدف")
@app_commands.guild_only()
@app_commands.checks.has_permissions(move_members=True)
async def moveme_slash(
    interaction: discord.Interaction, channel: discord.VoiceChannel
) -> None:
    await run_moveme(interaction, channel)


mute_group = app_commands.Group(name="mute", description="أوامر الكتم")


@mute_group.command(name="voice", description="كتم/رفع كتم عضو صوتيًا")
@app_commands.describe(member="العضو المستهدف")
@app_commands.checks.has_permissions(mute_members=True)
async def mute_voice_slash(interaction: discord.Interaction, member: discord.Member) -> None:
    await run_mute_voice(interaction, member)


@mute_group.command(name="text", description="كتم/رفع كتم عضو نصيًا")
@app_commands.describe(member="العضو المستهدف")
@app_commands.checks.has_permissions(moderate_members=True)
async def mute_text_slash(interaction: discord.Interaction, member: discord.Member) -> None:
    await run_mute_text(interaction, member)


bot.tree.add_command(role_group)
bot.tree.add_command(points_group)
bot.tree.add_command(move_group)
bot.tree.add_command(mute_group)
bot.tree.add_command(set_group)
bot.tree.add_command(remove_group)
bot.tree.add_command(add_group)
bot.tree.add_command(levelrole_group)


# ---------------------------------------------------------------------------
# نفس الأوامر بصيغة ! (Prefix Commands)
# ---------------------------------------------------------------------------
@bot.command(name="ping")
async def ping_prefix(ctx: commands.Context) -> None:
    await run_ping(ctx)


@bot.command(name="avatar")
@commands.guild_only()
async def avatar_prefix(
    ctx: commands.Context, member: Optional[discord.Member] = None
) -> None:
    await run_avatar(ctx, member or ctx.author)


@bot.command(name="user")
@commands.guild_only()
async def userinfo_prefix(
    ctx: commands.Context, member: Optional[discord.Member] = None
) -> None:
    await run_userinfo(ctx, member or ctx.author)


@bot.command(name="clear")
@commands.guild_only()
@commands.has_permissions(administrator=True)
@commands.bot_has_permissions(manage_messages=True, read_message_history=True)
async def clear_prefix(ctx: commands.Context, amount: int) -> None:
    if amount < 1 or amount > 100:
        await ctx.send("عدد الرسائل يجب أن يكون بين 1 و100.")
        return
    deleted_messages = await ctx.channel.purge(limit=amount + 1)
    confirmation = await ctx.send(f"تم مسح **{len(deleted_messages) - 1}** رسالة.")
    await asyncio.sleep(3)
    await confirmation.delete()


@bot.command(name="kick")
@commands.guild_only()
@commands.has_permissions(administrator=True)
@commands.bot_has_permissions(kick_members=True)
async def kick_prefix(
    ctx: commands.Context, member: discord.Member, *, reason: str = "بدون سبب"
) -> None:
    failure = moderation_target_error(ctx.guild, ctx.author, member)
    if failure:
        await ctx.send(failure)
        return
    await member.kick(reason=f"{ctx.author}: {reason}")
    await ctx.send(f"تم طرد {member.mention}. السبب: {reason}")


@bot.command(name="ban")
@commands.guild_only()
@commands.has_permissions(administrator=True)
@commands.bot_has_permissions(ban_members=True)
async def ban_prefix(
    ctx: commands.Context, member: discord.Member, *, reason: str = "بدون سبب"
) -> None:
    failure = moderation_target_error(ctx.guild, ctx.author, member)
    if failure:
        await ctx.send(failure)
        return
    await member.ban(reason=f"{ctx.author}: {reason}")
    await ctx.send(f"تم تبنيد {member.mention}. السبب: {reason}")


@bot.command(name="timeout")
@commands.guild_only()
@commands.has_permissions(administrator=True)
@commands.bot_has_permissions(moderate_members=True)
async def timeout_prefix(
    ctx: commands.Context,
    member: discord.Member,
    duration: TimeoutDuration,
    *,
    reason: str = "بدون سبب",
) -> None:
    failure = moderation_target_error(ctx.guild, ctx.author, member)
    if failure:
        await ctx.send(failure)
        return
    await member.timeout(duration, reason=f"{ctx.author}: {reason}")
    await ctx.send(f"تم وضع {member.mention} في تايم أوت. السبب: {reason}")


@bot.command(name="warn")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def warn_prefix(
    ctx: commands.Context, member: discord.Member, *, reason: str = "بدون سبب"
) -> None:
    failure = moderation_target_error(ctx.guild, ctx.author, member)
    if failure:
        await ctx.send(failure)
        return
    warning_id = add_warning(ctx.guild.id, member.id, ctx.author.id, reason)
    try:
        await member.send(f"تم تحذيرك في الخادم **{ctx.guild.name}**. السبب: {reason}")
    except discord.Forbidden:
        pass
    await ctx.send(f"تم تحذير {member.mention}. رقم التحذير: **#{warning_id}**. السبب: {reason}")


@bot.command(name="warnings", aliases=["warns"])
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def warnings_prefix(
    ctx: commands.Context, member: Optional[discord.Member] = None
) -> None:
    await run_warnings(ctx, member)


@bot.command(name="server")
@commands.guild_only()
async def server_prefix(ctx: commands.Context) -> None:
    await run_server_info(ctx)


@bot.command(name="rep")
@commands.guild_only()
async def rep_prefix(ctx: commands.Context, member: discord.Member) -> None:
    await run_rep(ctx, member)


@bot.group(name="role", invoke_without_command=True)
@commands.guild_only()
async def role_group_prefix(ctx: commands.Context) -> None:
    await ctx.send(
        "طريقة الاستخدام: `!role give @عضو @رول` أو `!role remove @عضو @رول` "
        "أو `!role multiple give/remove @رول @عضو1 @عضو2 ...`"
    )


@role_group_prefix.command(name="give")
@commands.has_permissions(manage_roles=True)
async def role_give_prefix(
    ctx: commands.Context, member: discord.Member, role: discord.Role
) -> None:
    await run_role_give(ctx, member, role)


@role_group_prefix.command(name="remove")
@commands.has_permissions(manage_roles=True)
async def role_remove_prefix(
    ctx: commands.Context, member: discord.Member, role: discord.Role
) -> None:
    await run_role_remove(ctx, member, role)


@role_group_prefix.command(name="multiple")
@commands.has_permissions(administrator=True)
async def role_multiple_prefix(
    ctx: commands.Context, action: str, role: discord.Role, *, members_text: str
) -> None:
    action = "give" if action.lower() in ("give", "اضافة", "إضافة") else "remove"
    await run_role_multiple(ctx, action, role, members_text)


@bot.command(name="roles")
@commands.guild_only()
async def roles_prefix(ctx: commands.Context) -> None:
    await run_roles_list(ctx)


@bot.group(name="points", invoke_without_command=True)
@commands.guild_only()
async def points_group_prefix(ctx: commands.Context) -> None:
    await ctx.send(
        "طريقة الاستخدام: `!points increase/decrease/set @عضو المقدار` أو "
        "`!points list` أو `!points reset [@عضو]`"
    )


@points_group_prefix.command(name="increase")
@commands.has_permissions(manage_events=True)
async def points_increase_prefix(
    ctx: commands.Context, member: discord.Member, amount: int
) -> None:
    await run_points_increase(ctx, member, amount)


@points_group_prefix.command(name="decrease")
@commands.has_permissions(manage_events=True)
async def points_decrease_prefix(
    ctx: commands.Context, member: discord.Member, amount: int
) -> None:
    await run_points_decrease(ctx, member, amount)


@points_group_prefix.command(name="set")
@commands.has_permissions(manage_events=True)
async def points_set_prefix(
    ctx: commands.Context, member: discord.Member, amount: int
) -> None:
    await run_points_set(ctx, member, amount)


@points_group_prefix.command(name="list")
@commands.has_permissions(manage_events=True)
async def points_list_prefix(ctx: commands.Context) -> None:
    await run_points_list(ctx)


@points_group_prefix.command(name="reset")
@commands.has_permissions(manage_events=True)
async def points_reset_prefix(
    ctx: commands.Context, member: Optional[discord.Member] = None
) -> None:
    await run_points_reset(ctx, member)


@bot.command(name="reset")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def reset_prefix(
    ctx: commands.Context, reset_type: str, member: Optional[discord.Member] = None
) -> None:
    reset_type = reset_type.lower()
    if reset_type in ("text", "كتابي"):
        reset_type = "text"
    elif reset_type in ("voice", "صوتي"):
        reset_type = "voice"
    else:
        reset_type = "all"
    await run_reset_xp(ctx, reset_type, member)


@bot.group(name="move", invoke_without_command=True)
@commands.guild_only()
async def move_group_prefix(ctx: commands.Context) -> None:
    await ctx.send(
        "طريقة الاستخدام: `!move all` أو `!move user @عضو #الروم_الصوتي`"
    )


@move_group_prefix.command(name="all")
@commands.has_permissions(move_members=True)
async def move_all_prefix(
    ctx: commands.Context, source: Optional[discord.VoiceChannel] = None
) -> None:
    await run_move_all(ctx, source)


@move_group_prefix.command(name="user")
@commands.has_permissions(move_members=True)
async def move_user_prefix(
    ctx: commands.Context, member: discord.Member, channel: discord.VoiceChannel
) -> None:
    await run_move_user(ctx, member, channel)


@bot.command(name="moveme")
@commands.guild_only()
@commands.has_permissions(move_members=True)
async def moveme_prefix(ctx: commands.Context, channel: discord.VoiceChannel) -> None:
    await run_moveme(ctx, channel)


@bot.group(name="mute", invoke_without_command=True)
@commands.guild_only()
async def mute_group_prefix(ctx: commands.Context) -> None:
    await ctx.send("طريقة الاستخدام: `!mute voice @عضو` أو `!mute text @عضو`")


@mute_group_prefix.command(name="voice")
@commands.has_permissions(mute_members=True)
async def mute_voice_prefix(ctx: commands.Context, member: discord.Member) -> None:
    await run_mute_voice(ctx, member)


@mute_group_prefix.command(name="text")
@commands.has_permissions(moderate_members=True)
async def mute_text_prefix(ctx: commands.Context, member: discord.Member) -> None:
    await run_mute_text(ctx, member)


@bot.group(name="remove", aliases=["rm"], invoke_without_command=True)
@commands.guild_only()
async def remove_group_prefix(ctx: commands.Context) -> None:
    await ctx.send("طريقة الاستخدام: `!remove warn <رقم>` أو `!remove commands channel #القناة`")


@remove_group_prefix.command(name="warn", aliases=["delwarn", "removewarn"])
@commands.has_permissions(administrator=True)
async def removewarn_prefix(ctx: commands.Context, warning_id: int) -> None:
    await run_removewarn(ctx, warning_id)


@bot.group(name="add", invoke_without_command=True)
@commands.guild_only()
async def add_group_prefix(ctx: commands.Context) -> None:
    await ctx.send("طريقة الاستخدام: `!add xp <المقدار>` أو `!add petals <المقدار>`")


@add_group_prefix.command(name="xp", aliases=["addxp"])
@commands.has_permissions(administrator=True)
async def addxp_prefix(
    ctx: commands.Context, amount: int, member: Optional[discord.Member] = None
) -> None:
    if amount < 1 or amount > 100_000:
        await ctx.send("مقدار XP يجب أن يكون بين 1 و100000.")
        return
    target = member or ctx.author
    old_xp, new_xp = add_member_xp(ctx.guild.id, target.id, amount, source="both")
    await ctx.send(f"تمت إضافة **{amount:,} XP** إلى الدردشة النصية والصوت للعضو {target.mention}.")
    if level_from_xp(new_xp) > level_from_xp(old_xp):
        await send_level_up_message(target, level_from_xp(new_xp), "both")


@bot.command(name="rank", aliases=["r"])
@commands.guild_only()
async def rank_prefix(
    ctx: commands.Context, member: Optional[discord.Member] = None
) -> None:
    await run_rank(ctx, member or ctx.author)


@bot.command(name="profile", aliases=["p"])
@commands.guild_only()
async def profile_prefix(
    ctx: commands.Context, member: Optional[discord.Member] = None
) -> None:
    await run_profile(ctx, member or ctx.author)


@bot.command(name="daily", aliases=["d"])
@commands.guild_only()
async def daily_prefix(ctx: commands.Context) -> None:
    await run_daily(ctx)


@bot.command(name="balance", aliases=["c", "pt", "بتلات"])
@commands.guild_only()
async def balance_prefix(
    ctx: commands.Context,
    member: Optional[discord.Member] = None,
    amount: Optional[int] = None,
) -> None:
    # pt                -> يعرض رصيدك أنت
    # pt @شخص           -> يعرض رصيد هذا الشخص
    # pt @شخص المبلغ    -> يحول المبلغ لهذا الشخص
    if member is not None and amount is not None:
        await run_give(ctx, member, amount)
        return
    await run_balance(ctx, member or ctx.author)


@bot.command(name="give", aliases=["g"])
@commands.guild_only()
async def give_prefix(
    ctx: commands.Context, member: discord.Member, amount: int
) -> None:
    await run_give(ctx, member, amount)


PERIOD_WORDS = {"day": "day", "week": "week", "month": "month", "all": "all",
                "يوم": "day", "اسبوع": "week", "أسبوع": "week", "شهر": "month"}


@bot.command(name="top", aliases=["t"])
@commands.guild_only()
async def top_prefix(
    ctx: commands.Context, arg1: str = "petals", arg2: str = "all"
) -> None:
    board_type = "petals"
    period = "all"
    for value in (arg1, arg2):
        lowered = value.lower()
        if lowered in ("level", "مستوى"):
            board_type = "level"
        elif lowered in ("petals", "بتلز"):
            board_type = "petals"
        elif lowered in PERIOD_WORDS:
            board_type = "level"
            period = PERIOD_WORDS[lowered]
    await run_top(ctx, board_type, period)


@bot.group(name="set", invoke_without_command=True)
@commands.guild_only()
async def set_group_prefix(ctx: commands.Context) -> None:
    await ctx.send("طريقة الاستخدام: `!set commands channel #القناة`")


@set_group_prefix.group(name="commands", invoke_without_command=True)
async def set_commands_group_prefix(ctx: commands.Context) -> None:
    await ctx.send("طريقة الاستخدام: `!set commands channel #القناة`")


@set_commands_group_prefix.command(name="channel", aliases=["scc", "setcommandschannel"])
@commands.has_permissions(administrator=True)
async def setcommandschannel_prefix(
    ctx: commands.Context, channel: discord.TextChannel
) -> None:
    add_commands_channel(ctx.guild.id, channel.id)
    allowed_channels = get_commands_channels(ctx.guild.id)
    channels_text = "، ".join(f"<#{cid}>" for cid in allowed_channels)
    await ctx.send(
        f"✅ تمت إضافة القناة {channel.mention} إلى قائمة القنوات المسموح فيها بتنفيذ الأوامر.\n"
        f"القنوات المسموحة حاليًا: {channels_text}"
    )


@set_group_prefix.group(name="level", invoke_without_command=True)
async def set_level_group_prefix(ctx: commands.Context) -> None:
    await ctx.send("طريقة الاستخدام: `!set level channel #الروم`")


@set_level_group_prefix.command(name="channel")
@commands.has_permissions(administrator=True)
async def set_level_channel_prefix(
    ctx: commands.Context, channel: discord.TextChannel
) -> None:
    set_level_channel_id(ctx.guild.id, channel.id)
    await ctx.send(f"✅ تم تحديد {channel.mention} كروم لإشعارات رفع المستوى.")


@set_group_prefix.group(name="join", invoke_without_command=True)
async def set_join_group_prefix(ctx: commands.Context) -> None:
    await ctx.send("طريقة الاستخدام: `!set join role @الرتبة`")


@set_join_group_prefix.command(name="role")
@commands.has_permissions(administrator=True)
async def set_join_role_prefix(
    ctx: commands.Context, role: Optional[discord.Role] = None
) -> None:
    set_join_role_id(ctx.guild.id, role.id if role else None)
    if role:
        await ctx.send(f"✅ تم تحديد {role.mention} كرتبة دخول تلقائية.")
    else:
        await ctx.send("✅ تم إلغاء رتبة الدخول التلقائية.")


@bot.group(name="levelrole", invoke_without_command=True)
@commands.guild_only()
async def levelrole_group_prefix(ctx: commands.Context) -> None:
    await ctx.send(
        "طريقة الاستخدام: `!levelrole add <المستوى> @الرتبة` أو "
        "`!levelrole remove <المستوى>` أو `!levelrole list`"
    )


@levelrole_group_prefix.command(name="add")
@commands.has_permissions(administrator=True)
async def levelrole_add_prefix(
    ctx: commands.Context, level: int, role: discord.Role
) -> None:
    set_level_role_reward(ctx.guild.id, level, role.id)
    await ctx.send(f"✅ عند الوصول للمستوى **{level}**، بيحصل العضو تلقائيًا على {role.mention}.")


@levelrole_group_prefix.command(name="remove")
@commands.has_permissions(administrator=True)
async def levelrole_remove_prefix(ctx: commands.Context, level: int) -> None:
    success = remove_level_role_reward(ctx.guild.id, level)
    if success:
        await ctx.send(f"✅ تم حذف مكافأة المستوى **{level}**.")
    else:
        await ctx.send(f"لا توجد مكافأة مسجلة للمستوى **{level}**.")


@levelrole_group_prefix.command(name="list")
async def levelrole_list_prefix(ctx: commands.Context) -> None:
    rewards = get_level_role_rewards(ctx.guild.id)
    if not rewards:
        await ctx.send("ما فيه أي رتب مستويات مفعّلة حاليًا.")
        return
    lines = [f"المستوى **{row['level']}** ← <@&{row['role_id']}>" for row in rewards]
    await ctx.send("\n".join(lines))


@remove_group_prefix.group(name="commands", invoke_without_command=True)
async def remove_commands_group_prefix(ctx: commands.Context) -> None:
    await ctx.send("طريقة الاستخدام: `!remove commands channel #القناة`")


@remove_commands_group_prefix.command(name="channel", aliases=["rcc", "removecommandschannel"])
@commands.has_permissions(administrator=True)
async def removecommandschannel_prefix(
    ctx: commands.Context, channel: discord.TextChannel
) -> None:
    success = remove_commands_channel(ctx.guild.id, channel.id)
    if success:
        await ctx.send(f"✅ تم حذف القناة {channel.mention} من قائمة القنوات المسموح فيها بتنفيذ الأوامر.")
    else:
        await ctx.send(f"القناة {channel.mention} غير موجودة أساسًا ضمن القائمة.")


@add_group_prefix.command(name="petals", aliases=["ap", "addpetals"])
@commands.has_permissions(administrator=True)
async def addpetals_prefix(
    ctx: commands.Context, amount: int, member: Optional[discord.Member] = None
) -> None:
    await run_addpetals(ctx, member or ctx.author, amount)


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
        message = "تعذّر تنفيذ العملية بسبب رفض Discord؛ يرجى التحقق من الصلاحيات وترتيب الأدوار."
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


COMMAND_PARAMS: dict[str, list[tuple[str, str, bool]]] = {
    "warn": [("member", "العضو", True), ("reason", "السبب", False)],
    "ban": [("member", "العضو", True), ("reason", "السبب", False)],
    "kick": [("member", "العضو", True), ("reason", "السبب", False)],
    "timeout": [("member", "العضو", True), ("duration", "المدة", True), ("reason", "السبب", False)],
    "give": [("member", "العضو", True), ("amount", "المبلغ", True)],
    "add xp": [("amount", "المقدار", True), ("member", "العضو", False)],
    "add petals": [("amount", "المقدار", True), ("member", "العضو", False)],
    "clear": [("amount", "العدد", True)],
    "set commands channel": [("channel", "القناة", True)],
    "remove commands channel": [("channel", "القناة", True)],
    "warnings": [("member", "العضو", False)],
    "remove warn": [("warning_id", "رقم التحذير", True)],
}


def build_missing_argument_message(command_name: str, missing_param: str) -> str:
    """يبني رسالة خطأ توضح الاستخدام الصحيح مع سهم يشير للعنصر الناقص."""
    params = COMMAND_PARAMS.get(command_name)
    if not params:
        return f"بيانات ناقصة. الاستخدام الصحيح: `!{command_name} ...`"

    tokens = []
    missing_token = None
    missing_label = missing_param
    for name, label, required in params:
        token = f"<{name}>" if required else f"[{name}]"
        tokens.append(token)
        if name == missing_param:
            missing_token = token
            missing_label = label

    usage_line = f"!{command_name} " + " ".join(tokens)
    if missing_token is None:
        return f"بيانات ناقصة. الاستخدام الصحيح: `{usage_line}`"

    token_index = usage_line.find(missing_token)
    caret_line = " " * token_index + "^" * len(missing_token)

    return (
        f"```\n{usage_line}\n{caret_line}\n"
        f"{missing_label} عنصر مطلوب ولكنه مفقود في رسالتك.\n```"
    )


@bot.event
async def on_command_error(
    ctx: commands.Context, error: commands.CommandError
) -> None:
    original_error = getattr(error, "original", error)

    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.send("هذا الأمر متاح داخل الخادم فقط.")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("ليس لديك الصلاحية المطلوبة لاستخدام هذا الأمر.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("البوت لا يملك الصلاحيات المطلوبة لتنفيذ هذا الأمر.")
    elif isinstance(error, commands.MissingRequiredArgument):
        message = build_missing_argument_message(ctx.command.qualified_name, error.param.name)
        await ctx.send(message)
    elif isinstance(error, commands.BadArgument):
        await ctx.send(str(error))
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"يرجى الانتظار {error.retry_after:.1f} ثانية قبل المحاولة مرة أخرى.")
    elif isinstance(original_error, discord.Forbidden):
        await ctx.send("تعذّر تنفيذ العملية بسبب رفض Discord؛ يرجى التحقق من الصلاحيات وترتيب الأدوار.")
    elif isinstance(
        original_error, (FileNotFoundError, OSError, ValueError, discord.HTTPException)
    ):
        await ctx.send("تعذر إنشاء بطاقة المستوى حاليًا، يرجى التحقق من خلفية البطاقة وصورة العضو.")
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
import discord
from discord.ext import commands, tasks

import asyncio
import json
import os
import re
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo

import asyncpg
from aiohttp import web
from dotenv import load_dotenv


# ================== LOAD ENV ==================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

# Chuỗi kết nối Postgres (Neon / Supabase). Dạng:
# postgresql://user:pass@host/dbname?sslmode=require
DATABASE_URL = os.getenv("DATABASE_URL")

VIDEO_CHANNEL_ID = int(os.getenv("VIDEO_CHANNEL_ID", "0"))
REPORT_CHANNEL_ID = int(os.getenv("REPORT_CHANNEL_ID", str(VIDEO_CHANNEL_ID)))

YOUTUBE_BOT_ID = int(os.getenv("YOUTUBE_BOT_ID", "0"))
TSZ_USER_ID = int(os.getenv("TSZ_USER_ID", "0"))

WORKER_NAME = os.getenv("WORKER_NAME", "milo")
PRICE_PER_VIDEO = int(os.getenv("PRICE_PER_VIDEO", "113000"))

START_CYCLE_KEY = os.getenv("START_CYCLE_KEY", "2026-07")

# Khóa của dòng dữ liệu trong bảng Postgres
DATA_KEY = os.getenv("DATA_KEY", "payroll")

TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Ho_Chi_Minh"))

REPORT_HOUR = int(os.getenv("REPORT_HOUR", "9"))
REPORT_MINUTE = int(os.getenv("REPORT_MINUTE", "0"))

COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")

# Bật DEBUG=1 để in log soi tin nhắn (dùng khi chưa cộng tiền được).
DEBUG = os.getenv("DEBUG", "0") == "1"

# Render tự set PORT cho Web Service. Local thì mặc định 10000.
WEB_PORT = int(os.getenv("PORT", "10000"))


if not TOKEN:
    raise RuntimeError("Thiếu DISCORD_TOKEN trong biến môi trường")

if not DATABASE_URL:
    raise RuntimeError("Thiếu DATABASE_URL trong biến môi trường")

if VIDEO_CHANNEL_ID == 0:
    raise RuntimeError("Thiếu VIDEO_CHANNEL_ID trong biến môi trường")

# YOUTUBE_BOT_ID là tùy chọn:
# - Nếu set: chỉ tính tin của đúng bot/webhook đó.
# - Nếu để trống (0): tính mọi link YouTube do bot/webhook đăng trong kênh.

if TSZ_USER_ID == 0:
    raise RuntimeError("Thiếu TSZ_USER_ID trong biến môi trường")


# ================== DISCORD SETUP ==================

intents = discord.Intents.default()
intents.message_content = True

data_lock = asyncio.Lock()

# Pool kết nối Postgres, khởi tạo trong setup_hook
db_pool = None


class PayBot(commands.Bot):
    async def setup_hook(self):
        # Chạy 1 lần trước khi bot kết nối Discord.
        await init_db()
        asyncio.create_task(start_web_server())


bot = PayBot(command_prefix=COMMAND_PREFIX, intents=intents)


# ================== DATABASE ==================

async def init_db():
    global db_pool

    db_pool = await asyncpg.create_pool(DATABASE_URL)

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_data (
                id   TEXT PRIMARY KEY,
                data JSONB NOT NULL
            )
            """
        )

    print("Đã kết nối Postgres.")


async def load_data():
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT data FROM bot_data WHERE id = $1", DATA_KEY
        )

    if row is None:
        return {}

    data = row["data"]

    # asyncpg trả JSONB dưới dạng chuỗi
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return {}

    if not isinstance(data, dict):
        return {}

    return data


async def save_data(data):
    payload = json.dumps(data, ensure_ascii=False)

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO bot_data (id, data)
            VALUES ($1, $2::jsonb)
            ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data
            """,
            DATA_KEY,
            payload,
        )


def get_worker_data(data):
    data.setdefault(WORKER_NAME, {})
    data[WORKER_NAME].setdefault("_reported_cycles", [])
    return data[WORKER_NAME]


# ================== WEB SERVER (KEEP ALIVE) ==================

async def _health(request):
    return web.Response(text="Bot tinh tien dang chay.")


async def start_web_server():
    # Render Web Service bắt buộc phải mở 1 cổng, nếu không sẽ báo lỗi
    # "no open ports detected". Endpoint này cũng để UptimeRobot ping
    # cho service khỏi ngủ.
    app = web.Application()
    app.router.add_get("/", _health)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()

    print(f"Web server keep-alive đang chạy ở cổng {WEB_PORT}")


# ================== TIME / CYCLE ==================

def shift_month(year: int, month: int, delta: int):
    month += delta
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    return year, month


def make_cycle_key(year: int, month: int):
    return f"{year}-{month:02d}"


def parse_cycle_key(cycle_key: str):
    year_str, month_str = cycle_key.split("-")
    return int(year_str), int(month_str)


def valid_cycle_key(key: str):
    return bool(re.fullmatch(r"\d{4}-\d{2}", key))


def add_cycle(cycle_key: str, delta: int):
    year, month = parse_cycle_key(cycle_key)
    year, month = shift_month(year, month, delta)
    return make_cycle_key(year, month)


def iter_cycles(start_cycle_key: str, end_cycle_key: str):
    current = start_cycle_key

    while current <= end_cycle_key:
        yield current
        current = add_cycle(current, 1)


def get_cycle_key(d: date):
    # Chu kỳ:
    # 09 tháng này -> 08 tháng sau
    # Ví dụ:
    # 09/07/2026 -> 08/08/2026 = cycle 2026-07

    if d.day >= 9:
        return make_cycle_key(d.year, d.month)

    year, month = shift_month(d.year, d.month, -1)
    return make_cycle_key(year, month)


def get_cycle_report_due_datetime(cycle_key: str):
    # Cycle 2026-07 báo vào 09/08/2026 lúc REPORT_HOUR:REPORT_MINUTE

    year, month = parse_cycle_key(cycle_key)
    next_year, next_month = shift_month(year, month, 1)

    return datetime(
        next_year,
        next_month,
        9,
        REPORT_HOUR,
        REPORT_MINUTE,
        tzinfo=TZ
    )


def get_message_date_vn(message: discord.Message):
    created_at = message.created_at

    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    return created_at.astimezone(TZ).date()


# ================== YOUTUBE VIDEO ID ==================

def extract_youtube_video_id(text: str):
    patterns = [
        r"youtu\.be/([a-zA-Z0-9_-]{11})",
        r"youtube\.com/watch\?.*?v=([a-zA-Z0-9_-]{11})",
        r"youtube\.com/shorts/([a-zA-Z0-9_-]{11})",
        r"youtube\.com/embed/([a-zA-Z0-9_-]{11})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)

    return None


def get_video_id_from_message(message: discord.Message):
    parts = [message.content or ""]

    for embed in message.embeds:
        if embed.url:
            parts.append(embed.url)

        if embed.title:
            parts.append(embed.title)

        if embed.description:
            parts.append(embed.description)

        for field in embed.fields:
            parts.append(field.name or "")
            parts.append(field.value or "")

    combined = "\n".join(parts)
    return extract_youtube_video_id(combined)


def video_already_recorded(worker_data, video_id: str):
    # Dedup toàn bộ lịch sử.
    # Cùng 1 video bị YouTube bot gửi lại sẽ không bị tính tiền lần 2.

    for cycle_key, videos in worker_data.items():
        if cycle_key.startswith("_"):
            continue

        if not isinstance(videos, list):
            continue

        if video_id in videos:
            return True

    return False


def count_cycle_videos(worker_data, cycle_key: str):
    videos = worker_data.get(cycle_key, [])

    if not isinstance(videos, list):
        return 0

    return len(set(videos))


def money_format(amount: int):
    return f"{amount:,}".replace(",", ".")


def is_admin(ctx):
    # Chỉ admin / người quản lý server mới được sửa dữ liệu lương.
    perms = getattr(ctx.author, "guild_permissions", None)
    return bool(perms and (perms.administrator or perms.manage_guild))


# ================== BOT EVENTS ==================

@bot.event
async def on_ready():
    print(f"Bot đã online: {bot.user}")
    print(f"Worker: {WORKER_NAME}")
    print(f"Giá mỗi video: {money_format(PRICE_PER_VIDEO)}đ")
    print(f"Chu kỳ bắt đầu: {START_CYCLE_KEY}")
    print(f"Kênh video: {VIDEO_CHANNEL_ID}")
    print(f"Kênh báo cáo: {REPORT_CHANNEL_ID}")

    if not monthly_payment_report.is_running():
        monthly_payment_report.start()


@bot.event
async def on_message(message: discord.Message):
    if bot.user and message.author.id == bot.user.id:
        return

    if DEBUG:
        print(
            f"[DEBUG] channel={message.channel.id} author={message.author.id} "
            f"({message.author}) webhook={message.webhook_id} "
            f"embeds={len(message.embeds)} content={message.content[:120]!r}"
        )

    if message.channel.id != VIDEO_CHANNEL_ID:
        await bot.process_commands(message)
        return

    if DEBUG:
        try:
            vid_try = get_video_id_from_message(message)
            await message.channel.send(
                f"🔧 DEBUG | author=`{message.author.id}` bot=`{message.author.bot}` "
                f"webhook=`{message.webhook_id}` embeds=`{len(message.embeds)}` "
                f"video_id=`{vid_try}` content=`{(message.content or '')[:80]}`"
            )
        except Exception as e:
            print(f"[DEBUG] gửi debug thất bại: {e}")

    is_bot_or_webhook = bool(message.author.bot or message.webhook_id)

    if YOUTUBE_BOT_ID != 0:
        # Đã cấu hình: khớp theo id của bot HOẶC id webhook.
        if message.author.id != YOUTUBE_BOT_ID and message.webhook_id != YOUTUBE_BOT_ID:
            if DEBUG:
                print(
                    f"[DEBUG] Đúng kênh nhưng author={message.author.id} / "
                    f"webhook={message.webhook_id} KHÁC YOUTUBE_BOT_ID={YOUTUBE_BOT_ID}"
                )
            await bot.process_commands(message)
            return
    else:
        # Chưa cấu hình: chỉ tính tin do bot/webhook đăng, bỏ qua người thật gõ tay.
        if not is_bot_or_webhook:
            if DEBUG:
                print(
                    f"[DEBUG] Bỏ qua vì không phải bot/webhook: author={message.author.id}"
                )
            await bot.process_commands(message)
            return

    video_id = get_video_id_from_message(message)

    if not video_id:
        if DEBUG:
            embeds_info = [(e.title, e.url, e.description) for e in message.embeds]
            print(
                f"[DEBUG] Đúng kênh + đúng bot nhưng KHÔNG tìm được video ID. "
                f"content={message.content!r} embeds={embeds_info}"
            )
        await bot.process_commands(message)
        return

    msg_date = get_message_date_vn(message)
    cycle_key = get_cycle_key(msg_date)

    async with data_lock:
        data = await load_data()
        worker_data = get_worker_data(data)

        if video_already_recorded(worker_data, video_id):
            await bot.process_commands(message)
            return

        worker_data.setdefault(cycle_key, [])
        worker_data[cycle_key].append(video_id)

        await save_data(data)

    print(f"Đã ghi nhận video của {WORKER_NAME}: {video_id} | cycle {cycle_key}")

    # Thả icon để thấy ngay là đã ghi nhận
    try:
        await message.add_reaction("💰")
    except Exception:
        pass

    await bot.process_commands(message)


# ================== AUTO REPORT ==================

@tasks.loop(minutes=1)
async def monthly_payment_report():
    now = datetime.now(TZ)
    current_cycle = get_cycle_key(now.date())

    async with data_lock:
        data = await load_data()
        worker_data = get_worker_data(data)

        worker_data.setdefault("_reported_cycles", [])
        reported_cycles = set(worker_data["_reported_cycles"])

        cycles_to_report = []
        changed = False

        # Tạo và kiểm tra tất cả cycle từ START_CYCLE_KEY đến cycle hiện tại.
        # Tháng nào 0 video vẫn báo 0đ.
        for cycle_key in iter_cycles(START_CYCLE_KEY, current_cycle):
            if cycle_key not in worker_data:
                worker_data[cycle_key] = []
                changed = True

            if cycle_key in reported_cycles:
                continue

            due_time = get_cycle_report_due_datetime(cycle_key)

            if now >= due_time:
                cycles_to_report.append(cycle_key)

        if changed:
            await save_data(data)

    if not cycles_to_report:
        return

    channel = bot.get_channel(REPORT_CHANNEL_ID)

    if channel is None:
        try:
            channel = await bot.fetch_channel(REPORT_CHANNEL_ID)
        except Exception as e:
            print(f"Không lấy được kênh báo cáo: {e}")
            return

    for cycle_key in sorted(cycles_to_report):
        async with data_lock:
            data = await load_data()
            worker_data = get_worker_data(data)

            worker_data.setdefault("_reported_cycles", [])

            if cycle_key in worker_data["_reported_cycles"]:
                continue

            worker_data.setdefault(cycle_key, [])

            total_videos = count_cycle_videos(worker_data, cycle_key)
            total_money = total_videos * PRICE_PER_VIDEO

        month_number = int(cycle_key.split("-")[1])
        money_text = money_format(total_money)

        text = (
            f"<@{TSZ_USER_ID}> tổng tiền của {WORKER_NAME} tháng {month_number} "
            f"là {money_text}đ và có {total_videos} video đã đăng"
        )

        try:
            await channel.send(text)
        except Exception as e:
            print(f"Gửi báo cáo thất bại cho cycle {cycle_key}: {e}")
            continue

        async with data_lock:
            data = await load_data()
            worker_data = get_worker_data(data)

            worker_data.setdefault("_reported_cycles", [])

            if cycle_key not in worker_data["_reported_cycles"]:
                worker_data["_reported_cycles"].append(cycle_key)

            worker_data.setdefault(cycle_key, [])

            await save_data(data)

        print(f"Đã báo cáo tiền {WORKER_NAME} cycle {cycle_key}")


@monthly_payment_report.before_loop
async def before_monthly_payment_report():
    await bot.wait_until_ready()


# ================== COMMANDS ==================

@bot.command()
async def kiemtramilo(ctx):
    """
    Kiểm tra cấu hình ngay trong Discord.
    Gõ !kiemtramilo trong kênh #video-moi để xem kênh có khớp không.
    """

    here = ctx.channel.id
    match = "✅ KHỚP" if here == VIDEO_CHANNEL_ID else "❌ KHÔNG KHỚP"

    yt = YOUTUBE_BOT_ID if YOUTUBE_BOT_ID != 0 else "0 (tính mọi bot/webhook)"

    await ctx.send(
        f"**Kiểm tra cấu hình bot:**\n"
        f"- Kênh bạn đang gõ: `{here}`\n"
        f"- VIDEO_CHANNEL_ID đã set: `{VIDEO_CHANNEL_ID}`\n"
        f"- Kết quả: **{match}**\n"
        f"- YOUTUBE_BOT_ID: `{yt}`\n"
        f"- Worker: `{WORKER_NAME}` | Giá: {money_format(PRICE_PER_VIDEO)}đ\n\n"
        f"Nếu KHÔNG KHỚP: copy số `{here}` vào biến `VIDEO_CHANNEL_ID` trên Render."
    )


@bot.command()
async def tienmilo(ctx, cycle_key: str = None):
    """
    Xem tiền tạm tính.

    Dùng:
    !tienmilo
    !tienmilo 2026-07
    """

    if cycle_key is None:
        today = datetime.now(TZ).date()
        cycle_key = get_cycle_key(today)

    if not valid_cycle_key(cycle_key):
        await ctx.send("Sai định dạng. Dùng dạng: `!tienmilo 2026-07`")
        return

    async with data_lock:
        data = await load_data()
        worker_data = get_worker_data(data)

        total_videos = count_cycle_videos(worker_data, cycle_key)
        total_money = total_videos * PRICE_PER_VIDEO

    month_number = int(cycle_key.split("-")[1])

    await ctx.send(
        f"Tạm tính tiền của {WORKER_NAME} tháng {month_number}: "
        f"{money_format(total_money)}đ / {total_videos} video đã đăng"
    )


@bot.command()
async def baocaomilo(ctx, cycle_key: str = None):
    """
    Báo cáo thủ công.

    Dùng:
    !baocaomilo
    !baocaomilo 2026-07
    """

    if cycle_key is None:
        today = datetime.now(TZ).date()
        current_cycle = get_cycle_key(today)
        cycle_key = add_cycle(current_cycle, -1)

    if not valid_cycle_key(cycle_key):
        await ctx.send("Sai định dạng. Dùng dạng: `!baocaomilo 2026-07`")
        return

    async with data_lock:
        data = await load_data()
        worker_data = get_worker_data(data)

        total_videos = count_cycle_videos(worker_data, cycle_key)
        total_money = total_videos * PRICE_PER_VIDEO

    month_number = int(cycle_key.split("-")[1])

    await ctx.send(
        f"<@{TSZ_USER_ID}> tổng tiền của {WORKER_NAME} tháng {month_number} "
        f"là {money_format(total_money)}đ và có {total_videos} video đã đăng"
    )


@bot.command()
async def testbaocao(ctx, cycle_key: str = None):
    """
    Gửi thử báo cáo vào ĐÚNG kênh REPORT_CHANNEL_ID (kênh báo cáo tự động),
    để kiểm tra báo cáo có rơi đúng #tinh-tien không.

    Dùng:
    !testbaocao
    !testbaocao 2026-07
    """

    if not is_admin(ctx):
        await ctx.send("Bạn không có quyền dùng lệnh này.")
        return

    if cycle_key is None:
        cycle_key = get_cycle_key(datetime.now(TZ).date())

    if not valid_cycle_key(cycle_key):
        await ctx.send("Sai định dạng. Dùng dạng: `!testbaocao 2026-07`")
        return

    channel = bot.get_channel(REPORT_CHANNEL_ID)

    if channel is None:
        try:
            channel = await bot.fetch_channel(REPORT_CHANNEL_ID)
        except Exception as e:
            await ctx.send(
                f"❌ Không tìm thấy kênh báo cáo `{REPORT_CHANNEL_ID}`: {e}\n"
                f"Kiểm tra lại biến REPORT_CHANNEL_ID trên Render."
            )
            return

    async with data_lock:
        data = await load_data()
        worker_data = get_worker_data(data)

        total_videos = count_cycle_videos(worker_data, cycle_key)
        total_money = total_videos * PRICE_PER_VIDEO

    month_number = int(cycle_key.split("-")[1])

    try:
        await channel.send(
            f"[THỬ] <@{TSZ_USER_ID}> tổng tiền của {WORKER_NAME} tháng {month_number} "
            f"là {money_format(total_money)}đ và có {total_videos} video đã đăng"
        )
    except Exception as e:
        await ctx.send(f"❌ Gửi vào kênh báo cáo thất bại: {e}")
        return

    await ctx.send(
        f"✅ Đã gửi thử báo cáo vào kênh `{REPORT_CHANNEL_ID}` (<#{REPORT_CHANNEL_ID}>). "
        f"Kiểm tra kênh đó xem tin đã tới chưa."
    )


@bot.command()
async def danhsachmilo(ctx, cycle_key: str = None):
    """
    Xem danh sách video ID đã tính tiền.

    Dùng:
    !danhsachmilo
    !danhsachmilo 2026-07
    """

    if cycle_key is None:
        today = datetime.now(TZ).date()
        cycle_key = get_cycle_key(today)

    if not valid_cycle_key(cycle_key):
        await ctx.send("Sai định dạng. Dùng dạng: `!danhsachmilo 2026-07`")
        return

    async with data_lock:
        data = await load_data()
        worker_data = get_worker_data(data)

        videos = worker_data.get(cycle_key, [])

        if not isinstance(videos, list):
            videos = []

    total_videos = len(set(videos))
    total_money = total_videos * PRICE_PER_VIDEO

    if total_videos == 0:
        await ctx.send(
            f"Cycle `{cycle_key}` chưa có video nào. Tổng tiền: 0đ."
        )
        return

    lines = []
    for index, video_id in enumerate(sorted(set(videos)), start=1):
        lines.append(f"{index}. https://youtu.be/{video_id}")

    text = (
        f"Danh sách video của {WORKER_NAME} cycle `{cycle_key}`:\n"
        f"Tổng: {total_videos} video / {money_format(total_money)}đ\n\n"
        + "\n".join(lines)
    )

    if len(text) <= 1900:
        await ctx.send(text)
        return

    chunks = []
    current = (
        f"Danh sách video của {WORKER_NAME} cycle `{cycle_key}`:\n"
        f"Tổng: {total_videos} video / {money_format(total_money)}đ\n\n"
    )

    for line in lines:
        if len(current) + len(line) + 1 > 1900:
            chunks.append(current)
            current = ""

        current += line + "\n"

    if current.strip():
        chunks.append(current)

    for chunk in chunks:
        await ctx.send(chunk)


@bot.command()
async def themvideomilo(ctx, video_url_or_id: str, cycle_key: str = None):
    """
    Thêm video thủ công nếu bot YouTube bị sót.

    Dùng:
    !themvideomilo https://youtu.be/E5qNw_lKqq8
    !themvideomilo E5qNw_lKqq8 2026-07
    """

    if not is_admin(ctx):
        await ctx.send("Bạn không có quyền dùng lệnh này.")
        return

    if cycle_key is None:
        today = datetime.now(TZ).date()
        cycle_key = get_cycle_key(today)

    if not valid_cycle_key(cycle_key):
        await ctx.send("Sai định dạng cycle. Dùng dạng: `2026-07`")
        return

    video_id = extract_youtube_video_id(video_url_or_id)

    if video_id is None and re.fullmatch(r"[a-zA-Z0-9_-]{11}", video_url_or_id):
        video_id = video_url_or_id

    if video_id is None:
        await ctx.send("Không lấy được video ID từ link hoặc ID đã nhập.")
        return

    async with data_lock:
        data = await load_data()
        worker_data = get_worker_data(data)

        if video_already_recorded(worker_data, video_id):
            await ctx.send(f"Video này đã được ghi nhận trước đó: `{video_id}`")
            return

        worker_data.setdefault(cycle_key, [])
        worker_data[cycle_key].append(video_id)

        await save_data(data)

    await ctx.send(f"Đã thêm video `{video_id}` vào cycle `{cycle_key}`.")


@bot.command()
async def xoavideomilo(ctx, video_url_or_id: str):
    """
    Xóa video thủ công khỏi dữ liệu nếu tính nhầm.

    Dùng:
    !xoavideomilo https://youtu.be/E5qNw_lKqq8
    !xoavideomilo E5qNw_lKqq8
    """

    if not is_admin(ctx):
        await ctx.send("Bạn không có quyền dùng lệnh này.")
        return

    video_id = extract_youtube_video_id(video_url_or_id)

    if video_id is None and re.fullmatch(r"[a-zA-Z0-9_-]{11}", video_url_or_id):
        video_id = video_url_or_id

    if video_id is None:
        await ctx.send("Không lấy được video ID từ link hoặc ID đã nhập.")
        return

    removed_from = []

    async with data_lock:
        data = await load_data()
        worker_data = get_worker_data(data)

        for cycle_key, videos in worker_data.items():
            if cycle_key.startswith("_"):
                continue

            if not isinstance(videos, list):
                continue

            if video_id in videos:
                worker_data[cycle_key] = [v for v in videos if v != video_id]
                removed_from.append(cycle_key)

        if removed_from:
            await save_data(data)

    if not removed_from:
        await ctx.send(f"Không tìm thấy video `{video_id}` trong dữ liệu.")
        return

    await ctx.send(
        f"Đã xóa video `{video_id}` khỏi cycle: {', '.join(removed_from)}"
    )


if not valid_cycle_key(START_CYCLE_KEY):
    raise RuntimeError("START_CYCLE_KEY sai định dạng, phải là dạng YYYY-MM ví dụ 2026-07")


bot.run(TOKEN)

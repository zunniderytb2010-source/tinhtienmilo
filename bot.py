import discord
from discord.ext import commands, tasks

import asyncio
import json
import os
import re
from pathlib import Path
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


# ================== LOAD ENV ==================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

VIDEO_CHANNEL_ID = int(os.getenv("VIDEO_CHANNEL_ID", "0"))
REPORT_CHANNEL_ID = int(os.getenv("REPORT_CHANNEL_ID", str(VIDEO_CHANNEL_ID)))

YOUTUBE_BOT_ID = int(os.getenv("YOUTUBE_BOT_ID", "0"))
TSZ_USER_ID = int(os.getenv("TSZ_USER_ID", "0"))

WORKER_NAME = os.getenv("WORKER_NAME", "milo")
PRICE_PER_VIDEO = int(os.getenv("PRICE_PER_VIDEO", "113000"))

START_CYCLE_KEY = os.getenv("START_CYCLE_KEY", "2026-07")

DATA_FILE = Path(os.getenv("DATA_FILE", "milo_pay.json"))
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Ho_Chi_Minh"))

REPORT_HOUR = int(os.getenv("REPORT_HOUR", "9"))
REPORT_MINUTE = int(os.getenv("REPORT_MINUTE", "0"))

COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")


if not TOKEN:
    raise RuntimeError("Thiếu DISCORD_TOKEN trong file .env")

if VIDEO_CHANNEL_ID == 0:
    raise RuntimeError("Thiếu VIDEO_CHANNEL_ID trong file .env")

if YOUTUBE_BOT_ID == 0:
    raise RuntimeError("Thiếu YOUTUBE_BOT_ID trong file .env")

if TSZ_USER_ID == 0:
    raise RuntimeError("Thiếu TSZ_USER_ID trong file .env")


# ================== DISCORD SETUP ==================

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

data_lock = asyncio.Lock()


# ================== DATA ==================

def load_data():
    if not DATA_FILE.exists():
        return {}

    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return {}

        return data

    except json.JSONDecodeError:
        corrupt_name = DATA_FILE.with_name(
            f"{DATA_FILE.stem}.corrupt-{datetime.now(TZ).strftime('%Y%m%d-%H%M%S')}{DATA_FILE.suffix}"
        )

        try:
            DATA_FILE.replace(corrupt_name)
            print(f"File JSON hỏng, đã đổi tên thành: {corrupt_name}")
        except OSError:
            print("File JSON hỏng, không đổi tên được.")

        return {}

    except OSError as e:
        print(f"Lỗi đọc file data: {e}")
        return {}


def save_data(data):
    tmp_file = DATA_FILE.with_suffix(DATA_FILE.suffix + ".tmp")

    with tmp_file.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    os.replace(tmp_file, DATA_FILE)


def get_worker_data(data):
    data.setdefault(WORKER_NAME, {})
    data[WORKER_NAME].setdefault("_reported_cycles", [])
    return data[WORKER_NAME]


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

    if message.channel.id != VIDEO_CHANNEL_ID:
        await bot.process_commands(message)
        return

    if message.author.id != YOUTUBE_BOT_ID:
        await bot.process_commands(message)
        return

    video_id = get_video_id_from_message(message)

    if not video_id:
        await bot.process_commands(message)
        return

    msg_date = get_message_date_vn(message)
    cycle_key = get_cycle_key(msg_date)

    async with data_lock:
        data = load_data()
        worker_data = get_worker_data(data)

        if video_already_recorded(worker_data, video_id):
            await bot.process_commands(message)
            return

        worker_data.setdefault(cycle_key, [])
        worker_data[cycle_key].append(video_id)

        save_data(data)

    print(f"Đã ghi nhận video của {WORKER_NAME}: {video_id} | cycle {cycle_key}")

    await bot.process_commands(message)


# ================== AUTO REPORT ==================

@tasks.loop(minutes=1)
async def monthly_payment_report():
    now = datetime.now(TZ)
    current_cycle = get_cycle_key(now.date())

    async with data_lock:
        data = load_data()
        worker_data = get_worker_data(data)

        worker_data.setdefault("_reported_cycles", [])
        reported_cycles = set(worker_data["_reported_cycles"])

        cycles_to_report = []

        # Tạo và kiểm tra tất cả cycle từ START_CYCLE_KEY đến cycle hiện tại.
        # Tháng nào 0 video vẫn báo 0đ.
        for cycle_key in iter_cycles(START_CYCLE_KEY, current_cycle):
            worker_data.setdefault(cycle_key, [])

            if cycle_key in reported_cycles:
                continue

            due_time = get_cycle_report_due_datetime(cycle_key)

            if now >= due_time:
                cycles_to_report.append(cycle_key)

        save_data(data)

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
            data = load_data()
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
            data = load_data()
            worker_data = get_worker_data(data)

            worker_data.setdefault("_reported_cycles", [])

            if cycle_key not in worker_data["_reported_cycles"]:
                worker_data["_reported_cycles"].append(cycle_key)

            worker_data.setdefault(cycle_key, [])

            save_data(data)

        print(f"Đã báo cáo tiền {WORKER_NAME} cycle {cycle_key}")


@monthly_payment_report.before_loop
async def before_monthly_payment_report():
    await bot.wait_until_ready()


# ================== COMMANDS ==================

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
        data = load_data()
        worker_data = get_worker_data(data)

        worker_data.setdefault(cycle_key, [])

        total_videos = count_cycle_videos(worker_data, cycle_key)
        total_money = total_videos * PRICE_PER_VIDEO

        save_data(data)

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
        data = load_data()
        worker_data = get_worker_data(data)

        worker_data.setdefault(cycle_key, [])

        total_videos = count_cycle_videos(worker_data, cycle_key)
        total_money = total_videos * PRICE_PER_VIDEO

        save_data(data)

    month_number = int(cycle_key.split("-")[1])

    await ctx.send(
        f"<@{TSZ_USER_ID}> tổng tiền của {WORKER_NAME} tháng {month_number} "
        f"là {money_format(total_money)}đ và có {total_videos} video đã đăng"
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
        data = load_data()
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
        data = load_data()
        worker_data = get_worker_data(data)

        if video_already_recorded(worker_data, video_id):
            await ctx.send(f"Video này đã được ghi nhận trước đó: `{video_id}`")
            return

        worker_data.setdefault(cycle_key, [])
        worker_data[cycle_key].append(video_id)

        save_data(data)

    await ctx.send(f"Đã thêm video `{video_id}` vào cycle `{cycle_key}`.")


@bot.command()
async def xoavideomilo(ctx, video_url_or_id: str):
    """
    Xóa video thủ công khỏi dữ liệu nếu tính nhầm.

    Dùng:
    !xoavideomilo https://youtu.be/E5qNw_lKqq8
    !xoavideomilo E5qNw_lKqq8
    """

    video_id = extract_youtube_video_id(video_url_or_id)

    if video_id is None and re.fullmatch(r"[a-zA-Z0-9_-]{11}", video_url_or_id):
        video_id = video_url_or_id

    if video_id is None:
        await ctx.send("Không lấy được video ID từ link hoặc ID đã nhập.")
        return

    removed_from = []

    async with data_lock:
        data = load_data()
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
            save_data(data)

    if not removed_from:
        await ctx.send(f"Không tìm thấy video `{video_id}` trong dữ liệu.")
        return

    await ctx.send(
        f"Đã xóa video `{video_id}` khỏi cycle: {', '.join(removed_from)}"
    )


bot.run(TOKEN)
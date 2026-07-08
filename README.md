# tinhtienmilo

Bot Discord tính tiền lương theo số video YouTube đã đăng.

## Cách hoạt động

- Bot theo dõi kênh `VIDEO_CHANNEL_ID`, mỗi khi bot YouTube (`YOUTUBE_BOT_ID`) đăng link video sẽ ghi nhận 1 video.
- Cùng 1 video (theo video ID) chỉ tính tiền **1 lần**, dù bị đăng lại.
- **Chu kỳ tính**: từ ngày 9 tháng này đến ngày 8 tháng sau. Ví dụ chu kỳ `2026-07` = 09/07 → 08/08.
- Đến ngày 9 lúc `REPORT_HOUR:REPORT_MINUTE`, bot tự gửi báo cáo tổng tiền và tag `TSZ_USER_ID`.
- Dữ liệu lưu trong **Postgres** (không lưu file), nên host trên Render không bị mất khi restart.

## Lệnh

| Lệnh | Việc | Quyền |
|------|------|-------|
| `!tienmilo [2026-07]` | Xem tiền tạm tính | Ai cũng dùng được |
| `!danhsachmilo [2026-07]` | Xem danh sách video đã tính | Ai cũng dùng được |
| `!baocaomilo [2026-07]` | Báo cáo thủ công | Ai cũng dùng được |
| `!themvideomilo <link\|id> [2026-07]` | Thêm video thủ công | Chỉ admin server |
| `!xoavideomilo <link\|id>` | Xóa video tính nhầm | Chỉ admin server |

Bỏ trống chu kỳ thì mặc định lấy chu kỳ hiện tại (riêng `!baocaomilo` lấy chu kỳ trước).

## Chạy thử ở máy local

```bash
pip install -r requirements.txt
cp .env.example .env   # rồi mở .env điền token, DATABASE_URL và các ID
python bot.py
```

Trong Discord Developer Portal, bật **MESSAGE CONTENT INTENT** cho bot (bắt buộc để đọc được link video).

## Deploy lên Render.com (miễn phí)

### 1. Tạo database Postgres miễn phí

Chọn 1 trong 2 (dữ liệu lương sẽ nằm ở đây, đừng để mất):
- **Neon** (khuyến nghị): https://neon.tech → tạo project → copy **Connection string** (dạng `postgresql://...?sslmode=require`).
- **Supabase**: https://supabase.com → Project Settings → Database → **Connection string**.

Chuỗi này chính là `DATABASE_URL`.

### 2. Tạo Web Service trên Render

- New → **Web Service** → kết nối repo này (hoặc dùng **Blueprint** với file `render.yaml` có sẵn).
- Build command: `pip install -r requirements.txt`
- Start command: `python bot.py`
- Tab **Environment**: điền `DISCORD_TOKEN`, `DATABASE_URL`, `VIDEO_CHANNEL_ID`, `YOUTUBE_BOT_ID`, `TSZ_USER_ID`, và các biến khác nếu cần.
- **KHÔNG** cần set `PORT`, Render tự set.

### 3. Chống ngủ (quan trọng với gói Free)

Render Free tự ngủ sau 15 phút không có request → bot sẽ offline. Cách giữ:
- Sao chép URL của service (dạng `https://tinhtienmilo.onrender.com`).
- Vào https://uptimerobot.com (free) → tạo monitor **HTTP(s)** trỏ vào URL đó, ping mỗi **5 phút**.

Vậy là bot chạy 24/7 và không mất dữ liệu.

## Lưu ý

- **KHÔNG** commit file `.env` (đã có `.gitignore`).
- Toàn bộ tiền lương nằm trong Postgres — thỉnh thoảng nên export/sao lưu database.

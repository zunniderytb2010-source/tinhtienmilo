# tinhtienmilo

Bot Discord tính tiền lương theo số video YouTube đã đăng.

## Cách hoạt động

- Bot theo dõi kênh `VIDEO_CHANNEL_ID`, mỗi khi bot YouTube (`YOUTUBE_BOT_ID`) đăng link video sẽ ghi nhận 1 video.
- Cùng 1 video (theo video ID) chỉ tính tiền **1 lần**, dù bị đăng lại.
- **Chu kỳ tính**: từ ngày 9 tháng này đến ngày 8 tháng sau. Ví dụ chu kỳ `2026-07` = 09/07 → 08/08.
- Đến ngày 9 lúc `REPORT_HOUR:REPORT_MINUTE`, bot tự gửi báo cáo tổng tiền và tag `TSZ_USER_ID`.

## Cài đặt

```bash
pip install -r requirements.txt
cp .env.example .env   # rồi mở .env điền token và các ID
python bot.py
```

Trong Discord Developer Portal, bật **MESSAGE CONTENT INTENT** cho bot (bắt buộc để đọc được link video).

## Lệnh

| Lệnh | Việc | Quyền |
|------|------|-------|
| `!tienmilo [2026-07]` | Xem tiền tạm tính | Ai cũng dùng được |
| `!danhsachmilo [2026-07]` | Xem danh sách video đã tính | Ai cũng dùng được |
| `!baocaomilo [2026-07]` | Báo cáo thủ công | Ai cũng dùng được |
| `!themvideomilo <link\|id> [2026-07]` | Thêm video thủ công | Chỉ admin server |
| `!xoavideomilo <link\|id>` | Xóa video tính nhầm | Chỉ admin server |

Bỏ trống chu kỳ thì mặc định lấy chu kỳ hiện tại (riêng `!baocaomilo` lấy chu kỳ trước).

## Lưu ý

- **KHÔNG** commit file `.env` và `milo_pay.json` (đã có `.gitignore`).
- Dữ liệu lưu trong `milo_pay.json`, nhớ sao lưu file này.

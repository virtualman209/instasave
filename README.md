# InstaSaved – tự tải mục “Đã lưu” Instagram về mini PC (OMV)

Web app tự host chạy bằng Docker trên OpenMediaVault:

- Tự động đồng bộ mục **Saved** của tài khoản Instagram theo chu kỳ (mặc định 6 giờ), chỉ tải bài mới.
- Tải ảnh, video, album (đủ từng ảnh trong album) về đúng thư mục danh mục.
- **Tự phân loại** theo: quy tắc của bạn (hashtag, @tác giả, từ khoá, loại bài) → tên **Bộ sưu tập** bạn đã tạo trên Instagram → danh mục mặc định.
- **Giữ link gốc** của từng bài: hiện trong web, ghi vào file `.txt` cạnh ảnh, và xuất được toàn bộ ra CSV (mở bằng Excel).
- Giao diện web dùng tốt trên điện thoại: xem lưới, tìm kiếm, lọc, xem ảnh/video, đổi danh mục thủ công.

## Cấu trúc thư mục dữ liệu

```
instagram/                 ← shared folder trên OMV (truy cập được qua SMB)
├── media/
│   ├── Du lịch/
│   │   ├── natgeo_C8xYz12AbCd_1.jpg
│   │   ├── natgeo_C8xYz12AbCd_2.jpg
│   │   └── natgeo_C8xYz12AbCd.txt     ← link, tác giả, chú thích
│   ├── Ẩm thực/
│   └── Chưa phân loại/
├── thumbs/                ← ảnh thu nhỏ cho web
├── instasaved.db          ← cơ sở dữ liệu
└── ig_session             ← phiên đăng nhập Instagram (giữ bí mật)
```

## Cài đặt trên OMV (lần đầu)

**Yêu cầu:** OMV 6/7 đã cài Docker (plugin **omv-extras** → **Compose**) và `git` (`apt install git`).

1. Tạo shared folder, ví dụ `instagram` (Storage → Shared Folders). Ghi lại *Absolute Path*.
2. SSH vào mini PC rồi tải code:
   ```bash
   cd /srv/dev-disk-by-uuid-XXXX/appdata        # thư mục bất kỳ để chứa code
   git clone https://github.com/virtualman209/instasave.git
   cd instasave
   cp .env.example .env
   nano .env        # sửa APP_PASSWORD, DATA_PATH (đường dẫn ở bước 1), PORT
   ./update.sh
   ```
3. Mở `http://IP-mini-pc:8088`, đăng nhập bằng `admin` / mật khẩu đã đặt.

## Cập nhật

```bash
cd /srv/dev-disk-by-uuid-XXXX/appdata/instasave
./update.sh            # tải bản mới từ GitHub, build lại nếu có thay đổi
./update.sh --force    # build lại kể cả khi code không đổi (cập nhật instaloader khi Instagram đổi API)
```

File `.env` và toàn bộ dữ liệu trong `DATA_PATH` được giữ nguyên khi cập nhật.

## Đăng nhập Instagram (khuyên dùng cookie)

Instagram hay chặn đăng nhập bằng mật khẩu từ máy chủ lạ, nên cách ổn định nhất là dùng lại phiên trên trình duyệt:

1. Trên máy tính, đăng nhập instagram.com bằng Chrome/Edge.
2. Nhấn **F12** → tab **Network** → F5 tải lại trang.
3. Bấm request đầu tiên (`www.instagram.com`) → **Headers** → **Request Headers** → copy toàn bộ giá trị dòng `cookie:`.
4. Vào tab **Tài khoản & cài đặt** trên InstaSaved → dán → **Đăng nhập bằng cookie**.

(Cũng có thể dán JSON xuất từ extension *Cookie-Editor*, hoặc chỉ dán giá trị `sessionid`.)
Phiên thường dùng được vài tuần đến vài tháng. Khi hết hạn, nhật ký sẽ báo “Phiên đăng nhập hết hạn” → dán cookie mới.
Đừng bấm “Đăng xuất” trên trình duyệt sau khi copy cookie, vì làm vậy sẽ huỷ luôn phiên đó.

## Sử dụng

- **Lần đầu:** bấm **Đồng bộ ngay**. Nếu bạn đã lưu rất nhiều bài, lần đầu có thể mất vài giờ (mỗi bài nghỉ 4 giây cho an toàn). Có thể bấm **Dừng** rồi chạy tiếp lúc khác.
- **Các lần sau:** tự chạy theo chu kỳ. App dừng khi gặp 20 bài đã có liên tiếp, nên rất nhanh.
- **Quy tắc phân loại:** ví dụ `Hashtag: travel → Du lịch`, `Tài khoản: natgeo → Du lịch`, `Từ khoá: công thức → Ẩm thực`, `Loại: video → Video`. Sau khi thêm, bấm **Áp dụng lại cho tất cả bài** để chuyển các bài cũ (file sẽ được di chuyển sang thư mục mới).
- **Đổi danh mục thủ công:** bấm vào ảnh → nhập danh mục → **Đổi danh mục**. Bài đổi tay sẽ không bị quy tắc ghi đè (đánh dấu ✎). Để trả về tự động, xoá trống ô rồi bấm Đổi.
- **Bộ sưu tập Instagram:** nếu bạn đã chia Saved thành các Collection trên app Instagram, tên collection được dùng làm danh mục khi không có quy tắc nào khớp.
- **Xuất link:** nút **Xuất link (CSV)** xuất danh mục đang chọn (hoặc tất cả).

## Lưu ý

- Chỉ dùng cho tài khoản **của chính bạn**, để lưu trữ cá nhân. Việc tự động hoá nằm ngoài điều khoản của Instagram. Nếu tải quá nhanh, Instagram có thể tạm khoá hoặc yêu cầu xác minh. Hãy giữ độ trễ ≥ 3 giây và chu kỳ ≥ 3 giờ.
- Tính năng đọc Collection dùng API nội bộ của Instagram. Nếu Instagram đổi API, phần này sẽ được bỏ qua (có ghi trong Nhật ký), còn việc tải ảnh vẫn chạy bình thường.
- Nếu tải bị lỗi hàng loạt do Instagram thay đổi, chạy `./update.sh --force` để cập nhật thư viện instaloader.
- Đừng mở cổng web ra Internet. Nếu cần xem từ xa, hãy dùng Tailscale/WireGuard.
- Quyền file: container chạy bằng root. Nếu không xoá/sửa được file qua SMB, thêm `user: "1000:100"` vào compose (UID/GID của user OMV), rồi `chown -R 1000:100` thư mục dữ liệu.

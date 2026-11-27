# --- CẤU HÌNH API & TỦ ---
API_GET_URL = "http://10.0.108.10:99/api.smartcabinet/apiGet"
API_POST_URL = "http://10.0.108.10:99/api.smartcabinet/apiPost"
API_PUT_URL = "http://10.0.108.10:99/api.smartcabinet/apiPut"
API_TIMEOUT = 5               # giây
DEFAULT_CABINET_ID = "SmartCabinet1"

# --- CHẾ ĐỘ VẬN HÀNH ---
DEBUG_MODE = False            # True: bật phím test (1,2,3,4, Space, F5, Esc). LUÔN để False khi chạy thật!
SHOW_EXIT_BUTTON = True       # Nút EXIT trên header (kiosk thật nên để False)
ADMIN_CARD_CODES = []
# Cảm biến ô (S1..S30):
#   False (DEMO / thiếu cảm biến): ô trống chỉ hiện ĐỎ, KHÔNG chặn thao tác. Quét tem -> mượn/trả theo trạng thái DB.
#   True  (vận hành thật, đủ cảm biến): phải lấy/đặt thiết bị (ô đỏ) mới được quét; còn ô đỏ thì không kết thúc phiên.
# Thiết bị có "Sensor": null trong DB luôn được coi là không có cảm biến.
SENSOR_ENFORCE = False         # Mã thẻ admin (chuỗi), dùng để giải phóng phiên bị kẹt ô đỏ. VD: ["123456"]

# --- CẤU HÌNH THỜI GIAN PHIÊN (ms) ---
SESSION_TIMEOUT_MS = 30000    # Tự đóng phiên sau 30 giây không thao tác
PENDING_REMIND_MS = 10000     # Chu kỳ nhắc khi còn ô đỏ / cửa chưa đóng
UI_POLL_MS = 50               # Chu kỳ GUI lấy sự kiện từ các luồng phần cứng

# --- LƯU TRẠNG THÁI & NHẬT KÝ ---
PENDING_STATE_FILE = "pending_state.json"   # Lưu ô đỏ để không mất khi app khởi động lại
AUDIT_LOG_FILE = "audit_log.jsonl"          # Nhật ký sự kiện (mở phiên, bất thường, giao dịch lỗi...)

# --- CẤU HÌNH PHẦN CỨNG (GPIO & SERIAL ARDUINO) ---
WIEGAND_GPIO_0 = 23
WIEGAND_GPIO_1 = 24
WIEGAND_CHECK_PARITY = True   # Kiểm tra parity khung Wiegand 26-bit

# Nếu biết chính xác tên thiết bị UHF (xem bằng `python3 -m evdev.evtest`), điền vào đây để không bắt nhầm bàn phím thật
UHF_DEVICE_NAME = ""
# Từ khóa dò tìm, theo THỨ TỰ ƯU TIÊN ("keyboard"/"usb" để cuối vì dễ trùng bàn phím thật)
UHF_KEYWORDS = ["sycreader", "rfid", "reader", "barcode", "hid", "keyboard", "usb"]
UHF_RECONNECT_S = 3

SERIAL_PORT = "/dev/ttyACM0"  # Cổng USB Arduino Mega 2560 trên Raspberry Pi 4
SERIAL_BAUDRATE = 115200
SERIAL_RECONNECT_S = 3
ARDUINO_OPEN_CMD = "OPEN"
TOTAL_SENSORS = 30            # S1 -> S30

# --- CẤU HÌNH MÀU SẮC ---
BG_COLOR = "#f4f7fb"
WHITE = "#ffffff"
TEXT_MAIN = "#1e293b"
TEXT_LIGHT = "#64748b"
PRIMARY_BLUE = "#0f4a8a"
REFRESH_BG = "#0284c7"

BORROW_BTN_BG = "#2563eb"   # Xanh dương: phiên đang mở
RETURN_BTN_BG = "#059669"   # Xanh ngọc: thành công
CABINET_BTN_BG = "#4f46e5"  # Chàm tím cho nút chọn tủ
CANCEL_BTN_BG = "#64748b"   # Xám trung tính

# Màu trạng thái từng ô thiết bị
AVAIL_BG = "#dcfce7"        # Xanh lá nhạt: Sẵn sàng trong tủ
AVAIL_FG = "#166534"

IN_USE_BG = "#94a3b8"       # Xám: Đang được mượn
IN_USE_FG = "#ffffff"

PENDING_BG = "#ef4444"      # Đỏ: Vừa lấy/đặt thiết bị, chờ quét UHF RFID xác thực
PENDING_FG = "#ffffff"

BORROW_BG = "#fee2e2"
BORROW_FG = "#991b1b"
OVERDUE_BG = "#ffedd5"
OVERDUE_FG = "#9a3412"
EXIT_BG = "#ef4444"
WARN_BG = "#d97706"         # Cam: cảnh báo (giao dịch ghi thiếu lịch sử...)

# --- MÀU NỔI BẬT CHO Ô NOTICE ---
NOTICE_BG = "#fef3c7"
NOTICE_BORDER = "#f59e0b"
NOTICE_TITLE_FG = "#b45309"
NOTICE_TEXT_FG = "#78350f"

# --- CẤU HÌNH FONT CHỮ ---
# "Segoe UI" không có trên Raspberry Pi OS -> dùng DejaVu Sans (có sẵn). Trên Windows có thể đổi lại "Segoe UI".
FONT_FAMILY = "DejaVu Sans"
FONT_MAIN = (FONT_FAMILY, 10)
FONT_BOLD = (FONT_FAMILY, 10, "bold")
FONT_TITLE = (FONT_FAMILY, 16, "bold")
FONT_HEADER = (FONT_FAMILY, 12, "bold")
FONT_BTN_LARGE = (FONT_FAMILY, 13, "bold")
FONT_NOTICE_TITLE = (FONT_FAMILY, 11, "bold")
FONT_NOTICE_TEXT = (FONT_FAMILY, 10, "bold")

# --- BỐ CỤC LƯỚI THIẾT BỊ ---
GRID_COLUMNS = 5              # Số ô mỗi hàng (10 cột không vừa màn 1024px khi có danh sách mượn bên phải)

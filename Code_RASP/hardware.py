import re
import threading
import time

import pigpio
import evdev
import serial
import serial.tools.list_ports
from evdev import ecodes

from config import (UHF_KEYWORDS, UHF_DEVICE_NAME, UHF_RECONNECT_S,
                    SERIAL_PORT, SERIAL_BAUDRATE, SERIAL_RECONNECT_S)


def _safe_call(fn, *args):
    """Gọi callback mà không để lỗi phía GUI làm chết luồng phần cứng."""
    if not fn:
        return
    try:
        fn(*args)
    except Exception as e:
        print(f"[CALLBACK ERROR] {getattr(fn, '__name__', fn)}: {e}")


# =====================================================================
class WiegandReader:
    """Đầu đọc thẻ nhân viên Wiegand 26-bit qua GPIO (pigpio)."""

    def __init__(self, pi, gpio_0, gpio_1, callback, bit_timeout=50, check_parity=True):
        self.pi = pi
        self.gpio_0 = gpio_0
        self.gpio_1 = gpio_1
        self.callback = callback
        self.bit_timeout = bit_timeout
        self.check_parity = check_parity

        self.in_code = False
        self.bits = 0
        self.num_bits = 0

        for g in (gpio_0, gpio_1):
            self.pi.set_mode(g, pigpio.INPUT)
            self.pi.set_pull_up_down(g, pigpio.PUD_UP)

        self.cb_0 = self.pi.callback(gpio_0, pigpio.FALLING_EDGE, self._cb)
        self.cb_1 = self.pi.callback(gpio_1, pigpio.FALLING_EDGE, self._cb)

    def _set_watchdog(self, ms):
        self.pi.set_watchdog(self.gpio_0, ms)
        self.pi.set_watchdog(self.gpio_1, ms)

    def _cb(self, gpio, level, tick):
        if level == 0:  # cạnh xuống = 1 bit
            if not self.in_code:
                self.in_code = True
                self.bits = 0
                self.num_bits = 0
            self.bits = (self.bits << 1) | (1 if gpio == self.gpio_1 else 0)
            self.num_bits += 1
            self._set_watchdog(self.bit_timeout)

        elif level == pigpio.TIMEOUT and self.in_code:  # hết bit -> kết thúc khung
            self.in_code = False
            self._set_watchdog(0)
            bits, num_bits = self.bits, self.num_bits
            self.bits = 0
            self.num_bits = 0
            self._decode(bits, num_bits)

    def _decode(self, bits, num_bits):
        if num_bits != 26:
            print(f"[Wiegand] Bỏ qua khung {num_bits} bit (chỉ hỗ trợ 26-bit) - có thể do nhiễu")
            return
        if self.check_parity and not self._parity_ok_26(bits):
            print("[Wiegand] Sai parity, bỏ qua khung")
            return
        card_number = (bits >> 1) & 0xFFFF
        print(f"[Wiegand] Mã nhân viên nhận được: {card_number}")
        _safe_call(self.callback, str(card_number))

    @staticmethod
    def _parity_ok_26(bits):
        """H10301: bit đầu = parity CHẴN của 12 bit dữ liệu đầu; bit cuối = parity LẺ của 12 bit dữ liệu cuối."""
        first = (bits >> 25) & 1
        last = bits & 1
        upper = (bits >> 13) & 0xFFF
        lower = (bits >> 1) & 0xFFF
        even_ok = (bin(upper).count("1") + first) % 2 == 0
        odd_ok = (bin(lower).count("1") + last) % 2 == 1
        return even_ok and odd_ok

    def cleanup(self):
        try:
            self._set_watchdog(0)
        except Exception:
            pass
        self.cb_0.cancel()
        self.cb_1.cancel()


# =====================================================================
SENSOR_RE = re.compile(r"\b(S\d{1,2})\s*=\s*([01])\b")
DOOR_RE = re.compile(r"\bDOOR\s*=\s*(OPEN|CLOSED)\b")

# VID USB: Arduino (2341, 2A03), CH340/CH341 (1A86), FTDI (0403), CP210x (10C4)
ARDUINO_VIDS = {0x2341, 0x2A03, 0x1A86, 0x0403, 0x10C4}
ARDUINO_DESC_KEYWORDS = ("arduino", "mega", "ch340", "ch341", "usb2.0-serial", "usb serial", "ft232", "cp210")

KEY_MAP = {f"KEY_{c}": c for c in "0123456789ABCDEF"}
KEY_MAP.update({f"KEY_KP{d}": d for d in "0123456789"})
MAX_CODE_LEN = 64


class UHFListener:
    """
    Đọc đầu đọc UHF/thẻ dạng HID-keyboard qua evdev.
    - grab() thiết bị để phím KHÔNG lọt vào cửa sổ Tk (tránh kích hoạt phím tắt).
    - Tự kết nối lại khi rút/cắm hoặc lỗi.
    """

    def __init__(self, on_tag_scanned, on_employee_scanned=None, on_status_change=None,
                 device_name=UHF_DEVICE_NAME, keywords=UHF_KEYWORDS, reconnect_s=UHF_RECONNECT_S):
        self.on_tag_scanned = on_tag_scanned
        self.on_employee_scanned = on_employee_scanned
        self.on_status_change = on_status_change
        self.device_name = (device_name or "").lower()
        self.keywords = [k.lower() for k in keywords]
        self.reconnect_s = reconnect_s
        self._running = False
        self._dev = None
        self._last_status = None

    def start(self):
        self._running = True
        threading.Thread(target=self._worker, daemon=True).start()

    def stop(self):
        self._running = False
        self._close_device(self._dev)

    def _notify_status(self, connected, detail=""):
        status = (connected, detail)
        if status == self._last_status:
            return
        self._last_status = status
        print(f"[UHF] {'Online' if connected else 'Offline'}: {detail}")
        _safe_call(self.on_status_change, connected, detail)

    def _dispatch_code(self, code):
        code = code.strip().upper()
        if not code:
            return
        if self.on_employee_scanned is None:  # chế độ khuyên dùng: GUI tự phân loại
            print(f"📡 [UHF] Mã quét: {code}")
            _safe_call(self.on_tag_scanned, code)
            return
        # Chế độ cũ: phân loại theo định dạng (sai khi tem thiết bị toàn chữ số)
        if len(code) <= 12 and code.isdigit():
            print(f"🪪 [UHF-EMP] Quét thẻ nhân viên: {code}")
            _safe_call(self.on_employee_scanned, code)
        else:
            print(f"🎯 [UHF-TAG] Quét tem thiết bị: {code}")
            _safe_call(self.on_tag_scanned, code)

    def _find_device(self):
        devices = []
        for path in evdev.list_devices():
            try:
                devices.append(evdev.InputDevice(path))
            except OSError:
                pass

        chosen = None
        if self.device_name:
            chosen = next((d for d in devices if self.device_name in d.name.lower()), None)
        else:
            for kw in self.keywords:  # duyệt theo thứ tự ưu tiên
                chosen = next((d for d in devices if kw in d.name.lower()), None)
                if chosen:
                    if kw in ("keyboard", "usb"):
                        print(f"⚠️ [UHF] Chọn '{chosen.name}' theo từ khóa chung '{kw}'. "
                              f"Nên đặt UHF_DEVICE_NAME trong config.py để tránh bắt nhầm bàn phím thật.")
                    break

        for d in devices:
            if d is not chosen:
                d.close()
        return chosen

    @staticmethod
    def _key_name(code):
        name = ecodes.KEY.get(code)
        if isinstance(name, (list, tuple)):  # một số mã có nhiều tên alias
            name = name[0] if name else None
        return name

    @staticmethod
    def _close_device(dev):
        if dev is None:
            return
        try:
            dev.ungrab()
        except Exception:
            pass
        try:
            dev.close()
        except Exception:
            pass

    def _worker(self):
        while self._running:
            dev = None
            try:
                dev = self._find_device()
                if dev is None:
                    self._notify_status(False, "Not Found")
                    time.sleep(self.reconnect_s)
                    continue

                try:
                    dev.grab()
                except OSError as e:
                    print(f"⚠️ [UHF] Không grab được thiết bị ({e}). Phím quét có thể lọt vào giao diện!")

                self._dev = dev
                self._notify_status(True, dev.name)
                card_data = ""

                for event in dev.read_loop():
                    if not self._running:
                        break
                    if event.type != ecodes.EV_KEY or event.value != 1:
                        continue
                    key = self._key_name(event.code)
                    if key in ("KEY_ENTER", "KEY_KPENTER"):
                        if card_data:
                            self._dispatch_code(card_data)
                        card_data = ""
                    elif key in KEY_MAP:
                        card_data += KEY_MAP[key]
                        if len(card_data) > MAX_CODE_LEN:  # rác / thiếu Enter
                            card_data = ""

            except PermissionError:
                print("❌ [LỖI QUYỀN] evdev bị chặn. Chạy bằng sudo hoặc thêm user vào nhóm 'input'.")
                self._notify_status(False, "Permission Denied")
            except OSError as e:
                if self._running:
                    print(f"❌ [UHF] Mất kết nối: {e}")
                    self._notify_status(False, "Disconnected")
            except Exception as e:
                print(f"❌ [LỖI UHF]: {e}")
                self._notify_status(False, "Error")
            finally:
                self._dev = None
                self._close_device(dev)

            if self._running:
                time.sleep(self.reconnect_s)


# =====================================================================
class ArduinoSerialBridge:
    """
    Giao tiếp Serial 2 chiều với Arduino Mega 2560 (30 cảm biến S1..S30 + khóa tủ + cảm biến cửa)
    - Gửi: "OPEN\\n"
    - Nhận: "S1=0", "S2=1", ..., "DOOR=OPEN", "DOOR=CLOSED"
    """

    def __init__(self, on_sensor_change=None, on_door_change=None, on_status_change=None):
        self.on_sensor_change = on_sensor_change
        self.on_door_change = on_door_change
        self.on_status_change = on_status_change
        self.ser = None
        self.running = False
        self._lock = threading.Lock()
        self._last_status = None
        self._last_error = None

    def start(self):
        self.running = True
        threading.Thread(target=self._read_worker, daemon=True).start()

    def stop(self):
        self.running = False
        with self._lock:
            ser, self.ser = self.ser, None
        self._close(ser)

    def send_command(self, cmd):
        with self._lock:
            if self.ser and self.ser.is_open:
                try:
                    self.ser.write(f"{cmd.strip()}\n".encode("utf-8"))
                    self.ser.flush()
                    print(f"📤 [ARDUINO TX] {cmd.strip()}")
                    return True
                except Exception as e:
                    print(f"❌ [ARDUINO TX ERROR] {e}")
            else:
                print(f"⚠️ [ARDUINO SIMULATED TX] {cmd.strip()} (Chưa kết nối cổng Serial)")
        return False

    def _notify_status(self, connected, detail):
        if (connected, detail) == self._last_status:
            return
        self._last_status = (connected, detail)
        _safe_call(self.on_status_change, connected, detail)

    @staticmethod
    def _close(ser):
        if ser is None:
            return
        try:
            ser.close()
        except Exception:
            pass

    @staticmethod
    def _candidate_ports():
        """
        Danh sách cổng có thể là Arduino, theo thứ tự ưu tiên:
        1. SERIAL_PORT trong config (nếu tồn tại)
        2. Cổng có VID của Arduino / chip USB-serial phổ biến (CH340, FTDI, CP210x) hoặc mô tả khớp
        3. Mọi /dev/ttyACM* rồi /dev/ttyUSB* (Mega bản clone CH340 hiện là ttyUSB0)
        Bỏ qua UART nội của Pi (ttyAMA*, ttyS*).
        """
        ports = [p for p in serial.tools.list_ports.comports()
                 if "ttyAMA" not in p.device and "ttyS" not in p.device]

        def rank(p):
            info = f"{p.description or ''} {p.manufacturer or ''}".lower()
            if SERIAL_PORT and p.device == SERIAL_PORT:
                return 0
            if (p.vid in ARDUINO_VIDS) or any(k in info for k in ARDUINO_DESC_KEYWORDS):
                return 1
            if "ttyACM" in p.device:
                return 2
            if "ttyUSB" in p.device:
                return 3
            return 4

        return [p.device for p in sorted(ports, key=lambda p: (rank(p), p.device))]

    @staticmethod
    def describe_ports():
        lines = []
        for p in serial.tools.list_ports.comports():
            vid = f"{p.vid:04X}:{p.pid:04X}" if p.vid is not None else "----:----"
            lines.append(f"   {p.device}  [{vid}]  {p.description or ''}")
        return "\n".join(lines) or "   (không có cổng serial nào)"

    def _handle_line(self, line):
        """
        Hỗ trợ cả 2 kiểu Arduino gửi:
          - mỗi dòng 1 ô     : "S1=0"
          - cả tủ trên 1 dòng: "S1=1,S2=1,S3=1,...,S30=0"  (phân cách bằng , ; hoặc khoảng trắng)
        1 = ô CÓ thiết bị, 0 = thiết bị ĐÃ LẤY ra.
        """
        for sensor_name, value in SENSOR_RE.findall(line):
            _safe_call(self.on_sensor_change, sensor_name, int(value))
        for state in DOOR_RE.findall(line):
            _safe_call(self.on_door_change, state == "OPEN")

    def _open_any_port(self):
        """Thử lần lượt các cổng ứng viên, trả về (serial, port) hoặc (None, lý do lỗi)."""
        candidates = self._candidate_ports()
        if not candidates:
            return None, "Không tìm thấy cổng USB-serial nào"
        errors = []
        for port in candidates:
            try:
                return serial.Serial(port, SERIAL_BAUDRATE, timeout=1), port
            except Exception as e:
                errors.append(f"{port}: {e}")
        return None, " | ".join(errors)

    def _log_once(self, msg):
        if msg != self._last_error:
            self._last_error = msg
            print(msg)

    def _read_worker(self):
        while self.running:
            ser = None
            try:
                ser, port_or_err = self._open_any_port()
                if ser is None:
                    self._log_once(f"❌ [ARDUINO] {port_or_err}. Các cổng hiện có:\n{self.describe_ports()}\n"
                                   f"   -> Kiểm tra cáp USB; nếu lỗi 'Permission denied' chạy: "
                                   f"sudo usermod -aG dialout $USER (rồi đăng nhập lại)")
                    self._notify_status(False, "Not Found")
                else:
                    port = port_or_err
                    time.sleep(1.5)  # Mega tự reset khi mở cổng
                    ser.reset_input_buffer()
                    with self._lock:
                        self.ser = ser
                    self._last_error = None
                    print(f"✅ [ARDUINO] Đã kết nối Serial tại {port}")
                    self._notify_status(True, port)

                    while self.running and ser.is_open:
                        raw_line = ser.readline()
                        if not raw_line:
                            continue
                        line = raw_line.decode("utf-8", errors="ignore").strip().upper()
                        if line:
                            print(f"📥 [ARDUINO RX] {line}")
                            self._handle_line(line)

            except Exception as e:
                if self.running:
                    self._log_once(f"❌ [ARDUINO] Mất kết nối: {e}")
                    self._notify_status(False, str(e))
            finally:
                with self._lock:
                    if self.ser is ser:
                        self.ser = None
                self._close(ser)  # tránh rò rỉ file descriptor khi kết nối lại

            if self.running:
                time.sleep(SERIAL_RECONNECT_S)

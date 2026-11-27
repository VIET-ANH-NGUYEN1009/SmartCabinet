import queue
import threading
import traceback
import tkinter as tk
from tkinter import ttk
from datetime import datetime

from config import *
import api_service
import storage

ACTION_BORROW = "BORROW"
ACTION_RETURN = "RETURN"
ACTION_LABEL = {ACTION_BORROW: "MƯỢN", ACTION_RETURN: "TRẢ"}


def _sensor_sort_key(code):
    code = str(code or "")
    digits = "".join(ch for ch in code if ch.isdigit())
    return (int(digits) if digits else 9999, code)


def _fmt_time(raw):
    raw = "" if raw is None else str(raw)
    if "T" in raw:
        date_p, time_p = raw.split("T", 1)
        return f"{date_p[-5:].replace('-', '/')} {time_p[:5]}"
    return raw


class SmartCabinetApp:
    """
    Luồng trạng thái:
    1. SELECT_CABINET : Chọn tủ (tự bỏ qua nếu hệ thống chỉ có 1 tủ)
    2. SCAN_CARD      : Chờ quét thẻ nhân viên -> xác thực -> gửi "OPEN" cho Arduino
    3. DOOR_OPEN      : Phiên mở. Hành động xác định THEO TỪNG Ô, không theo cả phiên:
                          - Ô có thiết bị "available" chuyển Sx=0 (lấy ra)  -> ĐỎ, chờ quét để MƯỢN
                          - Ô có thiết bị "borrowed"  chuyển Sx=1 (đặt vào) -> ĐỎ, chờ quét để TRẢ
                          - Đặt lại / lấy lại ngay  -> hủy trạng thái đỏ
                        Quét UHF chỉ được chấp nhận khi ô của thiết bị đó đang ĐỎ (đã thực sự lấy/đặt).
                        Phiên chỉ kết thúc khi không còn ô đỏ (hoặc thẻ admin giải phóng, có ghi log).

    Mọi sự kiện từ luồng phần cứng/API được đưa vào hàng đợi và xử lý trên luồng Tk chính.
    """
    STATE_SELECT_CABINET = "SELECT_CABINET"
    STATE_SCAN_CARD = "SCAN_CARD"
    STATE_DOOR_OPEN = "DOOR_OPEN"

    def __init__(self, root):
        self.root = root
        self.root.title("Smart Cabinet")
        self.root.attributes("-fullscreen", True)
        self.root.geometry("1024x600")
        self.root.configure(bg=BG_COLOR)

        self._ui_queue = queue.Queue()
        self._admin_cards = {str(c).strip().upper() for c in ADMIN_CARD_CODES if str(c).strip()}

        # --- Dữ liệu ---
        self.cabinets_list = []
        self.selected_cabinet = DEFAULT_CABINET_ID
        self.items_data = []
        self.lending_data = []
        self.api_error_msg = ""
        self.api_warning_msg = ""
        self.cards = {}         # RFID tag -> card
        self.sensor_cards = {}  # Sx -> card
        self.summary_vars = {}

        # --- Phiên ---
        self.workflow_state = self.STATE_SCAN_CARD
        self.current_user = None
        self.user_borrowed_count = 0
        self.pending = {}        # Sx -> ACTION_BORROW / ACTION_RETURN (trong phiên, chờ quét UHF)
        self.anomalies = set()   # Sx lệch với DB khi CHƯA có phiên (sẽ thành pending khi mở phiên)
        self._unassigned_logged = set()
        self.sensor_state = {}   # Sx -> 0/1 (giá trị mới nhất từ Arduino: 1 = có thiết bị, 0 = đã lấy)
        self.session_timer = None
        self.banner_timer = None
        self.processing_uhf = False
        self.verifying_card = False
        self.loading = False
        self._pending_timeout_logged = False
        self.lbl_door_instruction = None

        self.send_arduino_cmd = None

        # --- Trạng thái thiết bị ---
        self.rfid_connected = False
        self.wiegand_connected = False
        self.arduino_connected = False
        self.api_connected = False
        self.door_open = None    # None = chưa biết (chỉ cập nhật từ tín hiệu DOOR= của Arduino)

        self.setup_ui()
        self.setup_bindings()
        self._restore_pending_state()
        self._poll_ui_queue()
        self.refresh_api_data(reset_state=True)

    # ================================================================ hàng đợi luồng
    def _post(self, fn, *args):
        """Thread-safe: các luồng nền chỉ được gọi hàm này, không chạm trực tiếp vào Tk / dữ liệu."""
        self._ui_queue.put((fn, args))

    def _poll_ui_queue(self):
        try:
            while True:
                fn, args = self._ui_queue.get_nowait()
                try:
                    fn(*args)
                except Exception:
                    print(f"[UI ERROR] {getattr(fn, '__name__', fn)}")
                    traceback.print_exc()
        except queue.Empty:
            pass
        self.root.after(UI_POLL_MS, self._poll_ui_queue)

    # ================================================================ API cho luồng phần cứng
    def set_arduino_sender(self, sender_func):
        self.send_arduino_cmd = sender_func

    def set_rfid_status(self, connected, detail=""):
        self._post(self._apply_device_status, "rfid", connected)

    def set_wiegand_status(self, connected):
        self._post(self._apply_device_status, "wiegand", connected)

    def set_arduino_status(self, connected, detail=""):
        self._post(self._apply_device_status, "arduino", connected)

    def handle_employee_scan(self, card_code):
        self._post(self._on_employee_card, str(card_code).strip().upper())

    def handle_sensor_change(self, sensor_name, value):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return
        self._post(self._on_sensor, str(sensor_name).strip().upper(), value)

    def handle_door_change(self, is_open):
        self._post(self._on_door, bool(is_open))

    def handle_uhf_scan(self, uhf_id):
        self._post(self._on_uhf, str(uhf_id).strip().upper())

    def handle_uhf_code(self, code):
        """Mã bất kỳ từ đầu đọc UHF (chưa phân loại). GUI tự quyết định là tem thiết bị hay thẻ nhân viên."""
        self._post(self._on_uhf_code, str(code).strip().upper())

    # ================================================================ phân loại mã từ đầu đọc UHF
    def _find_card_by_tag(self, code):
        """Tìm thiết bị theo tag; chấp nhận khác biệt số 0 ở đầu (VD 05617626 <-> 5617626)."""
        card = self.cards.get(code)
        if card is not None:
            return card
        stripped = code.lstrip("0")
        if not stripped:
            return None
        for tag, c in self.cards.items():
            if tag.lstrip("0") == stripped:
                return c
        return None

    def _on_uhf_code(self, code):
        """
        Thứ tự phân loại:
        1. Trùng tem thiết bị trong tủ đang chọn         -> TEM THIẾT BỊ
        2. Đang mở phiên (trừ thẻ admin)                 -> coi là TEM THIẾT BỊ (báo "không tìm thấy" nếu lạ)
        3. Toàn chữ số, <= 12 ký tự                      -> THẺ NHÂN VIÊN
        4. Còn lại                                       -> tem thiết bị lạ
        """
        if not code:
            return

        card = self._find_card_by_tag(code)
        if card is not None:
            print(f"🎯 [UHF] Tem thiết bị: {code} -> {card['data'].get('name')}")
            self._on_uhf(card["data"]["id"])
            return

        if self.workflow_state == self.STATE_DOOR_OPEN and code not in self._admin_cards:
            print(f"🎯 [UHF] Mã lạ trong phiên, xử lý như tem thiết bị: {code}")
            self._on_uhf(code)
            return

        if code.isdigit() and len(code) <= 12:
            print(f"🪪 [UHF] Thẻ nhân viên: {code}")
            self._on_employee_card(code)
            return

        print(f"🎯 [UHF] Tem không thuộc tủ {self.selected_cabinet}: {code}")
        self._on_uhf(code)

    # ================================================================ UI khung
    def setup_ui(self):
        self.main_container = tk.Frame(self.root, bg=BG_COLOR, padx=10, pady=10)
        self.main_container.pack(fill=tk.BOTH, expand=True)

        self.create_header()

        self.middle_frame = tk.Frame(self.main_container, bg=BG_COLOR)
        self.middle_frame.pack(fill=tk.BOTH, expand=True, pady=10)

        self.left_panel = tk.Frame(self.middle_frame, bg=BG_COLOR)
        self.left_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))

        self.right_panel = tk.Frame(self.middle_frame, bg=WHITE, width=470)
        self.right_panel.pack(side=tk.RIGHT, fill=tk.Y)
        self.right_panel.pack_propagate(False)

        self.create_bottom_panel()

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview.Heading", font=(FONT_FAMILY, 9, "bold"), background="#f1f5f9")
        style.configure("Treeview", font=(FONT_FAMILY, 9), rowheight=25)

    def _apply_device_status(self, which, connected):
        setattr(self, f"{which}_connected", bool(connected))
        if which == "arduino" and not connected:
            self.door_open = None
        self.update_system_status_ui()

    def update_system_status_ui(self):
        if self.rfid_connected and self.wiegand_connected:
            self.lbl_rfid_status.config(text="🟢 RFID & Wiegand: Online", fg=AVAIL_FG)
        elif self.rfid_connected or self.wiegand_connected:
            part = "UHF Only" if self.rfid_connected else "Wiegand Only"
            self.lbl_rfid_status.config(text=f"🟡 Reader: {part}", fg=NOTICE_TITLE_FG)
        else:
            self.lbl_rfid_status.config(text="🔴 RFID Reader: Offline", fg=BORROW_FG)

        mega = "OK" if self.arduino_connected else "Offline"
        if self.door_open is None:
            self.lbl_door_status.config(text=f"⚪ Door: Unknown | Mega: {mega}", fg=TEXT_LIGHT)
        elif self.door_open:
            self.lbl_door_status.config(text=f"🟠 Door: Open | Mega: {mega}", fg=NOTICE_TITLE_FG)
        else:
            self.lbl_door_status.config(text=f"🟢 Door: Closed | Mega: {mega}", fg=AVAIL_FG)

        if self.api_connected:
            self.lbl_api_status.config(text="🟢 Server API: Connected", fg=AVAIL_FG)
        else:
            self.lbl_api_status.config(text="🔴 Server API: Disconnected", fg=BORROW_FG)

    # ================================================================ tải dữ liệu
    def refresh_api_data(self, reset_state=False):
        """reset_state=True: sau khi tải xong đưa về màn chờ (chỉ khi KHÔNG có phiên đang mở)."""
        if self.loading:
            return
        self.loading = True
        self.btn_refresh.config(text="↻ LOADING...", state=tk.DISABLED)
        allow_switch = self.workflow_state != self.STATE_DOOR_OPEN
        threading.Thread(
            target=self._fetch_worker,
            args=(self.selected_cabinet, allow_switch, reset_state),
            daemon=True
        ).start()

    def _fetch_worker(self, cabinet, allow_switch, reset_state):
        result = {"cabinets": None, "cabinet": cabinet, "items": [], "lending": [], "error": "", "warning": ""}
        try:
            cabinets, err_cab = api_service.fetch_cabinets()
            if err_cab:
                result["error"] = err_cab
            else:
                result["cabinets"] = cabinets
                names = [c.get("Cabinet") for c in cabinets if c.get("Cabinet")]
                if allow_switch and names and cabinet not in names:
                    cabinet = names[0]
                    result["cabinet"] = cabinet
                items, lending, err, warn = api_service.fetch_cabinet_data(cabinet)
                result.update(items=items, lending=lending, error=err, warning=warn)
        except Exception as e:
            traceback.print_exc()
            result["error"] = f"Lỗi hệ thống không xác định: {e}"
        self._post(self._on_fetch_complete, result, reset_state)

    def _on_fetch_complete(self, result, reset_state):
        self.loading = False
        self.btn_refresh.config(text="↻ REFRESH", state=tk.NORMAL)
        in_session = self.workflow_state == self.STATE_DOOR_OPEN

        if result["error"]:
            self.api_connected = False
            if in_session and self.items_data:
                # Đang có phiên: giữ dữ liệu cũ để không làm hỏng phiên
                self.update_system_status_ui()
                self._show_banner(f"⚠️ Không tải lại được dữ liệu: {result['error']}\n(Tiếp tục dùng dữ liệu cũ)",
                                  NOTICE_TITLE_FG, 3000)
                return
            self.api_error_msg = result["error"]
            self.api_warning_msg = ""
            self.items_data = []
            self.lending_data = []
            if result["cabinets"] is not None:
                self.cabinets_list = result["cabinets"]
        else:
            self.api_connected = True
            self.api_error_msg = ""
            self.api_warning_msg = result["warning"]
            self.cabinets_list = result["cabinets"] or []
            self.selected_cabinet = result["cabinet"]
            self.items_data = result["items"]
            self.lending_data = result["lending"]
            if in_session and self.current_user:
                self.user_borrowed_count = self._count_user_borrowed(self.current_user.get("code", ""))

        self.lbl_cabinet_subtitle.config(
            text=f"Cabinet: {self.selected_cabinet}  |  Auto Borrow / Return"
        )
        self.update_system_status_ui()
        self.rebuild_device_and_lending_ui()

        if reset_state and not in_session:
            self._go_idle()
        else:
            self.render_action_panel()

    # ================================================================ phiên làm việc
    def _cancel_session_timer(self):
        if self.session_timer is not None:
            try:
                self.root.after_cancel(self.session_timer)
            except Exception:
                pass
            self.session_timer = None

    def _reset_session_timer(self, timeout_ms=SESSION_TIMEOUT_MS):
        """Luôn đặt lại timer (kể cả khi đang có ô đỏ) để phiên không bao giờ 'mất' timer."""
        self._cancel_session_timer()
        self.session_timer = self.root.after(timeout_ms, self._on_session_timeout)

    def _on_session_timeout(self):
        self.session_timer = None
        if self.workflow_state != self.STATE_DOOR_OPEN:
            self._go_idle()
            return

        if self.pending and SENSOR_ENFORCE:
            pending_str = self._pending_text()
            if not self._pending_timeout_logged:
                self._pending_timeout_logged = True
                storage.audit("PENDING_TIMEOUT", cabinet=self.selected_cabinet,
                              user=self.current_user, pending=dict(self.pending))
            self._show_banner(
                f"🚨 Ô [{pending_str}] đang MÀU ĐỎ!\nVui lòng quét UHF RFID thiết bị trước khi kết thúc!",
                PENDING_BG, 3500)
            self._reset_session_timer(PENDING_REMIND_MS)
            return

        if self.door_open:  # Arduino xác nhận cửa vẫn mở -> giữ phiên, nhắc đóng cửa
            self._show_banner("🚪 Vui lòng ĐÓNG CỬA TỦ để kết thúc phiên.", NOTICE_TITLE_FG, 3000)
            self._reset_session_timer(PENDING_REMIND_MS)
            return

        self.end_session("timeout")

    def _go_idle(self):
        self._cancel_session_timer()
        valid = [c for c in self.cabinets_list if c.get("Cabinet")]
        if len(valid) > 1:
            self.workflow_state = self.STATE_SELECT_CABINET
        else:
            self.workflow_state = self.STATE_SCAN_CARD
        self.render_action_panel()

    def request_finish(self):
        """Nút HOÀN TẤT."""
        if self.workflow_state != self.STATE_DOOR_OPEN:
            self._go_idle()
            return
        if self.pending and SENSOR_ENFORCE:
            self._show_banner(
                f"🚨 Ô [{self._pending_text()}] đang MÀU ĐỎ!\nVui lòng quét UHF RFID thiết bị trước khi kết thúc!",
                PENDING_BG, 3500)
            return
        self.end_session("user_finish")

    def end_session(self, reason):
        if self.workflow_state == self.STATE_DOOR_OPEN:
            storage.audit("SESSION_END", reason=reason, cabinet=self.selected_cabinet,
                          user=(self.current_user or {}).get("code"))
        self._cancel_session_timer()
        self.current_user = None
        self.user_borrowed_count = 0
        self.pending.clear()
        self.processing_uhf = False
        self._pending_timeout_logged = False
        self._persist_pending()
        self.workflow_state = self.STATE_SCAN_CARD  # để reconcile tính theo chế độ ngoài phiên
        self._reconcile_sensors()                   # ô còn lệch (VD admin giải phóng) -> vẫn hiện đỏ
        self._repaint_all_cards()
        self.update_summary()
        self._go_idle()

    def on_select_cabinet(self, cabinet_name):
        self.selected_cabinet = cabinet_name
        self.workflow_state = self.STATE_SCAN_CARD
        self._reset_session_timer()
        self.render_action_panel()
        self.refresh_api_data(reset_state=False)

    def _count_user_borrowed(self, emp_code):
        emp = str(emp_code).strip().upper()
        if not emp:
            return 0
        borrowed = set()
        for rec in self.lending_data:
            if str(rec.get("LENDING_Requester") or "").strip().upper() == emp:
                borrowed.add(str(rec.get("controlNo") or rec.get("RFID_codes") or id(rec)))
        for item in self.items_data:
            raw = item.get("raw_data", {}) or {}
            if item.get("status") == "borrowed" and str(raw.get("LENDING_Requester") or "").strip().upper() == emp:
                borrowed.add(item.get("name"))
        return len(borrowed)

    # ---------------------------------------------------------------- lưu / khôi phục ô đỏ
    def _persist_pending(self):
        if SENSOR_ENFORCE and self.workflow_state == self.STATE_DOOR_OPEN and self.pending and self.current_user:
            storage.save_pending_state({
                "cabinet": self.selected_cabinet,
                "user": self.current_user,
                "pending": self.pending,
                "saved_at": datetime.now().isoformat(timespec="seconds"),
            })
        else:
            storage.save_pending_state(None)

    def _restore_pending_state(self):
        state = storage.load_pending_state()
        if not state:
            return
        if not SENSOR_ENFORCE:
            storage.save_pending_state(None)
            return
        pending = {str(k).upper(): v for k, v in state["pending"].items() if v in (ACTION_BORROW, ACTION_RETURN)}
        if not pending:
            storage.save_pending_state(None)
            return
        self.selected_cabinet = state.get("cabinet") or DEFAULT_CABINET_ID
        self.current_user = state["user"]
        self.pending = pending
        self.workflow_state = self.STATE_DOOR_OPEN
        storage.audit("PENDING_RESTORED", cabinet=self.selected_cabinet, user=self.current_user, pending=pending)
        self._reset_session_timer(PENDING_REMIND_MS)

    # ================================================================ thẻ nhân viên
    def _on_employee_card(self, card_code):
        if not card_code:
            return

        if card_code in self._admin_cards and self.workflow_state == self.STATE_DOOR_OPEN and self.pending:
            self._admin_force_release(card_code)
            return

        if self.workflow_state == self.STATE_DOOR_OPEN:
            self._show_banner("⚠️ Tủ đang mở phiên làm việc! Vui lòng hoàn tất quét thiết bị.", NOTICE_TITLE_FG, 2200)
            return
        if self.workflow_state == self.STATE_SELECT_CABINET:
            self._show_banner("⚠️ Vui lòng CHỌN TỦ trước khi quét thẻ.", NOTICE_TITLE_FG, 2200)
            return
        if self.api_error_msg:
            self._show_banner(f"⚠️ Hệ thống đang lỗi, chưa thể mở tủ.\n{self.api_error_msg}", EXIT_BG, 3000)
            return
        if self.verifying_card or self.loading:
            return

        self.verifying_card = True
        self.render_action_panel()
        threading.Thread(target=self._verify_worker, args=(card_code,), daemon=True).start()

    def _verify_worker(self, card_code):
        try:
            is_valid, result = api_service.verify_employee(card_code)
        except Exception as e:
            traceback.print_exc()
            is_valid, result = False, f"Lỗi hệ thống: {e}"
        self._post(self._on_verify_result, card_code, is_valid, result)

    def _on_verify_result(self, card_code, is_valid, result):
        self.verifying_card = False
        if self.workflow_state != self.STATE_SCAN_CARD:  # trạng thái đã đổi trong lúc chờ API
            self.render_action_panel()
            return
        if not is_valid:
            storage.audit("AUTH_DENIED", card=card_code, reason=result)
            self._show_banner(f"⚠️ TỪ CHỐI TRUY CẬP\nMã thẻ: {card_code} - {result}", EXIT_BG, 3000)
            return
        self._open_session(result)

    def _open_session(self, user_info):
        self.current_user = user_info
        self.user_borrowed_count = self._count_user_borrowed(user_info.get("code", ""))
        self.pending.clear()
        self._pending_timeout_logged = False
        self.workflow_state = self.STATE_DOOR_OPEN
        storage.audit("SESSION_OPEN", cabinet=self.selected_cabinet, user=user_info.get("code"),
                      name=user_info.get("name"))

        if self.send_arduino_cmd:
            self.send_arduino_cmd(ARDUINO_OPEN_CMD)

        # Ô nào đang lệch DB (đã trống từ trước) -> chuyển thành ô ĐỎ chờ quét trong phiên này
        self._reconcile_sensors()
        self.render_action_panel()
        self._reset_session_timer()

    def _admin_force_release(self, admin_card):
        storage.audit("ADMIN_FORCE_RELEASE", admin=admin_card, cabinet=self.selected_cabinet,
                      user=self.current_user, pending=dict(self.pending))
        released = self._pending_text()
        self.end_session("admin_force")
        self._show_banner(f"🛡️ Admin đã giải phóng phiên.\nCác ô [{released}] đã được ghi log để kiểm tra.",
                          CABINET_BTN_BG, 3500)
        self.refresh_api_data(reset_state=False)

    # ================================================================ cảm biến & cửa
    _UNKNOWN = object()

    def _evaluate_sensor(self, sensor):
        """
        So cảm biến với DB:
          - available + 0 (thiết bị đã bị lấy)   -> cần quét để MƯỢN
          - borrowed  + 1 (thiết bị đã đặt vào)  -> cần quét để TRẢ
          - khớp DB                              -> None
          - chưa có dữ liệu cảm biến / chưa gán  -> _UNKNOWN
        """
        card = self.sensor_cards.get(sensor)
        value = self.sensor_state.get(sensor)
        if card is None or value is None:
            return self._UNKNOWN
        status = card["data"].get("status", "available")
        if status == "available" and value == 0:
            return ACTION_BORROW
        if status == "borrowed" and value == 1:
            return ACTION_RETURN
        return None

    def _apply_sensor(self, sensor):
        """Cập nhật pending (trong phiên) hoặc anomalies (ngoài phiên) cho 1 ô. Trả về True nếu có thay đổi."""
        action = self._evaluate_sensor(sensor)
        if action is self._UNKNOWN:
            return False

        if self.workflow_state == self.STATE_DOOR_OPEN:
            changed = sensor in self.anomalies
            self.anomalies.discard(sensor)
            if action and self.pending.get(sensor) != action:
                self.pending[sensor] = action
                print(f"🔴 [{sensor}] -> ĐỎ, chờ quét UHF để {ACTION_LABEL[action]}")
                return True
            if not action and sensor in self.pending:
                del self.pending[sensor]
                print(f"↩️ [{sensor}] khớp lại DB -> hủy chờ quét")
                return True
            return changed

        if action and sensor not in self.anomalies:
            self.anomalies.add(sensor)
            return True
        if not action and sensor in self.anomalies:
            self.anomalies.discard(sensor)
            return True
        return False

    def _reconcile_sensors(self):
        """Tính lại toàn bộ ô từ trạng thái cảm biến mới nhất (sau khi tải DB, mở/đóng phiên)."""
        before = set(self.anomalies)
        changed = [s for s in list(self.sensor_cards) if self._apply_sensor(s)]
        for s in changed:
            self._paint_card(self.sensor_cards[s])
        if changed:
            self._persist_pending()
            self.update_summary()
            self.render_action_panel()
        self._announce_anomalies(self.anomalies - before)

    def _announce_anomalies(self, new_anomalies):
        if not new_anomalies or self.workflow_state == self.STATE_DOOR_OPEN:
            return
        names = ", ".join(f"{s} ({self.sensor_cards[s]['data'].get('name')})"
                          for s in sorted(new_anomalies, key=_sensor_sort_key))
        storage.audit("SENSOR_ANOMALY", sensors=sorted(new_anomalies), door_open=self.door_open,
                      detail={s: {"value": self.sensor_state.get(s),
                                  "db_status": self.sensor_cards[s]["data"].get("status")} for s in new_anomalies})
        if not SENSOR_ENFORCE:
            return
        self._show_banner(f"🚨 Ô [{names}] lệch với dữ liệu khi chưa mở phiên!\n"
                          f"Quét thẻ nhân viên rồi quét tem thiết bị để xác nhận.", PENDING_BG, 4000)

    def _on_sensor(self, sensor_name, value):
        if value not in (0, 1):
            return
        prev = self.sensor_state.get(sensor_name)
        self.sensor_state[sensor_name] = value
        if prev == value:      # Arduino gửi lại cả tủ liên tục -> chỉ xử lý ô THAY ĐỔI
            return

        card = self.sensor_cards.get(sensor_name)
        if not card:
            if sensor_name not in self._unassigned_logged:
                self._unassigned_logged.add(sensor_name)
                print(f"[SENSOR] {sensor_name} chưa gán thiết bị trong DB -> bỏ qua")
            return

        if self.workflow_state == self.STATE_DOOR_OPEN:
            self._reset_session_timer()  # có thao tác -> gia hạn phiên

        before = set(self.anomalies)
        if self._apply_sensor(sensor_name):
            self._paint_card(card)
            self._persist_pending()
            self.update_summary()
            self.render_action_panel()
        self._announce_anomalies(self.anomalies - before)

    def _on_door(self, is_open):
        self.door_open = is_open
        self.update_system_status_ui()

        if is_open and self.workflow_state != self.STATE_DOOR_OPEN:
            storage.audit("DOOR_OPEN_WITHOUT_SESSION", cabinet=self.selected_cabinet)
            self._show_banner("🚨 Cửa tủ MỞ khi chưa có phiên hợp lệ! Đã ghi log.", PENDING_BG, 3500)
            return

        if not is_open and self.workflow_state == self.STATE_DOOR_OPEN:
            if self.pending and SENSOR_ENFORCE:
                storage.audit("DOOR_CLOSED_WITH_PENDING", cabinet=self.selected_cabinet,
                              user=self.current_user, pending=dict(self.pending))
                self._show_banner(
                    f"🚨 Cửa đã đóng nhưng ô [{self._pending_text()}] CHƯA quét UHF!\n"
                    f"Vui lòng quét tem thiết bị.", PENDING_BG, 4000)
            else:
                self.end_session("door_closed")

    # ================================================================ quét UHF thiết bị
    def _on_uhf(self, uhf_id):
        if self.workflow_state != self.STATE_DOOR_OPEN or not self.current_user:
            print(f"[BỎ QUA UHF] Chưa xác thực mở tủ. Tag: {uhf_id}")
            return
        if self.processing_uhf:
            self._show_banner("⏳ Đang xử lý thiết bị trước, vui lòng chờ...", NOTICE_TITLE_FG, 1500)
            return

        self._reset_session_timer()
        card = self.cards.get(uhf_id)
        if not card:
            self._show_banner(f"⚠️ Không tìm thấy thiết bị trong {self.selected_cabinet}\nTag: {uhf_id}", EXIT_BG, 2500)
            return

        item = card["data"]
        sensor_name = item.get("sensor", "")
        eq_name = item.get("name", "N/A")
        status = item.get("status", "available")

        if sensor_name and SENSOR_ENFORCE:
            action = self.pending.get(sensor_name)
            if action is None:  # Bắt buộc phải lấy/đặt thật (ô đỏ) rồi mới quét
                if status == "available":
                    msg = f"⚠️ Hãy LẤY [{eq_name}] ra khỏi ô {sensor_name} trước rồi mới quét!"
                else:
                    msg = f"⚠️ Hãy ĐẶT [{eq_name}] vào ô {sensor_name} trước rồi mới quét!"
                self._show_banner(msg, NOTICE_TITLE_FG, 2500)
                return
        else:
            # Không có cảm biến hoặc chế độ demo: mượn/trả theo trạng thái DB
            action = ACTION_BORROW if status == "available" else ACTION_RETURN

        if action == ACTION_BORROW and status != "available":
            self._show_banner(f"⚠️ Thiết bị [{eq_name}] đang ở trạng thái ĐÃ MƯỢN!", NOTICE_TITLE_FG, 2500)
            return
        if action == ACTION_RETURN and status != "borrowed":
            self._show_banner(f"⚠️ Thiết bị [{eq_name}] đang sẵn sàng trong tủ (Chưa được mượn)!", NOTICE_TITLE_FG, 2500)
            return

        self.processing_uhf = True
        self.render_action_panel()
        if self.lbl_door_instruction is not None:
            self.lbl_door_instruction.config(text=f"⏳ Đang xác thực thiết bị: {eq_name} ({sensor_name or '-'})...")

        threading.Thread(
            target=self._uhf_transaction_worker,
            args=(item, dict(self.current_user), action, self.selected_cabinet),
            daemon=True
        ).start()

    def _uhf_transaction_worker(self, item, user_info, action, cabinet_id):
        try:
            if action == ACTION_BORROW:
                tx_status, msg = api_service.borrow_equipment(item, user_info, cabinet_id)
            else:
                tx_status, msg = api_service.return_equipment(item, user_info)
        except Exception as e:
            traceback.print_exc()
            tx_status, msg = api_service.TX_FAIL, f"Lỗi hệ thống: {e}"
        self._post(self._on_uhf_transaction_done, tx_status, msg, item, action, user_info)

    def _on_uhf_transaction_done(self, tx_status, msg, item, action, user_info):
        self.processing_uhf = False
        sensor_name = item.get("sensor", "")

        if tx_status in (api_service.TX_OK, api_service.TX_PARTIAL):
            self.pending.pop(sensor_name, None)
            item["status"] = "borrowed" if action == ACTION_BORROW else "available"
            if self.current_user:
                delta = 1 if action == ACTION_BORROW else -1
                self.user_borrowed_count = max(0, self.user_borrowed_count + delta)
            storage.audit("TRANSACTION", action=action, result=tx_status, device=item.get("name"),
                          tag=item.get("id"), sensor=sensor_name, user=user_info.get("code"))

            card = self.sensor_cards.get(sensor_name) or self.cards.get(item.get("id", ""))
            if card:
                card["data"]["status"] = item["status"]
                if sensor_name in self.sensor_cards:
                    self._apply_sensor(sensor_name)  # VD quét trước khi lấy ra -> ô vẫn đỏ đến khi lấy
                self._paint_card(card)
            self._persist_pending()
            self.update_summary()

            if tx_status == api_service.TX_OK:
                self._show_banner(f"✅ {msg}", RETURN_BTN_BG, 2200)
            else:
                self._show_banner(f"⚠️ {msg}", WARN_BG, 4500)
            self.refresh_api_data(reset_state=False)
        else:
            self._show_banner(f"❌ {msg}", EXIT_BG, 3000)

        if self.workflow_state == self.STATE_DOOR_OPEN:
            self._reset_session_timer()

    # ================================================================ thanh hành động (dưới)
    def _pending_text(self):
        return ", ".join(f"{s} ({ACTION_LABEL[a]})" for s, a in sorted(self.pending.items(),
                                                                       key=lambda kv: _sensor_sort_key(kv[0])))

    def _clear_action_panel(self):
        for widget in self.action_panel.winfo_children():
            widget.destroy()
        self.lbl_door_instruction = None

    def _show_banner(self, message, bg_color, duration_ms=2500):
        if self.banner_timer is not None:
            try:
                self.root.after_cancel(self.banner_timer)
            except Exception:
                pass
        self._clear_action_panel()
        self.action_panel.config(bg=bg_color, highlightthickness=0)
        tk.Label(
            self.action_panel, text=message, font=(FONT_FAMILY, 12, "bold"),
            bg=bg_color, fg=WHITE, justify="center", wraplength=480
        ).pack(expand=True, fill=tk.BOTH, padx=10)
        self.banner_timer = self.root.after(duration_ms, self._end_banner)

    def _end_banner(self):
        self.banner_timer = None
        self.render_action_panel()

    def render_action_panel(self):
        if self.banner_timer is not None:  # banner đang hiển thị -> sẽ tự render khi hết
            return
        self._clear_action_panel()
        in_session = self.workflow_state == self.STATE_DOOR_OPEN

        if self.api_error_msg and not in_session:
            self.action_panel.config(bg=BORROW_FG, highlightthickness=0)
            tk.Label(
                self.action_panel, text=f"⚠️ LỖI HỆ THỐNG\n{self.api_error_msg}",
                font=(FONT_FAMILY, 12, "bold"), bg=BORROW_FG, fg=WHITE, justify="center", wraplength=480
            ).pack(expand=True, fill=tk.BOTH, padx=8)
            return

        if self.workflow_state == self.STATE_SELECT_CABINET:
            self._render_select_cabinet()
        elif self.workflow_state == self.STATE_SCAN_CARD:
            self._render_scan_card()
        else:
            self._render_door_open()

    def _render_select_cabinet(self):
        self.action_panel.config(bg=WHITE, highlightbackground=CABINET_BTN_BG, highlightthickness=2)
        tk.Label(self.action_panel, text="CHỌN TỦ THIẾT BỊ", font=(FONT_FAMILY, 10, "bold"),
                 bg=WHITE, fg=CABINET_BTN_BG).pack(pady=(4, 2))

        cab_row = tk.Frame(self.action_panel, bg=WHITE)
        cab_row.pack(fill=tk.BOTH, expand=True, padx=10, pady=(2, 8))
        for cab in self.cabinets_list:
            cab_name = cab.get("Cabinet")
            if not cab_name:
                continue
            tk.Button(
                cab_row, text=f"🗄️ {cab_name}\n({cab.get('TotalEquipment', 0)} thiết bị)",
                font=(FONT_FAMILY, 11, "bold"), bg=CABINET_BTN_BG, fg=WHITE,
                activebackground="#4338ca", activeforeground=WHITE, relief=tk.FLAT, cursor="hand2",
                command=lambda name=cab_name: self.on_select_cabinet(name)
            ).pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4)

    def _render_scan_card(self):
        bg = PRIMARY_BLUE
        self.action_panel.config(bg=bg, highlightthickness=0)
        content = tk.Frame(self.action_panel, bg=bg)
        content.pack(fill=tk.BOTH, expand=True, padx=12, pady=6)

        tk.Label(content, text=f"TỦ: {self.selected_cabinet}  •  TỰ ĐỘNG NHẬN DIỆN MƯỢN / TRẢ",
                 font=(FONT_FAMILY, 10, "bold"), bg=bg, fg="#bae6fd", anchor="w").pack(fill=tk.X, pady=(2, 2))
        if self.verifying_card:
            text = "⏳ Đang xác thực thẻ nhân viên..."
        elif self.anomalies and SENSOR_ENFORCE:
            bad = ", ".join(sorted(self.anomalies, key=_sensor_sort_key))
            text = f"🔴 Ô [{bad}] chưa xác thực — quét thẻ NV rồi quét tem thiết bị"
        else:
            text = "((•)) Vui lòng quét thẻ nhân viên để mở tủ..."
        tk.Label(content, text=text, font=(FONT_FAMILY, 12 if self.anomalies else 14, "bold"), bg=bg, fg=WHITE,
                 anchor="w", justify="left", wraplength=480).pack(fill=tk.X)

    def _render_door_open(self):
        bg = PENDING_BG if (self.pending and SENSOR_ENFORCE) else BORROW_BTN_BG
        user = self.current_user or {}
        self.action_panel.config(bg=bg, highlightthickness=0)

        content = tk.Frame(self.action_panel, bg=bg)
        content.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)
        text_frame = tk.Frame(content, bg=bg)
        text_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        tk.Label(
            text_frame,
            text=f"✅ {user.get('code', 'N/A')} - {user.get('name', 'N/A')} ({user.get('dept', 'N/A')})"
                 f"  •  Đang mượn: {self.user_borrowed_count}",
            font=(FONT_FAMILY, 10, "bold"), bg=bg, fg=WHITE, anchor="w"
        ).pack(fill=tk.X, pady=(2, 2))

        if self.processing_uhf:
            instruction = "⏳ Đang xử lý giao dịch..."
        elif self.pending and SENSOR_ENFORCE:
            instruction = f"🔴 Ô [{self._pending_text()}] ĐANG CHỜ QUÉT UHF RFID!"
        elif self.pending:
            bad = ", ".join(sorted(self.pending, key=_sensor_sort_key))
            instruction = f"🔓 Quét tem UHF để MƯỢN / TRẢ  •  Ô đỏ: {bad}"
        else:
            instruction = "🔓 Lấy thiết bị để MƯỢN / Đặt thiết bị vào ô để TRẢ, rồi quét UHF RFID"

        self.lbl_door_instruction = tk.Label(
            text_frame, text=instruction, font=(FONT_FAMILY, 11, "bold"),
            bg=bg, fg=WHITE, anchor="w", justify="left", wraplength=380
        )
        self.lbl_door_instruction.pack(fill=tk.X)

        tk.Button(
            content, text="✔ HOÀN TẤT", font=FONT_BOLD, bg="#1e293b", fg=WHITE,
            relief=tk.FLAT, padx=12, pady=8, cursor="hand2", command=self.request_finish
        ).pack(side=tk.RIGHT, padx=(8, 0))

    # ================================================================ lưới thiết bị
    def get_colors_by_status(self, status):
        if status == "available":
            return AVAIL_BG, AVAIL_FG
        if status == "borrowed":
            return IN_USE_BG, IN_USE_FG
        return OVERDUE_BG, OVERDUE_FG

    def _paint_card(self, card):
        item = card["data"]
        sensor = item.get("sensor", "")
        action = self.pending.get(sensor) if sensor else None
        if action:
            bg, fg = PENDING_BG, PENDING_FG
            sub = f"{sensor} • QUÉT ({ACTION_LABEL[action]})" if SENSOR_ENFORCE else f"{sensor} • KIỂM TRA"
        elif sensor and sensor in self.anomalies:
            bg, fg = PENDING_BG, PENDING_FG
            sub = f"{sensor} • CHƯA XÁC THỰC" if SENSOR_ENFORCE else f"{sensor} • KIỂM TRA"
        else:
            bg, fg = self.get_colors_by_status(item.get("status", "available"))
            sub = sensor or "-"
        card["frame"].config(bg=bg)
        card["label"].config(bg=bg, fg=fg)
        card["sub_label"].config(bg=bg, fg=fg, text=sub)

    def _repaint_all_cards(self):
        for card in self.cards.values():
            self._paint_card(card)
        for card in self.sensor_cards.values():
            self._paint_card(card)

    def rebuild_device_and_lending_ui(self):
        for widget in self.left_panel.winfo_children():
            widget.destroy()
        for widget in self.right_panel.winfo_children():
            widget.destroy()
        self.cards.clear()
        self.sensor_cards.clear()

        if self.api_error_msg:
            tk.Label(self.left_panel, text=f"⚠️ {self.api_error_msg}", font=FONT_HEADER,
                     fg=BORROW_FG, bg=BG_COLOR, wraplength=450).pack(expand=True)
        else:
            self.create_device_grid()
            self._reconcile_sensors()  # áp trạng thái cảm biến đã nhận lên dữ liệu mới

        self.create_borrowing_list()
        self.update_summary()

    def create_device_grid(self):
        if not self.items_data:
            tk.Label(self.left_panel, text=f"⚠️ {self.api_warning_msg or 'Tủ chưa có thiết bị'}",
                     font=FONT_HEADER, fg=NOTICE_TITLE_FG, bg=BG_COLOR, wraplength=450).pack(expand=True)
            return

        layers = {}
        for item in self.items_data:
            key = (item.get("layer_no", 999), item.get("layer", "LAYER ?"))
            layers.setdefault(key, []).append(item)

        cols = max(1, GRID_COLUMNS)
        for (_, layer_name), items in sorted(layers.items()):
            items = sorted(items, key=lambda it: _sensor_sort_key(it.get("sensor")))

            layer_frame = tk.Frame(self.left_panel, bg=WHITE)
            layer_frame.pack(fill=tk.X, pady=(0, 6))
            tk.Label(layer_frame, text=layer_name, font=FONT_BOLD, fg=PRIMARY_BLUE, bg=WHITE).pack(anchor="w", padx=8, pady=2)

            grid_f = tk.Frame(layer_frame, bg=WHITE)
            grid_f.pack(fill=tk.X, padx=8, pady=(0, 6))
            for c in range(cols):
                grid_f.columnconfigure(c, weight=1, uniform="cell")

            for idx, item in enumerate(items):
                r, c = divmod(idx, cols)
                box = tk.Frame(grid_f, bd=1, relief=tk.SOLID)
                box.grid(row=r, column=c, padx=2, pady=2, sticky="nsew")
                lbl = tk.Label(box, text=item.get("name", "N/A"), font=(FONT_FAMILY, 9, "bold"))
                lbl.pack(pady=(3, 0), fill=tk.X)
                sub_lbl = tk.Label(box, text="", font=(FONT_FAMILY, 7))
                sub_lbl.pack(pady=(0, 2), fill=tk.X)

                card = {"frame": box, "label": lbl, "sub_label": sub_lbl, "data": item}
                self._paint_card(card)

                if item.get("id"):
                    self.cards[item["id"]] = card
                sensor = item.get("sensor", "")
                if sensor:
                    if sensor in self.sensor_cards:
                        print(f"⚠️ [DỮ LIỆU] Cảm biến {sensor} được gán cho nhiều thiết bị!")
                    self.sensor_cards[sensor] = card

    # ================================================================ danh sách mượn (phải)
    def create_borrowing_list(self):
        tk.Label(self.right_panel, text=f"Current Borrowing List ({self.selected_cabinet})",
                 font=FONT_HEADER, bg=WHITE, fg=PRIMARY_BLUE).pack(anchor="w", padx=10, pady=(10, 4))

        lending_warning = self.api_warning_msg if self.items_data else ""
        if lending_warning:
            tk.Label(self.right_panel, text=f"⚠️ {lending_warning}", font=(FONT_FAMILY, 9, "bold"),
                     bg=WHITE, fg=NOTICE_TITLE_FG, wraplength=440, justify="left").pack(anchor="w", padx=10)

        tree_frame = tk.Frame(self.right_panel, bg=WHITE)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(4, 10))

        columns = ("#", "Employee", "Dept", "Device", "Borrow Time")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings")
        scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)

        for col, text, width, anchor in (
            ("#", "#", 35, "center"),
            ("Employee", "Employee", 90, "w"),
            ("Dept", "Dept", 105, "w"),
            ("Device", "Device", 90, "center"),
            ("Borrow Time", "Time", 95, "w"),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor)

        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        for idx, rec in enumerate(self.lending_data, start=1):
            self.tree.insert("", tk.END, values=(
                idx,
                rec.get("LENDING_Requester") or "N/A",
                rec.get("LENDING_requestDept") or "N/A",
                rec.get("controlNo") or rec.get("NameOf_Equipment") or "N/A",
                _fmt_time(rec.get("LENDING_requestDate")),
            ))

    # ================================================================ header / bottom
    def create_header(self):
        header_frame = tk.Frame(self.main_container, bg=WHITE, height=70)
        header_frame.pack(fill=tk.X)
        header_frame.pack_propagate(False)

        title_frame = tk.Frame(header_frame, bg=WHITE)
        title_frame.pack(side=tk.LEFT, padx=12, pady=8)
        tk.Label(title_frame, text="SMART CABINET", font=FONT_TITLE, fg=PRIMARY_BLUE, bg=WHITE).pack(anchor="w")
        self.lbl_cabinet_subtitle = tk.Label(
            title_frame, text=f"Cabinet: {self.selected_cabinet}  |  Auto Borrow / Return",
            font=(FONT_FAMILY, 8), fg=TEXT_LIGHT, bg=WHITE
        )
        self.lbl_cabinet_subtitle.pack(anchor="w")

        stats_frame = tk.Frame(header_frame, bg=WHITE)
        stats_frame.pack(side=tk.LEFT, padx=8, fill=tk.Y, pady=5)
        self.summary_vars = {k: tk.StringVar(value="0") for k in ("Total", "Available", "Borrowed", "Pending")}
        self.create_stat_box(stats_frame, "Total", "#e2e8f0", PRIMARY_BLUE, self.summary_vars["Total"])
        self.create_stat_box(stats_frame, "Available", AVAIL_BG, AVAIL_FG, self.summary_vars["Available"])
        self.create_stat_box(stats_frame, "In Use", "#e2e8f0", "#475569", self.summary_vars["Borrowed"])
        self.create_stat_box(stats_frame, "Pending", BORROW_BG, BORROW_FG, self.summary_vars["Pending"])

        if SHOW_EXIT_BUTTON or DEBUG_MODE:
            tk.Button(header_frame, text="✖ EXIT", font=FONT_BOLD, bg=EXIT_BG, fg=WHITE,
                      relief=tk.FLAT, padx=12, pady=6, cursor="hand2",
                      command=self.exit_app).pack(side=tk.RIGHT, padx=(6, 12))

        self.btn_refresh = tk.Button(
            header_frame, text="↻ REFRESH", font=FONT_BOLD, bg=REFRESH_BG, fg=WHITE,
            relief=tk.FLAT, padx=10, pady=6, cursor="hand2", command=self._on_refresh_clicked
        )
        self.btn_refresh.pack(side=tk.RIGHT, padx=4)

        self.clock_lbl = tk.Label(header_frame, text="", font=FONT_BOLD, bg=WHITE, fg=TEXT_MAIN, justify="right")
        self.clock_lbl.pack(side=tk.RIGHT, padx=8)
        self.update_clock()

    def _on_refresh_clicked(self):
        # Không bao giờ reset phiên đang mở; ngoài phiên thì quay về màn chờ như cũ
        self.refresh_api_data(reset_state=self.workflow_state != self.STATE_DOOR_OPEN)

    def create_stat_box(self, parent, title, bg_color, fg_color, var):
        frame = tk.Frame(parent, bg=bg_color, width=64)
        frame.pack(side=tk.LEFT, fill=tk.Y, padx=3)
        frame.pack_propagate(False)
        tk.Label(frame, text=title, font=(FONT_FAMILY, 8), bg=bg_color, fg=TEXT_LIGHT).pack(pady=(5, 0))
        tk.Label(frame, textvariable=var, font=(FONT_FAMILY, 15, "bold"), bg=bg_color, fg=fg_color).pack()

    def create_bottom_panel(self):
        bottom_frame = tk.Frame(self.main_container, bg=BG_COLOR, height=96)
        bottom_frame.pack(fill=tk.X, side=tk.BOTTOM)
        bottom_frame.pack_propagate(False)

        status_frame = tk.Frame(bottom_frame, bg=WHITE, width=230)
        status_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
        status_frame.pack_propagate(False)
        tk.Label(status_frame, text="⚙ System Status", font=FONT_BOLD, bg=WHITE, fg=PRIMARY_BLUE).pack(anchor="w", padx=10, pady=(4, 2))

        self.lbl_rfid_status = tk.Label(status_frame, text="🔴 RFID Reader: Checking...", font=(FONT_FAMILY, 8, "bold"), bg=WHITE, fg=TEXT_LIGHT)
        self.lbl_rfid_status.pack(anchor="w", padx=14)
        self.lbl_door_status = tk.Label(status_frame, text="⚪ Door: Unknown | Mega: Checking", font=(FONT_FAMILY, 8, "bold"), bg=WHITE, fg=TEXT_LIGHT)
        self.lbl_door_status.pack(anchor="w", padx=14)
        self.lbl_api_status = tk.Label(status_frame, text="🟡 Server API: Connecting...", font=(FONT_FAMILY, 8, "bold"), bg=WHITE, fg=NOTICE_TITLE_FG)
        self.lbl_api_status.pack(anchor="w", padx=14)

        notice_frame = tk.Frame(bottom_frame, bg=NOTICE_BG, width=230,
                                highlightbackground=NOTICE_BORDER, highlightthickness=2)
        notice_frame.pack(side=tk.RIGHT, fill=tk.Y, padx=(10, 0))
        notice_frame.pack_propagate(False)
        tk.Label(notice_frame, text="⚠️ IMPORTANT NOTICE", font=FONT_NOTICE_TITLE,
                 bg=NOTICE_BG, fg=NOTICE_TITLE_FG).pack(anchor="w", padx=10, pady=(6, 2))
        tk.Label(notice_frame, text="Red = Waiting RFID scan\nGrey = In use\nGreen = Available",
                 font=FONT_NOTICE_TEXT, bg=NOTICE_BG, fg=NOTICE_TEXT_FG, justify="left").pack(anchor="w", padx=10)

        self.action_panel = tk.Frame(bottom_frame, bg=WHITE)
        self.action_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.render_action_panel()

    def update_summary(self):
        self.summary_vars["Total"].set(str(len(self.items_data)))
        self.summary_vars["Available"].set(str(sum(1 for i in self.items_data if i.get("status") == "available")))
        self.summary_vars["Borrowed"].set(str(sum(1 for i in self.items_data if i.get("status") == "borrowed")))
        self.summary_vars["Pending"].set(str(len(self.pending) + len(self.anomalies)))

    def update_clock(self):
        self.clock_lbl.config(text=datetime.now().strftime("%b %d, %Y\n%H:%M:%S\n%a"))
        self.root.after(1000, self.update_clock)

    # ================================================================ phím (chỉ khi DEBUG)
    def setup_bindings(self):
        if not DEBUG_MODE:
            return
        print("⚠️ DEBUG_MODE đang BẬT - phím test hoạt động. Tắt trước khi triển khai!")
        test_user = {"code": "TEST001", "name": "Test User", "dept": "TEST", "card_code": "TEST001"}
        self.root.bind("<Escape>", lambda e: self.exit_app())
        self.root.bind("<space>", lambda e: self.request_finish())
        self.root.bind("<F5>", lambda e: self._on_refresh_clicked())
        self.root.bind("1", lambda e: self._open_session(dict(test_user)) if self.workflow_state == self.STATE_SCAN_CARD else None)
        self.root.bind("2", lambda e: self._on_sensor("S1", 0))
        self.root.bind("4", lambda e: self._on_sensor("S1", 1))
        self.root.bind("3", lambda e: self._on_uhf(self.sensor_cards["S1"]["data"]["id"]) if "S1" in self.sensor_cards else None)

    def exit_app(self):
        print("Đang thoát chương trình...")
        self.root.quit()

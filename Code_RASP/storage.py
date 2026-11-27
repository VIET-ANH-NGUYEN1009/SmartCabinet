"""
Lưu trạng thái ô đỏ (pending) xuống đĩa và ghi nhật ký sự kiện (audit log).
- Pending được lưu để khi app khởi động lại không mất dấu thiết bị đã lấy ra mà chưa quét.
- Audit log (JSON Lines) để admin tra cứu các sự kiện bất thường / giao dịch ghi thiếu.
"""
import json
import os
import threading
from datetime import datetime

from config import PENDING_STATE_FILE, AUDIT_LOG_FILE

_lock = threading.Lock()


def audit(event, **data):
    record = {"time": datetime.now().isoformat(timespec="seconds"), "event": event}
    record.update(data)
    print(f"[AUDIT] {record}")
    try:
        with _lock, open(AUDIT_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as e:
        print(f"[AUDIT] Không ghi được file log: {e}")


def save_pending_state(state):
    """state = dict để lưu, hoặc None để xóa."""
    try:
        with _lock:
            if not state:
                if os.path.exists(PENDING_STATE_FILE):
                    os.remove(PENDING_STATE_FILE)
                return
            tmp = PENDING_STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, PENDING_STATE_FILE)  # ghi nguyên tử, không hỏng file khi mất điện giữa chừng
    except OSError as e:
        print(f"[STORAGE] Không lưu được trạng thái pending: {e}")


def load_pending_state():
    try:
        with _lock, open(PENDING_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        print(f"[STORAGE] File trạng thái pending bị lỗi, bỏ qua: {e}")
        return None

    if not isinstance(state, dict) or not isinstance(state.get("pending"), dict) or not isinstance(state.get("user"), dict):
        return None
    return state

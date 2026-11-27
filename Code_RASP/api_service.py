"""
Giao tiếp với API Smart Cabinet (Node.js/Express chạy sau IIS).

Quy ước Using_Status trên server:
    'Lending'            = đang có người mượn
    'In Use' / 'Returned' / khác = đang ở trong tủ, sẵn sàng cho mượn

Kết quả giao dịch mượn/trả: (TX_OK | TX_PARTIAL | TX_FAIL, message)
    TX_OK      : thành công đầy đủ
    TX_PARTIAL : thiết bị đã đổi trạng thái nhưng lịch sử ghi thiếu -> đã ghi audit log
    TX_FAIL    : thất bại, trạng thái thiết bị chưa đổi

Tương thích server:
    - Server mới: POST /apiPost/borrowEquipment, PUT /apiPut/updateStatusReturned (1 transaction mỗi thao tác)
    - Server cũ : chưa có borrowEquipment -> tự dùng 2 bước updateEquipment_Lending_Status + insertLendingHistory
"""

import time
from datetime import datetime

import requests

from config import (
    API_GET_URL,
    API_POST_URL,
    API_PUT_URL,
    DEFAULT_CABINET_ID,
    API_TIMEOUT,
)
import storage

TX_OK = "ok"
TX_PARTIAL = "partial"
TX_FAIL = "fail"

CONN_ERROR_MSG = "Không thể kết nối đến Máy chủ API!\nVui lòng kiểm tra mạng LAN."
UNKNOWN_RESULT_MSG = "Mất kết nối giữa chừng, chưa rõ kết quả.\nBấm REFRESH để kiểm tra trước khi quét lại."


# =================================================================== tiện ích
def _s(value, default=""):
    """Chuyển về chuỗi đã strip, an toàn với None."""
    return default if value is None else str(value).strip()


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _json(res):
    """Body JSON dạng dict, hoặc None nếu không phải JSON (VD trang lỗi HTML của IIS)."""
    try:
        body = res.json()
    except ValueError:
        return None
    return body if isinstance(body, (dict, list)) else None


def _extract_list(body):
    """API có lúc trả {"data": [...]}, có lúc trả thẳng list, có lúc data = null."""
    data = body.get("data") if isinstance(body, dict) else body
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _error_text(res):
    """Thông báo lỗi dễ hiểu từ response lỗi."""
    body = _json(res)
    if isinstance(body, dict) and body.get("message"):
        return str(body["message"])
    if res.status_code == 405 and body is None:
        return "Server chặn phương thức (405) - kiểm tra web.config / WebDAV trên IIS"
    return f"HTTP {res.status_code}"


def _route_missing(res):
    """Express trả 404 dạng HTML ("Cannot POST ...") khi server chưa có route -> server bản cũ."""
    return res is not None and res.status_code == 404 and _json(res) is None


def _call(method, url, *, params=None, payload=None, retries=0):
    """
    Gọi HTTP. Trả về (response hoặc None, chuỗi lỗi kết nối).
    retries chỉ dùng cho thao tác gửi lại an toàn (GET, PUT); POST để 0 để tránh ghi trùng.
    Thử lại khi lỗi mạng hoặc 5xx.
    """
    res, err = None, ""
    for attempt in range(retries + 1):
        try:
            res = requests.request(
                method, url, params=params, json=payload, timeout=API_TIMEOUT
            )
            err = ""
            if res.status_code < 500:
                break
        except requests.exceptions.RequestException as e:
            res, err = None, str(e)
        if attempt < retries:
            time.sleep(0.5)

    if res is not None and res.status_code != 200:
        print(
            f"[API {method}] {url} -> {res.status_code} | Allow: {res.headers.get('Allow', '-')} | "
            f"payload={payload} | body={res.text[:300]!r}"
        )
    elif res is None:
        print(f"[API {method}] {url} -> lỗi kết nối: {err}")
    return res, err


# =================================================================== đọc dữ liệu
def fetch_cabinets():
    """Trả về (cabinets_list, error_msg)."""
    res, err = _call("GET", f"{API_GET_URL}/CabinetCount", retries=1)
    if res is None:
        return [], CONN_ERROR_MSG
    if res.status_code != 200:
        return [], f"Lỗi API danh sách tủ: {_error_text(res)}"
    body = _json(res)
    if body is None:
        return [], "Máy chủ trả dữ liệu tủ không hợp lệ!"
    data = _extract_list(body)
    return (data or [{"Cabinet": DEFAULT_CABINET_ID, "TotalEquipment": 0}]), ""


def _parse_item(item):
    using_status = _s(item.get("Using_Status")).lower()
    layer_raw = _s(item.get("Layer"), "1") or "1"
    try:
        layer_no = int(float(layer_raw))
    except ValueError:
        layer_no = 999

    return {
        "id": _s(item.get("RFID_codes")).upper(),
        "name": _s(item.get("controlNo")) or "N/A",
        "vn_name": _s(item.get("VietNamese_Name") or item.get("NameOf_Equipment")),
        "layer": f"LAYER {layer_raw}",
        "layer_no": layer_no,
        "sensor": _s(
            item.get("Sensor")
        ).upper(),  # Sensor = null -> "" = không có cảm biến
        "slot": _s(item.get("Cabinet_Slot")),
        "status": "borrowed" if using_status == "lending" else "available",
        "raw_data": item,
    }


def fetch_cabinet_data(cabinet_id):
    """
    Trả về (items_data, lending_data, error_msg, warning_msg)
        error_msg  : lỗi nghiêm trọng (không có dữ liệu thiết bị) -> khóa thao tác
        warning_msg: lỗi phụ (tủ trống, không tải được danh sách mượn) -> vẫn cho thao tác
    """
    cabinet_id = cabinet_id or DEFAULT_CABINET_ID
    warnings = []

    res, _ = _call(
        "GET",
        f"{API_GET_URL}/getEquipment_table",
        params={"Cabinet": cabinet_id},
        retries=1,
    )
    if res is None:
        return [], [], CONN_ERROR_MSG, ""
    if res.status_code == 404:  # server trả 404 khi tủ không có thiết bị
        items_data = []
    elif res.status_code != 200:
        return [], [], f"Lỗi API Tủ ({cabinet_id}): {_error_text(res)}", ""
    else:
        body = _json(res)
        if body is None:
            return [], [], "Máy chủ trả dữ liệu thiết bị không hợp lệ!", ""
        items_data = [_parse_item(it) for it in _extract_list(body)]

    if not items_data:
        warnings.append(f"Không có thiết bị nào trong tủ {cabinet_id}!")

    lending_data = []
    res, _ = _call(
        "GET",
        f"{API_GET_URL}/getEquipment_lending",
        params={"Cabinet": cabinet_id},
        retries=1,
    )
    if res is None:
        warnings.append("Không tải được danh sách đang mượn.")
    elif res.status_code == 200:
        lending_data = _extract_list(_json(res) or {})
    elif res.status_code != 404:  # 404 = không có thiết bị nào đang mượn
        warnings.append(f"Lỗi API Lịch sử mượn: {_error_text(res)}")

    return items_data, lending_data, "", "\n".join(warnings)


def verify_employee(card_code):
    """Kiểm tra mã thẻ nhân viên (Code_Solenoid). Trả về (is_valid, user_info_dict hoặc error_msg)."""
    res, _ = _call(
        "GET", f"{API_GET_URL}/getUser", params={"Code_Solenoid": card_code}, retries=1
    )
    if res is None:
        return False, "Không thể kết nối đến máy chủ!"
    if res.status_code == 404:
        return False, "Không tìm thấy thông tin nhân viên!"
    if res.status_code != 200:
        return False, f"Lỗi phản hồi máy chủ: {_error_text(res)}"

    body = _json(res)
    if not isinstance(body, dict) or body.get("result") != "OK":
        return False, "Mã thẻ không tồn tại trong hệ thống!"
    users = _extract_list(body)
    if not users:
        return False, "Mã thẻ không tồn tại trong hệ thống!"
    u = users[0]

    return True, {
        "code": _s(
            u.get("Code")
            or u.get("EmployeeCode")
            or u.get("EmpCode")
            or u.get("UserID")
            or card_code
        ),
        "name": _s(u.get("FullName") or u.get("Name")) or "N/A",
        "dept": _s(u.get("Dept") or u.get("Department") or u.get("DeptName")) or "N/A",
        "card_code": _s(u.get("Code_Solenoid") or card_code),
    }


# =================================================================== giao dịch
def borrow_equipment(item_data, user_info, cabinet_id):

    control_no = (item_data.get("raw_data") or {}).get("controlNo") or item_data.get(
        "name", "N/A"
    )

    now_str = _now_str()

    return _borrow_legacy(
        item_data=item_data,
        user_info=user_info,
        cabinet_id=cabinet_id,
        control_no=control_no,
        now_str=now_str,
    )


def _borrow_legacy(item_data, user_info, cabinet_id, control_no, now_str):

    raw = item_data.get("raw_data") or {}

    res, _ = _call(
        "PUT",
        f"{API_PUT_URL}/updateEquipment_Lending_Status",
        retries=1,
        payload={
            "RFID_codes": item_data.get("id", ""),
            "Code": user_info.get("code"),
            "Dept": user_info.get("dept"),
            "time": now_str,
        },
    )
    if res is None:
        return TX_FAIL, CONN_ERROR_MSG
    if res.status_code != 200:
        return TX_FAIL, f"Không mượn được {control_no}:\n{_error_text(res)}"

    history = {
        "controlNo": control_no,
        "Category": raw.get("Category") or raw.get("Category1", ""),
        "equipmentName": raw.get("NameOf_Equipment")
        or raw.get("equipmentName")
        or control_no,
        "borrower": user_info.get("code"),
        "Borrow_Name": user_info.get("name"),
        "Code_Solenoid": user_info.get("card_code"),
        "borrowDate": now_str,
        "Dept": user_info.get("dept"),
        "Location": cabinet_id,
    }
    res, err = _call("POST", f"{API_POST_URL}/insertLendingHistory", payload=history)
    if res is None or res.status_code != 200:
        storage.audit(
            "BORROW_HISTORY_MISSING",
            payload=history,
            error=err if res is None else _error_text(res),
        )
        return (
            TX_PARTIAL,
            f"Đã mượn {control_no} nhưng CHƯA ghi được lịch sử.\nĐã lưu log, vui lòng báo admin.",
        )
    return TX_OK, f"Mượn thành công: {control_no}"


def return_equipment(item_data, user_info):
    """TRẢ thiết bị: PUT /apiPut/updateStatusReturned (đổi trạng thái + đóng lịch sử)."""
    control_no = (item_data.get("raw_data") or {}).get("controlNo") or item_data.get(
        "name", "N/A"
    )
    payload = {
        "RFID_codes": item_data.get("id", ""),
        "Code": user_info.get("code"),
        "Code_Solenoid": user_info.get("card_code"),
        "Dept": user_info.get("dept"),
        "controlNo": control_no,
        "time": _now_str(),
    }

    # PUT trả thiết bị gửi lại an toàn: lần 2 server sẽ trả 409 "chưa được mượn" nếu lần 1 đã thành công
    res, _ = _call(
        "PUT", f"{API_PUT_URL}/updateStatusReturned", payload=payload, retries=1
    )
    if res is None:
        storage.audit("RETURN_UNKNOWN_RESULT", payload=payload)
        return TX_FAIL, UNKNOWN_RESULT_MSG
    if res.status_code != 200:
        return TX_FAIL, f"Không trả được {control_no}:\n{_error_text(res)}"

    body = _json(res)
    if (
        isinstance(body, dict) and body.get("historyClosed") == 0
    ):  # chỉ server mới có trường này
        storage.audit("RETURN_HISTORY_NOT_FOUND", payload=payload)
        return (
            TX_PARTIAL,
            f"Đã trả {control_no} nhưng không tìm thấy lịch sử mượn.\nĐã lưu log.",
        )
    return TX_OK, f"Trả thành công: {control_no}"

import tkinter as tk

import pigpio

from config import WIEGAND_GPIO_0, WIEGAND_GPIO_1, WIEGAND_CHECK_PARITY, DEBUG_MODE
from gui import SmartCabinetApp
from hardware import WiegandReader, UHFListener, ArduinoSerialBridge


def main():
    print("Khởi tạo hệ thống Smart Cabinet...")
    if DEBUG_MODE:
        print("⚠️ DEBUG_MODE = True: phím test đang bật, KHÔNG dùng cấu hình này khi triển khai.")

    pi = pigpio.pi()
    root = tk.Tk()
    app = SmartCabinetApp(root)
    root.protocol("WM_DELETE_WINDOW", app.exit_app)

    # 1. Arduino Mega 2560 (30 cảm biến S1..S30, cảm biến cửa, lệnh OPEN)
    arduino_bridge = ArduinoSerialBridge(
        on_sensor_change=app.handle_sensor_change,
        on_door_change=app.handle_door_change,
        on_status_change=app.set_arduino_status,
    )
    app.set_arduino_sender(arduino_bridge.send_command)
    arduino_bridge.start()

    # 2. Đầu đọc UHF (evdev, có grab + tự kết nối lại).
   
    uhf_listener = UHFListener(
        on_tag_scanned=app.handle_uhf_code,
        on_employee_scanned=None,
        on_status_change=app.set_rfid_status,
    )
    uhf_listener.start()

    # 3. Đầu đọc thẻ nhân viên Wiegand (GPIO)
    reader = None
    if pi.connected:
        reader = WiegandReader(
            pi, gpio_0=WIEGAND_GPIO_0, gpio_1=WIEGAND_GPIO_1,
            callback=app.handle_employee_scan, check_parity=WIEGAND_CHECK_PARITY,
        )
        app.set_wiegand_status(True)
    else:
        app.set_wiegand_status(False)
        print("Cảnh báo: pigpio chưa kết nối (chạy `sudo pigpiod`). Đầu đọc Wiegand bị tắt.")

    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        uhf_listener.stop()
        arduino_bridge.stop()
        if reader:
            reader.cleanup()
        if pi.connected:
            pi.stop()
        print("Đã giải phóng tài nguyên và đóng ứng dụng.")


if __name__ == "__main__":
    main()

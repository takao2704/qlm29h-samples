#!/usr/bin/env python3
"""
rtk_client.py  –  NTRIP client for Quectel QLM29HBAA-GM

Receives RTCM corrections from Quectel RTK service and injects
them into the GNSS module via USB serial.

Requirements:
    pip3 install -r requirements.txt

Usage:
    1. Set NTRIP_USER and NTRIP_PASS as environment variables with the
       credentials provided by SORACOM:
           export NTRIP_USER="..."
           export NTRIP_PASS="..."
    2. Run: python3 rtk_client.py
"""

import socket
import serial
import threading
import time
import base64
import os
import sys

# ===== 設定（ここを編集してください） =====
SERIAL_PORT = "/dev/ttyUSB0"        # シリアルポート（環境に合わせて変更）
SERIAL_BAUD = 115200                # ボーレート

NTRIP_HOST  = "qrtksa1.quectel.com" # NTRIPサーバー
NTRIP_PORT  = 2101                   # NTRIPポート
NTRIP_MOUNT = "AUTO"                 # マウントポイント
NTRIP_USER  = os.environ.get("NTRIP_USER", "")  # 環境変数から取得
NTRIP_PASS  = os.environ.get("NTRIP_PASS", "")  # 環境変数から取得
# ==========================================

# RTCMメッセージ番号の説明
RTCM_MSG_LABELS = {
    1005: "reference station coordinates (stationary)",
    1033: "receiver/antenna description",
    1074: "GPS MSM4",
    1084: "GLONASS MSM4",
    1094: "Galileo MSM4",
    1124: "BDS MSM4",
    3257: "proprietary (Quectel RTK extension)",
    4094: "proprietary (Quectel RTK service)",
}

# GGA品質インジケーター
GGA_QUALITY = {
    "0": "No Fix",
    "1": "GPS SPS Mode",
    "2": "Differential GPS / SPS / SBAS Mode",
    "4": "Fixed RTK",
    "5": "Float RTK",
}

latest_gga = None
gga_lock   = threading.Lock()
running    = True


def nmea_checksum(sentence: str) -> str:
    """$ と * の間のXORチェックサムを計算"""
    data = sentence.strip().lstrip("$").split("*")[0]
    cs = 0
    for c in data:
        cs ^= ord(c)
    return format(cs, "02X")


def rtcm_label(data: bytes) -> str:
    """RTCMパケットのヘッダーからメッセージIDを読み取り、説明を返す"""
    labels = []
    pos = 0
    while pos < len(data) - 3:
        if data[pos] != 0xD3:
            pos += 1
            continue
        if pos + 2 >= len(data):
            break
        length = ((data[pos + 1] & 0x03) << 8) | data[pos + 2]
        if pos + 3 + length > len(data):
            break
        if pos + 4 < len(data):
            msg_id = ((data[pos + 3] & 0xFF) << 4) | ((data[pos + 4] & 0xF0) >> 4)
            label = RTCM_MSG_LABELS.get(msg_id, f"msg#{msg_id}")
            if label not in labels:
                labels.append(label)
        pos += 3 + length + 3  # header + payload + CRC
    return " / ".join(labels) if labels else "heartbeat (keep-alive)"


def init_rtk_mode(ser: serial.Serial):
    """RTKモードを有効化するコマンドを送信"""
    cmd = "PQTMCFGRTK,W,1,1"
    cs  = nmea_checksum(cmd)
    msg = f"${cmd}*{cs}\r\n"
    print(f"[INIT] Enabling RTK mode: {msg.strip()}")
    ser.write(msg.encode())
    time.sleep(0.5)


def read_serial(ser: serial.Serial):
    """シリアルからNMEAを読み続け、GGAを更新してFixステータスを表示"""
    global latest_gga, running
    buf = ""
    while running:
        try:
            chunk = ser.read(ser.in_waiting or 1).decode("ascii", errors="ignore")
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                if "GGA" in line and line.startswith("$"):
                    with gga_lock:
                        latest_gga = line + "\r\n"
        except Exception as e:
            if running:
                print(f"[SERIAL READ ERROR] {e}")
            time.sleep(0.1)


def build_ntrip_request() -> bytes:
    """NTRIP v1.0 GETリクエストを生成"""
    if NTRIP_USER or NTRIP_PASS:
        cred = base64.b64encode(f"{NTRIP_USER}:{NTRIP_PASS}".encode()).decode()
        auth = f"Authorization: Basic {cred}\r\n"
    else:
        auth = ""

    req = (
        f"GET /{NTRIP_MOUNT} HTTP/1.0\r\n"
        f"User-Agent: NTRIP PythonClient/1.0\r\n"
        f"Host: {NTRIP_HOST}\r\n"
        f"Accept: */*\r\n"
        f"Connection: close\r\n"
        f"{auth}"
        f"\r\n"
    )
    return req.encode()


def ntrip_client(ser: serial.Serial):
    """NTRIPサーバーに接続してRTCMを受信し、UARTへ書き込む"""
    global running

    while running:
        sock = None
        try:
            print(f"[NTRIP] Connecting to {NTRIP_HOST}:{NTRIP_PORT} ...")
            sock = socket.create_connection((NTRIP_HOST, NTRIP_PORT), timeout=10)
            sock.sendall(build_ntrip_request())

            # レスポンスヘッダー確認
            response = b""
            while b"\r\n\r\n" not in response and b"ICY 200 OK" not in response:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                response += chunk

            if b"ICY 200 OK" not in response and b"200 OK" not in response:
                print(f"[NTRIP] Connection failed:\n{response.decode(errors='ignore')[:200]}")
                time.sleep(5)
                continue

            print("[NTRIP] Connected. Receiving RTCM corrections...")
            last_gga_sent = 0
            buf = b""

            while running:
                sock.settimeout(5.0)
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    chunk = b""

                if chunk:
                    buf += chunk
                    # バッファが大きくなったら書き込み・ログ表示
                    if len(buf) >= 100 or (len(buf) > 0 and not chunk):
                        ser.write(buf)
                        label = rtcm_label(buf)
                        print(f"[NTRIP] RTCM forwarded: {len(buf):4d} bytes  [{label}]")
                        buf = b""

                # GGAを1秒ごとにキャスターへ送信
                now = time.time()
                if now - last_gga_sent >= 1.0:
                    with gga_lock:
                        gga = latest_gga
                    if gga:
                        sock.sendall(gga.encode())
                        # Fix状態をログに表示
                        parts = gga.strip().split(",")
                        if len(parts) >= 7:
                            quality_code = parts[6]
                            quality_name = GGA_QUALITY.get(quality_code, f"Unknown({quality_code})")
                            print(f"[NTRIP] GGA sent ({quality_name}): {gga.strip()}")
                    last_gga_sent = now

        except Exception as e:
            if running:
                print(f"[NTRIP] Error: {e}. Reconnecting in 5s...")
            time.sleep(5)
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass


def main():
    global running

    # 設定確認
    if not NTRIP_USER or not NTRIP_PASS:
        print("[ERROR] NTRIP_USER and NTRIP_PASS must be set.")
        print("        export NTRIP_USER=... NTRIP_PASS=... and re-run.")
        sys.exit(1)

    print(f"[MAIN] Opening serial port {SERIAL_PORT} @ {SERIAL_BAUD} bps")
    print(f"       If port not found, check with: ls /dev/ttyUSB* /dev/ttyACM*")
    try:
        ser = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=1)
    except serial.SerialException as e:
        print(f"[ERROR] Cannot open serial port: {e}")
        print(f"        Check available ports with: ls /dev/ttyUSB*")
        sys.exit(1)

    time.sleep(1)
    init_rtk_mode(ser)

    # スレッド起動
    t_serial = threading.Thread(target=read_serial, args=(ser,), daemon=True)
    t_ntrip  = threading.Thread(target=ntrip_client, args=(ser,), daemon=True)
    t_serial.start()
    t_ntrip.start()

    print("[MAIN] Waiting for first GNSS fix (up to 30s)...")
    time.sleep(5)
    print("[MAIN] Running. Press Ctrl+C to stop.")
    print("[MAIN] Watch for 'Fixed RTK' in GGA output to confirm RTK positioning.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[MAIN] Stopping...")
        running = False

    ser.close()
    print("[MAIN] Done.")


if __name__ == "__main__":
    main()

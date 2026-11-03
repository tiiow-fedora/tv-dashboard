"""Samsung Tizen TV remote control over the WebSocket API (port 8002).

CLI:  python tvctl.py key KEY_HOME
      python tvctl.py open http://192.168.1.181:8900/
      python tvctl.py wake
      python tvctl.py status
"""
import base64
import json
import os
import socket
import ssl
import sys
import time
import urllib.request

import websocket

HERE = os.path.dirname(os.path.abspath(__file__))
TV_IP = "192.168.1.159"
TV_MAC = "B8:BC:5B:EA:82:26"
APP_NAME = "HomelabTVDash"


def _token():
    try:
        with open(os.path.join(HERE, "tv_token.txt")) as f:
            return f.read().strip()
    except OSError:
        return ""


def _connect(timeout=6):
    name = base64.b64encode(APP_NAME.encode()).decode()
    url = (f"wss://{TV_IP}:8002/api/v2/channels/samsung.remote.control"
           f"?name={name}&token={_token()}")
    ws = websocket.create_connection(url, timeout=timeout,
                                     sslopt={"cert_reqs": ssl.CERT_NONE})
    hello = json.loads(ws.recv())
    if hello.get("event") != "ms.channel.connect":
        ws.close()
        raise RuntimeError(f"TV refused connection: {hello}")
    # The TV issues a new token if the old one was rejected; keep it.
    new_tok = (hello.get("data") or {}).get("token")
    if new_tok and new_tok != _token():
        with open(os.path.join(HERE, "tv_token.txt"), "w") as f:
            f.write(new_tok)
    return ws


def send_key(key, ws=None):
    own = ws is None
    ws = ws or _connect()
    try:
        ws.send(json.dumps({"method": "ms.remote.control", "params": {
            "Cmd": "Click", "DataOfCmd": key, "Option": "false",
            "TypeOfRemote": "SendRemoteKey"}}))
    finally:
        if own:
            ws.close()


def send_keys(keys, gap=0.08):
    """Send several keys over one connection (e.g. to walk the browser pointer)."""
    ws = _connect()
    try:
        for k in keys:
            send_key(k, ws)
            time.sleep(gap)
    finally:
        ws.close()


def park_pointer():
    """Push the Tizen browser's on-screen pointer into the bottom-right corner."""
    send_keys(["KEY_RIGHT"] * 30 + ["KEY_DOWN"] * 20)


def open_browser(url):
    ws = _connect()
    try:
        ws.send(json.dumps({"method": "ms.channel.emit", "params": {
            "event": "ed.apps.launch", "to": "host", "data": {
                "appId": "org.tizen.browser", "action_type": "NATIVE_LAUNCH",
                "metaTag": url}}}))
    finally:
        ws.close()


def wake():
    """Wake-on-LAN magic packet. Only works if the TV has 'Power On with Mobile' enabled."""
    mac = bytes.fromhex(TV_MAC.replace(":", ""))
    pkt = b"\xff" * 6 + mac * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for addr in ("255.255.255.255", "192.168.1.255", TV_IP):
            s.sendto(pkt, (addr, 9))


def status():
    """Return the TV's device info dict, or None if the TV is off/unreachable."""
    try:
        with urllib.request.urlopen(f"http://{TV_IP}:8001/api/v2/", timeout=2) as r:
            d = json.load(r).get("device", {})
        return {"on": d.get("PowerState", "on") == "on", "name": d.get("name"),
                "model": d.get("modelName")}
    except Exception:
        return None


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "key":
        send_key(sys.argv[2])
    elif cmd == "open":
        open_browser(sys.argv[2])
    elif cmd == "park":
        park_pointer()
    elif cmd == "wake":
        wake()
    else:
        print(status())

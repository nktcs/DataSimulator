"""
Example consuming application. Shows both integration patterns:
  1. REST pull  - request a single reading on demand
  2. WebSocket  - subscribe to a continuous stream (like a ROS2 topic)

Run the server first:
    uvicorn app.main:app --port 8000

Then:
    python client_example.py rest
    python client_example.py stream vibration
    python client_example.py stream all
"""
import asyncio
import json
import sys

import requests
import websockets

BASE_URL = "http://localhost:8000"
WS_URL = "ws://localhost:8000"


def rest_demo():
    print("-- starting a new inspection run --")
    run = requests.post(f"{BASE_URL}/inspection/start").json()
    print(run)

    print("\n-- setting locomotion_state=walking, anomaly_mode=True --")
    requests.post(f"{BASE_URL}/config", json={"locomotion_state": "walking", "anomaly_mode": True})

    print("\n-- single sensor sample, targeted at a specific asset --")
    r = requests.get(f"{BASE_URL}/sensors/gas/sample", params={"asset_id": "Valve-V22"}).json()
    print(json.dumps(r, indent=2))

    print("\n-- snapshot across all sensors (random asset per sensor) --")
    r = requests.get(f"{BASE_URL}/sensors/all/sample").json()
    for sensor_type, reading in r.items():
        print(f"  {sensor_type}: asset={reading['asset_id']} "
              f"checkpoint={reading['checkpoint_status']} quality={reading['quality_flag']}")


async def stream_demo(sensor_type="vibration", n_messages=10):
    uri = f"{WS_URL}/sensors/{sensor_type}/stream"
    print(f"-- connecting to {uri} --")
    async with websockets.connect(uri) as ws:
        for _ in range(n_messages):
            msg = await ws.recv()
            reading = json.loads(msg)
            print(f"[{reading['sensor_type']}] asset={reading['asset_id']} "
                  f"checkpoint={reading['checkpoint_status']} quality={reading['quality_flag']} "
                  f"run={reading['inspection_run_id']}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "rest"
    if mode == "rest":
        rest_demo()
    elif mode == "stream":
        sensor = sys.argv[2] if len(sys.argv) > 2 else "vibration"
        asyncio.run(stream_demo(sensor))
    else:
        print("Usage: python client_example.py [rest | stream <sensor_type>]")

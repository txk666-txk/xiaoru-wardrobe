"""End-to-end check for Fay -> GPT-SoVITS -> digital-human audio WebSocket."""

import asyncio
import json

import requests
import websockets


FAY_HTTP = "http://127.0.0.1:5000"
FAY_HUMAN_WS = "ws://127.0.0.1:10002"
TEST_USER = "User"
TEST_TEXT = "你好，小如网页语音链路验证成功。"


async def main():
    async with websockets.connect(FAY_HUMAN_WS) as websocket:
        await websocket.send(json.dumps({"Username": TEST_USER, "Output": True}))
        await asyncio.sleep(0.2)

        response = await asyncio.to_thread(
            requests.post,
            f"{FAY_HTTP}/transparent-pass",
            json={"user": TEST_USER, "text": TEST_TEXT},
            timeout=10,
        )
        response.raise_for_status()
        print("transparent-pass:", response.json())

        while True:
            raw_message = await asyncio.wait_for(websocket.recv(), timeout=120)
            message = json.loads(raw_message)
            data = message.get("Data") or {}
            if message.get("Topic") != "human" or data.get("Key") != "audio":
                continue

            audio_url = data.get("HttpValue")
            if not audio_url:
                continue
            audio_response = await asyncio.to_thread(requests.get, audio_url, timeout=30)
            audio_response.raise_for_status()
            if not audio_response.content.startswith(b"RIFF"):
                raise RuntimeError("Fay audio URL did not return a WAV file")

            print(
                "chain-ok:",
                json.dumps(
                    {
                        "audio_url": audio_url,
                        "audio_bytes": len(audio_response.content),
                        "text": data.get("Text"),
                    },
                    ensure_ascii=False,
                ),
            )
            return


if __name__ == "__main__":
    asyncio.run(main())

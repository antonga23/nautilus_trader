import asyncio
import websockets


async def hello():
    uri = "ws://localhost:8765"
    async with websockets.connect(uri) as websocket:
        while True:
            # Wait for a response
            response = await websocket.recv()
            if response == "heartbeat":
                print("< heartbeat received!")
            # Send a message
            await websocket.send("Hello Server!")
            print("> Hello Server!")
            await asyncio.sleep(20)  # Send heartbeat every 5 seconds
            print(f"< {response}")


asyncio.get_event_loop().run_until_complete(hello())

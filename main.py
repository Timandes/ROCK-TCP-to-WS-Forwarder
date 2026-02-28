#!/usr/bin/env python3
"""TCP to WebSocket forwarder with persistent WebSocket connection."""

import argparse
import asyncio
import signal
import sys

import websockets


class WebSocketManager:
    """Manages a persistent WebSocket connection with auto-reconnect."""

    MAX_RETRIES = 3
    RECONNECT_INTERVAL = 3
    SANDBOX_NOT_STARTED_KEYWORDS = ["沙箱未启动", "sandbox not started", "not found"]

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.ws: websockets.ClientConnection | None = None
        self._connect_event = asyncio.Event()
        self._disconnect_event = asyncio.Event()
        self._running = False
        self._reconnect_task: asyncio.Task | None = None
        self._pending_writers: set[asyncio.StreamWriter] = set()
        self._retry_count = 0

    def _is_sandbox_not_started(self, error: Exception) -> bool:
        """Check if error indicates sandbox not started."""
        error_str = str(error).lower()
        return any(kw in error_str for kw in [kw.lower() for kw in self.SANDBOX_NOT_STARTED_KEYWORDS])

    async def _do_connect(self) -> bool:
        """Attempt to connect. Returns True on success."""
        try:
            self.ws = await websockets.connect(self.ws_url)
            return True
        except Exception as e:
            if self._is_sandbox_not_started(e):
                print(f"Sandbox not started, exiting: {e}")
                sys.exit(1)
            raise

    async def start(self) -> None:
        """Start the WebSocket connection. Exit on max retries exceeded."""
        self._running = True
        self._reconnect_task = asyncio.create_task(self._connect_loop())
        # Wait for initial connection
        await self._connect_event.wait()

    async def _connect_loop(self) -> None:
        """Handle connection and reconnection with max 3 retries total."""
        while self._running:
            if self.ws is None:
                if self._retry_count >= self.MAX_RETRIES:
                    print(f"Max retries ({self.MAX_RETRIES}) reached, exiting")
                    sys.exit(1)

                self._retry_count += 1
                remaining = self.MAX_RETRIES - self._retry_count
                print(f"Connecting to WebSocket... (attempt {self._retry_count}/{self.MAX_RETRIES}, {remaining} retries left)")
                try:
                    await self._do_connect()
                    self._retry_count = 0  # Reset retry count on success
                    print(f"WebSocket connected: {self.ws_url}")
                    self._connect_event.set()
                    self._disconnect_event.clear()
                except Exception as e:
                    print(f"Connection failed: {e}")
                    if self._retry_count < self.MAX_RETRIES:
                        print(f"Retrying in {self.RECONNECT_INTERVAL}s...")
                        await asyncio.sleep(self.RECONNECT_INTERVAL)
                    continue
            else:
                try:
                    await self.ws.wait_closed()
                    print("WebSocket connection lost")
                    self.ws = None
                    self._connect_event.clear()
                    self._disconnect_event.set()
                except Exception:
                    pass

    async def wait_connected(self) -> None:
        """Wait until WebSocket is connected."""
        await self._connect_event.wait()

    async def send(self, data: bytes) -> None:
        """Send data through WebSocket."""
        if self.ws:
            await self.ws.send(data)

    async def receive_loop(self, writer: asyncio.StreamWriter) -> None:
        """Receive data from WebSocket and forward to TCP client."""
        self._pending_writers.add(writer)
        try:
            while self._running and self.ws:
                try:
                    async for message in self.ws:
                        if isinstance(message, bytes):
                            writer.write(message)
                            await writer.drain()
                except websockets.ConnectionClosed:
                    # Connection lost, wait for reconnect
                    await self._disconnect_event.wait()
                    if not self._running:
                        break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"WS receive error: {e}")
        finally:
            self._pending_writers.discard(writer)

    async def stop(self) -> None:
        """Stop the manager."""
        self._running = False
        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
        if self.ws:
            await self.ws.close()


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    ws_manager: WebSocketManager,
) -> None:
    """Handle a single TCP client connection."""
    client_addr = writer.get_extra_info('peername')
    print(f"TCP client connected: {client_addr}")

    # Wait for WebSocket to be connected
    await ws_manager.wait_connected()

    # Start receive task
    recv_task = asyncio.create_task(ws_manager.receive_loop(writer))

    async def forward_tcp_to_ws():
        """Forward data from TCP to WebSocket."""
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                await ws_manager.send(data)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"TCP read error: {e}")

    send_task = asyncio.create_task(forward_tcp_to_ws())

    try:
        # Wait for either direction to complete
        done, pending = await asyncio.wait(
            [recv_task, send_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    finally:
        writer.close()
        await writer.wait_closed()
        print(f"TCP client disconnected: {client_addr}")


async def main(sandbox_id: str | None, port: int = 8888, ws_port: int = 8080) -> None:
    """Start the TCP server."""
    if sandbox_id:
        ws_url = f"ws://localhost:{ws_port}/apis/envs/sandbox/v1/sandboxes/{sandbox_id}/portforward?port=9999"
    else:
        ws_url = f"ws://localhost:{ws_port}/portforward?port=9999"

    # Create and start WebSocket manager
    ws_manager = WebSocketManager(ws_url)
    await ws_manager.start()

    # Start TCP server
    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, ws_manager),
        "0.0.0.0",
        port,
    )

    addr = server.sockets[0].getsockname()
    print(f"TCP server listening on {addr[0]}:{addr[1]}")
    if sandbox_id:
        print(f"Forwarding to sandbox: {sandbox_id}")
    else:
        print("Forwarding to local portforward")

    # Setup signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def signal_handler():
        print("\nShutting down...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    async with server:
        # Run until stop signal
        await stop_event.wait()
        server.close()
        await server.wait_closed()

    # Cleanup
    await ws_manager.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TCP to WebSocket forwarder")
    parser.add_argument(
        "sandbox_id",
        nargs="?",
        default=None,
        help="Sandbox ID for WebSocket URL (optional)",
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=8888,
        help="TCP port to listen on (default: 8888)",
    )
    parser.add_argument(
        "-w",
        "--ws-port",
        type=int,
        default=8080,
        help="WebSocket server port (default: 8080)",
    )

    args = parser.parse_args()
    asyncio.run(main(args.sandbox_id, args.port, args.ws_port))

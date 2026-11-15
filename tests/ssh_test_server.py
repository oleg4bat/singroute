"""Pinned loopback SSH transport shared by router and GUI tests."""

import socket
import threading
from contextlib import contextmanager

import paramiko


@contextmanager
def loopback_ssh_client(
    router, *, server_auth="password", client_options=None, connect=True
):
    """Use the product's SSH client through a real local SSH transport."""
    from singroute.infrastructure.ssh_router import SshRouterClient

    key = paramiko.RSAKey.generate(2048)
    host_key = f"{key.get_name()} {key.get_base64()}"
    key_will_be_rejected = (client_options or {}).get(
        "trusted_host_key", host_key
    ) != host_key
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    transport_holder = []
    channels = []
    workers = []
    router.auth_attempts = []

    class Server(paramiko.ServerInterface):
        def check_auth_none(self, username):
            router.auth_attempts.append("none")
            return (
                paramiko.AUTH_SUCCESSFUL
                if server_auth == "none" and username == "root"
                else paramiko.AUTH_FAILED
            )

        def check_auth_password(self, username, password):
            router.auth_attempts.append("password")
            expected = "" if server_auth == "empty-password" else "test-password"
            return (
                paramiko.AUTH_SUCCESSFUL
                if server_auth != "none" and (username, password) == ("root", expected)
                else paramiko.AUTH_FAILED
            )

        def get_allowed_auths(self, username):
            return "password"

        def check_channel_request(self, kind, chanid):
            return (
                paramiko.OPEN_SUCCEEDED
                if kind == "session"
                else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
            )

        def check_channel_exec_request(self, channel, command):
            def execute():
                command_text = command.decode("utf-8")
                try:
                    # EOF also guarantees the exec-request acceptance reached
                    # the client before this worker sends output/exit status.
                    data = bytearray()
                    while chunk := channel.recv(32768):
                        data.extend(chunk)
                    content = data.decode("utf-8") if data else None
                    result = router.run(command_text, content)
                    if result.stdout:
                        channel.sendall(result.stdout.encode("utf-8"))
                    if result.stderr:
                        channel.sendall_stderr(result.stderr.encode("utf-8"))
                    channel.send_exit_status(result.exit_code)
                    channel.shutdown_write()
                finally:
                    channel.close()

            worker = threading.Thread(target=execute, daemon=True)
            workers.append(worker)
            worker.start()
            return True

    def serve():
        connection, _ = listener.accept()
        transport = paramiko.Transport(connection)
        transport_holder.append(transport)
        transport.add_server_key(key)
        try:
            transport.start_server(server=Server())
        except (EOFError, paramiko.SSHException):
            # Host-key refusal may close the connection before start_server
            # observes that key exchange finished. The client test checks the
            # specific UnknownHostKeyError/HostKeyMismatchError itself.
            if key_will_be_rejected:
                return
            raise
        while transport.is_active():
            channel = transport.accept(timeout=0.1)
            if channel is not None:
                channels.append(channel)

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    progress = []
    options = {
        "port": port,
        "password": "test-password",
        "auth_mode": "password",
        "trusted_host_key": host_key,
        "progress_callback": progress.append,
        "command_timeout": 20,
        **(client_options or {}),
    }
    client = SshRouterClient("127.0.0.1", **options)
    try:
        if connect:
            client.connect()
        yield client, progress
    finally:
        client.close()
        for transport in transport_holder:
            transport.close()
        listener.close()
        server.join(timeout=5)
        for worker in workers:
            worker.join(timeout=5)

"""Fixed QEMU guestfwd bridge. No arguments or model-selected destinations."""
import os
import select
import socket

with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as remote:
    remote.connect("/run/talos-computer-egress/proxy.sock")
    remote.settimeout(120)
    while True:
        ready, _, _ = select.select([0, remote], [], [], 120)
        if not ready:
            break
        for source in ready:
            data = os.read(0, 65536) if source == 0 else remote.recv(65536)
            if not data:
                raise SystemExit(0)
            if source == 0:
                remote.sendall(data)
            else:
                view = memoryview(data)
                while view:
                    view = view[os.write(1, view):]

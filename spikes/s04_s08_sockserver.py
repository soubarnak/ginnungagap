#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
#
# S-08 helper (CL-5): unix stream server that answers each connection with its generation
# token and its pid, so a probe can tell which server instance answered.
#
#   python3 -I sockserver.py <socket path> <token>
#
# An existing file at the path is unlinked first. The old inode is left alone, so a
# container that still holds it keeps talking to whatever listens on that inode.
import os
import socket
import sys

path, token = sys.argv[1], sys.argv[2]
if os.path.lexists(path):
    os.unlink(path)
    print(f"{token}: unlinked existing {path}", flush=True)
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(path)
srv.listen(8)
print(f"{token}: listening on {path} pid={os.getpid()}", flush=True)
while True:
    conn, _ = srv.accept()
    with conn:
        conn.sendall(f"{token} pid={os.getpid()}\n".encode())

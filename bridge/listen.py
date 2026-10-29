#!/usr/bin/env python3
"""Test listener for the BuckshotCalc bridge: accepts the mod, prints each line, checks it is JSON with v and ev."""
import json, socket, sys

port = int(sys.argv[1]) if len(sys.argv) > 1 else 47600
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", port))
srv.listen(1)
n = bad = 0
while True:
    conn, _ = srv.accept()
    print("# connected", flush=True)
    buf = b""
    while True:
        data = conn.recv(65536)
        if not data:
            break
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            n += 1
            try:
                m = json.loads(line)
                assert isinstance(m, dict) and "v" in m and "ev" in m
                if m["ev"] == "hello":
                    conn.sendall(b'{"v":1,"ev":"hello_ack","ok":true,"known":true}\n')
            except Exception as e:
                bad += 1
                print("# BAD LINE (%s): %r" % (e, line), flush=True)
            print(line.decode(errors="replace"), flush=True)
    print("# disconnected: %d lines, %d bad" % (n, bad), flush=True)

#!/usr/bin/env python3
"""Create a virtual uinput device for m5_check.py (needs root).

Usage: m5_uinput.py gamepad|keyboard. Prints "inputN [event nodes]" and keeps the
device until SIGTERM/SIGINT, which destroys it.
"""
import fcntl, glob, os, signal, struct, sys, time

def ioc(d, t, nr, size): return (d << 30) | (size << 16) | (ord(t) << 8) | nr
W, R = 1, 2
UI_DEV_CREATE = ioc(0, 'U', 1, 0); UI_DEV_DESTROY = ioc(0, 'U', 2, 0)
UI_DEV_SETUP = ioc(W, 'U', 3, 92); UI_ABS_SETUP = ioc(W, 'U', 4, 28)
UI_SET_EVBIT = ioc(W, 'U', 100, 4); UI_SET_KEYBIT = ioc(W, 'U', 101, 4); UI_SET_ABSBIT = ioc(W, 'U', 103, 4)
UI_GET_SYSNAME = ioc(R, 'U', 44, 64)
EV_KEY, EV_ABS = 1, 3
kind = sys.argv[1]
fd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)
fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
if kind == 'gamepad':
    for code in range(0x130, 0x13a): fcntl.ioctl(fd, UI_SET_KEYBIT, code)   # BTN_SOUTH.. BTN_GAMEPAD
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_ABS)
    for axis in (0, 1, 3, 4):
        fcntl.ioctl(fd, UI_SET_ABSBIT, axis)
        fcntl.ioctl(fd, UI_ABS_SETUP, struct.pack('=HHiiiiii', axis, 0, 0, -32768, 32767, 16, 128, 0))
    name = b'ggm5 test gamepad'
elif kind == 'keyboard':
    for code in range(1, 120): fcntl.ioctl(fd, UI_SET_KEYBIT, code)           # KEY_ESC .. letters
    name = b'ggm5 test keyboard'
else:
    raise SystemExit('kind: gamepad|keyboard')
fcntl.ioctl(fd, UI_DEV_SETUP, struct.pack('=HHHH80sI', 3, 0x1234, 0x5678 if kind == 'gamepad' else 0x5679, 1, name, 0))
fcntl.ioctl(fd, UI_DEV_CREATE)
buf = fcntl.ioctl(fd, UI_GET_SYSNAME, b'\0' * 64)
sysname = buf.split(b'\0')[0].decode()
time.sleep(0.3)
nodes = sorted(glob.glob(f'/sys/devices/virtual/input/{sysname}/event*') + glob.glob(f'/sys/devices/virtual/input/{sysname}/js*'))
print(sysname, [os.path.basename(n) for n in nodes], flush=True)
def bye(*_):
    fcntl.ioctl(fd, UI_DEV_DESTROY); os.close(fd); sys.exit(0)
signal.signal(signal.SIGTERM, bye); signal.signal(signal.SIGINT, bye)
while True: time.sleep(1)

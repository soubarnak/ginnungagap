import ctypes, os, sys
libc = ctypes.CDLL(None, use_errno=True)
def sc(n,*a):
    r = libc.syscall(n,*[ctypes.c_long(x) if isinstance(x,int) else x for x in a])
    if r<0: e=ctypes.get_errno(); raise OSError(e, os.strerror(e))
    return r
OPEN_TREE=428; MOVE_MOUNT=429
OPEN_TREE_CLONE=1; OPEN_TREE_CLOEXEC=os.O_CLOEXEC; AT_RECURSIVE=0x8000
MOVE_MOUNT_F_EMPTY_PATH=4
pid, src, dest = int(sys.argv[1]), sys.argv[2], sys.argv[3]
tfd = sc(OPEN_TREE, -100, src.encode(), OPEN_TREE_CLONE|OPEN_TREE_CLOEXEC|AT_RECURSIVE)
nsfd = os.open(f"/proc/{pid}/ns/mnt", os.O_RDONLY)
if os.fork()==0:
    if libc.setns(nsfd, 0x00020000)!=0: print("setns fail", os.strerror(ctypes.get_errno())); os._exit(1)
    os.makedirs(dest, exist_ok=True)
    sc(MOVE_MOUNT, tfd, b"", -100, dest.encode(), MOVE_MOUNT_F_EMPTY_PATH)
    os._exit(0)
_, st = os.wait(); print("child status", st)

from pathlib import Path

from ytop import fsinfo
from ytop.fsinfo import FAT32_MAX_FILE, fits_on, fs_info


def test_fs_info_parses_mounts(monkeypatch):
    monkeypatch.setattr(
        fsinfo,
        "_read_mounts",
        lambda: "/dev/sda1 / ext4 rw 0 0\n/dev/sdb1 /run/media/u/STICK vfat rw 0 0\n",
    )
    p = Path("/run/media/u/STICK/lib")
    info = fs_info(p)
    assert info.fstype == "vfat"
    assert info.max_file_bytes == FAT32_MAX_FILE
    assert fits_on(p, 3_900_000_000) is False
    assert fits_on(p, None) is True
    assert fs_info(Path("/home/x")).max_file_bytes is None


def test_fs_info_octal_escapes(monkeypatch):
    monkeypatch.setattr(
        fsinfo,
        "_read_mounts",
        lambda: "/dev/sda1 / ext4 rw 0 0\n/dev/sdb1 /run/media/u/MY\\040STICK vfat rw 0 0\n",
    )
    assert fs_info(Path("/run/media/u/MY STICK/x")).fstype == "vfat"


def test_fs_info_darwin_msdos(monkeypatch):
    monkeypatch.setattr(fsinfo.sys, "platform", "darwin")
    for name, expect, limit in [
        ("msdos", "vfat", FAT32_MAX_FILE),
        ("exfat", "exfat", None),
        ("apfs", "apfs", None),
        ("hfs", "hfs", None),
    ]:
        monkeypatch.setattr(fsinfo, "_darwin_fstype", lambda p, n=name: n)
        info = fs_info(Path("/Volumes/X"))
        assert (info.fstype, info.max_file_bytes) == (expect, limit)


def test_fs_info_windows_fat32_and_exfat(monkeypatch):
    monkeypatch.setattr(fsinfo.sys, "platform", "win32")
    for name, expect, limit in [
        ("FAT32", "vfat", FAT32_MAX_FILE),
        ("exFAT", "exfat", None),
        ("NTFS", "ntfs", None),
        ("ReFS", "refs", None),
    ]:
        monkeypatch.setattr(fsinfo, "_windows_fstype", lambda p, n=name: n)
        info = fs_info(Path("D:/"))
        assert (info.fstype, info.max_file_bytes) == (expect, limit)
    monkeypatch.setattr(fsinfo, "_windows_fstype", lambda p: "FAT")
    assert fs_info(Path("D:/")).fstype == "vfat"


def test_fs_info_errors_unknown(monkeypatch):
    monkeypatch.setattr(fsinfo.sys, "platform", "darwin")
    monkeypatch.setattr(fsinfo, "_darwin_fstype", lambda p: None)
    assert fs_info(Path("/x")) == fsinfo.FsInfo("unknown", None)

    def boom(p):
        raise OSError("x")

    monkeypatch.setattr(fsinfo.sys, "platform", "win32")
    monkeypatch.setattr(fsinfo, "_windows_fstype", boom)
    assert fs_info(Path("D:/")) == fsinfo.FsInfo("unknown", None)


def test_statfs_prefers_inode64():
    class Libc:
        pass

    libc = Libc()
    libc.statfs = lambda *a: 0
    plain = fsinfo._statfs_func(libc)
    assert plain is libc.statfs
    libc.__dict__["statfs$INODE64"] = lambda *a: 0
    assert fsinfo._statfs_func(libc) is libc.__dict__["statfs$INODE64"]


def test_refused_root_posix_and_windows(monkeypatch):
    from pathlib import PurePosixPath, PureWindowsPath

    monkeypatch.setenv("SystemDrive", "C:")
    assert fsinfo.refused_root(PurePosixPath("/"), windows=False)
    assert not fsinfo.refused_root(PurePosixPath("/run/media/u/S"), windows=False)
    # A USB stick on Windows is reached as its drive root: allowed.
    assert not fsinfo.refused_root(PureWindowsPath("E:\\"), windows=True)
    assert not fsinfo.refused_root(PureWindowsPath("e:/"), windows=True)
    assert not fsinfo.refused_root(PureWindowsPath("E:\\YTOP"), windows=True)
    # ...but never the system drive.
    assert fsinfo.refused_root(PureWindowsPath("C:\\"), windows=True)
    assert fsinfo.refused_root(PureWindowsPath("c:\\"), windows=True)
    assert not fsinfo.refused_root(PureWindowsPath("C:\\Users\\x\\S"), windows=True)
    assert fsinfo.refused_root(PureWindowsPath("\\"), windows=True)
    monkeypatch.setenv("SystemDrive", "D:")
    assert fsinfo.refused_root(PureWindowsPath("D:\\"), windows=True)
    assert not fsinfo.refused_root(PureWindowsPath("C:\\"), windows=True)


def test_refused_root_follows_sys_platform(monkeypatch):
    from pathlib import PureWindowsPath

    monkeypatch.setenv("SystemDrive", "C:")
    monkeypatch.setattr(fsinfo.sys, "platform", "win32")
    assert not fsinfo.refused_root(PureWindowsPath("E:\\"))
    assert fsinfo.refused_root(PureWindowsPath("C:\\"))
    monkeypatch.setattr(fsinfo.sys, "platform", "linux")
    assert fsinfo.refused_root(Path("/"))

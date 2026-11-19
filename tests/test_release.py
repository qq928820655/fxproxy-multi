"""发行包结构测试（tools/build_release.py）。

不打网络、不依赖 dist/ 里已有的包：每次在临时目录现场构建再校验，
所以 gitignore 掉 dist 也不影响。

运行: python tests/test_release.py
"""
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.build_release import (REQUIRED, build, build_tar, verify, verify_tar,  # noqa: E402
                                 version)
from tools.build_release import main as build_main  # noqa: E402

PASS = 0


def check(cond, msg):
    global PASS
    assert cond, msg
    PASS += 1
    print(f"  ok  {msg}")


def main():
    ver = version()
    top = f"fxproxy-{ver}"
    print(f"[release] 现场构建 v{ver}")
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / f"{top}-win.zip"
        meta = build(out, top)
        check(out.is_file() and meta["count"] > 20, f"构建出 {meta['count']} 个文件")

        ok, problems = verify(out, top)
        check(ok, f"结构校验通过（{problems or '无问题'}）")

        names = zipfile.ZipFile(out).namelist()
        check(all(n.startswith(top + "/") for n in names), "所有条目在唯一顶层目录下")
        for req in REQUIRED:
            check(f"{top}/{req}" in names, f"包含 {req}")
        check(not any("__pycache__" in n or n.endswith(".pyc") for n in names),
              "不含 pyc 缓存")
        check(not any(n.endswith(".crx-key.pem") for n in names), "不含 CRX 签名私钥")
        check(not any(n.rsplit("/", 1)[-1] == "config.json" for n in names),
              "不含真实 config.json（凭证）")
        check(f"{top}/fxproxy-chrome-extension.zip" in names, "含扩展 zip")
        check(f"{top}/fxproxy-chrome-extension.crx" in names, "含扩展 crx")

        print("[release] 校验能抓出坏包")
        # 少一个必需文件
        broken = Path(d) / "broken.zip"
        with zipfile.ZipFile(out) as src, zipfile.ZipFile(broken, "w") as dst:
            for n in src.namelist():
                if n == f"{top}/pyproject.toml":
                    continue
                dst.writestr(n, src.read(n))
        ok2, probs2 = verify(broken, top)
        check(not ok2 and any("pyproject.toml" in p for p in probs2), "缺 pyproject 报错")

        # 多套一层 / 平铺
        flat = Path(d) / "flat.zip"
        with zipfile.ZipFile(out) as src, zipfile.ZipFile(flat, "w") as dst:
            for n in src.namelist():
                dst.writestr(n.split("/", 1)[1], src.read(n))
        ok3, probs3 = verify(flat, top)
        check(not ok3 and any("顶层目录" in p for p in probs3), "平铺包被拒")

        # 混进私钥
        dirty = Path(d) / "dirty.zip"
        with zipfile.ZipFile(out) as src, zipfile.ZipFile(dirty, "w") as dst:
            for n in src.namelist():
                dst.writestr(n, src.read(n))
            dst.writestr(f"{top}/tools/.crx-key.pem", b"secret")
        ok4, probs4 = verify(dirty, top)
        check(not ok4 and any("crx-key" in p for p in probs4), "私钥混入被拒")

        print("[release] Linux tar.gz")
        tar_out = Path(d) / f"{top}-linux.tar.gz"
        meta_t = build_tar(tar_out, top)
        check(tar_out.is_file() and meta_t["count"] > 20, f"tar 构建出 {meta_t['count']} 个文件")
        ok_t, probs_t = verify_tar(tar_out, top)
        check(ok_t, f"tar 结构校验通过（{probs_t or '无问题'}）")
        with tarfile.open(tar_out) as tf:
            members = tf.getmembers()
            names_t = {m.name for m in members if m.isfile()}
            shs = [m for m in members if m.name.endswith(".sh")]
            check(len(shs) >= 2, f"tar 里有两个 Linux 脚本（实得 {len(shs)}）")
            check(all(m.mode & 0o111 for m in shs), "tar 里 .sh 都是 0755")
            check(all(b"\r\n" not in tf.extractfile(m).read() for m in shs),
                  "tar 里 .sh 都是 LF")
            check(all(m.uid == 0 and m.gid == 0 for m in members), "tar 归零 uid/gid")
        for req in ("deploy/linux/install.sh", "deploy/linux/fxproxy-ctl.sh",
                    "deploy/windows/install.bat"):
            check(f"{top}/{req}" in names_t, f"tar 包含 {req}")

        # 缺 install.sh 的 tar 必须被拒
        broken_tar = Path(d) / "broken.tar.gz"
        with tarfile.open(tar_out) as src, tarfile.open(broken_tar, "w:gz") as dst:
            for m in src.getmembers():
                if m.name.endswith("deploy/linux/install.sh"):
                    continue
                dst.addfile(m, src.extractfile(m) if m.isfile() else None)
        ok_bt, probs_bt = verify_tar(broken_tar, top)
        check(not ok_bt and any("install.sh" in p for p in probs_bt), "tar 缺 install.sh 被拒")

    # 版本只体现在包名上：包内顶层目录固定 fxproxy，升级时解压覆盖同名目录即可
    print("\n[pkg] 包内顶层目录固定")
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "fixed.zip"
        rc = build_main(["--target", "win", "-o", str(out)])
        ok_fixed, _ = verify(out, "fxproxy")
        check(rc == 0 and ok_fixed, "顶层目录是 fxproxy（不带版本号）")
        with zipfile.ZipFile(out) as zf:
            tops = {n.split("/", 1)[0] for n in zf.namelist()}
        check(tops == {"fxproxy"}, f"顶层唯一且无版本号（实得 {sorted(tops)}）")

    print(f"\nALL PASS ({PASS} checks)")


if __name__ == "__main__":
    main()

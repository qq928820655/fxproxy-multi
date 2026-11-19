"""发行包结构测试（tools/build_release.py）。

不打网络、不依赖 dist/ 里已有的包：每次在临时目录现场构建再校验，
所以 gitignore 掉 dist 也不影响。

运行: python tests/test_release.py
"""
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.build_release import REQUIRED, build, verify, version  # noqa: E402

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

    print(f"\nALL PASS ({PASS} checks)")


if __name__ == "__main__":
    main()

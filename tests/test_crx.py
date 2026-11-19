"""CRX 打包回归：内部结构必须平铺在根目录、签名必须有效。

运行: python tests/test_crx.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import build_crx as bc

PASS = 0


def check(cond, msg):
    global PASS
    assert cond, msg
    PASS += 1
    print(f"  ok  {msg}")


def main():
    print("[crx] 从源码现场构建")
    zip_bytes = bc.build_zip()
    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
    check(all("/" not in n for n in names), "zip 条目无子目录")
    check(set(names) == set(bc.EXT_FILES), "zip 恰含 5 个扩展文件，无多余嵌套")

    crx, meta = bc.build_crx3(zip_bytes)
    check(crx[:4] == b"Cr24", "CRX magic = Cr24")
    import struct
    check(struct.unpack("<I", crx[4:8])[0] == 3, "CRX 版本 = 3")
    info = bc.verify_crx(crx)
    check(info["signature_valid"] is True, "CRX3 RSA-SHA256 签名验证通过")
    check(info["crx_id"] == meta["crx_id"], "crx_id 一致")

    print("[crx] 仓库内产出的 .crx")
    out = ROOT / "fxproxy-chrome-extension.crx"
    if out.exists():
        shipped = bc.verify_crx(out.read_bytes())
        check(all("/" not in n for n in shipped["entries"]), "随仓库分发的 .crx 也是平铺结构")
        check(shipped.get("signature_valid") is True, "随仓库分发的 .crx 签名有效")
    else:
        print("  --  未找到随仓库分发的 .crx（跳过）")

    # 反向用例：人为套一层目录，verify 必须拒绝
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
        for n in bc.EXT_FILES:
            zf.writestr("fxproxy-chrome-extension/" + n, b"x")
    nested, _ = bc.build_crx3(bio.getvalue())
    try:
        bc.verify_crx(nested)
        nested_rejected = False
    except ValueError:
        nested_rejected = True
    check(nested_rejected, "嵌套目录的 CRX 被 verify 拒绝（正是报错根因）")

    print(f"\nALL CRX TESTS PASSED ({PASS} checks)")


if __name__ == "__main__":
    main()

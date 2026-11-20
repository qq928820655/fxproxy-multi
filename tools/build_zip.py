#!/usr/bin/env python3
"""打包 Chrome 扩展 zip，结构对齐 VirtualBrowser 已成功安装的参考包。

VirtualBrowser 的插件上传通道按 **zip 内唯一顶层目录名** 创建扩展目录：

    zip:  <top>/manifest.json          ← 必须有这一层
    disk: extensions/<top>/manifest.json

平铺 zip（manifest.json 直接在根）会让它解出错误的前缀，然后按 manifest 的
content_scripts 去 <name>/content.js 找文件，报：

    ENOENT: no such file or directory, open
    '...\\User Data\\extensions\\fxproxy-chrome-extension\\content.js'

所以包内必须带一层目录，且该目录名与扩展目录名一致。

用法：
    python tools/build_zip.py                 # 生成 dist zip（默认 fxproxy-chrome-extension.zip）
    python tools/build_zip.py --verify FILE   # 校验结构

依赖：无（标准库）
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "chrome-extension"
DEFAULT_OUT = ROOT / "fxproxy-chrome-extension.zip"
DEFAULT_TOP = "fxproxy-chrome-extension"

# manifest 里可能引用到的字段 -> 需要一并打进包的文件
MANIFEST_FILE_KEYS = ("js", "css", "default_popup", "default_icon", "icons", "service_worker")

# 参考包（mc-cookie-grabber-v1.3.5）顶层目录里也带一份 INSTALL.txt，照做
INSTALL_TEXT = """fxproxy — FxA Token Extractor
============================

安装（VirtualBrowser / 指纹浏览器）
-----------------------------------
1. 插件管理 -> 安装插件
2. 插件来源选「本地CRX文件」，或直接把本 zip 拖到上传框
3. 装好后在插件列表里点「配置使用环境」，勾选要用它的环境

注意：zip 内必须保留顶层目录 fxproxy-chrome-extension/，
manifest.json 在这一层里面。平铺 zip 会让浏览器解压路径错位，
报 ENOENT ...\\extensions\\fxproxy-chrome-extension\\content.js。

安装（普通 Chrome）
-------------------
chrome://extensions -> 开发者模式 -> 加载已解压的扩展程序 -> 选解压出来的
fxproxy-chrome-extension 目录。

使用
----
1. 在住宅 IP 的浏览器里登录 https://accounts.firefox.com
2. 点插件图标，在「导出格式」里选一种
3. 点「提取并复制凭证」
4. 回到 fxproxy 面板「添加账号」，方式选与导出相同的那一项，粘贴

导出格式与面板「方式」的对应
------------------------------
Cookie 头（默认）  ->  方式选「Cookie / 插件导出」
sessionToken       ->  方式选「sessionToken」
邮箱               ->  方式选「邮箱 + 密码」（密码自己补）

为什么没有 refresh_token：面板第 1 项「直接粘贴 refresh_token」是给
手上已经有 refresh_token 的人用的。插件只能拿到 sessionToken，
换成 refresh_token 需要 Hawk 签名 + PKCE 两步，浏览器端做不到，
硬做只会返回 401 "Invalid authentication token in request signature"。
所以这一步放在面板里做，插件不重复。

前提：面板所在机器要装了登录依赖，否则会报 No module named 'curl_cffi'。
    python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple "curl_cffi>=0.7" "pyfxa>=0.8"
装完重启服务进程。
"""


def _collect_manifest_refs(manifest: dict) -> set[str]:
    """从 manifest 里抓出所有被引用的相对路径，用于完整性校验。"""
    refs: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in MANIFEST_FILE_KEYS:
                    if isinstance(v, str):
                        refs.add(v)
                    elif isinstance(v, dict):
                        for vv in v.values():
                            if isinstance(vv, str):
                                refs.add(vv)
                    elif isinstance(v, list):
                        for vv in v:
                            if isinstance(vv, str):
                                refs.add(vv)
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(manifest)
    return refs


def build_zip(src: Path = SRC_DIR, out: Path = DEFAULT_OUT, top: str = DEFAULT_TOP) -> dict:
    """把 src 目录内容装进 <top>/ 前缀，写出 zip。返回结构摘要。"""
    files = sorted(p for p in src.rglob("*") if p.is_file())
    if not files:
        raise SystemExit(f"错误：{src} 下没有文件")

    refs = set()
    mf = src / "manifest.json"
    if mf.exists():
        refs = _collect_manifest_refs(json.loads(mf.read_text(encoding="utf-8")))

    missing_src = sorted(r for r in refs if not (src / r).exists())
    if missing_src:
        raise SystemExit(f"错误：manifest 引用了不存在的文件：{missing_src}")

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in files:
            arc = f"{top}/{f.relative_to(src).as_posix()}"
            zi = zipfile.ZipInfo.from_file(f, arcname=arc)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            z.writestr(zi, f.read_bytes())
        # 说明文件放在扩展目录内，不影响加载，只为对齐参考包结构
        if not any(f.name == "INSTALL.txt" for f in files):
            zi = zipfile.ZipInfo.from_file(src / "manifest.json", arcname=f"{top}/INSTALL.txt")
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            z.writestr(zi, INSTALL_TEXT.encode("utf-8"))
    tmp.replace(out)

    return {"path": str(out), "top": top, "count": len(files), "refs": sorted(refs)}


def verify_zip(path: Path) -> dict:
    """校验：唯一顶层目录 / manifest 在该目录内 / manifest 引用文件齐全 / 无垃圾文件。"""
    info: dict = {"path": str(path), "ok": False, "problems": [], "entries": []}
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        info["entries"] = names

        tops = {n.split("/", 1)[0] for n in names}
        # 平铺包的特征是「有条目不含 /」，先单独判它，给出的报错才有指向性
        if any("/" not in n for n in names):
            info["problems"].append("有文件直接落在 zip 根（平铺），VirtualBrowser 会解错路径")
            info["top"] = None
            return info

        if len(tops) != 1:
            info["problems"].append(f"顶层目录不唯一：{sorted(tops)}")
            info["top"] = None
            return info
        top = tops.pop()
        info["top"] = top

        if f"{top}/manifest.json" not in names:
            info["problems"].append(f"manifest.json 不在 {top}/ 下")
            return info

        manifest = json.loads(z.read(f"{top}/manifest.json").decode("utf-8"))
        info["manifest_version"] = manifest.get("manifest_version")
        info["ext_name"] = manifest.get("name")
        info["ext_version"] = manifest.get("version")

        present = set(names)
        for ref in sorted(_collect_manifest_refs(manifest)):
            if f"{top}/{ref}" not in present:
                info["problems"].append(f"manifest 引用的文件缺失：{ref}")

        for bad in ("__MACOSX", ".DS_Store", "Thumbs.db"):
            if any(bad in n for n in names):
                info["problems"].append(f"包含垃圾文件：{bad}")

    info["ok"] = not info["problems"]
    return info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="打包/校验 VirtualBrowser 可用的扩展 zip")
    ap.add_argument("--src", default=str(SRC_DIR), help="扩展源码目录")
    ap.add_argument("-o", "--out", default=str(DEFAULT_OUT), help="输出 zip 路径")
    ap.add_argument("--top", default=DEFAULT_TOP, help="包内顶层目录名（= 扩展目录名）")
    ap.add_argument("--verify", metavar="FILE", help="只校验已有 zip")
    args = ap.parse_args(argv)

    if args.verify:
        v = verify_zip(Path(args.verify))
        print(json.dumps(v, ensure_ascii=False, indent=2))
        return 0 if v["ok"] else 1

    r = build_zip(Path(args.src), Path(args.out), args.top)
    print(f"已生成 {r['path']}  顶层目录={r['top']}/  文件数={r['count']}")
    v = verify_zip(Path(args.out))
    print(f"校验：ok={v['ok']}  顶层={v.get('top')}  "
          f"mv{v.get('manifest_version')}  {v.get('ext_name')} v{v.get('ext_version')}")
    if v["problems"]:
        for p in v["problems"]:
            print(f"  ! {p}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

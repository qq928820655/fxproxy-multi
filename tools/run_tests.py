"""一键跑全部测试。

以前每加一个测试文件都要手工往命令里补一条，漏了没人知道 —— test_versions.py
就是这么漏在清单外的。这里改成扫目录，新加的 test_*.py 自动进来。

跑: python tools/run_tests.py [-q]
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
QUIET = "-q" in sys.argv


def run(cmd, name):
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    ok = r.returncode == 0
    if not QUIET or not ok:
        print(f"{name:28} {'OK' if ok else 'FAIL'}")
    if not ok:
        tail = (r.stdout or "") + (r.stderr or "")
        print("\n".join("    " + ln for ln in tail.strip().splitlines()[-25:]))
    return ok


def main():
    py_tests = sorted(p.name for p in (ROOT / "tests").glob("test_*.py"))
    js_tests = sorted(p.name for p in (ROOT / "tests").glob("test_*.js"))

    total = passed = 0
    for name in py_tests:
        total += 1
        passed += run([PY, f"tests/{name}"], name)

    node = None
    for cand in ("node", "node.exe"):
        try:
            subprocess.run([cand, "--version"], capture_output=True, check=True)
            node = cand
            break
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    if node:
        for name in js_tests:
            total += 1
            passed += run([node, f"tests/{name}"], name)
    else:
        print(f"{'node':28} skip (未安装)")

    print(f"\n{passed}/{total} 通过" + ("" if passed == total else f"，{total - passed} 个失败"))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())

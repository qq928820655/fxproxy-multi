"""Build a CRX from chrome-extension/ with files FLAT at the archive root.

Two issues this fixes
---------------------
1. ``ENOENT ... extensions\\...\\content.js`` — the CRX zip had an extra
   top-level folder (``fxproxy-chrome-extension/content.js``). The browser
   unwraps into ``extensions/<id>/`` and looks for content.js at the root, so
   the nested copy is invisible. Every entry here is a bare filename.
2. ``Cannot read properties of null (reading 'toString')`` — the fingerprint
   browser (VirtualBrowser) crashed parsing a signed CRX3 protobuf header.
   Default output is therefore **CRX2** (fixed 16-byte header, widest support).
   Use ``--crx3`` if you specifically need CRX3.

Note: VirtualBrowser also accepts a plain ZIP with manifest.json at root
("unpacked source, installed in developer mode") — that is the most robust
route of all; see README. This script is for when a real .crx is required.

Usage
-----
    python tools/build_crx.py                       # CRX2 -> fxproxy-chrome-extension.crx
    python tools/build_crx.py --crx3 -o out.crx     # CRX3 instead
    python tools/build_crx.py --verify out.crx      # verify structure + signature

Requires: cryptography  (pip install cryptography)
"""
from __future__ import annotations

import argparse
import hashlib
import io
import struct
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "chrome-extension"
DEFAULT_OUT = ROOT / "fxproxy-chrome-extension.crx"
MAGIC = b"Cr24"
EXT_FILES = ("manifest.json", "content.js", "formats.js", "popup.html", "popup.js")


# ---- minimal protobuf writer (varint + length-delimited only, CRX3 path) --
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _bytes_field(field_no: int, value: bytes) -> bytes:
    return _varint((field_no << 3) | 2) + _varint(len(value)) + value


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


def _fields(buf: bytes) -> dict[int, list[bytes]]:
    out: dict[int, list[bytes]] = {}
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        field_no, wire = key >> 3, key & 7
        if wire != 2:
            raise ValueError(f"unexpected protobuf wire type {wire}")
        length, pos = _read_varint(buf, pos)
        value = buf[pos:pos + length]
        if len(value) != length:
            raise ValueError("truncated protobuf field")
        pos += length
        out.setdefault(field_no, []).append(value)
    return out


# ---- zip with guaranteed-flat entries -----------------------------------
def build_zip() -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in EXT_FILES:
            path = SRC / name
            if not path.is_file():
                raise FileNotFoundError(f"missing extension file: {path}")
            zf.write(path, arcname=name)  # arcname = bare filename: the whole point
    return bio.getvalue()


def _key():
    try:
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:
        raise SystemExit("cryptography 未安装：pip install cryptography")
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _spki(private_key) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def ext_id_from_spki(spki: bytes) -> str:
    """Chrome extension ID: SHA256(pubkey)[:16], nibble -> 'a'..'p'."""
    return "".join(chr(ord("a") + (b >> 4)) + chr(ord("a") + (b & 0xF))
                   for b in hashlib.sha256(spki).digest()[:16])


# ---- CRX2 (default: widest compatibility) --------------------------------
def build_crx2(zip_bytes: bytes) -> tuple[bytes, dict]:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    key = _key()
    spki = _spki(key)
    # CRX2 signature = RSA-SHA256 over (public_key || archive)
    signature = key.sign(spki + zip_bytes, padding.PKCS1v15(), hashes.SHA256())
    crx = MAGIC + struct.pack("<III", 2, len(spki), len(signature)) + spki + signature + zip_bytes
    return crx, {"version": 2, "crx_id": ext_id_from_spki(spki),
                 "signature_len": len(signature), "zip_len": len(zip_bytes)}


# ---- CRX3 (optional) ------------------------------------------------------
def build_crx3(zip_bytes: bytes) -> tuple[bytes, dict]:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    key = _key()
    spki = _spki(key)
    crx_id = hashlib.sha256(spki).digest()[:16]
    signed_header_data = _bytes_field(1, crx_id)
    to_sign = (MAGIC + struct.pack("<II", 3, len(signed_header_data))
               + signed_header_data + zip_bytes)
    signature = key.sign(to_sign, padding.PKCS1v15(), hashes.SHA256())
    proof = _bytes_field(1, spki) + _bytes_field(2, signature)
    header = _bytes_field(2, proof) + _bytes_field(4, crx_id)
    crx = MAGIC + struct.pack("<II", 3, len(header)) + header + zip_bytes
    return crx, {"version": 3, "crx_id": crx_id.hex(),
                 "signature_len": len(signature), "zip_len": len(zip_bytes)}


# ---- extract / verify -----------------------------------------------------
def extract_zip(crx: bytes) -> tuple[bytes, int, dict]:
    if crx[:4] != MAGIC:
        raise ValueError("not a CRX (missing Cr24 magic)")
    version = struct.unpack("<I", crx[4:8])[0]
    if version == 2:
        # magic(4) + version(4) + pub_len(4) + sig_len(4) = 16-byte prefix
        pub_len, sig_len = struct.unpack("<II", crx[8:16])
        start = 16 + pub_len + sig_len
        return crx[start:], version, {"pub_len": pub_len, "sig_len": sig_len}
    if version == 3:
        header_size = struct.unpack("<I", crx[8:12])[0]
        return crx[12 + header_size:], version, {"header_size": header_size}
    raise ValueError(f"unsupported CRX version {version}")


def verify_crx(crx: bytes) -> dict:
    zip_bytes, version, vinfo = extract_zip(crx)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        bad = [n for n in names if "/" in n or n.startswith(".")]
        if bad:
            raise ValueError(f"CRX entries are not flat at root: {bad}")
        missing = [f for f in EXT_FILES if f not in names]
        if missing:
            raise ValueError(f"CRX missing files: {missing}")
        for f in EXT_FILES:
            p = SRC / f
            if p.exists() and zf.read(f) != p.read_bytes():
                raise ValueError(f"CRX content mismatch: {f}")

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    info: dict = {"version": version, "entries": names, **vinfo}

    if version == 2:
        pub_len, sig_len = vinfo["pub_len"], vinfo["sig_len"]
        spki = crx[16:16 + pub_len]
        sig = crx[16 + pub_len:16 + pub_len + sig_len]
        pub = serialization.load_der_public_key(spki)
        pub.verify(sig, spki + zip_bytes, padding.PKCS1v15(), hashes.SHA256())
        info["signature_valid"] = True
        info["crx_id"] = ext_id_from_spki(spki)
    else:
        header_size = vinfo["header_size"]
        header = _fields(crx[12:12 + header_size])
        crx_id = header[4][0] if 4 in header else None
        if crx_id is None:
            raise ValueError("CRX3 header missing crx_id")
        info["crx_id"] = crx_id.hex()
        proof = _fields(header[2][0]) if 2 in header else {}
        spki = proof.get(1, [b""])[0]
        sig = proof.get(2, [b""])[0]
        signed_header_data = _bytes_field(1, crx_id)
        to_sign = (MAGIC + struct.pack("<II", 3, len(signed_header_data))
                   + signed_header_data + zip_bytes)
        pub = serialization.load_der_public_key(spki)
        pub.verify(sig, to_sign, padding.PKCS1v15(), hashes.SHA256())
        info["signature_valid"] = True
    return info


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--crx3", action="store_true", help="build CRX3 instead of the default CRX2")
    ap.add_argument("--verify", type=Path, help="verify an existing CRX instead of building")
    args = ap.parse_args()

    if args.verify:
        info = verify_crx(args.verify.read_bytes())
        print(f"OK {args.verify}  version={info['version']}  crx_id={info.get('crx_id', '-')}  "
              f"sig_valid={info.get('signature_valid')}  entries={info['entries']}")
        return 0

    zip_bytes = build_zip()
    crx, meta = (build_crx3 if args.crx3 else build_crx2)(zip_bytes)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(crx)
    info = verify_crx(crx)
    print(f"built {args.output}  ({len(crx)} bytes, CRX{meta['version']})")
    print(f"  crx_id     : {meta['crx_id']}")
    print(f"  signature  : RSA-SHA256, {meta['signature_len']} bytes")
    print(f"  entries    : {info['entries']}")
    print("  flat-root  : OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

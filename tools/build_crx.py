"""Build a signed CRX3 from chrome-extension/ with files FLAT at the root.

Why this exists
---------------
The fingerprint browser (VirtualBrowser) reported:

    ENOENT ... extensions\\fxproxy-chrome-extension\\content.js

That happens when the CRX zip has an extra top-level folder, i.e. its entries
look like ``fxproxy-chrome-extension/content.js``. The browser unwraps the CRX
into ``extensions/<id>/`` and then looks for ``content.js`` directly there, so
the nested copy is invisible. This builder guarantees every entry is a bare
filename at the archive root.

Usage
-----
    python tools/build_crx.py                       # -> fxproxy-chrome-extension.crx
    python tools/build_crx.py -o /path/out.crx
    python tools/build_crx.py --verify out.crx      # verify an existing CRX

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


# ---- minimal protobuf writer (varint + length-delimited only) ------------
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _bytes_field(field_no: int, value: bytes) -> bytes:
    """Protobuf wire type 2 (length-delimited) field."""
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
    """Parse top-level protobuf fields into {field_no: [raw_value, ...]}."""
    out: dict[int, list[bytes]] = {}
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        field_no, wire = key >> 3, key & 7
        if wire != 2:  # this schema only uses length-delimited fields
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
            # arcname = bare filename — this is the whole point of the fix
            zf.write(path, arcname=name)
    return bio.getvalue()


# ---- CRX3 assembly -------------------------------------------------------
def build_crx3(zip_bytes: bytes) -> tuple[bytes, dict]:
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError:
        raise SystemExit("cryptography 未安装：pip install cryptography")

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    spki = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    crx_id = hashlib.sha256(spki).digest()[:16]

    # CrxSignedHeaderData { bytes crx_id = 1; }
    signed_header_data = _bytes_field(1, crx_id)

    # Chrome verifies the signature over this exact byte sequence: the 12-byte
    # prefix, the SIGNED header data (not the full header), then the zip.
    to_sign = (MAGIC + struct.pack("<II", 3, len(signed_header_data))
               + signed_header_data + zip_bytes)
    signature = private_key.sign(to_sign, padding.PKCS1v15(), hashes.SHA256())

    # AsymmetricKeyProof { bytes public_key = 1; bytes signature = 2; }
    proof = _bytes_field(1, spki) + _bytes_field(2, signature)
    # CrxFileHeader { AsymmetricKeyProof sha256_with_rsa = 2; bytes crx_id = 4; }
    header = _bytes_field(2, proof) + _bytes_field(4, crx_id)

    crx = MAGIC + struct.pack("<II", 3, len(header)) + header + zip_bytes
    return crx, {"crx_id": crx_id.hex(), "public_key_len": len(spki),
                 "signature_len": len(signature), "zip_len": len(zip_bytes)}


def extract_zip(crx: bytes) -> bytes:
    if crx[:4] != MAGIC:
        raise ValueError("not a CRX (missing Cr24 magic)")
    version, header_size = struct.unpack("<II", crx[4:12])
    if version not in (2, 3):
        raise ValueError(f"unsupported CRX version {version}")
    if version == 3:
        return crx[12 + header_size:]
    # CRX2: pubkey len + signature len follow the 12-byte prefix
    pub_len, sig_len = struct.unpack("<II", crx[12:20])
    return crx[20 + pub_len + sig_len:]


def verify_crx(crx: bytes) -> dict:
    zip_bytes = extract_zip(crx)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        bad = [n for n in names if "/" in n or n.startswith(".")]
        if bad:
            raise ValueError(f"CRX entries are not flat at root: {bad}")
        missing = [f for f in EXT_FILES if f not in names]
        if missing:
            raise ValueError(f"CRX missing files: {missing}")
        for f in EXT_FILES:
            if SRC.joinpath(f).exists() and zf.read(f) != SRC.joinpath(f).read_bytes():
                raise ValueError(f"CRX content mismatch: {f}")
    info = {"version": struct.unpack("<I", crx[4:8])[0], "entries": names}
    if info["version"] == 3:
        header_size = struct.unpack("<I", crx[8:12])[0]
        header = _fields(crx[12:12 + header_size])
        crx_id = header[4][0] if 4 in header else None
        if crx_id is not None:
            info["crx_id"] = crx_id.hex()
        if 2 in header:
            proof = _fields(header[2][0])
            spki = proof.get(1, [b""])[0]
            signature = proof.get(2, [b""])[0]
            info["public_key"] = len(spki)
            info["signature"] = len(signature)
            # Rebuild the exact signed byte sequence and verify the RSA-SHA256
            # signature, so --verify proves the CRX is cryptographically valid.
            if crx_id is None:
                raise ValueError("CRX3 header missing crx_id")
            signed_header_data = _bytes_field(1, crx_id)
            to_sign = (MAGIC + struct.pack("<II", 3, len(signed_header_data))
                       + signed_header_data + zip_bytes)
            try:
                from cryptography.hazmat.primitives import hashes, serialization
                from cryptography.hazmat.primitives.asymmetric import padding
                pub = serialization.load_der_public_key(spki)
                pub.verify(signature, to_sign, padding.PKCS1v15(), hashes.SHA256())
                info["signature_valid"] = True
            except ImportError:
                info["signature_valid"] = "skipped (cryptography not installed)"
            except Exception as e:
                raise ValueError(f"CRX3 signature verification failed: {e}")
    return info


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--verify", type=Path, help="verify an existing CRX instead of building")
    args = ap.parse_args()

    if args.verify:
        info = verify_crx(args.verify.read_bytes())
        print(f"OK {args.verify}  version={info['version']}  "
              f"crx_id={info.get('crx_id', '-')}  "
              f"sig_valid={info.get('signature_valid', 'n/a')}  entries={info['entries']}")
        return 0

    zip_bytes = build_zip()
    crx, meta = build_crx3(zip_bytes)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(crx)
    info = verify_crx(crx)
    print(f"built {args.output}  ({len(crx)} bytes)")
    print(f"  crx_id     : {meta['crx_id']}")
    print(f"  signature  : RSA-SHA256, {meta['signature_len']} bytes")
    print(f"  entries    : {info['entries']}")
    print("  flat-root  : OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

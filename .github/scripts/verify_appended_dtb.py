#!/usr/bin/env python3
"""
verify_appended_dtb.py -- assert the appended DTB payload of Image.gz-dtb
contains the device tree this build was supposed to produce.

WHY this exists:
  Image.gz-dtb = gzip(Image) + every .dtb found under out/arch/arm64/boot/dts/
  (arch/arm64/boot/Makefile: DTB_NAMES is empty, so it falls back to
  `find -L $(obj)/dts/ -name '*.dtb'`).

  That means a defconfig missing the right board symbol silently produces an
  image whose appended DTBs are the GENERIC Qualcomm boards only. The build
  still "succeeds" and the zip still looks fine -- but the device boots the
  wrong device tree, and any DT-level override (e.g. the OC
  qcom,perfcl-force-speedbin) is simply absent.

  This happened to jasmine-stock: it had no CONFIG_MACH_XIAOMI_WAYNE, so
  sdm660-mtp-jasmine.dtb was never built and the "jasmine" zip carried only
  sdm660-mtp.dtb and friends.

Usage:
  verify_appended_dtb.py <Image.gz-dtb> --expect-model <substr> \
      [--expect-model <substr> ...] [--expect-prop <name>[=<value>]] \
      [--expect-dtb <basename.dtb>]

Exit 0 on success, 1 on any failed assertion.
"""

import argparse
import gzip
import os
import struct
import sys
import zlib

MAGIC = b"\xd0\x0d\xfe\xed"


def align4(n):
    r = n % 4
    return n + (4 - r) if r else n


def split_dtbs(blob):
    """Yield (offset, bytes) for every DTB found in blob."""
    i = 0
    while True:
        j = blob.find(MAGIC, i)
        if j < 0:
            return
        if j + 8 > len(blob):
            return
        totalsize = struct.unpack(">I", blob[j + 4:j + 8])[0]
        if totalsize < 40 or j + totalsize > len(blob):
            i = j + 4
            continue
        yield j, blob[j:j + totalsize]
        i = j + totalsize


def parse_dtb(chunk):
    """Return (model, {prop_name: value}) for a single DTB."""
    (magic, totalsize, off_struct, off_strings, off_rsvmap, ver,
     lastcomp, bootcpu, size_strings, size_struct) = struct.unpack(">10I", chunk[:40])

    def getstr(off):
        e = chunk.index(b"\0", off_strings + off)
        return chunk[off_strings + off:e].decode("utf-8", "replace")

    pos = off_struct
    end = off_struct + size_struct
    model = None
    props = {}
    while pos < end:
        tok = struct.unpack(">I", chunk[pos:pos + 4])[0]
        if tok == 1:                                  # BEGIN_NODE
            z = chunk.index(b"\0", pos + 4)
            pos = align4(z + 1)
        elif tok == 2:                                # END_NODE
            pos += 4
        elif tok == 3:                                # PROP
            ln, nameoff = struct.unpack(">II", chunk[pos + 4:pos + 12])
            pname = getstr(nameoff)
            val = chunk[pos + 12:pos + 12 + ln]
            if pname == "model":
                model = val.rstrip(b"\0").decode("utf-8", "replace")
            if pname == "qcom,board-id":
                cells = [struct.unpack(">I", val[i:i + 4])[0]
                         for i in range(0, len(val) - 3, 4)]
                props[pname] = cells
            elif len(val) == 4:
                props[pname] = struct.unpack(">I", val)[0]
            elif len(val) == 0:
                props[pname] = ""
            else:
                props.setdefault(pname, val)
            pos = align4(pos + 12 + ln)
        elif tok == 4:                                # NOP
            pos += 4
        elif tok == 9:                                # END
            break
        else:
            break
    return model, props


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--expect-model", action="append", default=[],
                    help="substring that must appear in some DTB's model")
    ap.add_argument("--expect-board-id", action="append", default=[],
                    help="board-id cell list (space separated decimal) that must "
                         "appear in some DTB, e.g. '196616 0' for <0x030008 0>")
    ap.add_argument("--expect-prop", action="append", default=[],
                    help="prop name, or name=value, that must be present")
    ap.add_argument("--expect-dtb", action="append", default=[],
                    help="basename of a .dtb that must exist on disk next to the image")
    args = ap.parse_args()

    if not os.path.isfile(args.image):
        print("FAIL: image not found: %s" % args.image)
        return 1

    data = open(args.image, "rb").read()
    d = zlib.decompressobj(31)
    try:
        d.decompress(data)
    except Exception as e:
        print("FAIL: not a gzip stream: %s" % e)
        return 1
    appended = d.unused_data

    print("== appended DTB verification ==")
    print("image        : %s" % args.image)
    print("image size   : %d bytes" % len(data))
    print("appended size: %d bytes" % len(appended))

    if not appended:
        print("FAIL: no appended DTB payload after the gzip stream")
        return 1

    dtbs = []
    for off, chunk in split_dtbs(appended):
        try:
            model, props = parse_dtb(chunk)
        except Exception as e:
            print("  [off=%d] parse error: %s" % (off, e))
            continue
        dtbs.append((off, chunk, model, props))

    print("DTB count    : %d" % len(dtbs))
    for off, chunk, model, props in dtbs:
        oc = {k: v for k, v in props.items()
              if "force-speedbin" in k}
        suffix = ("  OC=%s" % oc) if oc else ""
        bid = props.get("qcom,board-id")
        bids = ("  board-id=%s" % bid) if bid else ""
        print("  off=%-8d size=%-8d model=%r%s%s" % (off, len(chunk), model, bids, suffix))

    rc = 0

    # --- expected models -------------------------------------------------
    models = [(m or "") for _, _, m, _ in dtbs]
    for want in args.expect_model:
        if any(want.lower() in m.lower() for m in models):
            print("OK   model contains %r" % want)
        else:
            print("FAIL no DTB model contains %r" % want)
            rc = 1

    # --- expected board-ids ---------------------------------------------
    # qcom,board-id is a list of (id, variant) pairs, e.g. wayne has
    # <0x020008 0>, <0x50008 0> -> [131080, 0, 327688, 0]. Match on the
    # leading pair so a caller can assert "this is the wayne board".
    for spec in args.expect_board_id:
        want = [int(x, 0) for x in spec.split()]
        hit = False
        for _, _, m, props in dtbs:
            got = props.get("qcom,board-id")
            if isinstance(got, list) and got[:len(want)] == want:
                print("OK   board-id %s present (model=%r)" % (want, m))
                hit = True
        if not hit:
            seen = [props.get("qcom,board-id") for _, _, _, props in dtbs
                    if props.get("qcom,board-id")]
            print("FAIL board-id %s absent (seen: %s)" % (want, seen[:6]))
            rc = 1

    # --- expected properties --------------------------------------------
    for spec in args.expect_prop:
        if "=" in spec:
            name, raw = spec.split("=", 1)
            want = int(raw, 0)
        else:
            name, want = spec, None

        hits = [(m, props.get(name)) for _, _, m, props in dtbs if name in props]
        if not hits:
            print("FAIL property %r absent from every DTB" % name)
            rc = 1
            continue
        if want is None:
            print("OK   property %r present in %d DTB(s)" % (name, len(hits)))
        elif any(v == want for _, v in hits):
            print("OK   property %r = <%d> present" % (name, want))
        else:
            print("FAIL property %r present but no DTB has value <%d> (saw %s)"
                  % (name, want, [v for _, v in hits]))
            rc = 1

    # --- expected .dtb files on disk ------------------------------------
    base = os.path.dirname(os.path.abspath(args.image))
    for name in args.expect_dtb:
        # the build tree keeps them under dts/qcom/
        found = None
        for root, _dirs, files in os.walk(base):
            if name in files:
                found = os.path.join(root, name)
                break
        if found:
            print("OK   %s present (%s)" % (name, found))
        else:
            print("FAIL %s not found under %s" % (name, base))
            rc = 1

    print()
    print("VERDICT: %s" % ("PASS" if rc == 0 else "FAIL"))
    return rc


if __name__ == "__main__":
    sys.exit(main())

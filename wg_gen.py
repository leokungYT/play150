# -*- coding: utf-8 -*-
"""สร้างไฟล์ WireGuard (.conf) ของ Windscribe ให้ครบทุกจอ โดยไม่ต้องกดโหลดทีละเมือง

หลักการ: ไฟล์ .conf ของ Windscribe = Key Pair ของเรา ([Interface] + PresharedKey)
         + PublicKey/Endpoint ของเซิร์ฟเวอร์ (มีในรายชื่อเซิร์ฟเวอร์สาธารณะของ Windscribe)
ดังนั้นใช้ไฟล์ที่โหลดมาแล้ว 1 ไฟล์เป็นต้นแบบ (เอาแค่ Key Pair) แล้วสร้างไฟล์คนละเซิร์ฟเวอร์ได้หลายสิบไฟล์
แต่ละไฟล์คนละเซิร์ฟเวอร์ = ต่อพร้อมกันได้ ไม่ตีกัน (กุญแจเดียวกันห้ามต่อเซิร์ฟเวอร์เดียวกันซ้ำ)

ใช้:  python wg_gen.py 30            -> ให้มีไฟล์ใน wg/ ครบ 30 ไฟล์ (เพิ่มเฉพาะที่ขาด)
      python wg_gen.py 30 TH SG JP   -> เลือกประเทศเอง (รหัส 2 ตัว) ตามลำดับ
"""
import json
import os
import sys
import urllib.request

WG_DIR = "wg"
SERVERLIST_URL = "https://assets.windscribe.com/serverlist/mob-v2/1/0"
# ประเทศใกล้ไทยก่อน (ping ต่ำ เกมโหลดเร็ว) แล้วค่อยไกลออกไป
DEFAULT_COUNTRIES = ["TH", "SG", "HK", "MY", "VN", "JP", "KR", "TW", "ID", "PH", "KH",
                     "IN", "AU", "NZ", "AE", "TR", "US", "CA", "GB", "DE", "FR", "NL"]


def read_conf(path):
    kv = {}
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip()
    return kv


def main():
    # python wg_gen.py 15 [--account account2] [--machine 3] [--total 30] [TH SG ...]
    args = sys.argv[1:]
    opt = {}
    for k in ("--account", "--machine", "--total"):
        if k in args:
            i = args.index(k)
            opt[k] = args[i + 1]
            del args[i:i + 2]
    save = "--save-config" in args
    if save:
        args.remove("--save-config")
    want = int(args[0]) if args and args[0].isdigit() else 30
    countries = [c.upper() for c in args[1:]] or None
    if save:
        save_config(opt.get("--account"), opt.get("--machine"))
    return generate(want, countries, None, int(opt.get("--machine", 0)), opt.get("--account"), int(opt.get("--total", 0)))


def save_config(account=None, machine=None, path="configmain.json"):
    """เขียน wg_enabled=1 / wg_account / wg_machine ลง configmain.json (แก้เฉพาะค่านั้น ไม่จัดรูปไฟล์ใหม่)"""
    import re
    raw = open(path, "rb").read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    t = raw.decode("utf-8-sig")
    nl = "\r\n" if "\r\n" in t else "\n"
    vals = {"wg_enabled": "1"}
    if account:
        vals["wg_account"] = json.dumps(account)
    if machine:
        vals["wg_machine"] = str(int(machine))
    for k, v in vals.items():
        pat = r'("' + k + r'"\s*:\s*)("[^"]*"|[^,\r\n}]+)'
        if re.search(pat, t):
            t = re.sub(pat, lambda m, v=v: m.group(1) + v, t, count=1)
        else:
            t = t.replace("{", "{" + nl + '    "' + k + '": ' + v + ",", 1)
    json.loads(t)                       # กันไฟล์พัง: JSON ไม่ถูกจะ error ก่อนเขียน
    open(path, "wb").write((b"\xef\xbb\xbf" if bom else b"") + t.encode("utf-8"))
    print("[WG-GEN] configmain.json: " + ", ".join(f"{k}={v}" for k, v in vals.items()))


def load_blocked(wg_dir):
    """{endpoint: เวลาที่โดนบล็อก} ที่ยังไม่หมดอายุ (wg_blocked_days วัน, default 7)"""
    try:
        with open(os.path.join(wg_dir, ".blocked.json"), encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}
    try:
        days = float(json.load(open(os.path.join(os.path.dirname(os.path.abspath(wg_dir)), "configmain.json"),
                                    encoding="utf-8-sig")).get("wg_blocked_days", 7))
    except Exception:
        days = 7.0
    import time as _t
    return {k: v for k, v in d.items() if _t.time() - v < days * 86400}


def generate(want, countries=None, wg_dir=None, machine=0, account=None, total=0, managed_ok=False):
    """ให้มีไฟล์ใน wg/ อย่างน้อย want ไฟล์ - คืน 0 = สำเร็จ/ครบแล้ว, 1 = สร้างไม่ได้

    machine (1, 2, 3 ...) = โหมดหลายเครื่องใช้ Key Pair เดียวกัน: เครื่องที่ N ได้เซิร์ฟเวอร์ช่วงของตัวเอง
    (ลำดับ (N-1)*want ... ) ไม่มีวันชนกับเครื่องอื่นที่ใช้กุญแจเดียวกัน - ไฟล์ต้นแบบถูกย้ายไปเป็น wg/keypair.txt
    """
    global WG_DIR
    if wg_dir:
        WG_DIR = wg_dir
    countries = countries or DEFAULT_COUNTRIES
    machine = int(machine or 0)
    # ไม่ได้ระบุบัญชี/จำนวนเครื่อง -> อ่านจาก configmain.json ข้างโฟลเดอร์ wg (ใช้ได้ทั้งตอนบอทเรียกและตอนกด wg-gen.bat)
    if account is None or not total:
        try:
            _cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(WG_DIR)), "configmain.json"),
                                  encoding="utf-8-sig"))
        except Exception:
            _cfg = {}
        if account is None:
            account = str(_cfg.get("wg_account", "") or "").strip() or None
        if not total:
            total = int(_cfg.get("wg_total_machines", 30) or 30)
    key_txt = os.path.join(WG_DIR, "keypair.txt")
    if os.path.exists(os.path.join(WG_DIR, ".managed")) and not managed_ok:
        print("[WG-GEN] ไฟล์ VPN มาจาก server (RemoteFileManager) - ไม่สร้างเพิ่มเอง")
        return 0

    confs = sorted(f for f in os.listdir(WG_DIR) if f.lower().endswith(".conf")) if os.path.isdir(WG_DIR) else []
    if account:
        # ===== โหมดเลือกบัญชี: กุญแจอยู่ที่ wg_accounts/<account>/ (1-5 ไฟล์ จากหน้า Config Generator ของบัญชีนั้น) =====
        acc_dir = os.path.join(os.path.dirname(os.path.abspath(WG_DIR)), "wg_accounts", account)
        keys, seen = [], set()
        for f in sorted(os.listdir(acc_dir)) if os.path.isdir(acc_dir) else []:
            if f.lower().endswith((".conf", ".txt")):
                kv = read_conf(os.path.join(acc_dir, f))
                if all(kv.get(k) for k in ("PrivateKey", "Address", "DNS", "PresharedKey")) and kv["PrivateKey"] not in seen:
                    seen.add(kv["PrivateKey"])
                    keys.append(kv)
        if not keys:
            # บัญชีนี้ไม่มีกุญแจ -> หาจากที่อื่นเอง: บัญชีอื่นใน wg_accounts/, keypair.txt, ไฟล์ .conf ใน wg/, ไฟล์สำรอง
            root = os.path.dirname(os.path.abspath(WG_DIR))
            cands = []
            accs = os.path.join(root, "wg_accounts")
            for a2 in sorted(os.listdir(accs)) if os.path.isdir(accs) else []:
                d2 = os.path.join(accs, a2)
                if os.path.isdir(d2):
                    cands += [os.path.join(d2, x) for x in sorted(os.listdir(d2)) if x.lower().endswith((".conf", ".txt"))]
            cands += [key_txt] + [os.path.join(WG_DIR, c) for c in confs]
            for p in cands:
                if os.path.isfile(p):
                    kv = read_conf(p)
                    if all(kv.get(k) for k in ("PrivateKey", "Address", "DNS", "PresharedKey")):
                        keys.append(kv)
                        print(f"[WG-GEN] บัญชี '{account}' ไม่มีกุญแจ - ใช้กุญแจจาก {p} แทน")
                        break
        if not keys:
            print(f"[WG-GEN] ไม่มีกุญแจ Windscribe ในเครื่องเลย - วางไฟล์ .conf ของ Windscribe 1 ไฟล์ไว้ที่ {acc_dir}")
            return 1
        # สำรองกุญแจไว้นอก wg/ เสมอ (wg/ โดนล้างได้) -> wg_accounts/_backup/key.conf
        try:
            bk = os.path.join(os.path.dirname(os.path.abspath(WG_DIR)), "wg_accounts", "_backup")
            os.makedirs(bk, exist_ok=True)
            if not os.path.exists(os.path.join(bk, "key.conf")):
                k0 = keys[0]
                with open(os.path.join(bk, "key.conf"), "w", encoding="utf-8", newline="\n") as fh:
                    fh.write("[Interface]\n" + "".join(f"{k} = {k0[k]}\n" for k in ("PrivateKey", "Address", "DNS")) +
                             "\n[Peer]\n" + f"PresharedKey = {k0['PresharedKey']}\n")
        except Exception:
            pass
        os.makedirs(WG_DIR, exist_ok=True)
        marker = os.path.join(WG_DIR, ".account")
        prev = open(marker, encoding="utf-8").read().strip() if os.path.exists(marker) else ""
        if prev != account:
            # เปลี่ยนบัญชี -> ลบไฟล์ของบัญชีเก่า (+ การจองของจอ) แล้วสร้างใหม่ทั้งหมด
            for f in os.listdir(WG_DIR):
                if f.lower().endswith(".conf") or f == ".managed":
                    os.remove(os.path.join(WG_DIR, f))   # keypair.txt ห้ามลบ (เคยลบแล้วกุญแจหายทั้งเครื่อง สร้าง IP ไม่ได้อีกเลย)
            cl = os.path.join(WG_DIR, ".claims")
            if os.path.isdir(cl):
                for f in os.listdir(cl):
                    os.remove(os.path.join(cl, f))
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write(account)
            confs = []
            print(f"[WG-GEN] เปลี่ยนเป็นบัญชี '{account}'" + (f" (จาก '{prev}')" if prev else "") + " - ลบไฟล์เก่าแล้วสร้างใหม่")
        # เครื่องที่ใช้บัญชีเดียวกันแบ่งกุญแจตามเลขเครื่อง: เครื่อง 1..G = กุญแจ 1, G+1..2G = กุญแจ 2 ...
        machine = machine or 1
        total = max(int(total or 0), machine)
        group = -(-total // len(keys))
        kidx = min((machine - 1) // group, len(keys) - 1)
        tpl, tpl_name = keys[kidx], f"{account} กุญแจที่ {kidx + 1}/{len(keys)}"
    elif os.path.exists(key_txt):
        tpl = read_conf(key_txt)
        tpl_name = "keypair.txt"
    elif confs:
        tpl = read_conf(os.path.join(WG_DIR, confs[0]))
        tpl_name = confs[0]
    else:
        # wg/ ว่าง -> หากุญแจจาก wg_accounts/ (สำรอง _backup ก่อน แล้วบัญชีอื่น) แทนการยอมแพ้
        accs = os.path.join(os.path.dirname(os.path.abspath(WG_DIR)), "wg_accounts")
        cands = [os.path.join(accs, "_backup", "key.conf")]
        for a2 in sorted(os.listdir(accs)) if os.path.isdir(accs) else []:
            d2 = os.path.join(accs, a2)
            if os.path.isdir(d2):
                cands += [os.path.join(d2, x) for x in sorted(os.listdir(d2)) if x.lower().endswith((".conf", ".txt"))]
        tpl = None
        for p in cands:
            if os.path.isfile(p):
                kv = read_conf(p)
                if all(kv.get(k) for k in ("PrivateKey", "Address", "DNS", "PresharedKey")):
                    tpl, tpl_name = kv, p
                    break
        if tpl is None:
            print(f"[WG-GEN] ไม่มีกุญแจ Windscribe ในเครื่องเลย (wg/ และ wg_accounts/ ว่าง) - โหลดไฟล์ .conf จาก Config Generator ของ Windscribe มาวางใน wg_accounts/<ชื่อบัญชี>/ 1 ไฟล์")
            return 1
        os.makedirs(WG_DIR, exist_ok=True)
        print(f"[WG-GEN] wg/ ว่าง - ใช้กุญแจจาก {tpl_name}")
    if machine and not account and tpl_name != "keypair.txt" and os.path.exists(os.path.join(WG_DIR, str(tpl_name))):
        # ไฟล์ต้นแบบ = เซิร์ฟเวอร์เดียวกันทุกเครื่องในกลุ่มกุญแจ -> ห้ามใช้ต่อจริง เก็บไว้เป็นกุญแจอย่างเดียว
        os.replace(os.path.join(WG_DIR, tpl_name), key_txt)
        print(f"[WG-GEN] โหมดหลายเครื่อง: ย้าย {tpl_name} -> keypair.txt (ใช้อ่านกุญแจ ไม่ใช้ต่อ VPN)")
        confs = [c for c in confs if c != tpl_name]
    have = [read_conf(os.path.join(WG_DIR, f)) for f in confs]
    for k in ("PrivateKey", "Address", "DNS", "PresharedKey"):
        if not tpl.get(k):
            print(f"[WG-GEN] ไฟล์ต้นแบบ {confs[0]} ไม่มี {k} - ใช้ไฟล์จากหน้า Config Generator ของ Windscribe")
            return 1
    print(f"[WG-GEN] ใช้ Key Pair จาก {tpl_name} | มีอยู่แล้ว {len(confs)} ไฟล์ ต้องการ {want}"
          + (f" | เครื่องที่ {machine}" if machine else ""))
    if len(confs) >= want:
        print("[WG-GEN] ครบแล้ว ไม่ต้องสร้างเพิ่ม")
        return 0

    print("[WG-GEN] โหลดรายชื่อเซิร์ฟเวอร์ Windscribe...")
    req = urllib.request.Request(SERVERLIST_URL, headers={"User-Agent": "Mozilla/5.0"})
    data = None
    for _try in range(3):
        try:
            data = json.load(urllib.request.urlopen(req, timeout=30))["data"]
            break
        except Exception as e:
            print(f"[WG-GEN] โหลดรายชื่อเซิร์ฟเวอร์ไม่ได้ ({e}) - ลองใหม่ ({_try + 1}/3)")
            import time as _t; _t.sleep(5)
    if data is None:
        print("[WG-GEN] เน็ต/DNS ของเครื่องใช้ไม่ได้ - ยังสร้าง IP ไม่ได้ (เช็คว่าเข้าเว็บได้ไหม ถ้าไม่ได้ให้รัน fix-dns.bat)")
        return 1

    # เซิร์ฟเวอร์ที่ใช้อยู่แล้ว (กุญแจไหนก็ตาม) ไม่ซ้ำ จะได้ IP ไม่ซ้ำด้วย
    used = {kv.get("PublicKey") for kv in have}
    # ประเทศที่เซิร์ฟเวอร์เกมบล็อก IP VPN (ทดสอบ 2026-10-06: Bangkok ออกเน็ตได้แต่ rangers-api ไม่ตอบ)
    try:
        _ex = json.load(open(os.path.join(os.path.dirname(os.path.abspath(WG_DIR)), "configmain.json"),
                             encoding="utf-8-sig")).get("wg_exclude_countries", ["TH"])
    except Exception:
        _ex = ["TH"]
    exclude = {str(c).upper() for c in (_ex or [])}
    blocked = load_blocked(WG_DIR)      # เซิร์ฟเวอร์ที่บอทเจอว่าเกมบล็อก IP (wg/.blocked.json) - ไม่สร้างซ้ำ
    order = {c: i for i, c in enumerate(countries)}
    groups = []
    for loc in data:
        cc = loc.get("country_code", "")
        if not loc.get("status", 1) or cc in exclude:
            continue
        if cc not in order and not machine:
            continue                        # โหมดเครื่องเดียว: เอาเฉพาะประเทศที่เลือก
        for g in loc.get("groups") or []:
            if not g.get("wg_pubkey") or not g.get("wg_endpoint") or not g.get("nodes"):
                continue
            if g["wg_endpoint"] in blocked:
                continue
            groups.append((order.get(cc, len(order)), cc, g))
    # เรียงแบบตายตัว (ทุกเครื่องได้ลำดับเดียวกัน) -> แบ่งช่วงต่อเครื่องได้ไม่ชน
    groups.sort(key=lambda x: (x[0], x[1], int(x[2].get("id") or 0)))
    if machine:
        start = ((machine - 1) * want) % max(1, len(groups))
        groups = (groups[start:] + groups[:start])[:want]
        print(f"[WG-GEN] เครื่องที่ {machine}: ใช้เซิร์ฟเวอร์ลำดับ {start + 1}-{start + len(groups)} จาก {len(groups) and len(groups)}")
    groups = [x for x in groups if x[2]["wg_pubkey"] not in used]

    made = 0
    port = tpl.get("Endpoint", ":443").rsplit(":", 1)[-1] or "443"
    for _, cc, g in groups:
        if len(confs) + made >= want:
            break
        city = (g.get("city") or cc).replace(" ", "-")
        nick = (g.get("nick") or str(g.get("id"))).replace(" ", "-")
        name = f"Windscribe-{city}-{nick}-WG.conf"
        path = os.path.join(WG_DIR, name)
        if os.path.exists(path):
            continue
        text = (
            "[Interface]\n"
            f"PrivateKey = {tpl['PrivateKey']}\n"
            f"Address = {tpl['Address']}\n"
            f"DNS = {tpl['DNS']}\n"
            "\n[Peer]\n"
            f"PublicKey = {g['wg_pubkey']}\n"
            f"AllowedIPs = {tpl.get('AllowedIPs', '0.0.0.0/0, ::/0')}\n"
            f"Endpoint = {g['wg_endpoint']}:{port}\n"
            f"PresharedKey = {tpl['PresharedKey']}\n"
        )
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        made += 1
        print(f"[WG-GEN] + {name}  ({cc} {g['wg_endpoint']})")

    total = len(confs) + made
    print(f"[WG-GEN] สร้างเพิ่ม {made} ไฟล์ - ตอนนี้มี {total} ไฟล์ใน {WG_DIR}/")
    if total < want:
        print(f"[WG-GEN] เซิร์ฟเวอร์ในประเทศที่เลือกไม่พอ ({total}/{want}) - เพิ่มรหัสประเทศ เช่น: python wg_gen.py {want} TH SG JP US DE")
    return 0


if __name__ == "__main__":
    sys.exit(main())

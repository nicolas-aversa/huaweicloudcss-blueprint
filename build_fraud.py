"""Build script: genera datasets/fraud.log (JSONL) a partir de los CSVs
del IEEE-CIS Fraud Detection dataset (train_transaction.csv + train_identity.csv).

Left-join por TransactionID. Convierte TransactionDT a ISO8601 (ref 2017-11-30).
Selecciona ~50 campos interpretables (skip V1-V339). Skip nulls/empties.

Uso:  py build_fraud.py
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATASETS = Path(__file__).parent / "datasets"
TX_CSV = DATASETS / "train_transaction.csv"
ID_CSV = DATASETS / "train_identity.csv"
OUT = DATASETS / "fraud-detection.log"

REF_DATE = datetime(2017, 11, 30, tzinfo=timezone.utc)

C_COLS = [f"C{i}" for i in range(1, 15)]
D_COLS = [f"D{i}" for i in range(1, 16)]
M_COLS = [f"M{i}" for i in range(1, 10)]


def _str(v: str) -> str | None:
    v = (v or "").strip()
    return v or None


def _int(v: str) -> int | None:
    v = (v or "").strip()
    if not v:
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def _float(v: str) -> float | None:
    v = (v or "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def load_identity() -> dict[int, dict]:
    identity: dict[int, dict] = {}
    with open(ID_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tid = _int(row.get("TransactionID", ""))
            if tid is None:
                continue
            dev: dict = {}
            dt = _str(row.get("DeviceType", ""))
            if dt:
                dev["type"] = dt
            di = _str(row.get("DeviceInfo", ""))
            if di:
                dev["info"] = di
            os_ = _str(row.get("id_31", ""))
            if os_:
                dev["os"] = os_
            screen = _str(row.get("id_33", ""))
            if screen:
                dev["screen"] = screen
            if dev:
                identity[tid] = dev
    return identity


def build() -> int:
    if not TX_CSV.exists():
        print(f"[error] no existe {TX_CSV}", file=sys.stderr)
        return 1
    if not ID_CSV.exists():
        print(f"[error] no existe {ID_CSV}", file=sys.stderr)
        return 1

    print(f"[build] cargando identity ({ID_CSV.name})...")
    identity = load_identity()
    print(f"[build] {len(identity):,} registros de identity")

    print(f"[build] procesando transactions -> {OUT.name}...")
    count = 0
    fraud_count = 0
    with open(TX_CSV, "r", encoding="utf-8") as fin, \
         open(OUT, "w", encoding="utf-8") as fout:
        reader = csv.DictReader(fin)
        for row in reader:
            tid = _int(row.get("TransactionID", ""))
            if tid is None:
                continue
            dt_val = _int(row.get("TransactionDT", ""))
            if dt_val is None:
                continue
            ts = (REF_DATE + timedelta(seconds=dt_val)).strftime(
                "%Y-%m-%dT%H:%M:%S.000Z"
            )
            is_fraud = _int(row.get("isFraud", "")) or 0
            if is_fraud:
                fraud_count += 1

            obj: dict = {
                "transaction_id": str(tid),
                "timestamp": ts,
                "is_fraud": is_fraud,
            }

            amt = _float(row.get("TransactionAmt", ""))
            if amt is not None:
                obj["amount"] = amt

            pcd = _str(row.get("ProductCD", ""))
            if pcd:
                obj["product_cd"] = pcd

            card: dict = {}
            c1 = _str(row.get("card1", ""))
            if c1:
                card["number"] = c1
            c4 = _str(row.get("card4", ""))
            if c4:
                card["brand"] = c4
            c6 = _str(row.get("card6", ""))
            if c6:
                card["type"] = c6
            if card:
                obj["card"] = card

            addr: dict = {}
            a1 = _int(row.get("addr1", ""))
            if a1 is not None:
                addr["region1"] = str(a1)
            a2 = _int(row.get("addr2", ""))
            if a2 is not None:
                addr["region2"] = str(a2)
            if addr:
                obj["address"] = addr

            dist: dict = {}
            d1 = _float(row.get("dist1", ""))
            if d1 is not None:
                dist["dist1"] = d1
            d2 = _float(row.get("dist2", ""))
            if d2 is not None:
                dist["dist2"] = d2
            if dist:
                obj["distance"] = dist

            email: dict = {}
            pe = _str(row.get("P_emaildomain", ""))
            if pe:
                email["purchaser"] = pe
            re_ = _str(row.get("R_emaildomain", ""))
            if re_:
                email["recipient"] = re_
            if email:
                obj["email"] = email

            counting: dict = {}
            for col in C_COLS:
                v = _float(row.get(col, ""))
                if v is not None:
                    counting[f"c{col[1:]}"] = v
            if counting:
                obj["counting"] = counting

            timedelta_d: dict = {}
            for col in D_COLS:
                v = _float(row.get(col, ""))
                if v is not None:
                    timedelta_d[f"d{col[1:]}"] = v
            if timedelta_d:
                obj["timedelta"] = timedelta_d

            match: dict = {}
            for col in M_COLS:
                v = _str(row.get(col, ""))
                if v:
                    match[f"m{col[1:]}"] = v
            if match:
                obj["match"] = match

            dev = identity.get(tid)
            if dev:
                obj["device"] = dev

            fout.write(
                json.dumps(obj, separators=(",", ":")) + "\n"
            )
            count += 1

    size_mb = OUT.stat().st_size / (1024 * 1024)
    print(
        f"[build] {count:,} filas ({fraud_count:,} fraude, "
        f"{fraud_count / count * 100:.2f}%) -> {OUT.name} ({size_mb:.1f} MB)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(build())

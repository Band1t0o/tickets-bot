from __future__ import annotations

import csv
import json
import pathlib
from collections.abc import Iterable
from datetime import datetime

from .models import Offer


class Storage:
    def __init__(self, data_dir: str, seen_file: str):
        self.root = pathlib.Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.seen_file = pathlib.Path(seen_file)
        self.seen_file.parent.mkdir(parents=True, exist_ok=True)
        self._seen = set()
        if self.seen_file.exists():
            self._seen = set(x.strip() for x in self.seen_file.read_text().splitlines() if x.strip())

    def today_dir(self) -> pathlib.Path:
        d = self.root / datetime.utcnow().strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def is_new(self, offer: Offer) -> bool:
        return offer.content_hash() not in self._seen

    def mark_seen(self, offers: Iterable[Offer]) -> None:
        with self.seen_file.open("a", encoding="utf-8") as f:
            for o in offers:
                h = o.content_hash()
                if h not in self._seen:
                    f.write(h + "\n")
                    self._seen.add(h)

    def write(self, offers: list[Offer], filename_stem: str) -> tuple[str, str]:
        out_dir = self.today_dir()
        csv_path = out_dir / f"{filename_stem}.csv"
        jsonl_path = out_dir / f"{filename_stem}.jsonl"

        # CSV
        if offers:
            with csv_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(offers[0].to_dict().keys()))
                writer.writeheader()
                for o in offers:
                    writer.writerow(o.to_dict())

        # JSONL
        with jsonl_path.open("w", encoding="utf-8") as f:
            for o in offers:
                f.write(json.dumps(o.to_dict(), ensure_ascii=False) + "\n")

        # meta run info
        meta = out_dir / "meta.json"
        meta_data = {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "files": [csv_path.name, jsonl_path.name],
            "num_offers": len(offers),
        }
        if meta.exists():
            try:
                old = json.loads(meta.read_text())
                old.setdefault("runs", []).append(meta_data)
                meta.write_text(json.dumps(old, indent=2))
            except Exception:
                meta.write_text(json.dumps({"runs": [meta_data]}, indent=2))
        else:
            meta.write_text(json.dumps({"runs": [meta_data]}, indent=2))

        return str(csv_path), str(jsonl_path)

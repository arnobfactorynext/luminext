"""Observed runtime versus an explicit scheduled-on baseline, never an invented bill."""
import json
import sqlite3
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from .store import now


class EnergyProfile(BaseModel):
    enabled: bool = True
    watts: float = Field(gt=0, le=1_000_000, allow_inf_nan=False)
    tariff: float | None = Field(default=None, ge=0, le=100_000, allow_inf_nan=False)
    currency: str = Field(default="BDT", min_length=1, max_length=8, pattern=r"^[A-Z]+$")
    start: str = "08:00"
    end: str = "18:00"
    weekdays: list[int] = Field(default_factory=lambda: [0,1,2,3,4], min_length=1, max_length=7)
    timezone: str = "Asia/Dhaka"

    @field_validator("start", "end")
    @classmethod
    def clock(cls, value):
        datetime.strptime(value, "%H:%M")
        return value

    @field_validator("timezone")
    @classmethod
    def zone(cls, value):
        try:
            ZoneInfo(value)
        except Exception:
            raise ValueError("Unknown IANA timezone") from None
        return value

    @field_validator("weekdays")
    @classmethod
    def days(cls, value):
        if any(d < 0 or d > 6 for d in value):
            raise ValueError("Weekdays use Monday=0 through Sunday=6")
        return sorted(set(value))


def scheduled_seconds(start, end, profile):
    zone = ZoneInfo(profile["timezone"])
    day = datetime.fromtimestamp(start, zone).date() - timedelta(days=1)
    last = datetime.fromtimestamp(end, zone).date()
    a, b = time.fromisoformat(profile["start"]), time.fromisoformat(profile["end"])
    total = 0.
    while day <= last:
        if day.weekday() in profile["weekdays"]:
            left = datetime.combine(day, a, zone).timestamp()
            right = datetime.combine(day + timedelta(days=1 if b <= a else 0), b, zone).timestamp()
            total += max(0., min(end, right)-max(start, left))
        day += timedelta(days=1)
    return total


class EnergyTracker:
    def __init__(self, store):
        self.store = store
        self.active = {}
        store.db.execute("CREATE TABLE IF NOT EXISTS energy_intervals(entity TEXT,start REAL,end REAL,state INTEGER,manual INTEGER,profile TEXT)")
        store.db.execute("CREATE INDEX IF NOT EXISTS energy_time ON energy_intervals(entity,end)")
        store.db.execute("CREATE INDEX IF NOT EXISTS energy_end ON energy_intervals(end)")
        store.db.execute("INSERT OR IGNORE INTO schema_version VALUES(2)")
        store.db.commit()

    def observe(self, entity, at=None, force=False):
        at = now() if at is None else at
        id = entity["id"]
        profile = entity.get("energy_profile")
        valid = entity.get("available") and isinstance(entity.get("state"), bool) and profile and profile.get("enabled", True) and not entity.get("simulated")
        current = {"state": entity.get("state"), "manual": bool(entity.get("override")) or entity.get("control_source")=="manual", "profile": profile} if valid else None
        old = self.active.get(id)
        if old and (force or not current or any(old[k] != current[k] for k in current) or at-old["start"] >= 300):
            if at > old["start"]:
                self.store.db.execute("INSERT INTO energy_intervals VALUES(?,?,?,?,?,?)", (id,old["start"],at,old["state"],old["manual"],json.dumps(old["profile"])))
                self.store.db.commit()
            self.active.pop(id, None)
        if current and id not in self.active:
            self.active[id] = {**current, "start": at}

    def tick(self, entities, at=None):
        at = now() if at is None else at
        for entity in entities:
            if entity["type"] == "switch":
                self.observe(entity, at)

    def flush(self, entities):
        for entity in entities:
            if entity["type"] == "switch":
                self.observe(entity, force=True)
        self.active.clear()

    def report(self, entities, days=30, at=None, active=None):
        at = now() if at is None else at
        start = at-days*86400
        db = sqlite3.connect(f"file:{(self.store.directory/'luminext.db').resolve().as_posix()}?mode=ro",uri=True)
        db.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in db.execute("SELECT * FROM energy_intervals WHERE end>? AND start<?", (start,at))]
        finally:
            db.close()
        pending = self.active if active is None else active
        rows += [{"entity": id,"start":v["start"],"end":at,"state":v["state"],"manual":v["manual"],"profile":v["profile"]} for id,v in pending.items()]
        totals = {}
        for row in rows:
            a,b = max(start,row["start"]),min(at,row["end"])
            if b <= a:
                continue
            p = json.loads(row["profile"]) if isinstance(row["profile"],str) else row["profile"]
            scheduled = scheduled_seconds(a,b,p)
            item = totals.setdefault(row["entity"], {"entity_id":row["entity"],"observed_hours":0.,"baseline_hours":0.,"on_hours":0.,"avoided_hours":0.,"manual_avoided_hours":0.,"estimated_actual_kwh":0.,"estimated_baseline_kwh":0.,"estimated_avoided_kwh":0.,"estimated_cost_avoided":0.,"currencies":set(),"missing_tariff":False})
            hours, baseline = (b-a)/3600, scheduled/3600
            on = hours if row["state"] else 0.
            avoided = 0. if row["state"] else baseline
            item["observed_hours"] += hours
            item["baseline_hours"] += baseline
            item["on_hours"] += on
            item["avoided_hours"] += avoided
            item["manual_avoided_hours"] += avoided if row["manual"] else 0
            item["estimated_actual_kwh"] += on*p["watts"]/1000
            item["estimated_baseline_kwh"] += baseline*p["watts"]/1000
            item["estimated_avoided_kwh"] += avoided*p["watts"]/1000
            if p.get("tariff") is None:
                item["missing_tariff"] = True
            else:
                item["estimated_cost_avoided"] += avoided*p["watts"]/1000*p["tariff"]
                item["currencies"].add(p["currency"])
        names = {e["id"]:e["name"] for e in entities}
        output = []
        for item in totals.values():
            currencies = item.pop("currencies")
            missing = item.pop("missing_tariff")
            item["currency"] = next(iter(currencies)) if len(currencies)==1 else None
            if missing or len(currencies)!=1:
                item["estimated_cost_avoided"] = None
            item["name"] = names.get(item["entity_id"], item["entity_id"])
            item["estimated_net_kwh_delta"] = item["estimated_baseline_kwh"]-item["estimated_actual_kwh"]
            output.append(item)
        return {"start":start,"end":at,"days":days,"method":"Estimated from observed relay states and configured rated watts; avoided runtime is only within the scheduled-on baseline. Manual off periods are included, not attributed exclusively to automation. Offline time, controller downtime and simulated devices are excluded. This is not a utility-meter measurement.","loads":output}

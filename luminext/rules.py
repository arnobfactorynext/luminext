"""Validated HA-style subset; never evaluates templates or arbitrary Python."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import yaml

from .store import now, uid


class RuleError(ValueError):
    pass


def as_list(value):
    return value if isinstance(value, list) else [value]


def parse_rule(text, entities):
    if len(text) > 100_000:
        raise RuleError("Rule exceeds 100 KB")
    try:
        rule = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise RuleError(str(e)) from e
    if not isinstance(rule, dict):
        raise RuleError("An automation must be a YAML mapping")
    if any(not isinstance(key, str) for key in rule):
        raise RuleError("Automation keys must be strings")
    allowed = {"id", "alias", "description", "trigger", "triggers", "condition", "conditions", "action", "actions", "mode", "max"}
    if set(rule) - allowed:
        raise RuleError(f"Unsupported keys: {', '.join(set(rule) - allowed)}. See the supported YAML reference.")
    rule["trigger"] = as_list(rule.get("trigger", rule.get("triggers", [])))
    rule["action"] = as_list(rule.get("action", rule.get("actions", [])))
    rule["condition"] = as_list(rule.get("condition", rule.get("conditions", [])))
    rule["mode"] = rule.get("mode", "single")
    rule["max"] = int(rule.get("max", 10))
    if rule["mode"] not in {"single", "restart", "queued", "parallel"} or not 1 <= rule["max"] <= 50:
        raise RuleError("mode must be single/restart/queued/parallel; max must be 1..50")
    if not rule["trigger"] or not rule["action"]:
        raise RuleError("Provide at least one trigger and one action")

    def keys(item, allowed_keys):
        if not isinstance(item, dict):
            raise RuleError("Expected a mapping")
        if any(not isinstance(key, str) for key in item):
            raise RuleError("Option keys must be strings")
        unsupported = set(item) - set(allowed_keys)
        if unsupported:
            raise RuleError(f"Unsupported option(s): {', '.join(unsupported)}")

    def entity_ids(item, required=True):
        ids = as_list(item.get("entity_id", []))
        if required and not ids:
            raise RuleError("entity_id is required")
        for id in ids:
            if id not in entities:
                raise RuleError(f"Unknown entity: {id}")
        return ids

    def condition(item, depth=0):
        if depth > 8 or not isinstance(item, dict):
            raise RuleError("Invalid or excessively nested condition")
        kind = item.get("condition")
        if kind in {"and", "or", "not"}:
            keys(item, {"condition", "conditions"})
            nested = item.get("conditions", [])
            if not isinstance(nested, list) or not nested:
                raise RuleError("Condition group needs conditions")
            for child in nested:
                condition(child, depth + 1)
        elif kind in {"state", "numeric_state"}:
            keys(item, {"condition", "entity_id", "state"} if kind == "state" else {"condition", "entity_id", "above", "below"})
            entity_ids(item)
            if kind == "state" and str(item.get("state")).lower() not in {"on", "off", "unknown", "unavailable", "true", "false"}:
                raise RuleError("State must be on/off/unknown/unavailable")
            if kind == "numeric_state":
                numeric(item)
        elif kind == "time":
            keys(item, {"condition", "after", "before"})
            for field in ("after", "before"):
                if field in item:
                    datetime.strptime(str(item[field]), "%H:%M:%S")
        else:
            raise RuleError(f"Unsupported condition: {kind}")

    def numeric(item):
        if "above" not in item and "below" not in item:
            raise RuleError("numeric_state needs above or below")
        for field in ("above", "below"):
            if field in item:
                float(item[field])

    def actions(items, depth=0):
        if depth > 8 or len(items) > 100:
            raise RuleError("Too many or excessively nested actions")
        for action in items:
            if not isinstance(action, dict):
                raise RuleError("Each action must be a mapping")
            if "delay" in action:
                keys(action, {"delay"})
                delay_seconds(action["delay"])
            elif "choose" in action:
                keys(action, {"choose", "default"})
                for branch in action["choose"]:
                    keys(branch, {"conditions", "sequence"})
                    for c in as_list(branch.get("conditions", [])):
                        condition(c, depth + 1)
                    actions(branch.get("sequence", []), depth + 1)
                actions(action.get("default", []), depth + 1)
            else:
                keys(action, {"service", "action", "target", "entity_id", "data"})
                service = action.get("service", action.get("action"))
                if service not in {"switch.turn_on", "switch.turn_off", "switch.toggle", "luminext.notify"}:
                    raise RuleError(f"Unsupported action: {service}")
                if service.startswith("switch."):
                    target = action.get("target", action)
                    for id in entity_ids(target):
                        if entities[id]["type"] != "switch":
                            raise RuleError(f"Not a switch: {id}")
    try:
        for t in rule["trigger"]:
            platform = t.get("platform", t.get("trigger"))
            if platform in {"state", "numeric_state"}:
                keys(t, {"platform", "trigger", "entity_id", "from", "to"} if platform == "state" else {"platform", "trigger", "entity_id", "above", "below"})
                entity_ids(t)
                if platform == "numeric_state":
                    numeric(t)
                if "for" in t:
                    raise RuleError("Trigger 'for' is not supported; configure camera off-delay or an action delay")
            elif platform == "time":
                keys(t, {"platform", "trigger", "at"})
                datetime.strptime(str(t["at"]), "%H:%M:%S")
            elif platform == "manual":
                keys(t, {"platform", "trigger"})
            else:
                raise RuleError(f"Unsupported trigger: {platform}")
        for c in rule["condition"]:
            condition(c)
        actions(rule["action"])
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError) as e:
        raise RuleError(str(e)) from e
    return rule


def state_string(value):
    if value is True:
        return "on"
    if value is False:
        return "off"
    return str(value).lower()


def delay_seconds(value):
    if isinstance(value, dict):
        seconds = float(value.get("seconds", 0)) + 60 * float(value.get("minutes", 0)) + 3600 * float(value.get("hours", 0))
    elif isinstance(value, str) and ":" in value:
        h, m, s = map(float, value.split(":"))
        seconds = h * 3600 + m * 60 + s
    else:
        seconds = float(value)
    if not 0 <= seconds <= 86400:
        raise RuleError("Delay must be 0..86400 seconds")
    return seconds


class RuleEngine:
    def __init__(self, runtime):
        self.rt = runtime
        self.running = {}
        self.last_schedule = {}

    def matches_numeric(self, condition, value):
        try:
            v = float(value)
            return ("above" not in condition or v > float(condition["above"])) and ("below" not in condition or v < float(condition["below"]))
        except (TypeError, ValueError):
            return False

    def conditions(self, items):
        return all(self.condition(c) for c in items)

    def condition(self, c):
        kind = c["condition"]
        if kind in {"and", "or", "not"}:
            values = [self.condition(x) for x in c["conditions"]]
            return all(values) if kind == "and" else any(values) if kind == "or" else not any(values)
        if kind == "time":
            local = datetime.now(ZoneInfo(self.rt.settings().get("timezone", "Asia/Dhaka"))).strftime("%H:%M:%S")
            start, end = c.get("after", "00:00:00"), c.get("before", "23:59:59")
            return start <= local <= end if start <= end else local >= start or local <= end
        values = [self.rt.entities[id].get("state") for id in as_list(c["entity_id"])]
        if kind == "state":
            return all(state_string(v) == state_string(c["state"]) for v in values)
        return all(self.matches_numeric(c, v) for v in values)

    async def on_state(self, id, old, new):
        for record in self.rt.store.all("automation"):
            if not record.get("enabled", True):
                continue
            rule = record["rule"]
            for t in rule["trigger"]:
                kind = t.get("platform", t.get("trigger"))
                if id not in as_list(t.get("entity_id", [])):
                    continue
                matched = False
                if kind == "state":
                    matched = ("to" not in t or state_string(new) == state_string(t["to"])) and ("from" not in t or state_string(old) == state_string(t["from"]))
                elif kind == "numeric_state":
                    matched = self.matches_numeric(t, new) and not self.matches_numeric(t, old)
                if matched:
                    await self.trigger(record, {"entity_id": id, "from": old, "to": new})
                    break

    async def tick(self):
        local = datetime.now(ZoneInfo(self.rt.settings().get("timezone", "Asia/Dhaka")))
        for record in self.rt.store.all("automation"):
            if not record.get("enabled", True):
                continue
            for t in record["rule"]["trigger"]:
                if t.get("platform", t.get("trigger")) == "time" and t["at"] == local.strftime("%H:%M:%S"):
                    key = local.strftime("%Y-%m-%d %H:%M:%S")
                    if self.last_schedule.get(record["id"]) != key:
                        self.last_schedule[record["id"]] = key
                        await self.trigger(record, {"time": key})

    async def trigger(self, record, trigger, manual=False):
        if not record.get("enabled", True) and not manual:
            return
        id, rule = record["id"], record["rule"]
        active = [t for t in self.running.get(id, []) if not t.done()]
        mode = rule["mode"]
        if mode == "single" and active:
            return
        if mode == "restart":
            for task in active:
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            active = []
        if len(active) >= rule["max"]:
            self.rt.store.event("trace", id, {"result": "max_runs_reached", "trigger": trigger})
            return
        previous = active[-1] if active and mode == "queued" else None
        task = asyncio.create_task(self.run(record, trigger, previous))
        self.running[id] = active + [task]

    async def cancel(self, id):
        tasks = self.running.pop(id, [])
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self, record, trigger, previous=None):
        trace = {"id": uid("run"), "started": now(), "trigger": trigger, "steps": []}
        try:
            if previous:
                try:
                    await asyncio.shield(previous)
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
            passed = self.conditions(record["rule"]["condition"])
            trace["conditions_passed"] = passed
            if passed:
                await self.actions(record["rule"]["action"], trace["steps"])
            trace["result"] = "completed" if passed else "conditions_not_met"
        except asyncio.CancelledError:
            trace["result"] = "cancelled"
            raise
        except Exception as e:
            trace["result"] = "failed"
            trace["error"] = str(e)
        finally:
            trace["finished"] = now()
            self.rt.store.event("trace", record["id"], trace)
            await self.rt.broadcast({"type": "refresh"})

    async def actions(self, actions, steps):
        for action in actions:
            if "delay" in action:
                seconds = delay_seconds(action["delay"])
                steps.append({"delay": seconds})
                await asyncio.sleep(seconds)
            elif "choose" in action:
                chosen = next((b for b in action["choose"] if self.conditions(as_list(b.get("conditions", [])))), None)
                steps.append({"choose": chosen is not None})
                await self.actions(chosen["sequence"] if chosen else action.get("default", []), steps)
            else:
                service = action.get("service", action.get("action"))
                if service == "luminext.notify":
                    message = str(action.get("data", {}).get("message", "Automation notification"))[:1000]
                    self.rt.store.event("notification", "system", {"message": message})
                    steps.append({"notification": message})
                else:
                    ids = as_list(action.get("target", action)["entity_id"])
                    results = await asyncio.gather(*(self.rt.command_switch(id, service.split(".")[1], "automation") for id in ids))
                    steps.extend({"entity_id": id, **result} for id, result in zip(ids, results))

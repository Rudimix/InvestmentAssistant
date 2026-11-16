"""Общие публичные справочники брокера с неизменяемыми версиями и временем наблюдения."""

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from .mcp_client import ToolError
from .models import BrokerMetadataRecord, data_json, timestamp


class BrokerMetadata:
    def __init__(self, repo, clock=None):
        self.repo = repo
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._locks = defaultdict(asyncio.Lock)

    async def get(self, market, tool: str, identity: str, call=None) -> BrokerMetadataRecord:
        if tool not in {"invest_get_asset", "invest_get_brand"} or not identity:
            raise ValueError("Не указан поддерживаемый справочник или идентификатор")
        key = f"broker-meta:{tool}:{identity}"
        async with self._locks[key]:
            now = timestamp(self.clock())
            cached = self.repo.get_source_refresh(key)
            if cached and cached["last_success_at"] and now-timestamp(cached["last_success_at"]) < timedelta(days=1):
                record = self.repo.get_broker_metadata(tool, identity, cached["payload"]["version"])
                if record:
                    return record
            raw = await (call or market.report_call)(tool, {"id": identity})
            if tool == "invest_get_asset":
                asset = raw.get("asset", {})
                payload = {name: asset[name] for name in ("uid", "type", "name", "instruments") if name in asset}
                payload["brand"] = {"uid": asset.get("brand", {}).get("uid")}
            else:
                payload = {name: raw[name] for name in ("uid", "name", "company", "sector", "countryOfRisk") if name in raw}
            if payload.get("uid") != identity:
                raise ValueError("Справочник не подтвердил запрошенный идентификатор")
            version = sha256(data_json(payload).encode()).hexdigest()
            record = self.repo.save_broker_metadata(BrokerMetadataRecord(tool, identity, version, timestamp(self.clock()), payload))
            self.repo.save_source_refresh(key, "complete", {"version": record.version}, self.clock())
            return record


async def enrich_snapshot_sectors(repo, market, snapshot, clock):
    metadata = BrokerMetadata(repo, clock)
    for position in snapshot.positions:
        if position.sector:
            position.sector_as_of = position.sector_as_of or snapshot.observed_at
            continue
        if not position.asset_id or position.instrument_type not in {"share", "bond"}:
            continue
        try:
            asset = await metadata.get(market, "invest_get_asset", position.asset_id)
            brand_id = asset.payload.get("brand", {}).get("uid")
            if not brand_id:
                continue
            brand = await metadata.get(market, "invest_get_brand", brand_id)
            position.sector = str(brand.payload.get("sector") or "")
            position.sector_as_of = max(asset.observed_at, brand.observed_at)
            position.sector_source_version = brand.version
        except (ToolError, ValueError, TypeError, AttributeError):
            continue
    return snapshot


def historical_sector(repo, asset_id: str, as_of):
    asset = repo.get_broker_metadata("invest_get_asset", asset_id, as_of=as_of)
    if asset is None:
        return {}
    brand_id = asset.payload.get("brand", {}).get("uid")
    brand = repo.get_broker_metadata("invest_get_brand", brand_id, as_of=as_of) if brand_id else None
    if brand is None:
        return {}
    return {"sector": str(brand.payload.get("sector") or ""), "sector_as_of": max(asset.observed_at, brand.observed_at),
            "sector_source_version": brand.version}

from __future__ import annotations

import json
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional
from uuid import uuid4

import paho.mqtt.client as mqtt
from paho.mqtt import MQTTException
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError, PyMongoError


load_dotenv(Path(__file__).with_name(".env"))


@dataclass(frozen=True)
class AppConfig:
    mongodb_uri: str
    mongodb_database: str
    mqtt_host: str
    mqtt_port: int
    mqtt_name: str
    mqtt_username: Optional[str]
    mqtt_password: Optional[str]
    mqtt_tls: bool
    mqtt_keepalive: int
    mqtt_publish_timeout: int


def load_config() -> AppConfig:
    required = ("MONGODB_URI", "MQTT_HOST", "MQTT_NAME")
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    mqtt_name = os.environ["MQTT_NAME"].strip()
    if any(character in mqtt_name for character in ("/", "+", "#")):
        raise RuntimeError("MQTT_NAME cannot contain '/', '+' or '#'.")

    try:
        mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
        mqtt_keepalive = int(os.getenv("MQTT_KEEPALIVE", "30"))
        mqtt_publish_timeout = int(os.getenv("MQTT_PUBLISH_TIMEOUT", "10"))
    except ValueError as exc:
        raise RuntimeError(
            "MQTT_PORT, MQTT_KEEPALIVE, and MQTT_PUBLISH_TIMEOUT must be integers."
        ) from exc

    if not 1 <= mqtt_port <= 65535:
        raise RuntimeError("MQTT_PORT must be between 1 and 65535.")
    if mqtt_keepalive < 1 or mqtt_publish_timeout < 1:
        raise RuntimeError("MQTT_KEEPALIVE and MQTT_PUBLISH_TIMEOUT must be positive.")

    mqtt_tls_value = os.getenv("MQTT_TLS", "false").strip().lower()
    if mqtt_tls_value not in {"true", "false"}:
        raise RuntimeError("MQTT_TLS must be 'true' or 'false'.")

    return AppConfig(
        mongodb_uri=os.environ["MONGODB_URI"].strip(),
        mongodb_database=os.getenv("MONGODB_DATABASE", "ecse3038").strip(),
        mqtt_host=os.environ["MQTT_HOST"].strip(),
        mqtt_port=mqtt_port,
        mqtt_name=mqtt_name,
        mqtt_username=os.getenv("MQTT_USERNAME") or None,
        mqtt_password=os.getenv("MQTT_PASSWORD") or None,
        mqtt_tls=mqtt_tls_value == "true",
        mqtt_keepalive=mqtt_keepalive,
        mqtt_publish_timeout=mqtt_publish_timeout,
    )


class FanCreate(BaseModel):
    mac: str = Field(min_length=1)
    room: str = Field(min_length=1)


class FanPublic(BaseModel):
    mac: str
    room: str


class FanCreated(FanPublic):
    api_key: str


class ReadingCreate(BaseModel):
    mac: str = Field(min_length=1)
    humidity: float = Field(ge=0, le=100, allow_inf_nan=False)


class ReadingPublic(BaseModel):
    mac: str
    humidity: float
    timestamp: datetime


class FanSettings(BaseModel):
    mode: Literal["auto", "on", "off"]
    max_humidity: float = Field(ge=0, le=100, allow_inf_nan=False)


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _collection(request: Request, name: str) -> Collection:
    return request.app.state.database[name]


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    client = MongoClient(config.mongodb_uri, serverSelectionTimeoutMS=5000)
    try:
        client.admin.command("ping")
        database = client[config.mongodb_database]
        fans = database["fans"]
        readings = database["readings"]
        settings = database["settings"]

        fans.create_index([("mac", ASCENDING)], unique=True)
        fans.create_index([("api_key", ASCENDING)], unique=True)
        readings.create_index([("timestamp", DESCENDING)])
        readings.create_index([("mac", ASCENDING), ("timestamp", DESCENDING)])
        settings.create_index([("mac", ASCENDING)], unique=True)

        app.state.mongo_client = client
        app.state.database = database
        app.state.config = config
    except PyMongoError as exc:
        client.close()
        raise RuntimeError(
            "Could not connect to or initialize MongoDB. Check MONGODB_URI and Atlas network access."
        ) from exc

    try:
        yield
    finally:
        client.close()


app = FastAPI(title="ECSE3038 Fan API", lifespan=lifespan)


@app.post("/fans", response_model=FanCreated, status_code=201)
def create_fan(fan: FanCreate, request: Request) -> FanCreated:
    fans = _collection(request, "fans")
    api_key = secrets.token_urlsafe(16)
    try:
        fans.insert_one(
            {"mac": fan.mac, "room": fan.room, "api_key": api_key}
        )
    except DuplicateKeyError as exc:
        if fans.find_one({"mac": fan.mac}, {"_id": 1}):
            raise HTTPException(status_code=409, detail="A fan with this MAC already exists.") from exc
        raise HTTPException(
            status_code=503, detail="Could not create a unique API key; please retry."
        ) from exc

    return FanCreated(mac=fan.mac, room=fan.room, api_key=api_key)


@app.get("/fans", response_model=list[FanPublic])
def list_fans(request: Request) -> list[FanPublic]:
    fans = _collection(request, "fans")
    documents = fans.find(
        {}, {"_id": 0, "mac": 1, "room": 1}
    ).sort("mac", ASCENDING)
    return [FanPublic(**document) for document in documents]


@app.get("/fans/{mac}", response_model=FanPublic)
def get_fan(mac: str, request: Request) -> FanPublic:
    fan = _collection(request, "fans").find_one(
        {"mac": mac}, {"_id": 0, "mac": 1, "room": 1}
    )
    if fan is None:
        raise HTTPException(status_code=404, detail="Fan not found.")
    return FanPublic(**fan)


@app.post("/readings", response_model=ReadingPublic, status_code=201)
def create_reading(
    reading: ReadingCreate,
    request: Request,
    api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> ReadingPublic:
    fans = _collection(request, "fans")
    if not api_key:
        raise HTTPException(status_code=401, detail="X-API-Key header is required.")

    key_owner = fans.find_one({"api_key": api_key}, {"_id": 0, "mac": 1})
    if key_owner is None:
        raise HTTPException(status_code=401, detail="Invalid API key.")

    fan = fans.find_one({"mac": reading.mac}, {"_id": 0, "mac": 1})
    if fan is None:
        raise HTTPException(status_code=404, detail="Fan not found.")
    if key_owner["mac"] != reading.mac:
        raise HTTPException(status_code=403, detail="API key belongs to another fan.")

    timestamp = datetime.now(timezone.utc)
    _collection(request, "readings").insert_one(
        {
            "mac": reading.mac,
            "humidity": reading.humidity,
            "timestamp": timestamp,
        }
    )
    return ReadingPublic(
        mac=reading.mac, humidity=reading.humidity, timestamp=timestamp
    )


@app.get("/readings", response_model=list[ReadingPublic])
def list_readings(
    request: Request,
    mac: Optional[str] = Query(default=None, min_length=1),
    limit: int = Query(default=10, ge=1, le=100),
) -> list[ReadingPublic]:
    query = {"mac": mac} if mac is not None else {}
    documents = _collection(request, "readings").find(
        query, {"_id": 0, "mac": 1, "humidity": 1, "timestamp": 1}
    ).sort([("timestamp", DESCENDING), ("_id", DESCENDING)]).limit(limit)

    return [
        ReadingPublic(
            mac=document["mac"],
            humidity=document["humidity"],
            timestamp=_utc_datetime(document["timestamp"]),
        )
        for document in documents
    ]


def _publish_settings(config: AppConfig, settings: FanSettings) -> None:
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"ecse3038-api-{uuid4().hex[:12]}",
    )
    if config.mqtt_username:
        client.username_pw_set(config.mqtt_username, config.mqtt_password)
    if config.mqtt_tls:
        client.tls_set()

    loop_started = False
    try:
        client.connect(
            config.mqtt_host, config.mqtt_port, config.mqtt_keepalive
        )
        client.loop_start()
        loop_started = True
        topic = f"ecse3038/{config.mqtt_name}/fan/settings"
        payload = json.dumps(settings.model_dump(), separators=(",", ":"))
        result = client.publish(topic, payload, qos=1, retain=True)
        result.wait_for_publish(timeout=config.mqtt_publish_timeout)
        if not result.is_published():
            raise TimeoutError("MQTT publish did not complete before the timeout.")
    except (OSError, MQTTException, RuntimeError, TimeoutError) as exc:
        raise RuntimeError("MQTT settings publish failed.") from exc
    finally:
        if loop_started:
            client.disconnect()
            client.loop_stop()


@app.put("/fans/{mac}/settings", response_model=FanSettings)
def put_fan_settings(
    mac: str,
    settings: FanSettings,
    request: Request,
    response: Response,
) -> FanSettings:
    if _collection(request, "fans").find_one({"mac": mac}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="Fan not found.")

    try:
        previous = _collection(request, "settings").find_one_and_update(
            {"mac": mac},
            {"$set": settings.model_dump()},
            upsert=True,
            return_document=ReturnDocument.BEFORE,
        )
    except DuplicateKeyError:
        previous = _collection(request, "settings").find_one_and_update(
            {"mac": mac},
            {"$set": settings.model_dump()},
            upsert=False,
            return_document=ReturnDocument.BEFORE,
        )

    response.status_code = 201 if previous is None else 200
    try:
        _publish_settings(request.app.state.config, settings)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Settings were saved, but MQTT publish failed.",
        ) from exc
    return settings


@app.delete("/fans/{mac}", status_code=204)
def delete_fan(
    mac: str,
    request: Request,
    api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> Response:
    fans = _collection(request, "fans")
    if not api_key:
        raise HTTPException(status_code=401, detail="X-API-Key header is required.")

    key_owner = fans.find_one({"api_key": api_key}, {"_id": 0, "mac": 1})
    if key_owner is None:
        raise HTTPException(status_code=401, detail="Invalid API key.")

    fan = fans.find_one({"mac": mac}, {"_id": 0, "mac": 1})
    if fan is None:
        raise HTTPException(status_code=404, detail="Fan not found.")
    if key_owner["mac"] != mac:
        raise HTTPException(status_code=403, detail="API key belongs to another fan.")

    result = fans.delete_one({"mac": mac, "api_key": api_key})
    if result.deleted_count != 1:
        raise HTTPException(status_code=404, detail="Fan not found.")
    _collection(request, "settings").delete_one({"mac": mac})
    return Response(status_code=204)

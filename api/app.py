import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal, Optional

import paho.mqtt.client as mqtt
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field
from pymongo import DESCENDING, MongoClient

load_dotenv()

MONGODB_URI = os.environ["MONGODB_URI"]
MQTT_NAME = os.environ["MQTT_NAME"]  # lowercase letters and digits
BROKER_HOST = "135.232.203.144"
BROKER_PORT = 1883
SETTINGS_TOPIC = f"ecse3038/{MQTT_NAME}/fan/settings"

# ---------- MongoDB ----------
client = MongoClient(MONGODB_URI, tz_aware=True)
db = client["bathroom"]
fans = db["fans"]
readings = db["readings"]
settings_col = db["settings"]
fans.create_index("mac", unique=True)

# ---------- MQTT ----------
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # connect_async + loop_start: the API still starts if the broker is slow
    mqtt_client.connect_async(BROKER_HOST, BROKER_PORT, keepalive=60)
    mqtt_client.loop_start()
    yield
    mqtt_client.loop_stop()
    mqtt_client.disconnect()


app = FastAPI(title="Bathroom fan monitor", lifespan=lifespan)


# ---------- Models ----------
class FanIn(BaseModel):
    mac: str
    room: str


class ReadingIn(BaseModel):
    mac: str
    humidity: float = Field(ge=0, le=100)  # 422 outside 0..100


class SettingsIn(BaseModel):
    mode: Literal["auto", "on", "off"]  # 422 if anything else
    max_humidity: float = Field(ge=0, le=100)  # 422 outside 0..100


# ---------- A1: fans ----------
@app.post("/fans", status_code=201)
def create_fan(fan: FanIn):
    if fans.find_one({"mac": fan.mac}):
        raise HTTPException(status_code=409, detail="Fan already registered")
    key = secrets.token_urlsafe(16)
    fans.insert_one({"mac": fan.mac, "room": fan.room, "api_key": key})
    # The key is returned only here, once
    return {"mac": fan.mac, "room": fan.room, "api_key": key}


@app.get("/fans")
def list_fans():
    # Never expose the keys (or Mongo's _id)
    return list(fans.find({}, {"_id": 0, "api_key": 0}))


@app.get("/fans/{mac}")
def get_fan(mac: str):
    fan = fans.find_one({"mac": mac}, {"_id": 0, "api_key": 0})
    if fan is None:
        raise HTTPException(status_code=404, detail="Unknown fan")
    return fan


# ---------- A2: post a reading ----------
@app.post("/readings", status_code=201)
def create_reading(reading: ReadingIn, x_api_key: str | None = Header(default=None)):
    if x_api_key is None:
        raise HTTPException(status_code=401, detail="Missing API key")

    owner = fans.find_one({"api_key": x_api_key})
    if owner is None:
        raise HTTPException(status_code=401, detail="Unknown API key")
    if owner["mac"] != reading.mac:
        raise HTTPException(status_code=403, detail="Key belongs to another fan")

    doc = {
        "mac": reading.mac,
        "humidity": reading.humidity,
        "time": datetime.now(timezone.utc),  # server adds the time
    }
    readings.insert_one(doc)
    return {"mac": doc["mac"], "humidity": doc["humidity"], "time": doc["time"]}


# ---------- A3: list readings ----------
@app.get("/readings")
def list_readings(
    mac: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=100),
):
    query = {}
    if mac:
        query["mac"] = mac
    return list(readings.find(query, {"_id": 0}).sort("time", DESCENDING).limit(limit))


# ---------- A4: fan settings ----------
@app.put("/fans/{mac}/settings")
def set_fan_settings(mac: str, settings: SettingsIn, response: Response):
    if fans.find_one({"mac": mac}) is None:
        raise HTTPException(status_code=404, detail="Unknown fan")
    if not mqtt_client.is_connected():
        raise HTTPException(
            status_code=503,
            detail="MQTT broker is not connected; settings were not saved.",
        )

    existing = settings_col.find_one({"mac": mac})
    settings_col.update_one(
        {"mac": mac},
        {
            "$set": {
                "mac": mac,
                "mode": settings.mode,
                "max_humidity": settings.max_humidity,
            }
        },
        upsert=True,
    )
    response.status_code = 200 if existing else 201

    payload = json.dumps(
        {"mode": settings.mode, "max_humidity": float(settings.max_humidity)}
    )
    try:
        publish_info = mqtt_client.publish(
            SETTINGS_TOPIC, payload, qos=1, retain=True
        )
        if publish_info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(
                f"MQTT publish failed with result code {publish_info.rc}."
            )
        publish_info.wait_for_publish(timeout=10)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Settings were saved, but the MQTT publish failed or timed out.",
        ) from exc
    if not publish_info.is_published():
        raise HTTPException(
            status_code=503,
            detail="Settings were saved, but the MQTT publish was not acknowledged.",
        )
    return {"mac": mac, "mode": settings.mode, "max_humidity": settings.max_humidity}


# ---------- A5: delete a fan ----------
@app.delete("/fans/{mac}", status_code=204)
def delete_fan(mac: str, x_api_key: str | None = Header(default=None)):
    if x_api_key is None:
        raise HTTPException(status_code=401, detail="Missing API key")

    owner = fans.find_one({"api_key": x_api_key})
    if owner is None:
        raise HTTPException(status_code=401, detail="Unknown API key")

    fan = fans.find_one({"mac": mac})
    if fan is None:
        raise HTTPException(status_code=404, detail="Unknown fan")

    if owner["mac"] != mac:
        raise HTTPException(status_code=403, detail="Key belongs to another fan")

    fans.delete_one({"mac": mac})
    return Response(status_code=204)

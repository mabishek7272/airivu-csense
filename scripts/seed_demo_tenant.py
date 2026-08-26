"""Seeds a tenant with realistic sites, cameras, detections, evidence and incidents.

Useful for exercising the CRM against something that looks like a real estate rather than
a single row: several cameras across two sites, staggered capture times, and a mix of
detections that did and did not become incidents.

Run from the repo root with the stack up:
    python scripts/seed_demo_tenant.py

Prints the generated credentials once. Local development only.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.request
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend", "shared"))

import cv2
import numpy as np
from csense_shared.pipeline.detections import (
    DetectionDocument,
    attach_evidence_ref,
    record_detection,
)
from csense_shared.pipeline.evidence import Annotation, capture_evidence
from csense_shared.pipeline.incidents import upsert_incident_from_match
from csense_shared.pipeline.rules import DetectedObject, Rule, evaluate
from csense_shared.storage.objects import create_client
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
INFRA = os.path.join(REPO, "infra")
API = "http://localhost:8080"
SAMPLE_IMAGE = "https://ultralytics.com/images/bus.jpg"
PASSWORD = "CrmDemo!Password123"

SITES = [
    ("Chennai Distribution Centre", "chennai-dc",
     {"line1": "12 Anna Salai", "city": "Chennai", "country": "IN"}, 13.082680, 80.270721, "Asia/Kolkata"),
    ("Coimbatore Cold Store", "cbe-cold",
     {"line1": "44 Avinashi Road", "city": "Coimbatore", "country": "IN"}, 11.016844, 76.955833, "Asia/Kolkata"),
]

CAMERAS = [
    ("Loading Bay 2", "cam-bay-02", "Hikvision", "DS-2CD2143G2"),
    ("Rear Perimeter", "cam-perim-01", "Dahua", "IPC-HFW3441T"),
    ("Chiller Entrance", "cam-chill-03", "Axis", "P3265-LVE"),
]

RESTRICTED_ZONE = [[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]]


def env(key: str) -> str:
    with open(os.path.join(REPO, ".env"), encoding="utf-8") as handle:
        for line in handle:
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    raise KeyError(key)


class MinioSettings:
    minio_endpoint = "localhost:9000"
    minio_use_tls = False

    def __init__(self) -> None:
        self.minio_root_user = env("MINIO_ROOT_USER")
        self.minio_root_password = env("MINIO_ROOT_PASSWORD")


def register_tenant() -> tuple[str, str]:
    email = f"ops-{uuid.uuid4().hex[:8]}@northwind.example"
    request = urllib.request.Request(
        f"{API}/api/v1/auth/register",
        data=json.dumps({
            "organization_name": "Northwind Logistics",
            "email": email,
            "password": PASSWORD,
            "display_name": "Operations Lead",
        }).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return email, json.load(response)["tenant_id"]


def run_inference(frame: bytes) -> dict:
    """Calls the AI runtime, which has no public route - so the request is made from
    inside the container network."""
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in (("model_name", "yolov8n-general"), ("confidence", "0.25")):
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="frame"; filename="f.jpg"\r\n'
        f"Content-Type: image/jpeg\r\n\r\n".encode()
    )
    parts.append(frame)
    parts.append(f"\r\n--{boundary}--\r\n".encode())

    payload_path = os.path.join(REPO, ".seed-frame.bin")
    with open(payload_path, "wb") as handle:
        handle.write(b"".join(parts))
    try:
        subprocess.run(
            ["docker", "compose", "--env-file", "../.env", "cp", payload_path, "ai-runtime:/tmp/seed-frame.bin"],
            cwd=INFRA, capture_output=True, text=True, check=True,
        )
        script = (
            "import urllib.request\n"
            "data = open('/tmp/seed-frame.bin','rb').read()\n"
            "req = urllib.request.Request('http://localhost:8000/internal/v1/infer', data=data,\n"
            f"    headers={{'Content-Type': 'multipart/form-data; boundary={boundary}'}}, method='POST')\n"
            "print(urllib.request.urlopen(req, timeout=300).read().decode())\n"
        )
        result = subprocess.run(
            ["docker", "compose", "--env-file", "../.env", "exec", "-T", "ai-runtime", "python", "-c", script],
            cwd=INFRA, capture_output=True, text=True, timeout=600, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"inference failed:\n{result.stderr[-800:]}")
        return json.loads(result.stdout)
    finally:
        if os.path.exists(payload_path):
            os.remove(payload_path)


def prepare() -> tuple[str, str, np.ndarray, dict]:
    """Blocking setup - registration, image fetch, inference - done before the event
    loop starts, so no coroutine makes a synchronous network call."""
    email, tenant_id = register_tenant()
    print(f"Tenant   : {tenant_id}")
    frame_bytes = urllib.request.urlopen(SAMPLE_IMAGE, timeout=60).read()
    image = cv2.imdecode(np.frombuffer(frame_bytes, np.uint8), cv2.IMREAD_COLOR)
    return email, tenant_id, image, run_inference(frame_bytes)


async def main() -> int:
    email, tenant_id, image, inference = prepare()
    objects = [
        DetectedObject(d["class_name"], d["confidence"], tuple(d["bbox"]))
        for d in inference["detections"]
    ]
    print(f"Inference: {len(objects)} objects in {inference['inference_ms']}ms")

    rule = Rule(
        type_code="zone.intrusion",
        alertable_classes=frozenset({"person"}),
        min_confidence=0.5,
        severity="high",
        roi_polygon=tuple((p[0], p[1]) for p in RESTRICTED_ZONE),
        min_roi_overlap=0.3,
    )
    outcome = evaluate(rule, objects, captured_at=dt.datetime.now(dt.UTC))
    matched_boxes = {o.bbox for o in outcome.matched}
    print(f"Rule     : {len(outcome.matched)} matched, {len(outcome.rejected)} rejected")

    engine = create_async_engine(
        f"postgresql+asyncpg://csense_app:{env('POSTGRES_PASSWORD')}@localhost:5432/csense"
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    minio = create_client(MinioSettings())

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        site_ids = []
        for name, code, address, lat, lon, tz in SITES:
            site_ids.append(
                (await session.execute(
                    text(
                        "INSERT INTO sites (tenant_id,name,code,address_json,latitude,longitude,timezone) "
                        "VALUES (:t,:n,:c,CAST(:a AS jsonb),:lat,:lon,:tz) RETURNING id"
                    ),
                    {"t": tenant_id, "n": name, "c": code, "a": json.dumps(address),
                     "lat": lat, "lon": lon, "tz": tz},
                )).scalar_one()
            )

        zone_id = (await session.execute(
            text(
                "INSERT INTO zones (tenant_id,site_id,name,zone_type,geometry_json) "
                "VALUES (:t,:s,'Restricted Dock','restricted',CAST(:g AS jsonb)) RETURNING id"
            ),
            {"t": tenant_id, "s": site_ids[0], "g": json.dumps(RESTRICTED_ZONE)},
        )).scalar_one()

        camera_ids = []
        for index, (name, code, vendor, model) in enumerate(CAMERAS):
            camera_ids.append(
                (await session.execute(
                    text(
                        "INSERT INTO cameras (tenant_id,site_id,zone_id,name,code,vendor,model,status) "
                        "VALUES (:t,:s,:z,:n,:c,:v,:m,'ready') RETURNING id"
                    ),
                    {"t": tenant_id, "s": site_ids[index % len(site_ids)],
                     "z": zone_id if index == 0 else None, "n": name, "c": code,
                     "v": vendor, "m": model},
                )).scalar_one()
            )

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.tenant_id',:t,true)"), {"t": tenant_id})
        now = dt.datetime.now(dt.UTC)

        for index in range(6):
            camera_id = camera_ids[index % len(camera_ids)]
            site_id = site_ids[(index % len(camera_ids)) % len(site_ids)]
            captured_at = now - dt.timedelta(minutes=7 * index)

            detection = await record_detection(session, DetectionDocument(
                tenant_id=uuid.UUID(tenant_id),
                site_id=site_id,
                camera_id=camera_id,
                event_type="person.restricted_zone",
                source_event_id=f"seed-{tenant_id[:8]}-{index}",
                capture_time=captured_at,
                confidence=outcome.best.confidence,
                objects=[
                    {"class": o.class_name, "confidence": round(o.confidence, 4),
                     "bbox": [round(v, 5) for v in o.bbox]}
                    for o in outcome.matched
                ],
                roi_id=str(zone_id),
            ))

            evidence = await capture_evidence(
                session, minio,
                tenant_id=uuid.UUID(tenant_id),
                camera_id=camera_id,
                image=image,
                capture_time=captured_at,
                detection_id=detection.detection_id,
                mask_boxes=[o.bbox for o in objects if o.class_name == "person"],
                annotations=[
                    Annotation(o.bbox, o.class_name, o.confidence, o.bbox in matched_boxes)
                    for o in objects
                ],
            )
            await attach_evidence_ref(
                session, tenant_id=uuid.UUID(tenant_id),
                detection_id=detection.detection_id,
                evidence_id=str(evidence.annotated.evidence_id),
            )

            # Only some detections escalate, so the two lists differ - which is the point
            # of having both a detection feed and an incident inbox.
            if index < 4:
                await upsert_incident_from_match(
                    session,
                    tenant_id=uuid.UUID(tenant_id),
                    site_id=site_id,
                    camera_id=camera_id,
                    rule=rule,
                    detected=outcome.best,
                    detection_id=detection.detection_id,
                    captured_at=captured_at,
                    zone_id=str(zone_id),
                )

        detections = (await session.execute(
            text("SELECT count(*) FROM detections WHERE tenant_id=:t"), {"t": tenant_id}
        )).scalar_one()
        incidents = (await session.execute(
            text("SELECT count(*) FROM incidents WHERE tenant_id=:t"), {"t": tenant_id}
        )).scalar_one()

    await engine.dispose()

    print(f"Seeded   : {detections} detections, {incidents} incidents, "
          f"{len(CAMERAS)} cameras across {len(SITES)} sites")
    print("\nSign in at http://app.localhost:8080/")
    print(f"  email    : {email}")
    print(f"  password : {PASSWORD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""What each demo mode runs. One table, so turning on a new model (weapon, door, helmet/vest
PPE) once it is trained and registered is a one-line change."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Mode:
    code: str
    label: str
    description: str
    model_name: str | None  # None = not available yet
    min_confidence: float = 0.35
    only_classes: frozenset[str] | None = None
    unavailable_reason: str | None = None
    # For a model whose registry row has no label map (the runtime then returns numeric
    # class ids): id -> display label.
    labels: dict[str, str] | None = None

    @property
    def available(self) -> bool:
        return self.model_name is not None


MODES: dict[str, Mode] = {
    m.code: m
    for m in (
        Mode("object", "Objects", "Everyday objects: people, vehicles, bags, screens and more.",
             "yolov8n-general", 0.35),
        Mode("phone", "Phone", "Spots a mobile phone.",
             "yolov8n-general", 0.25, frozenset({"cell phone"})),
        Mode("fire", "Fire & smoke", "Flame and smoke.",
             "fire-smoke-optimized150", 0.30),
        Mode("face", "Faces", "Finds faces. Detection only - nobody is identified or stored.",
             "insightface-buffalo-l-detect", 0.50),
        Mode("ppe", "Hygiene PPE", "Masks and gloves in food-handling settings.",
             "kitchen-safety-y8", 0.35,
             labels={"0": "Gloves on", "1": "Hairnet on", "2": "No mask", "3": "Mask on",
                     "4": "No gloves", "5": "No hairnet"}),
        Mode("weapon", "Weapons", "Handguns, knives and rifles.", None,
             unavailable_reason="Coming soon"),
        Mode("door", "Door open / closed", "Whether a door is open or closed.", None,
             unavailable_reason="Coming soon"),
    )
}

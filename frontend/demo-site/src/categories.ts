export interface Category {
  id: "general" | "person" | "plates";
  title: string;
  modelName: string;
  tagline: string;
  description: string;
}

// Order here is display order. Every image behind these categories is a real detection
// on real camera footage from a real customer site, rendered offline by
// scripts/build_demo_assets.py — nothing here calls a model live. See that script's own
// docstring for how the frames were produced and why plates are always blurred.
export const CATEGORIES: Category[] = [
  {
    id: "general",
    title: "Vehicle & Object Detection",
    modelName: "yolov8n-general",
    tagline: "Cars, trucks, and buses — tracked as they move through the lot.",
    description:
      "CSense classifies and boxes every vehicle in frame in real time, day or night, " +
      "so a site knows what's actually on it without a person watching a monitor.",
  },
  {
    id: "person",
    title: "Person Detection",
    modelName: "yolov8n-person",
    tagline: "People on site, flagged the moment they enter frame.",
    description:
      "The same runtime, a different model: CSense finds people specifically, which is " +
      "what drives after-hours intrusion alerts and site-safety rules.",
  },
  {
    id: "plates",
    title: "License Plate Detection",
    modelName: "license-plate-detector",
    tagline: "Detected — and automatically redacted.",
    description:
      "Every plate CSense finds is blurred by default before a frame is ever stored or " +
      "shown. Privacy isn't a setting to remember to turn on — it's what happens first.",
  },
];

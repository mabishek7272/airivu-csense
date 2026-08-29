import { useCallback, useEffect, useId, useRef, useState } from "react";

/** Draws and edits a zone boundary over a camera frame.
 *
 *  The polygon decides where a rule applies, so what is drawn here changes what raises an
 *  incident. Two consequences shape the design.
 *
 *  **It must be usable without a mouse.** A drag-only editor excludes anyone who cannot
 *  use a pointer, and this is a security tool that people are required to operate. Every
 *  point is a focusable element: arrow keys nudge it, Shift+arrow moves it coarsely,
 *  Delete removes it, and points can be added from the keyboard. That is more work than a
 *  drag handler and it is not optional.
 *
 *  **Coordinates are normalised, not pixels.** Stored 0..1 against the frame, so the zone
 *  means the same thing whether the camera streams 704x576 or 2592x1520 — and it survives
 *  someone switching a camera to its substream, which would otherwise silently move every
 *  boundary. The SVG uses a 0..1 viewBox so the conversion happens once, at the edges.
 */

export type Point = [number, number];

const NUDGE = 0.005;
const NUDGE_COARSE = 0.05;

export function PolygonEditor({
  points,
  onChange,
  backdropUrl,
  disabled,
  minPoints = 3,
  maxPoints = 60,
}: {
  points: Point[];
  onChange: (points: Point[]) => void;
  backdropUrl?: string | null;
  disabled?: boolean;
  minPoints?: number;
  maxPoints?: number;
}) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [dragging, setDragging] = useState<number | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const describedBy = useId();

  const clamp = (v: number) => Math.min(1, Math.max(0, v));

  /** Pointer position as a fraction of the frame. */
  const toFraction = useCallback((clientX: number, clientY: number): Point => {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return [0, 0];
    return [
      clamp((clientX - rect.left) / rect.width),
      clamp((clientY - rect.top) / rect.height),
    ];
  }, []);

  const movePoint = useCallback(
    (index: number, next: Point) => {
      const updated = points.map((p, i) => (i === index ? next : p));
      onChange(updated);
    },
    [points, onChange],
  );

  useEffect(() => {
    if (dragging === null) return;

    const onMove = (event: PointerEvent) => {
      event.preventDefault();
      movePoint(dragging, toFraction(event.clientX, event.clientY));
    };
    const onUp = () => setDragging(null);

    // Listeners on the window, not the SVG: a fast drag outruns the element and the point
    // would stick to the boundary the moment the pointer left it.
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, [dragging, movePoint, toFraction]);

  function addPointAt(event: React.MouseEvent) {
    if (disabled || points.length >= maxPoints) return;
    const next = toFraction(event.clientX, event.clientY);
    // Inserted after the nearest existing vertex rather than appended, so clicking
    // between two points adds it *there*. Appending would jump the boundary across the
    // frame and back, which looks like a bug.
    const index = nearestEdge(points, next);
    const updated = [...points.slice(0, index + 1), next, ...points.slice(index + 1)];
    onChange(updated);
    setSelected(index + 1);
    setAnnouncement(`Point ${index + 2} added. ${updated.length} points.`);
  }

  function removePoint(index: number) {
    if (disabled || points.length <= minPoints) {
      setAnnouncement(
        `A zone needs at least ${minPoints} points. Remove the zone instead of emptying it.`,
      );
      return;
    }
    const updated = points.filter((_, i) => i !== index);
    onChange(updated);
    setSelected(null);
    setAnnouncement(`Point ${index + 1} removed. ${updated.length} points remain.`);
  }

  function onPointKeyDown(event: React.KeyboardEvent, index: number) {
    if (disabled) return;
    const step = event.shiftKey ? NUDGE_COARSE : NUDGE;
    const [x, y] = points[index];

    const moves: Record<string, Point> = {
      ArrowLeft: [clamp(x - step), y],
      ArrowRight: [clamp(x + step), y],
      ArrowUp: [x, clamp(y - step)],
      ArrowDown: [x, clamp(y + step)],
    };

    if (moves[event.key]) {
      event.preventDefault();
      const next = moves[event.key];
      movePoint(index, next);
      // Announced as percentages: "0.42" means nothing spoken aloud, "42 percent across"
      // does.
      setAnnouncement(
        `Point ${index + 1} at ${Math.round(next[0] * 100)} percent across, ` +
          `${Math.round(next[1] * 100)} percent down.`,
      );
      return;
    }

    if (event.key === "Delete" || event.key === "Backspace") {
      event.preventDefault();
      removePoint(index);
      return;
    }

    if (event.key === "Enter" || event.key === " ") {
      // Adds a point halfway to the next one, which is how a boundary gets refined
      // without a pointer.
      event.preventDefault();
      if (points.length >= maxPoints) return;
      const [nx, ny] = points[(index + 1) % points.length];
      const midpoint: Point = [(x + nx) / 2, (y + ny) / 2];
      const updated = [...points.slice(0, index + 1), midpoint, ...points.slice(index + 1)];
      onChange(updated);
      setSelected(index + 1);
      setAnnouncement(`Point added after ${index + 1}. ${updated.length} points.`);
    }
  }

  const path = points.map(([x, y]) => `${x},${y}`).join(" ");

  return (
    <div className="polygon-editor">
      <div className="polygon-canvas">
        {backdropUrl ? (
          <img src={backdropUrl} alt="" className="polygon-backdrop" />
        ) : (
          // No snapshot yet. A neutral grid is honest about that; a black rectangle would
          // read as a camera fault.
          <div className="polygon-backdrop polygon-backdrop-empty" aria-hidden="true">
            <span>No snapshot yet — draw against the grid</span>
          </div>
        )}

        <svg
          ref={svgRef}
          className="polygon-svg"
          viewBox="0 0 1 1"
          preserveAspectRatio="none"
          onClick={addPointAt}
          role="application"
          aria-label="Zone boundary editor"
          aria-describedby={describedBy}
        >
          {points.length >= 2 && (
            <polygon
              points={path}
              className="polygon-shape"
              // The shape must not swallow clicks meant for the canvas underneath, or
              // adding a point inside the zone becomes impossible.
              pointerEvents="none"
            />
          )}
          {points.map(([x, y], index) => (
            <g key={index}>
              {/* An ellipse, not a circle. The viewBox is 0..1 on both axes stretched
                  over a 16:9 box, so equal radii render visibly wider than they are tall.
                  Scaling the horizontal radius by the inverse aspect makes the handle
                  look round, which matters because a handle is a hit target and an
                  elongated one is harder to aim at. */}
              <ellipse
                cx={x}
                cy={y}
                rx={0.012 * (9 / 16)}
                ry={0.012}
                className={`polygon-handle${selected === index ? " is-selected" : ""}`}
                tabIndex={disabled ? -1 : 0}
                role="button"
                aria-label={
                  `Point ${index + 1} of ${points.length}, ` +
                  `${Math.round(x * 100)} percent across, ${Math.round(y * 100)} percent down. ` +
                  "Arrow keys to move, Enter to add a point after it, Delete to remove."
                }
                onPointerDown={(event) => {
                  if (disabled) return;
                  event.stopPropagation();
                  setDragging(index);
                  setSelected(index);
                }}
                onKeyDown={(event) => onPointKeyDown(event, index)}
                onFocus={() => setSelected(index)}
                // Clicks on a handle must not also add a point underneath it.
                onClick={(event) => event.stopPropagation()}
              />
            </g>
          ))}
        </svg>
      </div>

      <p className="field-hint" id={describedBy}>
        Click the image to add a point. Drag a point to move it. With the keyboard: Tab to
        a point, arrow keys to move it, Shift+arrow to move further, Enter to add a point
        after it, Delete to remove it.
      </p>

      <div className="polygon-status">
        <span>
          {points.length} point{points.length === 1 ? "" : "s"}
        </span>
        <span className="muted">covers {(areaOf(points) * 100).toFixed(1)}% of the frame</span>
        {points.length > 0 && !disabled && (
          <button
            type="button"
            className="btn-quiet"
            onClick={() => {
              onChange([]);
              setAnnouncement("All points cleared.");
            }}
          >
            Start over
          </button>
        )}
      </div>

      {/* Every change is announced here. Without it the editor is silent to a screen
          reader, and a silent editor is an unusable one. */}
      <p className="visually-hidden" role="status" aria-live="polite">
        {announcement}
      </p>
    </div>
  );
}

/** Index of the vertex after which a new point should be inserted.
 *
 *  Chooses the edge the click is closest to, so a point lands where it was aimed rather
 *  than at the end of the list. */
function nearestEdge(points: Point[], candidate: Point): number {
  if (points.length < 2) return points.length - 1;

  let best = points.length - 1;
  let bestDistance = Infinity;
  for (let i = 0; i < points.length; i++) {
    const a = points[i];
    const b = points[(i + 1) % points.length];
    const distance = distanceToSegment(candidate, a, b);
    if (distance < bestDistance) {
      bestDistance = distance;
      best = i;
    }
  }
  return best;
}

function distanceToSegment(p: Point, a: Point, b: Point): number {
  const [px, py] = p;
  const [ax, ay] = a;
  const [bx, by] = b;
  const dx = bx - ax;
  const dy = by - ay;
  const lengthSquared = dx * dx + dy * dy;
  if (lengthSquared === 0) return Math.hypot(px - ax, py - ay);
  // Clamped so the nearest point is on the segment, not its infinite extension.
  const t = Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / lengthSquared));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

/** Shoelace area, matching the server's own check so the UI and the API agree on whether
 *  a shape is too thin to be useful. */
export function areaOf(points: Point[]): number {
  if (points.length < 3) return 0;
  let total = 0;
  for (let i = 0; i < points.length; i++) {
    const [x1, y1] = points[i];
    const [x2, y2] = points[(i + 1) % points.length];
    total += x1 * y2 - x2 * y1;
  }
  return Math.abs(total) / 2;
}

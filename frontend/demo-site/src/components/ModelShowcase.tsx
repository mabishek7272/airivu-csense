import { useCallback, useEffect, useRef, useState } from "react";
import type { Category } from "../categories";
import { usePrefersReducedMotion } from "../hooks/usePrefersReducedMotion";

const ADVANCE_MS = 4000;

interface ModelShowcaseProps {
  category: Category;
  images: string[];
}

export function ModelShowcase({ category, images }: ModelShowcaseProps) {
  const [index, setIndex] = useState(0);
  const [paused, setPaused] = useState(false);
  const reducedMotion = usePrefersReducedMotion();
  const containerRef = useRef<HTMLDivElement>(null);

  // A category change (switching tabs) should always land on frame 1, not wherever the
  // previous category's carousel happened to stop.
  useEffect(() => setIndex(0), [category.id]);

  const goTo = useCallback(
    (next: number) => setIndex(((next % images.length) + images.length) % images.length),
    [images.length],
  );
  const goNext = useCallback(() => goTo(index + 1), [goTo, index]);
  const goPrev = useCallback(() => goTo(index - 1), [goTo, index]);

  useEffect(() => {
    if (reducedMotion || paused || images.length < 2) return;
    const timer = window.setInterval(() => setIndex((i) => (i + 1) % images.length), ADVANCE_MS);
    return () => window.clearInterval(timer);
  }, [reducedMotion, paused, images.length]);

  function onKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (event.key === "ArrowRight") {
      event.preventDefault();
      goNext();
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      goPrev();
    }
  }

  if (images.length === 0) {
    return (
      <div className="showcase-empty">
        <p>No example frames for this model yet.</p>
      </div>
    );
  }

  return (
    <div className="showcase">
      <div className="showcase-copy">
        <h2>{category.title}</h2>
        <p className="showcase-tagline">{category.tagline}</p>
        <p className="showcase-description">{category.description}</p>
        <p className="showcase-model">
          Model: <code>{category.modelName}</code>
        </p>
      </div>

      <div
        ref={containerRef}
        className="showcase-frame"
        tabIndex={0}
        role="group"
        aria-roledescription="carousel"
        aria-label={`${category.title} example frames`}
        onMouseEnter={() => setPaused(true)}
        onMouseLeave={() => setPaused(false)}
        onFocus={() => setPaused(true)}
        onBlur={() => setPaused(false)}
        onKeyDown={onKeyDown}
      >
        <img
          key={`${category.id}-${index}`}
          className="showcase-image"
          src={`/showcase/${category.id}/${images[index]}`}
          alt={`${category.title} — real detection example ${index + 1} of ${images.length}`}
        />

        {images.length > 1 && (
          <>
            <button
              type="button"
              className="showcase-nav showcase-nav-prev"
              onClick={goPrev}
              aria-label="Previous example"
            >
              ‹
            </button>
            <button
              type="button"
              className="showcase-nav showcase-nav-next"
              onClick={goNext}
              aria-label="Next example"
            >
              ›
            </button>

            <div className="showcase-dots" role="tablist" aria-label="Choose an example frame">
              {images.map((_, dotIndex) => (
                <button
                  key={dotIndex}
                  type="button"
                  role="tab"
                  aria-selected={dotIndex === index}
                  aria-label={`Example ${dotIndex + 1} of ${images.length}`}
                  className={dotIndex === index ? "showcase-dot showcase-dot-active" : "showcase-dot"}
                  onClick={() => goTo(dotIndex)}
                />
              ))}
            </div>
          </>
        )}

        <p className="visually-hidden" role="status" aria-live="polite">
          Showing example {index + 1} of {images.length}.
        </p>
      </div>
    </div>
  );
}

"""Detection boundary drawing.

The annotated variant is what an operator actually looks at, so these check the boxes land
in the right place and that the privacy default survives annotation - a box drawn over a
blurred face must not un-blur it.
"""
from __future__ import annotations

import numpy as np
import pytest

from csense_shared.pipeline.evidence import (
    ALERT_COLOUR,
    CONTEXT_COLOUR,
    Annotation,
    draw_detections,
    mask_regions,
)


def flat_image(width: int = 320, height: int = 240, value: int = 200) -> np.ndarray:
    return np.full((height, width, 3), value, dtype=np.uint8)


def noisy_image(width: int = 320, height: int = 240) -> np.ndarray:
    rng = np.random.default_rng(seed=7)
    return rng.integers(0, 255, size=(height, width, 3), dtype=np.uint8)


def test_no_annotations_is_a_copy():
    image = flat_image()
    result = draw_detections(image, [])
    assert np.array_equal(image, result)
    assert result is not image


def test_drawing_does_not_mutate_the_source():
    """The masked variant is stored separately; annotating must not alter it."""
    image = flat_image()
    before = image.copy()
    draw_detections(image, [Annotation((0.2, 0.2, 0.8, 0.8), "person", 0.9)])
    assert np.array_equal(image, before)


def test_box_is_drawn_at_the_right_place():
    image = flat_image()
    result = draw_detections(image, [Annotation((0.25, 0.25, 0.75, 0.75), "person", 0.9)])

    height, width = image.shape[:2]
    # The border pixel at the left edge of the box should have changed.
    assert not np.array_equal(result[int(0.5 * height), int(0.25 * width)], image[0, 0])
    # A pixel well outside the box, away from the label, should not have.
    assert np.array_equal(result[height - 1, width - 1], image[0, 0])


def test_triggered_and_rejected_use_different_colours():
    """An operator needs to see what the model saw *and* what the rule decided. Drawing
    both in one colour hides why the scene looked the way it did."""
    triggered = draw_detections(
        flat_image(), [Annotation((0.25, 0.25, 0.75, 0.75), "person", 0.9, triggered=True)]
    )
    rejected = draw_detections(
        flat_image(), [Annotation((0.25, 0.25, 0.75, 0.75), "person", 0.9, triggered=False)]
    )
    assert not np.array_equal(triggered, rejected)

    def has_colour(image, colour):
        return bool(np.any(np.all(image == np.array(colour, dtype=np.uint8), axis=-1)))

    assert has_colour(triggered, ALERT_COLOUR)
    assert has_colour(rejected, CONTEXT_COLOUR)


def test_label_for_a_box_at_the_top_stays_inside_the_frame():
    """A detection touching the top edge would otherwise have its label clipped away -
    exactly where an intrusion at the far end of a corridor appears."""
    result = draw_detections(flat_image(), [Annotation((0.1, 0.0, 0.5, 0.3), "person", 0.9)])
    # The label is drawn inside the box instead, so the top rows carry ink.
    assert not np.array_equal(result[0:20, 30:120], flat_image()[0:20, 30:120])


def test_degenerate_and_out_of_range_boxes_are_handled():
    image = flat_image()
    result = draw_detections(
        image,
        [
            Annotation((0.5, 0.5, 0.5, 0.5), "zero", 0.9),      # zero area
            Annotation((-0.5, -0.5, 1.5, 1.5), "huge", 0.9),    # beyond the frame
        ],
    )
    assert result.shape == image.shape


def test_annotation_preserves_the_blur_underneath():
    """The box shows where and what; it must never reveal who. Drawing on the masked image
    rather than the original is what guarantees that."""
    original = noisy_image()
    face_box = (0.3, 0.3, 0.7, 0.7)
    masked = mask_regions(original, [face_box])
    annotated = draw_detections(masked, [Annotation(face_box, "person", 0.9)])

    height, width = original.shape[:2]
    # Sample the interior of the region, inside the blur but away from the box border.
    interior = (
        slice(int(0.45 * height), int(0.55 * height)),
        slice(int(0.45 * width), int(0.55 * width)),
    )
    assert annotated[interior].var() < original[interior].var(), "blur must survive annotation"
    assert not np.array_equal(annotated[interior], original[interior])


@pytest.mark.parametrize("size", [(160, 120), (1920, 1080), (3840, 2160)])
def test_line_weight_scales_with_frame_size(size):
    """A fixed 2px box is invisible on 4K and covers a face on 320p, and one camera estate
    rarely shares a resolution."""
    width, height = size
    image = flat_image(width, height)
    result = draw_detections(image, [Annotation((0.25, 0.25, 0.75, 0.75), "person", 0.9)])

    changed = int(np.count_nonzero(np.any(result != image, axis=-1)))
    # Enough ink to be visible, but nowhere near filling the box.
    box_area = (0.5 * width) * (0.5 * height)
    assert changed > 0
    assert changed < box_area * 0.5

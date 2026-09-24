import { mapBox } from '../src/geometry';

const box = { x1: 0.25, y1: 0.25, x2: 0.75, y2: 0.75 };

describe('mapBox', () => {
  it('maps onto a view of the same aspect ratio with no offset', () => {
    const r = mapBox(box, { width: 400, height: 300 }, { width: 200, height: 150 }, 'cover');
    expect(r).toEqual({ x: 50, y: 37.5, width: 100, height: 75 });
  });

  it('cover crops the overflow: a wide image in a tall view is centred and scaled up', () => {
    // 400x200 image into a 100x100 view: scale = max(0.25, 0.5) = 0.5 -> 200x100, 50px cropped each side.
    const r = mapBox({ x1: 0, y1: 0, x2: 1, y2: 1 }, { width: 400, height: 200 }, { width: 100, height: 100 }, 'cover');
    expect(r).toEqual({ x: -50, y: 0, width: 200, height: 100 });
  });

  it('contain leaves bars: a wide image in a square view sits centred', () => {
    // scale = min(0.25, 0.5) = 0.25 -> 100x50, 25px bars top and bottom.
    const r = mapBox({ x1: 0, y1: 0, x2: 1, y2: 1 }, { width: 400, height: 200 }, { width: 100, height: 100 }, 'contain');
    expect(r).toEqual({ x: 0, y: 25, width: 100, height: 50 });
  });

  it('mirror flips the box horizontally only', () => {
    const left = { x1: 0, y1: 0.1, x2: 0.2, y2: 0.3 };
    const r = mapBox(left, { width: 100, height: 100 }, { width: 100, height: 100 }, 'cover', true);
    expect(r.x).toBeCloseTo(80);
    expect(r.width).toBeCloseTo(20);
    expect(r.y).toBeCloseTo(10);
    expect(r.height).toBeCloseTo(20);
  });

  it('is safe for an image with no size', () => {
    expect(mapBox(box, { width: 0, height: 0 }, { width: 100, height: 100 }, 'cover')).toEqual({
      x: 0, y: 0, width: 0, height: 0,
    });
  });
});

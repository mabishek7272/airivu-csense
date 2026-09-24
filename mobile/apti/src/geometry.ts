export interface NormBox {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export interface Size {
  width: number;
  height: number;
}

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * Maps a detection box (0..1 of the analysed image) to pixels inside the view showing that
 * image.
 *
 * `cover` is how a camera preview fills its view (the image is scaled until it fills both
 * dimensions and the overflow is cropped); `contain` is how a picked photo is shown (the
 * whole image visible, with bars). Getting this wrong is the usual reason boxes drift off
 * what they outline.
 *
 * `mirror` flips horizontally: the front-camera preview is mirrored, but the frame sent for
 * analysis is not, so a box in the frame's coordinates must be flipped to sit on the preview.
 */
export function mapBox(
  box: NormBox,
  image: Size,
  view: Size,
  fit: 'cover' | 'contain',
  mirror = false,
): Rect {
  if (image.width <= 0 || image.height <= 0) return { x: 0, y: 0, width: 0, height: 0 };
  const scale =
    fit === 'cover'
      ? Math.max(view.width / image.width, view.height / image.height)
      : Math.min(view.width / image.width, view.height / image.height);
  const offsetX = (view.width - image.width * scale) / 2;
  const offsetY = (view.height - image.height * scale) / 2;

  const x1 = mirror ? 1 - box.x2 : box.x1;
  const x2 = mirror ? 1 - box.x1 : box.x2;

  return {
    x: offsetX + x1 * image.width * scale,
    y: offsetY + box.y1 * image.height * scale,
    width: (x2 - x1) * image.width * scale,
    height: (box.y2 - box.y1) * image.height * scale,
  };
}

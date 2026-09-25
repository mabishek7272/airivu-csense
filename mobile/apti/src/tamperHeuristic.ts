/**
 * Flags a likely covered/blocked camera by watching the encoded JPEG's own byte size,
 * not raw pixels - no RN API in this app's dependency set exposes raw pixel stats without
 * a much heavier native module. A blocked lens (a finger, a black covering) is nearly
 * uniform colour, which JPEG compresses far smaller than a normal scene. A rolling
 * average avoids a false positive on one naturally simple frame (e.g. a blank wall) and
 * only fires once enough history exists to trust the average.
 */

const WINDOW = 8;
const MIN_SAMPLES = 4;
const DROP_RATIO = 0.35;

export class TamperDetector {
  private samples: number[] = [];

  /** Call once per captured frame with its encoded JPEG size in bytes. Returns true if
   *  this frame looks like a covered/blocked lens relative to recent frames. */
  check(byteSize: number): boolean {
    const tampered = this.samples.length >= MIN_SAMPLES && byteSize < this.average() * DROP_RATIO;
    this.samples.push(byteSize);
    if (this.samples.length > WINDOW) this.samples.shift();
    return tampered;
  }

  reset(): void {
    this.samples = [];
  }

  private average(): number {
    return this.samples.reduce((a, b) => a + b, 0) / this.samples.length;
  }
}

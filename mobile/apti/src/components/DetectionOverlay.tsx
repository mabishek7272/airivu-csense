import React from 'react';
import { StyleSheet, Text, View } from 'react-native';
import type { Detection } from '../api';
import { color, fontHeadingSemibold } from '../theme';
import { mapBox, type Size } from '../geometry';

const CORNER = 14;

/**
 * Bracket-style detection box, matching the approved Nocturne mockup: a faint full
 * outline plus four solid corner brackets, with the label pinned to the top-left corner
 * (or dropped below it when the box starts near the top edge, so the label never runs
 * off-screen).
 */
export function DetectionOverlay({
  detections,
  image,
  view,
  fit,
  mirror = false,
}: {
  detections: Detection[];
  image: Size;
  view: Size;
  fit: 'cover' | 'contain';
  mirror?: boolean;
}) {
  if (view.width === 0 || view.height === 0) return null;

  return (
    <View style={StyleSheet.absoluteFill} pointerEvents="none">
      {detections.map((d, index) => {
        const r = mapBox(d.box, image, view, fit, mirror);
        const labelBelow = r.y < 24;
        const text = `${d.label} ${Math.round(d.confidence * 100)}%`;
        return (
          <View key={index} style={{ position: 'absolute', left: r.x, top: r.y, width: r.width, height: r.height }}>
            <View style={styles.outline} />
            <View style={[styles.corner, styles.cornerTL]} />
            <View style={[styles.corner, styles.cornerTR]} />
            <View style={[styles.corner, styles.cornerBL]} />
            <View style={[styles.corner, styles.cornerBR]} />
            <View style={[styles.label, labelBelow ? { top: 2 } : { top: -22 }]}>
              <Text style={styles.labelText} numberOfLines={1}>
                {text}
              </Text>
            </View>
          </View>
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  outline: {
    position: 'absolute',
    inset: 0,
    borderWidth: 1,
    borderColor: 'rgba(181,171,252,0.35)', // accent-400 at 35%
    borderRadius: 3,
  },
  corner: { position: 'absolute', width: CORNER, height: CORNER, borderColor: color.accent300 },
  cornerTL: { left: -1, top: -1, borderLeftWidth: 2, borderTopWidth: 2 },
  cornerTR: { right: -1, top: -1, borderRightWidth: 2, borderTopWidth: 2 },
  cornerBL: { left: -1, bottom: -1, borderLeftWidth: 2, borderBottomWidth: 2 },
  cornerBR: { right: -1, bottom: -1, borderRightWidth: 2, borderBottomWidth: 2 },
  label: {
    position: 'absolute',
    left: -1,
    backgroundColor: color.accent800,
    borderRadius: 4,
    paddingVertical: 2,
    paddingHorizontal: 6,
  },
  labelText: { color: color.accent100, fontSize: 11, fontFamily: fontHeadingSemibold },
});

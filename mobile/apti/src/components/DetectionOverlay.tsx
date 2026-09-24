import React from 'react';
import { StyleSheet } from 'react-native';
import Svg, { Rect, Text as SvgText } from 'react-native-svg';
import type { Detection } from '../api';
import { mapBox, type Size } from '../geometry';

const COLOURS = ['#22d3ee', '#a3e635', '#f472b6', '#fbbf24', '#818cf8', '#fb7185'];

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
  const colourFor = new Map<string, string>();

  return (
    <Svg style={StyleSheet.absoluteFill} width={view.width} height={view.height} pointerEvents="none">
      {detections.map((d, index) => {
        if (!colourFor.has(d.label)) colourFor.set(d.label, COLOURS[colourFor.size % COLOURS.length]);
        const colour = colourFor.get(d.label)!;
        const r = mapBox(d.box, image, view, fit, mirror);
        const text = `${d.label} ${Math.round(d.confidence * 100)}%`;
        const labelWidth = text.length * 7.2 + 10;
        const labelY = r.y >= 20 ? r.y - 20 : r.y;
        return (
          <React.Fragment key={index}>
            <Rect x={r.x} y={r.y} width={r.width} height={r.height} stroke={colour} strokeWidth={2.5} fill="none" />
            <Rect x={r.x} y={labelY} width={labelWidth} height={20} fill={colour} />
            <SvgText x={r.x + 5} y={labelY + 14} fontSize={12} fontWeight="700" fill="#0b1220">
              {text}
            </SvgText>
          </React.Fragment>
        );
      })}
    </Svg>
  );
}

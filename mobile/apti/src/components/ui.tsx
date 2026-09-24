import React from 'react';
import { Pressable, StyleSheet, Text, View, type StyleProp, type ViewStyle } from 'react-native';
import { color, fontHeading, radius, shadow, space, textMuted50 } from '../theme';

/** `.card` / `.elev-sm` */
export function Card({
  style,
  elevated = false,
  children,
}: {
  style?: StyleProp<ViewStyle>;
  elevated?: boolean;
  children: React.ReactNode;
}) {
  return <View style={[styles.card, elevated && styles.cardElevated, style]}>{children}</View>;
}

/** `.card-kicker` */
export function Kicker({ children, style }: { children: React.ReactNode; style?: StyleProp<ViewStyle> }) {
  return (
    <Text style={[{ fontFamily: fontHeading, fontSize: 10, letterSpacing: 1, color: color.accent, textTransform: 'uppercase' }, style]}>
      {children}
    </Text>
  );
}

/** `.card-meta` */
export function Meta({ children, style }: { children: React.ReactNode; style?: StyleProp<ViewStyle> }) {
  return (
    <View style={[{ flexDirection: 'row', alignItems: 'center', gap: 6 }, style]}>
      {children}
    </View>
  );
}

type TagVariant = 'accent' | 'outline' | 'neutral';

/** `.tag` */
export function Tag({
  variant = 'accent',
  children,
  style,
}: {
  variant?: TagVariant;
  children: React.ReactNode;
  style?: StyleProp<ViewStyle>;
}) {
  const variantStyle =
    variant === 'outline'
      ? { borderWidth: 1, borderColor: color.accent, backgroundColor: 'transparent' as const }
      : variant === 'neutral'
        ? { backgroundColor: color.neutral800 }
        : { backgroundColor: color.accent800 };
  const textColor = variant === 'outline' ? color.accent : variant === 'neutral' ? color.neutral100 : color.accent100;
  return (
    <View style={[styles.tag, variantStyle, style]}>
      {typeof children === 'string' ? (
        <Text style={[styles.tagText, { color: textColor }]}>{children}</Text>
      ) : (
        children
      )}
    </View>
  );
}

type BtnVariant = 'primary' | 'secondary';

/** `.btn` / `.btn-primary` / `.btn-secondary` / `.btn-icon` / `.btn-block` */
export function Btn({
  variant = 'secondary',
  icon,
  block = false,
  square,
  disabled = false,
  onPress,
  children,
  style,
  accessibilityLabel,
}: {
  variant?: BtnVariant;
  icon?: React.ReactNode;
  block?: boolean;
  /** `.btn-icon`: a fixed square, icon-only button. */
  square?: number;
  disabled?: boolean;
  onPress?: () => void;
  children?: React.ReactNode;
  style?: StyleProp<ViewStyle>;
  accessibilityLabel?: string;
}) {
  const variantStyle = variant === 'primary' ? styles.btnPrimary : styles.btnSecondary;
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel}
      style={({ pressed }) => [
        styles.btn,
        variantStyle,
        block && { width: '100%' },
        square != null && { width: square, height: square, padding: 0 },
        disabled && { opacity: 0.45 },
        pressed && !disabled && (variant === 'primary' ? styles.btnPrimaryPressed : styles.btnSecondaryPressed),
        style,
      ]}
    >
      {icon}
      {children != null && (
        <Text style={[styles.btnText, { color: variant === 'primary' ? color.accent : color.text }]}>{children}</Text>
      )}
    </Pressable>
  );
}

/** `.seg` / `.seg-opt` */
export function Seg({ children, style }: { children: React.ReactNode; style?: StyleProp<ViewStyle> }) {
  return <View style={[styles.seg, style]}>{children}</View>;
}

export function SegOption({
  active,
  icon,
  first = false,
  onPress,
  children,
}: {
  active: boolean;
  icon?: React.ReactNode;
  first?: boolean;
  onPress: () => void;
  children: React.ReactNode;
}) {
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="radio"
      accessibilityState={{ selected: active }}
      style={[styles.segOpt, !first && styles.segOptDivider, active && styles.segOptActive]}
    >
      {icon}
      <Text style={[styles.segOptText, active && { color: color.accent }]}>{children}</Text>
    </Pressable>
  );
}

export function Muted({ children, style }: { children: React.ReactNode; style?: StyleProp<ViewStyle> }) {
  return <Text style={[{ color: textMuted50 }, style]}>{children}</Text>;
}

const styles = StyleSheet.create({
  card: {
    flexDirection: 'column',
    gap: space[2],
    padding: space[3],
    borderRadius: radius.md,
    backgroundColor: color.surface,
  },
  cardElevated: { ...shadow.md, borderWidth: 1, borderColor: color.neutral800 },
  tag: {
    alignSelf: 'flex-start',
    borderRadius: radius.md * 0.75,
    paddingVertical: 3,
    paddingHorizontal: 10,
  },
  tagText: { fontSize: 11, letterSpacing: 0.2 },
  btn: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 6,
    borderRadius: radius.md,
    borderWidth: 1,
    paddingVertical: space[2],
    paddingHorizontal: space[3] * 1.2,
  },
  btnText: { fontFamily: fontHeading, fontSize: 14 },
  btnPrimary: { borderColor: color.accent, backgroundColor: 'transparent' },
  btnPrimaryPressed: { backgroundColor: 'rgba(145,132,217,0.22)' },
  btnSecondary: { borderColor: color.divider, backgroundColor: 'transparent' },
  btnSecondaryPressed: { backgroundColor: 'rgba(233,233,237,0.14)' },
  seg: {
    flexDirection: 'row',
    borderWidth: 1,
    borderColor: color.divider,
    borderRadius: radius.md,
    overflow: 'hidden',
  },
  segOpt: {
    flex: 1,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 6,
    paddingVertical: 10,
    paddingHorizontal: 12,
  },
  segOptDivider: { borderLeftWidth: 1, borderLeftColor: color.divider },
  segOptActive: { borderWidth: 1, borderColor: color.accent, margin: -1, borderRadius: radius.md },
  segOptText: { color: color.text, fontSize: 14 },
});

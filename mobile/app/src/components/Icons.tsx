/** React Native (react-native-svg) port of the product's own icon set
 * (design/csense-ui/System.dc.html §03, already ported to web at
 * frontend/customer-crm/src/components/Icons.tsx) - same paths, same 20px grid, same 1.4
 * stroke, `currentColor`-equivalent via an explicit `color` prop (react-native-svg has no
 * CSS cascade to inherit `currentColor` from). Only the tab-bar/account icons this app
 * actually needs, not the full 24 - the CRM's web-only pages (Pipelines, Webhooks, Audit,
 * ...) have no mobile screen to appear on.
 */
import Svg, { Circle, Path, Rect } from 'react-native-svg';

export interface IconProps {
  size?: number;
  color?: string;
}

export function DashboardIcon({ size = 22, color = '#F5EDF0' }: IconProps) {
  return (
    <Svg width={size} height={size} viewBox="0 0 20 20" fill="none">
      <Rect x="2.5" y="2.5" width="6" height="6" rx="1.2" stroke={color} strokeWidth="1.4" />
      <Rect x="11.5" y="2.5" width="6" height="6" rx="1.2" stroke={color} strokeWidth="1.4" />
      <Rect x="2.5" y="11.5" width="6" height="6" rx="1.2" stroke={color} strokeWidth="1.4" />
      <Path d="M11.5 14.5h6" stroke={color} strokeWidth="1.4" strokeLinecap="round" />
    </Svg>
  );
}

export function IncidentIcon({ size = 22, color = '#F5EDF0' }: IconProps) {
  return (
    <Svg width={size} height={size} viewBox="0 0 20 20" fill="none">
      <Path
        d="M1.5 10h3l2-5.5L10 15l2.5-7 1.8 4h3.2"
        stroke={color}
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </Svg>
  );
}

export function CameraIcon({ size = 22, color = '#F5EDF0' }: IconProps) {
  return (
    <Svg width={size} height={size} viewBox="0 0 20 20" fill="none">
      <Rect x="1.8" y="5.4" width="12.4" height="9.2" rx="1.8" stroke={color} strokeWidth="1.4" />
      <Path d="M14.2 9.4 18.2 6.8v6.4l-4-2.6" stroke={color} strokeWidth="1.4" strokeLinejoin="round" />
      <Circle cx="8" cy="10" r="2.2" stroke={color} strokeWidth="1.3" />
    </Svg>
  );
}

/** Account tab - a person-in-a-frame mark, distinct from the web's generic "Settings"
 * gear (this tab is a person's own account, not tenant-wide settings). */
export function AccountIcon({ size = 22, color = '#F5EDF0' }: IconProps) {
  return (
    <Svg width={size} height={size} viewBox="0 0 20 20" fill="none">
      <Circle cx="10" cy="7" r="3.2" stroke={color} strokeWidth="1.4" />
      <Path d="M3.6 17c0-3.4 2.8-5.8 6.4-5.8s6.4 2.4 6.4 5.8" stroke={color} strokeWidth="1.4" strokeLinecap="round" />
    </Svg>
  );
}

export function AckIcon({ size = 22, color = '#F5EDF0' }: IconProps) {
  return (
    <Svg width={size} height={size} viewBox="0 0 20 20" fill="none">
      <Circle cx="10" cy="10" r="7.4" stroke={color} strokeWidth="1.4" />
      <Path d="m6.6 10.2 2.4 2.3 4.4-4.8" stroke={color} strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </Svg>
  );
}

export function ZoneIcon({ size = 22, color = '#F5EDF0' }: IconProps) {
  return (
    <Svg width={size} height={size} viewBox="0 0 20 20" fill="none">
      <Path
        d="M10 2.6 17.2 7v9.4L10 17.6 2.8 16.4V7L10 2.6Z"
        stroke={color}
        strokeWidth="1.4"
        strokeLinejoin="round"
        strokeDasharray="2.6 2"
      />
      <Circle cx="10" cy="2.6" r="1.5" fill={color} />
      <Circle cx="17.2" cy="7" r="1.5" fill={color} />
      <Circle cx="2.8" cy="7" r="1.5" fill={color} />
    </Svg>
  );
}

export function SearchIcon({ size = 22, color = '#F5EDF0' }: IconProps) {
  return (
    <Svg width={size} height={size} viewBox="0 0 20 20" fill="none">
      <Circle cx="8.8" cy="8.8" r="5.8" stroke={color} strokeWidth="1.5" />
      <Path d="m13.2 13.2 4 4" stroke={color} strokeWidth="1.5" strokeLinecap="round" />
    </Svg>
  );
}

export function LogoMark({ size = 26 }: { size?: number }) {
  return (
    <Svg width={size} height={size} viewBox="0 0 24 24" fill="none">
      <Path d="M12 2.5 20.5 7v10L12 21.5 3.5 17V7L12 2.5Z" stroke="#98134E" strokeWidth="1.4" fill="rgba(152,19,78,0.18)" />
      <Circle cx="12" cy="12" r="3.4" stroke="#FF8ABB" strokeWidth="1.4" />
      <Circle cx="12" cy="12" r="1.1" fill="#FF8ABB" />
      <Path d="M12 5.6v2.4M12 16v2.4M6.6 9v6M17.4 9v6" stroke="#98134E" strokeWidth="1.2" strokeLinecap="round" />
    </Svg>
  );
}

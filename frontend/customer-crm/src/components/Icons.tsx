/** The product's own 24-icon set (design/csense-ui/System.dc.html §03) - drawn for this
 *  product, not borrowed from a generic icon font. 20px grid, 1.4 stroke, `currentColor`
 *  throughout so each icon inherits whatever text colour its context sets (nav item,
 *  status row, empty state...). Every icon takes the same `IconProps` shape so it can
 *  drop into any of those contexts without a per-icon prop mismatch.
 *
 *  `aria-hidden` on every icon: each one is always paired with a visible text label
 *  (the nav item's own label, a badge's own severity text) - the icon is decoration on
 *  top of that label, never the only carrier of meaning. A screen reader announces the
 *  label; announcing the icon too would be noise.
 */
import type { SVGProps } from "react";

export interface IconProps extends SVGProps<SVGSVGElement> {
  size?: number;
}

function Icon({ size = 16, children, ...rest }: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 20 20"
      fill="none"
      aria-hidden="true"
      {...rest}
    >
      {children}
    </svg>
  );
}

export function DashboardIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="2.5" y="2.5" width="6" height="6" rx="1.2" stroke="currentColor" strokeWidth="1.4" />
      <rect x="11.5" y="2.5" width="6" height="6" rx="1.2" stroke="currentColor" strokeWidth="1.4" />
      <rect x="2.5" y="11.5" width="6" height="6" rx="1.2" stroke="currentColor" strokeWidth="1.4" />
      <path d="M11.5 14.5h6" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </Icon>
  );
}

export function IncidentIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path
        d="M1.5 10h3l2-5.5L10 15l2.5-7 1.8 4h3.2"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </Icon>
  );
}

export function DetectionIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="10" cy="10" r="7" stroke="currentColor" strokeWidth="1.4" />
      <circle cx="10" cy="10" r="2.6" stroke="currentColor" strokeWidth="1.4" />
      <path
        d="M10 1.4v3.2M10 15.4v3.2M1.4 10h3.2M15.4 10h3.2"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </Icon>
  );
}

export function SiteIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M3 17.5V5.2l6-2.7v15M9 17.5h8V8.4l-8-3.2" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
      <path d="M12 11h2M12 14h2M5.6 8h1.6M5.6 11.4h1.6" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
    </Icon>
  );
}

export function ZoneIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path
        d="M10 2.6 17.2 7v9.4L10 17.6 2.8 16.4V7L10 2.6Z"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
        strokeDasharray="2.6 2"
      />
      <circle cx="10" cy="2.6" r="1.5" fill="currentColor" />
      <circle cx="17.2" cy="7" r="1.5" fill="currentColor" />
      <circle cx="2.8" cy="7" r="1.5" fill="currentColor" />
    </Icon>
  );
}

export function CameraIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="1.8" y="5.4" width="12.4" height="9.2" rx="1.8" stroke="currentColor" strokeWidth="1.4" />
      <path d="M14.2 9.4 18.2 6.8v6.4l-4-2.6" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
      <circle cx="8" cy="10" r="2.2" stroke="currentColor" strokeWidth="1.3" />
    </Icon>
  );
}

export function EdgeIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="5.2" y="5.2" width="9.6" height="9.6" rx="1.6" stroke="currentColor" strokeWidth="1.4" />
      <rect x="8.4" y="8.4" width="3.2" height="3.2" rx="0.8" fill="currentColor" />
      <path
        d="M8 2.4v2.8M12 2.4v2.8M8 14.8v2.8M12 14.8v2.8M2.4 8h2.8M2.4 12h2.8M14.8 8h2.8M14.8 12h2.8"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
      />
    </Icon>
  );
}

export function PipelineIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="4" cy="10" r="2.2" stroke="currentColor" strokeWidth="1.4" />
      <circle cx="16" cy="5.4" r="2.2" stroke="currentColor" strokeWidth="1.4" />
      <circle cx="16" cy="14.6" r="2.2" stroke="currentColor" strokeWidth="1.4" />
      <path d="M6.1 9.1 13.9 6.2M6.1 10.9l7.8 2.9" stroke="currentColor" strokeWidth="1.4" />
    </Icon>
  );
}

export function RuleIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M3 4.6h5.4l2.6 5.4 2.6-5.4H17" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M11 10v5.4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <rect x="8.2" y="15" width="5.6" height="2.6" rx="1.3" stroke="currentColor" strokeWidth="1.4" />
    </Icon>
  );
}

export function RecipientIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="7.6" cy="7" r="2.8" stroke="currentColor" strokeWidth="1.4" />
      <path d="M2.6 16.4c0-2.8 2.2-4.6 5-4.6s5 1.8 5 4.6" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <path d="M13.4 5.6a2.6 2.6 0 0 1 0 4.6M15.4 12.4c1.4.7 2.2 2 2.2 3.6" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
    </Icon>
  );
}

export function NotifyIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="10" cy="10" r="1.8" fill="currentColor" />
      <path
        d="M6.6 6.6a4.8 4.8 0 0 0 0 6.8M13.4 13.4a4.8 4.8 0 0 0 0-6.8M4.2 4.2a8.2 8.2 0 0 0 0 11.6M15.8 15.8a8.2 8.2 0 0 0 0-11.6"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </Icon>
  );
}

export function ModelIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M10 2.4 17 6v8l-7 3.6L3 14V6l7-3.6Z" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
      <path d="M3 6l7 3.6L17 6M10 9.6v8" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
    </Icon>
  );
}

export function AuditIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="3.4" y="2.6" width="13.2" height="14.8" rx="1.8" stroke="currentColor" strokeWidth="1.4" />
      <path d="M6.6 7h6.8M6.6 10h6.8M6.6 13h4" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
    </Icon>
  );
}

export function GrantIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M10 2.6 4 5v4.4c0 3.6 2.4 6.8 6 8 3.6-1.2 6-4.4 6-8V5l-6-2.4Z" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
      <path d="m7.4 10 1.9 1.9 3.5-3.8" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
    </Icon>
  );
}

export function SettingsIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4 6h12M4 10h12M4 14h12" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <circle cx="8" cy="6" r="1.8" fill="#1A1418" stroke="currentColor" strokeWidth="1.4" />
      <circle cx="13" cy="10" r="1.8" fill="#1A1418" stroke="currentColor" strokeWidth="1.4" />
      <circle cx="7" cy="14" r="1.8" fill="#1A1418" stroke="currentColor" strokeWidth="1.4" />
    </Icon>
  );
}

export function SearchIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="8.8" cy="8.8" r="5.8" stroke="currentColor" strokeWidth="1.5" />
      <path d="m13.2 13.2 4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </Icon>
  );
}

export function FilterIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M3 4.6h14L11.8 11v5.2l-3.6-1.8V11L3 4.6Z" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
    </Icon>
  );
}

export function AlertBellIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M10 2.6c-2.9 0-5 2.2-5 5v3.1l-1.6 2.6h13.2L15 10.7V7.6c0-2.8-2.1-5-5-5Z" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
      <path d="M8.2 16.2a1.9 1.9 0 0 0 3.6 0" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </Icon>
  );
}

export function AckIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="10" cy="10" r="7.4" stroke="currentColor" strokeWidth="1.4" />
      <path d="m6.6 10.2 2.4 2.3 4.4-4.8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </Icon>
  );
}

export function UploadIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M10 13.4V3.6M6.4 7.2 10 3.6l3.6 3.6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M3.6 13.4v2a1.6 1.6 0 0 0 1.6 1.6h9.6a1.6 1.6 0 0 0 1.6-1.6v-2" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </Icon>
  );
}

export function PlateIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="2.6" y="7.4" width="14.8" height="7.2" rx="1.6" stroke="currentColor" strokeWidth="1.4" />
      <path d="M5.4 10.4h1.8M8.4 10.4h1.8M11.4 10.4h3.2M5.4 12.4h3.4M10 12.4h4.6" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
    </Icon>
  );
}

export function FireIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path
        d="M10 2.6c0 2.8-3.4 3.6-3.4 6.8A3.4 3.4 0 0 0 10 12.8a3.4 3.4 0 0 0 3.4-3.4c0-3.2-3.4-4-3.4-6.8Z"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
      />
      <path d="M5.6 14.8c1.3 1.6 2.7 2.4 4.4 2.4s3.1-.8 4.4-2.4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </Icon>
  );
}

export function PpeIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="10" cy="6.2" r="2.8" stroke="currentColor" strokeWidth="1.4" />
      <path d="M4.8 17c0-3 2.3-5 5.2-5s5.2 2 5.2 5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <path d="M7.4 4.6h5.2" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </Icon>
  );
}

export function ScheduleIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="2.6" y="4" width="14.8" height="12" rx="1.8" stroke="currentColor" strokeWidth="1.4" />
      <path d="M2.6 8h14.8" stroke="currentColor" strokeWidth="1.3" />
      <path d="M6.4 2.6v2.8M13.6 2.6v2.8" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </Icon>
  );
}

export function KitchenSafetyIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4 4v12M4 4h5.4a3 3 0 0 1 0 6H4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M13 4v12M17 4v12M13 4h4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </Icon>
  );
}

export function WebhookIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M6 12.4a3.6 3.6 0 1 1 3.2-5.2" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <circle cx="14.4" cy="5.6" r="2" stroke="currentColor" strokeWidth="1.4" />
      <circle cx="5.6" cy="14.4" r="2" stroke="currentColor" strokeWidth="1.4" />
      <circle cx="14.4" cy="14.4" r="2" stroke="currentColor" strokeWidth="1.4" />
      <path d="M9.4 7.6 12.6 5M8 11.4l4.8 1.6" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
    </Icon>
  );
}

export function TeamIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="7" cy="6.4" r="2.6" stroke="currentColor" strokeWidth="1.4" />
      <path d="M2.4 16c0-2.6 2-4.4 4.6-4.4s4.6 1.8 4.6 4.4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <circle cx="14.6" cy="7.4" r="2.1" stroke="currentColor" strokeWidth="1.3" />
      <path d="M12.8 11.6c2 .2 3.6 1.8 3.6 4" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
    </Icon>
  );
}

export const NAV_ICONS = {
  dashboard: DashboardIcon,
  incident: IncidentIcon,
  detection: DetectionIcon,
  site: SiteIcon,
  zone: ZoneIcon,
  camera: CameraIcon,
  edge: EdgeIcon,
  pipeline: PipelineIcon,
  rule: RuleIcon,
  recipient: RecipientIcon,
  notify: NotifyIcon,
  model: ModelIcon,
  audit: AuditIcon,
  grant: GrantIcon,
  settings: SettingsIcon,
  search: SearchIcon,
  filter: FilterIcon,
  alertBell: AlertBellIcon,
  ack: AckIcon,
  upload: UploadIcon,
  plate: PlateIcon,
  fire: FireIcon,
  ppe: PpeIcon,
  schedule: ScheduleIcon,
  kitchenSafety: KitchenSafetyIcon,
  webhook: WebhookIcon,
  team: TeamIcon,
} as const;

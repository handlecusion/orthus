/**
 * Shared primitives — Ojosama tokens.
 *
 *   Card           — white surface, 1px divider border, 6px radius,
 *                    --shadow-card (hover → --shadow-card-hover, no shift).
 *   Toolbar        — flex container for page-top control rows.
 *   ToolbarButton  — small white rectangular control: 32px tall, 1px
 *                    toolbar-border, 6px radius, icon + short label.
 *   Chip           — quiet text-meta badge for status/tags.
 *   ModeBadge      — uppercase 10px / 800 weight label (--text-badge).
 *   Banner         — light alert surface (fail / warn / pass).
 *   Skeleton       — neutral pulse for loading slots.
 *
 * Buttons go through ToolbarButton wherever possible. The legacy
 * `Button` (primary/ghost/outline) is removed: DESIGN.md prefers small
 * rectangular controls, not large pill primaries.
 */
import * as React from "react";

function cx(...parts: Array<string | false | null | undefined>) {
  return parts.filter(Boolean).join(" ");
}

/* -------------------------------- Card --------------------------------- */

export function Card({
  className,
  children,
  interactive = false,
  ...rest
}: React.HTMLAttributes<HTMLDivElement> & { interactive?: boolean }) {
  // Hover lifts shadow only; padding/border stay fixed so no layout shift.
  return (
    <div
      className={cx(
        "rounded-[var(--radius-card)]",
        interactive && "transition-shadow",
        className,
      )}
      style={{
        background: "var(--color-card)",
        border: "1px solid var(--color-divider)",
        boxShadow: "var(--shadow-card)",
      }}
      onMouseEnter={
        interactive
          ? (e) => {
              e.currentTarget.style.boxShadow = "var(--shadow-card-hover)";
            }
          : undefined
      }
      onMouseLeave={
        interactive
          ? (e) => {
              e.currentTarget.style.boxShadow = "var(--shadow-card)";
            }
          : undefined
      }
      {...rest}
    >
      {children}
    </div>
  );
}

/* ------------------------------ Page header ---------------------------- */

export function PageHeader({
  title,
  subtitle,
  right,
}: {
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  right?: React.ReactNode;
}) {
  return (
    <header
      className="orthus-page-header flex flex-wrap items-center justify-between gap-3 px-5 py-3"
      style={{
        background: "var(--color-app-canvas)",
        borderBottom: "1px solid var(--color-divider)",
      }}
    >
      <div className="min-w-0">
        <h1
          className="orthus-page-title truncate text-[var(--text-day-heading)] font-extrabold"
          style={{ color: "var(--color-text-strong)", lineHeight: 1.1 }}
        >
          {title}
        </h1>
        {subtitle ? (
          <p
            className="orthus-page-subtitle mt-0.5 truncate text-[var(--text-meta)]"
            style={{ color: "var(--color-text-muted)" }}
          >
            {subtitle}
          </p>
        ) : null}
      </div>
      {right ? (
        <div className="orthus-page-actions flex min-w-0 w-full items-center gap-2 sm:w-auto sm:shrink-0">
          {right}
        </div>
      ) : null}
    </header>
  );
}

/* -------------------------------- Toolbar ------------------------------ */

export function Toolbar({
  className,
  children,
  ...rest
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cx("orthus-toolbar flex flex-wrap items-center gap-2", className)}
      {...rest}
    >
      {children}
    </div>
  );
}

/* ----------------------------- ToolbarButton --------------------------- */
/*
 * Small white rectangular control.
 *   - 32px tall (sits inside the 30-34px window from DESIGN.md)
 *   - 1px var(--color-toolbar-border)
 *   - 6px radius
 *   - subtle shadow at rest, hover bg → var(--color-toolbar-hover)
 *   - intended for icon + short label
 * Two emphases:
 *   default — normal toolbar action.
 *   primary — same shape, slightly stronger weight; not a brand fill.
 */

type ToolbarBtnTone = "default" | "primary" | "accent";

export const ToolbarButton = React.forwardRef<
  HTMLButtonElement,
  React.ButtonHTMLAttributes<HTMLButtonElement> & {
    tone?: ToolbarBtnTone;
    icon?: React.ReactNode;
  }
>(function ToolbarButton(
  { tone = "default", icon, className, children, disabled, ...rest },
  ref,
) {
  // accent — the one real brand-filled primary per surface (indigo). default /
  // primary keep the quiet white rectangle (primary is only a weight bump).
  const isAccent = tone === "accent";
  const restBg = isAccent ? "var(--color-accent-fill)" : "var(--color-card)";
  const hoverBg = isAccent
    ? "color-mix(in srgb, black 8%, var(--color-accent-fill))"
    : "var(--color-toolbar-hover)";
  return (
    <button
      ref={ref}
      disabled={disabled}
      className={cx(
        "orthus-toolbar-button inline-flex h-8 items-center gap-1.5 rounded-[var(--radius-toolbar)] px-2.5 text-[var(--text-body-sm)] transition-colors",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      style={{
        background: restBg,
        border: isAccent
          ? "1px solid var(--color-accent-fill)"
          : "1px solid var(--color-toolbar-border)",
        color: isAccent ? "var(--color-accent-fg)" : "var(--color-text-strong)",
        fontWeight: tone === "primary" || isAccent ? 600 : 500,
        boxShadow: "var(--shadow-toolbar)",
      }}
      onMouseEnter={(e) => {
        if (disabled) return;
        e.currentTarget.style.background = hoverBg;
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.background = restBg;
      }}
      {...rest}
    >
      {icon ? (
        <span className="flex h-4 w-4 items-center justify-center" aria-hidden>
          {icon}
        </span>
      ) : null}
      {children}
    </button>
  );
});

/* --------------------------------- Chip -------------------------------- */
/*
 * Quiet metadata badge — text-meta sized, low contrast. Use this for
 * "수정됨" / "자동" / status text inline with a row. Hot colors are
 * reserved for Banner / progress / attention dots per DESIGN.md.
 */

export type ChipTone = "neutral" | "pass" | "fail" | "warn" | "muted" | "accent";

export function Chip({
  tone = "neutral",
  children,
}: {
  tone?: ChipTone;
  children: React.ReactNode;
}) {
  const map: Record<ChipTone, { bg: string; fg: string; border?: string }> = {
    neutral: {
      bg: "var(--color-card)",
      fg: "var(--color-text-body)",
      border: "var(--color-divider)",
    },
    muted: {
      bg: "transparent",
      fg: "var(--color-text-muted)",
    },
    pass: { bg: "var(--color-pass-bg)", fg: "var(--color-pass-fg)" },
    fail: { bg: "var(--color-fail-bg)", fg: "var(--color-fail-fg)" },
    warn: { bg: "var(--color-warn-bg)", fg: "var(--color-warn-fg)" },
    // K7.3 — "내 개인 메모" 표식. accent-bg 토큰이 없어 outlined accent로 표현(ring halo와 동색).
    accent: { bg: "transparent", fg: "var(--color-accent)", border: "var(--color-accent)" },
  };
  const s = map[tone];
  return (
    <span
      className="inline-flex items-center gap-1 rounded-[4px] px-1.5 py-[1px] text-[var(--text-meta)] font-medium"
      style={{
        background: s.bg,
        color: s.fg,
        border: s.border ? `1px solid ${s.border}` : "none",
      }}
    >
      {children}
    </span>
  );
}

/* ------------------------------ ModeBadge ------------------------------ */
/*
 * Uppercase 10px / 800 weight label — matches --text-badge. Used to
 * tag a result's mode (STRUCTURED / WIKI / etc.) inline with the
 * answer. Color stays neutral; the label, not a fill, carries meaning.
 */

export function ModeBadge({
  children,
  tone = "neutral",
}: {
  children: React.ReactNode;
  tone?: "neutral" | "muted";
}) {
  const color =
    tone === "muted"
      ? "var(--color-text-muted)"
      : "var(--color-text-strong)";
  return (
    <span
      className="inline-flex items-center rounded-[3px] px-1.5 py-[2px] uppercase tracking-[0.06em]"
      style={{
        fontSize: "var(--text-badge)",
        fontWeight: 800,
        color,
        background: "var(--color-card)",
        border: "1px solid var(--color-divider)",
        lineHeight: 1,
      }}
    >
      {children}
    </span>
  );
}

/* --------------------------------- Field ------------------------------- */
/*
 * Token-driven input/textarea base. Surface stays card-white so an input
 * inside a Card reads as a quiet inset. Border uses divider, focus border
 * darkens to text-strong (matches the focus-visible outline elsewhere).
 */
export const inputClass =
  "w-full rounded-[5px] bg-[var(--color-card)] px-2.5 py-2 text-[var(--text-body)] text-[color:var(--color-text-strong)] placeholder:text-[color:var(--color-text-faint)] transition-colors focus-visible:outline-none";

export const inputStyle: React.CSSProperties = {
  border: "1px solid var(--color-divider)",
};

/* --------------------------- status feedback --------------------------- */

export function Banner({
  tone,
  title,
  children,
}: {
  tone: "fail" | "warn" | "pass";
  title: string;
  children?: React.ReactNode;
}) {
  const map = {
    fail: {
      bg: "var(--color-fail-bg)",
      fg: "var(--color-fail-fg)",
      bd: "color-mix(in srgb, var(--color-fail-fg) 22%, white)",
    },
    warn: {
      bg: "var(--color-warn-bg)",
      fg: "var(--color-warn-fg)",
      bd: "color-mix(in srgb, var(--color-warn-fg) 22%, white)",
    },
    pass: {
      bg: "var(--color-pass-bg)",
      fg: "var(--color-pass-fg)",
      bd: "color-mix(in srgb, var(--color-pass-fg) 22%, white)",
    },
  } as const;
  const s = map[tone];
  return (
    <div
      role="alert"
      className="rounded-[5px] px-2.5 py-2 text-[var(--text-body-sm)]"
      style={{
        background: s.bg,
        color: s.fg,
        border: `1px solid ${s.bd}`,
      }}
    >
      <div className="font-semibold">{title}</div>
      {children ? <div className="mt-0.5 opacity-90">{children}</div> : null}
    </div>
  );
}

/* --------------------------- loading skeleton -------------------------- */

export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      className={cx("animate-pulse rounded-[4px]", className)}
      style={{
        background:
          "color-mix(in srgb, var(--color-divider) 80%, var(--color-card))",
      }}
    />
  );
}

/* -------------------------------- Avatar ------------------------------- */
/*
 * Circular initial-avatar — the identity atom the app lacked (identity was an
 * 8-10px --channel-* dot + text name). Driven by the SAME member color source
 * (memberColorMap) so nothing is recolored. bg = member color at FULL strength
 * (saturated), fg = white or --color-text-strong picked by luminance, so the
 * avatar flips correctly in dark without depending on a pastel pill mix.
 */

type AvatarSize = "xs" | "sm" | "md" | "lg";
const AVATAR_PX: Record<AvatarSize, number> = { xs: 16, sm: 20, md: 24, lg: 32 };

function avatarInitials(name: string): string {
  const n = (name || "").trim();
  if (!n) return "?";
  if (/[가-힣]/.test(n)) return n.replace(/\s/g, "").slice(-2); // 박기획 → 이철
  const parts = n.split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return n.slice(0, 2).toUpperCase();
}

// Pick a readable fg over a saturated bg. Only resolves literal hex; css vars
// (e.g. var(--channel-blue)) fall back to white, which reads fine on the
// mid-saturation channel palette.
function avatarFg(color?: string): string {
  const hex = /^#([0-9a-fA-F]{6})$/.exec(color || "");
  if (!hex) return "#ffffff";
  const v = parseInt(hex[1], 16);
  const r = (v >> 16) & 255;
  const g = (v >> 8) & 255;
  const b = v & 255;
  const L = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return L > 0.62 ? "var(--color-text-strong)" : "#ffffff";
}

export function Avatar({
  name,
  color,
  size = "sm",
  presence,
  title,
}: {
  name: string;
  color?: string;
  size?: AvatarSize;
  presence?: "online" | "editing" | null;
  title?: string;
}) {
  const px = AVATAR_PX[size];
  const bg = color || "var(--color-text-faint)";
  const fg = avatarFg(color);
  return (
    <span
      className="relative inline-flex shrink-0 items-center justify-center rounded-full font-bold"
      style={{
        width: px,
        height: px,
        background: bg,
        color: fg,
        fontSize: Math.max(9, Math.round(px * 0.42)),
        lineHeight: 1,
        boxShadow:
          presence === "editing" ? "0 0 0 2px var(--color-progress)" : undefined,
      }}
      title={title || name}
      aria-label={name}
    >
      {avatarInitials(name)}
      {presence === "online" ? (
        <span
          className="absolute rounded-full"
          style={{
            width: Math.round(px * 0.3),
            height: Math.round(px * 0.3),
            right: -1,
            bottom: -1,
            background: "var(--color-progress)",
            boxShadow: "0 0 0 1.5px var(--color-card)",
          }}
        />
      ) : null}
    </span>
  );
}

/* ------------------------------ AvatarStack ---------------------------- */

export type AvatarPerson = {
  name: string;
  color?: string;
  presence?: "online" | "editing" | null;
};

export function AvatarStack({
  people,
  size = "sm",
  max = 4,
}: {
  people: AvatarPerson[];
  size?: "sm" | "md";
  max?: number;
}) {
  const ordered = [...people].sort(
    (a, b) =>
      (b.presence === "editing" ? 1 : 0) - (a.presence === "editing" ? 1 : 0),
  );
  const shown = ordered.slice(0, max);
  const overflow = ordered.length - shown.length;
  const px = size === "md" ? 24 : 20;
  return (
    <span className="inline-flex items-center pl-1.5">
      {shown.map((p, i) => (
        <span
          key={i}
          className="rounded-full"
          style={{ marginLeft: -6, boxShadow: "0 0 0 2px var(--color-card)" }}
        >
          <Avatar name={p.name} color={p.color} size={size} presence={p.presence} />
        </span>
      ))}
      {overflow > 0 ? (
        <span
          className="inline-flex items-center justify-center rounded-full font-bold"
          style={{
            marginLeft: -6,
            width: px,
            height: px,
            fontSize: Math.round(px * 0.4),
            background: "var(--color-divider)",
            color: "var(--color-text-muted)",
            boxShadow: "0 0 0 2px var(--color-card)",
          }}
        >
          +{overflow}
        </span>
      ) : null}
    </span>
  );
}

/* ----------------------------- ChannelHeader --------------------------- */
/*
 * Per-surface Slack channel context band. A BACKWARD-COMPATIBLE superset of
 * PageHeader (same title/subtitle/right contract) so any call site upgrades
 * for free and un-migrated pages keep working. glyph = '#' or a project dot;
 * people = an AvatarStack injected into the right slot before actions.
 */

export function ChannelHeader({
  title,
  subtitle,
  right,
  glyph,
  people,
}: {
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  right?: React.ReactNode;
  glyph?: "#" | { dot: string };
  people?: AvatarPerson[];
}) {
  const glyphNode =
    glyph === "#" ? (
      <span className="mr-1" style={{ color: "var(--color-text-faint)", fontWeight: 800 }}>
        #
      </span>
    ) : glyph && typeof glyph === "object" ? (
      <span
        className="mr-1.5 inline-block h-2.5 w-2.5 shrink-0 rounded-full"
        style={{ background: glyph.dot }}
      />
    ) : null;
  return (
    <header
      className="orthus-page-header flex flex-wrap items-center justify-between gap-3 px-5 py-3"
      style={{
        background: "var(--color-app-canvas)",
        borderBottom: "1px solid var(--color-divider)",
      }}
    >
      <div className="min-w-0">
        <h1
          className="orthus-page-title flex items-center truncate text-[var(--text-day-heading)] font-extrabold"
          style={{ color: "var(--color-text-strong)", lineHeight: 1.1 }}
        >
          {glyphNode}
          {title}
        </h1>
        {subtitle ? (
          <p
            className="orthus-page-subtitle mt-0.5 truncate text-[var(--text-meta)]"
            style={{ color: "var(--color-text-muted)" }}
          >
            {subtitle}
          </p>
        ) : null}
      </div>
      {people?.length || right ? (
        <div className="orthus-page-actions flex min-w-0 w-full items-center gap-2 sm:w-auto sm:shrink-0">
          {people?.length ? <AvatarStack people={people} size="md" /> : null}
          {right}
        </div>
      ) : null}
    </header>
  );
}

/* -------------------------------- ListRow ------------------------------ */
/*
 * One row grammar everywhere — generalized from the mail MailRow. leading =
 * Avatar / checkbox / none; hover actions fade in with a coarse-pointer
 * fallback (globals: .orthus-row-actions) so touch keeps them visible. tone
 * = accent (indigo selection) / attention (coral urgency) / progress. Never
 * resizes on hover (bg + shadow only), matching DESIGN.md.
 */

export function ListRow({
  leading,
  title,
  meta,
  trailing,
  actions,
  tone = null,
  emphasized,
  muted,
  active,
  onClick,
  className,
}: {
  leading?: React.ReactNode;
  title: React.ReactNode;
  meta?: React.ReactNode;
  trailing?: React.ReactNode;
  actions?: React.ReactNode;
  tone?: "accent" | "attention" | "progress" | null;
  emphasized?: boolean;
  muted?: boolean;
  active?: boolean;
  onClick?: () => void;
  className?: string;
}) {
  const [hover, setHover] = React.useState(false);
  const toneBar =
    tone === "attention"
      ? "var(--color-attention)"
      : tone === "accent"
        ? "var(--color-accent)"
        : tone === "progress"
          ? "var(--color-progress)"
          : null;
  const baseBg = active
    ? "var(--color-accent-soft)"
    : tone === "attention"
      ? "color-mix(in srgb, var(--color-attention) 12%, var(--color-card))"
      : tone === "accent"
        ? "var(--color-accent-soft)"
        : "transparent";
  return (
    <div
      className={cx(
        "group relative flex items-start gap-2.5 rounded-[6px] px-3 py-2.5 transition-colors",
        muted && "opacity-65 hover:opacity-100",
        onClick && "cursor-pointer",
        className,
      )}
      style={{
        background:
          hover && !active
            ? "color-mix(in srgb, var(--color-sidebar-active) 66%, transparent)"
            : baseBg,
        boxShadow: toneBar ? `inset 3px 0 0 ${toneBar}` : undefined,
      }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onClick={onClick}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={
        onClick
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onClick();
              }
            }
          : undefined
      }
    >
      {leading != null ? <div className="mt-0.5 shrink-0">{leading}</div> : null}
      <div className="min-w-0 flex-1">
        <div
          className="text-[var(--text-body-sm)]"
          style={{
            color: emphasized ? "var(--color-text-strong)" : "var(--color-text-body)",
            fontWeight: emphasized ? 800 : 500,
          }}
        >
          {title}
        </div>
        {meta != null ? (
          <div
            className="mt-0.5 truncate text-[var(--text-meta)]"
            style={{ color: "var(--color-text-muted)" }}
          >
            {meta}
          </div>
        ) : null}
      </div>
      {trailing != null ? (
        <div className="mt-0.5 flex shrink-0 items-center gap-1.5">{trailing}</div>
      ) : null}
      {actions != null ? (
        <div className="orthus-row-actions shrink-0 self-center opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
          {actions}
        </div>
      ) : null}
    </div>
  );
}

/* ----------------------------- SectionHeader --------------------------- */
/*
 * Double duty: variant="nav" = a collapsible sidebar section/channel-group
 * header (chevron + optional activity dot); variant="card" = an in-card
 * section title (bold 13px + count + right action). Generalizes the plans
 * <h2>+<p> pairs and the sidebar 13/800 uppercase labels.
 */

export function SectionHeader({
  label,
  variant = "card",
  count,
  action,
  collapsible,
  collapsed,
  onToggle,
  activityDot,
}: {
  label: React.ReactNode;
  variant?: "nav" | "card";
  count?: number;
  action?: React.ReactNode;
  collapsible?: boolean;
  collapsed?: boolean;
  onToggle?: () => void;
  activityDot?: boolean;
}) {
  if (variant === "nav") {
    return (
      <div className="flex items-center gap-1 px-2 pb-1 pt-1">
        {collapsible ? (
          <button
            onClick={onToggle}
            aria-expanded={!collapsed}
            className="flex items-center gap-1 text-[13px] font-extrabold uppercase tracking-[0.02em]"
            style={{ color: "var(--color-text-muted)" }}
          >
            <span
              className="inline-block transition-transform"
              style={{ transform: collapsed ? "rotate(-90deg)" : "none", fontSize: 9 }}
            >
              ▾
            </span>
            {label}
          </button>
        ) : (
          <span
            className="text-[13px] font-extrabold uppercase tracking-[0.02em]"
            style={{ color: "var(--color-text-muted)" }}
          >
            {label}
          </span>
        )}
        {activityDot ? (
          <span
            className="ml-1 h-1.5 w-1.5 rounded-full"
            style={{ background: "var(--color-attention)" }}
          />
        ) : null}
      </div>
    );
  }
  return (
    <div className="mb-2 flex items-center justify-between gap-2">
      <div className="flex min-w-0 items-center gap-1.5">
        {collapsible ? (
          <button
            onClick={onToggle}
            aria-expanded={!collapsed}
            className="inline-flex items-center text-[10px]"
            style={{ color: "var(--color-text-muted)" }}
          >
            <span
              className="inline-block transition-transform"
              style={{ transform: collapsed ? "rotate(-90deg)" : "none" }}
            >
              ▾
            </span>
          </button>
        ) : null}
        <h3
          className="truncate text-[var(--text-body-sm)] font-bold"
          style={{ color: "var(--color-text-strong)" }}
        >
          {label}
        </h3>
        {typeof count === "number" ? (
          <span
            className="shrink-0 text-[var(--text-meta)] font-semibold"
            style={{ color: "var(--color-text-faint)" }}
          >
            {count}
          </span>
        ) : null}
      </div>
      {action}
    </div>
  );
}

/* -------------------------------- Composer ----------------------------- */
/*
 * Slack-style bottom input that replaces the tiny "+ 추가" / "+ 항목" text
 * buttons. Enter = submit (matches the plans Enter=save+next), Shift+Enter =
 * newline. Reuses inputClass/inputStyle.
 */

export function Composer({
  placeholder,
  onSubmit,
  hint = "↵",
  className,
}: {
  placeholder: string;
  onSubmit: (text: string) => void;
  hint?: string;
  className?: string;
}) {
  const [v, setV] = React.useState("");
  return (
    <div
      className={cx("relative mt-2 pt-2", className)}
      style={{ borderTop: "1px solid var(--color-divider)" }}
    >
      <textarea
        value={v}
        onChange={(e) => setV(e.target.value)}
        placeholder={placeholder}
        rows={1}
        className={cx(inputClass, "resize-none pr-8")}
        style={{ ...inputStyle, minHeight: 40 }}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            const t = v.trim();
            if (t) {
              onSubmit(t);
              setV("");
            }
          }
        }}
      />
      <span
        className="pointer-events-none absolute right-2.5 top-[18px] text-[var(--text-meta)]"
        style={{ color: "var(--color-text-faint)" }}
      >
        {hint}
      </span>
    </div>
  );
}

/* -------------------------------- StatTile ----------------------------- */
/*
 * Pinned dashboard stat tile (value + optional delta). Upgrades the flat KPI
 * link-cards without a chart lib. delta up = progress green, down = attention.
 */

export function StatTile({
  label,
  value,
  delta,
  href,
}: {
  label: React.ReactNode;
  value: React.ReactNode;
  delta?: { dir: "up" | "down"; text: string };
  href?: string;
}) {
  const inner = (
    <Card interactive={!!href} className="p-3">
      <div className="truncate text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
        {label}
      </div>
      <div className="mt-1 flex items-baseline gap-1.5">
        <span
          className="text-[var(--text-day-heading)] font-extrabold"
          style={{ color: "var(--color-text-strong)" }}
        >
          {value}
        </span>
        {delta ? (
          <span
            className="text-[var(--text-meta)] font-semibold"
            style={{
              color: delta.dir === "up" ? "var(--color-progress)" : "var(--color-attention)",
            }}
          >
            {delta.dir === "up" ? "▲" : "▼"} {delta.text}
          </span>
        ) : null}
      </div>
    </Card>
  );
  return href ? (
    <a href={href} className="block">
      {inner}
    </a>
  ) : (
    inner
  );
}

export { cx };

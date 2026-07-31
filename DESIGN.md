# Ojosama Style Reference
> Quiet operational board for daily task flow

**Theme:** light productivity UI with selective dark floating tools

Ojosama uses a calm, dense, operations-first interface. The product should feel like a workspace people can leave open all day: low contrast chrome, compact task cards, restrained type, clear column rhythm, and very little decorative styling. The design avoids marketing-page composition. It is a working surface: sidebar, toolbar, day board, backlog, detail modal, and channel picker.

The primary visual language is light gray on white, with small color accents reserved for progress, channels, backlog bucket badges, and status dots. Floating editing surfaces stay minimal. The task detail modal is light. The channel picker is intentionally dark because it behaves like a command palette over the card surface.

## Tokens - Colors

| Name | Value | Token | Role |
|------|-------|-------|------|
| App Canvas | `#f5f5f6` | `--color-app-canvas` | Main board background and empty work area |
| Sidebar Surface | `#e6e7e8` | `--color-sidebar` | Left navigation background |
| Sidebar Active | `#d4d5d7` | `--color-sidebar-active` | Selected nav item and brand block |
| Panel Surface | `#fbfbfc` | `--color-panel` | Backlog panel and right-side surfaces |
| Card Surface | `#ffffff` | `--color-card` | Task cards, add rows, toolbar buttons |
| Modal Surface | `#ffffff` | `--color-modal` | Task detail popup |
| Popover Dark | `#1f2023` | `--color-popover-dark` | Channel picker and channel manager |
| Divider | `#dedfe1` | `--color-divider` | Column, panel, card, and toolbar borders |
| Divider Soft | `#ececee` | `--color-divider-soft` | Modal internal separators |
| Text Strong | `#303133` | `--color-text-strong` | Card titles, modal title, primary labels |
| Text Body | `#4b4c4f` | `--color-text-body` | Navigation labels, backlog labels, body text |
| Text Muted | `#737477` | `--color-text-muted` | Dates, secondary metadata, inactive labels |
| Text Faint | `#a0a1a4` | `--color-text-faint` | Placeholders and disabled actions |
| Progress Green | `#57bf78` | `--color-progress` | Day load progress and completed states |
| Channel Blue | `#5d9bd8` | `--color-channel-blue` | Example channel color |
| Channel Amber | `#efc47b` | `--color-channel-amber` | Example channel color |
| Channel Mint | `#62c4ba` | `--color-channel-mint` | Example channel color |
| Channel Violet | `#9b68d6` | `--color-channel-violet` | Example channel color |
| Attention Dot | `#ef7f62` | `--color-attention` | Small notification or priority dots |
| Accent Indigo | `#4a63d6` | `--color-accent` | Interactive: selection / active / links / primary fill |

### Color role model (Slack-style redesign, 2026-07)

3색을 **역할로 엄격 분리**한다. 리디자인의 근간이자 "코랄(또는 그린) 하나가 브랜드·긴급·선택을
전부 담당"하던 문제의 해소다.

| 토큰 | 값(라이트/다크) | 역할 |
|---|---|---|
| `--color-accent` 인디고 | `#4a63d6` / `#8b9cf5` | **인터랙티브**: 선택·활성 행·활성 채널·링크·1차 액션 필·포커스 |
| `--color-attention` 코랄 | `#ef7f62` | **긴급만**: 안읽음·마감초과·우선순위 (브랜드 아님) |
| `--color-progress` 그린 | `#57bf78` | **완료/존재감**: 체크·완료·프리즌스 링/점 |

- **액센트 토큰은 3곳 동시** 정의한다 — `globals.css`의 `:root`(인라인 `var()` 소비) +
  `@theme`(유틸 `bg-accent`/`text-accent` 소비) + `[data-theme="dark"]`(재선언 없으면 다크에서
  라이트 인디고로 얼어붙음). 플립돼야 하는 액센트는 **인라인 `var(--color-accent)`**로 읽는다.
- `--channel-blue`(#5d9bd8)는 멤버 점 색으로만 남기고 인터랙티브 fallback으로 쓰지 않는다
  (액센트가 blue+indigo로 갈라지는 것 방지).
- 개인 보드(`personal-ojosama.css`)는 태스크/완료 정체성으로 `--green`을 유지하되, commit/primary
  액션(채널 저장 등)은 공유 `--accent`(indigo)로 harmonize한다.

### 공용 프리미티브 (`web/src/components/ui.tsx`)

슬랙식 표면 문법. 대부분 기존 패턴의 일반화다.

- `Avatar` / `AvatarStack` — 원형 이니셜 아바타(멤버색 채도 fill + 휘도 fg, 다크 자동 플립).
  `presence="editing"`는 그린 링, `"online"`은 그린 점.
- `ChannelHeader` — `PageHeader`의 하위호환 superset(`glyph="#"|{dot}`, `people`→AvatarStack).
  표면별 채널 문맥 밴드. 콜사이트는 무seam 업그레이드.
- `ListRow` — 메시지형 한 행 문법(`leading`/`title`/`meta`/`trailing`/`actions` hover 클러스터 /
  `tone="accent"|"attention"|"progress"` / `active`). `MailRow` 일반화.
- `SectionHeader` — 카드 내 섹션 타이틀(볼드 + count + 우측 액션) 또는 nav 접이식 그룹헤더.
- `StatTile` — 핀 스탯 타일(값 + up=progress/down=attention delta + href).
- `ToolbarButton tone="accent"` — 표면당 1개 1차 액션에 인디고 브랜드 필.

## Tokens - Typography

### System Sans - Primary product UI font. - `--font-ui`
- **Substitute:** Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif
- **Weights:** 400, 500, 650, 700, 800
- **Sizes:** 8px, 10px, 11px, 12.5px, 13px, 14px, 15px, 16px, 20px, 22px, 24px
- **Line height:** 1.05, 1.1, 1.14, 1.18, 1.2, 1.35, 1.42
- **Letter spacing:** `0`
- **Role:** Every visible product surface. Use one font family so dense task data stays stable and quiet.

### Type Scale

| Role | Size | Line Height | Weight | Token |
|------|------|-------------|--------|-------|
| micro-label | 8px | 1.2 | 700 | `--text-micro` |
| badge | 9-10px | 1 | 800 | `--text-badge` |
| metadata | 11px | 1.15 | 500 | `--text-meta` |
| card-subtask | 12.5px | 1.14 | 400 | `--text-card-subtask` |
| body-sm | 13px | 1.3 | 500 | `--text-body-sm` |
| body | 14px | 1.35 | 500 | `--text-body` |
| nav | 17px | 1.2 | 500 | `--text-nav` |
| day-heading | 20px | 1.1 | 800 | `--text-day-heading` |
| modal-title | 24px | 1.42 | 500 | `--text-modal-title` |

## Tokens - Spacing & Shapes

**Base unit:** 4px

**Density:** compact

### Spacing Scale

| Name | Value | Token |
|------|-------|-------|
| 2 | 2px | `--space-2` |
| 4 | 4px | `--space-4` |
| 6 | 6px | `--space-6` |
| 8 | 8px | `--space-8` |
| 10 | 10px | `--space-10` |
| 12 | 12px | `--space-12` |
| 14 | 14px | `--space-14` |
| 18 | 18px | `--space-18` |
| 24 | 24px | `--space-24` |
| 28 | 28px | `--space-28` |
| 36 | 36px | `--space-36` |

### Border Radius

| Element | Value |
|---------|-------|
| Nav item | 6px |
| Toolbar button | 5-6px |
| Task card | 5-6px |
| Add row | 5-6px |
| Modal | 8-10px |
| Popover | 7px |
| Circle controls | 999px |

### Shadows

| Name | Value | Token |
|------|-------|-------|
| card | `0 1px 4px rgba(0, 0, 0, 0.08)` | `--shadow-card` |
| card-hover | `0 2px 4px rgba(0,0,0,0.07), 0 6px 16px rgba(0,0,0,0.08)` | `--shadow-card-hover` |
| modal | `0 18px 55px rgba(0, 0, 0, 0.18)` | `--shadow-modal` |
| popover | `0 20px 48px rgba(0, 0, 0, 0.32)` | `--shadow-popover` |

### Layout

- **Viewport model:** app fills `100vw x 100vh`; body does not scroll.
- **Main grid:** left nav `236px`, content `minmax(0, 1fr)`, right rail `56px`.
- **Board columns:** 3 visible day columns, each about `270px`.
- **Backlog panel:** about `326px`.
- **Right integration rail:** `56px`, vertically spaced icons.
- **Task cards:** fixed-feeling dimensions. Hover must not resize card.

## Components

### Left Navigation
**Role:** Persistent workspace navigation.

Surface `#e6e7e8`. Brand and active nav item use `#d4d5d7`. Items are 38px tall with 6px radius. Icons are line icons, 18px, muted gray. Section labels are uppercase, 13px, 800 weight. Keep disabled/secondary ritual items visually muted. Avoid badges unless needed.

### Board Toolbar
**Role:** Date, filter, view, and panel controls.

Use small white rectangular buttons with 1px border `#d4d5d8`, 5-6px radius, and subtle shadow. Height should feel around 30-34px. Buttons use icon plus short label. Hover changes background to `#f2f2f3`, not brand color.

### Day Column Header
**Role:** Day name, date, and planned-load progress.

Day heading 20px / 800. Date 14px muted. Progress track is thin, light gray, with green fill based on load or completion. Keep header compact; add-row should sit close below.

### Add Task Row
**Role:** Fast task capture inside each day.

White surface, 38px height, thin border, 5-6px radius. Left plus icon, optional placeholder, hidden submit affordance. Sort control appears only when column has tasks. Empty columns show plus row without extra controls.

### Task Card
**Role:** Dense task summary for scanning and direct completion.

White card, 1px border, small radius, compact padding. Card width follows day column. Title and subtasks must fit without hero-size type. Scheduled time may show as small text above title. Estimated duration badges are not part of this system.

Default footer: completion circle left, channel tag right. Hover reveals secondary action icons in place: calendar, clock, flag, archive, drag handle. Hover must not change card height or layout. Completed state reduces text contrast and can fill the completion circle.

### Task Detail Modal
**Role:** Full task editing surface.

Centered floating light modal, about 672px wide on desktop, viewport-contained height, 8-10px radius. Background overlay dimmed gray. Header contains channel, priority, start, due, subtask action, more menu, expand, close. Body uses title input, subtasks, notes, comment row, and activity log.

Modal edits happen here, not inline on cards. Comment and activity footer stays visible while notes scroll. One activity row is enough for local mock flows: `created this`.

### Channel Picker
**Role:** Fast channel assignment and channel management.

Dark popover over task card or modal. Width around 306px for picker, wider for manager. Header label `Assign to channel:`, search input, list rows with colored `#`, selected checkmark, and `Manage channels`. Manager view supports name edit, color swatches, save, and new channel creation. Do not convert this popover to light unless whole product style changes.

### Backlog Panel
**Role:** Right-side parking lot for future tasks.

Panel surface `#fbfbfc`, left border, compact header. Bucket rows use 44px height plus 4px gap for a 48px rhythm. Bucket badges are 16px circles with one letter. Row label 13px / 700. Plus action sits at far right. Backlog should feel lighter than task board, not like a second dense table.

### Integration Rail
**Role:** Thin right rail for integrations and quick panels.

56px width. Light gray surface. Icons are centered, muted, spaced vertically. Active item gets soft gray pill background. Notification dots are tiny and should not dominate.

## Do's and Don'ts

### Do
- Use compact, repeatable dimensions for cards, rows, icon buttons, and columns.
- Make hover controls appear through opacity, not layout shifts.
- Keep task detail editing in modal, not card inline controls.
- Use channel color only on small `#` glyphs or badges.
- Keep cards white and neutral; let content, not decoration, carry attention.
- Preserve three-column board plus backlog panel on desktop.
- Treat backlog as lightweight temporal buckets, not a calendar.
- Use line icons from a consistent set.
- Verify text does not overflow cards or buttons at desktop and narrow widths.

### Don't
- Do not use landing-page hero layouts, big marketing cards, or explanatory feature copy inside app UI.
- Do not add duration estimates, planned/actual columns, or time-budget badges unless product scope changes.
- Do not make calendar timeline primary for this product.
- Do not use large rounded pill buttons everywhere; this UI prefers small rectangles.
- Do not use decorative gradients, blobs, stock imagery, or empty illustration panels.
- Do not use one bright brand color across the whole interface.
- Do not resize cards on hover.
- Do not expose card edit controls that belong in task detail modal.

## Surfaces

| Level | Name | Value | Purpose |
|-------|------|-------|---------|
| 0 | App Canvas | `#f5f5f6` | Main work area |
| 1 | Sidebar | `#e6e7e8` | Persistent left nav |
| 2 | Panel | `#fbfbfc` | Backlog and side panels |
| 3 | Card | `#ffffff` | Tasks, add rows, toolbar buttons |
| 4 | Modal | `#ffffff` | Task detail editor |
| 5 | Dark Popover | `#1f2023` | Channel picker and manager |

## Elevation

- **Toolbar/Add Rows:** 1px border plus small shadow only.
- **Task Cards:** card shadow present but quiet; hover increases shadow, not scale.
- **Task Detail Modal:** strongest light-surface elevation. Overlay handles separation.
- **Channel Popover:** dark floating surface with stronger shadow and small arrow.
- **Backlog Panel:** no heavy shadow; separated by border only.

## Imagery

No decorative imagery required. This style is UI-native. Use icons, status dots, channel colors, and structured spacing instead of illustrations. If product requires empty states, use plain text and a small icon, not a large graphic.

## Layout

Use a full-viewport application shell. Left navigation is fixed-width. Center board scrolls horizontally/vertically as needed but should show 3 day columns on desktop. Backlog panel sits on the right and can collapse. Integration rail remains a thin fixed column.

Primary workflow:

1. Capture task in day add row or backlog bucket.
2. Scan day cards by time/title/subtasks/channel.
3. Hover card for secondary controls.
4. Click card to edit in modal.
5. Assign channel through dark picker.
6. Move work between day board and backlog through drag/drop or bucket actions.

Mobile/narrow adaptation:

- At `<760px`, app shell becomes one content column plus a bottom navigation bar.
  Desktop sidebar and integration rail are hidden; full navigation stays reachable
  through a compact bottom-sheet/drawer menu.
- Reserve bottom safe area with `env(safe-area-inset-bottom)` and use `100dvh`
  for shell height. Avoid plain `100vh` for primary mobile app containers.
- Page headers become compact: one-line title, hidden or collapsed subtitle,
  and horizontally scrollable toolbar actions. Header/action text must not push
  the content column wider than the viewport.
- Touch targets for primary navigation, toolbar buttons, form controls, and card
  menu actions should be at least 38-44px high on coarse pointers.
- Keep board focused on one active column. Use segmented tabs for day/status
  columns; horizontal board scroll can remain on desktop/tablet, but phone users
  should not need side-scrolling to read the active workflow.
- Do not rely on hover for card actions on mobile. Completion, detail open,
  status/move, archive, and channel/action menus need explicit visible controls
  or a `...` action surface.
- Keep detail modal viewport-contained and use bottom-sheet/full-height behavior
  on phone widths. Modal body scrolls internally; footer/input controls remain
  reachable above the safe area.
- Backlog, filters, connector details, and long secondary panels become drawer,
  bottom sheet, accordion, or separate stacked section instead of side-by-side.
- Ask/Wiki surfaces should prioritize reading and typing: compact page header,
  full-width answer cards, one-column filters, and horizontally scrollable prompt
  chips. Avoid explanatory copy above the primary task.
- Mobile QA requires real browser verification at phone viewport width. Check:
  no incoherent overlap, no unwanted page-level horizontal scroll, bottom nav
  reachable, drawer closes, one-column board/tab behavior, keyboard-safe inputs,
  and readable answer/card content.

## Agent Prompt Guide

Quick Color Reference:
text strong: #303133
text muted: #737477
app background: #f5f5f6
sidebar: #e6e7e8
active nav: #d4d5d7
card/modal: #ffffff
panel: #fbfbfc
border: #dedfe1
progress: #57bf78
dark popover: #1f2023

Example Component Prompts:

1. Build a compact daily planning board with a 236px gray left sidebar, three 270px day columns, white task cards, thin progress bars under date headers, and a 326px backlog panel on the right. Use muted grays, small line icons, no hero copy, and no decorative backgrounds.

2. Design a task card for an operations board. White card, 1px gray border, 6px radius, compact padding, small scheduled time, 14px title, 12.5px subtask rows, completion circle bottom-left, channel tag bottom-right. On hover, reveal calendar/clock/flag icons without changing card size.

3. Design a task detail modal. Centered white modal about 672px wide, dim overlay, compact top toolbar with channel, priority, start, due, subtasks, more, expand, close. Body has title input, subtask checklist, notes, comment row, and one activity row. Keep footer visible while body scrolls.

4. Design a channel picker. Dark popover, `Assign to channel:` label, search input, colored `#` channel rows, selected checkmark, and `Manage channels` link. Manager view includes editable channel names, color swatches, save buttons, and new channel row.

## Similar Brands

- **Sunsama** - Ritual-light daily task board, calm gray shell, task-first workflow.
- **Linear** - Precise density, restrained states, high-quality keyboard/product feel.
- **Todoist** - Task clarity, quick capture, low-friction hierarchy.
- **Cron/Notion Calendar** - Calm calendar-adjacent chrome, light surfaces, muted controls.
- **GitHub Projects** - Practical board density and direct manipulation, when softened visually.

## Quick Start

### CSS Custom Properties

```css
:root {
  /* Colors */
  --color-app-canvas: #f5f5f6;
  --color-sidebar: #e6e7e8;
  --color-sidebar-active: #d4d5d7;
  --color-panel: #fbfbfc;
  --color-card: #ffffff;
  --color-modal: #ffffff;
  --color-popover-dark: #1f2023;
  --color-divider: #dedfe1;
  --color-divider-soft: #ececee;
  --color-text-strong: #303133;
  --color-text-body: #4b4c4f;
  --color-text-muted: #737477;
  --color-text-faint: #a0a1a4;
  --color-progress: #57bf78;
  --color-attention: #ef7f62;

  /* Channel palette */
  --channel-blue: #5d9bd8;
  --channel-amber: #efc47b;
  --channel-mint: #62c4ba;
  --channel-violet: #9b68d6;
  --channel-pink: #d36be9;
  --channel-slate: #9fb4bf;

  /* Typography */
  --font-ui: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --text-micro: 8px;
  --text-badge: 10px;
  --text-meta: 11px;
  --text-card-subtask: 12.5px;
  --text-body-sm: 13px;
  --text-body: 14px;
  --text-nav: 17px;
  --text-day-heading: 20px;
  --text-modal-title: 24px;

  /* Spacing */
  --space-2: 2px;
  --space-4: 4px;
  --space-6: 6px;
  --space-8: 8px;
  --space-10: 10px;
  --space-12: 12px;
  --space-14: 14px;
  --space-18: 18px;
  --space-24: 24px;
  --space-28: 28px;
  --space-36: 36px;

  /* Radius */
  --radius-nav: 6px;
  --radius-card: 6px;
  --radius-modal: 10px;
  --radius-popover: 7px;
  --radius-circle: 999px;

  /* Elevation */
  --shadow-card: 0 1px 4px rgba(0, 0, 0, 0.08);
  --shadow-card-hover: 0 2px 4px rgba(0, 0, 0, 0.07), 0 6px 16px rgba(0, 0, 0, 0.08);
  --shadow-modal: 0 18px 55px rgba(0, 0, 0, 0.18);
  --shadow-popover: 0 20px 48px rgba(0, 0, 0, 0.32);

  /* App shell */
  --sidebar-width: 236px;
  --day-column-width: 270px;
  --backlog-width: 326px;
  --integration-rail-width: 56px;
}
```

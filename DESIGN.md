# GroundKit design system

One palette for every GroundKit product: the Airport conditions dashboard (`dives/`, `web/`), the intro website, and the iOS and Android apps. Each app keeps its platform's own controls, type and spacing: [Apple Human Interface Guidelines](https://developer.apple.com/design/human-interface-guidelines/) on iOS, [Material 3](https://m3.material.io/) on Android. The web follows the dashboard.

## 1. Principles

1. **Usable outdoors first.** Crews read the apps in sunlight, at night, in gloves and in a hurry. Text meets WCAG AA (4.5:1, or 3:1 for large or bold text); controls and chart marks meet 3:1.
2. **Status never by colour alone.** Every status carries an icon and a word (Normal ops, Caution, Warning; On time, Late, Very late).
3. **The brand never looks like a status.** The brand is blue. Amber, orange and red mean caution or warning and are not used for branding or primary controls.
4. **Aviation conventions win over the brand.** Flight categories keep their standard colours (VFR green, MVFR blue, IFR red, LIFR magenta), even though MVFR is close to the brand blue. Category badges always show their label.
5. **Native first on mobile.** Shared colours, platform controls. iOS uses the soft status tones in §4 as asset colours, each with an Increase Contrast variant; Android keeps fixed schemes rather than wallpaper colour, for contrast outdoors.

## 2. Brand colours: light blue to navy

| Token | Light | Dark | Use |
|---|---|---|---|
| `primary` (navy) | `#0B3D91` | `#60A5FA` | Filled buttons, selected chips and tabs, iOS tint, Material `primary` |
| `on-primary` | `#FFFFFF` | `#0B1B36` | Text and icons on `primary` |
| `accent` (blue) | `#2563EB` | `#60A5FA` | Links, focus rings, brand text such as eyebrows |
| `sky` | `#38BDF8` | `#7DD3FC` | Decoration only (logo gradient, hero pattern). Never text, never on its own as a control |
| `primary-container` | `#D9E3F8` | `#12305F` | Tinted panels, Material `primaryContainer` |
| `on-primary-container` | `#001A43` | `#D6E4FF` | Text on `primary-container` |

In dark mode the primary becomes light blue, so filled controls take navy text (`on-primary`) rather than white.

**Logo.** The wheel-and-chock mark, white, on a tile with a diagonal gradient from `#38BDF8` (top left) through `#1D6FD0` (45%) to `#0B3D91` (bottom right). The middle stop keeps the white glyph above 4:1 where it sits. The same artwork is used for the iOS app icon, the Android adaptive icon (gradient background, white foreground, foreground also used for the themed icon), the website and the dashboard favicon. The master is `web/public/icon.svg`.

## 3. Neutrals

| Token | Light | Dark |
|---|---|---|
| `bg` | `#FFFFFF` (Android `#F5F6F8`) | `#121417` |
| `surface` | `#FFFFFF` | `#1B1E23` |
| `ink` | `#1A1A1A` | `#E6E7E9` |
| `muted` | `#6A6A6A` | `#9AA0A8` |
| `rule` | `#E5E5E5` | `#2E333A` |
| `row-active` | `#F3F4F6` | `#23272E` |

iOS uses the system backgrounds and label colours instead (`.background`, `.secondary`).

## 4. Status and data colours

| Meaning | Light | Dark | iOS |
|---|---|---|---|
| OK, on time, VFR | `#15803D` | `#22C55E` | `StatusOK` |
| Caution, late | `#B45309` | `#FBBF24` | `StatusCaution` |
| Warning, very late, IFR | `#C81E1E` | `#F87171` | `StatusWarning` |
| Unknown | `#6B7280` | `#9AA0A8` | `.secondary` |
| MVFR | `#1D4ED8` | `#60A5FA` | `.blue` |
| LIFR | `#A21CAF` | `#E879F9` | `.purple` |
| Arrival path | `#2563EB` | `#60A5FA` | |
| Departure path | `#EA580C` | `#FB923C` | |

The light values are darker than the Tailwind defaults so that they pass 4.5:1 as text on white, and white text on them passes too.

**Soft status tones (iOS).** Status is shown as a muted tone on a pale container, not as a saturated fill: icon and label in the tone, body text in the normal text colour. Each tone passes 4.5:1 on its container. Normal ops uses a plain check (`checkmark.circle.fill`), not a seal, which reads as an endorsement.

| Meaning | Tone light / dark | Container light / dark | Increase Contrast tone light / dark |
|---|---|---|---|
| OK | `#2B744A` / `#86C9A0` | `#E3F0E7` / `#1E3427` | `#1D5535` / `#A9DDBD` |
| Caution | `#94591A` / `#E2B672` | `#F7ECDB` / `#3A2F1C` | `#6E400F` / `#F0CD94` |
| Warning | `#AD3B3B` / `#EE9B95` | `#F7E3E1` / `#3E2525` | `#8A2525` / `#F7BDB8` |
| Info | `#2F6299` / `#94B8E3` | `#E2EBF6` / `#1D2B3F` | `#1F4775` / `#B8D0F0` |

## 5. Type

| Platform | Family | Notes |
|---|---|---|
| Web (dashboard, website) | Inter | Tabular numerals for times and counts |
| iOS | SF Pro (system), SF Rounded for the clocks | Dynamic Type sizes; glove mode one step larger |
| Android | Roboto (system) | Material type scale one step larger (`Type.kt`) |
| Raw METAR, TAF, NOTAM | System monospace | |

## 6. Shape and touch

- Web: 6 px radius on controls, 10 px on cards.
- iOS: 14 to 16 pt radius on cards and tiles; buttons at least 60 pt high (gloves).
- Android: Material 3 shapes; touch targets at least 48 dp, primary actions 56 dp or more.

## 7. Where the tokens live

| Product | File |
|---|---|
| Dashboard | `dives/airport_conditions/index.tsx` (`LIGHT`, `DARK`) |
| Website | `src/app/globals.css` (`--brand`, `--status-*`) |
| iOS | `Assets.xcassets` (`AccentColor`, `OnAccent`, `AppIcon`, `Status*`, `Status*Container`) |
| Android | `ui/theme/Color.kt`, `ui/theme/Theme.kt`, `res/drawable/ic_launcher_*.xml` |

Change a value here first, then in each file above.

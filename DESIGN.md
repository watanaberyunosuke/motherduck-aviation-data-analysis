# GroundKit design system

One palette for every GroundKit product: the Airport conditions dashboard (`dives/`, `web/`), the intro website, and the iOS and Android apps. Each app keeps its platform's own controls, type and spacing: [Apple Human Interface Guidelines](https://developer.apple.com/design/human-interface-guidelines/) on iOS, [Material 3](https://m3.material.io/) on Android. The web follows the dashboard.

## 1. Principles

1. **Usable outdoors first.** Crews read the apps in sunlight, at night, in gloves and in a hurry. Text meets WCAG AA (4.5:1, or 3:1 for large or bold text); controls and chart marks meet 3:1.
2. **Status never by colour alone.** Every status carries an icon and a word (Normal ops, Caution, Warning; On time, Late, Very late).
3. **The brand never looks like a status.** The brand is navy. The signal yellow is decoration only (the tug in the logo, accent rules), never a control, a badge or a fill behind text, because amber means caution. Amber, orange and red are not used for branding or primary controls.
4. **Aviation conventions win over the brand.** Flight categories keep their standard colours (VFR green, MVFR blue, IFR red, LIFR magenta), even though MVFR is close to the brand blue. Category badges always show their label.
5. **Native first on mobile.** Shared colours, platform controls. iOS uses the soft status tones in section 4 as asset colours, each with an Increase Contrast variant; Android keeps fixed schemes rather than wallpaper colour, for contrast outdoors.

## 2. Brand colours: navy and signal yellow

From the Set 2 logo sheet (`GroundKit Logo Concepts.pdf`).

| Token | Light | Dark | Use |
|---|---|---|---|
| `primary` (navy) | `#0F1F3D` | `#E8ECF3` | Filled buttons, selected chips and tabs, iOS tint, Material `primary` |
| `on-primary` | `#FFFFFF` | `#0F1F3D` | Text and icons on `primary` |
| `accent` | `#2A4A8A` | `#9DB8E8` | Eyebrows, icons, focus rings, account links |
| `signal` (yellow) | `#F4C400` | `#F4C400` | Decoration only: the logo's tug, accent rules. Never text, never a control |
| `tint` | `#EEF1F6` | `#13213B` | Alternate section background, `primary-container` |
| `on-tint` | `#0F1F3D` | `#E8ECF3` | Text on `tint` |

In dark mode the page is deep navy and filled controls turn near-white with navy text. Data links and the arrival path keep the data blue in section 4.

**Logo.** A tug pushing back an aircraft, seen from the side: white aircraft, yellow (`#F4C400`) tug and tow bar, grey (`#8796AD`) wheels, on a flat navy (`#0F1F3D`) tile with a 22% corner radius, in both themes. On a navy page give the tile a faint white outline (20%). The master is `web/public/icon.svg`; the website's `src/app/icon.svg` is the same file. The same artwork is used for the iOS app icon and the Android adaptive icon (navy background, artwork as foreground).

**Wordmark.** "Ground" ExtraBold (800) and "Kit" Medium (500), set in Archivo at 125% width, navy on light and white on navy, to the right of the tile.

## 3. Neutrals

| Token | Light | Dark |
|---|---|---|
| `bg` | `#FFFFFF` (Android `#F5F6F8`) | `#0B1528` |
| `surface` | `#FFFFFF` | `#13213B` |
| `ink` | `#14213D` | `#E8ECF3` |
| `muted` | `#5A6478` | `#9AA6BC` |
| `rule` | `#DDE2EA` | `#26344F` |
| `row-active` | `#EEF1F6` | `#1B2B4A` |

`muted` passes 4.5:1 on `bg` and `surface` in both themes. iOS uses the system backgrounds and label colours instead (`.background`, `.secondary`).

## 4. Status and data colours

| Meaning | Light | Dark | iOS |
|---|---|---|---|
| OK, on time, VFR | `#15803D` | `#22C55E` | `StatusOK` |
| Caution, late | `#B45309` | `#FBBF24` | `StatusCaution` |
| Warning, very late, IFR | `#C81E1E` | `#F87171` | `StatusWarning` |
| Unknown | `#6B7280` | `#9AA6BC` | `.secondary` |
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
| Web (dashboard, website) | Inter; Archivo (semi-expanded, 112.5%) for headings and the wordmark | Tabular numerals for times and counts |
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
| Dashboard | `dives/airport_conditions/index.tsx` (`LIGHT`, `DARK`), `web/src/account/account.css`, `web/index.html` |
| Website | `src/app/globals.css` (`--brand*`, `--status-*`) |
| iOS | `Assets.xcassets` (`AccentColor`, `OnAccent`, `AppIcon`, `Status*`, `Status*Container`) |
| Android | `ui/theme/Color.kt`, `ui/theme/Theme.kt`, `res/drawable/ic_launcher_*.xml` |

Change a value here first, then in each file above.

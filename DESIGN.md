---
name: WordPress Manager
description: A quiet, compact operations console for a private WordPress server.
colors:
  canvas: "#f6f7f8"
  surface: "#ffffff"
  ink: "#202a33"
  muted: "#65717b"
  line: "#e5e9ec"
  green: "#23674e"
  green-soft: "#eaf4ef"
  sidebar: "#17262a"
  amber: "#8a5811"
  amber-soft: "#fff6e7"
typography:
  headline:
    fontFamily: "Segoe UI, Arial, sans-serif"
    fontSize: "30px"
    fontWeight: 650
    lineHeight: 1.25
    letterSpacing: "-0.035em"
  title:
    fontSize: "16px"
    fontWeight: 650
    lineHeight: 1.4
  body:
    fontFamily: "Segoe UI, Arial, sans-serif"
    fontSize: "14px"
    lineHeight: 1.65
rounded:
  control: "7px"
  panel: "12px"
spacing:
  compact: "7px"
  control: "14px"
  panel: "22px"
  section: "27px"
  desktop-gutter: "37px"
components:
  button:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "10px 14px"
  panel:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.panel}"
---

## Overview

Operate mode: prioritize scanability and trustworthy state labels. Dark, persistent navigation anchors a light workspace. The interface serves the server operator's daily checks. Assumed visual direction pending operator feedback; derived from the requested working dashboard rather than a marketing identity.

## Colors

Use restrained green for current navigation, health and destination links. Amber identifies review or missing visibility. Gray means unknown or unavailable; never turn a failed check into a green success state. Pair every status color with text.

## Typography

One local sans family, no font downloads. Monospace is reserved for package versions, timestamps and detection sources. Resource numbers use tabular figures. Compact table labels are intentionally secondary; maintain browser zoom and responsive overflow.

## Layout

Desktop: 238px fixed sidebar, 73px top bar, 37px content gutters, maximum 1510px main width. Four resources share a divided band. Main service inventory sits beside a 282px workspace summary. At 990px, resources become two columns and the inventory becomes full width. At 700px, navigation becomes a horizontally scrollable top row; main gutters become 19px. Tables scroll within their own region.

## Elevation & Depth

Flat surfaces with one light border. No decorative shadows, gradients, photographs, illustrations or charts invented from absent measurements.

## Shapes

12px panel radii, 7px controls, compact rectangular status labels. Author small SVG icons with consistent 1.65px strokes.

## Components

Shared software table, state label and icon macros. All server values are HTML-escaped. Refresh is a progressive-enhancement link: HTMX replaces the observed content; ordinary navigation works without JavaScript. A failed refresh preserves the prior reading and reveals an alert. Focus rings are visible, and the skip link reaches the main content. No decorative page-load animation.

## Do's and Don'ts

- Show actual machine identity and observation time.
- Distinguish package installation from active process status.
- Say Unknown when a probe cannot establish a fact.
- Explain future capabilities in empty states without presenting a working mutation action.
- Do not claim that the whole server, backups or staging are secure from application-only checks.

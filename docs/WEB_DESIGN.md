# Web design direction

## Product subject

The web application is a formal linguistic evaluation interface for students,
teachers, and NLP researchers. The first release has one job: help a visitor
decide what a registered challenge measures, how its result is identified, and
whether the current deployment accepts submissions.

Functional structure is informed by EvalAI and Kattis. Accessibility, plain
language, errors, and page states follow GOV.UK Design System principles. The
visual identity is original and does not reproduce those products.

## Visual system

The direction is **linguistic annotation desk**: public challenge records read
like carefully indexed corpus entries, while a four-step evaluation trace makes
the deterministic process visible. This avoids both a marketing landing page
and a generic card-based administration dashboard.

### Color tokens

| Token | Value | Purpose |
| --- | --- | --- |
| Archive | `#F0F3F1` | Page canvas |
| Paper | `#FBFCFA` | Primary reading surface |
| Ink | `#13211D` | Main text and dark trace |
| Corpus | `#0D5C49` | Identity and open state |
| Annotation | `#A64024` | Sparse emphasis and draft state |
| Rule | `#CAD4CF` | Structure and grouping |

Color never communicates a state without a text label. Keyboard focus uses a
separate high-contrast blue so it cannot be confused with brand decoration.

### Type roles

- Interface and Chinese text use the local system's high-quality CJK sans-serif
  family to avoid runtime font downloads and layout shifts.
- IDs, hashes, versions, and metric keys use a native monospace family.
- Display hierarchy comes from size, weight, measure, and spacing rather than an
  unrelated decorative font.

### Layout

List page:

```text
+------------------------------------------------------------------+
| identity                                            public catalog |
+------------------------------------------------------------------+
| page purpose                         deterministic evaluation trace |
+------------------------------------------------------------------+
| count                                                            |
| title / ID              language   task       metric       status |
| ruled challenge record                                           |
| ruled challenge record                                           |
+------------------------------------------------------------------+
```

Detail page:

```text
+------------------------------------------------------------------+
| identity                                            public catalog |
+------------------------------------------------------------------+
| back / record ID                                                 |
| challenge title                                      availability |
+-------------------------------------------+----------------------+
| task and metric explanation               | publication status   |
| public facts                              | public-data boundary  |
+-------------------------------------------+----------------------+
| collapsible version and integrity record                         |
+------------------------------------------------------------------+
```

At narrow widths, records use a compact two-column metadata grid and collapse
to one column only near the 320-pixel minimum. Information order is preserved;
the mobile layout does not hide required metadata.

## Signature element

The evaluation trace is the only assertive visual device:

```text
registered corpus -> controlled input -> fixed model -> code scoring
```

It describes the real system architecture and reinforces why results are
reproducible. No other decorative illustration, gradient, glass surface, or
ambient animation competes with it.

## Content rules

- Chinese is primary. Stable API identifiers remain in their original form.
- Labels name concepts users recognize, not backend implementation details.
- Unknown task, language, or metric values remain visible instead of being
  guessed or suppressed.
- The UI never invents dates, participation counts, rankings, descriptions, or
  submission actions.
- Empty and failure states explain the condition and provide one valid next
  step when one exists.

## Quality gates

- Semantic landmarks and heading order.
- Complete keyboard access and visible focus.
- WCAG 2.2 AA color contrast and non-color status labels.
- Useful layouts at 360, 768, 1024, and 1440 CSS pixels.
- Reduced-motion preferences respected.
- Loading, empty, service failure, and not-found states.
- Automated component/API tests, lint, TypeScript checking, and production build.
- Final visual and UX-copy review before merge.

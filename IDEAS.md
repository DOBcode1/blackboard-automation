# IDEAS

A scratch pad for cross-domain applications of the blackboard-automation
technology stack. The core pipeline (authenticated scraping → text
extraction → AI structuring → conversational query) is potentially
reusable beyond academia.

**Important:** do NOT act on anything in this file. It exists to capture
ideas without diluting focus on the academic product. Revisit only after
the academic product has shipped and proven product-market fit.

---

## Adjacent domains (lowest-risk first tests)

- **K-12 LMS platforms** — PowerSchool, Schoology, Canvas K-12. Same user
  mentality (students/parents), different platform.
- **Graduate program portals** — LSAC, AAMC, application tracking systems
- **Professional certification platforms** — CFA, CPA, bar exam prep portals

## Distant domains (validate adjacent first)

- **Medical patient portals** — MyChart, Epic, Cerner. Patients drowning
  in test results, appointments, medication schedules.
- **Government benefit systems** — SSA, VA, state unemployment portals.
  Notoriously poor UX, high-stakes deadlines.
- **Legal case management** — clients accessing case files, court dates,
  filings.
- **Real estate transaction platforms** — buyers/sellers tracking
  inspections, contingencies, closing dates.
- **Corporate internal wikis** — Confluence, Notion workspaces at scale,
  where employees can't find anything.

## Architectural notes

To preserve this optionality, keep the codebase modular:

- **Generic engine layer** (reusable): authenticated browser sessions,
  text extraction pipeline, AI structuring prompts, conversational query
  interface
- **Domain layer** (academic-specific): course concepts, semester anchors,
  Blackboard DOM selectors, assignment types, grading concepts

The split should be clear enough that swapping the domain layer would
let the engine point at a different platform without engine rewrites.

## When to revisit

Not before:
- Academic product has paying users
- Multi-school expansion (Phase 11) is underway or complete
- A clear bottleneck in academic growth motivates exploring adjacent markets

---

# Documentation validation · 24 November 2026

## Evidence reviewed

The baseline for this review was PR #8 at `09f9b684`. The audit inventoried 319 branch files and traced the production path through the webhook handler, landing Lambda, projector, slot and NAT controllers, Glue job, Redshift loader, dbt models/tests, churn SQL and Tableau access template. The November 23–24 engineering audit supplied dated network, ownership and retirement evidence. This documentation review did not run a live slot.

The branch already had a Silver contract, dimensional model, marts ERD, classifier evaluation, Tableau guide and Terraform safeguards. Those references remain the detailed specifications. The new guides connect them, add missing workflow and database views, and correct stale retirement, warehouse-load and access-host descriptions.

## Acceptance checks

| Check | Result |
| --- | --- |
| Architecture diagrams | Five editable Graphviz figures rendered to self-contained SVG with embedded AWS icons. Exports opened locally and layout reviewed. |
| Processing and ER diagrams | Activity, sequence and five ER diagrams rendered with Mermaid 11.12 in headless Chromium. Operational relationships checked against migrations. Analytical relationships checked against source declarations, model SQL and tests. |
| Navigation | Relative Markdown link and image targets checked throughout the repository. |
| Python CI equivalents | Ruff passed for src and tests. Script compilation passed. Unit suite passed with 420 passed and one skipped. |
| Scope | Documentation, diagram sources, icon assets and rendering utility only. Application, deployment and state files remain outside this change. |
| Operational examples | AWS CLI operations and query fields reviewed against the implementation. Local development commands exercised. Live warehouse queries, deployment commands and teardown were not executed. |

The recorded November 21 warehouse and model results describe a dated checkpoint. The later infrastructure retirement verifies resource ownership and cleanup. To establish a particular day's processing result, follow the slot-specific checks in the [operations guide](operations/README.md).

## Audience-readability revision — 25 November 2026

The original documentation generation focused on architectural coverage and diagram rendering. A subsequent editorial pass improved the experience for two readers:

- **Hiring manager / reviewer:** The root README now starts with the business problem, result, engineering decisions, limitations, and an explicit path to technical depth. It does not present synthetic data or historical checkpoints as live commercial results.
- **Developer / maintainer:** The README and operations guide now offer a local-first test path, relevant entry points, a change-to-test map, and a clear boundary between read-only checks and AWS/Shopify mutations. The pipeline and architecture guides introduce plain-language workflows and key terms before detailed implementation.

The changes are restricted to Markdown; **no diagrams, application code, Terraform or AWS resources were changed in this editorial pass**. Existing diagram-rendering and unit-test results above are historical validation for the preceding documentation baseline, not newly executed tests for this revision. GitHub CI and rendered link inspection should still be checked at final PR review.

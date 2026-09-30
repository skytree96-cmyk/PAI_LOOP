# Department comparison bars — 2026-10-01

The department dashboard keeps its chart, detail metrics, and selection-rate
layout. The chart now compares notice counts on one shared axis: two adjacent
bars per department represent recommendations and explicit selections.

- The signed-in department stays first regardless of rank. Up to four other
  departments follow, ordered by selected count, then department name and ID.
  Organization accounts without a department see the top four departments.
- Clicking a department updates the detail metrics and recommendation-selection
  intersection. It does not change the notice list's department or work queues.
- Recommendations retain the open, not-cancelled keyword TOP/ROUTING definition.
  Selections retain latest-per-department GO/CONDITIONAL_GO over all stored
  notices, including closed notices and selections outside recommendations.
- `/dashboard/departments` adds `total_notice_count`, and per-department
  `selected_recommended_count` and `selection_available`. Existing coverage
  fields remain compatible; the comparison computes its rate from the explicit
  intersection instead of the coverage endpoint's legacy selection-rate field.
- Loading/error states distinguish missing counts from zero. A failed refresh
  labels retained data and offers a retry. Account changes clear cached comparison
  data and selection. Request ordering prevents older refreshes replacing newer
  data. Saved decisions and board reloads refresh department comparisons.

Validation: department statistics/coverage parity tests exercise cancelled and
closed notices, latest decision revisions, overlapping department choices, and
recommendation intersections. Frontend tests cover pinning, top-four ordering,
independent click details, zero/unknown values, and account resets. Browser
inspection uses synthetic data with the actual component and renderer.

Deployment follows the existing GitHub main → Cloud Build → Cloud Run `pai`
pipeline. No database migration, provider call, or infrastructure change is needed.

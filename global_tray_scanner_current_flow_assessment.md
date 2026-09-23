# Global Tray Scanner - Current Flow Assessment

## 1. Executive Summary

The global Tray ID scan/search feature is implemented mainly in `static/templates/base.html` and `adminportal/global_scan.py`.

Current behavior is frontend-initiated:

- User opens the global scanner from the header `Scan` button.
- `base.html` captures typed, pasted, or Enter-submitted scanner input.
- JavaScript posts `{tray_id, current_path}` to `/adminportal/global_tray_search/`.
- `GlobalTraySearchView` resolves candidate lot IDs from many tray/history tables, then checks module pick-table ownership in priority order.
- Backend returns the module, URL, lot ID, batch ID, optional page, and optional Jig Unloading metadata.
- Frontend either highlights the matching row on the current page or redirects to the returned URL with `?highlight=...&lot=...&batch=...&page=...`.
- Destination pages inherit `base.html`, whose auto-highlight script reads query params/sessionStorage, hunts for the row, scrolls to it, and applies `gs-hi`.
- Some same-page flows also auto-open module-specific modals, especially Jig Unloading and Input Screening.

Root cause for the current navigation behavior: the global scanner is intentionally built as a cross-module locator. The backend returns a navigable `url`, and `base.html:navigateToTrayModule()` calls `window.location.href` when the resolved module URL differs from the current path.

## 2. Current User Flow

Actual flow:

1. User clicks header `Scan` button or uses the configured shortcut.
2. `#globalScanStatus` appears and focuses `#globalScanInput`.
3. User scans, pastes, types 9 characters, or presses Enter.
4. `runScanFlow()` normalizes the value and posts to the global lookup API.
5. Backend returns a module result or a not-found/restricted response.
6. If result URL matches current path, frontend searches the current table and highlights/opens the row.
7. If result URL differs, frontend stores scan context in `sessionStorage`, appends URL query parameters, and redirects.
8. Destination page reads query parameters and highlights the target row.

## 3. Scanner UI Entry Point

FILE: `static/templates/base.html`

Key elements:

- `#globalScanBtn` at approximately line 1461.
- `#globalScanStatus` dropdown/container around line 1470.
- `#closeScanBtn` around line 1479.
- `#scanStatusMessage` around line 1486.
- `#globalScanInput` around line 1487.
- Placeholder: `Scan or paste Tray ID...`.
- `#scanResult` around line 1493.

The scanner is in the base layout, so every template extending `base.html` receives the global scan UI and scripts.

Important related display elements:

- `#scanInfoIcon`
- `#scanInfoText`

These are used by scanner status/banner messaging.

## 4. Frontend Event Flow

FILE: `static/templates/base.html`

Primary script begins around line 3510.

FUNCTION / HANDLER: `bindGlobalScanEvents()`

Approximate area: lines 3744-3807.

Responsibilities:

- Click on `#globalScanBtn` calls `openGlobalScanModal()`.
- Click outside scanner closes it.
- Click on `#closeScanBtn` closes it.
- `keydown` on `#globalScanInput` listens for `Enter`.
- `paste` normalizes input and immediately calls `runScanFlow()`.
- `input` normalizes input and auto-submits after 300 ms once length is at least `AUTO_SCAN_LENGTH = 9`.

Input detection methods currently used:

- `keydown` with Enter.
- `paste`.
- `input` with length-based auto-submit.
- Duplicate scan suppression in `isDuplicateScan()` with a 650 ms window.

FUNCTION: `runScanFlow(rawValue, source)`

Approximate area: lines 3818-3884.

Responsibilities:

- Normalizes the scan token via `normalizeTrayId()`.
- Disables the input while lookup is running.
- Sends fetch request to backend.
- Branches to restricted, not-found, same-page highlight, or cross-page navigation.

## 5. API / URL Flow

Frontend request:

FILE: `static/templates/base.html`

Approximate area: lines 3840-3848.

```javascript
fetch('/adminportal/global_tray_search/', {
  method: 'POST',
  credentials: 'same-origin',
  headers: {
    'Content-Type': 'application/json',
    'X-CSRFToken': getCookie('csrftoken'),
    'X-Requested-With': 'XMLHttpRequest'
  },
  body: JSON.stringify({ tray_id: trayId, current_path: window.location.pathname })
})
```

Django URL:

- URL: `/adminportal/global_tray_search/`
- URL name: `global_tray_search`
- urls.py file: `adminportal/urls.py`
- Pattern: `path('global_tray_search/', GlobalTraySearchView.as_view(), name='global_tray_search')`
- Included by project file: `watchcase_tracker/urls.py`
- Include path: `path('adminportal/', include('adminportal.urls'))`
- View: `adminportal.global_scan.GlobalTraySearchView`

Actual response shapes:

Success:

```json
{
  "success": true,
  "found": true,
  "module": "Brass QC",
  "url": "/brass_qc/brass_picktable/",
  "lot_id": "LID...",
  "batch_id": "BATCH...",
  "tray_id": "JB-A00001"
}
```

Some modules may also include:

```json
{
  "stock_lot_id": "LID...",
  "page": 2,
  "jig_completed_id": 123,
  "jig_id": "J098-0001"
}
```

Restricted:

```json
{
  "success": false,
  "found": true,
  "restricted": true,
  "tray_id": "JB-A00001",
  "message": "Currently it is available in '...' module"
}
```

Not found:

```json
{
  "success": false,
  "found": false,
  "tray_id": "JB-A00001",
  "message": "Not Exists"
}
```

Validation error:

```json
{
  "success": false,
  "error": "No tray_id provided"
}
```

## 6. Backend Tray Lookup

FILE: `adminportal/global_scan.py`

CLASS: `GlobalTraySearchView`

Entry method: `post()` around line 41.

Call sequence:

1. `post()`
2. `_search_all_modules(tray_id, current_path, user)`
3. `_resolve_candidate_lot_ids(tray_id, user)`
4. If Jig ID format, `_resolve_jig_unloading_by_jig_id()`
5. Per-module `_check_lot_in_*()` functions
6. `_user_can_access_result()` permission check
7. JSON response

The backend does not directly redirect. It returns a URL and metadata. Navigation is performed in JavaScript.

## 7. Module Resolution Logic

The logic is centralized in `adminportal/global_scan.py`, but it depends on module-specific selector/query logic.

Candidate phase:

`_resolve_candidate_lot_ids()` scans all relevant tray sources to collect possible `lot_id` and `batch_id` values. This stage intentionally includes historical/current tray records, because a tray may be inherited by downstream modules without being reinserted into every module's own tray table.

Current-location phase:

`_search_all_modules()` tests each candidate lot against active module pick-table/current workflow conditions. That second phase determines the current module.

Search priority in code, around lines 458-474:

1. Inprocess Inspection
2. Jig Unloading
3. Inprocess Inspection again
4. Nickel Wiping
5. Nickel Wiping Z2
6. Nickel Audit Z1
7. Nickel Audit Z2
8. Spider Spindle Z1
9. Spider Spindle Z2
10. IQF
11. Brass Audit
12. Brass QC
13. Input Screening
14. Day Planning
15. Jig Loading

Note: the class docstring says a different priority order, but the executable `checks` list above is the actual implementation.

Handling historical duplicates:

- The resolver may collect multiple candidate lots from historical tray records.
- Current module is not determined by the first raw tray-table hit.
- Each candidate lot is checked against pick-table/current workflow conditions.
- Completed/reject results are built by some checkers but are filtered out by `_is_main_or_pick_result()` in `_search_all_modules()`, so the primary navigation target is intended to remain a main/pick page.
- The first eligible result in the priority list becomes the fallback result.
- If a returned URL matches the submitted `current_path`, that same-page result is preferred.

## 8. Redirect Logic

FILE: `static/templates/base.html`

FUNCTION: `resultBelongsToCurrentPath(responseData)`

Approximate area: lines 3886-3918.

Responsibility:

- Compares returned `responseData.url` pathname to `window.location.pathname`.

FUNCTION: `navigateToTrayModule(responseData, trayId)`

Approximate area: lines 3920-3959.

Responsibility:

- Stores `globalScanModule`, `globalScanTrayId`, and `globalScanLotId` in `sessionStorage`.
- Builds query params:
  - `highlight`
  - `lot`
  - `batch`
  - `page`
- Executes navigation:

```javascript
window.location.href = targetUrl + separator + params.toString();
```

Navigation is frontend-initiated, not a Django `redirect()`.

## 9. Auto-Open Lot Logic

There are two related behaviors.

Cross-page navigation:

- `navigateToTrayModule()` appends query params.
- Destination page auto-highlight script reads:
  - `highlight`
  - `lot`
  - `batch`
- The destination table row is located by data attributes or cell text.

Same-page auto-open:

FILE: `static/templates/base.html`

Functions:

- `openDraftRowModal(row, responseData, trayId)` around lines 4530-4572.
- `maybeOpenInputScreeningVerifiedTrayModal(row, responseData, trayId)` around lines 4448-4499.
- `maybeOpenJigUnloadingTrayScan(row, responseData, trayId)` around lines 4502-4530.
- `handleTrayFound(match, responseData, trayId)` around lines 4574-4619.

Mechanisms:

- Looks for module action buttons inside the matched row.
- Calls `trigger.click()` for draft/view modals.
- Calls `window.openInputScreeningTrayVerificationFromGlobalScan()` if provided by Input Screening JS.
- Calls `window.openJigUnloadTrayScanFromGlobalScan()` if provided by Jig Unloading templates.

Module-specific functions found:

- `static/js/inputscreening_picktable.js`: `window.openInputScreeningTrayVerificationFromGlobalScan`.
- `static/templates/Jig_Unloading/Jig_Unloading_Main.html`: `window.openJigUnloadTrayScanFromGlobalScan`.
- `static/templates/Jig_Unloading - Zone_two/Jig_Unloading_Main_zone_two.html`: `window.openJigUnloadTrayScanFromGlobalScan`.

## 10. Tray Highlight Logic

Same-page highlight:

FILE: `static/templates/base.html`

Functions:

- `findTrayInCurrentPickTable()` around line 4266.
- `findRowInRoot()` around line 4052.
- `findRowWithDataTables()` around line 4082.
- `findRowInServerPagination()` around line 4227.
- `applyFoundHighlight(row)` around line 4322.
- `scrollRowToTop(row)` around line 4310.

CSS/classes:

- Global styles around lines 356-376.
- `gs-active-scan`
- `gs-hi`
- `gkb-row-focus`
- `dp-row-action-highlight`

Same-page highlight behavior:

- Adds `gs-active-scan`.
- Calls `window._gkbHighlightRow(row)` if available from `static/js/global_shortcut_manager.js`.
- Otherwise adds `gkb-row-focus` and `dp-row-action-highlight`.
- Scrolls row into view via `scrollRowToTop()`.

Cross-page destination highlight:

FILE: `static/templates/base.html`

Script starts around line 5197.

Functions:

- Reads `new URLSearchParams(window.location.search)`.
- Uses `highlight`, `lot`, and `batch`.
- Reads sessionStorage keys:
  - `globalScanModule`
  - `globalScanTrayId`
  - `globalScanLotId`
  - `globalScanBatchId`
- `findRow()` searches rows by:
  - `data-lot-id`
  - `data-stock-lot-id`
  - child data attributes
  - `data-batch-id`
  - `data-original-batch-id`
  - exact tray ID cell text
- `highlightRow(row)` adds `gs-hi`, scrolls to the row, removes the class after 12 seconds, and cleans the URL.

## 11. Models / Tables Queried

Actual lookup sources used in `adminportal/global_scan.py`:

| Model | File | Fields checked | Meaning / use | Order |
|---|---|---|---|---|
| `JigCompleted` | `Jig_Loading/models.py` | `jig_id`, `lot_id`, `last_process_module`, `draft_status`, `jig_position`, `draft_data` | Jig ID lookup, Jig Loading/Inprocess/Jig Unloading status | Early candidate and module checks |
| `JigLoadingRecord` via selectors | `Jig_Loading/models.py`, `Jig_Loading/selectors.py` | `scanned_trays`, `jig_id`, `lot_id`, `batch_id` | Active Jig Loading draft lookup by scanned tray/Jig ID | Candidate phase |
| `TotalStockModel` | `modelmasterapp/models.py` | `lot_id`, `batch_id`, workflow flags | Source stock row and movement flags for module status | Candidate and many module checks |
| `ModelMasterCreation` | `modelmasterapp/models.py` | `lot_id`, `batch_id`, `Moved_to_D_Picker`, `total_batch_quantity` | Day Planning current/completed state and batch fallback | Candidate and DP check |
| `TrayId` | `modelmasterapp/models.py` | `tray_id`, `lot_id`, `batch_id`, `scanned`, `delink_tray`, `rejected_tray` | Day Planning/source tray record | Candidate phase |
| `DraftTrayId` | `modelmasterapp/models.py` | `tray_id`, `lot_id`, `batch_id`, `delink_tray` | DP draft tray record | Candidate phase |
| `DPTrayId_History` | `DayPlanning/models.py` | `tray_id`, `lot_id`, `batch_id` | DP completed/history tray source used by Input Screening | Candidate phase |
| `IPTrayId` | `InputScreening/models.py` | `tray_id`, `lot_id` | Input Screening tray source | Candidate phase |
| `IP_Accepted_TrayID_Store` | `InputScreening/models.py` | `tray_id`, `top_tray_id`, `lot_id` | Accepted tray store | Candidate phase |
| `IP_TrayVerificationStatus` | `InputScreening/models.py` | `tray_id`, `lot_id`, `is_verified=True`, `verification_status='pass'` | Verified IS tray source | Candidate phase |
| `InputScreening_Submitted` | `InputScreening/models.py` | `lot_id`, `is_submitted`, `is_active` | IS completed status | IS module check |
| `IS_PartialRejectLot` | `InputScreening/models.py` | `parent_lot_id` | IS reject status | IS module check |
| `BrassTrayId` | `Brass_QC/models.py` | `tray_id`, `lot_id` | Brass QC tray source | Candidate phase |
| `Brass_Qc_Accepted_TrayID_Store` | `Brass_QC/models.py` | `tray_id`, `lot_id` | Brass QC accepted tray store | Candidate phase |
| `BrassAuditTrayId` | `BrassAudit/models.py` | `tray_id`, `lot_id` | Brass Audit tray source | Candidate phase |
| `Brass_Audit_Accepted_TrayID_Store` | `BrassAudit/models.py` | `tray_id`, `lot_id` | Brass Audit accepted tray store | Candidate phase |
| `Brass_Audit_Submission` | `BrassAudit/models.py` | `lot_id`, `is_completed` | Brass Audit completion status | Brass Audit check |
| `IQFTrayId` | `IQF/models.py` | `tray_id`, `lot_id` | IQF tray source | Candidate phase |
| `IQF_Accepted_TrayID_Store` | `IQF/models.py` | `tray_id`, `lot_id` | IQF accepted tray source | Candidate phase |
| `IQF_Submitted` | `IQF/models.py` | `lot_id`, `is_completed`, `rejected_qty` | IQF completed/reject status | IQF check |
| `JigLoadTrayId` | `Jig_Loading/models.py` | `tray_id`, `lot_id` | Jig Loading tray source | Candidate phase |
| `JigUnload_TrayId` | `Jig_Unloading/models.py` | `tray_id`, `lot_id` | Jig Unloading tray source | Candidate phase |
| `JUSubmittedZ1` | `Jig_Unloading/models.py` | `tray_data`, `lot_id`, `jig_completed_id`, `is_draft` | Finds trays stored in Jig Unloading JSON payload | Candidate phase |
| `JigUnloadAfterTable` | `Jig_Unloading/models.py` | `lot_id`, `combine_lot_ids`, `plating_color_id`, `total_case_qty`, `nq_*`, `na_*`, `ss_*` flags | Nickel Wiping/Audit/Spider current status and combined lot mapping | Candidate and module checks |
| `NickelQcTrayId` | `Nickel_Inspection/models.py` | `tray_id`, `lot_id` | Nickel Wiping Z1 tray source | Candidate phase |
| `nickel_inspection_zone_two.NickelQcTrayId` | `nickel_inspection_zone_two/models.py` | `tray_id`, `lot_id` | Nickel Wiping Z2 tray source | Candidate phase |
| `Nickel_AuditTrayId` | `Nickel_Audit/models.py` | `tray_id`, `lot_id` | Nickel Audit Z1 tray source | Candidate phase |
| `nickel_audit_zone_two.NickelQcTrayId` | `nickel_audit_zone_two/models.py` | `tray_id`, `lot_id` | Nickel Audit Z2 tray source | Candidate phase |
| `NickelAudit_Submission` | `Nickel_Audit/models.py` | `lot_id` | Nickel Audit completed event | Nickel Audit checks |
| `NickelWiping_FullAcceptRecord` | `Nickel_Inspection/models.py` | `source_lot_id` | Nickel Wiping completed accept event | Nickel Wiping check |
| `NickelWiping_FullRejectRecord` | `Nickel_Inspection/models.py` | `source_lot_id` | Nickel Wiping completed reject event | Nickel Wiping check |
| `NickelWiping_PartialAcceptRecord` | `Nickel_Inspection/models.py` | `source_lot_id`, `child_lot_id` | Nickel Wiping partial accept event | Nickel Wiping check |
| `SpiderSpindleZ1TrayId` | `SpiderSpindle_Z1/models.py` | `tray_id`, `lot_id` | Spider Spindle Z1 tray source | Candidate phase |
| `SpiderSpindleZ2TrayId` | `SpiderSpindle_Z2/models.py` | `tray_id`, `lot_id` | Spider Spindle Z2 tray source | Candidate phase |
| `Plating_Color` | `modelmasterapp/models.py` | `jig_unload_zone_1`, `jig_unload_zone_2`, `plating_color` | Zone routing for Jig Unloading/Nickel/Spider | Module routing |

## 12. Existing Data Available for Inline Status

| Field | Already Available? | Source | Additional Lookup Needed? |
|---|---:|---|---|
| Tray ID | Yes | Request value, response adds `tray_id` | No |
| Current Module | Yes | Response `module` from `_check_lot_in_*()` | No |
| Zone | Partly | Module string for Z2 routes, `_jig_unload_route()`, `Plating_Color` zone flags | Possibly for normalized separate `zone` field |
| Lot ID | Yes | Response `lot_id` or `stock_lot_id` | No |
| Lot Status | Partly | Module labels and workflow flags, e.g. active pick vs completed/reject branches | Yes, to return explicit `lot_status` consistently |
| Tray Status | Partly | `TrayId.scanned`, `delink_tray`, `rejected_tray`; accepted/reject/delink snapshot stores in module tables | Yes, to normalize status across modules |

## 13. Complete File Dependency Map

### A. Core Files

- `static/templates/base.html` - scanner UI, input handlers, API fetch, same-page highlight, cross-page redirect, destination auto-highlight.
- `adminportal/global_scan.py` - central global tray lookup, lot candidate resolution, module resolution, permission filtering, response construction.
- `adminportal/urls.py` - registers `global_tray_search`.
- `watchcase_tracker/urls.py` - includes `adminportal.urls` under `/adminportal/`.
- `adminportal/middleware.py` - provides `_MODULE_URL_MAP` used to map result URLs to allowed modules.
- `adminportal/services.py` - provides `get_user_allowed_module_names()` and `is_admin_user()` for result authorization.

### B. Destination / Highlight Files

- `static/templates/base.html` - destination query param/sessionStorage reader and `gs-hi` highlighter.
- `static/js/global_shortcut_manager.js` - provides row highlight helpers such as `_gkbHighlightRow()` and cleanup behavior.
- `static/js/inputscreening_picktable.js` - provides `openInputScreeningTrayVerificationFromGlobalScan()`.
- `static/templates/Jig_Unloading/Jig_Unloading_Main.html` - provides `openJigUnloadTrayScanFromGlobalScan()`.
- `static/templates/Jig_Unloading - Zone_two/Jig_Unloading_Main_zone_two.html` - provides Z2 `openJigUnloadTrayScanFromGlobalScan()`.
- Module templates that render searchable row identifiers (`data-lot-id`, `data-stock-lot-id`, `data-batch-id`, `data-tray-id`, `data-jig-id`) are indirectly involved because `base.html` row matching depends on those attributes.

### C. Data / Model / Utility Files

- `modelmasterapp/models.py` - `TrayId`, `DraftTrayId`, `TotalStockModel`, `ModelMasterCreation`, `Plating_Color`.
- `DayPlanning/models.py` - `DPTrayId_History`.
- `InputScreening/models.py` - `IPTrayId`, `IP_Accepted_TrayID_Store`, `IP_TrayVerificationStatus`, `InputScreening_Submitted`, `IS_PartialRejectLot`.
- `InputScreening/selectors.py` - `pick_table_queryset()`.
- `Brass_QC/models.py` - Brass QC tray and submission/status models.
- `Brass_QC/services/selectors.py` - Brass QC pick-table queryset.
- `BrassAudit/models.py` - Brass Audit tray and submission/status models.
- `BrassAudit/selectors.py` - Brass Audit pick-table queryset.
- `IQF/models.py` - IQF tray and submission/status models.
- `IQF/services/selectors.py` - IQF pick-table queryset.
- `Jig_Loading/models.py` - `JigLoadTrayId`, `JigCompleted`, active/draft Jig Loading records.
- `Jig_Loading/selectors.py` - `find_active_draft_by_jig_id()` and `find_active_draft_by_scanned_tray()`.
- `Jig_Unloading/models.py` - `JigUnload_TrayId`, `JUSubmittedZ1`, `JigUnloadAfterTable`.
- `Nickel_Inspection/models.py` - `NickelQcTrayId`, Nickel Wiping completion event records.
- `nickel_inspection_zone_two/models.py` - Z2 `NickelQcTrayId`.
- `Nickel_Audit/models.py` - `Nickel_AuditTrayId`, `NickelAudit_Submission`.
- `nickel_audit_zone_two/models.py` - Z2 Nickel Audit tray model.
- `SpiderSpindle_Z1/models.py` - `SpiderSpindleZ1TrayId`.
- `SpiderSpindle_Z2/models.py` - `SpiderSpindleZ2TrayId`.

## 14. Must Change / May Change / No Change

MUST CHANGE for future inline status behavior:

- `static/templates/base.html`
  - Stop calling `navigateToTrayModule()` for global scanner results.
  - Add/render an inline result panel under the scanner.
  - Keep same backend call but display returned fields instead of redirecting.
- `adminportal/global_scan.py`
  - Extend response with explicit `lot_status`, `tray_status`, and normalized `zone` if the UI must show them reliably.

MAY CHANGE:

- Add a small serializer/helper in `adminportal/global_scan.py` or a new adminportal service to normalize status fields.
- Add tests in `adminportal/tests.py` for inline response shape and no-redirect expectations.
- Improve response naming so frontend does not infer zone from `module` text.

SHOULD NOT NEED CHANGE:

- Module-specific tray scanning APIs.
- Tray occupancy validation logic.
- Movement business rules.
- Individual module submission logic.
- Existing tray table models, unless an explicit missing status source is proven.
- Existing pick-table selectors, because the global lookup already reuses them to avoid duplicate ownership rules.

## 15. Regression Risks

Global scanner vs module scanners:

- The global scanner lives in `base.html` and calls `/adminportal/global_tray_search/`.
- Module-specific tray scanners live inside production module templates/JS and use module-specific endpoints.
- Future changes should isolate global scanner behavior and not touch module tray validation endpoints.

Risks if changed carelessly:

- Removing redirect behavior without preserving backend lookup can break global search.
- Changing `adminportal/global_scan.py` priority can change current module resolution.
- Changing shared CSS classes (`gs-active-scan`, `gs-hi`, `dp-row-action-highlight`, `gkb-row-focus`) can affect keyboard row focus and module row highlighting.
- Removing query-param handling may affect current cross-page highlight behavior until future inline behavior is fully accepted.
- Changing `global_shortcut_manager.js` could affect keyboard workflows beyond scanning.
- Adding frontend-derived statuses would violate backend SSOT.
- Adding new global tray-state rules outside `GlobalTraySearchView` risks duplicating business logic.

Affected areas to protect:

- Day Planning normal release/scanning.
- Jig Loading draft and submitted rows.
- Jig Unloading Z1/Z2 tray scan opening.
- Input Screening verification flow.
- Brass QC/Audit pick-table resolution.
- IQF pick/completed/reject resolution.
- Nickel Wiping/Audit Z1/Z2 zone routing.
- Spider Spindle Z1/Z2 routing.
- Tray occupancy validation and reuse/delink rules.

## 16. Recommended Minimal-Change Approach

For the future implementation, reuse `GlobalTraySearchView` as the single lookup source.

Recommended path:

1. Keep `/adminportal/global_tray_search/` as the lookup endpoint.
2. Extend its response with explicit display fields:
   - `current_module`
   - `zone`
   - `lot_id`
   - `lot_status`
   - `tray_status`
3. In `base.html`, replace the cross-page branch in `runScanFlow()` with inline rendering.
4. Preserve a compatibility flag if needed, for example `navigate=false`, during transition.
5. Add backend tests for response fields and permission behavior.
6. Avoid touching module-specific tray validation endpoints.

Do not rewrite tray occupancy or module movement rules unless a specific mismatch is found. The existing lookup already attempts to use pick-table querysets and module movement flags as the source of truth.

## 17. Conclusion

The existing global tray scanner is a centralized lookup plus frontend navigation/highlight system. The backend determines the current module by resolving tray-to-lot candidates across tray tables and then checking those lots against module pick-table/current workflow rules. The frontend is responsible for redirecting, opening/highlighting rows, and maintaining scan UI state.

The future no-redirect feature can be implemented with limited changes by keeping the backend lookup and replacing the frontend navigation branch with an inline result renderer, plus adding explicit normalized status fields to the backend response.

Actual current flow:

```text
[Scanner Input: #globalScanInput in base.html]
      ->
[JS Handler: bindGlobalScanEvents/runScanFlow]
      ->
[API: POST /adminportal/global_tray_search/]
      ->
[Django View: GlobalTraySearchView.post]
      ->
[Lookup/Resolution: _resolve_candidate_lot_ids + _search_all_modules]
      ->
[Response: module/url/lot_id/batch_id/tray_id/...]
      ->
[Redirect: navigateToTrayModule -> window.location.href]
      ->
[Destination Module: URL returned by reverse(...)]
      ->
[Open Lot: row match + optional trigger.click/module hook]
      ->
[Highlight Tray: gs-hi / gs-active-scan / dp-row-action-highlight]
```

Proposed future architecture:

```text
[Scanner Input]
      ->
[Existing Lookup/Resolution]
      ->
[Status Response]
      ->
[Inline Tray Information Panel]
```

# Phase 3: Periodic reports (As-of dates)

Example outcome: ACME has the periodic report **Receivables** (key `party_code`, How often: Month-end).
1. The user opens **Receivables** → **Upload**. The date box suggests **31 Mar 2026 (missing)**.
2. They drop `receivables_mar.xlsx` → mapping → preview **31 Mar 2026: 120 rows for a new date** → Confirm.
3. In April a corrected file for 31 Mar arrives. Uploading it again gives the preview
   **31 Mar 2026: 2 new · 5 changed · 1 removed**. Only 31 Mar is replaced; 28 Feb is untouched.
4. **Dates** opens a calendar for 2026: Jan–Mar are coloured, and 30 Apr shows red (missing).

Back-filling: a SELECT that returns all month-ends of 2025 with a `report_date` column. On the
mapping screen, As-of date is set to "From a column: report_date", and the preview shows one line per date.

Earlier plans: `plan-phase-2a.md`, `plan-phase-2b.md`.

## Decisions (from the question rounds)
| # | Topic | Decision |
|---|---|---|
| 1 | Where the date comes from | Either one date picked on the upload page for the whole file, or, on the mapping screen, a file column ("From a column"), so one file can hold many dates. |
| 2 | Replace | Always replace the whole date: rows missing from the file are removed **for that date only**. There is no Add / update choice for periodic reports. |
| 3 | Allowed dates | A future date is refused ("31 Dec 2026 is in the future"). Any past date is allowed. |
| 4 | Undo | "Undo latest upload" works as now. It brings back exactly what the date or dates held before. |
| 5 | Remove a date | Uploaders get a **Remove this date** button. It is recorded in the history as an upload ("Removed 31 Mar 2026 · 120 rows") and can be undone. It is blocked if another dataset links to those rows. |
| 6 | How often | An optional setting for staff: Every day, Weekdays (Mon–Fri), Mon–Sat, Month-end, Quarter-end, or Any date (the default: no gaps shown). |
| 7 | Calendar | A **Dates** button opens a 12-month view of one year, with arrows to change the year. Loaded days are coloured; hovering shows "31 Mar 2026 · 120 rows · uploaded by Ravi on 2 Apr". Expected days with no data are red. Below it, a django-datatable list of dates (date, rows, last upload, Remove). |
| 8 | Suggested date | With How often set, the next missing expected date ("31 Mar 2026 (missing)"); otherwise today. The user can change it. |
| 9 | Pull again | Asks for the password **and** the date. The SELECT stays as saved, with no `{as_of}` placeholder yet. |
| 10 | Template | No As-of date column in the template. A file column named "As of date", "As-of date" or "Report date" is auto-mapped to the date. |
| 11 | Not in this phase | Trends and totals by day, week or month (Phase 5); browsing a date's rows (Phase 4). |
| 12 | Bad dates in a column | A blank, unreadable or future date blocks the upload: "Result row 5 (key 4010): As-of date is blank." |
| 13 | Many dates | Only the dates in the file are replaced. A file with Jan–Mar leaves Apr untouched. |
| 14 | Preview | One line per date (date, new, changed, removed; "new date" when first loaded), then the usual old → new details. |
| 15 | Where "missing" starts | Missing dates count from the **first loaded date** up to today. |
| 16 | Month-end / quarter-end | The last calendar day (31 Mar, 30 Jun), even on a Sunday. Quarters are Mar, Jun, Sep and Dec. |
| 17 | Changing How often | Allowed at any time. It only changes which dates show as missing and which date is suggested. |

## Build
**Model** (+ migration):
- `Dataset.how_often`: any / daily / weekdays / mon_sat / month_end / quarter_end; default any.
- `Upload.as_of_date` (one picked date, or empty), `Upload.as_of_column` (the file column name, or empty), and `Upload.dates` (JSON list of the dates touched, for the history and undo).

**`findata/periods.py`** (new, pure Python):
- `expected_dates(how_often, start, end)`
- `missing_dates(how_often, loaded, today)`: counts from the first loaded date
- `suggested_date(how_often, loaded, today)`

**Uploads** (`upload.py`):
- Stage `_as_of_date` from the picked date or from the mapped column. Bad dates become problems with the result row and key.
- Diff and apply scoped to the dates in the stage table:
  - new / changed: match on `_as_of_date` + key;
  - removed: rows of **those dates** that are not in the stage.
- The mode is always "replace dates".
- The preview summary is grouped by date.
- Undo goes through the same change log, whose key JSON includes `_as_of_date`.
- `remove_date(dataset, date, user)` is a change-logged "upload" with no file. It runs the link check first.

**Views and templates**:
- The upload page gets a date box (suggested date plus a "(missing)" hint) for periodic datasets. The File and Database tabs and Pull again both send it.
- The mapping screen gets an extra **As-of date** row: "The date picked on the upload page" or "From a column ▾".
- The preview shows the per-date table.
- The calendar page at `datasets/<id>/dates/?year=2026` has a 12-month grid (daisyUI) with tooltips, plus the django-datatable list with Remove (confirm page).
- The editor shows **How often** for periodic reports only, with a tooltip.
- The dataset row shows Upload, Pull again and **Dates** for periodic reports. The history shows the date or dates.

## Tests
- `periods`: each How often, month-end in Feb and in leap years, quarter-end, missing from the first loaded date only, suggested date.
- Picked date: a first load (new date); a re-upload replaces that date only (other dates untouched); removed rows only for that date.
- Date from a column: many dates in one file, with one preview line per date. Blank, bad and future dates give problems with the row and key.
- Future picked date refused. Undo restores the replaced date exactly. Remove a date, then undo it.
- A link from another dataset blocks removing a date.
- Pull again for a periodic dataset needs the date.
- Calendar: loaded and missing days, the year switch, the other company can't see it.
- How often can change while data exists.

## Not in this phase
Trends and totals (Phase 5), browsing rows (Phase 4), an `{as_of}` placeholder in SQL and scheduled pulls, financial-year or holiday calendars.

## Known limits and changes (after build)
- **How often** is set on the **Dates** page (staff only), not in the definition editor. Saving it there
  doesn't count as a definition change, so Undo and open previews keep working (decision 17).
- **Remove a date** is never blocked by links: a periodic report can't be linked to at all (its key repeats on
  every As-of date). The editor now says so instead of listing it under "Links to".
- With a date column remembered from last time, the upload page leaves the date empty. Typing a date **and**
  having a date column (or neither) stops Pull again on the mapping page, so the user picks one.
- The per-date summary is the preview's **Per date** tab (django-datatable). A file with several dates opens on it.
- "Missing" includes today for Every day / Weekdays / Mon–Sat.

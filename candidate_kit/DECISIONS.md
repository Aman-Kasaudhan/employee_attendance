# DECISIONS.md

1. **Indexes.** Employees: unique `emp_code`; `(department, emp_code)` for listing; `joined_on` for headcount. Logs: unique `(emp_code, date)` (the race guard, monthly view); `(date, emp_code)` for list and leaderboard; `(status, date, emp_code)` for status filters; `(emp_code, punch_in)` for punch-out. I rejected a covering index on `late_minutes`: extra write cost for little gain at this size.
2. **Punch-in race.** Both requests find the employee, then both try to insert. The unique index lets exactly one in; the other gets a duplicate-key error, which I turn into 409. No read-then-write.
3. **Ties.** The database ranks first ($rank, ties share a rank), then keeps rank <= limit. Everyone tied at the cutoff is returned, ordered by minutes then `emp_code`.
4. **Headcount.** The summary starts from `employees` (joined on or before month end) and looks up logs, so people with no logs still count.
5. **100x data.** Copy `department` onto each log (or keep daily roll-ups) so the trend needs no giant `$in` list of codes.
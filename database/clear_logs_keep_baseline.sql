-- ============================================================
-- Baseline attendance migration + day-log cleanup
-- Supabase SQL Editor -> New query -> paste -> Run once.
--
-- 1) Adds baseline columns to `subjects` (idempotent).
-- 2) Stores your university-portal totals as the baseline:
--        initial_total   = lectures conducted so far (163)
--        initial_present = 111   initial_absent = 52
--    From then on:  Overall Present = initial_present + Σ(day-log present)
--                   Overall Absent  = initial_absent  + Σ(day-log absent)
--                   Overall Total   = initial_total   + Σ(day-log scheduled)
-- 3) Clears the day-by-day logs so tracking starts fresh from this week.
--    `subjects` (and its baseline numbers) are NEVER touched by step 3.
-- ============================================================

-- 1) baseline columns ---------------------------------------
alter table public.subjects
    add column if not exists initial_total   integer not null default 0;
alter table public.subjects
    add column if not exists initial_present integer not null default 0;
alter table public.subjects
    add column if not exists initial_absent  integer not null default 0;

-- 2) baseline figures from the university portal (idempotent)
update public.subjects s
set initial_total   = v.t,
    initial_present = v.p,
    initial_absent  = v.a
from (values
    ('Basics Of Computer and C Programming',              17, 15,  2),
    ('Basics Of Computer and C Programming Lab',          12, 10,  2),
    ('Front-End Web Development',                         18, 10,  8),
    ('Front-End Web Development Lab',                     12,  5,  7),
    ('Environmental Studies - I',                         12, 10,  2),
    ('Communication & Professional Skills I',             16, 16,  0),
    ('Mini Project-I',                                     1,  1,  0),
    ('Fundamentals Of Intelligent & Autonomous Systems',  17, 12,  5),
    ('Technical Training - Advance Programming In C',      0,  0,  0),
    ('Computational & Quantum Physics',                   23, 13, 10),
    ('Computational & Quantum Physics Lab',               12,  4,  8),
    ('Computational Mathematics For Intelligent Systems', 23, 15,  8)
) as v(name, t, p, a)
where s.name = v.name;

-- 3) clear ALL day-by-day logs (baseline preserved in subjects) --
delete from public.attendance_log;
delete from public.daily_schedule;

-- …or, to keep the current week's entries, use these instead
-- (PostgreSQL date_trunc('week') returns this week's MONDAY):
-- delete from public.attendance_log
--   where date < date_trunc('week', current_date)::date;
-- delete from public.daily_schedule
--   where date < date_trunc('week', current_date)::date;
select date_trunc('week', current_date)::date as "current-week-monday";

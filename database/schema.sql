-- ============================================================
-- Attendance Tracking Portal — relational historical schema
-- Supabase SQL Editor -> New query -> paste -> Run
-- ============================================================

-- 1) The recurring weekly timetable (fully replaced whenever a new
--    timetable image is uploaded and processed)
create table if not exists public.weekly_schedule (
    id           bigint generated always as identity primary key,
    day_of_week  text not null
        check (day_of_week in ('Monday','Tuesday','Wednesday','Thursday',
                               'Friday','Saturday','Sunday')),
    subject_name text not null,
    position     integer not null default 0,   -- lecture order within the day
    created_at   timestamptz not null default now(),
    constraint weekly_schedule_day_subject_unique unique (day_of_week, subject_name)
);

-- 2) Historical day-by-day attendance log (one row per date + subject)
create table if not exists public.attendance_log (
    id           bigint generated always as identity primary key,
    date         date not null,
    subject_name text not null,
    status       text not null check (status in ('present', 'absent')),
    created_at   timestamptz not null default now(),
    constraint attendance_log_date_subject_unique unique (date, subject_name)
);

-- 3) Holidays (a holiday day's classes are simply never logged,
--    so they can never penalise the percentage)
create table if not exists public.holidays (
    id         bigint generated always as identity primary key,
    date       date not null unique,
    reason     text not null default 'Holiday',
    created_at timestamptz not null default now()
);

create index if not exists attendance_log_date_idx   on public.attendance_log (date);
create index if not exists weekly_schedule_day_idx   on public.weekly_schedule (day_of_week);

-- ------------------------------------------------------------
-- Row Level Security
-- The backend authenticates with your publishable/anon key, which IS
-- subject to RLS, so permissive policies are required for the app to
-- work. (If you switch the backend to the service_role key, it bypasses
-- RLS and you can drop these policies in production.)
-- ------------------------------------------------------------
alter table public.weekly_schedule enable row level security;
alter table public.attendance_log  enable row level security;
alter table public.holidays        enable row level security;

drop policy if exists "allow all (app)" on public.weekly_schedule;
create policy "allow all (app)" on public.weekly_schedule
    for all using (true) with check (true);

drop policy if exists "allow all (app)" on public.attendance_log;
create policy "allow all (app)" on public.attendance_log
    for all using (true) with check (true);

drop policy if exists "allow all (app)" on public.holidays;
create policy "allow all (app)" on public.holidays
    for all using (true) with check (true);

-- ------------------------------------------------------------
-- Handy view: one row per calendar day with the day's totals
-- ------------------------------------------------------------
create or replace view public.attendance_daily as
select
    l.date,
    count(*) filter (where l.status = 'present')                       as present,
    count(*) filter (where l.status = 'absent')                        as absent,
    case when count(*) = 0 then null
         else round(100.0 * count(*) filter (where l.status = 'present')
                    / count(*), 1) end                                 as percent,
    exists (select 1 from public.holidays h where h.date = l.date)     as is_holiday
from public.attendance_log l
group by l.date
order by l.date;

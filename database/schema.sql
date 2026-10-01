-- ============================================================
-- Attendance Portal v3 — MANUAL daily schedule + Groq chatbot
-- Supabase SQL Editor -> New query -> paste -> Run once.
--
-- Tables:
--   subjects        (id, name, created_at)                 — your semester's subjects
--   daily_schedule  (date, subject_id, lecture_count)      — how many classes on a date
--   attendance_log  (date, subject_id, present_count, absent_count)
--   holidays        (date, reason)
--
-- Stats formula: percentage = present / (present + absent).
-- Holidays are excluded from "scheduled" counts and their
-- attendance rows are cleared, so they can never penalise you.
-- ============================================================

-- 0) Retire the view from the v2 (OCR-era) schema.
drop view if exists public.attendance_daily;

-- 1) Preserve the v2 attendance history before changing the shape.
--    Old rows were (date, subject_name, status 'present'/'absent');
--    new rows are (date, subject_id, present_count, absent_count).
--    The old table is renamed (NOT dropped) to attendance_log_v2_backup
--    so your history stays queryable in the Supabase dashboard.
do $$
begin
    if to_regclass('public.attendance_log') is not null
       and not exists (
           select 1 from information_schema.columns
           where table_schema = 'public'
             and table_name   = 'attendance_log'
             and column_name  = 'present_count'
       )
    then
        if to_regclass('public.attendance_log_v2_backup') is null then
            alter table public.attendance_log rename to attendance_log_v2_backup;
        else
            -- a backup already exists — this old-shaped table can go
            drop table public.attendance_log cascade;
        end if;
        -- the legacy index kept its old name across the rename
        execute 'drop index if exists public.attendance_log_date_idx';
    end if;
end $$;

-- (The v2 weekly_schedule table is untouched and simply unused now;
--  drop it manually if you like: drop table public.weekly_schedule cascade;)

-- 2) Subjects ------------------------------------------------
create table if not exists public.subjects (
    id         bigint generated always as identity primary key,
    name       text not null unique,
    created_at timestamptz not null default now()
);

-- 3) How many lectures of each subject are scheduled on a date --
create table if not exists public.daily_schedule (
    id            bigint generated always as identity primary key,
    date          date not null,
    subject_id    bigint not null references public.subjects (id) on delete cascade,
    lecture_count integer not null check (lecture_count > 0),
    created_at    timestamptz not null default now(),
    constraint daily_schedule_date_subject_unique unique (date, subject_id)
);

-- 4) Attendance against the scheduled count -------------------
create table if not exists public.attendance_log (
    id            bigint generated always as identity primary key,
    date          date not null,
    subject_id    bigint not null references public.subjects (id) on delete cascade,
    present_count integer not null default 0 check (present_count >= 0),
    absent_count  integer not null default 0 check (absent_count  >= 0),
    created_at    timestamptz not null default now(),
    constraint attendance_log_date_subject_unique unique (date, subject_id)
);

-- 5) Holidays (same shape as v2 — existing rows are kept) -----
create table if not exists public.holidays (
    id         bigint generated always as identity primary key,
    date       date not null unique,
    reason     text not null default 'Holiday',
    created_at timestamptz not null default now()
);

-- 6) Indexes --------------------------------------------------
create index if not exists daily_schedule_date_idx    on public.daily_schedule (date);
create index if not exists daily_schedule_subject_idx on public.daily_schedule (subject_id);
create index if not exists attendance_log_date_idx    on public.attendance_log (date);
create index if not exists attendance_log_subject_idx on public.attendance_log (subject_id);

-- ------------------------------------------------------------
-- Row Level Security — the backend uses your publishable key,
-- which IS subject to RLS, so permissive policies are required.
-- ------------------------------------------------------------
alter table public.subjects       enable row level security;
alter table public.daily_schedule enable row level security;
alter table public.attendance_log enable row level security;
alter table public.holidays       enable row level security;

drop policy if exists "allow all (app)" on public.subjects;
create policy "allow all (app)" on public.subjects
    for all using (true) with check (true);

drop policy if exists "allow all (app)" on public.daily_schedule;
create policy "allow all (app)" on public.daily_schedule
    for all using (true) with check (true);

drop policy if exists "allow all (app)" on public.attendance_log;
create policy "allow all (app)" on public.attendance_log
    for all using (true) with check (true);

drop policy if exists "allow all (app)" on public.holidays;
create policy "allow all (app)" on public.holidays
    for all using (true) with check (true);

-- ------------------------------------------------------------
-- Seed: Semester 1 subjects (idempotent — safe to re-run)
-- ------------------------------------------------------------
insert into public.subjects (name) values
    ('Basics Of Computer and C Programming'),
    ('Basics Of Computer and C Programming Lab'),
    ('Front-End Web Development'),
    ('Front-End Web Development Lab'),
    ('Environmental Studies - I'),
    ('Communication & Professional Skills I'),
    ('Mini Project-I'),
    ('Fundamentals Of Intelligent & Autonomous Systems'),
    ('Technical Training - Advance Programming In C'),
    ('Computational & Quantum Physics'),
    ('Computational & Quantum Physics Lab'),
    ('Computational Mathematics For Intelligent Systems')
on conflict (name) do nothing;

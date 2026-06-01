-- ═══════════════════════════════════════════════════════
-- OutboundAI — performance + consistency migration
-- Safe to run multiple times (all IF NOT EXISTS / idempotent).
-- Run in Supabase Dashboard → SQL Editor.
-- ═══════════════════════════════════════════════════════

-- 1) Align agent_profiles default model with the app's canonical default.
--    (App code uses gemini-2.5-flash-native-audio-latest everywhere else.)
ALTER TABLE public.agent_profiles
    ALTER COLUMN model SET DEFAULT 'gemini-2.5-flash-native-audio-latest';

-- 2) Indexes for the columns the app filters/sorts on most often.
CREATE INDEX IF NOT EXISTS idx_call_logs_phone     ON public.call_logs   (phone_number);
CREATE INDEX IF NOT EXISTS idx_call_logs_timestamp ON public.call_logs   (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_appointments_phone  ON public.appointments (phone);
CREATE INDEX IF NOT EXISTS idx_appointments_date   ON public.appointments (date, time);
CREATE INDEX IF NOT EXISTS idx_appointments_status ON public.appointments (status);
CREATE INDEX IF NOT EXISTS idx_error_logs_ts       ON public.error_logs  (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_error_logs_src_lvl  ON public.error_logs  (source, level);
CREATE INDEX IF NOT EXISTS idx_campaigns_status    ON public.campaigns   (status);

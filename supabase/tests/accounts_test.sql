-- Checks the accounts migration on plain Postgres with a minimal stand-in for Supabase's
-- auth schema and roles. Run with scripts/test_supabase.sh. Any failed check raises.

\set ON_ERROR_STOP on
\o /dev/null

create or replace function pg_temp.check(ok boolean, what text) returns void language plpgsql as $$
begin
  if not coalesce(ok, false) then raise exception 'FAILED: %', what; end if;
  raise notice 'ok: %', what;
end $$;

-- Two users; the trigger creates their profiles and settings rows.
insert into auth.users (id, raw_user_meta_data) values
  ('11111111-1111-1111-1111-111111111111', '{"full_name": "Ada Ramp"}'),
  ('22222222-2222-2222-2222-222222222222', '{}');

select pg_temp.check((select display_name from public.profiles where id = '11111111-1111-1111-1111-111111111111') = 'Ada Ramp',
  'profile takes the provider name');
select pg_temp.check((select count(*) from public.user_settings) = 2, 'settings row per new user');

-- Signed in as user 1.
set role authenticated;
select set_config('request.jwt.claim.sub', '11111111-1111-1111-1111-111111111111', false);

select pg_temp.check(
  (select settings from public.merge_settings('{"airport": "VHHH", "gloveMode": true}',
     '{"airport": "2026-10-09T10:00:00Z", "gloveMode": "2026-10-09T10:00:00Z"}')) = '{"airport": "VHHH", "gloveMode": true}',
  'first merge stores the patch');

select pg_temp.check(
  (select settings ->> 'airport' from public.merge_settings('{"airport": "YMML"}', '{"airport": "2026-10-09T09:00:00Z"}')) = 'VHHH',
  'older stamp loses');

select pg_temp.check(
  (select settings from public.merge_settings('{"airport": "YSSY"}', '{"airport": "2026-10-09T11:00:00Z"}')) = '{"airport": "YSSY", "gloveMode": true}',
  'newer stamp wins and other keys are kept');

select pg_temp.check(
  (select (stamps ->> 'keepAwake')::timestamptz <= now() from public.merge_settings('{"keepAwake": true}', '{"keepAwake": "2999-01-01T00:00:00Z"}')),
  'future stamp is clamped to now');

select pg_temp.check(
  (select not (settings ? 'gloveMode') from public.merge_settings('{"gloveMode": null}', '{}')),
  'null removes the key');

do $$ begin
  perform public.merge_settings('{"bad key": 1}', '{}');
  raise exception 'FAILED: invalid key accepted';
exception when sqlstate '22023' then raise notice 'ok: invalid key rejected';
end $$;

do $$ begin
  perform public.merge_settings('[1]', '{}');
  raise exception 'FAILED: non-object patch accepted';
exception when sqlstate '22023' then raise notice 'ok: non-object patch rejected';
end $$;

-- RLS: user 1 sees only their own rows and cannot write settings directly.
select pg_temp.check((select count(*) from public.user_settings) = 1, 'sees only own settings');
select pg_temp.check((select count(*) from public.profiles) = 1, 'sees only own profile');

do $$ begin
  update public.user_settings set settings = '{}' where true;
  raise exception 'FAILED: direct settings write allowed';
exception when insufficient_privilege then raise notice 'ok: direct settings write refused';
end $$;

update public.profiles set display_name = 'Ada' where id = '22222222-2222-2222-2222-222222222222';
reset role;
select pg_temp.check((select display_name from public.profiles where id = '22222222-2222-2222-2222-222222222222') is null,
  'cannot rename another user');
set role authenticated;

update public.profiles set display_name = 'Ada R.';
select pg_temp.check((select display_name from public.profiles) = 'Ada R.', 'can rename self');
select public.set_display_name('  Ada Ramp  ');
select pg_temp.check((select display_name from public.profiles) = 'Ada Ramp', 'set_display_name trims');
select public.set_display_name('');
select pg_temp.check((select display_name from public.profiles) is null, 'set_display_name clears');

do $$ begin
  update public.profiles set id = '33333333-3333-3333-3333-333333333333';
  raise exception 'FAILED: profile id writable';
exception when insufficient_privilege then raise notice 'ok: profile id not writable';
end $$;

-- Signed out.
reset role;
set role anon;
select set_config('request.jwt.claim.sub', '', false);
do $$ begin
  perform public.merge_settings('{"airport": "VHHH"}', '{}');
  raise exception 'FAILED: anon could merge';
exception when insufficient_privilege then raise notice 'ok: anon cannot merge';
end $$;
do $$ begin
  perform count(*) from public.user_settings;
  raise exception 'FAILED: anon could read settings';
exception when insufficient_privilege then raise notice 'ok: anon cannot read settings';
end $$;

-- Delete own account cascades.
reset role;
set role authenticated;
select set_config('request.jwt.claim.sub', '11111111-1111-1111-1111-111111111111', false);
select public.delete_own_account();
reset role;
select pg_temp.check(not exists (select 1 from auth.users where id = '11111111-1111-1111-1111-111111111111'), 'auth user deleted');
select pg_temp.check(not exists (select 1 from public.user_settings where user_id = '11111111-1111-1111-1111-111111111111'), 'settings deleted');
select pg_temp.check(not exists (select 1 from public.profiles where id = '11111111-1111-1111-1111-111111111111'), 'profile deleted');
select pg_temp.check(exists (select 1 from auth.users where id = '22222222-2222-2222-2222-222222222222'), 'other user untouched');

-- A token that outlived its account gets PT401 (HTTP 401 from PostgREST), not a foreign-key error.
set role authenticated;
select set_config('request.jwt.claim.sub', '11111111-1111-1111-1111-111111111111', false);
do $$ begin
  perform public.merge_settings('{"airport": "VHHH"}', '{}');
  raise exception 'FAILED: deleted account could merge';
exception when sqlstate 'PT401' then raise notice 'ok: deleted account gets PT401';
end $$;
reset role;

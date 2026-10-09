-- GroundKit accounts: a profile and synced settings per user (Supabase Auth).
-- Sign-in is optional: the apps and the dashboard work without an account; signing in
-- syncs settings between iOS, Android and the dashboard. See docs/accounts.md.

-- Profiles ----------------------------------------------------------------------------

create table public.profiles (
  id           uuid primary key references auth.users (id) on delete cascade,
  display_name text check (char_length(display_name) <= 80),
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

alter table public.profiles enable row level security;

create policy "Read own profile" on public.profiles
  for select to authenticated using ((select auth.uid()) = id);
create policy "Update own profile" on public.profiles
  for update to authenticated using ((select auth.uid()) = id) with check ((select auth.uid()) = id);

-- set_display_name(name): the same as updating profiles.display_name, as a POST for
-- clients whose HTTP stack has no PATCH (Java's HttpURLConnection). Empty clears it.
create or replace function public.set_display_name(name text)
returns void
language sql
security invoker
set search_path = ''
as $$
  update public.profiles set display_name = left(nullif(trim(name), ''), 80) where id = (select auth.uid());
$$;

revoke all on function public.set_display_name(text) from public, anon;
grant execute on function public.set_display_name(text) to authenticated;

-- Settings ----------------------------------------------------------------------------
-- One flat JSON object of settings per user, with a timestamp per key. Clients never
-- write the row directly: merge_settings() keeps, per key, whichever value was changed
-- last, so two devices editing at once do not overwrite each other's other settings.

create table public.user_settings (
  user_id    uuid primary key references auth.users (id) on delete cascade,
  settings   jsonb not null default '{}'::jsonb check (jsonb_typeof(settings) = 'object'),
  stamps     jsonb not null default '{}'::jsonb check (jsonb_typeof(stamps) = 'object'),
  updated_at timestamptz not null default now()
);

alter table public.user_settings enable row level security;

create policy "Read own settings" on public.user_settings
  for select to authenticated using ((select auth.uid()) = user_id);
-- Reads only: writes go through merge_settings() below.
revoke all on public.profiles, public.user_settings from anon, authenticated;
grant select on public.user_settings to authenticated;
grant select, update (display_name) on public.profiles to authenticated;

-- merge_settings(patch, patch_stamps): apply the keys in `patch` whose stamp is newer than
-- the stored one and return the merged settings. A JSON null removes the key. A missing
-- or future stamp counts as now, so a device with a fast clock cannot pin a value.
create or replace function public.merge_settings(patch jsonb, patch_stamps jsonb default '{}'::jsonb)
returns table (settings jsonb, stamps jsonb, updated_at timestamptz)
language plpgsql
security definer
set search_path = ''
as $$
declare
  uid     uuid := auth.uid();
  cur_set jsonb;
  cur_st  jsonb;
  k       text;
  v       jsonb;
  s       timestamptz;
begin
  if uid is null then
    raise exception 'Sign in to sync settings' using errcode = '42501';
  end if;
  if patch is null or jsonb_typeof(patch) <> 'object' then
    raise exception 'patch must be a JSON object' using errcode = '22023';
  end if;
  if patch_stamps is null or jsonb_typeof(patch_stamps) <> 'object' then
    raise exception 'patch_stamps must be a JSON object' using errcode = '22023';
  end if;
  if octet_length(patch::text) > 16384 then
    raise exception 'patch is larger than 16 KB' using errcode = '22023';
  end if;

  -- A token can outlive its account (deleted on another device) by up to an hour.
  -- PT401 makes PostgREST answer 401, which the clients treat as signed out.
  if not exists (select 1 from auth.users where id = uid) then
    raise exception 'This account no longer exists' using errcode = 'PT401';
  end if;

  insert into public.user_settings (user_id) values (uid) on conflict (user_id) do nothing;
  select us.settings, us.stamps into cur_set, cur_st
    from public.user_settings us where us.user_id = uid for update;

  for k, v in select * from jsonb_each(patch) loop
    if k !~ '^[A-Za-z][A-Za-z0-9]{0,63}$' then
      raise exception 'invalid setting key: %', k using errcode = '22023';
    end if;
    s := least(coalesce((patch_stamps ->> k)::timestamptz, now()), now());
    if cur_st ->> k is null or s > (cur_st ->> k)::timestamptz then
      cur_set := case when jsonb_typeof(v) = 'null' then cur_set - k else jsonb_set(cur_set, array[k], v) end;
      cur_st  := jsonb_set(cur_st, array[k], to_jsonb(s));
    end if;
  end loop;

  if octet_length(cur_set::text) > 65536 then
    raise exception 'settings are larger than 64 KB' using errcode = '22023';
  end if;

  return query
    update public.user_settings us
       set settings = cur_set, stamps = cur_st, updated_at = now()
     where us.user_id = uid
    returning us.settings, us.stamps, us.updated_at;
end;
$$;

revoke all on function public.merge_settings(jsonb, jsonb) from public, anon;
grant execute on function public.merge_settings(jsonb, jsonb) to authenticated;

-- New users -----------------------------------------------------------------------------
-- A profile and an empty settings row for every new account. The display name comes from
-- the sign-up form (display_name) or the provider (Google and Microsoft send full_name or
-- name; Apple sends a name only on the first sign-in, and only to the app).

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.profiles (id, display_name)
  values (
    new.id,
    left(nullif(trim(coalesce(
      new.raw_user_meta_data ->> 'display_name',
      new.raw_user_meta_data ->> 'full_name',
      new.raw_user_meta_data ->> 'name'
    )), ''), 80)
  )
  on conflict (id) do nothing;
  insert into public.user_settings (user_id) values (new.id) on conflict (user_id) do nothing;
  return new;
end;
$$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

create or replace function public.touch_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

create trigger profiles_touch before update on public.profiles
  for each row execute function public.touch_updated_at();

-- Account deletion ------------------------------------------------------------------------
-- In-app deletion is required by the App Store (guideline 5.1.1(v)) and Google Play.
-- Deleting the auth user cascades to the profile and settings.

create or replace function public.delete_own_account()
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  uid uuid := auth.uid();
begin
  if uid is null then
    raise exception 'Sign in to delete your account' using errcode = '42501';
  end if;
  delete from auth.users where id = uid;
end;
$$;

revoke all on function public.delete_own_account() from public, anon;
grant execute on function public.delete_own_account() to authenticated;

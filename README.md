# Telegram Folder Collector Service

Collects Telegram messages from all chats, channels, and groups resolved from a user account's Telegram folder and stores them in PostgreSQL. The default folder is `MAX`.

This service uses a user MTProto session through Telethon. It does not use the Bot API.

## Configuration

Create `secrets/app.env`:

```env
TG_API_ID=123456
TG_API_HASH=replace-with-api-hash
TG_PHONE=
TG_SESSION_NAME=./sessions/max_collector
FOLDER_NAME=MAX
DB_DSN=postgresql+asyncpg://max_collector:max_collector@localhost:55432/max_collector
COLLECT_COMMENTS=true
DOWNLOAD_MEDIA=false
MEDIA_DIR=./media
SYNC_LIMIT_PER_CHAT=0
FOLDER_REFRESH_SECONDS=300
LOG_LEVEL=INFO
```

`TG_PHONE` is optional. If it is absent, the `login` command prompts for it.

For the Docker app container, `docker-compose.yml` overrides `DB_DSN` to use the Compose service hostname `postgres`.

## Local PostgreSQL

Start PostgreSQL:

```bash
docker compose up -d postgres
```

Apply migrations from the host:

```bash
python -m app.main migrate
```

Or apply them inside the app container:

```bash
docker compose run --rm app python -m app.main migrate
```

## Login

Create the Telethon session:

```bash
python -m app.main login
```

Enter the Telegram code and 2FA password if Telegram asks for them. Protect the generated `.session` file like a password.

The login command prints the delivery type returned by Telegram. `SentCodeTypeApp` means the code was sent to an already authorized Telegram app, not SMS. If Telegram allows it, request SMS delivery with:

```bash
python -m app.main login --force-sms
```

If Telegram does not deliver the code, use QR login instead:

```bash
python -m app.main login-qr
```

Open Telegram on your phone and scan it through Settings -> Devices -> Link Desktop Device.

## Inspect Folders

List folders:

```bash
python -m app.main inspect-folders
```

Inspect one folder:

```bash
python -m app.main inspect-folder --folder MAX
```

## Sync

Run an initial or incremental sync:

```bash
python -m app.main sync --folder MAX
```

For a bounded local test, set `SYNC_LIMIT_PER_CHAT=20` in `secrets/app.env`.

## Daemon

Run the long-lived service:

```bash
python -m app.main run --folder MAX
```

With Docker:

```bash
docker compose up --build app
```

## Comments And Media

Set `COLLECT_COMMENTS=false` to skip discussion comments.

Set `DOWNLOAD_MEDIA=true` to download media into `MEDIA_DIR`. Media download failures are logged and do not block message persistence.

## External PostgreSQL

Point `DB_DSN` to the external database:

```env
DB_DSN=postgresql+asyncpg://user:password@host:5432/database
```

Restrict database access with firewall rules, VPN, or private networking.

## Security Notes

- Do not commit `secrets/`, `.env`, session files, or media.
- Protect the Telethon `.session` file; it grants account access.
- Restrict PostgreSQL access to trusted networks.
- Use a dedicated database user with the minimum required privileges.

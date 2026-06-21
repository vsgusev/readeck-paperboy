# readeck-paperboy
### Send Readeck articles to your Kindle automatically

I love reading articles. I read them on my monitor, on my phone, but neither
feels as good as sinking into a comfy chair with a Kindle. I couldn't find a
tool that would just take the day's saved bookmarks and ship them off to my
Kindle, so I built one. Meet readeck-paperboy.

It's small on purpose — one Python file, two dependencies (`requests` and
`Pillow`), no database. State lives in Readeck's own labels, so you can restart it whenever
and nothing gets lost.

<img width="1904" height="2560" alt="photo_2026-06-21_12-57-15" src="https://github.com/user-attachments/assets/b9f6a5b3-9e38-4031-9fa3-8c6e9aa8b42d" />

## Quick start

```sh
git clone https://github.com/vsgusev/readeck-paperboy
cd readeck-paperboy
cp .env.example .env
docker compose up -d
```

You'll need a Readeck API token, an SMTP account to send from, and your sender
address in Amazon's Send-to-Kindle settings. The `.env.example` walks you
through each one; see [Requirements](#requirements) if you want the short
version first.

## How it works

Every `POLL_INTERVAL`, paperboy asks Readeck for bookmarks tagged `kindle` or
`kindle-{name}` that haven't been sent yet. For each one it grabs the article
as EPUB, does some magic so your Kindle shows a proper name instead of a sad
`article.epub`, and emails it to the matching Send-to-Kindle address. Once it
lands, the queue label gets swapped for `sent-to-{name}`, so nothing ever gets
sent twice.

Almost never, that is. Occasionally the email goes through but the label
update fails — in that case, the next cycle sends that article again. It's
logged, and I deliberately picked "delivered twice" over "silently lost".

## Labels & recipients

Recipients are configured in `DESTINATIONS` as `name:kindle-email` pairs, like
`vlad:vlad_x@kindle.com,anya:anya_y@kindle.com`, with one of them set as
`DEFAULT_DESTINATION`. Then in Readeck:

- Tag **`kindle`** → goes to the default recipient.
- Tag **`kindle-anya`** → goes to `anya`.
- Tag several at once (e.g. `kindle-anya` + `kindle-vlad`) → each recipient gets
  their own copy.
- After delivery the queue label is replaced by **`sent-to-{name}`**.

You add labels the usual way you tag things in Readeck. If you tag right in the
browser extension's label field, saving and queueing happen in the same click.

## Requirements

- A reachable Readeck instance and an **API token** (Readeck user settings →
  API Tokens).
- An **SMTP account** to send from. Use an app password — your regular login
  password won't work for SMTP.
- The sender address added to your Amazon **Approved Personal Document E-mail
  List**. Skip this one and Amazon silently drops your mail; nothing arrives,
  no error, just silence. The `.env.example` has the exact link.

## Configuration

Everything's configured through environment variables, each one documented
inline in [.env.example](.env.example).

Just in case, here are the optional ones:

- **`SMTP_PORT`** (`465`) — `465` is implicit TLS; anything else (e.g. `587`) uses STARTTLS.
- **`POLL_INTERVAL`** (`1h`) — how often to poll Readeck (`30s`, `5m`, `24h`, …).
- **`VERIFY_SSL`** (`true`) — set `false` for a self-signed Readeck cert.
- **`SMTP_FROM`** (= `SMTP_USER`) — sender address, when it differs from the SMTP login.

### Running without Docker

```sh
pip install -r requirements.txt
# export the variables from .env into your environment, then:
python src/main.py
```

## Health

`GET /healthz` returns `200` when the last poll reached Readeck recently, or
`503` if it's been silent for more than two poll intervals. The Docker
healthcheck and `docker-compose.yml` expose it on host port `8090`. To use a
different host port, change the left side of `8090:8080` in `docker-compose.yml`
— leave `HEALTHCHECK_PORT` alone; it's the in-container port the Docker
healthcheck also probes.

One subtlety: health reflects whether Readeck is reachable, not SMTP. A failed
email is logged but doesn't flip health — one flaky SMTP send shouldn't mark
the whole service down.

## License

MIT — see [LICENSE](LICENSE).

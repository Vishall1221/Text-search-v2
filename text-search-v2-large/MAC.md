# Run Text Search V2 Large on your existing Mac catalog

If using this GitHub repository, update the checkout and enter the new subfolder:

```bash
cd "/path/to/Text-search-v2"
git pull --ff-only
cd text-search-v2-large
```

Run the environment/setup commands below from this subfolder. For a downloaded
repository ZIP, enter its `text-search-v2-large` subfolder instead. The Desktop
path used below is only for a separately extracted standalone project folder.

Copy/extract this entire new project to a separate folder named
`Text-Search-v2-Large`. Keep it separate from your existing V1/V2 folders and keep
their configuration files. Do not copy Windows virtual environments, runtime
artifacts or demo calibration to the Mac.

## 1. Environment

```bash
cd "$HOME/Desktop/Text-search-v2/text-search-v2-large"  # adjust to your checkout
# Standalone ZIP instead: cd "$HOME/Desktop/Text-Search-v2-Large"
python3 --version                      # Python 3.11 or newer
python3 -m venv .venv                   # first time only
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## 2. Existing DB and existing BGE

Start the existing PostgreSQL server normally. Reuse the exact database connection
used for the successful `schema_v2_large` import; do not create another database.

```bash
export LARGE_DATABASE_URL='postgresql://YOUR_USER:YOUR_PASSWORD@127.0.0.1:5432/YOUR_DATABASE'
export EMBED_MODEL=bge-small-en-v1.5
export MODELS_DIR="/parent/folder/containing/bge-small-en-v1.5"
export API_PORT=8002
```

Use your real user/password/database; if no password is needed, omit
`:YOUR_PASSWORD`. URL-encode URL-special characters in passwords. Keep credentials
private. Alternatively copy `config/.env.example` to `config/.env` in THIS new
project, edit it locally, and avoid conflicting exported settings.

Example model directory:

```text
/Users/you/Models/
  bge-small-en-v1.5/
    tokenizer.json
    onnx/model.onnx
```

For that layout use `export MODELS_DIR="$HOME/Models"`. A flat model.onnx beside
tokenizer.json is also accepted. Reuse the same matching model that built your
2357 movie embeddings. No download or reimport is necessary for daily startup.

## 3. Start the new app

```bash
python -m app.start
```

Open http://127.0.0.1:8002/. Keep Terminal open. V1 on 8000 and original V2 on 8001
can remain separate. Ctrl+C stops this API, not PostgreSQL.

In another Terminal, readiness can be checked without Python configuration:

```bash
curl -s http://127.0.0.1:8002/api/ready
curl -s -X POST http://127.0.0.1:8002/api/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"feel-good movies","limit":5,"debug":true}'
```

Check `status: ready`, `schema: schema_v2_large`, your expected assets/documents,
and `dense: true`. `dense_calibrated: false` means rejection quality has not yet
been calibrated; it does not mean the connection failed. Do not copy the old
Windows demo cutoff.

Try your actual titles, `Thalapathy movies` when Vijay is present in cast, `Hindi
and Tamil movies`, `family films`, and your own plot descriptions. Unknown actor
aliases are not fabricated. Scene/dialogue requests explain that only metadata
is available. Results carry audio_languages, never primary_language.

## 4. Troubleshooting

Connection errors: verify existing PostgreSQL is running and the connection value
matches the database you imported into. Missing generation: check the selected DB
and that the earlier import printed `activated: true`; do not reimport blindly.
Dense disabled: check `dense_reason`, complete model/tokenizer files, and model
identity. Do not overwrite existing vectors with another model just to clear an
error. Port busy: use a different API_PORT or inspect the intended existing process.

No export is provided in this package. The code was tested on Windows with a
separate synthetic database; the commands have not been executed on your Mac.

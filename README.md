# Syllabus Studio

A Flask app that turns an uploaded syllabus into a multiple-choice practice quiz using OpenRouter or a local Ollama model. Supported uploads are text-based PDF, DOCX, and UTF-8 TXT files (10 MB maximum). The app stores quiz state in a local SQLite database and keeps answer keys on the server.

## Run locally

From this directory:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and set an OpenRouter API key from your OpenRouter account:

```dotenv
OPENROUTER_API_KEY=your_openrouter_api_key
OPENROUTER_MODEL=google/gemma-4-31b-it:free
LLM_PROVIDER=openrouter
FLASK_SECRET_KEY=replace-with-a-long-random-secret
```

Then start the application:

```powershell
flask --app app run --debug
```

Open <http://127.0.0.1:5000>. Never commit `.env`; it is ignored by Git. Use a unique, randomly generated `FLASK_SECRET_KEY` outside local development.

### Choose a model

Set `OPENROUTER_MODEL` to the model ID shown in OpenRouter's model catalog, then restart Flask. For example, `google/gemma-4-31b-it:free` is a currently listed free model. Free-model availability and rate limits can change; check OpenRouter's [free models](https://openrouter.ai/collections/free-models) page for current choices.

Meta Llama 3.1 8B is currently listed as `meta-llama/llama-3.1-8b-instruct` **without a `:free` variant** in OpenRouter's live catalog, so requests to it may incur usage charges. If you choose it, set:

```dotenv
OPENROUTER_MODEL=meta-llama/llama-3.1-8b-instruct
```

### Use Llama 3.1 locally with Ollama

Install Ollama from <https://ollama.com/download>, then in PowerShell download the 8B model:

```powershell
ollama pull llama3.1:8b
ollama run llama3.1:8b
```

The first command downloads the model; the second lets you test it interactively. Exit the interactive prompt with `/bye`. Ollama normally runs its local API at `http://localhost:11434`; leave it running while you use the quiz app.

In this app's `.env`, select Ollama:

```dotenv
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.1:8b
```

With Ollama selected, `OPENROUTER_API_KEY` is not used and can stay empty. Restart Flask after editing `.env`. The uploaded syllabus is sent to your local Ollama server rather than OpenRouter. The Llama 3.1 8B model needs several gigabytes of memory; generation may be slower on CPU-only machines.

## Tests

```powershell
python -m unittest discover -s tests -v
```

## Source-grounding safeguards

The generation prompt limits question content to the uploaded syllabus. Each generated question must include an exact, contiguous source excerpt of at least six words; the app verifies that excerpt against the extracted document and rejects the entire quiz if the requested question count or evidence checks fail. The excerpt and explanation are shown during answer review. This evidence check makes unsupported output less likely, but—as with any generative model—does not mathematically prove that every interpretation is correct; review the supporting excerpt when studying.

The document is not saved after extraction. Quiz questions, answer keys, and submitted answers are stored in `instance/quizzes.sqlite3` on the server.

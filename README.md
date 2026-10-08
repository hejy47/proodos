# Proodos

Proodos performs evidence-guided fault localization and automated repair for Java projects. It analyzes the project, selects a failing test, proposes a production-code change, validates it, and writes a standard unified diff patch.

## Setup

Run these commands in the project directory (or inside the `defects4j-trans` container):

```bash
uv sync

mvn -q -f test_runner/pom.xml -DskipTests clean package
mvn -q -f trace_agent/pom.xml -DskipTests clean package
```

Create a `.env` file in the Proodos directory. It is loaded automatically using `python-dotenv`; existing environment variables take priority. OpenAI is the default:

```dotenv
OPENAI_API_KEY=your_api_key
```

DeepSeek can be selected with `LLM_PROVIDER=deepseek`, `DEEPSEEK_API_KEY`, and optionally `DEEPSEEK_MODEL` or `DEEPSEEK_BASE_URL`.

## Run

For a Defects4J-Trans project, run the full pipeline with:

```bash
docker exec \
  -e PYTHONHASHSEED=0 \
  -w /code/proodos \
  defects4j-trans \
  .venv/bin/python main.py \
  --dataset defects4j \
  --project_path /datasets/defects4j-trans/closure-1 \
  --project_id Closure \
  --bug_id 1 \
  --result_dir /code/proodos/output/defects4j-trans \
  --stage all
```

To focus diagnosis on one failing test, add for example:

```bash
--test_case_id com.google.javascript.jscomp.RemoveUnusedVarsTest::testRemoveGlobal1
```

The public stages are:

- `preprocess`: build the static fault context.
- `debug`: diagnose, generate, compile, and test repairs.
- `all`: run both stages.

## Results

For the command above, the accepted patch is written to:

```text
/code/proodos/output/defects4j-trans/results/defects4j/closure-1.patch
```

The patch uses standard `--- a/...` and `+++ b/...` unified-diff format. Logs, preprocessing data, repair history, and token usage are stored under the same `result_dir`.

Use `python main.py --help` to see all options.

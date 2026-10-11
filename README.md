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

Java call candidates use receiver types, lexical variable scopes, imports, and
project inheritance. Constructors are resolved within their target class.
Unresolved calls and JDK/third-party targets do not create project-wide
same-name matches; virtual calls may retain several related implementations.

## Results

For the command above, the accepted patch is written to:

```text
/code/proodos/output/defects4j-trans/results/defects4j/closure-1.patch
```

The patch uses standard `--- a/...` and `+++ b/...` unified-diff format. Logs, preprocessing data, repair history, and token usage are stored under the same `result_dir`.

Optional repair budgets can be set through environment variables:

```dotenv
PROODOS_MAX_PATCH_ATTEMPTS=5
PROODOS_MAX_DIAGNOSIS_ROUNDS=3
PROODOS_MAX_REVIEW_ATTEMPTS=2
PROODOS_MAX_REPAIR_ROUNDS=5
PROODOS_TIME_BUDGET_SECONDS=3600
PROODOS_TEST_TIMEOUT_SECONDS=300
```

Each test command has a 300-second timeout by default, including full regression,
selected-test validation, test discovery, and tracing. The limit applies to the
whole command, not each test in a suite. On timeout, Proodos terminates the process
group and reports an execution error; partial results cannot count as a passing run.

The patch agent can query Java repair ingredients on demand: `list_accessible_variables`
returns variables and scopes, `list_callable_methods` returns project method signatures
and comments, and `read_code` reads indexed source. These tools use the preprocessing
SQLite index and return Markdown. JDK and third-party methods are outside the index.
After a patch is accepted, only its changed Java file is reindexed before repair continues.

After fault localization, code automatically assembles repair context from the existing
index and failure report, with no LLM calls or preprocessing changes. It has three sections:
`Test Code` (the complete selected test with its failure location marked),
`Test Failure Message` (the original failure report), and `Dependency Chain`
(call candidates and related source, including assertions inside test helpers).
Failure reports and supplementary source are bounded; the selected test is kept complete.
The patch agent uses this context to derive expected behavior before generating a patch
and records a concise evidence summary in its existing log.

Use `python main.py --help` to see all options.

# Architecture

## Overview

`llm-cost-guard` runs as a Docker-based GitHub Action on every pull request. It walks the changed Python files in the repository, uses Python's `ast` module to find AWS Bedrock and Azure OpenAI SDK call sites, prices each site against a bundled YAML pricing table, and writes a structured Markdown cost report as a PR comment. The tool sits at the point in the development workflow where a human reviewer is already looking at the code — making cost visibility a natural part of the review conversation rather than a separate audit step.

---

## Component map

```
action.yml          (input contract — defines the 6 inputs and cost-report output)
    │
    └─► main.py     (entrypoint — reads env vars, orchestrates the pipeline, writes outputs)
            │
            ├─► parser.py       (AST scanner — returns list[LLMCall])
            │
            ├─► estimator.py    (cost engine — returns EstimationResult)
            │       │
            │       └─► config/pricing.yml   (data — input/output rates per model)
            │
            └─► reporter.py     (formatter — returns Markdown string)
```

Data flows in one direction: `main.py` calls each module in sequence and passes the output of each as the input to the next. No module imports from another module except upward through this chain (`estimator` imports `LLMCall` from `parser`; `reporter` imports `EstimationResult` from `estimator`).

---

## Key design decisions

### AST parsing over regex

The parser uses Python's `ast.parse` to walk the syntax tree rather than scanning source text with regular expressions.

**Why:** Regex cannot reliably distinguish a method call from a comment, a string literal, or a multi-line expression split across logical continuation lines. An AST node for a `Call` expression is unambiguous regardless of how the source is formatted. The visitor pattern also makes it straightforward to traverse nested structures — for example, checking that a `.create()` call belongs to a `.chat.completions.` chain — without maintaining fragile regex state. The tradeoff is that files with syntax errors are silently skipped rather than partially matched; this is acceptable because a file that cannot be parsed cannot be executed either.

### `pricing.yml` as external config

Model pricing lives in `config/pricing.yml`, not hardcoded in Python.

**Why:** Cloud provider pricing changes on its own schedule, independent of any code change in this tool. Separating pricing data from logic means a rate update is a one-line YAML PR with no Python touched, no tests to re-run, and a clear diff for reviewers to verify against the source URL. It also allows organisations with enterprise pricing agreements to fork or override the file with their negotiated rates without modifying any source code.

### GitHub Action over VS Code extension

The tool is delivered as a GitHub Action running in a Docker container, not as a local IDE extension.

**Why:** An IDE extension requires individual installation by every developer. A GitHub Action is configured once at the org or repo level and enforced for all contributors regardless of their local tooling. This aligns with enterprise security postures where policy is applied centrally and cannot be opted out of by an individual. The Docker container also pins the Python version and dependencies, eliminating "works on my machine" variability in the scan results.

### Fail-on-threshold is opt-in

The `fail-on-threshold` input defaults to `false`. The action will report a threshold breach but will not block the PR unless the user explicitly enables it.

**Why:** Blocking PRs by default — before a team has calibrated their threshold against real traffic data — produces immediate friction and abandonment. The intended adoption path is: enable the action with reporting only, observe a few PRs to understand what realistic cost estimates look like for the codebase, then set a threshold and enable enforcement once there is organisational buy-in. Visibility first, enforcement optional.

---

## Known estimation boundaries

**Input token counts are structurally unresolvable at parse time.** The AST scanner can read literal string arguments, but real prompts are almost always assembled at runtime from variables, f-strings, database content, or conversation history. The `default-input-tokens` value is therefore a fixed assumption applied to every call site. There is no mechanism to improve this without runtime instrumentation.

**Output token extraction is a ceiling, not an expectation.** When `max_tokens` is present as a direct keyword argument, the parser extracts it. However, `max_tokens` is a hard ceiling — many calls complete well below it. A call with `max_tokens=4096` set as a safety limit but which typically returns 80 tokens will be priced at 4096. The report over-estimates output cost for these patterns.

**Streaming calls cannot report actual output token counts.** `invoke_model_with_response_stream` calls are detected and flagged as streaming in the `LLMCall` dataclass, but the number of tokens in a streamed response is determined incrementally at runtime based on the model's output and any user-side stopping logic. The same `max_tokens` extraction logic is applied, with the same ceiling caveat above.

**Nested token arguments are invisible to the parser.** AWS Bedrock's `invoke_model` takes a serialised JSON body as its `body` argument. Token limits specified inside that payload — such as `{"max_tokens": 1000}` inside the JSON string — are not extracted because they exist as string content, not as AST keyword nodes. Only top-level keyword arguments (`max_tokens=1000`) are parsed.

**Batch API calls are not detected.** AWS Bedrock Batch Inference uses a separate `create_model_invocation_job` API call, not `invoke_model`. Azure OpenAI batch deployments use a different endpoint structure. Neither is currently matched by the parser, so batch workloads produce no cost estimate at all rather than an incorrect one.

**Prompt cache hits reduce effective input cost, but we cannot predict hit rate.** AWS Bedrock Prompt Caching charges ~10% of the standard input rate for cache read tokens. Azure OpenAI charges 50%. Both require the prompt prefix to exceed a minimum length and be reused within the cache TTL. Whether a given call site will benefit from caching depends on runtime behaviour, not static structure. The estimator always charges the full input rate.

---

## Pricing update process

1. Check the current rates at [AWS Bedrock pricing](https://aws.amazon.com/bedrock/pricing/) and [Azure OpenAI pricing](https://azure.microsoft.com/en-us/pricing/details/cognitive-services/openai-service/).
2. Open `config/pricing.yml` and update the `input_cost_per_1k_tokens` and `output_cost_per_1k_tokens` values for the affected models.
3. Update the `# Pricing last verified:` date comment at the top of the file to today's date.
4. Open a PR. Include a link to the provider pricing page in the PR description so the change can be verified by a reviewer without leaving GitHub.
5. Merge. No code changes, no dependency updates, no Docker rebuild required — the updated YAML is read at action runtime.

To add a new model, add a new entry under the appropriate provider key (`aws_bedrock` or `azure_openai`) following the existing structure, then add a corresponding detection pattern in `parser.py` if the model uses a different SDK call shape.

---

## v2 considerations

### Vertex AI (Google) support

Adding Vertex AI detection requires two things in `parser.py`: recognising `vertexai.generative_models.GenerativeModel` instantiation and its `.generate_content()` call, and recognising `google.cloud.aiplatform` prediction client calls. The model ID extraction is more complex than Bedrock or Azure because Vertex AI model identifiers can be full resource paths (`projects/*/locations/*/publishers/google/models/gemini-1.5-pro`) or short aliases (`gemini-1.5-pro`). A normalisation step in the parser would be needed to map both forms to a canonical pricing key. Google also has separate pricing tiers for online prediction vs batch prediction vs provisioned throughput, mirroring the same structural complexity as the existing providers.

### Batch API detection is harder than standard detection

Standard API detection works because `invoke_model`, `converse`, and `chat.completions.create` are leaf calls — they appear directly in application code at the point of use. Batch API calls are submit/poll patterns: a job is created in one call, and results are retrieved asynchronously in another. The cost is incurred when the job runs, not when the submission call is made. Detecting the submission call is straightforward (AWS uses `create_model_invocation_job`; Azure uses a batches endpoint), but pricing it correctly requires knowing the input manifest size, which is a runtime artifact (typically a JSONL file). A v2 batch detector would likely report a warning ("batch job submission detected — cost cannot be estimated statically") rather than a dollar figure.

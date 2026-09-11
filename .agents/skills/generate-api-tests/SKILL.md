---
name: generate-api-tests
description: "Turn a Swagger 2 or OpenAPI 3 URL and plain-language API intent into schema-grounded positive, negative, boundary, and integration YAML scenarios. Use for API contract testing, operation sequences, response validation, or synthetic request generation without unconditional code generation."
---

# Generate API Tests

Use the live schema document as the source of truth. The tester supplies a Swagger/OpenAPI URL and intent; handle document download, version differences, operation analysis, test data design, and YAML generation.

Accept plain-language requests. Require only a schema URL (or a previously returned 64-character
`schemaId`) and testing intent. Ask for environment-variable names only when authentication or a
private endpoint requires them; never ask a non-technical tester to design request code, models,
builders, flows, enums, or dispatcher cases.

## Required workflow

1. Create a small schema-inspection scenario containing `loadApiSchema` with `schemaUrl` expressed through an environment variable when appropriate. Put schema-repository authentication in `headers`; never reuse operation credentials for schema download.
2. Show `plantain validate`, explain it, and wait for approval.
3. Show the schema execution command, explain that it performs a read-only HTTP GET, and wait for approval.
4. Read the content-addressed cached schema returned by the activity. Do not derive the API base URL from the schema-document URL.
5. Resolve Swagger 2 using `schemes`, `host`, and `basePath`; resolve OpenAPI 3 using `servers` and server-variable defaults. The runtime performs this resolution.
6. Analyze each in-scope operation's path/query/header/body requirements, serialization and media
   types, local references, formats, enums, constraints, security, and documented responses.
7. Generate independent YAML scenarios under `scenarios/generated/api/<api-name>/`.
8. Validate all files. Do not execute state-changing operations without describing their effects and receiving explicit approval.

Before every terminal command, present its exact text, explain what it does and why it is needed
at that stage, then wait for explicit approval. Never read `.env`; credentials remain
`env:VARIABLE_NAME` references in YAML.

External `$ref` documents are blocked by default. If the schema requires one, stop and explain which reference needs a separately approved, allowlisted retrieval design.

Cookie or form-data parameters, response-header contracts, and serialization styles the runtime
cannot faithfully encode also fail closed. Surface the unsupported contract feature before
generation; never silently disable validation to bypass it.

## Coverage model

Generate only cases supported by the schema or explicit requirements:

- one minimal valid request per operation;
- representative fully populated valid requests;
- required-field omission;
- type, format, enum, pattern, length, numeric, and collection boundaries;
- invalid and unknown path/query values when an error response is documented;
- authentication absent/invalid cases when security requirements and statuses are documented;
- response-schema validation for every expected status;
- operation chains using `${step.responseBody...}` when outputs create inputs for later calls;
- pairwise combinations for independent optional inputs rather than a Cartesian product.

For an intentionally invalid request, set `validateRequest: false`; otherwise the framework correctly blocks it before transmission. Keep `validateResponse: true` for ordinary positive and negative HTTP responses whenever the expected response has a usable schema.

When the test intent is specifically to prove that observed response data violates its declared
schema, set `callSchema.validateResponse: false` and immediately chain the same response into
`validateSchema` with `expectedValid: false`. Never weaken validation silently just to make a run
pass. An expected-invalid assertion must fail if the service later returns conforming data.
When the selected response declares multiple media schemas, include `contentType` with the actual
response media type; a single unambiguous media schema needs no extra YAML.

## YAML pattern

```yaml
scenario: "Petstore - get inventory with API key"
tags:
  - api
  - schema
  - positive

steps:
  - loadApiSchema:
      id: petstore_schema
      schemaUrl: "env:PETSTORE_SCHEMA_URL"

  - callSchema:
      id: get_inventory
      schemaId: "${petstore_schema.schemaId}"
      operationId: getInventory
      headers:
        Accept: application/json
        api_key: "env:PETSTORE_API_KEY"
      expectedStatus: 200
      validateRequest: true
      validateResponse: true

outputs:
  statusCode: "${get_inventory.statusCode}"
  responseBytes: "${get_inventory.responseBytes}"
```

The runtime supports a root Swagger URL such as `https://petstore.swagger.io/v2/swagger.json`; no proprietary URL-path convention or build-time code generation is required.

`loadApiSchema.headers` and `callSchema.schemaHeaders` apply only to schema download.
`callSchema.headers` applies only to the selected operation. Custom schema headers force a fresh,
bounded download; their values and hashes are never persisted. `sendRequest` remains the separate
schema-independent activity and should not be replaced by `callSchema`.

## Response assertions and diagnostics

Use `expectedMessage` on either `sendRequest` or `callSchema` only when the schema, explicit
requirement, or tester intent establishes a response substring. Matching is case-insensitive and
uses the complete decoded response; never invent expected server wording. Status, method, byte
count, and a sanitized response preview are logged before status, message, or schema assertions,
so a safe server diagnostic remains available after failure.

Response previews recursively apply built-in sensitive-key rules, `sensitive-keys.txt` fragments,
scenario-observed values, and opaque-token masking, then stop at 8 KiB. Use
`logResponseBody: false` when response content should not be persisted even after sanitization.
This affects logging only: the complete raw response remains isolated to the current scenario for
`${step.responseBody...}` chaining and is never shared across concurrent scenarios.

## Test-data and security rules

- Never place API keys, tokens, or credentials in generated files. Use `env:` references.
- Never log authorization headers or sensitive response fields.
- Petstore's documented `special-key` is public example data and belongs in `.env.example`; YAML
  must still reference it as `env:PETSTORE_API_KEY` to demonstrate the production-safe pattern.
- Reuse schema examples only when they satisfy the current operation constraints.
- Prefer `random:` values with collision-resistant prefixes for resources created in shared systems.
- Add cleanup only when the user authorizes the corresponding state-changing API operation.
- Do not generate a request for an undocumented negative expectation and pretend its status is known; record the gap instead.

Plantain interprets Swagger/OpenAPI contracts directly at runtime. Do not install or invoke a
model generator, endpoint builder, or generated-client workflow for scenario execution.

The framework reports assertion failures with value-free `http_status` and `failure_stage`
telemetry; schema failures additionally provide `schema_path` and `schema_rule`. Use the bounded,
sanitized response preview and these fields for diagnosis. Never echo an unsanitized response or
expected secret into generated YAML, logs, exceptions, or the user-facing summary.

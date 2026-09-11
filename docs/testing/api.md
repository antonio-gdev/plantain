# API testing

Plantain turns an API outcome into a bounded, reviewable test. It can work from a
known HTTP request or discover supported behavior from Swagger 2 and OpenAPI 3
metadata.

You describe the contract or business result. Plantain handles operation
selection, request construction, response checks, and safe value chaining.

## What you provide

For schema-grounded testing, provide:

- an approved schema URL or environment reference;
- the operation or outcome you care about;
- required authentication environment names;
- meaningful positive, negative, boundary, or sequence expectations.

For a direct HTTP check, provide:

- the approved target or base URL;
- the intended request behavior;
- the expected status and important response evidence;
- required environment-reference names.

For example:

> Using `PETSTORE_SCHEMA_URL` and `PETSTORE_API_KEY`, verify that the operation for
> finding available pets succeeds and that every returned item matches the
> documented pet response.

You do not need to identify internal activity names or construct the request body
yourself.

## Choose the outcome, not the mechanism

### Contract confidence

Ask Plantain to verify that a supported operation returns a response matching the
documented schema.

### Negative behavior

Describe the invalid or unauthorized condition and the expected safe response.
Plantain derives only values supported by the contract and policy.

### Boundary behavior

State the meaningful edge—for example, an empty collection, documented limit, or
required field boundary.

### Operation sequence

Describe the business sequence. Plantain can pass a bounded value from one step to
another within the same isolated scenario.

## What Plantain does

For an OpenAPI workflow, Plantain:

1. loads the approved schema within configured size and network limits;
2. identifies the exact supported operation;
3. resolves parameters and request structure from the contract;
4. creates a bounded request using environment references for credentials;
5. validates status, selected response evidence, and schema behavior;
6. retains sanitized outputs and result evidence.

For a direct HTTP workflow, Plantain applies the same redirect, timeout, response
size, cookie, and redaction boundaries without schema discovery.

## What you see

During creation, the agent workspace identifies schema inspection and test
authoring as separate stages. If the contract is ambiguous, Plantain asks for a
decision instead of selecting an operation by guesswork.

After execution, the result can show:

- pass or failure status;
- the safe request purpose and operation identity;
- response status and bounded sanitized evidence;
- schema-validation outcome;
- declared outputs used by later steps;
- focused failure details that do not expose raw protected payloads.

## What to review

Confirm that:

- the selected operation matches the intended business behavior;
- the target environment is approved;
- authentication is referenced from the environment;
- required parameters and boundaries are represented;
- the response checks prove more than a successful status when contract confidence
  is the goal;
- generated data is synthetic or explicitly approved;
- an operation sequence passes only the values that later steps need.

## Supported schema behavior

Plantain supports Swagger 2 and OpenAPI 3 documents within its explicit contract
surface. External references, unsupported schema features, ambiguous media types,
or unsafe targets fail closed rather than being approximated.

A contract that loads successfully is not automatically a useful test. Your review
still determines whether the selected behavior answers the original intent.

## When Plantain pauses

Plantain stops for clarification or correction when:

- the schema or target cannot be accessed under network policy;
- the operation cannot be resolved exactly;
- the contract requires unsupported behavior;
- authentication context is missing;
- an external schema reference is blocked;
- a redirect crosses an unsafe boundary;
- the request or response exceeds a configured limit;
- a required test value cannot be derived safely.

Do not bypass these boundaries with literal credentials or invented request
fields. Correct the approved context or refine the intended operation.

## API isolation and privacy

Each scenario receives its own HTTP cookies, values, and secret registry. Redirects
are revalidated, credentials are stripped across origins, and HTTPS-to-HTTP
downgrades are blocked.

Detailed evidence is bounded and sanitized before retention. Compact terminal
telemetry does not carry raw bodies.

Next, learn about [cross-system tests](cross-system.md),
[running tests](run-tests.md), or [reviewing results](results.md).

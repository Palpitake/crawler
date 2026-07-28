# Authentication architecture

Authentication is a standalone domain subsystem under crawler_agent.auth.
Browser tools collect facts, the auth package makes the decision, and
Supervisor schedules the next capability. This prevents model wording,
historical checkpoints, or a manual confirmation from becoming authentication
success by implication.

## Components

- models.py defines the state vocabulary and serialized compatibility facts.
- contracts.py builds a task-scoped verification contract and enforces the
  exact-host and explicit-IdP trust boundary.
- evidence.py translates Browser probes and same-auth-epoch observations into
  normalized evidence.
- decision.py is the deterministic reducer. It is the only component allowed
  to emit verified.
- service.py is the shared Browser and Supervisor facade.
- sessions.py scopes saved state and manages expiry, rejection, recovery, and
  quarantine metadata without persisting credential values.

crawler_agent.core.runtime_facts.normalize_auth_facts remains only as a
compatibility facade.

## Verification contract

Every Browser task creates a contract containing:

- exact target host and normalized target route;
- requested fields;
- explicitly allowed authentication domains;
- minimum substantive body size;
- whether current target evidence is mandatory.

The final business resource must be on the exact target host. SSO domains are
allowed only as intermediate authentication locations and must be configured
explicitly. A leading wildcard rule is supported when an operator intentionally
trusts every subdomain.

    BROWSER_AUTH_ALLOWED_DOMAINS=login.example-idp.com,*.trusted-idp.net

## State reduction

The reducer applies facts in this order:

1. Challenge evidence produces challenge.
2. A login gate produces required; if a saved or previously verified session
   hits the gate, it produces stale.
3. An untrusted authentication location or redirect produces rejected.
4. A manual or saved-session context becomes verified only when the target
   host and route are clear and current target evidence satisfies the contract.
5. A manual confirmation or loaded state without target evidence remains
   provisional.
6. Positive model claims without host evidence are ignored. Conservative
   required or challenge claims are retained so the host can probe them.

Canonical output includes state, authentication_state, authenticated,
verification_state, contract_satisfied, reason_codes, and
source=auth_decision_engine.

## Strategies

Saved state and interactive login are different strategies:

    saved state
      -> load scoped state
      -> open exact target
      -> auth probe
      -> verified | provisional | stale | rejected

    explicit login gate/challenge
      -> visible browser
      -> user confirmation
      -> save state provisionally
      -> reopen exact target headlessly
      -> auth probe
      -> verified | provisional | challenge | rejected

There is no unattended login-completion regex branch. Human confirmation is
required for the interactive strategy, and confirmation remains provisional
until the target contract passes.

## Session lifecycle

Session names contain the exact host, BROWSER_ACCOUNT_ALIAS, and a fingerprint
of engine, profile, locale, timezone, and proxy region. This prevents tenant,
account, or environment reuse.

Metadata lives beside storage state under browser_auth_states/.metadata. Only
counts and lifecycle facts are recorded; cookie, token, and storage values are
excluded.

- verified resets failures and activates the session.
- provisional, required, and challenge increment the inconclusive count; the
  default second failure quarantines the session.
- stale and rejected stop automatic loading immediately.
- expiry or an environment-fingerprint mismatch blocks loading.
- clearing an auth state also clears its lifecycle metadata.

Configuration:

    BROWSER_ACCOUNT_ALIAS=default
    BROWSER_AUTH_METADATA_TTL_DAYS=30
    BROWSER_AUTH_QUARANTINE_FAILURES=2
    BROWSER_AUTH_MIN_BODY_CHARS=300

## Evidence isolation

Authentication evidence is generation-scoped. Checkpoint aggregates and
pre-login item ledgers cannot satisfy a post-login contract. API evidence must
be explicitly marked as observed after authentication or carry the current
auth epoch. The host performs a final auth probe for claimed authentication
states and overwrites any model-supplied probe payload.

## Extending authentication

Add new site or IdP behavior as evidence adapters or explicit trust
configuration. Do not add site-specific success branches to Browser or
Supervisor. Any new state transition belongs in the reducer and requires an
offline test in tests/test_auth_domain.py; lifecycle behavior belongs in
tests/test_auth_sessions.py.

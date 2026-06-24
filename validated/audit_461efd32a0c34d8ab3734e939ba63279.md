Audit Report

## Title
Predictable `RuleId` UUID Generation via Time-Only PRNG Seed Enables Enumeration of Undisclosed Rate-Limit Rules — (File: `rs/boundary_node/rate_limits/canister/random.rs`)

## Summary
The rate-limit canister seeds its ChaCha20 PRNG using only `ic_cdk::api::time()` for 8 bytes and a constant `0x42` for the remaining 24 bytes. Because the IC consensus block time is publicly observable, the full PRNG state is reconstructible by any observer. The `get_rule_by_id` query endpoint is reachable by any unauthenticated caller (non-replicated queries bypass `inspect_message`), and for undisclosed rules it returns `Ok` with `incident_id` and version metadata visible. An attacker can predict all `RuleId` UUIDs and probe `get_rule_by_id` to enumerate undisclosed rules and their incident linkage before official disclosure.

## Finding Description

**Root cause — weak PRNG seed:**

In `rs/boundary_node/rate_limits/canister/random.rs` (lines 8–14), the `thread_local!` RNG is lazily initialized on first access with:

```rust
thread_local! {
  static RNG: RefCell<ChaCha20Rng> = {
    let mut seed = [42; 32];
    seed[..8].copy_from_slice(&time().to_le_bytes());
    RefCell::new(ChaCha20Rng::from_seed(seed))
  };
}
``` [1](#0-0) 

The first access occurs during the first `generate_random_uuid()` call in `add_config.rs` (line 115), which is triggered by the first `add_config` update call that introduces a new rule. The `time()` value at that moment is the IC consensus block time — deterministic and publicly observable via `read_state`. The remaining 24 bytes are the constant `0x42`. The entire 32-byte seed is therefore reconstructible by any observer who knows the block time of the first `add_config` call. [2](#0-1) 

**UUID generation path:**

`generate_random_uuid()` calls `getrandom::getrandom`, which is routed to `custom_getrandom_bytes_impl` via the registered custom handler, drawing 16 bytes from the seeded RNG. [3](#0-2) [4](#0-3) 

**`get_rule_by_id` is reachable by any caller:**

`get_rule_by_id` is declared as a `#[query]` method. The `inspect_message` hook only applies to ingress messages (update calls and replicated queries); non-replicated queries bypass it entirely. The `inspect_message` handler rejects all methods not in its explicit allowlist, but this does not apply to non-replicated query calls to `get_rule_by_id`. [5](#0-4) [6](#0-5) 

**Information leaked for undisclosed rules:**

`RuleGetter::get()` returns `Ok` for any caller, even for undisclosed rules. For unauthorized callers, `RuleConfidentialityFormatter::format()` only redacts `rule_raw` and `description` when `disclosed_at.is_none()`, but leaves `incident_id`, `added_in_version`, and `removed_in_version` fully visible. [7](#0-6) [8](#0-7) 

A non-existent rule returns `GetEntityError::NotFound` (an error), while an existing undisclosed rule returns `Ok` with `incident_id` visible. This distinction is the oracle that confirms rule existence. [9](#0-8) 

**Existing guards are insufficient:**

The `inspect_message` guard does not cover non-replicated queries. The `RuleConfidentialityFormatter` redacts content but not existence or incident linkage. There is no rate-limiting or authentication on `get_rule_by_id` query calls.

## Impact Explanation

The rate-limit canister's stated security model is that undisclosed rules — their `rule_raw` policy and `description` — remain confidential until explicitly disclosed. The `RuleId` is the sole access key to probe for a specific rule. Because `RuleId` values are fully predictable from publicly observable on-chain data, an unprivileged attacker can enumerate all undisclosed rules and learn their `incident_id` and version metadata before official disclosure. This breaks the confidentiality model of a boundary node security component, constituting a significant boundary/API security impact with concrete harm: security incidents and their associated rate-limit rules are meant to be secret until disclosed, but their existence and incident linkage become enumerable by any external party. This matches the **High** impact category: "Significant boundary/API security impact with concrete user or protocol harm."

## Likelihood Explanation

The attack requires no privileges. The IC consensus block time is publicly observable via `read_state`. The number of new rules added per `add_config` call is observable on-chain (each is an update call). The attacker reconstructs the seed, advances the ChaCha20 stream by 16 bytes per prior UUID, and calls `get_rule_by_id` for each predicted UUID. The only cost is the number of query calls, bounded by the number of `add_config` calls (small in practice). The attack is fully deterministic and repeatable.

## Recommendation

Replace the time-seeded RNG with a seed derived from `ic00::raw_rand`, which provides cryptographically unpredictable randomness from the subnet's threshold BLS randomness beacon. Since `raw_rand` is asynchronous, call it during `init`/`post_upgrade` and store the resulting seed before any UUID generation occurs, following the pattern used in `rs/nns/governance/src/timer_tasks/seeding.rs`. At minimum, XOR the `raw_rand` output with `time()` so that even if one source is predictable, the combined seed is not. [1](#0-0) 

## Proof of Concept

```
1. Observe the IC block time T (nanoseconds) of the first add_config update call
   that introduces a new rule (observable via read_state or a block explorer).
2. Construct seed: seed[0..8] = T.to_le_bytes(); seed[8..32] = [0x42u8; 24].
3. Initialize ChaCha20Rng::from_seed(seed).
4. For each new rule added across all prior add_config calls (count N, observable on-chain),
   advance the RNG by consuming 16 bytes per UUID to reach the target position.
5. Draw 16 bytes from the RNG to predict the next RuleId UUID.
6. Call get_rule_by_id(predicted_uuid) as a non-replicated query (no authentication required).
   - Ok response with incident_id field populated → undisclosed rule confirmed to exist.
   - Err(NotFound) → UUID not yet used; advance and retry.
7. Repeat to enumerate all undisclosed rules and their incident_id linkages.
```

### Citations

**File:** rs/boundary_node/rate_limits/canister/random.rs (L8-14)
```rust
thread_local! {
  static RNG: RefCell<ChaCha20Rng> = {
    let mut seed = [42; 32];
    seed[..8].copy_from_slice(&time().to_le_bytes());
    RefCell::new(ChaCha20Rng::from_seed(seed))
  };
}
```

**File:** rs/boundary_node/rate_limits/canister/random.rs (L22-29)
```rust
pub fn custom_getrandom_bytes_impl(dest: &mut [u8]) -> Result<(), getrandom::Error> {
    RNG.with(|rng| {
        let mut rng = rng.borrow_mut();
        rng.fill_bytes(dest);
    });

    Ok(())
}
```

**File:** rs/boundary_node/rate_limits/canister/add_config.rs (L115-115)
```rust
                let rule_id = RuleId(generate_random_uuid()?);
```

**File:** rs/boundary_node/rate_limits/canister/add_config.rs (L186-193)
```rust
fn generate_random_uuid() -> Result<Uuid, anyhow::Error> {
    let mut buf = [0_u8; 16];
    getrandom::getrandom(&mut buf)
        .map_err(|e| anyhow::anyhow!(e))
        .context("Failed to generate random bytes")?;
    let uuid = Uuid::from_slice(&buf).context("Failed to create UUID from bytes")?;
    Ok(uuid)
}
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L34-68)
```rust
#[inspect_message]
fn inspect_message() {
    // In order for this hook to succeed, accept_message() must be invoked.
    let caller_id: Principal = ic_cdk::api::caller();
    let called_method = ic_cdk::api::call::method_name();

    let (has_full_access, has_full_read_access) = with_canister_state(|state| {
        let authorized_principal = state.get_authorized_principal();
        (
            Some(caller_id) == authorized_principal,
            state.is_api_boundary_node_principal(&caller_id),
        )
    });

    if called_method == REPLICATED_QUERY_METHOD {
        if has_full_access || has_full_read_access {
            ic_cdk::api::call::accept_message();
        } else {
            ic_cdk::api::trap(
                "message_inspection_failed: method call is prohibited in the current context",
            );
        }
    } else if UPDATE_METHODS.contains(&called_method.as_str()) {
        if has_full_access {
            ic_cdk::api::call::accept_message();
        } else {
            ic_cdk::api::trap("message_inspection_failed: unauthorized caller");
        }
    } else {
        // All others calls are rejected
        ic_cdk::api::trap(
            "message_inspection_failed: method call is prohibited in the current context",
        );
    }
}
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L123-133)
```rust
#[query]
fn get_rule_by_id(rule_id: RuleId) -> GetRuleByIdResponse {
    let caller_id = ic_cdk::api::caller();
    let response = with_canister_state(|state| {
        let access_resolver = AccessLevelResolver::new(caller_id, state.clone());
        let formatter = RuleConfidentialityFormatter;
        let getter = RuleGetter::new(state, formatter, access_resolver);
        getter.get(&rule_id)
    })?;
    Ok(response)
}
```

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L215-246)
```rust
    fn get(&self, rule_id: &Self::Input) -> Result<Self::Output, Self::Error> {
        let rule_id = RuleId::try_from(rule_id.clone())
            .map_err(|_| GetEntityError::InvalidUuidFormat(rule_id.clone()))?;

        let stored_rule = self
            .canister_api
            .get_rule(&rule_id)
            .ok_or_else(|| GetEntityError::NotFound(rule_id.0.to_string()))?;

        let output_rule = OutputRuleMetadata {
            id: rule_id,
            incident_id: stored_rule.incident_id,
            rule_raw: Some(stored_rule.rule_raw),
            description: Some(stored_rule.description),
            disclosed_at: stored_rule.disclosed_at,
            added_in_version: stored_rule.added_in_version,
            removed_in_version: stored_rule.removed_in_version,
        };

        let is_authorized_viewer = self.access_resolver.get_access_level()
            == AccessLevel::FullAccess
            || self.access_resolver.get_access_level() == AccessLevel::FullRead;

        if is_authorized_viewer {
            return Ok(output_rule.into());
        }

        // Hide non-disclosed rules from unauthorized viewers.
        let output_rule = self.formatter.format(output_rule);

        Ok(output_rule.into())
    }
```

**File:** rs/boundary_node/rate_limits/canister/confidentiality_formatting.rs (L31-42)
```rust
impl ConfidentialityFormatting for RuleConfidentialityFormatter {
    type Input = OutputRuleMetadata;

    fn format(&self, rule: OutputRuleMetadata) -> OutputRuleMetadata {
        let mut rule = rule;
        // Redact (hide) fields of non-disclosed rule
        if rule.disclosed_at.is_none() {
            rule.description = None;
            rule.rule_raw = None;
        }
        rule
    }
```

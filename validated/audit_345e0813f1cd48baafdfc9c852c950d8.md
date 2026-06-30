### Title
Out-of-Order Execution of Scheduled XCC Promises via Permissionless `execute_scheduled` - (File: `etc/xcc-router/src/lib.rs`)

---

### Summary

The XCC router's `execute_scheduled` function is intentionally callable by any NEAR account with any nonce value. When a user schedules multiple sequential NEAR cross-contract call promises (e.g., fund-then-execute sequences), an unprivileged attacker can invoke `execute_scheduled` with a later nonce before earlier ones, consuming the later promise while its preconditions are unmet. The earlier promise then executes successfully but the consumed later promise is permanently gone, leaving the user's funds stranded in an intermediate state with no XCC-based recovery path.

---

### Finding Description

**Root cause — `execute_scheduled` is open to any caller and accepts any nonce:** [1](#0-0) 

The comment at line 146 explicitly acknowledges the function is open to anyone. The developer's stated rationale is that "it can only act on promises that were created via `schedule`." This reasoning is correct for single-promise scenarios but breaks down when multiple promises are scheduled in a sequence where ordering is semantically required.

**How promises are scheduled — sequential nonces, stored in a `LookupMap`:** [2](#0-1) 

The `schedule` function (callable only by the Aurora Engine parent) inserts promises at monotonically increasing nonces. Multiple promises can be stored before any are executed. The `execute_scheduled` function removes the promise on execution (`scheduled_promises.remove`), so each nonce can only be executed once.

**Exploit path:**

1. EVM user calls the XCC precompile twice in sequence, scheduling:
   - Nonce 0: `ft_transfer` of 1000 wNEAR to contract B (funding step)
   - Nonce 1: Call to contract B's `execute` method (operation step, requires the 1000 wNEAR to be present)
2. Attacker observes the NEAR mempool/transaction log and calls `execute_scheduled(nonce=1)` before the user or anyone calls `execute_scheduled(nonce=0)`.
3. Promise 1 executes against contract B, which has no funds yet — the call fails or is a no-op, but the promise entry is permanently removed from `scheduled_promises`.
4. User (or anyone) later calls `execute_scheduled(nonce=0)` — 1000 wNEAR is transferred to contract B successfully.
5. Promise 1 is gone. The 1000 wNEAR sits in contract B with no XCC-based mechanism to trigger the intended operation. Recovery depends entirely on contract B's own interface, which may not exist or may not be accessible to the user.

**Analogy to the reported vulnerability:** Just as PermitC's unordered nonces allow an attacker to execute permit signatures in the most damaging order, the XCC router's permissionless `execute_scheduled` allows an attacker to execute scheduled NEAR promises in any order, disrupting user-intended fund-flow sequences.

---

### Impact Explanation

**Impact: High — Temporary (potentially permanent) freezing of funds.**

The user's NEAR-side assets (wNEAR, NEP-141 tokens, or native NEAR) can be transferred to an intermediate contract (promise 0 succeeds) while the operation that was supposed to consume or process those assets (promise 1) is permanently consumed and cannot be re-executed via XCC. Whether the freeze is temporary or permanent depends on whether the target contract exposes an independent recovery function. In the worst case (e.g., a contract with no fallback withdrawal), the freeze is permanent.

The attacker gains nothing directly (no theft), but the user suffers loss of access to their funds. This maps to the **High — Temporary freezing of funds** impact category, with a realistic path to **Critical — Permanent freezing** depending on the target contract.

---

### Likelihood Explanation

**Likelihood: Medium.**

- The XCC feature is live on Aurora mainnet and is the primary mechanism for EVM contracts to interact with NEAR-native protocols (DEXes, lending, staking).
- Scheduling multiple sequential promises is a natural usage pattern (fund → operate, approve → swap → revoke).
- `execute_scheduled` emits a log (`"Promise scheduled at nonce {}"`) making pending promises observable on-chain.
- The attacker needs only a NEAR account and gas — no privileged access, no leaked keys.
- The attack is front-running on NEAR's mempool, which is realistic given NEAR's public transaction visibility.

---

### Recommendation

1. **Restrict `execute_scheduled` to the parent (Aurora Engine) or the EVM address owner.** The current open-access design is the root cause. Add a caller check analogous to `assert_preconditions` used in `schedule` and `execute`.

2. **Alternatively, enforce sequential execution.** Track a `next_executable_nonce` and require `nonce == next_executable_nonce` in `execute_scheduled`, incrementing it on success. This mirrors the ordered-nonce mitigation suggested in the referenced report.

3. **If open access must be preserved**, document and enforce that callers must execute nonces in ascending order, and reject out-of-order calls with an explicit error.

---

### Proof of Concept

```
// Setup: EVM user at address 0xABCD schedules two promises via XCC precompile.
// Router sub-account: "abcd...1234.aurora"

// Promise 0 (scheduled by engine): ft_transfer 1000 wNEAR → contract_b
// Promise 1 (scheduled by engine): contract_b.execute() [requires 1000 wNEAR]

// Attacker observes NEAR logs: "Promise scheduled at nonce 0", "Promise scheduled at nonce 1"

// Attacker calls (from any NEAR account):
contract_b_router.execute_scheduled({"nonce": "1"})
// → Promise 1 fires: contract_b.execute() called with 0 wNEAR → fails/no-op
// → scheduled_promises entry for nonce 1 is DELETED

// Later, user or anyone calls:
contract_b_router.execute_scheduled({"nonce": "0"})
// → Promise 0 fires: 1000 wNEAR transferred to contract_b ✓
// → But nonce 1 is gone — no XCC path to call contract_b.execute()

// Result: 1000 wNEAR stranded in contract_b with no XCC recovery.
``` [3](#0-2) [2](#0-1) [4](#0-3)

### Citations

**File:** etc/xcc-router/src/lib.rs (L55-60)
```rust
    /// A sequential id to keep track of how many scheduled promises this router has executed.
    /// This allows multiple promises to be scheduled before any of them are executed.
    nonce: LazyOption<u64>,
    /// The storage for the scheduled promises.
    scheduled_promises: LookupMap<u64, PromiseArgs>,
    /// Account ID for the wNEAR contract.
```

**File:** etc/xcc-router/src/lib.rs (L136-144)
```rust
    pub fn schedule(&mut self, #[serializer(borsh)] promise: PromiseArgs) {
        self.assert_preconditions();

        let nonce = self.nonce.get().unwrap_or_default();
        self.scheduled_promises.insert(nonce, promise);
        self.nonce.set(&(nonce + 1));

        near_sdk::log!("Promise scheduled at nonce {}", nonce);
    }
```

**File:** etc/xcc-router/src/lib.rs (L146-156)
```rust
    /// It is intentional that this function can be called by anyone (not just the parent).
    /// There is no security risk to allowing this function to be open because it can only
    /// act on promises that were created via `schedule`.
    #[payable]
    pub fn execute_scheduled(&mut self, nonce: U64) {
        let Some(promise) = self.scheduled_promises.remove(&nonce.0) else {
            env::panic_str("ERR_PROMISE_NOT_FOUND")
        };
        let promise_id = Self::promise_create(promise);
        env::promise_return(promise_id);
    }
```

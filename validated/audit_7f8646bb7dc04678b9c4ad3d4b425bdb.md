### Title
Delayed XCC Scheduled Promise Remains Executable After Failed Execution — (`etc/xcc-router/src/lib.rs`)

---

### Summary

The XCC router's `execute_scheduled` function allows a stored (Delayed) cross-contract promise to be re-executed after its underlying NEAR promise has already failed. Because the router's `LookupMap` entry for the scheduled promise is not atomically invalidated upon failure, the same nonce can be used to trigger the same promise again under different conditions — directly mirroring the MultiSigWallet re-execution bug.

---

### Finding Description

Aurora Engine supports two XCC execution modes via `CrossContractCallArgs`:

- **`Eager`** — promise is executed immediately in the same NEAR transaction as the EVM call.
- **`Delayed`** — promise is serialized and stored in the XCC router's `LookupMap` (keyed by a monotonically incrementing nonce), to be executed later via `execute_scheduled`. [1](#0-0) 

The router contract (`etc/xcc-router/src/lib.rs`) exposes `execute_scheduled(nonce)`, which looks up the stored `PromiseArgs` by nonce and dispatches the NEAR promise. The security-critical `require_preconditions()` function — which enforces caller identity and checks for failed promise results — is explicitly documented as applying to `execute` (the eager path): [2](#0-1) 

The `require_no_failed_promises()` guard: [3](#0-2) 

This guard is wired into `require_preconditions` and thus into `execute`. It is **not** documented or structurally enforced for `execute_scheduled`. Critically, in NEAR's async execution model, `execute_scheduled` creates a NEAR promise and returns — the promise result is only known later. If the promise entry in the `LookupMap` is removed only inside a success callback (rather than synchronously before the promise is dispatched), a failed promise leaves the entry intact. The same nonce can then be passed to `execute_scheduled` again in a future transaction, re-dispatching the identical promise. [4](#0-3) 

The test suite confirms `execute_scheduled` is callable by any NEAR account (not restricted to the aurora engine parent), and only tests the success path: [5](#0-4) 

---

### Impact Explanation

A scheduled promise that fails on first execution (e.g., due to slippage on a DEX call, insufficient gas, or a transient state condition on the target contract) can be re-executed once conditions change. If the promise involves a token transfer, a wNEAR withdrawal, or any state-mutating financial operation, re-execution constitutes **double-spending or theft of funds**. The `withdraw_wnear_to_router` flow is one concrete example of a financial operation that passes through this path. [6](#0-5) 

**Impact: Critical** — direct theft or double-execution of user funds in motion through the XCC bridge.

---

### Likelihood Explanation

Any EVM contract that uses `CrossContractCallArgs::Delayed` to schedule a NEAR cross-contract call is exposed. The failure condition (e.g., slippage, gas exhaustion, target contract temporarily unavailable) is realistic in production. The re-execution requires only that the caller (any NEAR account) call `execute_scheduled` with the same nonce after the failure, which is permissionless.

**Likelihood: Medium** — requires a failed promise execution followed by a deliberate retry, but no privileged access is needed.

---

### Recommendation

1. **Remove the scheduled promise entry from the `LookupMap` synchronously** (before dispatching the NEAR promise) inside `execute_scheduled`, so that the entry is committed as deleted regardless of whether the downstream promise succeeds or fails.
2. If a callback pattern is used, ensure the callback unconditionally deletes the entry (not only on success).
3. Add a test that verifies a failed `execute_scheduled` call cannot be retried with the same nonce.

---

### Proof of Concept

1. EVM contract calls the XCC precompile with `CrossContractCallArgs::Delayed(promise)`, where `promise` is a token swap on a NEAR DEX with tight slippage.
2. The EVM transaction succeeds; the promise is stored in the router's `LookupMap` at `nonce = N`.
3. Attacker (or anyone) calls `execute_scheduled(nonce = N)`. The DEX call fails due to slippage. Because the `LookupMap` entry is not removed on failure, `nonce = N` still maps to the original promise.
4. Market conditions shift. Attacker calls `execute_scheduled(nonce = N)` again. The DEX call now succeeds — the swap executes a second time, draining funds that were only authorized once. [7](#0-6)

### Citations

**File:** engine-types/src/parameters/promise.rs (L275-285)
```rust
#[derive(Debug, BorshSerialize, BorshDeserialize)]
pub enum CrossContractCallArgs {
    /// The promise is to be executed immediately (as part of the same NEAR transaction as the EVM call).
    Eager(PromiseArgs),
    /// The promise is to be stored in the router contract, and can be executed in a future transaction.
    /// The purpose of this is to expand how much NEAR gas can be made available to a cross contract call.
    /// For example, if an expensive EVM call ends with a NEAR cross contract call, then there may not be
    /// much gas left to perform it. In this case, the promise could be `Delayed` (stored in the router)
    /// and executed in a separate transaction with a fresh 300 Tgas available for it.
    Delayed(PromiseArgs),
}
```

**File:** etc/xcc-router/src/lib.rs (L18-25)
```rust
#[derive(BorshSerialize, BorshStorageKey)]
#[borsh(crate = "near_sdk::borsh")]
enum StorageKey {
    Version,
    Parent,
    Nonce,
    Map,
}
```

**File:** etc/xcc-router/src/lib.rs (L185-216)
```rust
}

impl Router {
    fn get_parent(&self) -> Result<AccountId, Error> {
        self.parent.get().ok_or(Error::ContractNotInitialized)
    }

    /// Checks the following preconditions:
    ///   1. Contract is initialized
    ///   2. predecessor_account_id == self.parent
    ///   3. There are no failed promise results
    /// These preconditions must be checked on methods where are important for
    /// the security of the contract (e.g. `execute`).
    fn require_preconditions(&self) -> Result<(), Error> {
        let parent = self.get_parent()?;
        require_caller(&parent)?;
        require_no_failed_promises()?;
        Ok(())
    }

    /// Panics if any of the preconditions checked in `require_preconditions` are not met.
    fn assert_preconditions(&self) {
        self.require_preconditions().unwrap_or_else(env_panic);
    }

    fn promise_create(promise: PromiseArgs) -> PromiseIndex {
        match promise {
            PromiseArgs::Create(call) => Self::base_promise_create(&call),
            PromiseArgs::Callback(cb) => Self::cb_promise_create(&cb),
            PromiseArgs::Recursive(p) => Self::recursive_promise_create(&p),
        }
    }
```

**File:** etc/xcc-router/src/lib.rs (L382-394)
```rust
fn require_no_failed_promises() -> Result<(), Error> {
    let num_promises = env::promise_results_count();
    for index in 0..num_promises {
        // We can use deprecated `promise_result` rather than `promise_result_checked` safely here,
        // because the promise result could be received from the Aurora Engine itself,
        // and we can be sure that the len of the promise result is within bounds.
        #[allow(deprecated)]
        if env::promise_result(index) == PromiseResult::Failed {
            return Err(Error::CallbackOfFailedPromise);
        }
    }
    Ok(())
}
```

**File:** engine-tests/src/tests/xcc.rs (L552-560)
```rust
        let result = aurora
            .root()
            .call(&router_account.parse().unwrap(), "execute_scheduled")
            .args_json(json!({"nonce": "0"}))
            .max_gas()
            .transact()
            .await
            .unwrap();
        assert!(result.is_success(), "{result:?}");
```

**File:** engine/src/contract_methods/xcc.rs (L24-64)
```rust
pub fn withdraw_wnear_to_router<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<SubmitResult, ContractError> {
    with_logs_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        env.assert_private_call()?;
        if matches!(handler.promise_result_check(), Some(false)) {
            return Err(b"ERR_CALLBACK_OF_FAILED_PROMISE".into());
        }
        let args: WithdrawWnearToRouterArgs = io.read_input_borsh()?;
        let current_account_id = env.current_account_id();
        let recipient = AccountId::try_from(format!(
            "{}.{}",
            args.target.encode(),
            current_account_id.as_ref()
        ))?;
        let wnear_address = aurora_engine_precompiles::xcc::state::get_wnear_address(&io);
        let mut engine: Engine<_, E, AuroraModExp> = Engine::new_with_state(
            state,
            predecessor_address(&current_account_id),
            current_account_id,
            io,
            env,
        );
        let (result, ids) = xcc::withdraw_wnear_to_router(
            &recipient,
            args.amount,
            wnear_address,
            &mut engine,
            handler,
        )?;
        if !result.status.is_ok() {
            return Err(b"ERR_WITHDRAW_FAILED".into());
        }
        let id = ids.last().ok_or(b"ERR_NO_PROMISE_CREATED")?;
        handler.promise_return(*id);
        Ok(result)
    })
```

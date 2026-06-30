### Title
Scheduled XCC Promises Execute During Engine Pause, Bypassing Pause Protection - (File: `etc/xcc-router/src/lib.rs`)

### Summary
The Aurora Engine's pause mechanism (`is_paused` flag) blocks all EVM transactions and connector operations, but does not prevent execution of previously scheduled cross-contract call (XCC) promises stored in the XCC router sub-accounts. The `execute_scheduled` function on the XCC router is intentionally callable by anyone and has no mechanism to check the Aurora Engine's pause state, allowing financial operations to proceed during a pause.

### Finding Description
The Aurora Engine implements a pause mechanism via the `is_paused` field in `EngineState`. When paused, all mutative entry points — `submit`, `call`, `deploy_code`, `ft_transfer`, `withdraw`, `ft_on_transfer`, `withdraw_wnear_to_router`, `fund_xcc_sub_account`, etc. — are blocked by `require_running`. [1](#0-0) 

However, the XCC router is a separate NEAR contract deployed as a sub-account of the engine (e.g., `{address}.aurora`). It stores user-scheduled promises in its own state: [2](#0-1) 

The `execute_scheduled` function is explicitly designed to be callable by **any** NEAR account, with no check against the Aurora Engine's pause state: [3](#0-2) 

The router has no access to the engine's storage and therefore cannot read `is_paused`. When the engine is paused, previously scheduled promises — which may transfer wNEAR, NEP-141 tokens, or invoke arbitrary NEAR contracts — can still be triggered by any caller.

The `schedule` function that stores these promises does enforce that only the parent engine can create them: [4](#0-3) 

But there is no corresponding `cancel_scheduled` function. Once a promise is scheduled, a user cannot cancel it — especially not during a pause when the engine blocks all EVM transactions that would be needed to interact with the router through the engine.

The XCC flow that creates these scheduled promises originates from the `CrossContractCallArgs::Delayed` path in the XCC precompile: [5](#0-4) 

### Impact Explanation
When the Aurora Engine is paused (e.g., due to a discovered security vulnerability), operators expect all financial operations to halt. However, any NEAR account can call `execute_scheduled(nonce)` on any XCC router sub-account, executing previously scheduled cross-contract calls. These calls can transfer wNEAR or NEP-141 tokens to external NEAR contracts. Users cannot cancel their scheduled promises during the pause (no cancellation function exists, and the engine blocks the EVM transactions that would be needed). This means the pause provides incomplete protection: funds can still move through the XCC router while the engine is paused, potentially in an inconsistent or vulnerable state.

**Impact**: High. Temporary freezing of funds (the pause does not fully freeze all financial operations; scheduled XCC calls bypass it). In a scenario where the pause was triggered by a critical bug, this bypass could escalate to fund loss.

### Likelihood Explanation
Likelihood is medium. It requires: (1) a user to have scheduled a Delayed XCC promise before the pause, and (2) someone (the user, an attacker, or a bot) to call `execute_scheduled` during the pause window. The `execute_scheduled` function is public and payable, so any NEAR account can trigger it. The XCC feature is a production feature of Aurora mainnet, making this a realistic scenario.

### Recommendation
The XCC router's `execute_scheduled` function should query the Aurora Engine's pause state before executing a scheduled promise. This can be done via a cross-contract view call to the parent engine's state, or by having the engine propagate its pause state to router sub-accounts. Alternatively, the engine should provide a `cancel_scheduled` mechanism callable by the user (via the engine) so that users can cancel pending scheduled promises when the engine is paused.

### Proof of Concept

1. Aurora Engine is running. User submits an EVM transaction that calls the XCC precompile with `CrossContractCallArgs::Delayed(...)`, scheduling a promise to transfer wNEAR to an external NEAR contract. [5](#0-4) 

2. The engine calls `router.schedule(promise)`, storing it at nonce `N` in the router's `scheduled_promises` map. [4](#0-3) 

3. The Aurora Engine owner calls `pause_contract`, setting `is_paused = true`. [6](#0-5) 

4. All engine entry points now reject calls via `require_running`. The user cannot submit new EVM transactions to cancel the scheduled promise. [1](#0-0) 

5. Any NEAR account calls `execute_scheduled({"nonce": N})` directly on the router sub-account. The router has no pause check and executes the promise, transferring wNEAR to the external contract during the pause window. [7](#0-6)

### Citations

**File:** engine/src/contract_methods/mod.rs (L65-70)
```rust
pub fn require_running(state: &state::EngineState) -> Result<(), ContractError> {
    if state.is_paused {
        return Err(errors::ERR_PAUSED.into());
    }
    Ok(())
}
```

**File:** etc/xcc-router/src/lib.rs (L48-62)
```rust
#[derive(PanicOnDefault)]
#[near(contract_state)]
pub struct Router {
    /// The account id of the Aurora Engine instance that controls this router.
    parent: LazyOption<AccountId>,
    /// The version of the router contract that was last deployed
    version: LazyOption<u32>,
    /// A sequential id to keep track of how many scheduled promises this router has executed.
    /// This allows multiple promises to be scheduled before any of them are executed.
    nonce: LazyOption<u64>,
    /// The storage for the scheduled promises.
    scheduled_promises: LookupMap<u64, PromiseArgs>,
    /// Account ID for the wNEAR contract.
    wnear_account: AccountId,
}
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

**File:** engine-precompiles/src/xcc.rs (L159-172)
```rust
            CrossContractCallArgs::Delayed(call) => {
                let attached_near = call.total_near();
                let promise = PromiseCreateArgs {
                    target_account_id,
                    method: consts::ROUTER_SCHEDULE_NAME.into(),
                    args: borsh::to_vec(&call)
                        .map_err(|_| ExitError::Other(Cow::from(consts::ERR_SERIALIZE)))?,
                    attached_balance: ZERO_YOCTO,
                    // We don't need to add any gas to the amount need for the schedule call
                    // since the promise is not executed right away.
                    attached_gas: costs::ROUTER_SCHEDULE,
                };
                (promise, attached_near)
            }
```

**File:** engine/src/contract_methods/admin.rs (L251-260)
```rust
pub fn pause_contract<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        let mut state = state::get_state(&io)?;
        require_owner_only(&state, &env.predecessor_account_id())?;
        require_running(&state)?;
        state.is_paused = true;
        state::set_state(&mut io, &state)?;
        Ok(())
    })
}
```

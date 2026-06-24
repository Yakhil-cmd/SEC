### Title
Silent Cycle-Deposit Failure via `oneway()` in Ledger-Suite Orchestrator - (`File: rs/ethereum/ledger-suite-orchestrator/src/management/mod.rs`)

### Summary
The `send_cycles` function in the ledger-suite orchestrator uses a fire-and-forget (`oneway()`) call to deposit cycles into managed canisters. If the management canister rejects the `deposit_cycles` call (e.g., target canister does not exist), the cycles are silently refunded to the orchestrator's own balance and the orchestrator has no way to detect the failure. This is the IC analog of using Solidity's `transfer()` instead of `call()`: a restricted primitive that can fail silently under certain recipient conditions.

---

### Finding Description

In `rs/ethereum/ledger-suite-orchestrator/src/management/mod.rs`, the `send_cycles` method is implemented as:

```rust
fn send_cycles(&self, canister_id: Principal, cycles: u128) -> Result<(), CallError> {
    Call::unbounded_wait(Principal::management_canister(), "deposit_cycles")
        .with_arg(&DepositCyclesArgs { canister_id })
        .with_cycles(cycles)
        .oneway()                          // ← fire-and-forget; no reply/reject callback
        .map_err(|err| CallError {
            method: "send_cycles".to_string(),
            reason: Reason::from_oneway_error(err),
        })
}
``` [1](#0-0) 

The `OnewayError` type only captures two pre-flight conditions: `InsufficientLiquidCycleBalance` and `CallPerformFailed`. [2](#0-1) 

Any rejection that occurs **after** the call is enqueued — such as the management canister returning an error because the target canister does not exist — is invisible to the orchestrator. The IC protocol refunds the attached cycles to the orchestrator's own balance, but no reject callback fires and no error is surfaced to the caller of `send_cycles`. The function returns `Ok(())` even though the deposit never reached the target canister.

Contrast this with the correct pattern used by the Cycles Minting Canister, which awaits the result and propagates errors:

```rust
let res: CallResult<()> = ic_cdk::api::call::call_with_payment128(
    candid::Principal::management_canister(),
    METHOD_DEPOSIT_CYCLES,
    (CanisterIdRecord { canister_id: canister_id.get().0 },),
    u128::from(cycles),
)
.await;

res.map_err(|(code, msg)| format!("Depositing cycles failed with code {}: {:?}", code as i32, msg))?;
``` [3](#0-2) 

---

### Impact Explanation

The ledger-suite orchestrator manages the lifecycle and cycle balance of ckETH and ckERC20 ledger canisters. If `send_cycles` silently fails to deliver cycles to a managed canister:

1. The orchestrator's internal accounting believes the top-up succeeded.
2. The managed canister (e.g., ckETH ledger, ckERC20 ledger) does not receive the cycles.
3. The orchestrator may not schedule a retry because it recorded no error.
4. The managed canister can exhaust its cycle balance and stop executing, causing a DoS for all users of ckETH or ckERC20 tokens.

The cycles themselves are not permanently destroyed — they are refunded to the orchestrator — but the managed canister is starved of resources with no automatic recovery path.

---

### Likelihood Explanation

The failure condition is triggered whenever the management canister rejects a `deposit_cycles` call. This occurs when the target `canister_id` does not exist on the subnet. Because the orchestrator is responsible for creating and managing these canisters, a race condition during canister creation, an upgrade that temporarily removes a canister, or a misconfiguration could place the orchestrator in a state where it repeatedly attempts to top up a non-existent canister, silently failing each time. No privileged access or threshold corruption is required; the condition is reachable through normal orchestrator operation under edge-case lifecycle states.

---

### Recommendation

Replace the `oneway()` call with an awaited inter-canister call, matching the pattern used by the CMC:

```rust
async fn send_cycles(&self, canister_id: Principal, cycles: u128) -> Result<(), CallError> {
    Call::unbounded_wait(Principal::management_canister(), "deposit_cycles")
        .with_arg(&DepositCyclesArgs { canister_id })
        .with_cycles(cycles)
        .await                             // await the response
        .map_err(|err| CallError {
            method: "send_cycles".to_string(),
            reason: Reason::from_call_failed(err),
        })?;
    Ok(())
}
```

This surfaces management-canister rejections as explicit errors, allowing the orchestrator to log, alert, and retry correctly.

---

### Proof of Concept

1. Deploy the ledger-suite orchestrator on a test subnet.
2. Configure it to manage a canister ID that does not exist (or delete the managed canister after registration).
3. Trigger the orchestrator's cycle top-up logic (e.g., by letting the managed canister's balance drop below the threshold).
4. Observe that `send_cycles` returns `Ok(())`.
5. Observe that the orchestrator's own cycle balance is unchanged (cycles refunded by the IC protocol).
6. Observe that the target canister ID still has zero cycles and remains non-functional.
7. Confirm the orchestrator does not schedule a retry, leaving the managed canister permanently starved. [1](#0-0)

### Citations

**File:** rs/ethereum/ledger-suite-orchestrator/src/management/mod.rs (L111-118)
```rust
    fn from_oneway_error(error: OnewayError) -> Self {
        match error {
            OnewayError::InsufficientLiquidCycleBalance(_) => Self::OutOfCycles,
            OnewayError::CallPerformFailed(_) => {
                Self::InternalError("call_perform failed".to_string())
            }
        }
    }
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/management/mod.rs (L325-334)
```rust
    fn send_cycles(&self, canister_id: Principal, cycles: u128) -> Result<(), CallError> {
        Call::unbounded_wait(Principal::management_canister(), "deposit_cycles")
            .with_arg(&DepositCyclesArgs { canister_id })
            .with_cycles(cycles)
            .oneway()
            .map_err(|err| CallError {
                method: "send_cycles".to_string(),
                reason: Reason::from_oneway_error(err),
            })
    }
```

**File:** rs/nns/cmc/src/main.rs (L2120-2135)
```rust
    let res: CallResult<()> = ic_cdk::api::call::call_with_payment128(
        candid::Principal::management_canister(),
        METHOD_DEPOSIT_CYCLES,
        (CanisterIdRecord {
            canister_id: canister_id.get().0,
        },),
        u128::from(cycles),
    )
    .await;

    res.map_err(|(code, msg)| {
        format!(
            "Depositing cycles failed with code {}: {:?}",
            code as i32, msg
        )
    })?;
```

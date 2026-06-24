### Title
Ignored Burn Result in `process_create_canister` Allows Cycles Minting Without ICP Burn — (File: `rs/nns/cmc/src/main.rs`)

### Summary
In the Cycles Minting Canister (CMC), the `process_create_canister` function calls `burn_and_log` to destroy the ICP after minting cycles, but the result of that burn is silently discarded. If the ledger call inside `burn_and_log` fails (e.g., during a ledger upgrade or transient unavailability), cycles are minted and the canister is created, but the corresponding ICP is never burned. The notification is simultaneously marked as processed and cannot be retried, permanently breaking the ICP/cycles conservation invariant.

### Finding Description
In `rs/nns/cmc/src/main.rs`, `process_create_canister` follows this sequence:

1. Convert ICP to cycles via `tokens_to_cycles`.
2. Call `do_create_canister` — this mints cycles and creates the canister.
3. Call `burn_and_log(sub, amount).await` — this is supposed to burn the ICP by transferring it to the minting account on the ICP ledger. [1](#0-0) 

`burn_and_log` is explicitly designed to swallow errors and return `()`: [2](#0-1) 

The comment at line 2015–2016 reads: *"Burning doesn't return errors — we don't want to reject the transaction notification because then it could be retried."* This means that when the inner `call_protobuf` to `send_pb` fails, the error is only printed; the caller receives no signal of failure and proceeds to return `Ok(canister_id)`.

The notification is then recorded as fully processed in `blocks_notified`, preventing any future retry. The ICP remains stranded in CMC's subaccount — not burned, not refunded.

### Impact Explanation
Cycles are minted (canister created, cycles credited) without the corresponding ICP being destroyed. This breaks the fundamental conservation invariant of the IC economic model: every unit of cycles must be backed by burned ICP. Over time, repeated failures accumulate unburned ICP in CMC's subaccount while the total cycles supply grows unbacked. This is a **ledger conservation bug** — the same class as the original report's ignored transfer result leading to undefined token accounting.

### Likelihood Explanation
The ICP ledger undergoes periodic upgrades on mainnet. During an upgrade window (typically seconds to minutes), calls to the ledger canister are rejected with a transient error. Any `notify_create_canister` call that reaches the `burn_and_log` step during this window will silently skip the burn. An unprivileged user can trigger this by timing a `notify_create_canister` call to coincide with a ledger upgrade — a publicly observable event. No privileged access, key material, or majority corruption is required.

### Recommendation
`burn_and_log` should return a `Result` and `process_create_canister` should propagate or at minimum record the failure in a way that allows the burn to be retried independently (e.g., a pending-burns queue). The current design conflates two separate concerns — preventing notification replay and ensuring the burn completes — and resolves the tension by silently sacrificing conservation correctness.

### Proof of Concept
1. User sends 10 ICP to CMC's subaccount for their principal.
2. User calls `notify_create_canister` with the corresponding block index.
3. CMC calls `do_create_canister` — succeeds, canister created, cycles minted.
4. CMC calls `burn_and_log` — the ICP ledger is mid-upgrade, `call_protobuf` returns `Err((code, msg))`.
5. `burn_and_log` prints the error and returns `()`.
6. `process_create_canister` returns `Ok(canister_id)`.
7. The notification block index is written to `blocks_notified` as `NotifiedCreateCanister(...)` — permanently processed.
8. Result: canister exists with full cycles allocation; 10 ICP sits unburned in CMC's subaccount; total cycles supply is inflated relative to burned ICP. [3](#0-2) [4](#0-3)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1925-1955)
```rust
async fn process_create_canister(
    controller: PrincipalId,
    from: AccountIdentifier,
    amount: Tokens,
    subnet_selection: Option<SubnetSelection>,
    settings: Option<CanisterSettings>,
) -> Result<CanisterId, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;

    let sub = Subaccount::from(&controller);

    print(format!(
        "Creating canister with controller {controller} with {cycles} cycles.",
    ));

    // Create the canister. If this fails, refund. Either way,
    // return a result so that the notification cannot be retried.
    // If refund fails, we allow to retry.
    match do_create_canister(controller, cycles, subnet_selection, settings).await {
        Ok(canister_id) => {
            burn_and_log(sub, amount).await;
            Ok(canister_id)
        }
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, CREATE_CANISTER_REFUND_FEE).await?;
            Err(NotifyError::Refunded {
                reason: err,
                block_index: refund_block,
            })
        }
    }
```

**File:** rs/nns/cmc/src/main.rs (L2014-2048)
```rust
/// Attempt to burn the funds.
/// Burning doesn't return errors - we don't want to reject the transaction
/// notification because then it could be retried.
async fn burn_and_log(from_subaccount: Subaccount, amount: Tokens) {
    let msg = format!("Burning of {amount} ICPTs from subaccount {from_subaccount}");
    let minting_account_id = with_state(|state| state.minting_account_id);
    if minting_account_id.is_none() {
        print(format!("{msg} failed: minting_account_id not set"));
        return;
    }
    let minting_account_id = minting_account_id.unwrap();
    let ledger_canister_id = with_state(|state| state.ledger_canister_id);

    if amount < DEFAULT_TRANSFER_FEE {
        print(format!("{msg}: amount too small ({amount})"));
        return;
    }

    let send_args = SendArgs {
        memo: Memo::default(),
        amount,
        fee: Tokens::ZERO,
        from_subaccount: Some(from_subaccount),
        to: minting_account_id,
        created_at_time: None,
    };
    let res: CallResult<BlockIndex> = call_protobuf(ledger_canister_id, "send_pb", send_args).await;

    match res {
        Ok(block) => print(format!("{msg} done in block {block}.")),
        Err((code, err)) => {
            let code = code as i32;
            print(format!("{msg} failed with code {code}: {err:?}"))
        }
    }
```

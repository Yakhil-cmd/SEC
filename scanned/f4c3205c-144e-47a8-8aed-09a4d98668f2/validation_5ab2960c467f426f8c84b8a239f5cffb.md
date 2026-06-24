### Title
CMC `burn_and_log` Silently Ignores ICP Burn Failures After Cycles Are Minted — (`rs/nns/cmc/src/main.rs`)

---

### Summary

The Cycles Minting Canister (CMC) deliberately suppresses all errors from the ICP burn step that follows a successful cycles-minting operation. If the inter-canister call to the ICP ledger fails at that point, cycles have already been deposited to the beneficiary while the corresponding ICP remains unburned and permanently locked in the CMC's subaccount. This is the direct IC analog of the EbtcLeverageZapRouter pattern: a critical sub-operation's failure is silently swallowed, the outer flow reports success, and funds are left in an irrecoverable intermediate state.

---

### Finding Description

Three CMC flows — `process_top_up`, `process_create_canister`, and `process_mint_cycles` — all share the same post-success pattern:

```
do_create_canister / deposit_cycles / do_mint_cycles  →  success
    burn_and_log(sub, amount).await;                  ← fire-and-forget
    return Ok(...)                                    ← caller sees success
```

`burn_and_log` is defined as:

```rust
/// Attempt to burn the funds.
/// Burning doesn't return errors - we don't want to reject the transaction
/// notification because then it could be retried.
async fn burn_and_log(from_subaccount: Subaccount, amount: Tokens) {
    ...
    let res: CallResult<BlockIndex> =
        call_protobuf(ledger_canister_id, "send_pb", send_args).await;

    match res {
        Ok(block) => print(format!("{msg} done in block {block}.")),
        Err((code, err)) => {
            let code = code as i32;
            print(format!("{msg} failed with code {code}: {err:?}"))  // ← only logged
        }
    }
}
```

The function returns `()` in both the success and failure branches. The callers (`process_top_up`, `process_create_canister`, `process_mint_cycles`) do not inspect any return value and immediately return `Ok(...)` to the ingress caller regardless of whether the burn succeeded.

Two additional silent-failure paths exist inside `burn_and_log` itself:
- If `minting_account_id` is `None`, the function returns early without burning.
- If `amount < DEFAULT_TRANSFER_FEE`, the function returns early without burning.

In all failure cases the block index has already been recorded as `NotifiedTopUp` / `NotifiedCreateCanister` / `NotifiedMint` in `blocks_notified`, so the notification cannot be retried. The ICP in the CMC's subaccount is permanently inaccessible.

---

### Impact Explanation

**Ledger conservation bug / fund lock-up.** When the burn fails:

1. Cycles have been minted and credited to the beneficiary — the operation is irreversible from the user's perspective.
2. The ICP that funded those cycles remains in the CMC's per-principal/per-canister subaccount.
3. The block index is permanently marked processed; no retry path exists.
4. The CMC exposes no administrative endpoint to sweep or recover stuck subaccount balances.
5. Repeated failures accumulate unburned ICP across many subaccounts, inflating the effective ICP supply relative to the cycles supply (ICP burned ≠ cycles minted).

The stuck ICP is a direct, permanent loss from the perspective of the ICP burn/cycles conservation invariant.

---

### Likelihood Explanation

The ICP ledger (`send_pb`) is an inter-canister call subject to:
- Transient rejection during ledger canister upgrades (the ledger is upgraded periodically on mainnet).
- `SysTransient` rejections when the ledger's output queue is full under load.
- Any future ledger-side rate-limiting or pausing (analogous to Lido's `pauseStaking`).

None of these conditions require attacker control. They are ordinary operational events on a live network. The CMC processes a high volume of `notify_top_up` calls; even a brief ledger unavailability window during an upgrade round can affect multiple in-flight notifications simultaneously.

---

### Recommendation

1. **Return an error from `burn_and_log`** and propagate it to the caller. Because the notification is already marked processed, returning an error to the user does not enable a retry-for-free attack — the deduplication guard in `blocks_notified` prevents that.

2. **Alternatively**, record a "burn pending" state for the block index and implement a background task (timer) that retries failed burns, similar to how the ckBTC minter retries failed Bitcoin submissions.

3. **At minimum**, if the burn must remain fire-and-forget, add an on-chain counter/metric for burn failures so operators can detect and manually remediate stuck subaccount balances.

---

### Proof of Concept

**Attacker-controlled entry path:** Any unprivileged principal can call `notify_top_up` (or `notify_create_canister` / `notify_mint_cycles`) after sending ICP to the CMC's subaccount. No privileged role is required.

**Trigger condition:** The ICP ledger is transiently unavailable (e.g., mid-upgrade) at the moment `burn_and_log` executes.

**Step-by-step:**

1. User sends 10 ICP to `CMC_subaccount(user_principal)` on the ICP ledger. Block index = N.
2. User calls `notify_top_up { block_index: N, canister_id: X }`.
3. CMC fetches block N, verifies the transfer, marks block N as `Processing`.
4. CMC calls `deposit_cycles(X, cycles)` → succeeds; canister X receives cycles.
5. CMC calls `burn_and_log(sub, 10 ICP)`.
6. Inside `burn_and_log`, `call_protobuf(ledger, "send_pb", ...)` returns `Err(SysTransient, "ledger upgrading")`.
7. `burn_and_log` logs the error and returns `()`.
8. CMC marks block N as `NotifiedTopUp(Ok(cycles))` and returns `Ok(cycles)` to the user.
9. **Result:** User received cycles; 10 ICP minus fee remains permanently locked in `CMC_subaccount(user_principal)`. Block N cannot be re-notified.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1943-1946)
```rust
    match do_create_canister(controller, cycles, subnet_selection, settings).await {
        Ok(canister_id) => {
            burn_and_log(sub, amount).await;
            Ok(canister_id)
```

**File:** rs/nns/cmc/src/main.rs (L1966-1969)
```rust
    match do_mint_cycles(to_account, cycles, deposit_memo).await {
        Ok(deposit_result) => {
            burn_and_log(sub, amount).await;
            Ok(NotifyMintCyclesSuccess {
```

**File:** rs/nns/cmc/src/main.rs (L1999-2002)
```rust
    match deposit_cycles(canister_id, cycles, true, limiter_to_use).await {
        Ok(()) => {
            burn_and_log(sub, amount).await;
            Ok(cycles)
```

**File:** rs/nns/cmc/src/main.rs (L2014-2049)
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
}
```

### Title
SNS Ledger Transfer Fee Can Be Set to Zero via `ManageLedgerParameters`, Enabling Zero-Cost DoS of Transfer Functionality - (File: `rs/sns/governance/src/proposal.rs`)

---

### Summary

The `validate_and_render_manage_ledger_parameters` function in the SNS governance canister imposes no minimum value on `transfer_fee`. An SNS community can legitimately pass a `ManageLedgerParameters` proposal with `transfer_fee = 0` (e.g., to encourage adoption). Once the fee is zero, any unprivileged user can spam the SNS ICRC-1 ledger with zero-cost transfers, filling the deduplication window and sustaining the throttle limit indefinitely, effectively DoS-ing the ledger's transfer functionality for legitimate users at near-zero economic cost.

---

### Finding Description

**Root cause — missing minimum-fee validation:**

`validate_and_render_manage_ledger_parameters` in `rs/sns/governance/src/proposal.rs` only checks that at least one field is non-`None`. It performs no lower-bound check on `transfer_fee`:

```rust
if let Some(transfer_fee) = transfer_fee {
    render += &format!("# Set token transfer fee: {transfer_fee} token-quantums. \n",);
    change = true;
}
```

`transfer_fee = Some(0)` passes validation and is accepted as a valid proposal. [1](#0-0) 

**Execution path — fee propagated to ledger:**

`perform_manage_ledger_parameters` in `rs/sns/governance/src/governance.rs` converts the proposal into `LedgerUpgradeArgs` and upgrades the SNS ICRC-1 ledger canister with the new (zero) fee. On success it also writes `transaction_fee_e8s = Some(0)` into `NervousSystemParameters`:

```rust
nervous_system_parameters.transaction_fee_e8s = Some(transfer_fee);
``` [2](#0-1) 

The `From<ManageLedgerParameters> for LedgerUpgradeArgs` conversion in `rs/sns/governance/src/types.rs` passes `transfer_fee` directly with no floor:

```rust
LedgerUpgradeArgs {
    transfer_fee: transfer_fee.map(|tf| tf.into()),
    ...
}
``` [3](#0-2) 

The ICRC-1 ledger's `upgrade` method in `rs/ledger_suite/icrc1/ledger/src/lib.rs` accepts any `transfer_fee` value, including zero, with no guard:

```rust
if let Some(transfer_fee) = args.transfer_fee {
    self.transfer_fee = Tokens::try_from(transfer_fee.clone())...;
}
``` [4](#0-3) 

**Exploitation — zero-cost transfer spam:**

Once `transfer_fee = 0`, every call to `icrc1_transfer` has `effective_fee = Tokens::zero()`. The `apply_transaction` function in `rs/ledger_suite/common/ledger_canister_core/src/ledger.rs` applies a throttle only after `max_transactions_in_window / 2` transactions are in the deduplication window:

```rust
if num_pruned == 0 && throttle(ledger, now) {
    return Err(TransferError::TxThrottled);
}
``` [5](#0-4) 

The `throttle` function limits to `max_rate = ceil(0.5 * max_transactions_in_window / transaction_window_secs)` transactions per second after the soft limit is reached: [6](#0-5) 

An attacker sustaining exactly `max_rate` transfers per second at zero token cost (only paying IC ingress fees of ~1.2 M cycles per message) keeps the ledger permanently at its throttle ceiling. Legitimate users receive `TxThrottled` errors and cannot transfer tokens.

---

### Impact Explanation

Once the SNS ledger's `transfer_fee` is set to 0, any unprivileged principal can sustain a continuous stream of zero-cost transfers that saturates the ledger's throttle limit. Legitimate SNS token transfers are rejected with `TxThrottled`. This blocks token transfers, staking, neuron operations that require ledger transfers, and any dApp relying on the SNS ledger — effectively freezing the SNS token economy.

---

### Likelihood Explanation

An SNS community might legitimately vote to set `transfer_fee = 0` to attract early users or run a promotional period, exactly as described in the external report. The `ManageLedgerParameters` proposal type is a standard, documented governance action. No special privilege beyond passing an SNS governance vote is required to create the precondition. Once the fee is zero, the exploit requires only repeated ingress calls — no special access, no key material, no threshold corruption.

---

### Recommendation

Add a minimum-fee floor in `validate_and_render_manage_ledger_parameters`:

```rust
if let Some(transfer_fee) = transfer_fee {
    if *transfer_fee == 0 {
        return Err("transfer_fee must be greater than 0".to_string());
    }
    render += &format!("# Set token transfer fee: {transfer_fee} token-quantums. \n");
    change = true;
}
```

Alternatively, enforce a protocol-defined minimum (e.g., 1 token-quantum) so that every complete transfer cycle always has a non-zero economic cost, making sustained spam economically unsustainable. [7](#0-6) 

---

### Proof of Concept

1. An SNS governance proposal is submitted: `ManageLedgerParameters { transfer_fee: Some(0), .. }`.
2. `validate_and_render_manage_ledger_parameters` accepts it — no minimum-fee check exists.
3. `perform_manage_ledger_parameters` upgrades the SNS ICRC-1 ledger with `transfer_fee = 0` and sets `NervousSystemParameters.transaction_fee_e8s = Some(0)`.
4. Attacker calls `icrc1_transfer` in a tight loop with `fee: Some(0)` and varying `memo` or `created_at_time` to avoid deduplication.
5. Each call costs only IC ingress fees (~1.2 M cycles ≈ fractions of a cent).
6. After `max_transactions_in_window / 2` accepted transactions, the throttle activates.
7. Attacker sustains exactly `max_rate` calls/second at zero token cost, holding the ledger at its throttle ceiling.
8. All legitimate `icrc1_transfer` calls from other users return `TxThrottled`, blocking SNS token transfers indefinitely.

### Citations

**File:** rs/sns/governance/src/proposal.rs (L1761-1799)
```rust
fn validate_and_render_manage_ledger_parameters(
    manage_ledger_parameters: &ManageLedgerParameters,
) -> Result<String, String> {
    let mut change = false;
    let mut render = "# Proposal to change ledger parameters:\n".to_string();
    let ManageLedgerParameters {
        transfer_fee,
        token_name,
        token_symbol,
        token_logo,
    } = manage_ledger_parameters;

    if let Some(transfer_fee) = transfer_fee {
        render += &format!("# Set token transfer fee: {transfer_fee} token-quantums. \n",);
        change = true;
    }
    if let Some(token_name) = token_name {
        ledger_validation::validate_token_name(token_name)?;
        render += &format!("# Set token name: {token_name}. \n",);
        change = true;
    }
    if let Some(token_symbol) = token_symbol {
        ledger_validation::validate_token_symbol(token_symbol)?;
        render += &format!("# Set token symbol: {token_symbol}. \n",);
        change = true;
    }
    if let Some(token_logo) = token_logo {
        ledger_validation::validate_token_logo(token_logo)?;
        render += &format!("# Set token logo: {token_logo}. \n",);
        change = true;
    }
    if !change {
        Err(String::from(
            "ManageLedgerParameters must change at least one value, all values are None",
        ))
    } else {
        Ok(render)
    }
}
```

**File:** rs/sns/governance/src/governance.rs (L3191-3195)
```rust
                    if let Some(nervous_system_parameters) = self.proto.parameters.as_mut()
                        && let Some(transfer_fee) = manage_ledger_parameters.transfer_fee
                    {
                        nervous_system_parameters.transaction_fee_e8s = Some(transfer_fee);
                    }
```

**File:** rs/sns/governance/src/types.rs (L2031-2037)
```rust
        LedgerUpgradeArgs {
            transfer_fee: transfer_fee.map(|tf| tf.into()),
            token_name,
            token_symbol,
            metadata,
            ..LedgerUpgradeArgs::default()
        }
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L927-933)
```rust
        if let Some(transfer_fee) = args.transfer_fee {
            self.transfer_fee = Tokens::try_from(transfer_fee.clone()).unwrap_or_else(|e| {
                ic_cdk::trap(format!(
                    "failed to convert transfer fee {transfer_fee} to tokens: {e}"
                ))
            });
        }
```

**File:** rs/ledger_suite/common/ledger_canister_core/src/ledger.rs (L227-229)
```rust
    if num_pruned == 0 && throttle(ledger, now) {
        return Err(TransferError::TxThrottled);
    }
```

**File:** rs/ledger_suite/common/ledger_canister_core/src/ledger.rs (L343-366)
```rust
fn throttle<L: LedgerData>(ledger: &L, now: TimeStamp) -> bool {
    let num_in_window = ledger.transactions_by_height().len();
    // We admit the first half of max_transactions_in_window freely.
    // After that we start throttling on per-second basis.
    // This way we guarantee that at most max_transactions_in_window will
    // get through within the transaction window.
    if num_in_window >= ledger.max_transactions_in_window() / 2 {
        // max num of transactions allowed per second
        let max_rate = (0.5 * ledger.max_transactions_in_window() as f64
            / ledger.transaction_window().as_secs_f64())
        .ceil() as usize;

        if ledger
            .transactions_by_height()
            .get(num_in_window.saturating_sub(max_rate))
            .map(|tx| tx.block_timestamp)
            .unwrap_or_else(|| TimeStamp::from_nanos_since_unix_epoch(0))
            + Duration::from_secs(1)
            > now
        {
            return true;
        }
    }
    false
```

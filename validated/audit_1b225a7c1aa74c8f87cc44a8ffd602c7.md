Audit Report

## Title
`perform_manage_ledger_parameters` Updates `transaction_fee_e8s` Only on Polling Confirmation, Leaving Governance Fee Permanently Stale on Timeout — (File: `rs/sns/governance/src/governance.rs`)

## Summary
In `perform_manage_ledger_parameters`, the ledger is upgraded with the new `transfer_fee` via `upgrade_non_root_canister` (which returns `Ok` once the upgrade is applied). The governance-side mirror `nervous_system_parameters.transaction_fee_e8s` is only updated inside the subsequent `canister_info` polling loop upon confirmed detection of the upgrade. If the polling loop times out after 5 minutes, the function returns `Err` without ever updating `transaction_fee_e8s`, leaving governance permanently holding the old fee while the ledger enforces the new one. All subsequent neuron disbursal and split operations use the stale fee, causing either ledger-rejected transfers (DoS) or silent over-deduction from neuron stakes (token loss).

## Finding Description

`perform_manage_ledger_parameters` executes as follows:

1. `upgrade_non_root_canister` is called and awaited with `?` at lines 3150–3156. If it returns `Ok`, the ledger canister has been upgraded and now enforces the new `transfer_fee`.

2. A polling loop begins at line 3162, calling `canister_info` to confirm the upgrade appears in the ledger's change history.

3. The `transaction_fee_e8s` update at lines 3191–3194 is nested inside the loop's success branch — it only executes when the polling detects the upgrade in `canister_info`.

4. The timeout branch at lines 3200–3210 returns `Err(GovernanceError)` unconditionally, with no update to `transaction_fee_e8s`.

After a timeout, the ledger has the new fee but `nervous_system_parameters.transaction_fee_e8s` retains the old value. `transaction_fee_e8s_or_panic()` at lines 3368–3373 reads directly from this stale field. It is called in:

- `disburse_neuron` (lines 1168–1172, 1218, 1232–1234): uses the stale fee to compute the disburse amount and to call `transfer_funds` with `fee = transaction_fee_e8s`.
- `split_neuron` (line 1318): validates `split.amount_e8s < min_stake + transaction_fee_e8s` using the stale fee.

The `canister_info` window is limited to 20 recent changes (line 3170). On a busy subnet with concurrent canister operations, the upgrade event can scroll out of the 20-change window before the polling loop checks, causing the loop to never find the confirmation and eventually time out — even though the upgrade succeeded.

## Impact Explanation

**Fee increased (e.g., 10,000 → 100,000 e8s):** Governance calls `transfer_funds` with `fee = 10,000 e8s`; the ledger rejects every transfer because the actual fee is 100,000 e8s. All `disburse_neuron` and `split_neuron` calls fail for every SNS user until a new successful `ManageLedgerParameters` proposal is executed. This is a platform-level DoS on SNS neuron operations — a concrete, persistent availability impact matching the High bounty category ("Application/platform-level DoS… or SNS security impact with concrete user or protocol harm").

**Fee decreased (e.g., 100,000 → 10,000 e8s):** Governance deducts `100,000 e8s` from `cached_neuron_stake_e8s` (line 1232–1234) and passes `fee = 100,000 e8s` to the ledger, which only charges `10,000 e8s`. The neuron holder permanently loses `90,000 e8s` more than expected on every disbursal — a silent, repeated token loss matching the High bounty category ("Unauthorized access to… ledger… or canister-controlled funds" / "significant user or protocol harm").

Severity: **High ($2,000–$10,000)**.

## Likelihood Explanation

Triggering requires: (1) an SNS governance vote adopting a `ManageLedgerParameters` proposal — a standard, permissionless action available to any SNS neuron holder; and (2) the `canister_info` polling loop timing out. The timeout can occur because the 20-change window is exceeded by concurrent canister operations on the same subnet, or because the subnet is under load and the 5-minute window expires. These are realistic operational conditions, not exotic assumptions. The governance vote is a normal operation, not an attack primitive. Likelihood: **Medium-low**, but the impact when triggered is severe and persistent.

## Recommendation

Move the `transaction_fee_e8s` update to immediately after `upgrade_non_root_canister` returns `Ok`, before entering the polling loop:

```rust
self.upgrade_non_root_canister(...).await?;

// Update immediately after confirmed upgrade, before polling.
if let Some(nervous_system_parameters) = self.proto.parameters.as_mut()
    && let Some(transfer_fee) = manage_ledger_parameters.transfer_fee
{
    nervous_system_parameters.transaction_fee_e8s = Some(transfer_fee);
}

// Polling loop remains for proposal status tracking only.
let mark_failed_at_seconds = self.env.now() + 5 * 60;
loop { ... }
```

This ensures governance's fee is always consistent with the ledger regardless of polling outcome.

## Proof of Concept

1. An SNS neuron holder submits a `ManageLedgerParameters` proposal setting `transfer_fee` from 10,000 to 100,000 e8s (as shown in the integration test at `rs/sns/integration_tests/src/manage_ledger_parameters.rs` lines 50–64).
2. The proposal is adopted by governance vote.
3. `perform_manage_ledger_parameters` executes: `upgrade_non_root_canister` returns `Ok` — ledger now enforces `transfer_fee = 100,000 e8s`.
4. The `canister_info` polling loop runs on a busy subnet; the upgrade event scrolls past the 20-change window before detection. After 5 minutes, lines 3200–3210 return `Err` — `transaction_fee_e8s` is never updated and remains `10,000 e8s`.
5. A neuron holder calls `disburse_neuron`. Governance computes `disburse_amount_e8s -= 10,000` (line 1171) and calls `transfer_funds(..., fee = 10,000, ...)` (line 1218). The ledger rejects: expected fee is 100,000 e8s. The disbursal fails; the neuron is locked.

A deterministic integration test can reproduce this by: (a) using PocketIC to simulate a busy subnet where the ledger's `canister_info` change history fills past 20 entries before the polling loop checks, or (b) mocking `env.now()` to advance past `mark_failed_at_seconds` on the first polling iteration, then asserting that `transaction_fee_e8s_or_panic()` still returns the old value and that a subsequent `disburse_neuron` call fails. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1168-1172)
```rust
        // Subtract the transaction fee from the amount to disburse since it will
        // be deducted from the source (the neuron's) account.
        if disburse_amount_e8s > transaction_fee_e8s {
            disburse_amount_e8s -= transaction_fee_e8s
        }
```

**File:** rs/sns/governance/src/governance.rs (L1214-1222)
```rust
        let block_height = self
            .ledger
            .transfer_funds(
                disburse_amount_e8s,
                transaction_fee_e8s,
                Some(from_subaccount),
                to_account,
                self.env.now(),
            )
```

**File:** rs/sns/governance/src/governance.rs (L1232-1234)
```rust
        let to_deduct = disburse_amount_e8s + transaction_fee_e8s;
        // The transfer was successful we can change the stake of the neuron.
        neuron.cached_neuron_stake_e8s = neuron.cached_neuron_stake_e8s.saturating_sub(to_deduct);
```

**File:** rs/sns/governance/src/governance.rs (L1318-1330)
```rust
        if split.amount_e8s < min_stake + transaction_fee_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                format!(
                    "Trying to split a neuron with argument {} e8s. This is too little: \
                      at the minimum, one needs the minimum neuron stake, which is {} e8s, \
                      plus the transaction fee, which is {}. Hence the minimum split amount is {}.",
                    split.amount_e8s,
                    min_stake,
                    transaction_fee_e8s,
                    min_stake + transaction_fee_e8s
                ),
            ));
```

**File:** rs/sns/governance/src/governance.rs (L3150-3156)
```rust
        self.upgrade_non_root_canister(
            ledger_canister_id,
            Wasm::Bytes(ledger_wasm),
            ledger_upgrade_arg,
            CanisterInstallMode::Upgrade,
        )
        .await?;
```

**File:** rs/sns/governance/src/governance.rs (L3188-3196)
```rust
                {
                    // success
                    // update nervous-system-parameters transaction_fee if the fee is changed.
                    if let Some(nervous_system_parameters) = self.proto.parameters.as_mut()
                        && let Some(transfer_fee) = manage_ledger_parameters.transfer_fee
                    {
                        nervous_system_parameters.transaction_fee_e8s = Some(transfer_fee);
                    }
                    return Ok(());
```

**File:** rs/sns/governance/src/governance.rs (L3200-3210)
```rust
            if self.env.now() > mark_failed_at_seconds {
                let error = format!(
                    "Upgrade marked as failed at {}. \
                     Did not find an upgrade in the ledger's canister_info recent_changes.",
                    format_timestamp_for_humans(self.env.now()),
                );
                return Err(GovernanceError::new_with_message(
                    ErrorType::External,
                    error,
                ));
            }
```

**File:** rs/sns/governance/src/governance.rs (L3368-3373)
```rust
    /// Returns the ledger's transaction fee as stored in the service nervous parameters.
    pub(crate) fn transaction_fee_e8s_or_panic(&self) -> u64 {
        self.nervous_system_parameters_or_panic()
            .transaction_fee_e8s
            .expect("NervousSystemParameters must have transaction_fee_e8s")
    }
```

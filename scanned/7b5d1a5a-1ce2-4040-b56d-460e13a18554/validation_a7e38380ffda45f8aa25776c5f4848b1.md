### Title
NNS Proposal Visibility Window Allows Front-Running of ckBTC Bitcoin Checker Blocklist Updates - (File: rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs)

### Summary
The Bitcoin Checker canister's OFAC/SDN blocklist can only be updated by upgrading the canister via an NNS governance proposal. Because NNS proposals are publicly visible for days before execution, a user whose Bitcoin address is about to be added to the blocklist can observe the pending upgrade proposal and call `retrieve_btc` to withdraw ckBTC to their sanctioned address before the new blocklist takes effect, bypassing the compliance check entirely.

### Finding Description

The Bitcoin Checker canister (`oltsj-fqaaa-aaaar-qal5q-cai`) stores the OFAC SDN blocklist as a hardcoded constant `BTC_ADDRESS_BLOCKLIST` compiled into the canister Wasm. The only mechanism to update this list is to upgrade the canister itself via an NNS proposal, as documented explicitly:

> "The list can only be modified by upgrading the Bitcoin checker canister itself, which requires an NNS proposal as the NNS is the only controller of the Bitcoin checker canister." [1](#0-0) 

The blocklist is a static sorted array checked via binary search: [2](#0-1) 

When a user calls `retrieve_btc`, the minter calls `check_address` against the Bitcoin Checker canister before allowing the withdrawal: [3](#0-2) 

The attack window is the NNS proposal voting period. Standard NNS proposals have a voting period of approximately 4 days. During this entire window, the proposal payload — including the new Wasm binary containing the updated blocklist — is publicly readable by anyone querying the NNS governance canister. A targeted user can:

1. Monitor NNS proposals for Bitcoin Checker upgrades (trivially done via the NNS dashboard or API).
2. Decode the new Wasm to identify which addresses are being added to the blocklist.
3. Before the proposal executes, call `retrieve_btc` or `retrieve_btc_with_approval` with their soon-to-be-blocked address.
4. The `check_address` call returns `Clean` (old blocklist is still active), and the withdrawal succeeds.
5. After the proposal executes, the address is blocked — but the BTC has already been sent.

The same window applies to the deposit side: a user whose deposit UTXO is about to be quarantined can call `update_balance` before the upgrade executes to mint ckBTC from a tainted UTXO. [4](#0-3) 

### Impact Explanation

A sanctioned entity holding ckBTC can evade the OFAC compliance mechanism by front-running the blocklist update. They can successfully withdraw BTC to a sanctioned Bitcoin address during the proposal voting window, defeating the purpose of the compliance check. This undermines the chain-fusion compliance guarantee that ckBTC is designed to provide. The impact is a **chain-fusion compliance bypass** — the sanctioned address receives BTC despite being on the OFAC SDN list.

### Likelihood Explanation

Medium. The attacker must:
- Hold ckBTC at the time the proposal is submitted.
- Monitor NNS proposals (trivially done via public APIs or the NNS dashboard).
- Decode the new Wasm to identify their address in the updated blocklist (requires technical capability but is feasible).
- Act within the voting window (typically 4 days — ample time).

No privileged access is required. The entry path is a standard unprivileged `retrieve_btc` ingress call.

### Recommendation

1. **Decouple the blocklist from the canister Wasm**: Store the SDN list in stable memory and expose an NNS-gated update method (e.g., callable only by the NNS root canister) that updates the list atomically without requiring a full canister upgrade. This allows the list to be updated in a single round-trip without a multi-day voting window.
2. **Reduce the proposal voting period for compliance-critical upgrades**: Use a fast-track proposal type or a shorter minimum voting period for Bitcoin Checker upgrades.
3. **Retroactive quarantine on deposit**: When a blocklist update executes, scan existing `checked_utxos` for newly-tainted UTXOs and quarantine them, preventing any pending `update_balance` calls from minting ckBTC for addresses that were clean at check time but tainted at mint time.

### Proof of Concept

1. DFINITY submits NNS proposal `X` to upgrade the Bitcoin Checker canister with a new Wasm that adds address `1ABC...` to `BTC_ADDRESS_BLOCKLIST`.
2. Attacker queries the NNS governance canister: `get_proposal_info(X)` — the full proposal payload including the new Wasm hash is publicly readable.
3. Attacker decodes the new Wasm, finds `1ABC...` in the updated `BTC_ADDRESS_BLOCKLIST`.
4. Attacker calls `retrieve_btc({ address: "1ABC...", amount: <their_balance> })` on the ckBTC minter.
5. The minter calls `check_address("1ABC...")` on the Bitcoin Checker — the old Wasm is still running, `1ABC...` is not yet in the list, response is `Clean`.
6. The minter burns the attacker's ckBTC and queues a BTC withdrawal to `1ABC...`.
7. Days later, the NNS proposal executes, upgrading the Bitcoin Checker with the new blocklist.
8. The BTC has already been sent to `1ABC...` — the compliance check was bypassed. [5](#0-4) [6](#0-5)

### Citations

**File:** rs/bitcoin/checker/README.md (L20-20)
```markdown
The Bitcoin checker canister stores a copy of the SDN list internally. The list can only be modified by upgrading the Bitcoin checker canister itself, which requires an NNS proposal as the NNS is the only controller of the Bitcoin checker canister.
```

**File:** rs/bitcoin/checker/lib/blocklist.rs (L9-11)
```rust
/// BTC is not accepted from nor sent to addresses on this list.
/// NOTE: Keep it sorted!
pub const BTC_ADDRESS_BLOCKLIST: &[&str] = &[
```

**File:** rs/bitcoin/checker/lib/blocklist.rs (L532-536)
```rust
pub fn is_blocked(address: &Address) -> bool {
    BTC_ADDRESS_BLOCKLIST
        .binary_search(&address.to_string().as_ref())
        .is_ok()
}
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L186-202)
```rust
    let btc_checker_principal = read_state(|s| s.btc_checker_principal).map(|id| id.get().into());
    let status = check_address(btc_checker_principal, args.address.clone(), runtime).await?;
    match status {
        BtcAddressCheckStatus::Tainted => {
            log!(
                Priority::Debug,
                "rejected an attempt to withdraw {} BTC to address {} due to failed Bitcoin check",
                crate::tx::DisplayAmount(args.amount),
                args.address,
            );
            return Err(RetrieveBtcError::GenericError {
                error_message: "Destination address is tainted".to_string(),
                error_code: ErrorCode::TaintedAddress as u64,
            });
        }
        BtcAddressCheckStatus::Clean => {}
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L302-318)
```rust
        let status = check_utxo(&utxo, &args, runtime).await?;
        match status {
            // Skip utxos that are already checked but has unknown mint status
            UtxoCheckStatus::CleanButMintUnknown => continue,
            UtxoCheckStatus::Clean => {
                mutate_state(|s| {
                    state::audit::mark_utxo_checked(s, utxo.clone(), caller_account, runtime)
                });
            }
            UtxoCheckStatus::Tainted => {
                mutate_state(|s| {
                    state::audit::quarantine_utxo(s, utxo.clone(), caller_account, now, runtime)
                });
                utxo_statuses.push(UtxoStatus::Tainted(utxo.clone()));
                continue;
            }
        };
```
